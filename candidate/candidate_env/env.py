"""
On-Call Engineer Environment

A StatefulToolEnv where the agent acts as an on-call engineer, triaging
and resolving infrastructure incidents using observability and operational tools.
"""

import json
import random
from typing import Any

import verifiers as vf
from datasets import Dataset

from candidate_env.world import (
    World, ServiceState, Alert, Deploy, _fmt_ts, _ago, generate_world,
)
from candidate_env.faults import inject_fault
from candidate_env.scoring import compute_reward
from candidate_env.data_gen import generate_dataset, generate_episode
from candidate_env import tools as tool_fns


SYSTEM_PROMPT = """You are an experienced on-call engineer responsible for a microservices architecture. When incidents occur, you must efficiently triage, diagnose, and remediate issues using the tools available to you.

Your workflow:
1. Review alerts and incident context
2. Investigate using observability tools (logs, metrics, traces, topology)
3. Form a hypothesis about the root cause
4. Take targeted remediation action (restart, rollback, config change, scale, etc.)
5. Post status updates for stakeholders
6. Escalate to specialized teams when the issue is outside your domain
7. Call resolve() when done, clearly stating the root cause and what you did

Be efficient — every tool call takes simulated time. Investigate methodically but don't check every service if the evidence points to a specific cause. Diagnose before acting — blind restarts waste time and can cause collateral damage."""


class OnCallEngineerEnv(vf.StatefulToolEnv):
    def __init__(self, **kwargs):
        # We register tools with args_to_skip=["state"] so the LLM
        # doesn't see the state parameter in tool schemas.
        super().__init__(
            tools=[],  # We'll add tools after super().__init__
            max_turns=20,  # ~20 tool calls before forced stop
            **kwargs,
        )

        # Register all tools, hiding the 'state' parameter
        self.add_tool(tool_fns.get_alerts, args_to_skip=["state"])
        self.add_tool(tool_fns.query_logs, args_to_skip=["state"])
        self.add_tool(tool_fns.query_metrics, args_to_skip=["state"])
        self.add_tool(tool_fns.get_traces, args_to_skip=["state"])
        self.add_tool(tool_fns.get_service_topology, args_to_skip=["state"])
        self.add_tool(tool_fns.get_service_status, args_to_skip=["state"])
        self.add_tool(tool_fns.get_recent_deploys, args_to_skip=["state"])
        self.add_tool(tool_fns.get_config, args_to_skip=["state"])
        self.add_tool(tool_fns.run_command, args_to_skip=["state"])
        self.add_tool(tool_fns.get_runbook, args_to_skip=["state"])
        self.add_tool(tool_fns.get_incident_channel, args_to_skip=["state"])
        self.add_tool(tool_fns.post_status_update, args_to_skip=["state"])
        self.add_tool(tool_fns.page_engineer, args_to_skip=["state"])
        self.add_tool(tool_fns.resolve, args_to_skip=["state"])

    async def setup_state(self, state: vf.State) -> vf.State:
        """Initialize per-rollout state by reconstructing the world from episode info."""
        state = await super().setup_state(state)

        # Reconstruct world from the serialized info
        info = state["input"].get("info", "{}")
        if isinstance(info, str):
            info = json.loads(info)

        world_data = info.get("world", {})
        world = _reconstruct_world(world_data)

        state["world"] = world
        state["episode_info"] = info

        return state

    def update_tool_args(
        self,
        tool_name: str,
        tool_args: dict,
        messages: Any,
        state: vf.State,
        **kwargs,
    ) -> dict:
        """Inject the hidden 'state' dict into every tool call."""
        # Check time budget
        world: World = state["world"]
        if world.simulated_time_used >= 60 and tool_name != "resolve":
            # Force-end: out of time. Don't inject state — tool will get
            # a minimal state that records the timeout
            pass

        # Inject the state containing the world
        tool_args["state"] = state

        # Auto-end if agent calls resolve
        if tool_name == "resolve":
            state["is_completed"] = True

        return tool_args


