"""
Tool implementations for the on-call engineer environment.

Each tool is a plain function that takes a `state` dict (hidden from the LLM)
plus visible arguments. The state contains the World object and tracking info.
"""

import json
import random
from typing import Any
from candidate_env.world import World, _fmt_ts


# ── Tool time costs (simulated minutes) ─────────────────────────────────────

TOOL_COSTS = {
    "get_alerts": 0.5,
    "query_logs": 1.5,
    "query_metrics": 1.0,
    "get_traces": 2.0,
    "get_service_topology": 0.5,
    "get_service_status": 1.0,
    "get_recent_deploys": 1.0,
    "get_config": 1.0,
    "run_command": 2.0,
    "get_runbook": 0.5,
    "get_incident_channel": 0.5,
    "post_status_update": 0.5,
    "page_engineer": 1.0,
    "resolve": 0.0,
}


def _tick(state: dict, tool_name: str):
    """Advance simulated time for a tool call and track call count."""
    world: World = state["world"]
    cost = TOOL_COSTS.get(tool_name, 1.0)
    world.simulated_time_used += cost
    # Track total tool call count for efficiency scoring
    if not hasattr(world, "total_tool_calls"):
        world.total_tool_calls = 0
    world.total_tool_calls += 1


def _check_duplicate_call(state: dict, tool_name: str, **kwargs) -> str | None:
    """Detect repeated identical tool calls and warn the agent."""
    call_key = f"{tool_name}:{json.dumps(kwargs, sort_keys=True, default=str)}"
    recent = state.get("_recent_calls", [])
    if recent and recent[-1] == call_key:
        return (f"Note: You just called {tool_name} with the same parameters. "
                f"Results are unchanged. Consider a different approach or "
                f"declare resolution with resolve().")
    recent.append(call_key)
    state["_recent_calls"] = recent[-5:]  # keep last 5
    return None


# ═════════════════════════════════════════════════════════════════════════════
# OBSERVABILITY TOOLS
# ═════════════════════════════════════════════════════════════════════════════

def get_alerts(state: dict) -> str:
    """Return currently firing alerts with severity, service, timestamp, and summary."""
    _tick(state, "get_alerts")
    world: World = state["world"]
    if not world.alerts:
        return "No alerts currently firing."
    lines = []
    for a in sorted(world.alerts, key=lambda x: x.severity):
        lines.append(
            f"[{a.severity}] {a.service}: {a.summary} "
            f"(fired at {_fmt_ts(a.fired_at)})"
        )
    return "\n".join(lines)


def query_logs(service: str, time_range: str = "30m", filter: str = "", state: dict = {}) -> str:
    """Query log lines from a service. Supports keyword/regex filter. Returns at most 50 lines.

    Args:
        service: Service name to query logs from
        time_range: Time range to search (e.g. '30m', '1h', '2h')
        filter: Optional keyword or regex filter (e.g. 'error', 'timeout')
    """
    _tick(state, "query_logs")
    dup = _check_duplicate_call(state, "query_logs", service=service,
                                 time_range=time_range, filter=filter)
    world: World = state["world"]
    if service not in world.services and service not in world.logs:
        return f"Service '{service}' not found."
    logs = world.logs.get(service, [])
    if filter:
        filter_lower = filter.lower()
        logs = [l for l in logs if filter_lower in l.lower()]
    if not logs:
        result = f"No logs matching filter for {service}."
    else:
        result = "\n".join(logs[-50:])
    if dup:
        result = dup + "\n\n" + result
    elapsed = f"\n[Elapsed: {world.simulated_time_used:.0f}min / 60min budget]"
    return result + elapsed


