# OpsKit 日志采集端

交互安装先检查当前安装包位置，推荐本系统下的 `opskit-collector` 安装目录；选择后复制、校验并从新位置继续。可用 `--install-dir /实际目录` 指定位置或 `--keep-location` 保留当前位置；Windows 对应 `-InstallDir`、`-KeepLocation`。已有非空目标不会覆盖；只生成配置和非交互调用默认不搬移。

本模块在每台业务机上独立运行 Fluent Bit，将应用日志文件发送到管理中心 VictoriaLogs。它不安装 Spug、DBX 或 NPM，也不需要管理中心的完整配置和管理员密码。

## 安装前准备

- 直接运行本模块安装入口即可检查并在线补装 Python 3.9+、Docker 和 Compose V2，已有可用依赖会复用；首次准备依赖需联网及必要的管理员授权。
- 已获得管理中心生成的 `collector.env`，业务机能连接其中的管理机地址和端口。
- 已确认应用日志目录及文件命名规则，容器有读取权限。

龙蜥通过包管理器并行安装新版 Python，保留系统 Python；缺 Docker 时优先使用现有仓库，必要时加入 OpsKit 专用官方 Docker 仓库，变更前备份并核验。macOS 缺依赖时通过 Homebrew，Windows 10/11 通过 WinGet 准备 Python、Docker Desktop 和必要的 WSL 2，须使用 Linux 容器。Docker Desktop 首次许可、系统授权或要求的重启需在系统界面完成，之后重跑同一命令即可继续。Windows Server 不在本次支持范围内。

安装脚本不修改系统防火墙、Docker 用户组、daemon.json 或业务日志目录权限。若目标系统启用 SELinux，按实际策略允许只读日志访问，不要直接批量修改系统目录标签。

## 安装与配置

将整个 `collector/` 目录复制到业务机，把管理中心提供的 `collector.env` 复制为本目录的 `.env`。从工具包根目录执行：

```bash
./install.sh collector
```

只复制了采集模块时，在本目录执行：

```bash
./install.sh
```

Windows 在本模块目录打开 PowerShell：

```powershell
.\install.cmd
.\install.cmd -ConfigureOnly
.\install.cmd -Reconfigure
.\install.cmd -Offline
```

CMD 仅对本次进程使用 RemoteSigned，不更改系统执行策略；下载 ZIP 被系统锁定时先解除可信文件锁定再解压。Windows 业务日志目录使用本地磁盘路径，例如 `C:\apps\teaching\logs` 或 `C:/apps/teaching/logs`，允许中文和空格；配置中会规范成正斜杠。不挂载整个磁盘、不支持 UNC 网络共享，Docker Desktop 需有权共享并读取该目录。

向导补充本机标签、日志目录与文件匹配规则。每台业务机使用独立的 `HOST_ID`，其他标签按实际应用填写。

| 配置 | 示例或含义 |
| --- | --- |
| `OPS_HOST` | 实际连接的管理机 IP 或可解析域名，例如 `10.20.30.40` |
| `OPS_PORT` | 管理机 HTTP 入口端口，默认 80；经过外层 HTTPS 时通常为 443 |
| `OPS_VHOST` | 管理域名，例如 `ops.example.internal`，通过 HTTP `Host` 头匹配 NPM 主机 |
| `OPS_SCHEME` | 默认 `http`，可选 `https` |
| `LOG_AGENT_USER`、`LOG_AGENT_PASSWORD` | 管理中心提供的采集专用账户 |
| `HOST_ID` | 当前业务机标识，例如 `vm-app-01` |
| `PROJECT_ID` | 项目标识，例如 `vocational-art` |
| `APP_ID` | 应用标识，例如 `teaching-api` |
| `APP_ENV` | 环境标识，例如 `production` |
| `HOST_LOG_DIR` | 已存在的宿主机日志目录绝对路径，例如 `/opt/teaching/logs` 或 `C:/apps/teaching/logs`，不允许挂载整个根目录或磁盘 |
| `LOG_FILE_PATTERN` | 目录内的日志文件规则，例如 `*.log` |

