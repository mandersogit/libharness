#!/usr/bin/env bash
# Install @earendil-works/pi-coding-agent@$PI_PKG_VERSION into .sandbox/pi-install/.
# Uses the sandboxed Node and a project-local npm cache (.sandbox/npm-cache).
# Idempotent: skips if the target version is already installed.
set -euo pipefail
source "$(dirname "$0")/lib/env.sh"

if [[ ! -x "$NODE_BIN/node" ]]; then
  echo "error: Node not installed. Run scripts/setup-node.sh first." >&2
  exit 1
fi

mkdir -p "$PI_INSTALL" "$NPM_CACHE"
cd "$PI_INSTALL"

if [[ ! -f package.json ]]; then
  PATH="$NODE_BIN:$PATH" NPM_CONFIG_CACHE="$NPM_CACHE" \
    "$NODE_BIN/npm" init -y >/dev/null
fi

installed_version=""
if [[ -f "$PI_CLI" ]]; then
  installed_version="$(
    PATH="$NODE_BIN:$PATH" NPM_CONFIG_CACHE="$NPM_CACHE" \
      "$NODE_BIN/npm" pkg get dependencies.@earendil-works/pi-coding-agent 2>/dev/null \
      | tr -d '"^~ ' \
      | tr -d "'"
  )"
fi

if [[ "$installed_version" == "$PI_PKG_VERSION" ]]; then
  echo "pi-coding-agent@$PI_PKG_VERSION already installed at $PI_CLI"
  exit 0
fi

if [[ -n "$installed_version" && "$installed_version" != "{}" ]]; then
  echo "Replacing pi-coding-agent@$installed_version with @$PI_PKG_VERSION"
fi

echo "Installing @earendil-works/pi-coding-agent@$PI_PKG_VERSION..."
PATH="$NODE_BIN:$PATH" NPM_CONFIG_CACHE="$NPM_CACHE" \
  "$NODE_BIN/npm" install --loglevel=error \
  "@earendil-works/pi-coding-agent@$PI_PKG_VERSION"

if [[ ! -f "$PI_CLI" ]]; then
  echo "error: install reported success but $PI_CLI is missing" >&2
  exit 1
fi

echo
echo "Installed: $PI_CLI"
PATH="$NODE_BIN:$PATH" "$NODE_BIN/node" "$PI_CLI" --version || true
