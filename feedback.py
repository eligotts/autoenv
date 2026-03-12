"""
AutoEnv Feedback Pipeline (Numeric Stats)
==========================================
This script is FIXED — the agent CANNOT modify it.

Reads rollout outputs from prime eval, computes numeric statistics and
RL readiness metrics, and appends a structured entry to the feedback log.

Evaluates two models:
  - Target model: the model we intend to train with RL (aim for 0.2-0.7 mean reward)
  - Strong model: a capable model used to verify tasks are solvable (should score >0.7)

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
import tomllib
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


def compute_rl_readiness(target_stats: dict | None) -> dict:
    """Assess whether the environment is ready for RL training based on target model.

    For the target model, the ideal starting point is:
    - Mean reward ~0.2-0.7 (enough signal to learn, room to improve)
    - Reward spread (std > 0.1) so the model sees varied outcomes
    - Non-trivial solve rate (0.1-0.8) — not all-or-nothing
    - Low error rate
    """
    if not target_stats or target_stats["n_rollouts"] == 0:
        return {
            "mean_reward": 0.0,
            "interpretation": "No rollouts available.",
        }

    mean = target_stats["mean_reward"]
    std = target_stats["std_reward"]
    solve_rate = target_stats["solve_rate"]

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


def compute_strong_model_assessment(strong_stats: dict | None) -> dict:
    """Assess whether the strong model can solve the tasks.

    The strong model should score high (>0.7). If it can't solve the tasks,
    they're too hard or the scoring is broken.
    """
    if not strong_stats or strong_stats["n_rollouts"] == 0:
        return {
            "mean_reward": 0.0,
            "interpretation": "No strong model rollouts available.",
        }

    mean = strong_stats["mean_reward"]
    std = strong_stats["std_reward"]
    solve_rate = strong_stats["solve_rate"]

    issues = []
    if mean < 0.3:
        issues.append(f"Strong model mean reward very low ({mean:.2f}) — tasks may be too hard, poorly designed, or scoring may be broken. A strong model should be able to solve most tasks.")
    elif mean < 0.5:
        issues.append(f"Strong model mean reward low ({mean:.2f}) — many tasks are unsolvable even for a strong model. Review task generation and scoring.")
    elif mean < 0.7:
        issues.append(f"Strong model mean reward moderate ({mean:.2f}) — approaching acceptable but ideally should be >0.7. Some tasks may still be too hard or scoring too strict.")
    else:
        issues.append(f"Strong model mean reward good ({mean:.2f}) — tasks are solvable and scoring appears fair.")

    if solve_rate < 0.5:
        issues.append(f"Strong model solve rate low ({solve_rate:.0%}) — too many tasks are unsolvable.")
    elif solve_rate < 0.7:
        issues.append(f"Strong model solve rate moderate ({solve_rate:.0%}) — some tasks may need adjustment.")
    else:
        issues.append(f"Strong model solve rate healthy ({solve_rate:.0%}).")

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

    # Also check strong model
    cur_strong = current.get("strong_model", {})
    prev_strong = previous.get("strong_model", {})
    cur_strong_mean = cur_strong.get("mean_reward", 0)
    prev_strong_mean = prev_strong.get("mean_reward", 0)

    mean_improved = cur_mean > prev_mean + 0.01
    mean_regressed = cur_mean < prev_mean - 0.01
    solve_improved = cur_solve > prev_solve + 0.02
    solve_regressed = cur_solve < prev_solve - 0.02
    strong_regressed = cur_strong_mean < prev_strong_mean - 0.05

    reasons = []
    if mean_improved:
        reasons.append(f"target mean_reward improved ({prev_mean:.3f} → {cur_mean:.3f})")
    elif mean_regressed:
        reasons.append(f"target mean_reward regressed ({prev_mean:.3f} → {cur_mean:.3f})")
    else:
        reasons.append(f"target mean_reward roughly flat ({prev_mean:.3f} → {cur_mean:.3f})")

    if solve_improved:
        reasons.append(f"target solve_rate improved ({prev_solve:.3f} → {cur_solve:.3f})")
    elif solve_regressed:
        reasons.append(f"target solve_rate regressed ({prev_solve:.3f} → {cur_solve:.3f})")

    if strong_regressed:
        reasons.append(f"strong model regressed ({prev_strong_mean:.3f} → {cur_strong_mean:.3f})")

    if mean_regressed and solve_regressed:
        verdict = "discard"
    elif mean_regressed and not solve_improved:
        verdict = "discard"
    elif strong_regressed and not mean_improved:
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

    # Read config to identify target and strong models
    with open(args.config, "rb") as f:
        config = tomllib.load(f)
    target_model = config["eval"]["target_model"]
    strong_model = config["eval"]["strong_model"]

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
            "strong_model": {},
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

        # Find target and strong model stats by matching model names
        target_stats = None
        strong_stats = None
        for model_name, stats in all_stats.items():
            if target_model in model_name or model_name in target_model:
                target_stats = stats
            if strong_model in model_name or model_name in strong_model:
                strong_stats = stats

        rl_readiness = compute_rl_readiness(target_stats)
        strong_assessment = compute_strong_model_assessment(strong_stats)

        feedback = {
            "commit": args.commit,
            "timestamp": timestamp,
            "description": args.description,
            "eval_success": args.eval_success.lower() == "true",
            "stats": all_stats,
            "rl_readiness": rl_readiness,
            "strong_model": strong_assessment,
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
