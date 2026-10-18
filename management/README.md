# OpsKit 管理中心

在独立管理机上集中运行统一工作台、Nginx Proxy Manager、Spug、DBX、VictoriaLogs；业务机只需安装另一份 `collector/` 日志采集模块。两者可以分别复制、安装和离线交付。

直接运行安装入口时会显示当前安装包位置；已在推荐目录就原地继续，否则可选推荐位置、输入自定义目录或原地安装。只复制本模块时目录名使用 `opskit-management`。可用 `--install-dir /实际目录` 指定位置或 `--keep-location` 保留当前目录；Windows 对应 `-InstallDir`、`-KeepLocation`。目标非空时允许确认覆盖，先在 `.backups/install-时间/` 备份并校验，保留现有配置、数据和项目文档；失败恢复本次修改。非交互覆盖须加 `--overwrite`，Windows 为 `-Overwrite`。只生成配置或非交互调用默认不搬移。

外层已有 Nginx 负责 HTTPS。本模块只提供 HTTP，默认首页为 `http://127.0.0.1/`（80 端口），Nginx Proxy Manager（下称 NPM）管理界面为 `/npm/`；不映射 443，不要求用户准备证书，也不申请业务证书。NPM 镜像启动要求的 `/etc/letsencrypt` 使用独立空数据卷挂载，HTTP 代理不使用它。

## 统一应用工作台（Hub）

首页“交付文档”可搜索、预览和下载现场/Spug 手册、项目资料及内置说明。支持 Markdown、文本、PDF、Word `.docx` 和常见图片，所有预览资源均在本地；Word 支持正文、表格和内嵌图片，复杂排版可下载原件查看。旧 `.doc` 先转 `.docx` 或 PDF。

文档以普通文件保存在本模块 `documents/`，Hub 以只读方式挂载；也能用 vim 查看。开发者在业务制包配置 `documents` 中指定文件或目录，生成手册自动收录。直接给现有管理端补资料时可放到 `documents/` 独立本地目录，然后在文档中心刷新；不能将实际 `.env`、密钥或业务日志作为文档。详细约定见 [文档中心说明](documents/README.md)。

Hub 在本机和服务器上具有相同能力：收藏入口、发现本机 Docker 容器与端口、识别其他 HTTP/HTTPS 服务，以及统一搜索。完整安装后固定首页为 `/`，由 NPM 监听 80 端口，旧 `/hub/` 自动跳转到 `/`，首次预置 Spug、DBX、VictoriaLogs 和 NPM；也可加入管理机上的其他应用。真正的主机、发布及数据库操作仍在各工具中完成。

只需要工作台时，在工具包根目录运行 `./install.sh hub`，或在管理目录运行 `./install.sh --hub-only`。默认访问 `http://127.0.0.1/`，安装后 Hub 和 NPM 两个容器在后台运行；可追加 `--port 19090` 更换 HTTP 端口。NPM 管理页面是 `http://127.0.0.1/npm/`，登录信息保存在 `.workbench/hub.env`（Unix 权限 600，Windows 使用受限 NTFS ACL），自动生成后重跑保留。首页只绑定本机回环地址，沿用本地 Hub 的访问范围。这只是安装范围的选择，不是另一种 Hub 模式，也不需要启动其他运维容器。

宿主机发现进程读取当前本机 Docker 上下文、Compose 项目、状态及已发布端口；远程 Docker 上下文会提示切回本机。HTTP 发现读取实际监听端口后发送 HEAD，识别普通 HTTP 与 HTTPS，不跟随重定向，不读取响应正文、Cookie 或凭据。容器发布端口不在其他 HTTP 服务中重复显示。Linux 使用 `ss`（安装器可补装 iproute / iproute2），macOS 使用 `lsof`，Windows 使用 PowerShell 的 `Get-NetTCPConnection`；每轮最多检查 128 个监听端口。数据库等非 HTTP 端口只展示映射，不提供 Web 打开按钮。HTTP 响应码不等于业务健康状态，登录页面或未知路径也可能返回 401、403、404。

