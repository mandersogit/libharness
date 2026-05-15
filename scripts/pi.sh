#!/usr/bin/env bash
# Run the sandboxed pi CLI with HOME pointed at .sandbox/pi-home/ so pi can't
# touch the real ~/.pi/.
#
# Usage: scripts/pi.sh [pi args...]
# Examples:
#   scripts/pi.sh --version
#   scripts/pi.sh --mode rpc --no-session --offline --no-extensions
set -euo pipefail
source "$(dirname "$0")/lib/env.sh"

if [[ ! -f "$PI_CLI" ]]; then
  echo "error: pi not installed at $PI_CLI. Run scripts/setup-pi.sh first." >&2
  exit 1
fi

if [[ ! -x "$NODE_BIN/node" ]]; then
  echo "error: Node missing at $NODE_BIN/node. Run scripts/setup-node.sh first." >&2
  exit 1
fi

mkdir -p "$PI_HOME"

HOME="$PI_HOME" \
PATH="$NODE_BIN:${PATH:-/usr/bin:/bin}" \
PI_OFFLINE="${PI_OFFLINE:-1}" \
PI_SKIP_VERSION_CHECK=1 \
exec "$NODE_BIN/node" "$PI_CLI" "$@"
