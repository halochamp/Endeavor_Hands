#!/bin/zsh
# Double-click to start the local Secure MCP Tunnel for ChatGPT.
# The runtime key lives in the logged-in user's macOS Keychain, never in this file.
#
# Run ./start_tunnel.sh once first (see README.md) to create the tunnel-client
# profile before using this launcher.

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
WORKSPACE_DIR="$HOME/Desktop"
TUNNEL_CLIENT="$PROJECT_DIR/bin/tunnel-client"
PROFILE_NAME="endeavor-chatgpt"
KEYCHAIN_SERVICE="endeavor-chatgpt-tunnel-runtime"
STATUS_URL="http://127.0.0.1:8765"

[[ -x "$TUNNEL_CLIENT" ]] || {
  print -u2 "Missing tunnel-client: $TUNNEL_CLIENT"
  print -u2 "Download it from OpenAI Platform and place it at bin/tunnel-client."
  exit 1
}

if /usr/bin/curl --fail --silent --max-time 1 "$STATUS_URL/readyz" >/dev/null; then
  print "The Endeavor Hands tunnel is already running."
  open "$STATUS_URL/ui"
  exit 0
fi

# `security -w` returns the secret only to this process; it is not printed.
if ! CONTROL_PLANE_API_KEY=$(/usr/bin/security find-generic-password \
  -a "$USER" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null); then
  print "No runtime key is stored in Keychain yet."
  read -r -s "CONTROL_PLANE_API_KEY?Paste runtime API key (hidden): "
  print
  [[ -n "$CONTROL_PLANE_API_KEY" ]] || {
    print -u2 "A runtime API key is required."
    exit 1
  }

  # macOS Keychain may ask for your login keychain password. The value is
  # stored in Keychain and is not written to the profile, this script, or logs.
  /usr/bin/security add-generic-password -U \
    -a "$USER" -s "$KEYCHAIN_SERVICE" -w "$CONTROL_PLANE_API_KEY"
  print "Runtime key saved in Keychain."
fi

export CONTROL_PLANE_API_KEY
# The MCP server inherits this and may edit existing files anywhere under Desktop.
export V2_WORKSPACE="$WORKSPACE_DIR"
# File removal is forbidden even within the writable workspace.
export V2_DENY_FILE_DELETION=1
print "Starting Endeavor Hands tunnel. Keep this Terminal window open."
exec "$TUNNEL_CLIENT" run --profile "$PROFILE_NAME" --mcp.connection-max-ttl 168h0m0s
