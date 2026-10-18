#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [[ -z "${OPSKIT_LOCATION_READY:-}" ]]; then
  . "$ROOT/management/scripts/bootstrap.sh"
  opskit_bootstrap --configure-only "$@"
  exec "$OPSKIT_PYTHON" -B "$ROOT/management/scripts/location.py" --source "$ROOT" --entry install.sh -- "$@"
fi
MODULE="${1:-}"
if [[ "$MODULE" == -* && "$MODULE" != --help && "$MODULE" != -h ]]; then MODULE=''; fi
. "$ROOT/management/scripts/bootstrap.sh"
opskit_find_python
if [[ -z "$MODULE" ]]; then
  "$OPSKIT_PYTHON" -B "$ROOT/management/scripts/console_ui.py"
  DEFAULT_MODULE="$("$OPSKIT_PYTHON" -B "$ROOT/management/scripts/options.py" read "$ROOT")"
  printf '请选择模块 [1/2/3，回车采用 %s]：' "$DEFAULT_MODULE"
  read -r MODULE
  MODULE="${MODULE:-$DEFAULT_MODULE}"
else
  shift
fi
if [[ " $* " != *' --help '* && " $* " != *' -h '* ]]; then
  case "$MODULE" in
    1|management|production|2|collector|3|hub|development)
      "$OPSKIT_PYTHON" -B "$ROOT/management/scripts/options.py" write "$ROOT" "$MODULE" ;;
  esac
fi
case "$MODULE" in
  1|management|production) exec "$ROOT/management/install.sh" "$@" ;;
  2|collector) exec "$ROOT/collector/install.sh" "$@" ;;
  3|hub|development) exec "$ROOT/management/install.sh" --hub-only "$@" ;;
  *) printf '用法：%s [hub|management|collector] [--configure-only|--offline|--reconfigure]\n' "$0" >&2; exit 1 ;;
esac
