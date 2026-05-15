#!/usr/bin/env bash
# Launch the sandboxed pi in interactive mode so you can run /login.
# OAuth tokens land in .sandbox/pi-home/.pi/agent/auth.json and persist
# across runs.
#
# Steps in the pi TUI:
#   1. type:  /login
#   2. pick the provider (e.g. "ChatGPT Plus/Pro (Codex)")
#   3. complete OAuth in the browser
#   4. type:  /quit
#
# Requires that the OAuth callback port (1455) is free.
set -euo pipefail
exec "$(dirname "$0")/pi.sh" "$@"
