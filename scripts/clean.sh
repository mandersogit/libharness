#!/usr/bin/env bash
# Remove the entire .sandbox/ directory. Run again from scratch with
# scripts/bootstrap.sh.
#
# Usage:
#   scripts/clean.sh           # asks for confirmation
#   scripts/clean.sh --yes     # no prompt
set -euo pipefail
source "$(dirname "$0")/lib/env.sh"

if [[ ! -d "$SANDBOX" ]]; then
  echo "Nothing to clean: $SANDBOX does not exist."
  exit 0
fi

if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Delete $SANDBOX ? [y/N] " reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

rm -rf "$SANDBOX"
echo "Removed $SANDBOX"