向导还可指定 `FLUENTBIT_IMAGE`，默认使用固定版本，可改为内网镜像仓库地址。

`OPS_HOST` 和 `OPS_VHOST` 可以不同：业务机直接连接管理机私网 IP 时，仍需发送正确的管理域名以匹配 NPM 配置。向导默认使用内网 HTTP，不生成或申请证书。

选择 HTTPS 时，连接目标必须提供受系统 CA 信任且与连接主机名匹配的证书；Fluent Bit 保持证书校验。已有企业私有 CA 需要先按组织方式加入受信任链，本模块不会生成证书或关闭校验。

## 只生成配置、重跑与离线安装

```bash
# 无需 Docker，只生成配置
./install.sh --configure-only

# 修改网络、标签和日志设置，保留已有凭据
./install.sh --reconfigure

# 仅使用已导入的本地镜像
./install.sh --offline
```

参数可以组合。只生成配置时会在必要时自动准备 Python，不检查或启动 Docker；离线标志禁止联网补依赖和拉镜像，须提前准备基础依赖及镜像。已存在的 `.env` 会被复用；配置变化前会备份并核验原文件。`.env` 含采集密码，Unix 使用权限 600，Windows 在 NTFS 上限制为当前用户、SYSTEM、管理员访问，已有 ACL 修改前会备份 SDDL，不提交或随公共交付包分发。

以下 `.sh` 工具用于 Linux/macOS；Windows 可用 `docker save` / `docker load` 导出导入镜像。离线镜像独立于管理中心导出，在与目标机相同 CPU 架构的联网机器上执行：

```bash
./scripts/export-images.sh
```

把导出的镜像包及校验文件复制到业务机，校验、导入后执行 `./install.sh --offline`。采集模块不需要管理中心镜像。

## 卸载采集端

本目录内运行 `./uninstall.sh --dry-run` 预览，`./uninstall.sh` 删除当前目录所属的采集容器和网络并保留数据；`./uninstall.sh --purge-data` 会先备份并核验 `.env`、配置备份和 Fluent Bit 读取位置/缓冲卷，再清空原配置和卷。Windows 对应为 `.\uninstall.cmd -DryRun`、`.\uninstall.cmd`、`.\uninstall.cmd -PurgeData`。默认需输入 `yes` 确认，可用 `--yes` / `-Yes` 跳过。

只操作当前安装目录的 Compose 资源，不删除宿主机的业务日志目录。备份位于工具包根目录或独立模块目录的 `.uninstall-backups/时间戳/`，包含采集密码与卷 TAR，保留其 `manifest.json` 以核对 SHA-256。备份失败时原容器和数据保留，容器可能已停止。没有容器可证明归属的旧孤立卷不按名称删除；先前若保留数据卸载，应先恢复容器挂载后再清空。

Python、Docker、镜像、源码及离线镜像包保留，卸载器不会安装依赖或启动 Docker；需已有 Python 3.9+ 和可用的本机 Linux Docker 引擎。清空读取位置后再安装将使用全新采集状态，需重新验证新日志写入和检索。

## 检查与验收

```bash
./scripts/check.sh
docker compose ps
docker compose logs --tail=100 fluent-bit
```

Windows 安装器会自动执行检查；日常可运行 `py -3 scripts/check.py`（若没有 py 启动器，使用已安装 Python 的实际路径）。

在日志文件中追加一条可识别的测试记录，然后进入管理中心 `/logs/select/vmui/`，按本机 `host`、项目 `project`、应用 `app` 和环境 `env` 标签检索。每台业务机分别完成一次写入和检索，才能确认整条链路。

默认每行日志形成一条记录，首次启动不回读已有内容；Java 等多行堆栈不会自动合并，需要结合应用日志格式增加多行解析规则。`fluentbit-data` 卷保存读取位置与本地缓冲，日常停止请保留该卷，避免重置采集位置。本地输出缓冲上限为 1 GB，持续无法上传且达到上限时，旧缓冲可能被丢弃，需及时检查连接。

当前采集账户在该部署内共享，标签用于检索，不构成项目级权限隔离。检查脚本和容器启动成功也不等同于已完成目标龙蜥系统或全部业务日志的验收。
