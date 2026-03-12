"""
Dataset generation for the on-call engineer environment.

Generates episodes with procedural world states and injected faults.
Each episode is a complete incident scenario the agent must resolve.
"""

import json
import random
from datasets import Dataset

from candidate_env.world import generate_world, _fmt_ts
from candidate_env.faults import inject_fault, FAULT_REGISTRY


def generate_episode(seed: int) -> dict:
    """Generate a single episode (world + fault + prompt).

    Returns a dict suitable for the verifiers Dataset format:
      - prompt: initial messages
      - answer: the correct root cause (for scoring reference)
      - task: "oncall-incident"
      - info: serialized world state + fault metadata
    """
    rng = random.Random(seed)

    # Decide difficulty — 25/40/35 split balances coverage with challenge
    roll = rng.random()
    if roll < 0.25:
        difficulty = "easy"
    elif roll < 0.65:
        difficulty = "medium"
    else:
        difficulty = "hard"

    # Generate world and inject fault
    num_services = rng.randint(8, 12)
    world = generate_world(rng, num_services=num_services)
    inject_fault(world, rng, difficulty=difficulty)

    # Build the initial prompt
    prompt_text = _build_prompt(world, difficulty)

    # Serialize world for state reconstruction
    world_data = _serialize_world(world)

    return {
        "prompt": [{"role": "user", "content": prompt_text}],
        "answer": world.fault_root_cause,
        "task": "oncall-incident",
        "info": json.dumps({
            "seed": seed,
            "difficulty": difficulty,
            "fault_type": world.fault_type,
            "fault_root_service": world.fault_root_service,
            "fault_mechanism": world.fault_mechanism,
            "fault_correct_remediation": world.fault_correct_remediation,
            "fault_requires_escalation": world.fault_requires_escalation,
            "world": world_data,
        }),
    }


def generate_dataset(num_episodes: int = 50, seed: int = 42) -> Dataset:
    """Generate a dataset of episodes."""
    episodes = []
    for i in range(num_episodes):
        ep = generate_episode(seed + i)
        episodes.append(ep)
    return Dataset.from_list(episodes)


def _build_prompt(world, difficulty: str) -> str:
    """Build the initial user prompt for an episode."""
    # Collect firing alerts for the prompt
    alert_lines = []
    for a in sorted(world.alerts, key=lambda x: x.severity):
        alert_lines.append(f"[{a.severity}] {a.service}: {a.summary}")

    alerts_text = "\n".join(alert_lines) if alert_lines else "No alerts firing."

    # Time of day flavor
    hour = int(world.now % 86400 / 3600)
    if hour < 6 or hour > 22:
        time_flavor = "It's the middle of the night."
    elif hour < 9:
        time_flavor = "It's early morning."
    elif hour > 18:
        time_flavor = "It's evening."
    else:
        time_flavor = ""

    # Build incident channel snippet if available
    channel_text = ""
    if world.incident_channel:
        channel_lines = []
        for msg in world.incident_channel[-3:]:
            channel_lines.append(f"  <{msg['from']}> {msg['text']}")
        channel_text = f"\n\nIncident channel (recent messages):\n" + "\n".join(channel_lines)

    prompt = f"""You are the on-call engineer. You've been paged with the following alerts:

{alerts_text}
{channel_text}

{time_flavor}

Your job is to triage, diagnose, and remediate this incident. Use the tools available to you to investigate. Be efficient — every tool call takes time, and the clock is ticking.

Remember to:
- Investigate before acting — don't restart or rollback without understanding the problem
- Post a status update so stakeholders know what's happening
- Escalate if the issue is outside your domain
- Call resolve() when you've identified the root cause and taken action

Available tools: get_alerts, query_logs, query_metrics, get_traces, get_service_topology, get_service_status, get_recent_deploys, get_config, run_command, get_runbook, get_incident_channel, post_status_update, page_engineer, resolve"""

    return prompt.strip()


def _serialize_world(world) -> dict:
    """Serialize the world state to a JSON-compatible dict."""
    return {
        "services": {
            name: {
                "name": svc.name,
                "kind": svc.kind,
                "protocol": svc.protocol,
                "healthy": svc.healthy,
                "replicas": svc.replicas,
                "cpu_percent": svc.cpu_percent,
                "memory_percent": svc.memory_percent,
                "version": svc.version,
                "previous_version": svc.previous_version,
                "last_deploy_ts": svc.last_deploy_ts,
                "last_deploy_by": svc.last_deploy_by,
                "last_deploy_changes": svc.last_deploy_changes,
                "config": svc.config,
                "error_rate": svc.error_rate,
                "latency_p50": svc.latency_p50,
                "latency_p99": svc.latency_p99,
                "request_rate": svc.request_rate,
                "queue_depth": svc.queue_depth,
                "connections_active": svc.connections_active,
                "connections_max": svc.connections_max,
            }
            for name, svc in world.services.items()
        },
        "dependencies": world.dependencies,
        "alerts": [
            {"severity": a.severity, "service": a.service, "summary": a.summary,
             "fired_at": a.fired_at, "is_related": a.is_related}
            for a in world.alerts
        ],
        "deploys": [
            {"service": d.service, "version": d.version,
             "previous_version": d.previous_version, "timestamp": d.timestamp,
             "deployed_by": d.deployed_by, "changes": d.changes, "is_cause": d.is_cause}
            for d in world.deploys
        ],
        "logs": world.logs,
        "incident_channel": world.incident_channel,
        "runbooks": world.runbooks,
        "now": world.now,
        "fault_type": world.fault_type,
        "fault_root_cause": world.fault_root_cause,
        "fault_root_service": world.fault_root_service,
        "fault_mechanism": world.fault_mechanism,
        "fault_requires_escalation": world.fault_requires_escalation,
        "fault_correct_remediation": world.fault_correct_remediation,
    }
