"""
Scoring / reward computation for the on-call engineer environment.

Multi-dimensional scoring:
  - Correct diagnosis (0.30) — requires resolve()
  - Effective remediation (0.30) — scored even without resolve()
  - Efficiency (0.15)
  - Communication (0.10)
  - No collateral damage (0.10)
  - Appropriate escalation (0.05)
"""

from candidate_env.world import World


def compute_reward(world: World) -> float:
    """Compute the overall 0-1 reward for a completed episode.

    Scores all components regardless of whether resolve() was called.
    If resolve() wasn't called, diagnosis gets 0 but remediation, communication,
    collateral, and escalation are still scored based on actions taken.
    """
    diagnosis = score_diagnosis(world)
    remediation = score_remediation(world)
    efficiency = score_efficiency(world)
    communication = score_communication(world)
    collateral = score_collateral(world)
    escalation = score_escalation(world)

    reward = (
        0.30 * diagnosis +
        0.30 * remediation +
        0.15 * efficiency +
        0.10 * communication +
        0.10 * collateral +
        0.05 * escalation
    )
    return round(min(1.0, max(0.0, reward)), 4)


def score_diagnosis(world: World) -> float:
    """Score how well the agent identified the root cause.

    Full credit: correct service AND correct mechanism
    60% credit: correct mechanism, wrong trigger
    40% credit: correct service, wrong mechanism
    0%: completely wrong or didn't call resolve()

    If resolve() was not called, diagnosis scores 0 — this is the main
    incentive to call resolve(). But other components still score.
    """
    if not world.resolved:
        return 0.0

    stated = world.resolution_root_cause.lower()
    actual_service = world.fault_root_service.lower()
    actual_mechanism = world.fault_mechanism.lower()

    # Check if the agent identified the right service
    found_service = actual_service in stated

    # Check if the agent identified the mechanism (use keywords)
    mechanism_keywords = _extract_mechanism_keywords(actual_mechanism)
    found_mechanism = any(kw in stated for kw in mechanism_keywords)

    if found_service and found_mechanism:
        return 1.0
    elif found_mechanism and not found_service:
        return 0.6
    elif found_service and not found_mechanism:
        return 0.4
    else:
        # Check for partial matches — did they at least identify a related service?
        for svc_name in world.services:
            if svc_name.lower() in stated and _is_related_to_fault(world, svc_name):
                return 0.2
        return 0.0


def score_remediation(world: World) -> float:
    """Score whether the agent's actions fixed the issue.

    Looks at actions_taken and whether the correct remediation was applied.
    Scored regardless of whether resolve() was called — agents who fix the
    issue but forget to call resolve still get remediation credit.
    """
    correct_rem = world.fault_correct_remediation.lower()
    actions = world.actions_taken

    # Escalation-required scenarios: credit for escalation + bonus for mitigation
    if world.fault_requires_escalation:
        if world.escalations and _escalation_relevant(world,
                world.escalations[0].get("team", "").lower()):
            # Full credit for correct escalation, bonus if also mitigated
            base = 1.0
            return base
        elif world.escalations:
            return 0.5  # escalated to wrong team
        elif actions:
            # Took actions but didn't escalate when should have
            return 0.2
        return 0.0

    if not actions:
        return 0.0

    # Check if the correct remediation was applied
    for action in actions:
        cmd = f"{action.get('command', '')}".lower()
        svc = action.get("service", "").lower()

        # Match against correct remediation
        if _remediation_matches(correct_rem, svc, cmd):
            return 1.0

    # Partial credit: right type of action on right service
    root_svc = world.fault_root_service.lower()
    for action in actions:
        svc = action.get("service", "").lower()
        if svc == root_svc:
            return 0.5  # right service, wrong action

    return 0.1  # at least they tried something


def score_efficiency(world: World) -> float:
    """Score based on simulated time used and total tool calls.

    Penalizes both shotgun debugging (too many total calls) and
    excessive remediation actions (too many run_command calls).
    """
    time_used = world.simulated_time_used
    n_actions = len(world.actions_taken)
    n_total_calls = getattr(world, "total_tool_calls", 0)

    # Time-based score (60 min budget) — smooth curve
    if time_used <= 5:
        time_score = 1.0
    elif time_used <= 60:
        # Linear decay from 1.0 at 5min to 0.0 at 60min
        time_score = max(0.0, 1.0 - (time_used - 5) / 55)
    else:
        time_score = 0.0

    # Penalize excessive remediation actions
    if n_actions <= 2:
        action_penalty = 0.0
    elif n_actions <= 4:
        action_penalty = 0.1
    else:
        action_penalty = 0.3

    # Penalize excessive total tool calls (shotgun investigation)
    if n_total_calls <= 10:
        query_penalty = 0.0
    elif n_total_calls <= 15:
        query_penalty = 0.1
    elif n_total_calls <= 20:
        query_penalty = 0.25
    else:
        query_penalty = 0.45

    return max(0.0, time_score - action_penalty - query_penalty)


