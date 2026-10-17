# OpsKit 企业软件运维工具包 v0.9.0

Hub 是统一的应用入口工作台，在本机和服务器上都能发现本机 Docker 容器、已发布端口及 HTTP/HTTPS 服务，并支持收藏与搜索。管理中心还组合主机运维、数据库查询和集中日志，适合约 3～5 台业务虚拟机；日志采集端独立安装。

| 模块 | 部署位置 | 内容 |
| --- | --- | --- |
| [管理中心](management/README.md) | 一台管理机 | 统一工作台、Nginx Proxy Manager、Spug、MariaDB、DBX、VictoriaLogs |
| [日志采集端](collector/README.md) | 每台业务机 | Fluent Bit，独立配置、安装及离线镜像 |
| [打包与交付 Skill](skills/README.md) | 联网开发环境 | 直出 OpsKit，或完成业务适配、目标依赖与镜像、现场向导及气隙封包 |

管理中心只提供 HTTP，由已有的外层 Nginx 处理 HTTPS。不需要在本包中配置、申请或分发证书。Nginx Proxy Manager 提供代理配置 WebUI。

## 将业务系统制成现场气隙包

把下面一行发送给 Claude Code 或 Codex，即可按[安装说明](skill.md)安装完整 Skill：

```text
安装这个 Skill：https://raw.githubusercontent.com/jakara/opskit/main/skill.md
```

调用时未明确模式，Skill 会先询问“是否直出 OpsKit？”。选择直出只打包 OpsKit 管理端、采集端、安装入口和内置说明，不包含业务应用、镜像或离线系统依赖；选择完整气隙包则按下文完成业务适配与验收。直出不需要业务仓库或 Docker，安装时仍需准备依赖和镜像。

也可将 `opskit.skill` 解压后，把完整 `opskit/` 放入 AI 助手的技能目录。在要交付的业务代码仓库中调用（Claude Code 对应 `/opskit`）：

```text
使用 $opskit，为当前仓库生成现场离线交付包。
目标龙蜥 8.9 x86_64，业务版本 1.2.0。
按现有部署方式划分主机角色，完成断网验收后生成正式包。
```

Skill 会把业务仓库适配、镜像固定、系统依赖收集和业务备份/验收脚本编制留在公司制包阶段。最终压缩包包含业务应用、OpsKit 管理端、采集端、离线 RPM、中文现场参数向导和逐角色 Spug 操作手册；驻场工程师解压后执行包内 `install.sh`，按主机角色填表即可安装。已有参数会保留，升级先备份，有状态数据的恢复按应用适配的策略执行。

开发者可在 `opskit-delivery.json` 中用 `documents` 指定交付资料，支持选择文件或目录：

```json
{
  "documents": ["README.md", "docs/部署说明.md", "docs/现场交接.docx", "docs/附件"]
}
```

Hub 首页提供“交付文档”入口，按交付手册、项目文档和内置说明分组，支持搜索、预览及原件下载。Markdown、文本、PDF、Word `.docx` 和图片均在本地预览，不需要外网；Word 内容预览支持正文、表格和内嵌图片，复杂排版以原件为准。旧 `.doc` 需先转 `.docx` 或 PDF。生成手册和所选资料以普通文件存放在 `opskit/management/documents/`，也可用 vim 查看。缺失、越界或不支持的文档会在制包阶段报错。

当前现场目标为龙蜥 8.9 / 8.10 的 amd64 / arm64；制包端支持具备 Python、Docker/Compose/Buildx 的 Linux、macOS 和 Windows。每个具体交付包都要按目标验收；缺镜像、依赖或完整验收时只生成待验收目录，不能封成正式包。Spug 手册提供可复制字段与发布命令，不会自动在客户 Spug 中注册应用。安装方式、契约和演练仓库见 [Skill 模块说明](skills/README.md)。

## 按需安装，同一个 Hub

| 安装范围 | 启动命令 | 用途 |
| --- | --- | --- |
| 精简管理端（Hub + NPM） | `./install.sh hub` | 发现本机 Docker 和 HTTP 服务，首页 `http://127.0.0.1/`，由 NPM 监听 80 端口 |
| 完整管理中心 | `./install.sh management` | 同一个 Hub，加上 NPM、Spug、DBX、VictoriaLogs 等工具 |
| 日志采集端 | `./install.sh collector` | 每台业务机独立采集日志 |