def _reward_fn(completion: Any, state: vf.State, **kwargs) -> float:
    """Compute reward from the world state after the episode."""
    world: World = state.get("world")
    if world is None:
        return 0.0

    # If the agent never called resolve, give minimal credit
    if not world.resolved:
        # Check if they at least communicated or escalated
        base = 0.0
        if world.status_updates:
            base += 0.05
        if world.escalations and world.fault_requires_escalation:
            base += 0.10
        return base

    return compute_reward(world)


def _reconstruct_world(data: dict) -> World:
    """Reconstruct a World object from serialized data."""
    world = World()

    if not data:
        return world

    world.now = data.get("now", 0.0)
    world.fault_type = data.get("fault_type", "")
    world.fault_root_cause = data.get("fault_root_cause", "")
    world.fault_root_service = data.get("fault_root_service", "")
    world.fault_mechanism = data.get("fault_mechanism", "")
    world.fault_requires_escalation = data.get("fault_requires_escalation", False)
    world.fault_correct_remediation = data.get("fault_correct_remediation", "")

    # Reconstruct services
    for name, svc_data in data.get("services", {}).items():
        world.services[name] = ServiceState(
            name=svc_data["name"],
            kind=svc_data["kind"],
            protocol=svc_data["protocol"],
            healthy=svc_data.get("healthy", True),
            replicas=svc_data.get("replicas", 3),
            cpu_percent=svc_data.get("cpu_percent", 25),
            memory_percent=svc_data.get("memory_percent", 40),
            version=svc_data.get("version", "v1.0.0"),
            previous_version=svc_data.get("previous_version", "v0.9.0"),
            last_deploy_ts=svc_data.get("last_deploy_ts", 0),
            last_deploy_by=svc_data.get("last_deploy_by", ""),
            last_deploy_changes=svc_data.get("last_deploy_changes", ""),
            config=svc_data.get("config", {}),
            error_rate=svc_data.get("error_rate", 0.001),
            latency_p50=svc_data.get("latency_p50", 10),
            latency_p99=svc_data.get("latency_p99", 50),
            request_rate=svc_data.get("request_rate", 100),
            queue_depth=svc_data.get("queue_depth", 0),
            connections_active=svc_data.get("connections_active", 5),
            connections_max=svc_data.get("connections_max", 50),
        )

    # Reconstruct dependencies
    world.dependencies = [tuple(d) for d in data.get("dependencies", [])]

    # Reconstruct alerts
    for a in data.get("alerts", []):
        world.alerts.append(Alert(
            severity=a["severity"], service=a["service"],
            summary=a["summary"], fired_at=a["fired_at"],
            is_related=a.get("is_related", True),
        ))

    # Reconstruct deploys
    for d in data.get("deploys", []):
        world.deploys.append(Deploy(
            service=d["service"], version=d["version"],
            previous_version=d["previous_version"],
            timestamp=d["timestamp"], deployed_by=d["deployed_by"],
            changes=d["changes"], is_cause=d.get("is_cause", False),
        ))

    # Logs, incident channel, runbooks
    world.logs = data.get("logs", {})
    world.incident_channel = data.get("incident_channel", [])
    world.runbooks = data.get("runbooks", {})

    return world


def load_environment(num_tasks: int = 50, eval_num_tasks: int = 20, **kwargs) -> OnCallEngineerEnv:
    """Load the on-call engineer environment.

    Args:
        num_tasks: Number of training episodes to generate
        eval_num_tasks: Number of eval episodes to generate
    """
    rubric = vf.Rubric(funcs=[_reward_fn])

    train_dataset = generate_dataset(num_episodes=num_tasks, seed=42)
    eval_dataset = generate_dataset(num_episodes=eval_num_tasks, seed=9999)

    return OnCallEngineerEnv(
        dataset=train_dataset,
        eval_dataset=eval_dataset,
        rubric=rubric,
        system_prompt=SYSTEM_PROMPT,
        **kwargs,
    )
