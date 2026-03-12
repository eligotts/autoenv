#!/usr/bin/env bash
# =============================================================================
# AutoEnv Setup
# =============================================================================
# Run this once to install dependencies.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Installing uv..."
curl -LsSf https://astral.sh/uv/install.sh | sh

echo ""
echo "Installing prime CLI..."
uv tool install prime

echo ""
echo "Initializing verifiers submodule..."
git submodule update --init --recursive

echo ""
echo "Creating virtual environment..."
uv venv "$SCRIPT_DIR/.venv" --python 3.12

echo ""
echo "Installing verifiers from submodule..."
uv pip install -e "$SCRIPT_DIR/verifiers" --python "$SCRIPT_DIR/.venv/bin/python"

echo ""
echo "Installing autoenv dependencies..."
uv pip install -e "$SCRIPT_DIR" --python "$SCRIPT_DIR/.venv/bin/python"

echo ""
echo "Installing candidate environment (stub)..."
uv pip install -e "$SCRIPT_DIR/candidate" --python "$SCRIPT_DIR/.venv/bin/python"

echo ""
echo "============================================="
echo "Setup complete."
echo "Venv: $SCRIPT_DIR/.venv"
echo "Make sure PRIME_API_KEY is set in your environment."
echo "============================================="
