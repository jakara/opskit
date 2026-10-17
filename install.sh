#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
MODULE="${1:-}"
if [[ -z "$MODULE" ]]; then
  printf 'OpsKit 安装向导\n  1) 管理中心（Hub 及完整运维工具）\n  2) 日志采集端（Fluent Bit，每台业务机安装一次）\n  3) Hub 与 NPM（本机 80 端口，发现 Docker 和 HTTP 服务）\n'
  read -r -p '请选择模块 [1/2/3]：' MODULE
else
  shift
fi
case "$MODULE" in
  1|management|production) exec "$ROOT/management/install.sh" "$@" ;;
  2|collector) exec "$ROOT/collector/install.sh" "$@" ;;
  3|hub|development) exec "$ROOT/management/install.sh" --hub-only "$@" ;;
  *) printf '用法：%s [hub|management|collector] [--configure-only|--offline|--reconfigure]\n' "$0" >&2; exit 1 ;;
esac
