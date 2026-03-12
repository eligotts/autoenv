#!/usr/bin/env bash
# =============================================================================
# AutoEnv Evaluation Harness
# =============================================================================
# This script is FIXED — the agent CANNOT modify it.
#
# What it does:
#   1. Activates the project venv, reinstalls candidate
#   2. Runs a smoke test (1 task, 1 rollout, 1 model) to catch obvious bugs
#   3. Runs prime eval against both the target model and the strong model
#   4. Computes numeric stats (feedback.py)
#   5. Launches Claude Code as judge to analyze rollouts and write feedback
#
# Usage:
#   bash evaluate.sh --description "brief description of changes"
#
# Everything (models, examples, rollouts) is read from config.toml.
# No knobs — every iteration runs the same eval for comparability.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
CONFIG_FILE="$SCRIPT_DIR/config.toml"
SPEC_FILE="$SCRIPT_DIR/spec.md"
CANDIDATE_DIR="$SCRIPT_DIR/candidate"
FEEDBACK_LOG="$SCRIPT_DIR/feedback_log.jsonl"
FEEDBACK_DIR="$SCRIPT_DIR/feedback"
ENV_ID="candidate-env"
PYTHON="$VENV_DIR/bin/python"

# ---- Activate venv ----
if [ ! -f "$PYTHON" ]; then
    echo "ERROR: venv not found at $VENV_DIR. Run setup.sh first."
    exit 1
fi
source "$VENV_DIR/bin/activate"

# ---- Parse arguments ----
DESCRIPTION=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --description)
            DESCRIPTION="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

# ---- Read config via Python (toml parsing) ----
read_config() {
    "$PYTHON" -c "
import tomllib, sys, json
with open('$CONFIG_FILE', 'rb') as f:
    config = tomllib.load(f)
$1
"
}

# Get all eval params from config (fixed, no options)
TARGET_MODEL=$(read_config "print(config['eval']['target_model'])")
STRONG_MODEL=$(read_config "print(config['eval']['strong_model'])")
NUM_EXAMPLES=$(read_config "print(config['eval']['num_examples'])")
ROLLOUTS_PER=$(read_config "print(config['eval']['rollouts_per_example'])")
# Resolve relative endpoints path against SCRIPT_DIR
ENDPOINTS_PATH_RAW=$(read_config "print(config['eval']['vf']['endpoints_path'])")
ENDPOINTS_PATH="$(cd "$SCRIPT_DIR" && realpath "$ENDPOINTS_PATH_RAW")"
PROVIDER=$(read_config "print(config['eval']['vf']['provider'])")
MAX_CONCURRENT=$(read_config "print(config['eval']['vf']['max_concurrent'])")

echo "============================================="
echo "AutoEnv Evaluation"
echo "============================================="
echo "Venv:         $VENV_DIR"
echo "Examples:     $NUM_EXAMPLES"
echo "Rollouts:     $ROLLOUTS_PER per example"
echo "Target model: $TARGET_MODEL"
echo "Strong model: $STRONG_MODEL"
echo "Description:  ${DESCRIPTION:-<none>}"
echo "============================================="

# ---- Step 0: Clean stale outputs from any interrupted prior run ----
OUTPUTS_DIR="$SCRIPT_DIR/outputs"
rm -rf "$OUTPUTS_DIR"

# ---- Step 1: Reinstall candidate environment ----
echo ""
echo "[1/5] Installing candidate environment..."
uv pip install -e "$CANDIDATE_DIR" --python "$PYTHON" --quiet 2>&1 | tail -1
echo "      Done."

# ---- Step 2: Smoke test (1 task, 1 rollout, target model) ----
echo ""
echo "[2/5] Smoke test (1 task, 1 rollout, $TARGET_MODEL)..."