def query_metrics(service: str, metric_name: str, time_range: str = "1h", state: dict = {}) -> str:
    """Query time-series metrics for a service. Returns recent datapoints.

    Args:
        service: Service name
        metric_name: One of: latency_p99, error_rate, cpu_usage, memory_usage, request_count, queue_depth, connections_active
        time_range: Time range (e.g. '5m', '30m', '1h')
    """
    _tick(state, "query_metrics")
    dup = _check_duplicate_call(state, "query_metrics", service=service,
                                 metric_name=metric_name, time_range=time_range)
    world: World = state["world"]
    if service not in world.services:
        return f"Service '{service}' not found."
    svc = world.services[service]

    # Generate a plausible time series ending at current value
    # Use time_used as part of seed so metrics change after remediation actions
    rng = random.Random(hash(f"{service}:{metric_name}:{int(world.simulated_time_used)}"))
    current = _get_metric_value(svc, metric_name)
    if current is None:
        return f"Unknown metric '{metric_name}' for {service}."

    # Parse time range to determine number of points
    n_points = 12
    if "5m" in time_range:
        n_points = 5
    elif "30m" in time_range:
        n_points = 10
    elif "1h" in time_range:
        n_points = 12
    elif "2h" in time_range:
        n_points = 20

    # Generate series with an inflection point showing when fault started
    series = _generate_metric_series(current, n_points, rng, world, svc, metric_name)

    header = f"Metrics: {service}/{metric_name} (last {time_range})\n"
    lines = []
    for i, (ts_label, val) in enumerate(series):
        lines.append(f"  {ts_label}: {val}")
    result = header + "\n".join(lines)
    if dup:
        result = dup + "\n\n" + result
    elapsed = f"\n[Elapsed: {world.simulated_time_used:.0f}min / 60min budget]"
    return result + elapsed


def get_traces(trace_id: str, state: dict = {}) -> str:
    """Return a distributed trace showing request flow across services with latencies per span.

    Args:
        trace_id: The trace ID to look up (or 'latest' for most recent failing trace)
    """
    _tick(state, "get_traces")
    world: World = state["world"]

    # Generate a synthetic trace based on the fault
    root_svc = world.fault_root_service
    if not root_svc or root_svc not in world.services:
        return "No traces found for the given trace ID."

    # Build a trace through the dependency chain
    spans = []
    span_id = 1

    # Find a path from gateway to fault service
    gateway = "api-gateway" if "api-gateway" in world.services else list(world.services.keys())[0]
    path = _find_dependency_path(world, gateway, root_svc)
    if not path:
        path = [gateway, root_svc]

    for i, svc_name in enumerate(path):
        svc = world.services.get(svc_name)
        if not svc:
            continue
        latency = svc.latency_p99 if svc.error_rate > 0.05 else svc.latency_p50
        error = svc.error_rate > 0.10
        spans.append({
            "span_id": f"span-{span_id}",
            "parent_id": f"span-{span_id-1}" if span_id > 1 else None,
            "service": svc_name,
            "operation": f"handle_request",
            "duration_ms": round(latency, 1),
            "status": "ERROR" if error else "OK",
            "error_message": f"upstream error from {svc_name}" if error else None,
        })
        span_id += 1

    header = f"Trace {trace_id}:\n"
    lines = []
    for s in spans:
        indent = "  " * (int(s["span_id"].split("-")[1]) - 1)
        status = f" ERROR: {s['error_message']}" if s["status"] == "ERROR" else ""
        lines.append(f"{indent}[{s['span_id']}] {s['service']}.{s['operation']} "
                     f"({s['duration_ms']}ms) {s['status']}{status}")
    return header + "\n".join(lines)


def get_service_topology(state: dict = {}) -> str:
    """Return the service dependency graph showing which services call which."""
    _tick(state, "get_service_topology")
    world: World = state["world"]
    if not world.dependencies:
        return "No dependency information available."
    lines = ["Service Dependency Graph:"]
    # Group by source
    by_src: dict[str, list[tuple[str, str]]] = {}
    for src, dst, proto in world.dependencies:
        by_src.setdefault(src, []).append((dst, proto))
    for src in sorted(by_src):
        deps = by_src[src]
        dep_strs = [f"{dst} ({proto})" for dst, proto in deps]
        lines.append(f"  {src} → {', '.join(dep_strs)}")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# INFRASTRUCTURE TOOLS
# ═════════════════════════════════════════════════════════════════════════════

def get_service_status(service: str, state: dict = {}) -> str:
    """Return health status, replicas, deploy info, and resource usage for a service.

    Args:
        service: Service name to check
    """
    _tick(state, "get_service_status")
    world: World = state["world"]
    if service not in world.services:
        return f"Service '{service}' not found."
    svc = world.services[service]
    return json.dumps(svc.status_dict(), indent=2)


def get_recent_deploys(time_range: str = "24h", state: dict = {}) -> str:
    """Return recent deployments across all services.

    Args:
        time_range: How far back to look (e.g. '2h', '24h', '7d')
    """
    _tick(state, "get_recent_deploys")
    world: World = state["world"]
    if not world.deploys:
        return "No recent deployments found."

    lines = ["Recent deployments:"]
    for d in sorted(world.deploys, key=lambda x: x.timestamp, reverse=True):
        lines.append(
            f"  [{_fmt_ts(d.timestamp)}] {d.service}: "
            f"{d.previous_version} → {d.version} "
            f"(by {d.deployed_by}) — {d.changes}"
        )
    return "\n".join(lines)


