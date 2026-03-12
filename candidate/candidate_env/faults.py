"""
Fault injection for the on-call engineer environment.

Each fault function takes a World and mutates it to simulate a specific
incident category. It sets alerts, modifies metrics/logs, adds deploys
if relevant, and records the ground truth (root cause, correct remediation).
"""

import random
import time
from candidate_env.world import (
    World, Alert, Deploy, ServiceState, _fmt_ts, _ago,
)


# ── Fault registry ──────────────────────────────────────────────────────────

FAULT_REGISTRY: list[dict] = []


def register_fault(category: str, difficulty: str):
    def decorator(fn):
        FAULT_REGISTRY.append({
            "fn": fn,
            "category": category,
            "difficulty": difficulty,
        })
        return fn
    return decorator


def inject_fault(world: World, rng: random.Random, difficulty: str | None = None) -> World:
    """Pick and inject a fault into the world."""
    candidates = FAULT_REGISTRY
    if difficulty:
        candidates = [f for f in candidates if f["difficulty"] == difficulty]
    if not candidates:
        candidates = FAULT_REGISTRY

    fault = rng.choice(candidates)
    fault["fn"](world, rng)
    return world


# ── Helper to add fault-related logs ────────────────────────────────────────

def _add_fault_logs(world: World, service: str, lines: list[str], minutes_ago: float = 0):
    """Append fault-indicative log lines to a service's logs."""
    for line in lines:
        ts = _fmt_ts(_ago(world.now, minutes_ago + random.uniform(0, 2)))
        world.logs.setdefault(service, []).append(f"[{ts}] {line}")
    world.logs[service] = sorted(world.logs[service])


def _add_red_herring_alert(world: World, rng: random.Random):
    """Add an unrelated alert to create noise."""
    svc = rng.choice(list(world.services.keys()))
    world.alerts.append(Alert(
        severity="P3",
        service=svc,
        summary=rng.choice([
            f"{svc}: disk usage at 72% (threshold 70%)",
            f"{svc}: certificate expires in 14 days",
            f"{svc}: response time p99 slightly elevated",
            f"{svc}: log volume increased 20% from baseline",
        ]),
        fired_at=_ago(world.now, rng.uniform(30, 120)),
        is_related=False,
    ))


def _add_red_herring_deploy(world: World, rng: random.Random):
    """Add an unrelated recent deploy to create noise."""
    candidates = [s for s in world.services.values() if s.kind == "service"]
    if not candidates:
        return
    svc = rng.choice(candidates)
    world.deploys.append(Deploy(
        service=svc.name,
        version=f"v{rng.randint(1,5)}.{rng.randint(0,20)}.{rng.randint(0,10)}",
        previous_version=svc.version,
        timestamp=_ago(world.now, rng.uniform(15, 90)),
        deployed_by=rng.choice(["ci-pipeline", "jane.smith", "deploy-bot"]),
        changes=rng.choice([
            "added logging for debugging",
            "updated error messages",
            "bumped dependency version",
        ]),
        is_cause=False,
    ))


# ═════════════════════════════════════════════════════════════════════════════
# EASY FAULTS
# ═════════════════════════════════════════════════════════════════════════════

