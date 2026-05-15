#!/usr/bin/env bash
# Minimal sanity check: confirm pi starts in RPC mode and answers get_state.
# Does not require a Python venv or an LLM key.
set -euo pipefail
source "$(dirname "$0")/lib/env.sh"

if [[ ! -f "$PI_CLI" ]]; then
  echo "error: pi not installed at $PI_CLI. Run scripts/setup-pi.sh." >&2
  exit 1
fi

mkdir -p "$PI_HOME"

echo "pi version:"
HOME="$PI_HOME" PATH="$NODE_BIN:$PATH" "$NODE_BIN/node" "$PI_CLI" --version
echo

echo "RPC get_state request:"
echo '{"id":"smoke-1","type":"get_state"}'

echo
echo "RPC response:"
printf '{"id":"smoke-1","type":"get_state"}\n' \
  | HOME="$PI_HOME" \
    PATH="$NODE_BIN:$PATH" \
    PI_OFFLINE=1 \
    PI_SKIP_VERSION_CHECK=1 \
    "$NODE_BIN/node" "$PI_CLI" \
        --mode rpc \
        --no-session \
        --offline \
        --no-extensions \
        --no-skills \
        --no-prompt-templates \
        --no-context-files \
  | head -1