def get_config(service: str, state: dict = {}) -> str:
    """Return current runtime configuration for a service.

    Args:
        service: Service name
    """
    _tick(state, "get_config")
    world: World = state["world"]
    if service not in world.services:
        return f"Service '{service}' not found."
    svc = world.services[service]
    return f"Configuration for {service}:\n{json.dumps(svc.config, indent=2)}"


def run_command(service: str, command: str, state: dict = {}) -> str:
    """Execute an operational command on a service.

    Args:
        service: Service name
        command: Command to run. Supported: 'restart', 'rollback', 'scale N', 'set_config KEY VALUE', 'flush_cache', 'drain_queue QUEUE', 'enable_circuit_breaker DOWNSTREAM', 'disable_feature_flag FLAG'
    """
    _tick(state, "run_command")
    world: World = state["world"]
    if service not in world.services:
        return f"Service '{service}' not found."

    svc = world.services[service]
    parts = command.strip().split()
    cmd = parts[0]
    args = parts[1:]

    world.actions_taken.append({"service": service, "command": command})

    if cmd == "restart":
        return _do_restart(world, svc)
    elif cmd == "rollback":
        return _do_rollback(world, svc)
    elif cmd == "scale" and args:
        return _do_scale(world, svc, int(args[0]))
    elif cmd == "set_config" and len(args) >= 2:
        return _do_set_config(world, svc, args[0], args[1])
    elif cmd == "flush_cache":
        return _do_flush_cache(world, svc)
    elif cmd == "drain_queue" and args:
        return _do_drain_queue(world, svc, args[0])
    elif cmd == "enable_circuit_breaker" and args:
        return _do_circuit_breaker(world, svc, args[0])
    elif cmd == "disable_feature_flag" and args:
        return _do_disable_flag(world, svc, args[0])
    else:
        return f"Unknown or malformed command: '{command}'. Supported: restart, rollback, scale N, set_config KEY VALUE, flush_cache, drain_queue QUEUE, enable_circuit_breaker DOWNSTREAM, disable_feature_flag FLAG"


# ═════════════════════════════════════════════════════════════════════════════
# COMMUNICATION TOOLS
# ═════════════════════════════════════════════════════════════════════════════

def get_runbook(service_or_alert: str, state: dict = {}) -> str:
    """Return the runbook for a service or alert type if one exists.

    Args:
        service_or_alert: Service name or alert type to look up
    """
    _tick(state, "get_runbook")
    world: World = state["world"]
    # Try exact match first, then partial
    if service_or_alert in world.runbooks:
        return world.runbooks[service_or_alert]
    for key, val in world.runbooks.items():
        if service_or_alert.lower() in key.lower():
            return val
    return f"No runbook found for '{service_or_alert}'."


def get_incident_channel(state: dict = {}) -> str:
    """Return recent messages from the incident Slack channel."""
    _tick(state, "get_incident_channel")
    world: World = state["world"]
    if not world.incident_channel:
        return "No recent messages in the incident channel."
    lines = []
    for msg in world.incident_channel:
        lines.append(f"[{msg['time']}] <{msg['from']}> {msg['text']}")
    return "\n".join(lines)


def post_status_update(message: str, state: dict = {}) -> str:
    """Post a status update visible to stakeholders. Should include what's happening, impact, current actions, and ETA if known.

    Args:
        message: The status update message
    """
    _tick(state, "post_status_update")
    world: World = state["world"]
    world.status_updates.append(message)
    return "Status update posted successfully."


def page_engineer(team: str, reason: str, state: dict = {}) -> str:
    """Escalate to another team with a reason.

    Args:
        team: Team to page (e.g. 'platform', 'security', 'dba', 'billing-team')
        reason: Clear reason for the escalation
    """
    _tick(state, "page_engineer")
    world: World = state["world"]
    world.escalations.append({"team": team, "reason": reason})
    return f"Paged {team} team. They have been notified with your reason."


def resolve(root_cause: str, remediation_summary: str, state: dict = {}) -> str:
    """Declare the incident resolved. Must state what went wrong and what was done.

    Args:
        root_cause: What caused the incident
        remediation_summary: What actions were taken to fix it
    """
    world: World = state["world"]
    world.resolved = True
    world.resolution_root_cause = root_cause
    world.resolution_summary = remediation_summary
    return "Incident marked as resolved."


