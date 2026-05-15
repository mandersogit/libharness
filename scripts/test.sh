#!/usr/bin/env bash
# Run pytest in the project venv with sandboxed pi available.
#
# Usage: scripts/test.sh [pytest args...]
# Examples:
#   scripts/test.sh
#   scripts/test.sh -v tests/test_real_pi_integration.py
set -euo pipefail
source "$(dirname "$0")/lib/env.sh"

if [[ ! -x "$VENV/bin/pytest" ]]; then
  echo "error: venv missing or pytest not installed at $VENV. Run 'make install'." >&2
  exit 1
fi

mkdir -p "$PI_HOME"

cd "$PROJECT_ROOT"

# Always export PI_CLI/HOME/PATH so live tests can find pi when it's
# installed. Tests requiring pi must mark themselves @pytest.mark.live or
# skip on PI_CLI/PI_HOME availability — we don't gate the whole runner on
# pi presence, so pure unit tests run without the sandbox.
HOME="$PI_HOME" \
PI_CLI="$PI_CLI" \
PI_OFFLINE=1 \
PI_SKIP_VERSION_CHECK=1 \
PATH="$NODE_BIN:${PATH:-/usr/bin:/bin}" \
exec "$VENV/bin/pytest" -q "$@"
