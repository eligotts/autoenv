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
echo "Installing verifiers from submodule..."
cd "$SCRIPT_DIR/verifiers"
uv pip install -e .

echo ""
echo "Installing autoenv dependencies..."
cd "$SCRIPT_DIR"
uv pip install -e .

echo ""
echo "Installing candidate environment (stub)..."
cd "$SCRIPT_DIR/candidate"
uv pip install -e .

echo ""
echo "Setup complete."
echo "Make sure PRIME_API_KEY is set in your environment."
