# OpsKit 制包 Skill 模块

`opskit/` 是供 AI 编程助手使用的 Skill，分发文件名为 `opskit.skill`，调用名为 `$opskit`。支持直出 OpsKit 独立安装包，或在业务代码仓库完成容器化适配、离线依赖收集、镜像固定、文档收集、隔离验收和气隙封包。

## 分发与使用

可直接把一行安装提示词发送给 Claude Code 或 Codex：

```text
安装这个 Skill：https://raw.githubusercontent.com/jakara/opskit/main/skill.md
```

智能体会按仓库根目录的 [skill.md](../skill.md) 下载完整源码、生成包含 OpsKit 快照的 Skill，并安装、核验。完成后会主动介绍当前版本的功能、直出与完整气隙交付的区别，以及可复制的使用示例。不要直接用通用目录安装器复制源码中的 `skills/opskit/`，它还缺少生成的 `assets/opskit/`。

也可将已打包 `.skill` 文件按 ZIP 解压，把其中的 `opskit/` 放到所用助手的技能目录。例如 Codex 为 `~/.agents/skills/opskit/`，Claude Code 为 `~/.claude/skills/opskit/`；Windows 对应用户目录下的同名位置，已配置的技能目录优先。不要只复制 SKILL.md，scripts、references、assets 都必须保留。

在业务仓库打开助手后调用：

```text
使用 $opskit，为当前仓库生成现场离线交付包。
目标龙蜥 8.9 x86_64，业务版本 1.2.0。
按现有部署方式划分主机角色，完成断网验收后生成正式包。
```

Claude Code 使用 `/opskit` 调用同一个 Skill。

## 先选择直出或完整气隙包

用户尚未指定模式时，Skill 会先问“是否直出 OpsKit？”，等待选择；本次任务已经明确模式时不重复询问。

- **直出**：只打包 Skill 自带的 OpsKit 固定快照，包含管理端、采集端、安装入口和内置说明。无需业务仓库、Docker、业务配置或气隙验收，不携带业务应用、镜像及离线系统依赖。
- **完整气隙包**：读取业务仓库，完成适配、依赖与镜像收集、隔离验收，再生成正式现场包。

已安装的完整 Skill 可直接运行：

```text
python3 <技能目录>/scripts/export_opskit.py --output <交付目录>
```

直出生成 `opskit-direct.tar.gz` 和同名 `.sha256`；包内含 `OPSKIT_SOURCE.json` 与 `SHA256SUMS`。只复制来源清单内且校验通过的文件，不覆盖已有归档或校验文件。源码中的 Skill 尚未带快照，需按下方分发流程打包后再使用该入口。直出包安装时仍需准备依赖和镜像，不代表完整离线安装或运行验收通过。

以下目标环境、业务配置、文档收集与现场验收说明适用于完整气隙模式。该模式会优先读取当前仓库的部署资源；没有可靠业务镜像、参数定义、备份/恢复方法或业务健康检查时，会在制包阶段补齐或明确指出缺口，不把工作留给初级驻场工程师。

当前支持的现场目标为龙蜥 8.9 / 8.10，amd64 / arm64；制包机需 Python 3.9+、Docker、Compose 和 Buildx，可使用 Linux、macOS、Windows。Windows 是制包端兼容范围，不代表龙蜥 RPM 包能用于 Windows 冷启动安装。

## Skill 包与现场包不同

- Skill 包：代码、模板、制包工具和 OpsKit 固定源码快照，交给公司工程师及 AI 助手。
- OpsKit 直出包：管理端、采集端、安装入口和内置说明，不包含业务应用、镜像及离线系统依赖。
- 正式气隙包：目标架构镜像、宿主机依赖、业务配置表单、OpsKit、现场脚本、Spug 操作手册和验收记录，交给驻场工程师。
- 暂存目录：待验收产物。缺少真实验收报告不能通过 seal，不可当成正式现场包分发。

现场解压正式包后运行 `./install.sh`，按中文菜单选择管理中心、业务角色或采集端。客户密码在现场输入并保存为受限权限文件，发行包不带真实客户配置。

## 文档随包交付

在业务仓库的 `opskit-delivery.json` 中加入 `documents`：

```json
{
  "documents": ["README.md", "docs/部署说明.md", "docs/现场交接.docx", "docs/附件"]
}
```

文件或目录均相对业务仓库；目录递归收集并保留相对路径。Markdown 引用的图片、其他文档也需选入，选择整个资料目录通常更方便。只选择允许对现场工程师公开的资料，真实环境配置、密钥、日志和数据库不会作为文档进入包。

自动生成的现场/Spug 手册、OpsKit 内置说明和项目资料共同进入 `opskit/management/documents/`，按应用 ID 区分。管理端启动后，在 Hub 首页点击“交付文档”即可搜索和预览；支持 Markdown、文本、PDF、Word `.docx` 和常见图片，并可下载原件。预览资源随包携带，不使用 Office 云服务或外部 CDN。旧 `.doc` 制包前需转换为 `.docx` 或 PDF。

文档均为普通文件，终端可直接查看。新版本只更新资料时，在管理机按生成手册执行 `--role management --action sync-docs`；也可用 `--documents-dir` 指向现有 Hub 的文档库。同步前会核验备份，只更新有来源记录的本应用文档；其他应用、手工资料和本地编辑冲突会受到保护，不会借文档更新升级 OpsKit。

主流程和配置契约见 [SKILL.md](opskit/SKILL.md)、[交付契约](opskit/references/bundle-contract.md)、[验收要求](opskit/references/acceptance.md)。内置 OpsKit 本体仅提供安装和检查；业务应用可按角色适配升级、备份和回退，不能把容器恢复替代数据库恢复。

小型独立演练仓库位于 [assets/example](opskit/assets/example/README.md)。实际使用时应以当前客户业务仓库为输入，应用特有的顺序、准备步骤和成功判据由助手写入制包配置的 `site_notes`，自动进入现场手册。

## 维护 Skill 分发包

本仓库不保存重复的 OpsKit 运行源码。打包时把当前源码经白名单过滤后生成到暂存目录的 `assets/opskit/`，附带文件校验和及来源记录；既能在其他代码仓库独立运行，又避免维护两份漂移的源码。

```bash
python3 skills/package_skill.py \
  --stage dist/skill-stage/opskit \
  --archive dist/opskit.skill
```

暂存目录和压缩包必须不存在，避免覆盖已有交付。通过 Skill Creator 编制时，还应使用其 `quick_validate.py` 和标准 `package_skill.py` 验证、打包暂存目录。依赖安装包和业务镜像不放进 Skill 本体，在生成具体现场包时按目标收集。
