"""
AutoEnv Feedback Pipeline
=========================
This script is FIXED — the agent CANNOT modify it.

Reads rollout outputs from vf-eval, computes numeric statistics,
runs LLM-as-judge for qualitative feedback, and appends a structured
feedback entry to the feedback log.

Usage:
    python feedback.py \
        --spec spec.md \
        --config config.toml \
        --outputs-dir candidate/outputs \
        --log-file feedback_log.jsonl \
        --commit abc1234 \
        --budget small \
        --description "Added timezone support"
"""

import argparse
import json
import os
import random
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from openai import OpenAI


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
        # run_dir name format: {env_id}--{model_with_dashes}/{run_uuid}
        # Find the latest run_uuid subdir
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


def compute_sweet_spot(all_model_stats: dict[str, dict]) -> dict:
    """Compute RL sweet-spot metrics across models.

    Returns a dict with numeric indicators of how well the environment
    discriminates across model capabilities.
    """
    means = [s["mean_reward"] for s in all_model_stats.values() if s["n_rollouts"] > 0]

    if len(means) < 2:
        return {
            "sweet_spot_score": 0.0,
            "overall_mean": means[0] if means else 0.0,
            "spread": 0.0,
            "center_distance": 1.0,
            "interpretation": "Not enough models to compute sweet spot.",
        }

    overall_mean = float(np.mean(means))
    spread = float(np.std(means))

    # How close is the overall mean to 0.5? (ideal for RL)
    center_distance = abs(overall_mean - 0.5)
    center_score = max(0.0, 1.0 - center_distance * 2)

    # How much spread between models? (want high spread)
    spread_score = min(spread / 0.25, 1.0)

    # Are any models at floor (< 0.05) or ceiling (> 0.95)?
    n_extreme = sum(1 for m in means if m < 0.05 or m > 0.95)
    extreme_penalty = n_extreme / len(means)

    sweet_spot_score = 0.4 * spread_score + 0.4 * center_score + 0.2 * (1 - extreme_penalty)

    # Interpretation
    if overall_mean < 0.1:
        interp = "Environment appears too hard — most models score near zero."
    elif overall_mean > 0.9:
        interp = "Environment appears too easy — most models score near perfect."
    elif spread < 0.05:
        interp = "Models perform similarly — environment doesn't discriminate well across capability levels."
    elif sweet_spot_score > 0.7:
        interp = "Good discrimination across models. Scores spread well around the RL sweet spot."
    else:
        interp = f"Moderate discrimination. Mean={overall_mean:.2f}, spread={spread:.2f}."

    return {
        "sweet_spot_score": round(sweet_spot_score, 3),
        "overall_mean": round(overall_mean, 3),
        "spread": round(spread, 3),
        "center_distance": round(center_distance, 3),
        "model_means": {k: round(v["mean_reward"], 3) for k, v in all_model_stats.items()},
        "interpretation": interp,
    }


# =============================================================================
# LLM Judge
# =============================================================================


def create_judge_client(config: dict) -> OpenAI:
    """Create an OpenAI client for judge calls."""
    judge_cfg = config["judge"]
    api_key = os.environ.get(judge_cfg["api_key_var"], "")
    if not api_key:
        print(
            f"WARNING: {judge_cfg['api_key_var']} not set. Judge feedback will be skipped.",
            file=sys.stderr,
        )
        return None
    return OpenAI(base_url=judge_cfg["base_url"], api_key=api_key)


