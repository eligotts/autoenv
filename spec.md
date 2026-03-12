# Environment Spec: On-Call Engineer

## Task description

The agent is an on-call engineer at a mid-size company running a microservices architecture. Each episode begins with an incoming incident: an alert fires, a customer complaint arrives, or a downstream team pages the on-call. The agent must triage, diagnose, and remediate the issue using the tools available — just as a real on-call engineer would during a shift.

The simulated world consists of 8–15 interconnected services (API gateway, auth, billing, notifications, search, worker queues, databases, caches, CDN) running across multiple hosts. Each episode generates a concrete infrastructure state — which services are running, their config, recent deploys, current load — and then injects a fault. The agent sees only what a real engineer would see: alerts, logs, metrics, and tool output. It does not see the fault directly.

The core loop is: **alert → triage → investigate → diagnose → remediate → verify → communicate**.

## Tools

The agent has access to the following tools, modeled after real infrastructure tooling:

### Observability
- `get_alerts()` — returns currently firing alerts with severity, service, timestamp, and summary
- `query_logs(service, time_range, filter=None)` — returns log lines from a service (supports keyword/regex filter). Returns at most 50 lines to simulate real log search.
- `query_metrics(service, metric_name, time_range)` — returns time-series data (e.g., latency_p99, error_rate, cpu_usage, memory_usage, request_count, queue_depth). Returns last N datapoints as a list.
- `get_traces(trace_id)` — returns a distributed trace showing request flow across services with latencies per span
- `get_service_topology()` — returns the dependency graph: which services call which, and over what protocol (HTTP, gRPC, async queue)

### Infrastructure
- `get_service_status(service)` — returns health check status, current replicas, last deploy timestamp, current version, and resource usage
- `get_recent_deploys(time_range)` — returns recent deployments across all services (who, what, when, what changed)
- `get_config(service)` — returns current runtime configuration (feature flags, connection strings with hosts redacted, timeouts, retry policies, rate limits)
- `run_command(service, command)` — execute an operational command on a service. Supported commands:
  - `restart` — restart the service
  - `rollback` — roll back to the previous deployed version
  - `scale <N>` — set replica count
  - `set_config <key> <value>` — update a runtime config value
  - `flush_cache` — clear the service's cache
  - `drain_queue <queue_name>` — drain a dead-letter or stuck queue
  - `enable_circuit_breaker <downstream>` — enable circuit breaker to a downstream dependency
  - `disable_feature_flag <flag>` — disable a feature flag

### Communication
- `get_runbook(service_or_alert)` — returns the runbook for a service or alert type if one exists (sometimes returns nothing — not every scenario has a runbook)
- `get_incident_channel()` — returns recent messages from the incident Slack channel (may contain chatter from other engineers, red herrings, or useful context)
- `post_status_update(message)` — post a status update visible to stakeholders. Should include: what's happening, impact, current actions, ETA if known.
- `page_engineer(team, reason)` — escalate to another team with a reason. Appropriate when the issue is outside the on-call's domain.

## Constraints and rules

