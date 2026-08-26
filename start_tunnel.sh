#!/bin/zsh
# Configure and run the local Secure MCP Tunnel without persisting credentials.

set -euo pipefail

PROJECT_DIR=${0:A:h}
TUNNEL_CLIENT="$PROJECT_DIR/bin/tunnel-client"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
SERVER_PATH="$PROJECT_DIR/server.py"
PROFILE_NAME="endeavor-chatgpt"

[[ -x "$TUNNEL_CLIENT" ]] || {
  print -u2 "Missing executable: $TUNNEL_CLIENT"
  print -u2 "Download tunnel-client from OpenAI Platform and place it at bin/tunnel-client."
  exit 1
}
[[ -x "$PYTHON_BIN" ]] || {
  print -u2 "Missing Python interpreter: $PYTHON_BIN"
  print -u2 "Run install_library/install.sh first to create the project's .venv."
  exit 1
}

read -r "TUNNEL_ID?Tunnel ID (tunnel_...): "
[[ "$TUNNEL_ID" == tunnel_* ]] || {
  print -u2 "Tunnel ID must begin with tunnel_."
  exit 1
}

# This intentionally remains only in this process environment. Do not paste it
# into chat, a config file, source code, or shell history.
read -r -s "CONTROL_PLANE_API_KEY?Paste runtime API key (hidden): "
print
[[ -n "$CONTROL_PLANE_API_KEY" ]] || {
  print -u2 "A runtime API key is required."
  exit 1
}
export CONTROL_PLANE_API_KEY

"$TUNNEL_CLIENT" init \
  --sample sample_mcp_stdio_local \
  --profile "$PROFILE_NAME" \
  --tunnel-id "$TUNNEL_ID" \
  --mcp-command "$PYTHON_BIN $SERVER_PATH"

"$TUNNEL_CLIENT" doctor --profile "$PROFILE_NAME" --explain
print "Tunnel is ready. Keep this Terminal open while using the ChatGPT app."
exec "$TUNNEL_CLIENT" run --profile "$PROFILE_NAME" --mcp.connection-max-ttl 168h0m0s
