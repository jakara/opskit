#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
. "$ROOT/scripts/bootstrap.sh"
opskit_find_python || { opskit_error '卸载需要已安装的 Python 3.9+，此入口不会安装依赖。'; exit 1; }
exec "$OPSKIT_PYTHON" -u "$ROOT/scripts/uninstall.py" "$@"
