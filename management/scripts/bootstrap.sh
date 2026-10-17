#!/usr/bin/env bash
# 本文件随模块独立分发；不修改系统 Python、Docker 用户组或 daemon.json。

opskit_error() {
  printf '安装未完成：%s\n' "$*" >&2
  return 1
}

opskit_find_python() {
  local candidate
  for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python39; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
      OPSKIT_PYTHON="$candidate"
      return 0
    fi
  done
  return 1
}

opskit_privileged() {
  if [[ "$EUID" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -- "$@"
  else
    opskit_error '安装基础依赖需要 root 或 sudo 权限，请以管理员身份重新运行同一安装命令。'
  fi
}

opskit_linux_release() {
  ID= ID_LIKE= VERSION_ID= VERSION_CODENAME=
  if [[ -r /etc/os-release ]]; then
    . /etc/os-release
  fi
  case " $ID $ID_LIKE " in
    *anolis*|*rhel*|*centos*|*rocky*|*almalinux*|*fedora*) OPSKIT_FAMILY=rhel ;;
    *ubuntu*|*debian*) OPSKIT_FAMILY=debian ;;
    *) opskit_error "暂不支持自动安装此 Linux 发行版（${ID:-未知}）的依赖。"; return 1 ;;
  esac
}

opskit_package_manager() {
  if command -v dnf >/dev/null 2>&1; then
    OPSKIT_PACKAGE_MANAGER=dnf
  elif command -v yum >/dev/null 2>&1; then
    OPSKIT_PACKAGE_MANAGER=yum
  else
    opskit_error '未找到 dnf 或 yum，无法安装龙蜥/RHEL 基础依赖。'
    return 1
  fi
}

opskit_install_python_linux() {
  local package
  opskit_linux_release || return 1
  if [[ "$OPSKIT_FAMILY" == rhel ]]; then
    opskit_package_manager || return 1
    for package in python3.11 python3.9 python39 python3; do
      printf '正在尝试从系统仓库安装 %s（保留系统 Python）……\n' "$package"
      if opskit_privileged "$OPSKIT_PACKAGE_MANAGER" -y install "$package" && opskit_find_python; then
        return 0
      fi
    done
  else
    opskit_privileged apt-get update || return 1
    opskit_privileged apt-get install -y python3 || return 1
    opskit_find_python && return 0
  fi
  opskit_error '系统仓库未提供可用的 Python 3.9+。请启用该发行版的 AppStream/更新仓库后重新运行；不会替换 platform-python 或编译覆盖系统解释器。'
}

# 只写入 OpsKit 专用仓库文件；覆盖前保留并核验原始内容。
opskit_install_file() {
  local source="$1" target="$2" backup
  if [[ -L "$target" ]]; then
    opskit_error "拒绝覆盖软链接：$target"
    return 1
  fi
  if [[ -f "$target" ]] && cmp -s "$source" "$target"; then
    return 0
  fi
  if [[ -e "$target" ]]; then
    backup="$target.opskit-backup-$(date +%Y%m%d-%H%M%S)-$$"
    opskit_privileged cp -p "$target" "$backup" || return 1
    opskit_privileged cmp -s "$target" "$backup" || { opskit_error "备份校验失败：$backup"; return 1; }
    printf '已备份原配置：%s\n' "$backup"
  fi
  opskit_privileged install -m 0644 "$source" "$target" || return 1
  opskit_privileged cmp -s "$source" "$target" || { opskit_error "写入校验失败：$target"; return 1; }
}

opskit_rhel_docker_repo() {
  local major="${VERSION_ID%%.*}" directory="${1:-/etc/yum.repos.d}" temp
  case "$major" in
    8|9|10) ;;
    *) opskit_error "Docker 官方 RHEL 仓库暂不适配此系统版本：${VERSION_ID:-未知}。"; return 1 ;;
  esac
  temp="$(mktemp)" || return 1
  cat > "$temp" <<EOF
