#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/bootstrap.sh
opskit_find_python || { opskit_error '未找到 Python 3.9+，请重新运行本模块的 install.sh 自动补齐依赖。'; exit 1; }
exec "$OPSKIT_PYTHON" scripts/check.py "$@"