“收藏入口”预填地址，可修改中文名称、分组、URL 和说明。入口可添加、编辑、移除，搜索支持名称、分组、地址、容器名和端口。独立 Hub 的收藏保存在 `.workbench/hub/entries.json`，修改前自动备份到同目录 `backups/`；首次升级会复制旧 `.workbench/development/` 的入口及备份并核验入口内容，保留原目录。页面每 30 秒刷新，也可手动刷新。

两种安装范围的 Hub 均运行于容器，使用相同的发现逻辑：安装器在宿主机启动发现进程，每 30 秒写入 `.workbench/discovery/state.json`，容器只读挂载该目录，网页服务不接触 Docker socket。完整管理端额外以 `discovery.py --control` 启用受限服务操作，只消费 Spug 专用共享卷中的固定请求；精简管理端不启用控制通道。Linux 以 root 安装且有 systemd 时，安装器保存并启用 `opskit-discovery-<安装路径摘要>.service`，支持重启自启和失败重启；原服务配置修改前自动备份。非 root、无 systemd，以及未配置自启的 macOS/Windows 环境，均按安装器提示启动后台进程，宿主机重启后需重跑原安装命令，恢复清单刷新和控制通道。超过 90 秒没有新清单时提示结果已过期。停止发现进程不会停止业务容器，但清单刷新和服务操作通道都会中断。

完整管理中心的入口目录继续存储于 `workbench-data` 持久卷，修改前自动备份。接口使用运维入口的 Cookie 会话鉴权，采集账户无权读取或修改；使用同一入口账户的人共享收藏。Hub 只发现当前宿主机，不跨远程 VM 发现应用，远程业务系统直接登记实际入口。

只有绑定公网或内网地址的端口才可能被其他电脑直接访问。回环端口在远程浏览器中显示“仅宿主机访问”；可在 NPM 配置反代，再收藏最终 URL。NPM、Spug 卡片默认使用同域名 `/npm/`、`/spug/`，浏览器不再需要分别连接管理端口。收藏入口不会自动开放端口或创建代理规则。

## 基础依赖自动准备

直接运行本模块安装入口，无需先手工安装 Python。脚本复用可用的 Python 3.9+、Docker 和 Compose V2，缺失时在线补装；Linux 通过包管理器，macOS 通过 Homebrew，Windows 通过 WinGet。龙蜥优先并行安装新版 Python，不替换系统解释器。Linux 缺 Docker 时优先使用现有仓库，必要时添加 OpsKit 专用 Docker 官方仓库，原文件变更前备份并核验。已健康的 Docker 不升级、不更换，不删除冲突的运行时包。

在线首次安装需要联网及必要的管理员授权。Docker Desktop 首次许可、系统授权及要求的重启由用户在系统界面完成；完成后重跑同一命令继续，不清空配置。Windows 10/11 使用 Docker Desktop 的 Linux 容器，必要时引导准备 WSL 2；本次不支持 Windows Server。macOS 缺 Homebrew 时运行其官方安装程序。

事先确定管理域名、外层最终访问地址、管理机监听地址和业务机可连接的日志接入地址。离线安装需先准备基础依赖，并导入相同 CPU 架构的镜像；离线标志禁止联网补依赖和拉镜像。安装器不改防火墙、DNS、外层 Nginx、Docker 用户组或 daemon.json。

## 一站式安装

从工具包根目录运行：

```bash
./install.sh management
```

只复制了 `management/` 时，在本目录运行：

```bash
./install.sh
```

Windows 在本模块目录的 PowerShell 中运行：

```powershell
.\install.cmd
# 只安装 Hub 与 NPM
.\install.cmd -HubOnly
# 只生成配置，不安装/启动 Docker
.\install.cmd -ConfigureOnly
# 重配或离线安装
.\install.cmd -Reconfigure
.\install.cmd -Offline
```

只复制本模块也可使用上述入口，不依赖工具包根目录。Windows 参数可组合，Hub 可追加 `-Port 19090`。CMD 为本次进程采用 RemoteSigned，不改系统执行策略；下载 ZIP 被系统锁定时先解除可信文件锁定再解压。配置目录应在 NTFS 上，凭据文件及其备份使用当前用户、SYSTEM、管理员专属 ACL，已有 ACL 修改前保存并核验 `.acl.txt` 格式的 SDDL。Unix 凭据权限为 600。