[opskit-docker-ce-stable]
name=OpsKit Docker CE Stable
baseurl=https://download.docker.com/linux/rhel/$major/\$basearch/stable
enabled=1
gpgcheck=1
gpgkey=https://download.docker.com/linux/rhel/gpg
EOF
  if ! opskit_install_file "$temp" "$directory/opskit-docker-ce.repo"; then
    rm -f "$temp"
    return 1
  fi
  rm -f "$temp"
}

opskit_debian_docker_repo() {
  local key repo architecture
  case "$ID" in ubuntu|debian) ;; *) opskit_error 'Docker 仓库自动配置仅支持原版 Ubuntu/Debian。'; return 1 ;; esac
  [[ "$VERSION_CODENAME" =~ ^[a-z][a-z0-9]*$ ]] || { opskit_error '无法确定系统版本代号。'; return 1; }
  architecture="$(dpkg --print-architecture)" || return 1
  [[ "$architecture" =~ ^[a-z0-9]+$ ]] || return 1
  opskit_privileged apt-get update || return 1
  opskit_privileged apt-get install -y ca-certificates curl || return 1
  key="$(mktemp)" || return 1
  repo="$(mktemp)" || { rm -f "$key"; return 1; }
  if ! curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$key"; then
    rm -f "$key" "$repo"
    return 1
  fi
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/opskit-docker.asc] https://download.docker.com/linux/%s %s stable\n' "$architecture" "$ID" "$VERSION_CODENAME" > "$repo"
  if ! opskit_privileged install -d -m 0755 /etc/apt/keyrings ||
     ! opskit_install_file "$key" /etc/apt/keyrings/opskit-docker.asc ||
     ! opskit_install_file "$repo" /etc/apt/sources.list.d/opskit-docker.list; then
    rm -f "$key" "$repo"
    return 1
  fi
  rm -f "$key" "$repo"
  opskit_privileged apt-get update
}

opskit_install_docker_linux() {
  local compose_only="$1"
  opskit_linux_release || return 1
  if [[ "$OPSKIT_FAMILY" == rhel ]]; then
    opskit_package_manager || return 1
    if [[ "$compose_only" == yes ]]; then
      opskit_privileged "$OPSKIT_PACKAGE_MANAGER" -y install docker-compose-plugin && return 0
    else
      opskit_privileged "$OPSKIT_PACKAGE_MANAGER" -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin && return 0
    fi
    opskit_rhel_docker_repo || return 1
    if [[ "$compose_only" == yes ]]; then
      opskit_privileged "$OPSKIT_PACKAGE_MANAGER" -y install docker-compose-plugin && return 0
    else
      opskit_privileged "$OPSKIT_PACKAGE_MANAGER" -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin && return 0
    fi
  else
    # 先复用系统软件源；只缺 Compose 时不更换已有 Docker 引擎。
    if [[ "$compose_only" == yes ]]; then
      opskit_privileged apt-get install -y docker-compose-v2 && return 0
      opskit_privileged apt-get install -y docker-compose-plugin && return 0
    else
      opskit_privileged apt-get install -y docker.io docker-compose-v2 && return 0
    fi
    opskit_debian_docker_repo || return 1
    if [[ "$compose_only" == yes ]]; then
      opskit_privileged apt-get install -y docker-compose-plugin && return 0
    else
      opskit_privileged apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin && return 0
    fi
  fi
  opskit_error 'Docker 依赖安装失败。请按包管理器提示处理仓库或现有运行时冲突后重试；脚本不会删除 Podman/runc，也不会强制替换软件包。'
}

opskit_find_brew() {
  if command -v brew >/dev/null 2>&1; then return 0; fi
  if [[ -x /opt/homebrew/bin/brew ]]; then
    export PATH="/opt/homebrew/bin:$PATH"
  elif [[ -x /usr/local/bin/brew ]]; then
    export PATH="/usr/local/bin:$PATH"
  else
    return 1
  fi
}