@register_fault("deploy_regression", "easy")
def fault_deploy_oom(world: World, rng: random.Random):
    """A recent deploy caused an OOM by removing cache eviction."""
    svc_name = _pick_service(world, rng, kinds=["service"])
    svc = world.services[svc_name]

    # Inject the bad deploy
    deploy_ago = rng.uniform(10, 30)
    bad_version = f"v{rng.randint(1,5)}.{rng.randint(0,20)}.{rng.randint(0,10)}"
    world.deploys.append(Deploy(
        service=svc_name,
        version=bad_version,
        previous_version=svc.version,
        timestamp=_ago(world.now, deploy_ago),
        deployed_by=rng.choice(["ci-pipeline", "alice.dev", "bob.jones"]),
        changes="refactored caching layer, removed legacy eviction policy",
        is_cause=True,
    ))
    svc.version = bad_version
    svc.last_deploy_ts = _ago(world.now, deploy_ago)

    # Mutate metrics
    svc.memory_percent = rng.uniform(92, 99)
    svc.error_rate = rng.uniform(0.15, 0.35)
    svc.healthy = False
    svc.latency_p99 = rng.uniform(2000, 8000)

    # Alert
    world.alerts.append(Alert("P1", svc_name,
        f"{svc_name}: error_rate > 5% (current: {svc.error_rate*100:.0f}%)",
        _ago(world.now, deploy_ago - 5), is_related=True))

    # Logs
    _add_fault_logs(world, svc_name, [
        f"ERROR {svc_name}: OutOfMemoryError: heap space exhausted",
        f"ERROR {svc_name}: Container killed by OOM killer (memory limit: 2Gi, used: 2.1Gi)",
        f"WARN {svc_name}: GC overhead limit exceeded, pause time 4500ms",
        f"ERROR {svc_name}: Request failed: service unavailable (OOM restart in progress)",
    ], minutes_ago=deploy_ago - 5)

    # Add a red herring alert even on easy difficulty
    _add_red_herring_alert(world, rng)

    world.fault_type = "deploy_regression"
    world.fault_root_service = svc_name
    world.fault_root_cause = (
        f"{svc_name} deploy ({bad_version}) removed cache eviction policy, "
        f"causing unbounded memory growth and OOM kills"
    )
    world.fault_mechanism = "OOM from removed cache eviction in new deploy"
    world.fault_correct_remediation = f"rollback {svc_name}"
    world.fault_requires_escalation = False


@register_fault("config_error", "easy")
def fault_config_typo(world: World, rng: random.Random):
    """A config change set an absurdly low timeout, causing cascading failures."""
    svc_name = _pick_service(world, rng, kinds=["service", "gateway"])
    svc = world.services[svc_name]

    # Bad config
    svc.config["timeout_ms"] = rng.choice([10, 50, 100])  # way too low
    svc.error_rate = rng.uniform(0.10, 0.25)
    svc.latency_p99 = rng.uniform(50, 200)  # looks ok-ish but lots of timeouts

    # Alert
    world.alerts.append(Alert("P1", svc_name,
        f"{svc_name}: error_rate > 5% (current: {svc.error_rate*100:.0f}%)",
        _ago(world.now, rng.uniform(5, 20)), is_related=True))

    _add_fault_logs(world, svc_name, [
        f"ERROR {svc_name}: Request timeout to downstream service",
        f"ERROR {svc_name}: TimeoutException: upstream call exceeded configured limit",
        f"WARN {svc_name}: Retry 1/3 failed: timeout",
        f"WARN {svc_name}: Retry 2/3 failed: timeout",
        f"ERROR {svc_name}: All retries exhausted, returning 503",
    ], minutes_ago=10)

    _add_red_herring_alert(world, rng)

    world.fault_type = "config_error"
    world.fault_root_service = svc_name
    world.fault_root_cause = (
        f"{svc_name} timeout_ms set to {svc.config['timeout_ms']}ms (too low), "
        f"causing timeouts on normal downstream requests"
    )
    world.fault_mechanism = "config timeout too low"
    world.fault_correct_remediation = f"set_config {svc_name} timeout_ms 10000"
    world.fault_requires_escalation = False


@register_fault("resource_exhaustion", "easy")
def fault_connection_pool(world: World, rng: random.Random):
    """Connection pool exhaustion from a connection leak."""
    svc_name = _pick_service(world, rng, kinds=["service"])
    svc = world.services[svc_name]

    # Bad state
    pool_max = svc.config.get("max_connections", 50)
    if isinstance(pool_max, (tuple, list)):
        pool_max = pool_max[0]
    svc.connections_active = pool_max
    svc.connections_max = pool_max
    svc.error_rate = rng.uniform(0.20, 0.40)
    svc.latency_p99 = rng.uniform(5000, 15000)

    world.alerts.append(Alert("P1", svc_name,
        f"{svc_name}: error_rate > 5% (current: {svc.error_rate*100:.0f}%)",
        _ago(world.now, rng.uniform(5, 15)), is_related=True))

    _add_fault_logs(world, svc_name, [
        f"ERROR {svc_name}: ConnectionPoolExhausted: no available connections "
        f"(pool={pool_max}, active={pool_max}, waiting={rng.randint(50, 500)})",
        f"ERROR {svc_name}: Timed out waiting for connection from pool after 30000ms",
        f"WARN {svc_name}: Connection pool utilization at 100%",
        f"ERROR {svc_name}: Request failed: could not acquire database connection",
    ], minutes_ago=8)

    world.fault_type = "resource_exhaustion"
    world.fault_root_service = svc_name
    world.fault_root_cause = (
        f"{svc_name} connection pool exhausted ({pool_max}/{pool_max} active). "
        f"Likely connection leak or insufficient pool size under current load."
    )
    world.fault_mechanism = "connection pool exhaustion"
    world.fault_correct_remediation = f"restart {svc_name}"
    world.fault_requires_escalation = False