1. **Tool cost**: Every tool call takes simulated time (30s–2min depending on the tool). Total episode time is capped at 60 minutes simulated. The agent should be efficient — shotgun debugging (calling every tool on every service) wastes time and is penalized.
2. **Do no harm**: Incorrect remediations can make things worse. Rolling back a healthy service, restarting something mid-migration, or draining a queue that's processing normally all cause additional damage. The agent should diagnose before acting.
3. **Runbooks are not always available or correct**: Some alerts have runbooks, some don't. Some runbooks are outdated. The agent must use judgment.
4. **Red herrings exist**: Not every alert or log anomaly is related to the incident. Correlated but non-causal signals are common (e.g., a deploy happened recently but didn't cause the issue; a different service is also alerting but from an unrelated pre-existing issue).
5. **Communication is required**: The agent must post at least one status update. Real on-call engineers who fix things silently are doing it wrong — stakeholders need to know what's happening.
6. **Escalation is sometimes correct**: Some incidents require expertise the on-call doesn't have (e.g., database corruption, security breach). Correctly escalating with a clear reason is better than flailing.
7. **The agent must explicitly declare resolution**: It calls a `resolve(root_cause, remediation_summary)` action to end the episode, stating what went wrong and what it did about it.

## Scoring

Scoring is multi-dimensional, combined into a single 0–1 reward:

| Component | Weight | Description |
|---|---|---|
| **Correct diagnosis** | 0.35 | Did the agent identify the actual root cause? Partial credit for identifying the right service but wrong mechanism, or right symptom but not root cause. |
| **Effective remediation** | 0.25 | Did the agent's actions actually fix (or appropriately mitigate) the issue? Partial credit for partial fixes. Zero if remediation made things worse. |
| **Efficiency** | 0.15 | How much simulated time was used? Fewer, more targeted tool calls score higher. Penalizes both shotgun debugging and analysis paralysis. |
| **Communication** | 0.10 | Did the agent post clear, accurate status updates? Were stakeholders kept informed? Scored on presence, accuracy, and clarity. |
| **No collateral damage** | 0.10 | Did the agent avoid making things worse? Unnecessary restarts, wrong rollbacks, or misapplied config changes are penalized. |
| **Appropriate escalation** | 0.05 | If escalation was the right call, did the agent escalate? If not needed, did the agent avoid unnecessary escalation? |

### Reward shaping details

- **Diagnosis partial credit**: Identifying the correct failing service but not the mechanism (e.g., "billing is down" but not "billing's connection pool is exhausted due to a leaked connection in the new deploy") gets 40% of the diagnosis score. Getting the mechanism right but attributing it to the wrong trigger gets 60%.
- **Remediation**: Full credit requires the issue to actually be resolved. A rollback that fixes the issue gets full credit even if the agent doesn't understand the root code bug — operational pragmatism is rewarded. Making things worse is scored as 0 for this component AND penalizes the collateral damage component.
- **Efficiency**: Scored as a curve — fast and correct is best, but a slow correct diagnosis beats a fast wrong one (the other components dominate). The penalty ramps up sharply past 40 minutes simulated time.

## Difficulty levels

### Easy (30% of episodes)
- Single service affected, clear causal chain
- Alert directly points to the problematic service
- Runbook exists and is accurate
- Root cause is a common pattern (OOM, deploy regression, config typo, expired cert)
- Example: Auth service OOM-killed after a deploy that removed a cache eviction policy. Alert says "auth: high error rate." Logs show OOM kills. Recent deploys show auth was deployed 20 min ago. Rollback fixes it.

### Medium (45% of episodes)
- 2–3 services involved, requires tracing a dependency chain
- Alert may fire on a symptom service, not the root cause service
- Runbook may be missing or partially relevant
- Requires correlating logs, metrics, and traces across services
- Red herring present (unrelated alert or recent deploy)
- Example: Search is slow. Traces show search → cache → miss → database, but the database is fine. The real issue: a config change to the cache service lowered TTLs to 1s, causing a cache stampede. The cache service's metrics show 50x the normal miss rate. Fixing the TTL config resolves it.

### Hard (25% of episodes)
- Cascading failure across 3+ services
- Root cause is non-obvious or multi-factorial
- May involve race conditions, subtle config interactions, or infrastructure-level issues (DNS, network partition, clock skew)
- Multiple alerts firing, multiple red herrings
- May require escalation as the correct action
- Example: Intermittent 502s across all services. API gateway error rate spiked. Multiple services show increased latency. Root cause: a network policy change (by the platform team, visible in deploys) is dropping 10% of packets between two availability zones. The on-call should identify the pattern (failures correlate with cross-AZ calls), escalate to the platform team with evidence, and enable circuit breakers as a mitigation.

## Incident categories

To ensure coverage, episodes should be drawn from these categories:

1. **Deploy regression** — new code introduces a bug, memory leak, or performance regression
2. **Configuration error** — bad config change (timeouts, feature flags, rate limits, connection strings)
3. **Resource exhaustion** — disk full, connection pool exhausted, OOM, queue backup
4. **Dependency failure** — downstream service or external dependency is degraded or down
5. **Cascading failure** — one failure triggers failures in dependent services
6. **Data issue** — corrupted cache, stale data, replication lag
7. **Infrastructure** — DNS, networking, certificate expiry, clock skew
8. **Load spike** — unexpected traffic surge overwhelming capacity
9. **Security incident** — suspicious traffic patterns, credential compromise (correct action is escalation)

## Example episode walkthrough

**Setup**: 12-service architecture. It's 2:30 AM. Alert fires:

```
ALERT [P1] billing: error_rate > 5% (current: 23%)
ALERT [P2] notifications: queue_depth > 10000 (current: 47,832)
```

**Good solution**:

1. `get_alerts()` → sees both alerts, notes billing is P1
2. `get_service_topology()` → sees billing → postgres, billing → notifications (async via queue)
3. `query_metrics("billing", "error_rate", "1h")` → error rate spiked 18 min ago from 0.1% to 23%
4. `get_recent_deploys("2h")` → billing v2.14.1 deployed 20 min ago
5. `query_logs("billing", "20m", filter="error|exception")` → `ConnectionPoolExhausted: no available connections (pool=20, active=20, waiting=347)`
6. `get_config("billing")` → connection pool max is 20 (was 50 in previous version — visible in deploy diff)
7. `post_status_update("Investigating P1 billing error rate spike. Correlated with billing deploy 20min ago. Connection pool exhaustion — likely config regression in v2.14.1. Notifications queue backup is downstream impact. Working on remediation.")`
8. `run_command("billing", "rollback")` → rolls back to v2.14.0
9. `query_metrics("billing", "error_rate", "5m")` → error rate dropping, now 2% and falling
10. `query_metrics("notifications", "queue_depth", "5m")` → queue draining
11. `resolve(root_cause="billing v2.14.1 reduced connection pool from 50 to 20, causing pool exhaustion under normal load. Notifications queue backup was downstream effect of billing errors.", remediation_summary="Rolled back billing to v2.14.0. Error rate recovering. Notifications queue draining. Will follow up with billing team on the config regression.")`

**Scoring**: Correct root cause (0.35), effective fix (0.25), efficient investigation — 11 tool calls, direct path (0.13/0.15), good status update (0.10), no collateral damage (0.10), no unnecessary escalation (0.05) = **0.98**

## Implementation notes

- The world state (services, configs, fault) should be generated procedurally per episode so the agent can't memorize solutions.
- Logs and metrics should look realistic — include normal noise, not just the fault signal.
- Time-series metrics should show a clear inflection point when the fault was injected, but with enough noise that it requires attention to spot.
- Traces should be structurally realistic (proper parent-child spans, realistic latency distributions).
- The agent's tool calls should modify the world state (e.g., a restart actually clears the issue if the issue was transient, a rollback reverts to the previous config).