SMOKE_SUCCESS=true
if (cd "$SCRIPT_DIR" && prime eval "$ENV_ID" \
    -m "$TARGET_MODEL" \
    -e "$ENDPOINTS_PATH" \
    -p "$PROVIDER" \
    -n 1 \
    -r 1 \
    -c 1 \
    -s \
    -d) 2>&1 | tee "$SCRIPT_DIR/.smoke_test.log"; then
    echo "      Smoke test passed."
else
    echo ""
    echo "============================================="
    echo "SMOKE TEST FAILED. Fix the environment before running the full eval."
    echo "Check .smoke_test.log for details."
    echo "============================================="
    exit 1
fi

# Clean smoke test outputs
rm -rf "$OUTPUTS_DIR"

# ---- Step 3: Run prime eval for both models ----
echo ""
echo "[3/5] Running full evaluations..."

EVAL_SUCCESS=true

for MODEL in "$TARGET_MODEL" "$STRONG_MODEL"; do
    echo ""
    echo "  Evaluating: $MODEL"
    echo "  Examples: $NUM_EXAMPLES, Rollouts/example: $ROLLOUTS_PER"

    # Run from SCRIPT_DIR so outputs land in $SCRIPT_DIR/outputs/
    if (cd "$SCRIPT_DIR" && prime eval "$ENV_ID" \
        -m "$MODEL" \
        -e "$ENDPOINTS_PATH" \
        -p "$PROVIDER" \
        -n "$NUM_EXAMPLES" \
        -r "$ROLLOUTS_PER" \
        -c "$MAX_CONCURRENT" \
        -s \
        -d) 2>&1 | tee "$SCRIPT_DIR/.eval_${MODEL//\//_}.log"; then
        echo "  Done: $MODEL"
    else
        echo "  FAILED: $MODEL (exit code $?)"
        EVAL_SUCCESS=false
    fi
done

# ---- Step 4: Compute numeric stats ----
echo ""
echo "[4/5] Computing numeric stats..."

COMMIT=$(cd "$SCRIPT_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")

"$PYTHON" "$SCRIPT_DIR/feedback.py" \
    --config "$CONFIG_FILE" \
    --outputs-dir "$OUTPUTS_DIR" \
    --log-file "$FEEDBACK_LOG" \
    --commit "$COMMIT" \
    --description "$DESCRIPTION" \
    --eval-success "$EVAL_SUCCESS"

# Print numeric stats for the agent
echo ""
echo "=== NUMERIC STATS ==="
tail -1 "$FEEDBACK_LOG" | "$PYTHON" -c "
import sys, json
entry = json.loads(sys.stdin.read())
print(json.dumps(entry, indent=2))
"

# ---- Step 5: Launch Claude Code judge for qualitative feedback ----
echo ""
echo "[5/5] Launching Claude Code judge..."

mkdir -p "$FEEDBACK_DIR"
FEEDBACK_FILE="$FEEDBACK_DIR/${COMMIT}.md"

# Find the rollout results files
ROLLOUT_PATHS=$(find "$OUTPUTS_DIR/evals" -name "results.jsonl" 2>/dev/null | tr '\n' ' ')

if [ -z "$ROLLOUT_PATHS" ]; then
    echo "  No rollout files found — skipping judge."
    echo "# No rollouts available for judge review" > "$FEEDBACK_FILE"