@register_fault("expired_cert", "easy")
def fault_expired_certificate(world: World, rng: random.Random):
    """An expired TLS certificate causes connection failures."""
    svc_name = _pick_service(world, rng, kinds=["service", "gateway"])
    svc = world.services[svc_name]

    svc.error_rate = rng.uniform(0.80, 1.0)  # almost everything fails
    svc.healthy = False

    world.alerts.append(Alert("P1", svc_name,
        f"{svc_name}: health check failing",
        _ago(world.now, rng.uniform(2, 10)), is_related=True))

    _add_fault_logs(world, svc_name, [
        f"ERROR {svc_name}: TLS handshake failed: certificate has expired",
        f"ERROR {svc_name}: x509: certificate has expired or is not yet valid",
        f"ERROR {svc_name}: SSL_ERROR_EXPIRED_CERT_ALERT from downstream",
        f"ERROR {svc_name}: All incoming connections rejected: TLS error",
    ], minutes_ago=5)

    world.fault_type = "expired_cert"
    world.fault_root_service = svc_name
    world.fault_root_cause = f"{svc_name} TLS certificate has expired, rejecting all connections"
    world.fault_mechanism = "expired TLS certificate"
    world.fault_correct_remediation = f"escalate to platform team for cert renewal"
    world.fault_requires_escalation = True


# ═════════════════════════════════════════════════════════════════════════════
# MEDIUM FAULTS
# ═════════════════════════════════════════════════════════════════════════════

@register_fault("dependency_failure", "hard")
def fault_cache_stampede(world: World, rng: random.Random):
    """Cache TTL set too low causes a cache stampede, overloading the database."""
    cache_name = "redis-cache"
    db_name = "postgres-primary"
    if cache_name not in world.services or db_name not in world.services:
        # fallback
        fault_config_typo(world, rng)
        return

    cache = world.services[cache_name]
    db = world.services[db_name]

    # A service that uses cache is alerting (symptom)
    symptom_svc = _pick_service(world, rng, kinds=["service"],
                                 exclude=[cache_name, db_name])
    symptom = world.services[symptom_svc]
    symptom.latency_p99 = rng.uniform(3000, 10000)
    symptom.error_rate = rng.uniform(0.05, 0.15)

    # Root cause: cache TTL set to 1s
    cache.config["ttl_seconds"] = 1
    db.cpu_percent = rng.uniform(85, 99)
    db.connections_active = rng.randint(180, 250)
    db.latency_p50 = rng.uniform(200, 500)
    db.latency_p99 = rng.uniform(2000, 5000)

    world.alerts.append(Alert("P1", symptom_svc,
        f"{symptom_svc}: latency_p99 > 2s (current: {symptom.latency_p99:.0f}ms)",
        _ago(world.now, rng.uniform(5, 15)), is_related=True))
    world.alerts.append(Alert("P2", db_name,
        f"{db_name}: cpu_usage > 80% (current: {db.cpu_percent:.0f}%)",
        _ago(world.now, rng.uniform(3, 10)), is_related=True))

    # Add symptom service metrics that show an inflection point
    symptom.latency_p50 = rng.uniform(500, 1500)

    _add_fault_logs(world, symptom_svc, [
        f"WARN {symptom_svc}: Slow query to {db_name}: 2300ms",
        f"WARN {symptom_svc}: Cache miss for key user:profile:*, falling through to DB",
        f"WARN {symptom_svc}: High cache miss rate detected — {cache_name} returning misses for 98% of requests",
        f"ERROR {symptom_svc}: Request timeout waiting for database response",
        f"WARN {symptom_svc}: {cache_name} TTL appears very low — keys expiring almost immediately",
    ], minutes_ago=8)

    _add_fault_logs(world, cache_name, [
        f"INFO {cache_name}: Cache stats: hit_ratio=0.02, miss_ratio=0.98",
        f"INFO {cache_name}: TTL=1s, eviction_count=45000/min",
        f"WARN {cache_name}: Unusually high miss rate detected",
    ], minutes_ago=8)

    _add_fault_logs(world, db_name, [
        f"WARN {db_name}: Connection count approaching limit: {db.connections_active}/500",
        f"WARN {db_name}: Slow query log: SELECT * FROM users WHERE id = $1 (avg 450ms)",
        f"WARN {db_name}: CPU load average: {db.cpu_percent/100*db.connections_active:.0f}",
    ], minutes_ago=6)

    # Red herring
    _add_red_herring_deploy(world, rng)

    world.fault_type = "dependency_failure"
    world.fault_root_service = cache_name
    world.fault_root_cause = (
        f"{cache_name} TTL set to 1s causing ~98% miss rate (cache stampede). "
        f"All reads fall through to {db_name}, overloading it. "
        f"{symptom_svc} is slow because its DB queries are backed up."
    )
    world.fault_mechanism = "cache stampede from low TTL"
    world.fault_correct_remediation = f"set_config {cache_name} ttl_seconds 300"
    world.fault_requires_escalation = False


