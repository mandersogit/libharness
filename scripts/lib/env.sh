# Sourced by other scripts. Defines paths to sandboxed tools.
#
# Do not execute. To use:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/env.sh"
# from a script in scripts/.
#
# Overridable via the environment:
#   PYTHON_VERSION   miniforge env to use for the venv (default: 3.11)
#   PI_PKG_VERSION   @earendil-works/pi-coding-agent version (default: 0.74.0)
#   NODE_CHANNEL     nodeenv --node value (default: lts)

set -euo pipefail

__env_sh_path="${BASH_SOURCE[0]}"
__env_sh_dir="$(cd "$(dirname "$__env_sh_path")" && pwd)"
PROJECT_ROOT="$(cd "$__env_sh_dir/../.." && pwd)"

SANDBOX="$PROJECT_ROOT/.sandbox"
NODE_PREFIX="$SANDBOX/nodeenv"
NODE_BIN="$NODE_PREFIX/bin"
NPM_CACHE="$SANDBOX/npm-cache"
PI_INSTALL="$SANDBOX/pi-install"
PI_CLI="$PI_INSTALL/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
PI_HOME="$SANDBOX/pi-home"

# Python venv is managed by `make install`, not by these scripts.
VENV="$PROJECT_ROOT/local.venv"

# nodeenv lives in the py3-14 miniforge env.
NODEENV_PYTHON="/opt/miniforge/envs/base-py3-14/bin/python"

PI_PKG_VERSION="${PI_PKG_VERSION:-0.74.0}"
NODE_CHANNEL="${NODE_CHANNEL:-lts}"

mkdir -p "$SANDBOX"
