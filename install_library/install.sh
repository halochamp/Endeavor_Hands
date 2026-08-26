#!/usr/bin/env bash
# Install ENDEAVOR_AGENT_CHATGPT into a project-local Python 3.11 virtual env.
#
# Usage:
#   bash install_library/install.sh
#
# This installer only installs Python dependencies. The three optional Swift
# helpers (screen accessibility, Apple Vision OCR, speech transcription) each
# compile themselves on first use if Xcode Command Line Tools are present —
# there is nothing to build here, and a missing compiler only disables those
# specific optional features, not the server itself.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

echo "=== ENDEAVOR_AGENT_CHATGPT installer ==="
echo

echo "[1/4] Checking platform"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[error] ENDEAVOR_AGENT_CHATGPT requires macOS (computer/read_file rely on macOS frameworks)."
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "[error] Apple Silicon is required; detected: $(uname -m)"
  exit 1
fi
echo "macOS Apple Silicon: OK"

echo
echo "[2/4] Checking Python 3.11"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[error] Could not find $PYTHON_BIN. Install Python 3.11, or set PYTHON_BIN to its full path."
  exit 1
fi
if ! "$PYTHON_BIN" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)
PY
then
  echo "[error] $PYTHON_BIN is not Python 3.11: $($PYTHON_BIN --version)"
  exit 1
fi
echo "Using: $($PYTHON_BIN --version)"

echo
echo "[3/4] Creating project virtual environment"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  echo "Created: $VENV_DIR"
else
  echo "Reusing: $VENV_DIR"
fi

PYTHON="$VENV_DIR/bin/python"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install --require-hashes -r "$PROJECT_DIR/requirements.txt"

echo
echo "[4/4] Installation complete"
cat <<EOF

Next steps:

  Run the server directly (for an MCP client that spawns it, e.g. Claude
  Desktop or \`mcp dev\`):
    "$VENV_DIR/bin/python3" "$PROJECT_DIR/server.py"

  Or connect ChatGPT web via the OpenAI Secure MCP Tunnel — see README.md's
  "ChatGPT web via OpenAI Secure MCP Tunnel" section and
  docs/CHATGPT_SETUP_TH.md for the full walkthrough.

First use of \`computer\` will prompt for macOS Accessibility (and possibly
Screen Recording) permission — see README.md's "macOS permission for
computer" section.
EOF
