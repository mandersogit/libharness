#!/usr/bin/env bash
# Install a sandboxed Node.js into .sandbox/nodeenv/ via nodeenv.
# Idempotent: skips if Node is already present at the target.
set -euo pipefail
source "$(dirname "$0")/lib/env.sh"

if [[ -x "$NODE_BIN/node" ]]; then
  echo "Node already installed: $("$NODE_BIN/node" --version) at $NODE_BIN/node"
  exit 0
fi

if [[ ! -x "$NODEENV_PYTHON" ]]; then
  echo "error: nodeenv host python not found at $NODEENV_PYTHON" >&2
  exit 1
fi

echo "Installing Node ($NODE_CHANNEL) into $NODE_PREFIX via nodeenv..."
"$NODEENV_PYTHON" -m nodeenv "$NODE_PREFIX" -n "$NODE_CHANNEL"
echo
echo "Installed: $("$NODE_BIN/node" --version)"
# npm's shebang is `#!/usr/bin/env node` so node must be on PATH for npm to run.
echo "npm:       $(PATH="$NODE_BIN:$PATH" "$NODE_BIN/npm" --version)"