向导依次配置以下内容，回车采用提示中的默认值；密码输入不回显，留空可自动生成。

全部服务启动并通过检查后，终端会统一回显一次 NPM、Spug、DBX、运维入口和日志写入的配置凭据，并显示保存路径。完整管理端保存在 `.env`，精简管理端保存在 `.workbench/hub.env`、回显运维入口和 NPM 登录信息；仅生成配置、取消或安装失败时不回显。Spug 数据库内部密码保存在配置中，不单独显示。在应用内自行修改过的账户和密码，以应用实际设置为准。

| 配置 | 含义与默认值 |
| --- | --- |
| 管理域名、最终协议和端口 | 浏览器经过外层 Nginx 后使用的地址，默认 `https://ops.example.internal:443` |
| HTTP 监听 IPv4 和端口 | NPM 的 Hub 首页及日志接入，默认 `127.0.0.1:80` |
| Spug 诊断端口 | 默认 `8081`；常用入口为 `/spug/` |
| NPM 初始化与诊断端口 | 默认 `127.0.0.1:8181`，仅供初始化及恢复；常用入口为 `/npm/` |
| 日志接入主机 | 业务机可以连接的管理机内网 IP 或域名，不含协议、端口和路径 |
| 日志保留天数 | 默认 14 天 |
| NPM 管理员 | 邮箱登录名及密码；邮箱仅作登录名，不会发送邮件 |
| Spug 管理员 | 初始用户名及密码 |
| 运维入口账户 | 站内登录页使用的 operator 账户；登录后可进入全部内置运维组件 |
| 日志写入账户 | 采集端专用 HTTP Basic 账户，必须与运维账户不同；重名时立即提示并要求重新输入 |
| DBX 登录密码 | 内置管理密码，由后端代登录；不再要求浏览器重复输入 |
| 各服务镜像地址 | 回车使用固定版本，也可填写官方备用源或私有仓库 |

如果外层 Nginx 或业务机位于另一台机器，HTTP 监听地址应填写管理机可达的内网 IPv4，不能使用 `127.0.0.1`。日常通过同域名 `/npm/` 访问代理管理。后端 API 诊断端口可继续仅监听本机；需要检查初始化状态时，可用 SSH 隧道，例如：

```bash
ssh -L 8181:127.0.0.1:8181 管理员@管理机地址
```

然后可查询 `http://127.0.0.1:8181/api/` 的后端状态。预编译前端使用子路径，原端口根路径不是备用 WebUI；常规管理界面仍从 `/npm/`、`/spug/` 打开。

向导会生成配置、拉取并启动容器，创建 NPM 初始管理员，生成运维入口会话配置，通过 API 建立采集访问列表，并创建可以在 NPM WebUI 中编辑的 Proxy Host。子路径规则保存在该主机的 Advanced 配置中。Spug 初始管理员由其镜像初始化，不需要手工查找随机密码。

重复运行会复用已有凭据，保留已有收藏及 WebUI 配置。旧版默认 HTTP 首页从 8080 迁移到 80，安装器同时启用 `legacy.compose.yaml` 将旧 8080 继续转发到 NPM，兼容已有 Fluent Bit 的写入。Spug 新默认端口是 8081；原配置修改前自动备份。新生成的 `collector.env` 使用 80，采集端可逐台更新。外层 Nginx 的上游也可改为 80；自定义的 HTTP 端口不自动改写。

本包生成的原 Spug 根代理会备份后更新为 `workbench:8090`，其他自定义代理规则保留。原静态 `/hub/` 导航块也会迁移。若根代理已改成其他上游，脚本不会覆盖，应在 NPM WebUI 中自行将该主机根上游指向 `workbench:8090`。旧 Spug 收藏 `/` 会迁移为其独立端口，已自定义的访问地址保留。

OpsKit 内置工具独立置顶并显示访问端口和路径，可编辑地址，不能从工作台移除；普通收藏继续支持增删改。单独安装 Hub 只置顶实际安装的 NPM，完整管理中心默认置顶四项。

## 配置文件与重跑

安装生成的文件均在本目录：