@register_fault("cascading_failure", "medium")
def fault_downstream_cascading(world: World, rng: random.Random):
    """A downstream service is down, causing cascading failures upstream."""
    # Pick a leaf service
    downstream = _pick_service(world, rng, kinds=["service"])
    ds = world.services[downstream]
    ds.healthy = False
    ds.error_rate = 1.0
    ds.replicas = 0

    # Find upstream services that depend on it
    upstreams = [src for src, dst, _ in world.dependencies if dst == downstream
                 and src in world.services]

    for up_name in upstreams[:3]:
        up = world.services[up_name]
        up.error_rate = rng.uniform(0.10, 0.40)
        up.latency_p99 = rng.uniform(5000, 15000)
        _add_fault_logs(world, up_name, [
            f"ERROR {up_name}: Connection refused to {downstream}:{rng.randint(8000,9000)}",
            f"ERROR {up_name}: Circuit breaker OPEN for {downstream}",
            f"WARN {up_name}: Degraded mode: skipping calls to {downstream}",
        ], minutes_ago=10)

    # Alert fires on upstream (symptom, not root cause)
    if upstreams:
        alert_svc = rng.choice(upstreams[:3])
        world.alerts.append(Alert("P1", alert_svc,
            f"{alert_svc}: error_rate > 5% (current: {world.services[alert_svc].error_rate*100:.0f}%)",
            _ago(world.now, rng.uniform(5, 12)), is_related=True))

    world.alerts.append(Alert("P2", downstream,
        f"{downstream}: health check failing (0 healthy replicas)",
        _ago(world.now, rng.uniform(8, 15)), is_related=True))

    # Include deploy info and version hint in crash logs
    bad_deploy = None
    for d in world.deploys:
        if d.service == downstream and d.is_cause:
            bad_deploy = d
    if not bad_deploy:
        bad_version = f"v{rng.randint(1,5)}.{rng.randint(0,20)}.{rng.randint(0,10)}"
        world.deploys.append(Deploy(
            service=downstream, version=bad_version,
            previous_version=ds.version,
            timestamp=_ago(world.now, rng.uniform(15, 40)),
            deployed_by="ci-pipeline",
            changes="refactored initialization logic",
            is_cause=True,
        ))
        ds.version = bad_version

    _add_fault_logs(world, downstream, [
        f"ERROR {downstream}: FATAL: unhandled exception during startup in {ds.version}",
        f"ERROR {downstream}: NullPointerException at init() — introduced in latest deploy",
        f"ERROR {downstream}: Process exited with code 1",
        f"ERROR {downstream}: CrashLoopBackOff: restarting in 30s (5 restarts in 10min)",
    ], minutes_ago=12)

    # Add runbook hint for crash-looping services
    world.runbooks[downstream] = (
        f"# Runbook: {downstream}\n\n"
        f"## CrashLoopBackOff\n"
        f"1. Check if there was a recent deploy: `get_recent_deploys`\n"
        f"2. If crash started after deploy, rollback: `run_command({downstream}, 'rollback')`\n"
        f"3. Check logs for the exception: `query_logs({downstream}, '30m', filter='error')`\n"
        f"4. If not deploy-related, check config changes and dependencies\n"
    )

    _add_red_herring_alert(world, rng)

    world.fault_type = "cascading_failure"
    world.fault_root_service = downstream
    world.fault_root_cause = (
        f"{downstream} is crash-looping (0 replicas healthy), causing cascading errors "
        f"in upstream services: {', '.join(upstreams[:3])}"
    )
    world.fault_mechanism = "downstream crash-loop causing upstream failures"
    world.fault_correct_remediation = f"rollback {downstream}"
    world.fault_requires_escalation = False