Hub 不再区分开发模式和生产模式。完整管理中心会预置运维工具入口，也会发现这台管理机上其他应用；远程业务系统可以手动收藏。主机操作、发布、数据库管理仍在 Spug、DBX 等应用内完成。

单独安装 Hub 时会后台启动 Hub 和 NPM 两个容器，默认监听本机回环地址的 80 与 8181 端口；追加 `--port 19090` 可改 HTTP 端口。统一支持入口添加、编辑、移除和搜索，配置保存于服务端，修改前自动备份。旧命令 `development`、`production` 仅保留为安装范围的兼容别名。

## 开始安装

交互安装先显示安装包位置和当前终端目录，再按系统推荐安装位置：

| 系统 | 推荐位置 |
| --- | --- |
| 龙蜥及其他 Linux | `/opt/opskit`、`/srv/opskit`、`~/opskit`；普通用户默认用户目录 |
| macOS | `~/Applications/opskit`、`~/opskit`、`/usr/local/share/opskit` |
| Windows | `%LOCALAPPDATA%\opskit`、`%USERPROFILE%\opskit`、`%ProgramData%\opskit` |

安装包已在本系统推荐位置时直接原地继续，不再询问搬移；完整包内的管理端、采集端也沿用这个位置。其他位置可选“就在当前目录安装”、自定义目录或取消，也可直接输入安装目录的绝对路径。显式指定 `--install-dir` / `-InstallDir` 时采用指定位置。复制后校验文件内容及脚本权限，再从新位置重新进入安装向导；原包保留，配置和后续启动路径以新位置为准。父子嵌套目录、链接和权限不足会立即提示并允许重选。系统共享目录需要写入权限。只复制单个模块时，推荐目录名分别使用 `opskit-management`、`opskit-collector`。

目标已有文件时，可确认覆盖：先将原文件备份到目标目录的 `.backups/install-时间/files/`，逐文件核验并生成 `SHA256SUMS`，通过后才替换程序文件。保留目标现有的 `.env`、`*.env`、工作台状态、数据、项目文档及未在新包出现的文件；新包中新增的文件正常复制。写入失败时恢复本次已修改的原文件，备份继续保留。类型冲突或备份失败会在覆盖前停止。备份、恢复路径会在终端显示。

复制包含普通文件、离线镜像和文档，排除 Git 元数据、历史 `.backups/`、根目录 `dist/`、虚拟环境及 Python 缓存。仅显示帮助、只生成配置或无交互输入时默认使用当前位置；需要复制时可显式指定目录。Linux/macOS 使用 `--install-dir`，Windows 使用 `-InstallDir`；`--keep-location` / `-KeepLocation` 明确保留原位置。非交互覆盖还须显式传入 `--overwrite` / `-Overwrite`，并同样执行备份、保留配置。完整气隙包沿用其现场运行目录，由现场入口自动复制 OpsKit，不重复询问位置。

安装入口会将模块、位置选择写入本机 `.install-options.json`。下次选择模块时，回车采用上次选择；在上次选定的目录直接继续，从原解压目录重跑则以上次的目标目录为默认值。显式目录参数、原地安装参数和已处于推荐目录的规则优先；覆盖已有文件仍须当次确认。

```bash
./install.sh management --install-dir "$HOME/opskit"
./install.sh management --install-dir "$HOME/opskit" --overwrite
./install.sh collector --keep-location
```

```powershell
.\install.cmd management -InstallDir "$env:LOCALAPPDATA\opskit"
.\install.cmd management -InstallDir "$env:LOCALAPPDATA\opskit" -Overwrite
.\install.cmd collector -KeepLocation
```

直接运行安装入口即可。脚本先检查并复用已有 Python 3.9+、Docker 和 Compose V2；缺失时自动准备依赖，然后进入配置向导。在线首次安装需要联网和必要的管理员授权，不再要求用户先手工安装 Python。

