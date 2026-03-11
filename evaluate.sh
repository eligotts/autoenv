#!/usr/bin/env bash
# =============================================================================
# AutoEnv Evaluation Harness
# =============================================================================
# This script is FIXED — the agent CANNOT modify it.
#
# What it does:
#   1. Installs the candidate environment
#   2. Runs prime eval against each model in the config
#   3. Runs the feedback pipeline on collected rollouts
#
# Usage:
#   bash evaluate.sh --description "brief description of changes"
#
# Everything (models, examples, rollouts) is read from config.toml.
# No knobs — every iteration runs the same eval for comparability.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.toml"
SPEC_FILE="$SCRIPT_DIR/spec.md"
CANDIDATE_DIR="$SCRIPT_DIR/candidate"
FEEDBACK_LOG="$SCRIPT_DIR/feedback_log.jsonl"
ENV_ID="candidate-env"

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
    python3 -c "
import tomllib, sys, json
with open('$CONFIG_FILE', 'rb') as f:
    config = tomllib.load(f)
$1
"
}

# Get all eval params from config (fixed, no options)
MODELS=$(read_config "print(json.dumps(config['eval']['models']))")
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
echo "Examples:    $NUM_EXAMPLES"
echo "Rollouts:    $ROLLOUTS_PER per example"
echo "Models:      $(echo "$MODELS" | python3 -c "import sys,json; print(', '.join(json.load(sys.stdin)))")"
echo "Description: ${DESCRIPTION:-<none>}"
echo "============================================="

# ---- Step 1: Install candidate environment ----
echo ""
echo "[1/3] Installing candidate environment..."
cd "$CANDIDATE_DIR"
uv pip install -e . --quiet 2>&1 | tail -1
cd "$SCRIPT_DIR"
echo "      Done."

# ---- Step 2: Run prime eval for each model ----
echo ""
echo "[2/3] Running evaluations..."

# Outputs go to $SCRIPT_DIR/outputs/
OUTPUTS_DIR="$SCRIPT_DIR/outputs"

EVAL_SUCCESS=true
MODEL_LIST=$(echo "$MODELS" | python3 -c "import sys,json; [print(m) for m in json.load(sys.stdin)]")

while IFS= read -r MODEL; do
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
done <<< "$MODEL_LIST"

# ---- Step 3: Run feedback pipeline ----
echo ""
echo "[3/3] Generating feedback..."

COMMIT=$(cd "$SCRIPT_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")

python3 "$SCRIPT_DIR/feedback.py" \
    --spec "$SPEC_FILE" \
    --config "$CONFIG_FILE" \
    --outputs-dir "$OUTPUTS_DIR" \
    --log-file "$FEEDBACK_LOG" \
    --commit "$COMMIT" \
    --description "$DESCRIPTION" \
    --eval-success "$EVAL_SUCCESS"

# Clean up outputs after feedback is generated so each iteration starts fresh
# (rollout data is captured in the feedback entry)
rm -rf "$OUTPUTS_DIR"

echo ""
echo "============================================="
echo "Evaluation complete. Feedback appended to:"
echo "  $FEEDBACK_LOG"
echo "============================================="

# Print the latest feedback entry for the agent to read
echo ""
echo "=== LATEST FEEDBACK ==="
tail -1 "$FEEDBACK_LOG" | python3 -c "
import sys, json
entry = json.loads(sys.stdin.read())
print(json.dumps(entry, indent=2))
"
