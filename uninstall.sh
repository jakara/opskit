#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
MODULE="${1:-all}"
if [[ "$MODULE" == --* ]]; then MODULE=all; else [[ "$#" -eq 0 ]] || shift; fi
case "$MODULE" in
  all) exec "$ROOT/management/uninstall.sh" --all "$@" ;;
  management) exec "$ROOT/management/uninstall.sh" "$@" ;;
  hub) exec "$ROOT/management/uninstall.sh" --hub-only "$@" ;;
  collector) exec "$ROOT/collector/uninstall.sh" "$@" ;;
  *) printf '用法：%s [all|hub|management|collector] [--dry-run|--purge-data|--yes]\n' "$0" >&2; exit 1 ;;
esac