- `.env`：管理中心全部配置和凭据，Unix 权限为 600，Windows 使用受限 NTFS ACL，保留在管理机。
- `.env.install-progress`：未完成向导的私密草稿，每项有效输入确认后立即保存。中断后重跑时恢复为默认值，已确认的密码保留；全部通过校验及正式配置输出完成后删除。重新配置时正式 `.env` 在完成前保持不变。
- `.workbench/hub.env`：单独 Hub 与 NPM 的配置及管理凭据，Unix 权限为 600，Windows 使用受限 NTFS ACL。单独安装的首页仅本机访问，不能将此文件的 `HUB_ONLY` 配置直接用于远程管理入口。
- `collector.env`：仅包含日志接入地址、协议、端口、虚拟主机名及采集账户，交给业务机使用。
- `outer-nginx.conf.example`：外层已有 HTTPS 服务需要合并的反向代理片段。

```bash
# 只填写并生成配置；不需要 Docker，也不启动容器
./install.sh --configure-only

# 重新引导网络配置；保留已有密码
./install.sh --reconfigure

# 仅使用本地依赖及已导入的镜像；禁止联网补装
./install.sh --offline
```

仅生成配置时仍会在缺失时自动准备 Python，但不操作 Docker。参数可以组合，例如 `./install.sh --configure-only --reconfigure`。修改已有配置前，脚本会先在 `.backups/` 中保存并核验原文件；保留这些备份以便恢复。

发现已有配置时提供三项，默认选择第一项：

1. **复用已有配置**：继续使用现有网络、密码和数据。
2. **重新配置（保留已有密码和数据）**：重新引导网络等配置；精简管理端可重配 HTTP 与 NPM 管理端口。
3. **完全重新安装（清空所有密码和数据，先备份）**：展示当前安装目录、容器及数据卷，输入 `yes` 后，先备份并核验，再清空当前安装的凭据、登录态、代理规则、数据库、日志及 Hub 数据，进入全新配置向导；密码留空会重新生成。取消或归属检查失败时不停止服务；备份失败时不删除原数据，但服务可能已经停止。

完整管理端的第三项清理当前管理目录下的完整和精简安装；精简管理端仅清理自身，保留同目录完整管理端的账户和数据。两者均保留源码、`documents/` 交付资料、Docker、Python、镜像、离线包及其他应用；管理端重装不修改业务机日志。可恢复备份保留在 `.uninstall-backups/时间戳/`，其中仍含原凭据。完全重装后须将新的 `collector.env` 重新发给各采集端。

存在中断草稿时仍可选择第三项；成功清空后不会沿用原草稿。第三项不能与 `--configure-only` 同时使用，也不会猜测删除缺少原容器挂载证明的数据卷。旧版根目录配置需先由原卸载入口处理，避免遗留旧数据。

明确传入 `--reconfigure`（Windows：`-Reconfigure`）直接重配；自动化可传 `--reuse-config`（Windows：`-ReuseConfig`）明确复用，二者不能同时使用。没有选择且输入结束时停止，不自动继续安装。

没有正式配置时，未完成草稿会自动进入向导；已有正式配置时选择复用即可继续草稿，回车采用上次确认的选项。精简管理端的 HTTP 端口默认沿用 `.workbench/hub.env`，也可显式传入 `--port` / `-Port`。安装模块和位置保存在 `.install-options.json`，这些本机文件均不随通用工具包分发。

`--reconfigure` 不会自动修改已有 NPM 主机和访问列表。改变域名或账户名称后，还须在 NPM WebUI 中核对对应配置；更改 Spug 初始化变量也不会重置已有账户。不要将手工修改 `.env` 当作已有应用密码的修改方式。

在线安装默认使用下列固定版本。2026-10-17 在本机制包环境核验了全部地址的 amd64/arm64 manifest，并实际拉取五项 DaoCloud 镜像；其摘要与本机缓存的官方同版摘要一致。公共加速仍可能限流或调整白名单，该核验不能保证客户内网可达。