# ═════════════════════════════════════════════════════════════════════════════
# COMMAND IMPLEMENTATIONS
# ═════════════════════════════════════════════════════════════════════════════

def _do_restart(world: World, svc) -> str:
    """Restart a service — clears transient issues but not root causes."""
    remediation = world.fault_correct_remediation
    is_root = svc.name == world.fault_root_service

    if is_root and "restart" in remediation:
        # Correct fix
        svc.error_rate = 0.002
        svc.healthy = True
        svc.latency_p99 = 50
        svc.memory_percent = 40
        svc.connections_active = 5
        return f"Service {svc.name} restarted. Health check: passing. Metrics normalizing."
    elif is_root:
        # Restart won't fix the root cause — temporary improvement
        old_err = svc.error_rate
        svc.error_rate = max(0.01, svc.error_rate * 0.5)
        svc.latency_p99 = max(100, svc.latency_p99 * 0.7)
        return (f"Service {svc.name} restarted. Error rate temporarily decreased "
                f"({old_err*100:.0f}% → {svc.error_rate*100:.0f}%). "
                f"Issue may recur if root cause is not addressed.")
    else:
        # Restarting a non-root service — potential collateral damage
        world.collateral_damage.append(f"Unnecessary restart of {svc.name}")
        return f"Service {svc.name} restarted. No issues detected — service was operating normally."


def _do_rollback(world: World, svc) -> str:
    remediation = world.fault_correct_remediation
    is_root = svc.name == world.fault_root_service

    # Check if a bad deploy exists for this service
    bad_deploy = any(d.is_cause and d.service == svc.name for d in world.deploys)

    if is_root and ("rollback" in remediation) and bad_deploy:
        svc.version = svc.previous_version
        svc.error_rate = 0.002
        svc.healthy = True
        svc.latency_p99 = 50
        svc.memory_percent = 40
        svc.connections_active = 5
        return (f"Rolled back {svc.name} to {svc.previous_version}. "
                f"Health check: passing. Error rate dropping.")
    elif bad_deploy:
        svc.version = svc.previous_version
        return f"Rolled back {svc.name} to {svc.previous_version}. No improvement in overall incident."
    else:
        world.collateral_damage.append(f"Unnecessary rollback of {svc.name}")
        return f"Rolled back {svc.name} to {svc.previous_version}. Warning: no recent deploy found that would warrant a rollback."


def _do_scale(world: World, svc, n: int) -> str:
    remediation = world.fault_correct_remediation
    is_root = svc.name == world.fault_root_service

    old_replicas = svc.replicas
    svc.replicas = n

    if is_root and "scale" in remediation:
        svc.error_rate = max(0.002, svc.error_rate * (old_replicas / max(n, 1)))
        svc.cpu_percent = max(15, svc.cpu_percent * (old_replicas / max(n, 1)))
        svc.latency_p99 = max(50, svc.latency_p99 * (old_replicas / max(n, 1)))
        return (f"Scaled {svc.name} from {old_replicas} to {n} replicas. "
                f"Load distributing. Error rate dropping.")
    else:
        return f"Scaled {svc.name} from {old_replicas} to {n} replicas."


def _do_set_config(world: World, svc, key: str, value: str) -> str:
    remediation = world.fault_correct_remediation
    is_root = svc.name == world.fault_root_service

    # Try to parse value
    try:
        parsed_value = int(value)
    except ValueError:
        try:
            parsed_value = float(value)
        except ValueError:
            parsed_value = value

    old_value = svc.config.get(key, "<not set>")
    svc.config[key] = parsed_value

    if is_root and "set_config" in remediation and key in remediation:
        svc.error_rate = 0.002
        svc.healthy = True
        svc.latency_p99 = 50
        return (f"Config updated: {svc.name}.{key} = {parsed_value} (was: {old_value}). "
                f"Metrics normalizing.")
    else:
        return f"Config updated: {svc.name}.{key} = {parsed_value} (was: {old_value})."


def _do_flush_cache(world: World, svc) -> str:
    remediation = world.fault_correct_remediation
    is_root = svc.name == world.fault_root_service

    if is_root and "flush_cache" in remediation:
        svc.error_rate = 0.002
        svc.healthy = True
        return f"Cache flushed for {svc.name}. Stale entries cleared. Metrics normalizing."
    elif svc.kind == "cache":
        return f"Cache flushed for {svc.name}. Temporary increase in miss rate expected."
    else:
        return f"Cache flushed for {svc.name}."