def sample_rollouts_for_judge(
    all_rollouts: dict[str, dict], num_samples: int
) -> list[dict]:
    """Sample rollouts stratified by score (high/mid/low) and model."""
    sampled = []
    for model_name, data in all_rollouts.items():
        rollouts = data["rollouts"]
        if not rollouts:
            continue

        # Sort by reward and pick from low/mid/high
        sorted_rollouts = sorted(rollouts, key=lambda r: r.get("reward", 0))
        n = len(sorted_rollouts)
        # Pick ~equal from each tercile
        per_tercile = max(1, num_samples // (3 * len(all_rollouts)))

        indices = set()
        # Low
        indices.update(range(min(per_tercile, n)))
        # Mid
        mid_start = max(0, n // 2 - per_tercile // 2)
        indices.update(range(mid_start, min(mid_start + per_tercile, n)))
        # High
        indices.update(range(max(0, n - per_tercile), n))

        for i in indices:
            r = sorted_rollouts[i]
            sampled.append(
                {
                    "model": model_name,
                    "reward": r.get("reward", 0),
                    "completion": r.get("completion", []),
                    "prompt": r.get("prompt", []),
                    "task": r.get("task", ""),
                    "metrics": r.get("metrics", {}),
                    "is_completed": r.get("is_completed", False),
                    "is_truncated": r.get("is_truncated", False),
                }
            )

    # Trim to target
    if len(sampled) > num_samples:
        random.shuffle(sampled)
        sampled = sampled[:num_samples]

    return sampled


def format_rollout_for_judge(rollout: dict, index: int) -> str:
    """Format a single rollout for inclusion in a judge prompt."""
    lines = [f"--- Rollout {index + 1} (model: {rollout['model']}, reward: {rollout['reward']:.3f}) ---"]

    # Include both prompt and completion to show full trajectory
    all_messages = []
    prompt_msgs = rollout.get("prompt", [])
    completion_msgs = rollout.get("completion", [])
    if completion_msgs:
        all_messages = completion_msgs  # completion usually includes the full trajectory
    elif prompt_msgs:
        all_messages = prompt_msgs

    for msg in all_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        # Handle tool_calls in assistant messages
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                lines.append(f"[{role} → tool call]: {fn.get('name', '?')}({fn.get('arguments', '')})")
            if not content:
                continue

        # Truncate very long messages
        if isinstance(content, str) and len(content) > 2000:
            content = content[:2000] + "\n... [truncated]"
        elif isinstance(content, list):
            # Handle multimodal content
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
            content = "\n".join(text_parts)
            if len(content) > 2000:
                content = content[:2000] + "\n... [truncated]"
        lines.append(f"[{role}]: {content}")

    # Show metrics if present
    if rollout.get("metrics"):
        lines.append(f"[metrics]: {json.dumps(rollout['metrics'])}")

    lines.append(f"--- End Rollout {index + 1} ---")
    return "\n".join(lines)


def format_rollouts_within_budget(
    sampled_rollouts: list[dict], max_total_chars: int
) -> str:
    """Format rollouts for judge, truncating if total would exceed char budget."""
    formatted = []
    total_chars = 0
    for i, r in enumerate(sampled_rollouts):
        text = format_rollout_for_judge(r, i)
        if total_chars + len(text) > max_total_chars and formatted:
            formatted.append(
                f"[... {len(sampled_rollouts) - i} more rollouts omitted to stay within token budget]"
            )
            break
        formatted.append(text)
        total_chars += len(text)
    return "\n\n".join(formatted)


def judge_spec_fidelity(
    client: OpenAI,
    model: str,
    spec: str,
    sampled_rollouts: list[dict],
    max_rollout_chars: int = 50000,
) -> str:
    """Ask LLM judge: does the environment faithfully implement the spec?"""

    rollout_text = format_rollouts_within_budget(sampled_rollouts, max_rollout_chars)

    prompt = f"""You are evaluating whether a verifiers environment faithfully implements a specification.

## Environment Specification
{spec}

## Sampled Rollouts
Below are actual rollouts from the environment, showing how models interact with it. Each rollout shows the full trajectory of messages, tool calls, and responses, along with the reward score it received.

{rollout_text}

## Your Task
Analyze these rollouts and evaluate how well the environment implements the specification. Consider:

1. **Task Structure**: Do the generated tasks match what the spec describes? Are all required elements present?
2. **Tool Interface**: Does the agent have the tools the spec requires? Do they work as described?
3. **Constraint Types**: Are the constraint types from the spec actually represented in tasks?
4. **Scoring Behavior**: Does the scoring appear to follow the spec's description?
5. **Difficulty & Solvability**: Do tasks appear appropriately challenging? Are they solvable?
6. **Missing Elements**: What parts of the spec are NOT yet implemented or are incorrect?
7. **Degenerate Behavior**: Are there any obvious exploits, reward hacks, or degenerate strategies visible?

Write a detailed analysis. Be specific — reference particular rollouts and concrete observations. Highlight both what's working well and what needs improvement. End with a prioritized list of the most important issues to fix."""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.3,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"[Judge call failed: {e}]"


def judge_reward_faithfulness(
    client: OpenAI,
    model: str,
    spec: str,
    sampled_rollouts: list[dict],
    max_rollout_chars: int = 50000,
) -> str:
    """Ask LLM judge: are the reward scores faithful to the spec's intent?"""

    rollout_text = format_rollouts_within_budget(sampled_rollouts, max_rollout_chars)

    prompt = f"""You are evaluating whether an environment's reward/scoring function faithfully measures what the specification intends.

## Environment Specification
The spec describes what "good" behavior looks like and how it should be scored:
{spec}

## Sampled Rollouts with Scores
Each rollout below includes the reward score it received from the environment's rubric.

{rollout_text}

## Your Task
Analyze whether the reward scores are *faithful* to the specification's intent. Consider:

1. **Score Calibration**: Do high-scoring rollouts genuinely exhibit better behavior than low-scoring ones, according to the spec?
2. **Reward Alignment**: Does the scoring function appear to measure what the spec says it should? For each rollout, does the score make sense given what happened?
3. **Partial Credit**: Is partial credit given appropriately? Does the gradient of scores correspond to meaningful differences in behavior quality?
4. **Gaming Potential**: Could a model achieve a high score WITHOUT actually doing what the spec intends? Are there obvious shortcuts or reward hacks?
5. **Zero/Perfect Scores**: Are zero scores deserved (true failure)? Are perfect scores deserved (genuinely excellent)?
6. **Score Consistency**: For similar behavior quality, are scores consistent? Or does the scoring seem noisy/arbitrary?

Write a detailed analysis. Compare specific rollout pairs — explain why one scored higher than another and whether that ranking is justified. Identify any cases where the score seems wrong or misleading. End with specific recommendations for improving the scoring function's faithfulness."""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.3,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"[Judge call failed: {e}]"


# =============================================================================
# Main feedback pipeline
# =============================================================================


def generate_feedback(
    spec_path: Path,
    config: dict,
    outputs_dir: Path,
    commit: str,
    description: str,
    eval_success: bool,
) -> dict:
    """Generate a complete feedback entry."""

    spec = spec_path.read_text()
    timestamp = datetime.now(timezone.utc).isoformat()

    # Collect rollouts
    all_rollouts = find_latest_rollouts(outputs_dir)

    if not all_rollouts:
        return {
            "commit": commit,
            "timestamp": timestamp,
            "description": description,
            "eval_success": eval_success,
            "error": "No rollout outputs found. Environment may have failed to load or crashed during evaluation.",
            "stats": {},
            "sweet_spot": {},
            "spec_fidelity_feedback": "",
            "reward_faithfulness_feedback": "",
        }

    # Compute per-model stats
    all_stats = {}
    for model_name, data in all_rollouts.items():
        stats = compute_model_stats(data["rollouts"])
        # Include any metrics from metadata
        metadata = data["metadata"]
        stats["avg_reward_from_metadata"] = metadata.get("avg_reward")
        stats["avg_metrics_from_metadata"] = metadata.get("avg_metrics", {})
        stats["time_ms"] = metadata.get("time_ms")
        all_stats[model_name] = stats

    # Sweet spot analysis
    sweet_spot = compute_sweet_spot(all_stats)

    # LLM judge feedback
    judge_cfg = config["judge"]
    client = create_judge_client(config)
    num_samples = judge_cfg.get("num_sample_rollouts", 10)
    max_rollout_chars = judge_cfg.get("max_rollout_chars", 50000)

    spec_fidelity = ""
    reward_faithfulness = ""

    if client:
        sampled = sample_rollouts_for_judge(all_rollouts, num_samples)
        if sampled:
            print("  Running spec fidelity judge...")
            spec_fidelity = judge_spec_fidelity(
                client, judge_cfg["model"], spec, sampled, max_rollout_chars
            )
            print("  Running reward faithfulness judge...")
            reward_faithfulness = judge_reward_faithfulness(
                client, judge_cfg["model"], spec, sampled, max_rollout_chars
            )
        else:
            spec_fidelity = "[No rollouts available for judge review]"
            reward_faithfulness = "[No rollouts available for judge review]"
    else:
        spec_fidelity = "[Skipped — API key not set]"
        reward_faithfulness = "[Skipped — API key not set]"

    return {
        "commit": commit,
        "timestamp": timestamp,
        "description": description,
        "eval_success": eval_success,
        "stats": all_stats,
        "sweet_spot": sweet_spot,
        "spec_fidelity_feedback": spec_fidelity,
        "reward_faithfulness_feedback": reward_faithfulness,
    }


def main():
    parser = argparse.ArgumentParser(description="AutoEnv feedback pipeline")
    parser.add_argument("--spec", required=True, help="Path to spec.md")
    parser.add_argument("--config", required=True, help="Path to config.toml")
    parser.add_argument("--outputs-dir", required=True, help="Path to eval outputs")
    parser.add_argument("--log-file", required=True, help="Path to feedback log")
    parser.add_argument("--commit", default="unknown", help="Git commit hash")
    parser.add_argument("--description", default="", help="Description of changes")
    parser.add_argument("--eval-success", default="true", help="Whether evals succeeded")
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        config = tomllib.load(f)

    feedback = generate_feedback(
        spec_path=Path(args.spec),
        config=config,
        outputs_dir=Path(args.outputs_dir),
        commit=args.commit,
        description=args.description,
        eval_success=args.eval_success.lower() == "true",
    )

    # Append to log
    log_path = Path(args.log_file)
    with open(log_path, "a") as f:
        f.write(json.dumps(feedback) + "\n")

    print(f"  Feedback entry appended to {log_path}")


if __name__ == "__main__":
    main()