| 组件 | 默认镜像 |
| --- | --- |
| Hub Python | `m.daocloud.io/docker.io/library/python:3.12-alpine` |
| NPM | `m.daocloud.io/docker.io/jc21/nginx-proxy-manager:2.16.0` |
| MariaDB | `m.daocloud.io/docker.io/library/mariadb:10.11` |
| VictoriaLogs | `m.daocloud.io/docker.io/victoriametrics/victoria-logs:v1.53.0` |
| Fluent Bit（采集模块） | `m.daocloud.io/docker.io/fluent/fluent-bit:5.1.4` |
| DBX（官方国内发布） | `docker.cnb.cool/dbxio.com/dbx:0.6.40` |
| Spug（官方境外发布） | `ghcr.io/openspug/spug:4.1` |

来源：[DaoCloud 加速说明](https://github.com/DaoCloud/public-image-mirror)、[DBX 官方说明](https://github.com/t8y2/dbx)、[Spug 官方安装说明](https://ops.spug.cc/docs/install-docker/)。Spug 4.1 尚无已核实的官方国内同版源，旧阿里云镜像不能替代当前版本；客户无对应网络时用 Skill 提前制作运维工具独立气隙包。

登记的同版本国内/官方来源可互为备用，网络超时等临时错误重试一次。备用成功后先备份并核验配置再保存可用地址，保留已有密码。旧 Spug 配置被加速服务白名单拒绝时仍会尝试官方 GHCR。

单个失败不会中断其他镜像下载。所有镜像就绪前不启动本次容器或发现进程；仍失败时汇总具体镜像、原因和 Docker 原始错误。按提示处理网络、仓库授权、磁盘空间或 CPU 架构后，原地重跑 `./install.sh`（Windows：`.\install.cmd`），无需重填配置。精简管理端使用 `./install.sh --hub-only`（Windows：`.\install.cmd -HubOnly`）。

本目录已有运行中的 Compose 容器时，安装器核对安装目录及 Compose 文件归属，在线刷新配置指定版本的镜像；全部准备完成后重建并重启服务，即使镜像版本未变也会重启。离线安装只用已导入镜像，不联网。持久卷、配置和应用内数据保留；同名项目若属于另一目录或安装范围则停止，不操作其他应用，也不自动切换到 `latest`。

自定义仓库、摘要引用、环境变量指定的镜像及 `image-defaults.json` 锁定的气隙包镜像不自动替换，也不修改 Docker 的镜像加速配置。可手动将 `.env`（精简管理端为 `.workbench/hub.env`）中的对应 `*_IMAGE` 改成已同步到内网仓库的同版、同架构镜像；私有仓库先 `docker login`。离线模式逐个检查本地镜像，缺失时列出需 `docker load` 导入的镜像，始终禁止拉取。

## 接入外层 Nginx 与业务机

将 `outer-nginx.conf.example` 合并到外层已有 HTTPS `server`，由外层管理员核验并重载。保留原始路径、`Host`、Cookie、Authorization 和 WebSocket 头；转发目标为本模块的 HTTP 监听地址。外层在其他机器或容器中时，须改用它能连接的管理机地址。证书继续由外层管理，不要再增加一套会覆盖采集账户的 Basic Auth。

| 功能 | 最终访问路径 |
| --- | --- |
| 统一应用工作台 | `/`，默认 80 端口 |
| NPM 代理管理 | `/npm/` |
| Spug 主机运维 | `/spug/` |
| DBX 数据库管理 | `/dbx/` |
| VictoriaLogs 日志查询 | `/logs/select/vmui/` |
| Fluent Bit 日志写入 | `/logs/insert/jsonline`，仅允许采集账户写入 |

NPM 2.16.0 与 Spug 4.1.1 使用包内预编译的子路径前端，安装机无需 Node.js。安装前校验前端文件和镜像身份；不兼容的自定义版本会停止安装，不能随意替换为 `latest`。镜像别名与离线导入均支持，必须与适配版本一致。构建与修改说明见 [前端适配说明](ui/README.md)；Spug 页面提供许可证与完整对应源码下载（`/spug/opskit-corresponding-source.tar.gz`），气隙包也包含该文件。

### 一次登录进入内置组件

访问首页时显示中文登录页，使用安装器配置的运维入口账户（默认 `operator`）。登录一次即可打开 Hub、Spug、NPM、DBX 和日志界面，不再弹出浏览器 Basic Auth 对话框。会话有效期为 12 小时，重启 Hub 后保留；到期后重新打开页面进入站内登录页，接口请求返回 401。首页右上角可退出登录。精简管理端也采用相同登录方式。

Spug、NPM 和 DBX 通过后端调用其固定版本的原生登录接口，映射到 `.env` 中配置的内置管理账号；组件密码不返回浏览器，浏览器只接收组件原生令牌或 Cookie。本次登录集成不修改组件源码，也不增加镜像或身份服务。`/npm/api/` 同时校验运维 Cookie 和组件原生 Bearer；Spug HTTP 与 WebSocket 同时受运维入口保护。后端诊断端口继续只供初始化和恢复，不作为界面入口。

这是一套共享管理账号代登录：组件权限和审计身份沿用各自内置管理账号，不提供逐人 SSO 或项目隔离。Spug 的 IP 绑定校验仍开启，原生会话固定绑定到内部代理地址。不要在 NPM 中删除 OpsKit 管理的会话校验片段。

如果组件开启 MFA、原生接口不兼容或内置密码失效，桥接页会显示错误，并提供“使用组件原生登录”，不会关闭额外验证。在组件内改密码后，请同步更新管理端 `.env`（精简端为 `.workbench/hub.env`），再复用配置重跑安装。凭据失效不会自动覆盖组件现有密码。

入口配置保存在 `.workbench/auth/`，完整与精简安装使用独立配置和 Cookie 名称；Unix 权限为 600，Windows 使用受限 ACL。operator 密码仅保存散列，组件凭据只读挂载给 Hub；配置不进入发布包。外层使用 HTTPS 时请正确设置 `OPS_PUBLIC_SCHEME=https`，安装器会启用浏览器 Secure Cookie。

退出会撤销当前运维会话，并阻止后续受保护请求和新建 WebSocket；已经签发的组件原生令牌、已经建立的长连接不会因此即时撤销。请保持诊断端口仅本机可达，离开共享电脑前退出并关闭组件标签页。日志采集仍使用独立 Basic 账户，不依赖浏览器会话。

在 Spug 界面添加业务机的 SSH 连接，在 DBX 界面配置实际数据库连接及其权限。安装器无法自动推断这些业务凭据。Spug 的安全设置保留原有检查，自动登录不会关闭 MFA 或 IP 绑定。

将整个 `collector/` 模块复制到每台业务机，同时将本目录生成的 `collector.env` 通过受控渠道复制为采集模块的 `.env`，再按采集端说明运行向导。不要将管理中心 `.env` 分发到业务机。采集端可直接连接管理机私网 HTTP，无需经过外层 HTTPS。

## 内置服务自动纳管

完整管理端首次安装和重跑时自动执行以下配置，精简管理端仅配置 Hub 与 NPM 入口。

| 位置 | 安装后可用的内容 |
| --- | --- |
| Spug → 配置中心 | `OpsKit 管理中心` 环境；七个内置应用的 Compose 服务名、内部地址、入口与权限范围；DBX、VictoriaLogs 的 `image` 为升级目标 |
| Spug → 监控中心 | Hub、DBX、VictoriaLogs、Spug、日志采集器的 HTTP 检查，NPM/MariaDB 的端口检查 |
| Spug → 任务计划 | `OpsKit · 管理服务只读巡检`，每 60 秒运行；含 MariaDB `SELECT 1` |
| Spug → 任务计划 | DBX、VictoriaLogs 各四个启动、停止、重启、升级任务，共八个，默认停用，供手动执行 |
| DBX → 连接 | `OpsKit · Spug MariaDB`，使用内部 `spugdb:3306/spug`；初始化验证实际查询和库表读取 |
| VictoriaLogs | 自动接收六个业务组件的容器输出及 NPM、Spug 的文件日志，按 `project=opskit`、`host`、`app`、`env=management` 检索 |

内部 Fluent Bit 通过 Docker 标准日志驱动接收日志，读取 NPM/Spug 日志卷，不挂载 Docker socket，也不修改宿主机 Docker 配置。`OPSKIT_LOG_FORWARD_PORT` 默认 `24224`，只发布到回环地址；端口已占用时可在 `.env` 中改成独立空闲端口。发送采用异步有界缓冲，日志端不可用时不阻断应用；缓冲耗尽或接收器重启窗口可能丢失容器输出，不能把它当作零丢失审计队列。MariaDB 自动生成的 root 密码输出、携带 `x-token` 等凭据的 URL 日志整行会在入库前屏蔽；采集器自身诊断保留在 `docker compose logs internal-logs`，避免循环采集。

Spug 初始化前会生成并核验 `/data/backups/opskit-spug/` 下的范围备份。已有对象和人工编辑不会覆盖；同名冲突记录在 `management/.workbench/self-management.json`，需在界面核对。DBX 仅对完全符合预置配置的连接做只读复验，人工改动的连接保留且标为未复验。DBX 的只读开关是可解锁的防误改，数据库账户仍是 Spug 原账户。

VictoriaLogs 提供 LogsQL HTTP 接口，不能作为 MySQL/SQL 数据库接入 DBX。DBX 仅预置 Spug MariaDB 连接；巡检中的数据库查询也仅对该 MariaDB 执行 `SELECT 1`，其他组件使用各自的 HTTP 或端口检查。

### 手动启停、重启与升级 DBX / VictoriaLogs

Spug 中的任务名称为 `OpsKit · DBX 启动/停止/重启/升级`、`OpsKit · VictoriaLogs 启动/停止/重启/升级`，类型为 `OpsKit 手动服务操作`。这八个任务保持停用，不要激活调度。

1. 升级时先进入“配置中心 → 应用配置”，选择 `OpsKit · DBX` 或 `OpsKit · VictoriaLogs`，在 `OpsKit 管理中心` 环境中修改 `image` 并保存。它只设置升级目标，保存配置不会立即更换容器。目标必须与当前安装使用同一镜像仓库，仅更换版本标签或 `@sha256:…` 摘要，不能切换仓库或镜像源。气隙安装使用离线模式，只接受已导入本机且架构匹配的镜像，不联网拉取。
2. 进入“任务计划”，找到相应服务和动作，点击“更多 → 执行测试”，确认后等待实际结果。启动、停止、重启不需要修改 `image`；升级才读取该值。页面最多等待一小时，超时后先核对历史和管理机结果，不要立即重复升级。
3. 点击“更多 → 历史记录”查看该次执行输出与成功或失败结果。原生“执行测试”会写入历史，但不更新任务的 `latest_*` 字段，因此列表中的“最新状态”和“详情”不能作为这次手动操作的结果依据。

执行请求经 Spug 专用请求卷交给宿主机 `discovery.py --control`，结果由宿主机写回供 Spug 读取。该通道仅允许本安装目录中唯一归属的 DBX、VictoriaLogs 容器，网页容器不挂 Docker socket，不通过管理机 SSH，也不提供任意宿主命令执行。

升级会先停止目标服务写入，完整备份它的所有可写数据卷并校验备份，再试运行目标镜像并检查容器网络及统一入口。备份、原配置与诊断记录保存在管理模块的 `.backups/service-control/<请求ID>/`。升级失败时会尝试恢复原镜像和备份数据；这是恢复到备份时点，试运行期间新增的日志或数据变更可能丢失。

若自动回退未完成，系统保留 `.workbench/control/recovery/<服务名>.json` 恢复锁（latch），拒绝该服务后续操作，不会把失败当作恢复成功继续执行。保留备份，在管理机重跑原安装命令触发恢复；恢复仍失败时继续锁定，不要删除锁文件或反复点击操作。macOS、Windows 等未配置自启的环境，宿主机重启后也需按已有提示重跑安装器，才能恢复该控制通道。

## 单独 Hub 的日常启停

在 `management/` 下执行：

```bash
# 查看或停止 Hub 与 NPM，不删除数据
docker compose --env-file .workbench/hub.env -f hub.compose.yaml ps
docker compose --env-file .workbench/hub.env -f hub.compose.yaml stop
# 恢复运行
./install.sh --hub-only --offline
```

单独 Hub 和完整管理中心默认都使用 80、8181 端口。同机切换安装范围时，先停止原范围的服务，保留卷和收藏，避免监听冲突；两套 NPM 使用独立持久卷。不要在 80 端口被其他程序占用时直接终止该程序，应先核对其用途。

## 卸载本机管理中心

在本目录执行：

```bash
./uninstall.sh --dry-run       # 预览，不修改
./uninstall.sh                 # 删除容器、网络和发现进程，保留数据
./uninstall.sh --purge-data    # 先备份核验，再清空配置、收藏及数据卷
# 只卸载独立 Hub 与 NPM
./uninstall.sh --hub-only --purge-data
```

Windows 对应为 `.\uninstall.cmd -DryRun`、`.\uninstall.cmd`、`.\uninstall.cmd -PurgeData`，独立 Hub 可追加 `-HubOnly`。默认执行前要求输入 `yes`；Linux/macOS 的 `--yes` 或 Windows 的 `-Yes` 可跳过确认。

脚本以当前目录和 Compose 标签确认资源归属，不操作其他目录的应用。清空前停止对应服务，保存本地配置、收藏和卷 TAR，检查备份内容及 SHA-256 后再删除原数据。可恢复备份保存在工具包根目录 `.uninstall-backups/时间戳/`，独立复制的模块则保存在本模块目录；含原密码，需妥善保管。备份失败时原容器和数据保留，但服务可能已停止。原 `backups/` 数据库逻辑备份和离线镜像包保留。

Docker、Python、镜像和源码均保留，重装会复用基础依赖。卸载不会联网补装或启动 Docker，需要已有 Python 3.9+ 与可用的本机 Linux Docker 引擎。没有容器可证明归属的旧孤立卷不按名称删除；若先前选择保留数据卸载，应先恢复本套件容器挂载，再执行清空卸载。同名项目跨目录或资源被其他容器共享时会拒绝操作。

清空成功后，运行本模块安装入口即可从首次配置开始测试。

## 检查、备份与离线交付

```bash
# 在本目录执行
./scripts/check.sh

# 查看服务及日志
docker compose ps
docker compose logs --tail=100

# Spug 数据库逻辑备份
./scripts/backup-spug-sql.sh

# 在同架构、可联网的机器导出本模块镜像
./scripts/export-images.sh
```

以上 `.sh` 日常脚本用于 Linux/macOS。Windows 安装器会自动执行检查；日常可运行 `py -3 scripts/check.py`（若未提供 py 启动器，使用已安装 Python 的实际路径）。Windows 镜像导出/导入使用 `docker save` / `docker load`。

数据库备份脚本只备份 Spug MariaDB，不能恢复整个工具包。还应备份 `.env`、工作台入口及其备份、NPM 数据、Spug 文件、DBX 数据与备份、VictoriaLogs 数据等持久卷，并验证恢复过程。不要用 `docker compose down -v` 作为日常停止命令，否则会删除持久卷。

镜像导出只涵盖管理中心；采集端有独立的导出脚本。将镜像包及校验文件传入目标机后，先校验，再导入：

```bash
cd offline-images
sha256sum -c SHA256SUMS.txt
docker load -i opskit-management-images.tar
cd ..
./install.sh --offline
```

离线交付需携带本模块配置模板及镜像，排除实际 `.env`、`collector.env`、`.backups/` 和业务数据。镜像导出机与目标机须使用相同 CPU 架构。

## 依赖引导验证

已在官方龙蜥 8.9 ARM64 基础容器中实际自动安装 Python 3.11.13 并生成 Hub 配置，原 platform-python 3.6.8 保持原版本；尚未据此宣称龙蜥实机 Docker 服务及重启自启验收通过。Windows 引导在官方 PowerShell 7.4 Linux 容器中通过语法解析与模拟流程检查，原生 Windows / PowerShell 5.1 / NTFS ACL / UAC / WSL / Docker Desktop 仍待实机验证。

## 验收范围

安装检查用于发现容器、入口鉴权和服务连接问题，不能替代目标环境验收。交付前还需实际确认外层 HTTPS、SSH 纳管、数据库查询、每台业务机日志写入和跨主机检索。只有执行过的检查才能记为通过；本说明不代表已完成龙蜥或生产环境验收。

统一入口采用站内登录与组件管理账号代登录；组件保留自己的权限和审计身份，不提供逐人身份联合或项目级隔离。AI 故障分析仍是扩展约定，见 `ai/README.md`。