@register_fault("data_issue", "medium")
def fault_stale_cache(world: World, rng: random.Random):
    """Cache serving stale data after a failed cache invalidation."""
    cache_name = "redis-cache"
    if cache_name not in world.services:
        fault_config_typo(world, rng)
        return

    svc_name = _pick_service(world, rng, kinds=["service"],
                              exclude=[cache_name])
    svc = world.services[svc_name]

    # No errors, but users are complaining about wrong data
    world.incident_channel = [
        {"from": "support-bot", "time": _fmt_ts(_ago(world.now, 20)),
         "text": f"Multiple customer complaints: seeing outdated information on {svc_name.replace('-', ' ')} pages"},
        {"from": "product-manager", "time": _fmt_ts(_ago(world.now, 15)),
         "text": "This is affecting checkout — customers see old prices. Paging on-call."},
        {"from": "on-call-bot", "time": _fmt_ts(_ago(world.now, 10)),
         "text": f"Page: P2 — data inconsistency reported in {svc_name}. Customer-facing impact."},
    ]

    world.alerts.append(Alert("P2", svc_name,
        f"{svc_name}: customer complaints spike detected",
        _ago(world.now, 10), is_related=True))

    _add_fault_logs(world, cache_name, [
        f"ERROR {cache_name}: INVALIDATE command failed: connection reset by peer",
        f"WARN {cache_name}: Batch invalidation incomplete: 0/1500 keys invalidated",
        f"INFO {cache_name}: Cache serving stale entries (last successful invalidation: 3h ago)",
        f"WARN {cache_name}: Data freshness check failed: cache entries older than expected TTL",
    ], minutes_ago=30)

    _add_fault_logs(world, svc_name, [
        f"INFO {svc_name}: Serving cached response for /api/prices (cache hit)",
        f"INFO {svc_name}: Request processed in 5ms (cached)",
        f"WARN {svc_name}: Customer complaint correlation: stale data reports match {cache_name} serving pattern",
    ], minutes_ago=5)

    # Add postgres logs showing normal load (red herring — DB is fine)
    db_name = "postgres-primary"
    if db_name in world.services:
        _add_fault_logs(world, db_name, [
            f"INFO {db_name}: Query performance normal, no anomalies detected",
            f"INFO {db_name}: Replication lag: 0ms (healthy)",
        ], minutes_ago=5)

    _add_red_herring_alert(world, rng)

    world.fault_type = "data_issue"
    world.fault_root_service = cache_name
    world.fault_root_cause = (
        f"{cache_name} failed to invalidate stale entries 3 hours ago. "
        f"{svc_name} is serving outdated data from cache. No errors visible "
        f"because the cache is functioning — just with stale data."
    )
    world.fault_mechanism = "failed cache invalidation serving stale data"
    world.fault_correct_remediation = f"flush_cache {cache_name}"
    world.fault_requires_escalation = False


