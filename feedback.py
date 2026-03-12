"""
AutoEnv Feedback Pipeline (Numeric Stats)
==========================================
This script is FIXED — the agent CANNOT modify it.

Reads rollout outputs from prime eval, computes numeric statistics and
RL readiness metrics, and appends a structured entry to the feedback log.

Qualitative feedback is handled separately by a Claude Code judge instance
launched from evaluate.sh.

Usage:
    python feedback.py \
        --config config.toml \
        --outputs-dir outputs \
        --log-file feedback_log.jsonl \
        --commit abc1234 \
        --description "Added timezone support"
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


# =============================================================================
# Rollout collection
# =============================================================================


def find_latest_rollouts(outputs_dir: Path) -> dict[str, dict]:
    """Find the most recent eval run per model under outputs_dir.

    Returns {model_name: {"metadata": dict, "rollouts": list[dict], "path": str}}
    """
    evals_dir = outputs_dir / "evals"
    if not evals_dir.exists():
        return {}

    results = {}
    for run_dir in sorted(evals_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        sub_runs = sorted(
            [d for d in run_dir.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
        )
        if not sub_runs:
            continue

        latest = sub_runs[-1]
        metadata_path = latest / "metadata.json"
        results_path = latest / "results.jsonl"

        if not metadata_path.exists() or not results_path.exists():
            continue

        with open(metadata_path) as f:
            metadata = json.load(f)

        rollouts = []
        with open(results_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rollouts.append(json.loads(line))

        model_name = metadata.get("model", run_dir.name)
        results[model_name] = {
            "metadata": metadata,
            "rollouts": rollouts,
            "path": str(latest),
        }

    return results


# =============================================================================
# Numeric statistics
# =============================================================================


def compute_model_stats(rollouts: list[dict]) -> dict:
    """Compute stats for a single model's rollouts."""
    rewards = [r.get("reward", 0.0) for r in rollouts]
    errors = [1 for r in rollouts if r.get("error")]
    completions = [r for r in rollouts if r.get("is_completed", False)]

    if not rewards:
        return {"mean_reward": 0.0, "std_reward": 0.0, "n_rollouts": 0, "n_errors": 0}

    return {
        "mean_reward": float(np.mean(rewards)),
        "median_reward": float(np.median(rewards)),
        "std_reward": float(np.std(rewards)),
        "min_reward": float(np.min(rewards)),
        "max_reward": float(np.max(rewards)),
        "n_rollouts": len(rollouts),
        "n_completed": len(completions),
        "n_errors": len(errors),
        "solve_rate": float(np.mean([1.0 if r > 0.5 else 0.0 for r in rewards])),
        "reward_distribution": {
            "0.0-0.2": int(sum(1 for r in rewards if r < 0.2)),
            "0.2-0.4": int(sum(1 for r in rewards if 0.2 <= r < 0.4)),
            "0.4-0.6": int(sum(1 for r in rewards if 0.4 <= r < 0.6)),
            "0.6-0.8": int(sum(1 for r in rewards if 0.6 <= r < 0.8)),
            "0.8-1.0": int(sum(1 for r in rewards if 0.8 <= r)),
        },
        "metrics": {},
    }


def compute_rl_readiness(all_model_stats: dict[str, dict]) -> dict:
    """Assess whether the environment is ready for RL training.

    For a single target model, the ideal starting point is:
    - Mean reward ~0.2-0.7 (enough signal to learn, room to improve)
    - Reward spread (std > 0.1) so the model sees varied outcomes
    - Non-trivial solve rate (0.1-0.8) — not all-or-nothing
    - Low error rate
    """
    stats = next(iter(all_model_stats.values()), None)
    if not stats or stats["n_rollouts"] == 0:
        return {
            "mean_reward": 0.0,
            "interpretation": "No rollouts available.",
        }

    mean = stats["mean_reward"]
    std = stats["std_reward"]
    solve_rate = stats["solve_rate"]

    issues = []
    if mean < 0.1:
        issues.append("Mean reward very low (<0.1) — environment may be too hard. Model needs to score *something* for RL to have gradient.")
    elif mean < 0.2:
        issues.append("Mean reward is low (0.1-0.2) — workable for RL but on the edge. Ideal starting range is 0.2-0.7.")
    elif mean > 0.8:
        issues.append("Mean reward very high (>0.8) — environment may be too easy. Little room for RL to improve.")
    else:
        issues.append(f"Mean reward ({mean:.2f}) is in a good range for RL training.")

    if std < 0.1:
        issues.append("Low reward variance — model gets similar scores on every task. RL needs varied outcomes.")
    else:
        issues.append(f"Reward variance is healthy (std={std:.2f}).")

    if solve_rate == 0:
        issues.append("Solve rate is 0% — model never succeeds. RL has no positive signal to reinforce.")
    elif solve_rate < 0.1:
        issues.append("Solve rate very low (<10%) — sparse positive signal for RL.")
    elif solve_rate > 0.9:
        issues.append("Solve rate very high (>90%) — little room for improvement.")

    return {
        "mean_reward": round(mean, 3),
        "std_reward": round(std, 3),
        "solve_rate": round(solve_rate, 3),
        "interpretation": " ".join(issues),
    }