opskit_prepare_brew() {
  local temp
  opskit_find_brew && return 0
  [[ "$EUID" -ne 0 ]] || { opskit_error 'macOS 请用普通用户运行安装器，Homebrew 会在需要时请求管理员权限。'; return 1; }
  printf '未找到 Homebrew，正在运行 Homebrew 官方安装程序……\n'
  temp="$(mktemp)" || return 1
  if ! curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$temp"; then
    rm -f "$temp"
    return 1
  fi
  if ! /bin/bash "$temp"; then
    rm -f "$temp"
    opskit_error 'Homebrew 尚未安装完成，请完成终端内的授权或系统工具安装后重新运行同一命令。'
    return 1
  fi
  rm -f "$temp"
  opskit_find_brew || { opskit_error 'Homebrew 安装后未就绪，请重新打开终端并运行同一命令。'; return 1; }
}

opskit_start_docker_linux() {
  if command -v systemctl >/dev/null 2>&1; then
    opskit_privileged systemctl enable --now docker
  elif command -v service >/dev/null 2>&1; then
    opskit_privileged service docker start
  else
    opskit_error 'Docker 已安装，但未找到 systemd/service；请启动现有 Docker 服务后重新运行。'
  fi
}

opskit_docker_ready() {
  docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

opskit_local_engine() {
  command -v dockerd >/dev/null 2>&1 || [[ -x /usr/bin/dockerd || -x /usr/sbin/dockerd ]]
}

opskit_desktop_installed() {
  [[ -d /Applications/Docker.app ]]
}

opskit_sudo_rerun() {
  if [[ "$EUID" -ne 0 ]] && command -v sudo >/dev/null 2>&1 && sudo -- docker info >/dev/null 2>&1; then
    printf '当前用户无权访问 Docker，将以 sudo 重新执行同一安装命令。\n'
    exec sudo -- "$OPSKIT_INSTALLER" "${OPSKIT_ARGS[@]}"
  fi
  return 1
}

opskit_prepare_docker() {
  local platform="$1" compose_only=no attempt
  if [[ "$platform" == Darwin && -x /Applications/Docker.app/Contents/Resources/bin/docker ]]; then
    export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
  fi
  opskit_docker_ready && return 0
  if [[ "$OPSKIT_OFFLINE" -eq 1 ]]; then
    if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
      opskit_error '离线模式缺少 Docker 或 Compose V2，未访问网络。请先在联网环境运行安装器准备依赖，再导入离线镜像后重试。'
      return 1
    fi
    if [[ "$platform" == Linux ]]; then
      opskit_sudo_rerun || true
      opskit_start_docker_linux || return 1
      opskit_docker_ready && return 0
      opskit_sudo_rerun || true
    fi
    opskit_error '离线模式下现有 Docker 服务未就绪，请启动该运行环境后重新运行同一命令。'
    return 1
  fi
  if [[ "$platform" == Darwin ]]; then
    if ! command -v docker >/dev/null 2>&1; then
      opskit_prepare_brew || return 1
      brew install --cask docker-desktop || return 1
      export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
    fi
    if opskit_desktop_installed; then
      open -a Docker || { opskit_error '无法打开 Docker Desktop，请从应用程序中完成首次启动后重新运行同一命令。'; return 1; }
      for attempt in 1 2 3 4 5; do
        opskit_docker_ready && return 0
        sleep 2
      done
    fi
    opskit_error 'Docker 尚未就绪。请完成 Docker Desktop 首次启动、许可确认及系统授权；出现运行中状态后重新执行同一安装命令（重启后也可续跑，已有配置会保留）。'
    return 1
  fi
  if command -v docker >/dev/null 2>&1; then
    if ! opskit_local_engine && ! docker info >/dev/null 2>&1; then
      opskit_error '已有 Docker 兼容客户端，但未发现可用的本机 Docker 引擎。请启动现有运行环境后重试；脚本不会替换远程、Podman 或其他既有运行时。'
      return 1
    fi
    compose_only=yes
  fi
  if [[ "$compose_only" == no ]] || ! docker compose version >/dev/null 2>&1; then
    opskit_install_docker_linux "$compose_only" || return 1
  fi
  opskit_docker_ready && return 0
  opskit_sudo_rerun || true
  opskit_start_docker_linux || return 1
  for attempt in 1 2 3 4 5; do
    opskit_docker_ready && return 0
    sleep 1
  done
  opskit_sudo_rerun || true
  opskit_error 'Docker 服务未就绪。请检查 Docker 服务状态后重新运行同一安装命令；已有配置会保留。'
}

opskit_prepare_discovery() {
  local platform="$1"
  [[ "${OPSKIT_MODULE:-management}" == management ]] || return 0
  if [[ "$platform" == Darwin ]]; then
    command -v lsof >/dev/null 2>&1 && return 0
    [[ "$OPSKIT_OFFLINE" -eq 0 ]] || { opskit_error '离线模式缺少 lsof，HTTP 服务发现不可用，未访问网络。'; return 1; }
    opskit_prepare_brew && brew install lsof || return 1
    command -v lsof >/dev/null 2>&1 || { opskit_error '安装后未找到 lsof，请重新打开终端并续跑。'; return 1; }
  else
    command -v ss >/dev/null 2>&1 && return 0
    [[ "$OPSKIT_OFFLINE" -eq 0 ]] || { opskit_error '离线模式缺少 ss（iproute/iproute2），HTTP 服务发现不可用，未访问网络。'; return 1; }
    opskit_linux_release || return 1
    if [[ "$OPSKIT_FAMILY" == rhel ]]; then
      opskit_package_manager || return 1
      opskit_privileged "$OPSKIT_PACKAGE_MANAGER" -y install iproute || return 1
    else
      opskit_privileged apt-get update || return 1
      opskit_privileged apt-get install -y iproute2 || return 1
    fi
    command -v ss >/dev/null 2>&1 || { opskit_error '安装后未找到 ss，HTTP 服务发现尚未就绪。'; return 1; }
  fi
}

opskit_bootstrap() {
  local argument platform skip_docker=0
  OPSKIT_ARGS=("$@")
  OPSKIT_OFFLINE=0
  for argument in "$@"; do
    case "$argument" in
      --offline) OPSKIT_OFFLINE=1 ;;
      --configure-only|--help|-h) skip_docker=1 ;;
    esac
  done
  if opskit_find_python; then
    # --help 和仅生成配置不检查 Docker，也无需探测系统发行版。
    [[ "$skip_docker" -eq 0 ]] || return 0
  elif [[ "$OPSKIT_OFFLINE" -eq 1 ]]; then
    opskit_error '离线模式缺少 Python 3.9+，未访问网络。请先在联网环境运行安装器准备依赖。'
    return 1
  else
    platform="$(uname -s)"
    case "$platform" in
      Linux) opskit_install_python_linux || return 1 ;;
      Darwin) opskit_prepare_brew && brew install python || return 1 ;;
      *) opskit_error '请在 Linux/macOS 运行 install.sh；Windows 请使用 install.cmd。'; return 1 ;;
    esac
    opskit_find_python || { opskit_error '依赖安装后仍未找到 Python 3.9+，请重新打开终端后运行同一命令。'; return 1; }
    [[ "$skip_docker" -eq 0 ]] || return 0
  fi
  platform="$(uname -s)"
  case "$platform" in
    Linux|Darwin) opskit_prepare_discovery "$platform" && opskit_prepare_docker "$platform" ;;
    *) opskit_error '请在 Linux/macOS 运行 install.sh；Windows 请使用 install.cmd。' ;;
  esac
}