@register_fault("load_spike", "medium")
def fault_load_spike(world: World, rng: random.Random):
    """Unexpected traffic surge overwhelming a service."""
    svc_name = _pick_service(world, rng, kinds=["service", "gateway"])
    svc = world.services[svc_name]

    svc.request_rate = svc.request_rate * rng.uniform(8, 20)
    svc.cpu_percent = rng.uniform(90, 99)
    svc.memory_percent = rng.uniform(80, 95)
    svc.error_rate = rng.uniform(0.15, 0.40)
    svc.latency_p99 = rng.uniform(5000, 15000)

    world.alerts.append(Alert("P1", svc_name,
        f"{svc_name}: error_rate > 5% (current: {svc.error_rate*100:.0f}%)",
        _ago(world.now, rng.uniform(5, 15)), is_related=True))
    world.alerts.append(Alert("P2", svc_name,
        f"{svc_name}: cpu_usage > 90% (current: {svc.cpu_percent:.0f}%)",
        _ago(world.now, rng.uniform(3, 10)), is_related=True))

    _add_fault_logs(world, svc_name, [
        f"WARN {svc_name}: Request rate {svc.request_rate:.0f}/s exceeds baseline by 10x",
        f"ERROR {svc_name}: Thread pool exhausted, rejecting connections",
        f"WARN {svc_name}: Response time degraded: p99={svc.latency_p99:.0f}ms",
        f"ERROR {svc_name}: 503 Service Unavailable (capacity exceeded)",
        f"WARN {svc_name}: Rate limiter activated: dropping {rng.randint(30,60)}% of requests",
    ], minutes_ago=8)

    world.incident_channel = [
        {"from": "marketing-bot", "time": _fmt_ts(_ago(world.now, 30)),
         "text": "Heads up: flash sale campaign going live in 30 minutes"},
        {"from": "monitoring-bot", "time": _fmt_ts(_ago(world.now, 8)),
         "text": f"Traffic spike detected on {svc_name}: 10x normal volume"},
    ]

    world.fault_type = "load_spike"
    world.fault_root_service = svc_name
    world.fault_root_cause = (
        f"Unexpected traffic surge on {svc_name} (~{svc.request_rate:.0f} rps, "
        f"10x baseline) likely from marketing campaign. Service is overloaded."
    )
    world.fault_mechanism = "traffic surge exceeding capacity"
    world.fault_correct_remediation = f"scale {svc_name} 10"
    world.fault_requires_escalation = False


# ═════════════════════════════════════════════════════════════════════════════
# HARD FAULTS
# ═════════════════════════════════════════════════════════════════════════════

@register_fault("infrastructure", "hard")
def fault_network_partition(world: World, rng: random.Random):
    """Network policy change causing packet loss between AZs."""
    # Multiple services affected
    affected = rng.sample(list(world.services.keys()),
                          min(5, len(world.services)))

    for svc_name in affected:
        svc = world.services[svc_name]
        # ~10-30% of requests fail (intermittent)
        svc.error_rate = rng.uniform(0.08, 0.30)
        svc.latency_p99 = rng.uniform(2000, 8000)

    # Multiple alerts
    for svc_name in affected[:3]:
        svc = world.services[svc_name]
        world.alerts.append(Alert(
            rng.choice(["P1", "P2"]), svc_name,
            f"{svc_name}: error_rate elevated ({svc.error_rate*100:.0f}%)",
            _ago(world.now, rng.uniform(5, 20)), is_related=True))

    # The cause: a platform team deploy
    platform_deploy_ago = rng.uniform(15, 30)
    world.deploys.append(Deploy(
        service="platform-networking",
        version="v2.1.0",
        previous_version="v2.0.0",
        timestamp=_ago(world.now, platform_deploy_ago),
        deployed_by="platform-team",
        changes="updated network policies for compliance",
        is_cause=True,
    ))

    for svc_name in affected:
        _add_fault_logs(world, svc_name, [
            f"ERROR {svc_name}: Connection timed out to {rng.choice(affected)}",
            f"WARN {svc_name}: Intermittent connection failures (success rate ~{100-rng.randint(10,30)}%)",
            f"ERROR {svc_name}: i/o timeout after 5s",
        ], minutes_ago=platform_deploy_ago - 3)

    # Red herrings
    _add_red_herring_deploy(world, rng)
    _add_red_herring_alert(world, rng)

    world.fault_type = "infrastructure"
    world.fault_root_service = "platform-networking"
    world.fault_root_cause = (
        f"Network policy update by platform team (deployed {platform_deploy_ago:.0f}min ago) "
        f"is dropping packets between availability zones. Affecting services: {', '.join(affected)}. "
        f"Pattern: failures correlate with cross-AZ calls."
    )
    world.fault_mechanism = "network partition from policy change"
    world.fault_correct_remediation = "escalate to platform team"
    world.fault_requires_escalation = True