def _do_drain_queue(world: World, svc, queue_name: str) -> str:
    if svc.kind == "worker":
        old_depth = svc.queue_depth
        svc.queue_depth = 0
        return f"Queue '{queue_name}' drained on {svc.name}. {old_depth} messages removed."
    return f"No queue '{queue_name}' found on {svc.name}."


def _do_circuit_breaker(world: World, svc, downstream: str) -> str:
    svc.config.setdefault("circuit_breakers", {})[downstream] = True
    return (f"Circuit breaker enabled: {svc.name} → {downstream}. "
            f"Requests to {downstream} will fail fast.")


def _do_disable_flag(world: World, svc, flag: str) -> str:
    remediation = world.fault_correct_remediation
    is_root = svc.name == world.fault_root_service
    flags = svc.config.get("feature_flags", {})

    if flag in flags:
        flags[flag] = False
        if is_root and "disable_feature_flag" in remediation and flag in remediation:
            svc.error_rate = 0.002
            return (f"Feature flag '{flag}' disabled on {svc.name}. "
                    f"Metrics normalizing.")
        return f"Feature flag '{flag}' disabled on {svc.name}."
    return f"Feature flag '{flag}' not found on {svc.name}."


# ═════════════════════════════════════════════════════════════════════════════
# METRIC SERIES GENERATION
# ═════════════════════════════════════════════════════════════════════════════

def _get_metric_value(svc, metric_name: str) -> float | None:
    mapping = {
        "latency_p99": svc.latency_p99,
        "error_rate": svc.error_rate,
        "cpu_usage": svc.cpu_percent,
        "memory_usage": svc.memory_percent,
        "request_count": svc.request_rate,
        "queue_depth": svc.queue_depth,
        "connections_active": svc.connections_active,
    }
    return mapping.get(metric_name)


def _generate_metric_series(current: float, n_points: int, rng: random.Random,
                            world: World, svc, metric_name: str) -> list[tuple[str, str]]:
    """Generate a time series ending at `current` with an inflection point."""
    # Determine baseline (normal value)
    # Show inflection for ANY service with elevated metrics (not just root cause)
    is_elevated = (
        (metric_name == "error_rate" and current > 0.03) or
        (metric_name == "latency_p99" and current > 500) or
        (metric_name == "cpu_usage" and current > 70) or
        (metric_name == "memory_usage" and current > 80) or
        (metric_name == "queue_depth" and current > 1000) or
        (metric_name == "connections_active" and current > svc.connections_max * 0.8)
    )
    baseline_multiplier = {
        "latency_p99": 0.1, "error_rate": 0.01, "cpu_usage": 0.4,
        "memory_usage": 0.5, "request_count": 0.8, "queue_depth": 0.01,
        "connections_active": 0.2,
    }.get(metric_name, 0.5)

    if is_elevated and current > 0:
        baseline = max(current * baseline_multiplier, 0.001)
    else:
        baseline = current * rng.uniform(0.85, 1.15)

    # Build series: baseline for first half, ramp to current for second half
    inflection = n_points // 2
    series = []
    for i in range(n_points):
        t_label = f"T-{(n_points - i - 1) * 5}min" if i < n_points - 1 else "now"
        if i < inflection:
            val = baseline * rng.uniform(0.9, 1.1)
        else:
            progress = (i - inflection) / max(n_points - inflection - 1, 1)
            val = baseline + (current - baseline) * progress
            val *= rng.uniform(0.92, 1.08)  # noise

        # Format
        if metric_name == "error_rate":
            series.append((t_label, f"{val*100:.2f}%"))
        elif metric_name in ("cpu_usage", "memory_usage"):
            series.append((t_label, f"{val:.1f}%"))
        elif metric_name in ("latency_p99",):
            series.append((t_label, f"{val:.0f}ms"))
        elif metric_name in ("request_count",):
            series.append((t_label, f"{val:.0f}/s"))
        elif metric_name in ("queue_depth", "connections_active"):
            series.append((t_label, f"{int(max(0, val))}"))
        else:
            series.append((t_label, f"{val:.2f}"))
    return series


def _find_dependency_path(world: World, start: str, end: str) -> list[str]:
    """BFS to find a dependency path between two services."""
    if start == end:
        return [start]
    adj: dict[str, list[str]] = {}
    for src, dst, _ in world.dependencies:
        adj.setdefault(src, []).append(dst)

    visited = {start}
    queue = [[start]]
    while queue:
        path = queue.pop(0)
        node = path[-1]
        for neighbor in adj.get(node, []):
            if neighbor == end:
                return path + [neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(path + [neighbor])
    return []
