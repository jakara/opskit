#!/usr/bin/env bash
# 交付包入口：先完整校验，再离线准备依赖，最后进入中文向导。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
fail() { printf '现场安装未开始：%s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || fail '本交付包现场端仅支持清单指定的龙蜥 Linux。'
command -v sha256sum >/dev/null 2>&1 || fail '缺少系统自带的 sha256sum，无法安全校验交付介质。'
[[ -f "$ROOT/SHA256SUMS" && ! -L "$ROOT/SHA256SUMS" ]] || fail '缺少有效的 SHA256SUMS。'
# 校验前拒绝绝对路径、越界、重复项、链接及未登记文件；不执行包内代码解析清单。
declare -A listed=()
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^([a-f0-9]{64})\ \ (.+)$ ]] || fail '校验清单格式错误。'
  name="${BASH_REMATCH[2]}"
  [[ "$name" != /* && "$name" != *\\* && "$name" != *:* && "$name" != SHA256SUMS ]] || fail '清单存在无效路径。'
  [[ "/$name/" != */../* && "/$name/" != */./* && "$name" != *$'\r'* ]] || fail '清单路径越界。'
  [[ -z "${listed[$name]+yes}" ]] || fail '清单存在重复项。'
  [[ -f "$ROOT/$name" && ! -L "$ROOT/$name" ]] || fail "文件缺失或为链接：$name"
  listed["$name"]=1
done < "$ROOT/SHA256SUMS"
while IFS= read -r -d '' path; do
  [[ ! -L "$path" ]] || fail '交付包中禁止软链接。'
  if [[ ! -f "$path" ]]; then
    [[ -d "$path" ]] || fail '交付包中禁止特殊文件。'
    continue
  fi
  relative="${path#"$ROOT/"}"
  [[ "$relative" == SHA256SUMS || -n "${listed[$relative]+yes}" ]] || fail "文件未登记：$relative"
done < <(find "$ROOT" -mindepth 1 -print0)
(cd "$ROOT" && sha256sum --strict --status -c SHA256SUMS) || fail '交付包校验失败，请重新复制完整发行包。'
[[ -n "${listed[delivery/site.py]+yes}" && -n "${listed[prerequisites/bootstrap.sh]+yes}" ]] || fail '清单缺少安装运行时。'
ACTION=''
ARGS=("$@")
for ((i=0; i<${#ARGS[@]}; i++)); do
  case "${ARGS[$i]}" in
    --action) ((i+1 < ${#ARGS[@]})) || fail '--action 缺少参数'; ACTION="${ARGS[$((i+1))]}" ;;
    --action=*) ACTION="${ARGS[$i]#--action=}" ;;
  esac
done
# 无参数时先选操作，确保“验收/备份”不会触发安装任何依赖。
if [[ -z "$ACTION" && "$*" != *--help* && "$*" != *--non-interactive* ]]; then
  printf '现场操作：1 安装 / 2 升级 / 3 验收 / 4 备份 / 5 回退\n'
  while true; do
    read -r -p '请选择操作 [1—5]：' choice
    case "$choice" in
      1) ACTION=install; break ;; 2) ACTION=upgrade; break ;; 3) ACTION=check; break ;;
      4) ACTION=backup; break ;; 5) ACTION=rollback; break ;;
      *) printf '请选择 1—5。\n' ;;
    esac
  done
  ARGS+=(--action "$ACTION")
fi
BOOTSTRAP_ARGS=(--root "$ROOT/prerequisites")
case "$ACTION" in
  check|backup|rollback) BOOTSTRAP_ARGS+=(--check-only) ;;
  sync-docs) BOOTSTRAP_ARGS+=(--python-only --check-only) ;;
  install|upgrade) ;;
  '') BOOTSTRAP_ARGS+=(--check-only) ;;
  *) fail '不支持的 --action。' ;;
esac
bash "$ROOT/prerequisites/bootstrap.sh" "${BOOTSTRAP_ARGS[@]}"
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    exec "$candidate" -B "$ROOT/delivery/site.py" "${ARGS[@]}"
  fi
done
fail '离线依赖执行后仍未找到 Python 3.9+；请检查 prerequisites 安装日志。'