@register_fault("cascading_failure", "hard")
def fault_multi_factor_cascade(world: World, rng: random.Random):
    """Multiple simultaneous issues creating a hard-to-diagnose cascade."""
    # Factor 1: a deploy introduced a memory leak
    svc1_name = _pick_service(world, rng, kinds=["service"])
    svc1 = world.services[svc1_name]
    svc1.memory_percent = rng.uniform(88, 96)
    svc1.latency_p99 = rng.uniform(3000, 8000)
    svc1.error_rate = rng.uniform(0.05, 0.15)

    deploy_ago = rng.uniform(60, 180)  # deployed hours ago, slow leak
    bad_version = f"v{rng.randint(1,5)}.{rng.randint(0,20)}.{rng.randint(0,10)}"
    world.deploys.append(Deploy(
        service=svc1_name, version=bad_version,
        previous_version=svc1.version,
        timestamp=_ago(world.now, deploy_ago),
        deployed_by="ci-pipeline",
        changes="added new feature: user preferences caching",
        is_cause=True,
    ))
    svc1.version = bad_version

    # Factor 2: that service's degradation is causing queue backup in a worker
    worker_name = _pick_service(world, rng, kinds=["worker"])
    if worker_name:
        worker = world.services[worker_name]
        worker.queue_depth = rng.randint(10000, 50000)
        worker.error_rate = rng.uniform(0.05, 0.20)
        _add_fault_logs(world, worker_name, [
            f"ERROR {worker_name}: Failed to process message: upstream {svc1_name} returned 503",
            f"WARN {worker_name}: Queue depth growing: {worker.queue_depth}",
            f"WARN {worker_name}: Dead letter queue: {rng.randint(100, 1000)} messages",
        ], minutes_ago=30)

    # Alerts on multiple services
    world.alerts.append(Alert("P1", svc1_name,
        f"{svc1_name}: memory_usage > 85% (current: {svc1.memory_percent:.0f}%)",
        _ago(world.now, rng.uniform(5, 20)), is_related=True))
    if worker_name:
        world.alerts.append(Alert("P2", worker_name,
            f"{worker_name}: queue_depth > 10000 (current: {world.services[worker_name].queue_depth})",
            _ago(world.now, rng.uniform(3, 15)), is_related=True))

    _add_fault_logs(world, svc1_name, [
        f"WARN {svc1_name}: GC pause: {rng.randint(500, 3000)}ms",
        f"WARN {svc1_name}: Heap usage: {svc1.memory_percent:.0f}% — approaching limit",
        f"ERROR {svc1_name}: Request failed: GC overhead limit exceeded",
    ], minutes_ago=20)

    # Red herrings
    _add_red_herring_alert(world, rng)
    _add_red_herring_deploy(world, rng)

    world.fault_type = "cascading_failure"
    world.fault_root_service = svc1_name
    world.fault_root_cause = (
        f"{svc1_name} has a memory leak introduced in {bad_version} "
        f"(deployed {deploy_ago:.0f}min ago). The leak is causing GC pressure and intermittent "
        f"503s, which is backing up the {worker_name or 'downstream'} queue."
    )
    world.fault_mechanism = "memory leak causing cascading queue backup"
    world.fault_correct_remediation = f"rollback {svc1_name}"
    world.fault_requires_escalation = False


@register_fault("security", "hard")
def fault_security_incident(world: World, rng: random.Random):
    """Suspicious traffic patterns suggesting credential compromise."""
    svc_name = "auth"
    if svc_name not in world.services:
        svc_name = _pick_service(world, rng, kinds=["service"])
    svc = world.services[svc_name]

    svc.request_rate = svc.request_rate * rng.uniform(3, 8)
    svc.error_rate = rng.uniform(0.30, 0.60)  # lots of auth failures

    world.alerts.append(Alert("P1", svc_name,
        f"{svc_name}: error_rate > 5% (current: {svc.error_rate*100:.0f}%)",
        _ago(world.now, rng.uniform(5, 15)), is_related=True))
    world.alerts.append(Alert("P1", svc_name,
        f"{svc_name}: unusual login pattern detected — {rng.randint(50, 200)} "
        f"failed attempts from {rng.randint(10, 30)} unique IPs in 5min",
        _ago(world.now, rng.uniform(3, 10)), is_related=True))

    _add_fault_logs(world, svc_name, [
        f"WARN {svc_name}: Brute force detection: 150 failed logins from IP range 45.33.x.x",
        f"ERROR {svc_name}: Invalid credentials for admin@company.com (attempt {rng.randint(20, 100)})",
        f"WARN {svc_name}: Rate limit exceeded for authentication endpoint",
        f"WARN {svc_name}: Successful login for admin@company.com from unusual IP 45.33.22.{rng.randint(1,255)}",
        f"WARN {svc_name}: API key created by admin@company.com — first time in 90 days",
        f"ERROR {svc_name}: Privilege escalation attempt detected: user tried to access /admin/users",
    ], minutes_ago=10)

    world.incident_channel = [
        {"from": "security-bot", "time": _fmt_ts(_ago(world.now, 8)),
         "text": f"Anomaly detected: unusual auth patterns on {svc_name}. "
                 f"Possible credential stuffing or compromised account."},
    ]

    # Red herring: another service is also having minor issues
    _add_red_herring_alert(world, rng)

    world.fault_type = "security"
    world.fault_root_service = svc_name
    world.fault_root_cause = (
        f"Credential stuffing attack against {svc_name}. An admin account appears "
        f"compromised (successful login from unusual IP after brute force). "
        f"API key was created — possible data exfiltration."
    )
    world.fault_mechanism = "credential compromise and brute force attack"
    world.fault_correct_remediation = "escalate to security team"
    world.fault_requires_escalation = True