else
    # Read numeric stats to include in judge prompt
    STATS_SUMMARY=$(tail -1 "$FEEDBACK_LOG" | "$PYTHON" -c "
import sys, json
entry = json.loads(sys.stdin.read())
rl = entry.get('rl_readiness', {})
strong = entry.get('strong_model', {})
lines = []
lines.append('Numeric stats for this iteration:')
lines.append('')
lines.append('TARGET MODEL (RL training candidate):')
lines.append(f'- Mean reward: {rl.get(\"mean_reward\", \"?\")}, Std: {rl.get(\"std_reward\", \"?\")}')
lines.append(f'- Solve rate: {rl.get(\"solve_rate\", \"?\")}')
lines.append(f'- RL readiness: {rl.get(\"interpretation\", \"?\")}')
lines.append('')
lines.append('STRONG MODEL (solvability check):')
lines.append(f'- Mean reward: {strong.get(\"mean_reward\", \"?\")}, Std: {strong.get(\"std_reward\", \"?\")}')
lines.append(f'- Solve rate: {strong.get(\"solve_rate\", \"?\")}')
lines.append(f'- Interpretation: {strong.get(\"interpretation\", \"?\")}')
lines.append('')
lines.append(f'- Verdict vs previous: {entry.get(\"verdict\", {}).get(\"verdict\", \"?\")}: {entry.get(\"verdict\", {}).get(\"reason\", \"?\")}')
print('\n'.join(lines))
")

    JUDGE_PROMPT="You are a feedback judge for an RL training environment. Your job is to analyze rollouts from a recent evaluation and provide detailed qualitative feedback.

## Your Instructions

1. Read the environment spec: $SPEC_FILE
2. Read the rollout results files in $OUTPUTS_DIR/evals/ — there are results.jsonl files containing the actual rollouts. Each line is a JSON object with fields like 'completion' (the full message trajectory), 'reward', 'info' (task data), etc. There are TWO sets of rollouts — one from the target model (RL training candidate) and one from the strong model (solvability check).
3. ACTUALLY READ THE ROLLOUTS. Open the results.jsonl files and examine the completion field of multiple rollouts from BOTH models. Look at the tool calls, tool responses, agent reasoning, and final scores. Read at least 6-8 rollouts spanning low, medium, and high reward scores.
4. Read the feedback log at $FEEDBACK_LOG to understand prior iterations.

## Context

Description of changes this iteration: ${DESCRIPTION:-none}
$STATS_SUMMARY

## What to Analyze

### Spec Fidelity
- Do the generated tasks match what the spec describes?
- Does the agent have the tools the spec requires? Do they work correctly?
- Are the constraint types from the spec represented in actual tasks?
- What parts of the spec are NOT yet implemented or are incorrect?
- Are there degenerate behaviors, exploits, or reward hacks visible?

### Reward Faithfulness
- Do high-scoring rollouts genuinely exhibit better behavior than low-scoring ones?
- Is partial credit given appropriately?
- Could a model achieve a high score WITHOUT doing what the spec intends?
- Are zero scores deserved? Are high scores deserved?

### RL Training Readiness (Target Model)
- For RL to work well, the target model needs mean reward in the 0.2-0.7 range.
- Is the environment too hard? Too easy? What specific changes would bring the mean reward into the ideal range?

### Solvability (Strong Model)
- The strong model should score HIGH (>0.7 mean reward). This validates that the tasks are actually solvable and the scoring is fair.
- If the strong model scores low, the tasks may be too hard, poorly designed, or the scoring may be broken.
- Compare strong model rollouts to target model rollouts: where does the strong model succeed that the target model fails? This reveals what the target model needs to learn.

## Output

Write your complete feedback analysis to: $FEEDBACK_FILE

Structure it as markdown with clear sections. Be specific — reference particular rollouts by their reward score and describe what happened. End with a prioritized list of issues to fix."

    echo "  Writing feedback to: $FEEDBACK_FILE"
    if env -u CLAUDECODE claude -p "$JUDGE_PROMPT" --print --dangerously-skip-permissions > "$FEEDBACK_FILE" 2>"$SCRIPT_DIR/.judge.log"; then
        echo "  Judge feedback written successfully."
    else
        echo "  WARNING: Claude Code judge failed (exit code $?). Check .judge.log"
        echo "# Judge failed — check .judge.log for details" > "$FEEDBACK_FILE"
    fi
fi

# Clean up outputs after feedback is generated
rm -rf "$OUTPUTS_DIR"

echo ""
echo "============================================="
echo "Evaluation complete."
echo "  Numeric stats: $FEEDBACK_LOG"
echo "  Judge feedback: $FEEDBACK_FILE"
echo "============================================="
