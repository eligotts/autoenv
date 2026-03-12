"""
World simulation for the on-call engineer environment.

Generates a realistic microservices architecture with services, dependencies,
configurations, metrics, logs, and traces. Each episode creates a fresh world
state, then a fault is injected.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Any


# ── Service catalog ──────────────────────────────────────────────────────────

SERVICE_CATALOG = [
    {"name": "api-gateway", "kind": "gateway", "protocol": "http"},
    {"name": "auth", "kind": "service", "protocol": "http"},
    {"name": "billing", "kind": "service", "protocol": "grpc"},
    {"name": "notifications", "kind": "service", "protocol": "async"},
    {"name": "search", "kind": "service", "protocol": "http"},
    {"name": "user-service", "kind": "service", "protocol": "http"},
    {"name": "order-service", "kind": "service", "protocol": "grpc"},
    {"name": "inventory", "kind": "service", "protocol": "grpc"},
    {"name": "payment-processor", "kind": "service", "protocol": "grpc"},
    {"name": "email-worker", "kind": "worker", "protocol": "async"},
    {"name": "analytics-pipeline", "kind": "worker", "protocol": "async"},
    {"name": "cdn-cache", "kind": "cache", "protocol": "http"},
    {"name": "redis-cache", "kind": "cache", "protocol": "tcp"},
    {"name": "postgres-primary", "kind": "database", "protocol": "tcp"},
    {"name": "postgres-replica", "kind": "database", "protocol": "tcp"},
]

# Realistic dependency patterns (from -> to)
DEPENDENCY_TEMPLATES = [
    ("api-gateway", "auth"),
    ("api-gateway", "user-service"),
    ("api-gateway", "search"),
    ("api-gateway", "order-service"),
    ("api-gateway", "billing"),
    ("auth", "redis-cache"),
    ("auth", "postgres-primary"),
    ("billing", "postgres-primary"),
    ("billing", "payment-processor"),
    ("billing", "notifications"),
    ("notifications", "email-worker"),
    ("order-service", "inventory"),
    ("order-service", "billing"),
    ("order-service", "postgres-primary"),
    ("search", "redis-cache"),
    ("search", "postgres-primary"),
    ("user-service", "postgres-primary"),
    ("user-service", "redis-cache"),
    ("inventory", "postgres-primary"),
    ("payment-processor", "postgres-primary"),
    ("analytics-pipeline", "postgres-replica"),
    ("postgres-replica", "postgres-primary"),
]


@dataclass
class ServiceState:
    name: str
    kind: str
    protocol: str
    healthy: bool = True
    replicas: int = 3
    cpu_percent: float = 25.0
    memory_percent: float = 40.0
    version: str = "v1.0.0"
    previous_version: str = "v0.9.0"
    last_deploy_ts: float = 0.0
    last_deploy_by: str = "deploy-bot"
    last_deploy_changes: str = "routine maintenance"
    config: dict = field(default_factory=dict)
    error_rate: float = 0.001
    latency_p50: float = 10.0
    latency_p99: float = 50.0
    request_rate: float = 100.0
    queue_depth: int = 0
    connections_active: int = 5
    connections_max: int = 50

    def status_dict(self) -> dict:
        return {
            "service": self.name,
            "healthy": self.healthy,
            "replicas": self.replicas,
            "version": self.version,
            "last_deploy": _fmt_ts(self.last_deploy_ts),
            "cpu_percent": round(self.cpu_percent, 1),
            "memory_percent": round(self.memory_percent, 1),
        }


@dataclass
class Alert:
    severity: str  # P1, P2, P3
    service: str
    summary: str
    fired_at: float
    is_related: bool  # whether this alert is actually related to the injected fault


@dataclass
class Deploy:
    service: str
    version: str
    previous_version: str
    timestamp: float
    deployed_by: str
    changes: str
    is_cause: bool  # whether this deploy caused the fault


@dataclass
class World:
    """Full simulated world state for one episode."""

    services: dict[str, ServiceState] = field(default_factory=dict)
    dependencies: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, protocol)
    alerts: list[Alert] = field(default_factory=list)
    deploys: list[Deploy] = field(default_factory=list)
    logs: dict[str, list[str]] = field(default_factory=dict)  # service -> log lines
    incident_channel: list[dict] = field(default_factory=list)  # slack messages
    runbooks: dict[str, str] = field(default_factory=dict)  # service_or_alert -> text

    # Fault metadata (hidden from agent)
    fault_type: str = ""
    fault_root_cause: str = ""
    fault_root_service: str = ""
    fault_mechanism: str = ""
    fault_requires_escalation: bool = False
    fault_correct_remediation: str = ""

    # Tracking
    simulated_time_used: float = 0.0
    actions_taken: list[dict] = field(default_factory=list)
    status_updates: list[str] = field(default_factory=list)
    escalations: list[dict] = field(default_factory=list)
    collateral_damage: list[str] = field(default_factory=list)
    resolved: bool = False
    resolution_root_cause: str = ""
    resolution_summary: str = ""

    # Epoch time for "now"
    now: float = 0.0


# ── Time helpers ─────────────────────────────────────────────────────────────

def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def _ago(now: float, minutes: float) -> float:
    return now - minutes * 60


# ── World generation ─────────────────────────────────────────────────────────

def generate_world(rng: random.Random, num_services: int = 12) -> World:
    """Generate a fresh world with realistic baseline state."""
    now = 1710000000.0 + rng.randint(0, 86400 * 30)  # random time in a month window
    world = World(now=now)

    # Pick services
    selected = list(SERVICE_CATALOG[:num_services])
    if num_services < len(SERVICE_CATALOG):
        # Always include core services
        core = {"api-gateway", "auth", "postgres-primary", "redis-cache"}
        selected_names = {s["name"] for s in selected}
        for s in SERVICE_CATALOG:
            if s["name"] in core and s["name"] not in selected_names:
                selected.append(s)

    for svc in selected:
        name = svc["name"]
        base_deploy = _ago(now, rng.uniform(24 * 60, 7 * 24 * 60))  # 1-7 days ago
        replicas = 1 if svc["kind"] == "database" else rng.choice([2, 3, 3, 4, 5])
        world.services[name] = ServiceState(
            name=name,
            kind=svc["kind"],
            protocol=svc["protocol"],
            replicas=replicas,
            version=f"v{rng.randint(1, 5)}.{rng.randint(0, 20)}.{rng.randint(0, 10)}",
            previous_version=f"v{rng.randint(1, 5)}.{rng.randint(0, 20)}.{rng.randint(0, 10)}",
            last_deploy_ts=base_deploy,
            last_deploy_by=rng.choice(["deploy-bot", "ci-pipeline", "jane.smith", "bob.jones"]),
            last_deploy_changes="routine maintenance",
            cpu_percent=rng.uniform(10, 40),
            memory_percent=rng.uniform(20, 55),
            latency_p50=rng.uniform(5, 30),
            latency_p99=rng.uniform(30, 100),
            request_rate=rng.uniform(50, 500),
            error_rate=rng.uniform(0.0001, 0.005),
            config=_generate_config(name, svc["kind"], rng),
        )

    # Build dependency graph from templates
    svc_names = set(world.services.keys())
    for src, dst in DEPENDENCY_TEMPLATES:
        if src in svc_names and dst in svc_names:
            proto = world.services[dst].protocol
            world.dependencies.append((src, dst, proto))

    # Generate baseline logs
    for name in world.services:
        world.logs[name] = _generate_baseline_logs(name, now, rng)

    # Add some recent deploys (normal, non-fault-causing)
    for _ in range(rng.randint(1, 3)):
        svc = rng.choice(list(world.services.values()))
        if svc.kind != "database":
            deploy_time = _ago(now, rng.uniform(6 * 60, 48 * 60))
            world.deploys.append(Deploy(
                service=svc.name,
                version=svc.version,
                previous_version=svc.previous_version,
                timestamp=deploy_time,
                deployed_by=svc.last_deploy_by,
                changes=rng.choice([
                    "dependency version bump",
                    "logging improvements",
                    "config cleanup",
                    "test coverage increase",
                ]),
                is_cause=False,
            ))

    # Runbooks for some services
    for name, svc in world.services.items():
        if rng.random() < 0.4:
            world.runbooks[name] = _generate_runbook(name, svc.kind, rng)

    return world


def _generate_config(name: str, kind: str, rng: random.Random) -> dict:
    """Generate realistic service config."""
    base = {
        "log_level": "info",
        "timeout_ms": rng.choice([5000, 10000, 15000, 30000]),
        "retry_count": rng.choice([2, 3, 5]),
        "retry_backoff_ms": rng.choice([100, 500, 1000]),
    }
    if kind == "service" or kind == "gateway":
        base["max_connections"] = rng.choice([50, 100, 200]),
        base["rate_limit_rps"] = rng.choice([1000, 5000, 10000])
        base["circuit_breaker_enabled"] = rng.choice([True, True, False])
        base["circuit_breaker_threshold"] = 0.5
    if kind == "cache":
        base["ttl_seconds"] = rng.choice([60, 300, 600, 3600])
        base["max_memory_mb"] = rng.choice([512, 1024, 2048])
    if kind == "database":
        base["max_connections"] = rng.choice([100, 200, 500])
        base["replication_lag_threshold_ms"] = 1000
    if kind == "worker":
        base["concurrency"] = rng.choice([4, 8, 16])
        base["batch_size"] = rng.choice([10, 50, 100])
        base["dead_letter_queue_enabled"] = True
    return base


def _generate_baseline_logs(service: str, now: float, rng: random.Random) -> list[str]:
    """Generate normal background logs for a service."""
    lines = []
    for i in range(rng.randint(15, 30)):
        ts = _fmt_ts(_ago(now, rng.uniform(0, 60)))
        level = rng.choices(["INFO", "DEBUG", "WARN"], weights=[80, 15, 5])[0]
        msg = rng.choice([
            "Health check passed",
            f"Request processed in {rng.randint(5, 80)}ms",
            "Connection pool stats: active=3 idle=17 total=20",
            "Scheduled job completed successfully",
            f"Cache hit ratio: {rng.uniform(0.85, 0.99):.2f}",
            "GC pause: 12ms",
            f"Processed {rng.randint(100, 1000)} messages in batch",
            "TLS certificate valid for 45 days",
            "Config reload: no changes detected",
        ])
        lines.append(f"[{ts}] {level} {service}: {msg}")
    return sorted(lines)


def _generate_runbook(service: str, kind: str, rng: random.Random) -> str:
    """Generate a runbook for a service."""
    base = f"# Runbook: {service}\n\n"
    if kind == "service" or kind == "gateway":
        base += "## High Error Rate\n"
        base += "1. Check recent deploys: `get_recent_deploys`\n"
        base += "2. Check logs for exceptions: `query_logs` with filter='error'\n"
        base += "3. If deploy caused it, rollback: `run_command(service, 'rollback')`\n"
        base += "4. If not deploy-related, check downstream dependencies\n\n"
        base += "## High Latency\n"
        base += "1. Check CPU/memory usage\n"
        base += "2. Check connection pool utilization\n"
        base += "3. Check downstream service health\n"
        base += "4. Consider scaling up if load-related\n"
    elif kind == "database":
        base += "## Connection Pool Exhaustion\n"
        base += "1. Check which services have most connections\n"
        base += "2. Look for connection leaks in recent deploys\n"
        base += "3. Consider increasing max_connections (carefully)\n\n"
        base += "## Replication Lag\n"
        base += "1. Check write volume on primary\n"
        base += "2. Check replica CPU/IO\n"
        base += "3. Escalate to DBA team if lag > 5s\n"
    elif kind == "cache":
        base += "## Cache Miss Rate Spike\n"
        base += "1. Check if TTL was changed\n"
        base += "2. Check memory usage (evictions?)\n"
        base += "3. Flush cache if data is stale: `run_command(service, 'flush_cache')`\n"
    elif kind == "worker":
        base += "## Queue Depth Growing\n"
        base += "1. Check worker error rate\n"
        base += "2. Check if workers are running\n"
        base += "3. Scale workers: `run_command(service, 'scale N')`\n"
        base += "4. Check dead letter queue for poisoned messages\n"
    return base
