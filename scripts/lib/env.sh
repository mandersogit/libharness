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
PI_CLI_NODE="$PI_INSTALL/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
PI_CLI_VENDORED="$PROJECT_ROOT/src/libharness/_vendor/pi/pi"
PI_HOME="$SANDBOX/pi-home"

# PI_CLI prefers the vendored precompiled binary (the new dev-loop
# default per dev-notes/2026-05-24-pi-vendoring-design.md). Falls back
# to the legacy node-pi path so contributors who haven't yet run the
# new `make install-pi` still get a working test runner. Override
# PI_CLI in the environment to force a specific binary.
if [[ -z "${PI_CLI:-}" ]]; then
  if [[ -x "$PI_CLI_VENDORED" ]]; then
    PI_CLI="$PI_CLI_VENDORED"
  else
    PI_CLI="$PI_CLI_NODE"
  fi
fi

# Python venv is managed by `make install`, not by these scripts. Override
# via LIBHARNESS_VENV to target a different venv (e.g. the 3.14t
# freethreaded venv at local-ft.venv/).
VENV="${LIBHARNESS_VENV:-$PROJECT_ROOT/local.venv}"

# nodeenv is a dev dep in the project venv (see pyproject.toml). Using
# $VENV here means install-pi-node-deprecated runs after install-311 in
# `make bootstrap`'s legacy ordering (back when bootstrap drove the node
# path). The active dev-loop `install-pi` target uses the vendored
# precompiled binary and does not need nodeenv.
NODEENV_PYTHON="$VENV/bin/python"

PI_PKG_VERSION="${PI_PKG_VERSION:-0.74.0}"
NODE_CHANNEL="${NODE_CHANNEL:-lts}"

mkdir -p "$SANDBOX"
