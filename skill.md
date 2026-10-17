# 安装 OpsKit Skill

本页供 Claude Code、Codex 等具有联网和文件操作能力的智能体执行。用户要求“安装这个 Skill”时，完成下面的安装与核验即可；不要启动 OpsKit 服务或为当前业务仓库制包。

## 安装来源与位置

- 仓库：`https://github.com/jakara/opskit.git`，默认分支 `main`。
- 本页原文：`https://raw.githubusercontent.com/jakara/opskit/main/skill.md`。
- Skill 名称：`opskit`。本页是安装说明，真正的技能入口是 `skills/opskit/SKILL.md`。

默认只安装到当前智能体的个人技能目录；用户明确指定项目目录时采用其指定位置：

| 智能体 | 默认目标目录 | 安装后调用 |
| --- | --- | --- |
| Codex | `~/.agents/skills/opskit/` | `$opskit` |
| Claude Code | `~/.claude/skills/opskit/` | `/opskit` |

`~` 表示当前用户主目录，Windows 使用对应用户目录。已有框架明确配置了其他技能根目录时沿用其配置；只操作 `opskit`，不改框架全局配置或其他技能，不需要同时安装到两处。

## 执行步骤

1. 在临时目录浅克隆完整仓库并记录实际提交 SHA：

   ```text
   git clone --depth 1 --branch main https://github.com/jakara/opskit.git <临时目录>/source
   git -C <临时目录>/source rev-parse HEAD
   ```

   使用本机已可用的 Git 和 Python 3.9+。Linux/macOS 通常为 `python3`，Windows 可用 `py -3`，先核对版本。只有缺失时才通过当前平台受支持的安装方式补齐；安装 Skill 不需要 Docker、Compose、Node.js 或管理员权限。网络、权限或依赖受限时明确说明缺项，不把未完成的安装报告为成功。

2. 从源码生成包含 OpsKit 固定快照的完整 Skill，输出必须是尚不存在的新目录，末级名称必须为 `opskit`：

   ```text
   <Python> -B <临时目录>/source/skills/package_skill.py --stage <临时目录>/prepared/opskit
   ```

   不能直接安装仓库中的 `skills/opskit/`，它尚未包含生成的 `assets/opskit/`。不需要查找 GitHub Release 或下载之前开发者本机的 `dist/opskit.skill`。

3. 检查目标位置。已有文件、目录或软链接时，先建立可恢复的备份并核验内容，再替换此技能；保留原软链接及其指向，不覆盖其外部源目录。将 `prepared/opskit/` 整目录复制到选定的目标位置，保留全部 `scripts/`、`references/`、`assets/` 和 `agents/`。使用独立副本，避免清理临时克隆后失效。

4. 核验目标副本与暂存目录的所有文件清单、SHA-256 和可执行文件权限一致。检查 `assets/opskit/OPSKIT_SOURCE.json` 的 `sha256` 与 `documentation_sha256` 中每个文件的校验和，并确认以下入口齐全：

   ```text
   SKILL.md
   scripts/build_bundle.py
   scripts/collect_prerequisites.py
   assets/site/site.py
   assets/site/install.sh
   assets/site/offline-bootstrap.sh
   assets/opskit/management/compose.yaml
   assets/opskit/collector/compose.yaml
   assets/opskit/management/portal/documents.js
   ```

   在已安装副本运行无副作用的检查，不要求连接 Docker：

   ```text
   <Python> -B <技能目标目录>/scripts/build_bundle.py --help
   <Python> -B <技能目标目录>/scripts/build_bundle.py inspect --repo <技能目标目录>/assets/example
   ```

5. 报告实际安装目录、来源提交 SHA、核验结果和当前框架的调用方式。确认框架能发现技能；若当前会话尚未刷新，提示重新打开会话后检查。文件检查通过与框架实际发现分开报告，不宣称已经验证业务系统的气隙部署。清理本次临时克隆，保留升级前备份。

安装后可在业务仓库使用：“使用 OpsKit，为当前仓库生成现场离线交付包，目标龙蜥 8.9 x86_64。”目标系统、架构及业务版本以具体项目为准。

技能目录与发现方式依据 [OpenAI 官方文档](https://learn.chatgpt.com/docs/build-skills)及 [Claude Code 官方文档](https://code.claude.com/docs/en/skills)。
