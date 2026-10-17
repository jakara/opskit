# 龙蜥离线基础依赖

这个模块用于**制包机联网、客户机断网**的交付方式。第一版目标为 Anolis OS 8.9 或 8.10，CPU 为 amd64 或 arm64；不同系统小版本和架构分别制包，不把一个包承诺为通用 Linux 安装包。制包机可以使用已有 Docker 的 Linux、macOS 或 Windows，不在制包机安装 RPM 或修改 Docker 配置。

## 工程师制包

```bash
python3 skills/opskit/scripts/collect_prerequisites.py \
  --os anolis --version 8.9 --arch amd64 \
  --output /绝对路径/prerequisites-anolis8.9-amd64
```

Windows 将 `python3` 换为本机可用的 Python 3.9+ 命令。下载和验收均明确指定 `--platform linux/<目标架构>`，不继承制包机默认架构；跨架构需要 Docker 能执行相应容器。首次运行会拉取官方 `registry.openanolis.cn/openanolis/anolisos:<版本>` 镜像，并创建两个自动删除的一次性容器。它们不挂载宿主 Docker socket、业务仓库或客户配置。

1. 下载容器使用目标系统仓库，必要时添加 Docker 官方 RHEL 8 stable 仓库。使用 `dnf download --resolve --alldeps` 收集完整强依赖，并用 `dnf install --downloadonly` 补齐同步升级已装 RPM 的反向依赖。包含并行安装的 Python 3.11、Docker Engine/CLI、containerd、Buildx、Compose 插件、iproute 和 procps-ng；不启用弱依赖。
2. 保存目标镜像内的发行版签名公钥；Docker 公钥必须匹配官方指纹 `060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35`。逐个检查 RPM 签名，建立包内本地仓库。
3. 第二个容器从原始目标镜像重新启动，明确使用 `--network none`。禁用所有在线仓库，只从包内 `file://` 仓库安装，保留 GPG 检查，并运行 Python、Docker/Compose、端口及进程工具的版本命令。
4. 只有上述验证实际成功，才发布输出目录及 `manifest.json`，记录包哈希、镜像 ID、平台、命令和完整日志。失败时保留点号开头的构建目录及日志，不生成可交付的依赖清单。

部分网络环境中，容器访问 Docker 官方源的 TLS 路径可能失败。收集器只在记录该失败后，尝试由制包机通过 HTTPS 从**同一个 Docker 官方目录**下载目标架构 RPM。文件仍在目标容器内核对签名并解析依赖；证据记录为 `docker_download_transport=host-https-verified-container`。不会改用第三方镜像源或关闭 TLS/GPG 验证。

## 交付包调用契约

制包器将输出目录原样复制为包内 `prerequisites/`，拒绝链接，并核验完整 `SHA256SUMS` 和 manifest 声明的哈希。

```text
prerequisites/
  bootstrap.sh              无需新版 Python 的首启入口
  manifest.json             target、RPM/签名公钥/仓库索引的哈希及验收证据
  target.txt                三行：anolis、8.9|8.10、amd64|arm64
  SHA256SUMS                除自身外全部文件的校验值
  python-packages.txt       Python 依赖文件清单，供审计
  rpms/*.rpm
  rpms/repodata/*           本地仓库索引，现场安装必需
  keys/*                    发行版及 Docker 签名公钥
  verification/*            下载、断网安装及版本命令的真实日志
```

`manifest.json` 的 `schema_version=1`，`target={os,version,arch}`；`files` 每项有 `path`、`sha256`、`group=python|runtime`；`keys` 和 `repository_files` 每项有 `path`、`sha256`。`verification` 至少包含 `offline_install=true`、`network=none`、`container_image`、`image_id`、`platform`、`python_version`、`docker_version`、`compose_version`、`commands` 和 `log`。`offline_install=true` 只表达干净容器中的 RPM 事务及 CLI 验收。

包根 `install.sh` 在寻找 Python 之前调用：

```bash
bash prerequisites/bootstrap.sh --root prerequisites
```

- `--python-only`：仅检查或补装 Python，不安装、启动 Docker。
- `--check-only`：检查哈希、目标平台及已有依赖，缺失立即报错；不安装软件、不启动服务。可与 `--python-only` 合用。
- 正常安装：先校验全部文件及目标系统/架构；已有 Python 3.9+、Docker/Compose、主机发现工具直接复用，仅按缺失软件名从本地仓库安装。缺权限时通过 `sudo` 重跑同一入口，不修改用户的组成员关系。

离线安装不访问网络，不修改 `/etc/yum.repos.d`，不使用 `--allowerasing`，不删除 Podman、runc 或用户数据，不改 `/usr/bin/python3` 指向。本地仓库以 `module_hotfixes=1` 允许已验签的模块 RPM 安装；该开关仅作用于本次包内仓库，不改系统模块流。遇到现有 Podman/runc 与缺失 Docker 的组合会停止，要求客户管理员先决定运行时整合方案。已有 Docker CLI 及 Compose 可用而 daemon 不可访问时，检查动作不会误认为必须更换运行时；安装动作仅尝试在运行 systemd 的客户机上启动 Docker，失败则保留明确错误。

## 验收边界

签名校验和断网 RPM 事务不能证明客户 VM 的 systemd、内核、SELinux、Docker daemon、磁盘和业务部署已经通过验收。到现场后仍由总安装入口执行 Docker 可用性检查及业务验收，实际冲突须保留错误信息；不能把容器中无法启动 systemd 的状态伪装为部署成功。

完整交付包应在代表性空白 VM 上断网验证一次。Anolis 8.9/8.10、amd64/arm64 是可选制包目标，并不代表每种组合已在本项目真实运行；以所交付包自身的 manifest 和日志为准。

官方依据：[Anolis OS](https://openanolis.cn/anolisos)、[Docker RHEL 安装与官方 RPM/GPG 来源](https://docs.docker.com/engine/install/rhel/)。使用 Docker 的 RHEL 8 RPM 在龙蜥上安装属于本工具实际验证的组合，不代表 Docker 官方对龙蜥发行版的认证。
