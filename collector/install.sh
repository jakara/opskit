#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
OPSKIT_INSTALLER="$ROOT/install.sh"
OPSKIT_MODULE=collector
. "$ROOT/scripts/bootstrap.sh"
opskit_bootstrap "$@"
exec "$OPSKIT_PYTHON" -u "$ROOT/scripts/install.py" "$@"