def score_communication(world: World) -> float:
    """Score status update quality.

    Requires at least one update. Multiple updates score higher (investigation
    update + remediation update). Checks that mentioned actions were actually taken.
    """
    if not world.status_updates:
        return 0.0

    n_updates = len(world.status_updates)

    # Score individual updates
    best_score = 0.0
    for update in world.status_updates:
        update_lower = update.lower()
        score = 0.2  # base credit for posting anything

        # Mentions the affected service
        if world.fault_root_service.lower() in update_lower:
            score += 0.15

        # Mentions impact with specificity — must mention a concrete service name
        impact_words = ["impact", "affect", "down", "degraded", "error", "failing",
                        "customer", "user", "slow", "timeout", "spike"]
        mentions_specific_service = any(
            svc.lower() in update_lower for svc in world.services
        )
        if any(w in update_lower for w in impact_words) and mentions_specific_service:
            score += 0.15
        elif any(w in update_lower for w in impact_words):
            score += 0.05  # generic impact statement, less credit

        # Mentions actions — but only credit if the action was actually taken
        action_map = {
            "rollback": any("rollback" in a.get("command", "") for a in world.actions_taken),
            "restart": any("restart" in a.get("command", "") for a in world.actions_taken),
            "scale": any("scale" in a.get("command", "") for a in world.actions_taken),
            "escalat": len(world.escalations) > 0,
            "flush": any("flush" in a.get("command", "") for a in world.actions_taken),
        }
        # Credit "investigating" always (it's always true during an incident)
        if "investigating" in update_lower or "triag" in update_lower:
            score += 0.15
        else:
            for action_word, was_taken in action_map.items():
                if action_word in update_lower:
                    if was_taken:
                        score += 0.15
                    # Don't credit claiming actions not taken
                    break

        # Mentions next steps
        next_words = ["eta", "next", "will", "plan", "follow up", "monitor"]
        if any(w in update_lower for w in next_words):
            score += 0.1

        best_score = max(best_score, min(1.0, score))

    # Bonus for multiple updates (investigation + remediation)
    if n_updates >= 2:
        best_score = min(1.0, best_score + 0.15)

    return best_score


def score_collateral(world: World) -> float:
    """Score for avoiding collateral damage.

    Starts at 1.0, penalized for each unnecessary/harmful action.
    """
    n_damage = len(world.collateral_damage)
    if n_damage == 0:
        return 1.0
    elif n_damage == 1:
        return 0.5
    elif n_damage == 2:
        return 0.2
    else:
        return 0.0


def score_escalation(world: World) -> float:
    """Score escalation appropriateness.

    If escalation required: full credit for escalating to right team
    If not required: full credit for not escalating
    """
    if world.fault_requires_escalation:
        if not world.escalations:
            return 0.0
        # Check if they escalated to a relevant team
        for esc in world.escalations:
            team = esc.get("team", "").lower()
            reason = esc.get("reason", "").lower()
            if _escalation_relevant(world, team):
                return 1.0
        return 0.3  # escalated but to wrong team
    else:
        if not world.escalations:
            return 1.0
        return 0.3  # unnecessary escalation


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_mechanism_keywords(mechanism: str) -> list[str]:
    """Extract searchable keywords from the fault mechanism."""
    keywords = []
    mechanism = mechanism.lower()
    keyword_map = {
        "oom": ["oom", "out of memory", "memory", "heap"],
        "timeout": ["timeout", "timed out"],
        "connection pool": ["connection pool", "connections", "pool exhausted"],
        "cache stampede": ["cache", "stampede", "ttl", "miss rate"],
        "certificate": ["cert", "tls", "ssl", "expired"],
        "crash": ["crash", "crashloop", "restart"],
        "memory leak": ["memory leak", "leak", "memory growth"],
        "network": ["network", "packet", "partition", "connectivity"],
        "security": ["credential", "brute force", "compromise", "attack"],
        "feature flag": ["feature flag", "flag", "double", "conflict"],
        "stale": ["stale", "invalidation", "stale data"],
        "config": ["config", "configuration", "timeout_ms", "ttl"],
        "load": ["load", "traffic", "surge", "capacity", "scale"],
    }
    for key, kws in keyword_map.items():
        if key in mechanism:
            keywords.extend(kws)
    if not keywords:
        keywords = mechanism.split()
    return keywords


def _is_related_to_fault(world: World, svc_name: str) -> bool:
    """Check if a service is in the dependency chain of the fault."""
    root = world.fault_root_service
    for src, dst, _ in world.dependencies:
        if (src == root and dst == svc_name) or (src == svc_name and dst == root):
            return True
    return svc_name == root


def _remediation_matches(correct: str, service: str, command: str) -> bool:
    """Check if an action matches the correct remediation."""
    # Parse correct remediation like "rollback billing" or "set_config redis-cache ttl_seconds 300"
    parts = correct.split()
    if not parts:
        return False

    correct_cmd = parts[0]
    correct_svc = parts[1] if len(parts) > 1 else ""

    # Check command type matches
    if correct_cmd not in command:
        return False

    # Check service matches
    if correct_svc and correct_svc not in service:
        return False

    # For set_config, check the key
    if correct_cmd == "set_config" and len(parts) >= 3:
        correct_key = parts[2]
        if correct_key not in command:
            return False

    return True


def _escalation_relevant(world: World, team: str) -> bool:
    """Check if the escalation target is appropriate."""
    fault_type = world.fault_type.lower()
    mechanism = world.fault_mechanism.lower()

    relevant_teams = {
        "security": ["security", "infosec", "sec"],
        "infrastructure": ["platform", "infrastructure", "infra", "network", "networking"],
        "expired_cert": ["platform", "infrastructure", "infra", "security", "cert"],
    }

    for fault_key, teams in relevant_teams.items():
        if fault_key in fault_type or fault_key in mechanism:
            if any(t in team for t in teams):
                return True

    return False