@register_fault("config_error", "hard")
def fault_feature_flag_interaction(world: World, rng: random.Random):
    """Two feature flags interact badly, causing subtle data corruption."""
    svc_name = _pick_service(world, rng, kinds=["service"])
    svc = world.services[svc_name]

    db_name = "postgres-primary"
    if db_name not in world.services:
        db_name = _pick_service(world, rng, kinds=["database"])

    # Low error rate — the problem is subtle
    svc.error_rate = rng.uniform(0.02, 0.08)

    svc.config["feature_flags"] = {
        "new_pricing_engine": True,
        "legacy_discount_compat": True,  # These two together cause issues
    }

    world.alerts.append(Alert("P2", svc_name,
        f"{svc_name}: error_rate slightly elevated ({svc.error_rate*100:.1f}%)",
        _ago(world.now, rng.uniform(30, 60)), is_related=True))

    world.incident_channel = [
        {"from": "support-bot", "time": _fmt_ts(_ago(world.now, 45)),
         "text": f"Customer reports: some orders have wrong discount amounts applied"},
        {"from": "data-engineer", "time": _fmt_ts(_ago(world.now, 30)),
         "text": f"Seeing inconsistent pricing data in analytics pipeline — "
                 f"might be related to the new pricing engine rollout?"},
        {"from": "product-manager", "time": _fmt_ts(_ago(world.now, 15)),
         "text": "This is getting worse. We need to figure out if it's the new pricing "
                 "engine or something else. Paging on-call."},
    ]

    _add_fault_logs(world, svc_name, [
        f"WARN {svc_name}: Pricing calculation mismatch: expected 45.99, got 39.99 for order #8821",
        f"WARN {svc_name}: Discount amount inconsistency detected in checkout flow",
        f"ERROR {svc_name}: Consistency check failed: order total does not match line items",
        f"INFO {svc_name}: Active feature flags: {list(svc.config['feature_flags'].keys())}",
    ], minutes_ago=40)

    _add_red_herring_deploy(world, rng)
    _add_red_herring_alert(world, rng)

    world.fault_type = "config_error"
    world.fault_root_service = svc_name
    world.fault_root_cause = (
        f"Feature flags 'new_pricing_engine' and 'legacy_discount_compat' are both enabled "
        f"on {svc_name}, causing double-discount application. The flags were designed to be "
        f"mutually exclusive but no guard was implemented."
    )
    world.fault_mechanism = "conflicting feature flags causing data corruption"
    world.fault_correct_remediation = f"disable_feature_flag {svc_name} legacy_discount_compat"
    world.fault_requires_escalation = False


# ── Utilities ───────────────────────────────────────────────────────────────

def _pick_service(world: World, rng: random.Random,
                  kinds: list[str] | None = None,
                  exclude: list[str] | None = None) -> str:
    """Pick a random service, optionally filtered by kind."""
    candidates = list(world.services.values())
    if kinds:
        candidates = [s for s in candidates if s.kind in kinds]
    if exclude:
        candidates = [s for s in candidates if s.name not in exclude]
    if not candidates:
        candidates = list(world.services.values())
    return rng.choice(candidates).name