def compute_verdict(current: dict, previous: dict | None) -> dict:
    """Compare current feedback against previous iteration to produce a keep/discard verdict.

    Returns {"verdict": "keep"|"discard", "reason": str}.
    The verdict is a soft signal — the agent should use judgment, not blindly obey.
    """
    if previous is None:
        return {"verdict": "keep", "reason": "First iteration — no baseline to compare against."}

    if not current.get("eval_success", True):
        return {"verdict": "discard", "reason": "Evaluation failed."}

    cur_rl = current.get("rl_readiness", {})
    prev_rl = previous.get("rl_readiness", {})
    cur_mean = cur_rl.get("mean_reward", 0)
    prev_mean = prev_rl.get("mean_reward", 0)
    cur_solve = cur_rl.get("solve_rate", 0)
    prev_solve = prev_rl.get("solve_rate", 0)

    mean_improved = cur_mean > prev_mean + 0.01
    mean_regressed = cur_mean < prev_mean - 0.01
    solve_improved = cur_solve > prev_solve + 0.02
    solve_regressed = cur_solve < prev_solve - 0.02

    reasons = []
    if mean_improved:
        reasons.append(f"mean_reward improved ({prev_mean:.3f} → {cur_mean:.3f})")
    elif mean_regressed:
        reasons.append(f"mean_reward regressed ({prev_mean:.3f} → {cur_mean:.3f})")
    else:
        reasons.append(f"mean_reward roughly flat ({prev_mean:.3f} → {cur_mean:.3f})")

    if solve_improved:
        reasons.append(f"solve_rate improved ({prev_solve:.3f} → {cur_solve:.3f})")
    elif solve_regressed:
        reasons.append(f"solve_rate regressed ({prev_solve:.3f} → {cur_solve:.3f})")

    if mean_regressed and solve_regressed:
        verdict = "discard"
    elif mean_regressed and not solve_improved:
        verdict = "discard"
    else:
        verdict = "keep"

    return {"verdict": verdict, "reason": "; ".join(reasons)}


# =============================================================================
# Main
# =============================================================================


def main():
    parser = argparse.ArgumentParser(description="AutoEnv feedback pipeline (numeric stats)")
    parser.add_argument("--config", required=True, help="Path to config.toml")
    parser.add_argument("--outputs-dir", required=True, help="Path to eval outputs")
    parser.add_argument("--log-file", required=True, help="Path to feedback log")
    parser.add_argument("--commit", default="unknown", help="Git commit hash")
    parser.add_argument("--description", default="", help="Description of changes")
    parser.add_argument("--eval-success", default="true", help="Whether evals succeeded")
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).isoformat()
    outputs_dir = Path(args.outputs_dir)

    # Collect rollouts
    all_rollouts = find_latest_rollouts(outputs_dir)

    if not all_rollouts:
        feedback = {
            "commit": args.commit,
            "timestamp": timestamp,
            "description": args.description,
            "eval_success": args.eval_success.lower() == "true",
            "error": "No rollout outputs found.",
            "stats": {},
            "rl_readiness": {},
        }
    else:
        # Compute per-model stats
        all_stats = {}
        for model_name, data in all_rollouts.items():
            stats = compute_model_stats(data["rollouts"])
            metadata = data["metadata"]
            stats["avg_reward_from_metadata"] = metadata.get("avg_reward")
            stats["avg_metrics_from_metadata"] = metadata.get("avg_metrics", {})
            stats["time_ms"] = metadata.get("time_ms")
            all_stats[model_name] = stats

        rl_readiness = compute_rl_readiness(all_stats)

        feedback = {
            "commit": args.commit,
            "timestamp": timestamp,
            "description": args.description,
            "eval_success": args.eval_success.lower() == "true",
            "stats": all_stats,
            "rl_readiness": rl_readiness,
        }

    # Read previous entry for verdict comparison
    log_path = Path(args.log_file)
    previous = None
    if log_path.exists():
        with open(log_path) as f:
            lines = f.readlines()
            if lines:
                try:
                    previous = json.loads(lines[-1].strip())
                except (json.JSONDecodeError, IndexError):
                    pass

    verdict = compute_verdict(feedback, previous)
    feedback["verdict"] = verdict

    # Append to log
    with open(log_path, "a") as f:
        f.write(json.dumps(feedback) + "\n")

    print(f"  Verdict: {verdict['verdict'].upper()} — {verdict['reason']}")
    print(f"  Feedback entry appended to {log_path}")


if __name__ == "__main__":
    main()
