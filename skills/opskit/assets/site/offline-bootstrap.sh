#!/usr/bin/env bash
# 首次运行时尚无新版 Python；此入口只使用 Anolis 自带的 shell/RPM 工具。
set -euo pipefail

root=""
python_only=0
check_only=0
original_args=("$@")
fail() { printf '离线基础环境检查失败：%s\n' "$*" >&2; exit 1; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) [ "$#" -ge 2 ] || fail '--root 缺少目录'; root="$2"; shift 2 ;;
    --python-only) python_only=1; shift ;;
    --check-only) check_only=1; shift ;;
    --help|-h) printf '用法：bootstrap.sh --root <prerequisites目录> [--python-only] [--check-only]\n'; exit 0 ;;
    *) fail "未知参数：$1" ;;
  esac
done
[ -n "$root" ] || fail '必须指定 --root'
root=$(cd "$root" && pwd -P) || fail '依赖目录不存在'
[ -f "$root/SHA256SUMS" ] && [ -f "$root/target.txt" ] && [ -f "$root/manifest.json" ] || fail '缺少依赖清单或校验文件'
command -v sha256sum >/dev/null 2>&1 || fail '缺少操作系统自带的 sha256sum'
[ -z "$(find "$root" -type l -print -quit)" ] || fail '依赖目录包含链接，禁止安装'
# 校验名单不能指向交付依赖目录之外；拒绝链接，避免包内容越界。
while IFS= read -r record || [ -n "$record" ]; do
  [[ "$record" =~ ^[0-9a-f]{64}\ \ .+ ]] || fail 'SHA256SUMS 格式错误'
  file=${record:66}
  case "$file" in /*|../*|*/../*|*/..|*\\*) fail '校验路径越界或包含反斜线' ;; esac
  [ -f "$root/$file" ] && [ ! -L "$root/$file" ] || fail "校验文件缺失或为链接：$file"
done < "$root/SHA256SUMS"
(cd "$root" && sha256sum -c SHA256SUMS --quiet) || fail '文件损坏，禁止安装，请重新复制交付包'

[ "$(uname -s)" = Linux ] || fail '此依赖包只用于目标龙蜥 Linux；制包端可使用 macOS/Windows'
[ -r /etc/os-release ] || fail '无法识别目标操作系统'
# 只读操作系统的可信发行信息，不 source 交付包中的任意 shell 内容。
. /etc/os-release
target_os=$(sed -n '1p' "$root/target.txt")
target_version=$(sed -n '2p' "$root/target.txt")
target_arch=$(sed -n '3p' "$root/target.txt")
[ "$target_os" = anolis ] || fail '依赖包不是已支持的 Anolis 包'
case "$target_version" in 8.9|8.10) ;; *) fail '依赖包的系统版本不受支持' ;; esac
case "$(uname -m)" in x86_64) actual_arch=amd64 ;; aarch64|arm64) actual_arch=arm64 ;; *) fail '当前 CPU 架构不受支持' ;; esac
[ "${ID:-}" = "$target_os" ] && [ "${VERSION_ID:-}" = "$target_version" ] && [ "$actual_arch" = "$target_arch" ] || fail "包要求 Anolis ${target_version} / ${target_arch}；当前为 ${ID:-未知} ${VERSION_ID:-未知} / ${actual_arch}"

find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,9))' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}
need_python=0
find_python >/dev/null || need_python=1
packages=()
[ "$need_python" -eq 0 ] || packages+=(python3.11)
if [ "$python_only" -eq 0 ]; then
  if command -v docker >/dev/null 2>&1; then
    docker_version=$(docker --version 2>/dev/null || true)
    [[ "$docker_version" == Docker\ version* ]] || fail '现有 docker 命令不是可用 Docker CLI；不会替换 Podman 或其他运行时'
    docker compose version >/dev/null 2>&1 || packages+=(docker-compose-plugin)
  else
    if rpm -q podman >/dev/null 2>&1 || rpm -q podman-docker >/dev/null 2>&1 || rpm -q runc >/dev/null 2>&1 || command -v podman >/dev/null 2>&1; then
      fail '当前主机含 Podman/runc；请由管理员确认运行时整合方案，本工具不会删除或替换现有运行时'
    fi
    packages+=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
  fi
  command -v ss >/dev/null 2>&1 || packages+=(iproute)
  command -v ps >/dev/null 2>&1 || packages+=(procps-ng)
fi
if [ "${#packages[@]}" -gt 0 ]; then
  [ "$check_only" -eq 0 ] || fail "当前缺少：${packages[*]}。检查模式不会安装；请先执行同一交付包的安装动作"
  if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || fail '安装缺失依赖需要 root，且当前系统没有 sudo'
    printf '需要管理员权限，从交付包安装缺失依赖：%s\n' "${packages[*]}"
    exec sudo bash "$root/bootstrap.sh" "${original_args[@]}"
  fi
  command -v dnf >/dev/null 2>&1 || fail '目标系统缺少 dnf'
  [ -f "$root/rpms/repodata/repomd.xml" ] || fail '缺少离线 RPM 仓库索引'
  shopt -s nullglob
  keys=("$root"/keys/*)
  [ "${#keys[@]}" -gt 0 ] || fail '缺少 RPM 签名公钥'
  for key in "${keys[@]}"; do rpmkeys --import "$key"; done
  repository_path=${root//%/%25}
  repository_path=${repository_path// /%20}
  repository_path=${repository_path//#/%23}
  repository_path=${repository_path//\?/%3F}
  # 仅启用包内 file:// 仓库；开启包签名校验，不下载、不擦除冲突软件、不改系统软件源。
  dnf -y --disablerepo='*' --repofrompath="opskit-offline,file://$repository_path/rpms" \
    --setopt=opskit-offline.gpgcheck=1 --setopt=opskit-offline.repo_gpgcheck=0 --setopt=opskit-offline.module_hotfixes=1 \
    --setopt=install_weak_deps=False install "${packages[@]}"
fi
python_command=$(find_python) || fail '安装后仍找不到 Python 3.9+'
if [ "$python_only" -eq 0 ]; then
  docker --version >/dev/null && docker compose version >/dev/null || fail 'Docker / Compose CLI 不可用'
  command -v ss >/dev/null && command -v ps >/dev/null || fail '主机端口或进程发现工具不可用'
  if [ "$check_only" -eq 0 ] && ! docker info >/dev/null 2>&1; then
    if [ "$(id -u)" -ne 0 ]; then
      command -v sudo >/dev/null 2>&1 || fail 'Docker 引擎不可访问，请由管理员检查权限'
      exec sudo bash "$root/bootstrap.sh" "${original_args[@]}"
    fi
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
      systemctl start docker || fail 'Docker 服务未启动，请查看 systemctl status docker'
      docker info >/dev/null 2>&1 || fail 'Docker 服务启动后仍不可用'
    else
      fail '当前没有可用 Docker 引擎，且未运行 systemd；容器内 RPM 验收不能代替客户 VM 的服务启动验收'
    fi
  fi
fi
printf '离线基础环境校验通过：Anolis %s / %s，%s\n' "$target_version" "$target_arch" "$("$python_command" --version 2>&1)"
