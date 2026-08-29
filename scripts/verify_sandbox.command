#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${ENDEAVOR_PYTHON:-$ROOT/.venv/bin/python3}"
SANDBOX_EXEC="${SANDBOX_EXEC:-/usr/bin/sandbox-exec}"

if [[ ! -x "$SANDBOX_EXEC" ]]; then
  echo "FAIL: sandbox-exec not found at $SANDBOX_EXEC" >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "FAIL: Endeavor Python not found at $PYTHON" >&2
  exit 2
fi

cd "$ROOT"
WORKSPACE="$($PYTHON -c 'import config; print(config.WORKSPACE)')"
PROFILE="$(mktemp /private/tmp/endeavor-hands-sandbox.XXXXXX.sb)"
TARGET="$WORKSPACE/.sandbox_verify_$$.txt"
WRITE_TARGET="$WORKSPACE/.sandbox_verify_write_$$.txt"
mkdir -p "$WORKSPACE"

cleanup() {
  rm -f "$PROFILE" "$TARGET" "$WRITE_TARGET"
}
trap cleanup EXIT

"$PYTHON" - "$PROFILE" <<'PY'
import sys
from pathlib import Path
import config
from tools._sandbox import build_sandbox_profile
Path(sys.argv[1]).write_text(build_sandbox_profile(config.WORKSPACE), encoding="utf-8")
PY

printf 'keep\n' > "$TARGET"

# Positive boundary check: a normal write inside the approved workspace must work.
"$SANDBOX_EXEC" -f "$PROFILE" /bin/sh -c 'printf "ok\n" > "$1"' sh "$WRITE_TARGET"
if [[ "$(cat "$WRITE_TARGET")" != "ok" ]]; then
  echo "FAIL: sandboxed workspace write did not complete" >&2
  exit 1
fi

# Negative boundary check: ordinary sandboxed commands must not unlink workspace files.
if "$SANDBOX_EXEC" -f "$PROFILE" /bin/rm "$TARGET" >/private/tmp/endeavor-hands-sandbox-rm.out 2>&1; then
  echo "FAIL: sandbox unexpectedly allowed workspace file deletion" >&2
  exit 1
fi
if [[ ! -f "$TARGET" ]]; then
  echo "FAIL: deletion probe removed the protected file" >&2
  exit 1
fi

# Production must actually invoke sandbox-exec. A missing executable must fail rather than
# silently falling back to direct execution; unit tests cover argv construction separately.
echo "PASS: real one-layer Endeavor Hands sandbox boundary is active"
echo "  workspace write: allowed"
echo "  workspace unlink: denied"
echo "Run this verifier from a normal Terminal, not through Endeavor Hands' sandboxed bash tool."