- 龙蜥 / RHEL 系：优先使用系统仓库并行安装新版 Python，保留 `platform-python` 和原 `python3`。缺 Docker/Compose 时先尝试已有仓库，必要时加入 OpsKit 专用 Docker 官方 RHEL 仓库，仓库变更前备份并核验。
- Ubuntu / Debian：通过系统包管理器安装依赖，必要时加入 Docker 官方仓库；已可用的 Docker 不更换、不升级。
- macOS：优先复用已有依赖；缺失时通过 Homebrew 安装，缺 Homebrew 时启动其官方安装程序。缺 Docker 时安装 Docker Desktop，首次启动的系统授权和许可确认在应用界面完成。
- Windows 10/11：提供原生 PowerShell / CMD 入口，通过 WinGet 安装 Python 和 Docker Desktop；缺 WinGet 时使用微软官方模块准备。Docker 须运行 Linux 容器，缺 WSL 2 时尝试安装；UAC、厂商首次许可和系统要求的重启仍需用户在系统界面完成，然后重跑同一命令继续。[Docker Windows 安装说明](https://docs.docker.com/desktop/setup/install/windows-install/)

脚本不会删除 Podman/runc、强制解决包冲突、改 Docker 用户组或 `daemon.json`，也不会修改防火墙、DNS 和外层 Nginx。Linux 缺监听查询工具时补装 `iproute` / `iproute2`；Windows 使用系统 PowerShell 读取监听清单。仓库不可达或运行时冲突会给出具体失败信息，保留已有配置。

```bash
# 交互选择管理中心、日志采集端或仅运行 Hub
./install.sh

# 在管理机运行
./install.sh management

# 在业务机运行；也可只复制 collector/，在其中运行 ./install.sh
./install.sh collector
```

Windows 在解压目录打开 PowerShell，使用同一个安装入口：

```powershell
# 也可双击 install.cmd，按提示选择模块
.\install.cmd hub
.\install.cmd management
.\install.cmd collector

# Windows 使用命名参数；可组合
.\install.cmd management -ConfigureOnly
.\install.cmd management -Reconfigure
.\install.cmd hub -Offline -Port 19090
```

Windows 独立模块直接运行目录内的 `.\install.cmd`，管理模块的仅 Hub 参数为 `-HubOnly`。CMD 仅为本次 PowerShell 进程使用 RemoteSigned 策略，不更改系统执行策略；从网络下载的 ZIP 若被 Windows 标记为受阻，先按系统提示解除可信文件的锁定再解压。Windows 配置目录应位于 NTFS，本包将凭据文件和备份限制为当前用户、SYSTEM 和管理员访问，已有 ACL 修改前保存可恢复的 SDDL。Windows Server 不在本次支持范围内。

向导填写网络、账户、保留时间或本机日志路径，随后校验配置、拉取镜像、启动容器并检查连接。管理端会自动初始化 NPM 管理员和代理规则，并生成外层反代示例及采集端连接配置。首次部署后，在 Spug 中添加实际 SSH 主机，在 DBX 中添加实际数据库连接。

管理端和采集端每确认一项有效输入，就立即保存到本模块 `.env.install-progress`；包括已确认的密码和镜像地址。异常、Ctrl+C 或输入中断后，重跑同一入口会以上次确认的值为默认值，回车即可保留。只有全部校验通过才写入正式 `.env` 并清除草稿；重新配置中断时原正式配置保持不变。已完成配置直接复用，修改时用 `--reconfigure`（Windows：`-Reconfigure`）。精简管理端也会复用上次的 HTTP 端口，显式指定新端口时才更改。

草稿含凭据，和正式配置一样使用受限权限及写入前备份，不打印密码、不进入 Git 或通用 Skill/工具包；只留在对应安装机。清空数据卸载会先备份并核验，再清除对应模块的草稿。

镜像逐个下载，单个失败不会打断其他镜像；网络超时等临时故障重试一次。Spug 和 DBX 默认使用官方 GHCR、CNB 的固定版本，已知官方同版本地址可自动互为备用；切换成功后先备份原配置，再保存可用来源。自定义仓库、摘要引用、环境变量指定的来源及气隙包锁定镜像不自动改写。仍有缺项时列出具体镜像、错误与续装命令，保留配置和已下载镜像，全部准备好后才启动容器与宿主机发现进程。离线模式只检查本地镜像，缺失时提示导入，不尝试联网。

```bash
# 先填配置，暂不启动容器
./install.sh management --configure-only

# 修改已有网络配置；保留已有凭据，修改前备份
./install.sh management --reconfigure

# 离线：基础依赖已准备、镜像已导入后安装
./install.sh management --offline
```

管理机默认 HTTP 首页 `http://127.0.0.1/`（80 端口）、NPM 管理界面 `127.0.0.1:8181`、Spug 独立入口 `127.0.0.1:8081`。外层代理或采集端位于别的机器时，向导中须改成可达的管理机内网监听地址。外层保留管理域名的 Host 头并转发到 HTTP 入口。

管理机安装后，将 `management/collector.env` 通过受控渠道复制为业务机 `collector/.env`，再运行采集向导补齐标签、日志目录及文件规则。该连接配置只包含采集凭据；不要分发管理中心的 `.env`。默认直连管理机私网 HTTP，也可在采集向导中选择已有外层 HTTPS。

`--configure-only`（Windows：`-ConfigureOnly`）只准备 Python 并生成配置，不检查、安装或启动 Docker。`--offline`（Windows：`-Offline`）禁止联网补依赖及拉镜像；首次离线交付须预装基础依赖并导入镜像，本包不包含系统安装包。

## 目录与日常使用

```text
install.sh                 Linux / macOS 统一安装入口
install.cmd / install.ps1   Windows 统一安装入口
management/                可独立复制的管理中心
  install.sh / install.cmd  管理端安装向导
  compose.yaml             管理端服务
  hub.compose.yaml         单独安装 Hub 与 NPM，共用服务定义
  scripts/                 检查、备份、镜像导出、代理初始化
  portal/                  统一 Hub、只读宿主机发现服务
  documents/               随包文档库，Hub 只读预览
  ai/README.md             AI 扩展约定（尚未实现 AI 服务）
collector/                 可独立复制的日志采集模块
  install.sh / install.cmd  采集端安装向导
  compose.yaml             Fluent Bit 服务
  fluent-bit.conf          日志采集规则
skills/                    气隙交付 Skill 与分发打包器
  opskit/            工作流、制包工具、现场运行时、契约与独立演练仓库
  package_skill.py          生成包含当前 OpsKit 源码快照的自包含 Skill
  scripts/                 检查与镜像导出
```

首页 `/` 直接显示 Hub；原 `/hub/` 自动跳转到 `/`。OpsKit 内置工具单独置顶并显示端口与路径，普通收藏、Docker 和其他 HTTP 服务在下方。Spug 使用独立的 8081 端口，DBX 位于 `/dbx/`，日志位于 `/logs/select/vmui/`。NPM WebUI 使用独立管理端口，可通过 SSH 隧道访问。人工访问与日志写入使用两套账户，采集账户不能查询日志或打开运维界面。

重复安装不会清空数据或重置已有密码。旧版工作台升级会先备份，将本包原始 Spug 根路由改为 Hub，并迁移原始静态导航块，保留其他 WebUI 代理规则。每个模块均有 `scripts/check.sh` 和 `scripts/export-images.sh`，后者为 Linux/macOS 的镜像导出入口；Windows 可使用 `docker save` / `docker load` 导出导入镜像。具体备份、离线交付与验收方式见各模块中文说明。备份文件可能包含密码，和 `.env` 一起受控保存。

## Hub 发现与升级

两种安装范围都由安装器启动宿主机只读发现进程，每 30 秒生成一次清单，Hub 容器只读挂载该清单，不挂载 Docker socket。Linux 以 root 安装且有 systemd 时自动配置发现进程自启；其他环境使用后台进程，重启宿主机后需重跑安装器。快照超过 90 秒未更新会在页面提示，保留上次结果。

HTTP 发现只对本机已经监听的 TCP 端口发送 HEAD，不扫描局域网、不跟随重定向，也不读取页面或登录凭据；每轮最多检查 128 个监听端口。HTTP 401/403/404 等响应也表示识别到了 HTTP 服务，不代表业务健康。仅绑定回环地址的服务仍只能在宿主机或经 SSH 隧道访问；远程访问应在 NPM 配置代理，并收藏实际访问地址。未发布端口的容器只展示信息，通过已配置的工具卡片或手动入口访问。

单独 Hub 的 NPM 登录信息保存在 `management/.workbench/hub.env`，Unix 权限为 600，Windows 使用受限 NTFS ACL；安装时自动生成密码，重跑保留。其首页只在本机回环地址开放，不增加第二套登录。完整管理中心继续使用运维入口鉴权。

旧版默认 HTTP 首页从 8080 升级到 80，同时通过 `legacy.compose.yaml` 保留原 8080 日志接入口，已有采集端可以继续使用。新装 Spug 使用 8081，采集端新配置使用 80。安装器修改前备份配置。原 Spug 首页收藏 `/` 会迁移到其独立端口，自定义访问地址保留。外层 Nginx 的上游端口也需相应调整。

v0.3 单独 Hub 的已有收藏会复制并校验到 `management/.workbench/hub/`，原 `.workbench/development/` 保留以便恢复。容器版继续使用原 `workbench-data` 卷，不覆盖已有收藏。

## 卸载与重新测试安装

Linux / macOS 在工具包根目录执行：

```bash
# 先预览当前目录对应的资源，不修改任何内容
./uninstall.sh --dry-run

# 卸载当前目录的管理端和采集端，保留配置、收藏及数据卷
./uninstall.sh

# 重新测试首次安装：核验备份后清空配置、收藏和数据卷
./uninstall.sh --purge-data

# 也可指定范围；加 --yes 跳过交互确认
./uninstall.sh hub --purge-data --yes
```

Windows 使用对应入口：

```powershell
.\uninstall.cmd -DryRun
.\uninstall.cmd
.\uninstall.cmd -PurgeData
# 只卸载独立 Hub 与 NPM
.\uninstall.cmd hub -PurgeData -Yes
```

可选范围为 `all`（默认）、`hub`、`management`、`collector`。只复制单个模块时，直接运行该模块内的 `uninstall.sh` 或 `uninstall.cmd`。管理模块默认包含同目录的完整管理中心和独立 Hub；`hub` 只选独立 Hub，不会连带卸载另一套完整管理中心。

卸载器根据 Compose 标签中的安装目录及配置文件确认归属，不按容器名称前缀猜测，也不执行全局清理。它停止对应容器和宿主机发现进程，删除对应容器、网络及发现进程的 systemd 单元。发现进程仍被同目录另一安装范围使用时会保留。远程 Docker 上下文、同名项目跨目录、卷或网络被其他容器共享时会拒绝操作。

`--purge-data` / `-PurgeData` 会先停止服务，将本地配置、收藏和卷内容备份到工具包根目录的 `.uninstall-backups/时间戳/`（独立复制的模块位于模块目录内），逐文件及 TAR 读取检查通过后才删除原数据。`manifest.json` 记录备份文件的 SHA-256；备份含原密码，应受控保管。备份失败时保留原容器和数据，容器可能已停止，修复原因后可重跑；不要把此时当作卸载成功。原数据库逻辑备份目录、离线镜像包及源码保留。

卸载不会移除 Docker、Docker Desktop、Python、镜像或其他应用。因此重新运行安装器会复用现有基础依赖和缓存镜像。卸载时需要已安装的 Python 3.9+ 和可用的本机 Linux Docker 引擎，脚本不会安装依赖或启动 Docker。

没有容器可证明归属的孤立卷不会被按名称删除。如果此前选择保留数据卸载，之后再想清空旧卷，应先恢复该套件的容器挂载，再使用清空参数卸载。已有容器项目中存在无法读取备份的孤立卷时，清空操作会在停止服务前报错。

清空卸载成功后，再运行 `./install.sh` 或 `.\install.cmd`，即可从首次配置开始手动测试。

## 从 v0.1 模板迁移

旧根目录 `.env` 存在时，管理向导会只读导入并补齐新配置；原文件保留。已有部署应先备份配置和持久卷，再停止旧 Compose 中的 nginx 服务，最后部署新模块。旧管理服务使用相同 `opskit` 项目名和卷名，可以复用数据；若曾使用 `-p` 指定其他项目名，继续通过 `COMPOSE_PROJECT_NAME` 指定同名项目，避免连接到新空卷。

原 `agents/` 替换为独立 `collector/`。旧采集端配置可复制到新目录，再运行 `./install.sh --reconfigure` 补齐 HTTP 端口和 `OPS_VHOST` 等字段。默认采集端项目名仍为 `opskit-collector`，可保留读取位置卷。旧 `nginx/tls`、`nginx/auth` 和 `ca.pem` 不再由新模块使用；确认迁移成功后再由管理者处理旧文件。

v0.7 卸载新增 13 项自动检查，与原功能合计 62 项通过。Windows 卸载入口在禁网 PowerShell 7.4 容器中通过 59 项语法及模拟流程断言。隔离真实 Docker 项目验证了预览无修改、数据卷 TAR 内容及 SHA-256 备份、清空卸载、重复运行和其他容器保留。原生 Windows 与 Linux systemd 的实际卸载仍需目标机验收。

## 验证与边界

开发检查：在工具包根目录执行 `python3 -m unittest discover -s tests -v`。测试覆盖安装输入、持久化与备份、只读发现、监听端口解析、HTTP HEAD 探测、快照异常、接口边界和旧版代理升级。

v0.2 阶段在本机 ARM64 Docker 的隔离项目中，已验证 NPM 从空白数据初始化、重跑保留 WebUI 修改、Spug 和 DBX 登录、全部入口鉴权、真实 Fluent Bit 新增文件日志写入 VictoriaLogs 及四标签检索；Spug 数据库逻辑备份也已生成。Spug、DBX 分别使用官方 GHCR、CNB 同版本镜像。未执行完整离线导入或备份恢复。

v0.3 已验证真实本机 Docker 发现、浏览器入口增删改与搜索，以及隔离 NPM 中的工作台反代、运维/采集账户隔离、旧静态路由升级和自定义代理规则保留。该次 NPM 验收中的其他业务上游只提供 DNS 别名，未重新启动 Spug、DBX 等全套服务。

v0.4 已通过 22 项自动检查，实测 macOS 本机 Docker 与 HTTP/HTTPS 发现、浏览器 HTTP 服务收藏/编辑/搜索/移除，以及隔离 NPM 与 Hub 容器的宿主机快照展示、访问隔离和重启后收藏保留。Linux 监听解析和 systemd 单元配置由测试样本验证，尚未在龙蜥实机执行自启验收。

v0.5 将 NPM 首页设为 80 端口根路径，内置工具独立置顶并显示端口。25 项自动检查通过；隔离的 6 个真实管理服务验证了根首页、Spug 独立端口登录、DBX/日志入口鉴权、原根代理与 Spug 收藏迁移、自定义规则保留，以及旧日志接入口的兼容。

v0.6 增加 Linux/macOS 自动依赖引导及原生 Windows CMD/PowerShell 安装入口。49 项 Python 自动检查通过；微软官方 PowerShell 7.4 Linux 容器中通过 5 个交付脚本的语法解析、40 项禁网模拟断言，并另行解析了 Windows ACL 和监听查询脚本。在龙蜥官方 Anolis OS 8.9 ARM64 隔离容器中，实际从系统仓库安装 Python 3.11.13，成功生成 Hub 配置，原 platform-python 3.6.8 包保持原版本。

这不代表龙蜥实机的 Docker 服务或重启自启已经验收，也不代表原生 Windows、Windows PowerShell 5.1、NTFS ACL、UAC、WSL 和 Docker Desktop 的实际安装已经验收。Windows 分支的当前证据是语法解析与模拟流程测试。

目标环境仍需验证外层 HTTPS、真实 SSH 纳管、数据库查询，以及每台业务机追加日志后的跨主机检索。容器启动或模拟后端测试不代表已完成这些验收。

v0.8 增加自包含的 `opskit-airgap` Skill。独立分发快照已在另一个演练仓库完成真实镜像导出、完整暂存和缺项验收拒绝封包；龙蜥官方 8.9 ARM64 干净容器中完成 194 个签名 RPM 的禁网安装，原系统 Python 保留。现场 bootstrap 的仅 Python、全依赖安装及检查入口均实测，无 daemon/systemd 时准确停止。业务运行时由 macOS 控制器在独立 Linux ARM64 Docker 项目中完成安装、检查、升级、回退及 HTTP 版本核对，测试后其他容器未变。

上述业务组件验证未执行完整龙蜥 VM 的现场入口，也不是全套 OpsKit 的断网安装验收。原生 Windows 制包、其他龙蜥版本/CPU 组合、真实业务数据恢复及客户 Spug 发布仍须随具体项目验收；不以通用演练报告替代客户应用的验收记录。

v0.9 将 Skill 分发名统一为 `opskit.skill`，调用名为 `$opskit`。开发者指定的项目资料、自动生成的现场/Spug 手册及内置说明可在 Hub 离线搜索、预览和下载。已在真实浏览器验证 Markdown 相对链接与图片、Word `.docx` 标题/表格/内嵌图片、PDF 中文分页及原件下载；文档接口和原 Hub 功能的 19 项回归通过。Word 提供内容预览，复杂版式以原件为准；旧 `.doc` 制包前需转为 `.docx` 或 PDF。

本包仍使用应用自身账户和共享入口鉴权，没有实现统一 SSO 或逐人审计；AI 诊断目前只有扩展约定。各上游组件保留其原有开源许可，本工具包提供部署配置与安装脚本。
