#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
OPSKIT_INSTALLER="$ROOT/install.sh"
OPSKIT_MODULE=management
. "$ROOT/scripts/bootstrap.sh"
if [[ -z "${OPSKIT_LOCATION_READY:-}" ]]; then
  opskit_bootstrap --configure-only "$@"
  exec "$OPSKIT_PYTHON" -B "$ROOT/scripts/location.py" --source "$ROOT" --entry install.sh -- "$@"
fi
opskit_bootstrap "$@"
exec "$OPSKIT_PYTHON" -u "$ROOT/scripts/install.py" "$@"
