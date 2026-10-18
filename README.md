# OpsKit 企业软件运维工具包 v0.9.0

**面向驻场工程师与运维工程师的现场工具箱。**

将应用入口、主机运维、数据库查询、集中日志和交付文档收敛到一处。适合约 3～5 台业务虚拟机的客户现场，也可以只安装 Hub，管理本机的 Docker 应用与 HTTP 服务。

[快速开始](#快速开始) · [功能组件](#功能组件) · [气隙交付](#气隙交付) · [文档导航](#文档导航) · [开发与验证](#开发与验证)

## 功能组件

| 能力 | 组件 | 用途 |
| --- | --- | --- |
| 统一入口 | **Hub** | 发现本机 Docker 容器与 HTTP/HTTPS 服务；内置运维入口置顶，支持收藏、搜索和编辑 |
| 代理配置 | **Nginx Proxy Manager** | 通过 WebUI 管理反向代理；默认由已有外层 Nginx 提供 HTTPS |
| 主机运维 | **Spug** | SSH 纳管、批量操作与应用发布 |
| 数据查询 | **DBX** | 在浏览器中连接、查询和管理数据库 |
| 集中日志 | **VictoriaLogs + Fluent Bit** | 业务机独立采集，管理中心统一检索 |
| 文档预览 | **Hub 文档中心** | 离线搜索、预览和下载现场手册、项目资料、Word 与 PDF |
| 离线交付 | **opskit.skill** | 按目标系统和架构制作运维工具或业务系统完整气隙包 |

MariaDB 为 Spug 保存管理数据，不用于托管业务数据库。Hub 在本地和服务器上使用同一套能力；主机操作和数据库操作由对应工具完成。

## 快速开始

### 1. 获取项目

```bash
git clone https://github.com/jakara/opskit.git
cd opskit
```

已拿到完整气隙包时，解压后直接运行**包内根目录**的安装入口，按主机角色选择，无需克隆仓库或手工导入镜像。

### 2. 运行安装向导

**Linux / macOS**

```bash
./install.sh
```

**Windows 10 / 11**

```powershell
.\install.cmd
```

向导提供三种安装范围，也可在命令后直接指定：

| 安装范围 | 参数 | 安装内容 | 部署位置 |
| --- | --- | --- | --- |
| **完整管理中心** | `management` | Hub、NPM、Spug、MariaDB、DBX、VictoriaLogs、内部日志采集 | 一台管理机 |
| **日志采集端** | `collector` | Fluent Bit | 每台业务机 |
| **精简管理端** | `hub` | Hub + NPM | 本机或只需入口管理的服务器 |

例如：`./install.sh management`，Windows 对应 `.\install.cmd management`。管理端和采集端可分别复制、独立安装。

首次联网安装会自动准备缺失的 Python 3.9+、Docker 和 Compose V2，复用已有可用依赖。向导会引导选择安装目录、网络、账户及日志配置；中断后重跑可沿用上次已确认的选项。系统授权、Docker Desktop 首次许可和必要的重启仍需在系统界面完成。

已有配置时先选择复用或重新配置；服务启动并检查成功后，终端统一回显一次管理端登录凭据和保存位置。已有运行中的本套件容器会在镜像就绪后自动重建重启，保留持久化数据。

### 3. 打开工作台并接入业务机

| 入口 | 默认地址或路径 |
| --- | --- |
| **Hub 首页** | `http://127.0.0.1/` |
| NPM 管理界面 | `/npm/`，从 Hub 进入 |
| Spug | `/spug/`，从 Hub 进入 |
| DBX | `/dbx/`，从 Hub 进入 |
| 日志查询 | `/logs/select/vmui/`，从 Hub 进入 |

管理中心提供 HTTP，证书由外层 Nginx 管理。**外层代理或采集端在其他机器上时，HTTP 监听地址应填写管理机可达的内网 IPv4**；仅供同机访问的端口可保留 `127.0.0.1`。

完整管理端安装时自动完成内置服务登记：Spug 展示各组件配置、健康监控及每分钟只读巡检，并预置 DBX、VictoriaLogs 各四个启动、停止、重启和升级任务，共八个，默认停用。手动操作使用任务的“更多 → 执行测试”，结果查看“更多 → 历史记录”；不要激活这些任务的调度。DBX 预置 Spug MariaDB 连接并验证查询；日志中心汇集内置组件的容器输出、NPM 访问日志和 Spug 文件日志。重跑保留人工编辑，初始化结果保存在 `management/.workbench/self-management.json`。

升级前在 Spug 配置中心对应应用、`OpsKit 管理中心` 环境的 `image` 中设置目标，仅允许当前仓库的其他版本或 digest；气隙安装只使用已导入本机的镜像。宿主机发现进程通过 `--control` 消费仅 Spug 可写的请求卷，只允许操作本安装的 DBX、VictoriaLogs，网页容器不挂 Docker socket，也无需管理机 SSH。升级先停止目标服务写入并全量备份数据卷；失败回退到备份时点，试运行期间新增日志或变更可能丢失。回退未完成时锁定该服务操作，需在管理机重跑安装器恢复，不能直接重复操作。备份位于 `management/.backups/service-control/`，完整步骤见[内置服务自动纳管](management/README.md#内置服务自动纳管)。

VictoriaLogs 使用 LogsQL，不是关系数据库，因此在日志界面查询，不向 DBX 添加无效的 SQL 连接。DBX 仅预置 Spug MariaDB 连接，默认启用可由管理员解锁的只读防误改；数据库查询健康检查也仅针对 Spug MariaDB。

安装后还需完成三项业务接入：

1. 在 Spug 中添加实际 SSH 主机，在 DBX 中添加数据库连接。
2. 将管理中心生成的 `management/collector.env` 复制为业务机的 `collector/.env`，再运行采集向导。只分发采集凭据，不分发管理中心 `.env`。
3. 在每台业务机追加测试日志，并到管理中心按主机、项目、应用和环境标签检索。

详细字段与外层反代配置见 [管理中心说明](management/README.md) 和 [采集端说明](collector/README.md)。

## 气隙交付

按现场网络条件选择交付方式：

| 交付方式 | 制包时携带 | 现场安装 |
| --- | --- | --- |
| **OpsKit 联网安装版** | 管理端、采集端、安装入口和中文文档 | 安装器在线获取依赖与镜像 |
| **OpsKit 独立气隙包** | 上述内容、七个运维镜像、目标 OS 离线依赖 | 从包内准备 Python/Docker，导入镜像并离线安装 |
| **业务系统完整气隙包** | OpsKit 气隙资源、业务应用、项目资料及专用手册 | 按主机角色安装，业务与运维工具均无需外网下载 |

**完整气隙目标：龙蜥 8.9 / 8.10 × amd64（x86_64）/ arm64（aarch64）。** Skill 会确认客户的系统、版本和 CPU 架构。Windows/macOS 使用现有联网安装入口；龙蜥 RPM 包不能用于它们的离线依赖安装。

联网安装默认采用已核验的 DaoCloud 地址与 DBX 官方 CNB，并提供登记的同版本备用来源。Spug 4.1 仍使用境外官方源；客户无法访问时，提前制作气隙包。来源、重试和续装行为见 [镜像说明](management/README.md#配置文件与重跑)。

### 安装与使用 Skill

把下面一行发送给 Claude Code 或 Codex：

```text
安装这个 Skill：https://raw.githubusercontent.com/jakara/opskit/main/skill.md
```

安装后，按目标使用：

```text
使用 $opskit，只制作运维工具气隙包，目标龙蜥 8.9 x86_64，不包含业务应用。
```

```text
使用 $opskit，为当前仓库生成完整气隙包，目标龙蜥 8.9 x86_64，业务版本 1.2.0。
```

Claude Code 可使用 `/opskit` 调用。只需联网安装版时，明确要求“打包 OpsKit 联网安装版，镜像在 install 运行时拉取”。

Skill 在制包阶段完成业务适配、镜像固定、依赖收集和手册编制。**缺少目标环境的完整断网验收时，只生成待验收目录，不封成正式交付包。** 现场工程师拿到正式包后，解压并运行 `./install.sh`，按中文向导填写客户参数。

[Skill 使用与分发](skills/README.md) · [运维工具独立气隙包](skills/opskit/references/opskit-only.md) · [交付契约](skills/opskit/references/bundle-contract.md) · [验收要求](skills/opskit/references/acceptance.md)

## 安装配置与日常使用

<details>
<summary><strong>安装位置、覆盖已有文件与中断续装</strong></summary>

安装包已在推荐目录时原地继续；其他位置可选推荐目录、自定义目录或当前位置。复制后校验内容和脚本权限，再从目标位置进入向导，原包保留。

| 系统 | 推荐位置 |
| --- | --- |
| Linux / 龙蜥 | `/opt/opskit`、`/srv/opskit`、`~/opskit` |
| macOS | `~/Applications/opskit`、`~/opskit`、`/usr/local/share/opskit` |
| Windows | `%LOCALAPPDATA%\opskit`、`%USERPROFILE%\opskit`、`%ProgramData%\opskit` |

- 模块与位置保存在 `.install-options.json`；每项有效配置保存在模块的 `.env.install-progress`，重跑时作为默认值。
- 全部校验通过后才写入正式 `.env` 并清除草稿；重新配置中断时保留原正式配置。
- 覆盖已有程序前，在 `.backups/install-时间/` 备份并校验；保留已有配置、数据、工作台状态及项目资料。失败时恢复本次修改。
- 草稿和备份可能含密码，使用受限权限，不进入 Git、通用 Skill 或公共安装包。

| 操作 | Linux / macOS 参数 | Windows 参数 |
| --- | --- | --- |
| 指定安装目录 | `--install-dir /实际目录` | `-InstallDir "C:\实际目录"` |
| 保留当前位置 | `--keep-location` | `-KeepLocation` |
| 非交互覆盖（仍先备份） | `--overwrite` | `-Overwrite` |
| 只生成配置，不操作 Docker | `--configure-only` | `-ConfigureOnly` |
| 修改已有配置 | `--reconfigure` | `-Reconfigure` |
| 明确复用配置，跳过选择菜单 | `--reuse-config` | `-ReuseConfig` |
| 禁止联网补依赖或拉镜像 | `--offline` | `-Offline` |
| 精简管理端更改 HTTP 端口 | `--port 19090` | `-Port 19090` |

普通安装包的 `--offline` 要求基础依赖和镜像已在本机准备好；完整气隙包的根入口会自动使用包内资源。仅生成配置时，仍会在缺失时准备 Python。

</details>

<details>
<summary><strong>系统依赖与 Windows 注意事项</strong></summary>

| 系统 | 依赖准备方式 |
| --- | --- |
| 龙蜥 / RHEL 系 | 系统包管理器并行安装新版 Python，保留系统解释器；必要时添加专用 Docker 官方仓库，变更前备份 |
| Ubuntu / Debian | 系统包管理器，必要时添加 Docker 官方仓库 |
| macOS | 复用现有依赖；缺失时使用 Homebrew、Docker Desktop |
| Windows 10 / 11 | WinGet、Docker Desktop 的 Linux 容器，必要时准备 WSL 2 |

- 已可用的 Docker 不更换或升级；不删除冲突的运行时包，不修改 `daemon.json`、Docker 用户组、防火墙、DNS 或外层 Nginx。
- Windows 配置目录使用 NTFS，凭据与备份限制为当前用户、SYSTEM 和管理员访问；不改变系统执行策略。可信 ZIP 若被系统锁定，先解除锁定再解压。
- Windows Server 不在当前安装支持范围。原生 Windows 的完整安装仍需实机验收。

</details>

### 检查、备份与卸载

在工具包根目录执行：

```bash
./management/scripts/check.sh       # 检查管理中心
./collector/scripts/check.sh        # 检查采集端
./management/scripts/backup-spug-sql.sh  # 仅备份 Spug 数据库

./uninstall.sh --dry-run            # 预览当前安装对应的资源
./uninstall.sh                      # 卸载容器等资源，保留配置和数据
```

Windows 使用 `.\uninstall.cmd -DryRun` 预览、`.\uninstall.cmd` 卸载；日常检查见各模块说明。

<details>
<summary><strong>清空重装、备份范围与旧版迁移</strong></summary>

- **清空重装：** 使用 `./uninstall.sh --purge-data`，Windows 对应 `.\uninstall.cmd -PurgeData`。先备份并核验配置、收藏和卷内容，再删除原数据；备份位于 `.uninstall-backups/时间戳/`。失败时保留原数据，但服务可能已停止。
- **卸载范围：** 可选 `all`（默认）、`hub`、`management`、`collector`。按安装目录和 Compose 标签确认资源归属，保留 Docker、Python、镜像及其他应用；共享资源或无法确认归属时拒绝清理。
- **完整备份：** Spug SQL 脚本只备份 MariaDB；还需备份配置、工作台、NPM、Spug 文件、DBX 和日志数据等持久卷，并验证恢复。日常停止不要使用会删卷的 `docker compose down -v`。
- **v0.1 迁移：** 先备份配置与持久卷，停止旧 Compose 的 nginx。旧根目录 `.env` 只读导入并补齐；自定义 Compose 项目名继续通过 `COMPOSE_PROJECT_NAME` 保持一致。原 `agents/` 改为 `collector/`，补齐采集配置后验证日志链路。
- **入口迁移：** Hub 首页使用 80 端口 `/`，NPM、Spug 使用 `/npm/`、`/spug/`；8181/8081 等原端口保留作后端初始化与诊断，旧 8080 日志接入口保留兼容。安装器备份后迁移原始根代理及收藏，保留自定义规则；外层 Nginx 上游需相应核对。
- **旧收藏：** 原 `.workbench/development/` 收藏复制并校验到 `.workbench/hub/`，原目录保留；容器版继续使用既有持久卷。旧 `development`、`production` 命令保留为安装范围别名。

</details>

## 文档导航

| 我想…… | 阅读 |
| --- | --- |
| 配置管理端、代理、账户与服务入口 | [管理中心](management/README.md) |
| 配置日志目录、标签、缓冲与检索 | [日志采集端](collector/README.md) |
| 把项目资料放进 Hub | [文档中心](management/documents/README.md) |
| 制作联网安装包或完整气隙包 | [Skill 模块](skills/README.md) |
| 接入未来的 AI 诊断能力 | [AI 扩展约定](management/ai/README.md) |

文档中心支持 Markdown、文本、PDF、Word `.docx` 和常见图片，预览资源随包携带。Word 提供正文、表格和内嵌图片的内容预览，复杂排版以原件为准；旧 `.doc` 需先转换。资料保存在 `management/documents/`，可直接用终端查看。

<details>
<summary><strong>项目目录</strong></summary>

```text
opskit/
├── install.sh / install.cmd / install.ps1     # 统一安装入口
├── uninstall.sh / uninstall.cmd / uninstall.ps1
├── management/                              # 管理中心，可独立部署
│   ├── portal/                              # Hub 与宿主机发现
│   ├── documents/                           # 随包文档库
│   └── scripts/                             # 安装、检查、备份与镜像导出
├── collector/                               # Fluent Bit 独立采集端
├── skills/                                  # 交付 Skill、模板与制包工具
└── tests/                                   # 开发测试，不进入分发包
```

</details>

## 开发与验证

在仓库根目录运行自动化检查：

```bash
python3 -m unittest discover -s tests -v
```

| 验证范围 | 当前证据 |
| --- | --- |
| 安装与配置 | 自动化覆盖输入校验、续装、备份、镜像失败处理、位置选择与卸载 |
| 管理服务与日志 | 在隔离 ARM64 Docker 项目中验证过 NPM、Spug、DBX、入口鉴权及 Fluent Bit 实际日志写入 |
| 离线资源 | 龙蜥 8.9 ARM64 容器中完成 194 个签名 RPM 的禁网安装；七个运维镜像完成归档校验与离线导入 |
| 文档预览 | 真实浏览器验证过 Markdown 链接与图片、Word 正文/表格/内嵌图片及 PDF 中文分页 |

这些记录不代表所有平台或客户环境已验收。完整气隙包仍需目标环境的管理端与采集端断网运行验证；外层 HTTPS、真实 SSH 纳管、数据库查询、跨主机日志，以及原生 Windows 安装均需按项目验收。

运维入口采用中文站内登录，operator 一次登录即可进入内置工具，由后端映射各组件管理账号。组件权限和审计身份沿用这些账号，尚未提供逐人身份联合或项目级权限隔离。详见[登录说明](management/README.md#一次登录进入内置组件)。AI 诊断尚处于扩展约定阶段。各上游组件保留其原有许可。

欢迎通过 [Issues](https://github.com/jakara/opskit/issues) 反馈问题，通过 Pull Request 改进项目。报告安装问题时，请提供系统版本、CPU 架构、执行命令和脱敏错误输出；不提交 `.env`、密码、客户数据或包含凭据的备份。
