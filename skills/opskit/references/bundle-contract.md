# 气隙交付契约 v1

制包配置为业务仓库内 `opskit-delivery.json`，UTF-8 JSON。所有源路径相对业务仓库，不允许越界或软链接。目标第一版为 `os=anolis`、`version=8.9|8.10`、`arch=amd64|arm64`。制包端可运行 Python 3.9+ 和 Docker/Buildx/Compose 的 Linux、macOS、Windows。

```json
{
  "schema_version": 1,
  "application": {"id": "college-app", "name": "教学业务系统", "version": "1.0.0"},
  "target": {"os": "anolis", "version": "8.9", "arch": "amd64"},
  "documents": ["README.md", "docs/部署说明.md", "docs/现场交接.docx", "docs/附件"],
  "roles": [{
    "id": "web", "name": "应用服务器", "project_name": "college-app-web",
    "compose": "deploy/compose.yaml",
    "images": {"web": "nginx:1.27-alpine"},
    "files": [{"source": "deploy/nginx.conf", "target": "nginx.conf"}],
    "parameters": [{"key": "HTTP_PORT", "label": "业务 HTTP 端口", "default": "8088", "required": true, "secret": false, "type": "port"}],
    "checks": [{"url": "http://127.0.0.1:${HTTP_PORT}/", "status": 200}],
    "stateful": false, "rollback": "app-only", "hooks": {},
    "site_notes": ["在管理中心安装完成后安装本角色。", "业务入口返回本版本页面，登录后按应用验收步骤核对。"]
  }],
  "opskit_images": {}
}
```

`images` 是服务名到已构建镜像的映射；未填的服务可使用 Compose 中不含变量的 image。存在 build 时必须先按目标平台构建并指定对应镜像，最终 Compose 删除 build，不复制源仓库和真实 .env。制包不得向客户仓库、线上服务器或镜像仓库推送，除非本轮用户已授权。

`files` 只允许显式必要的非敏感配置、静态资源；target 相对该角色目录。不能复制实际 .env、密钥、业务数据库、日志、node_modules、.git。业务参数由 parameters 建模，现场生成受限权限 site.env，不把真实密码打入包。Compose 必须保留参数引用，不能从制包机的环境或 .env 注入凭据。

Compose 按角色 project_name 解析，隐式网络和卷使用该项目命名空间。v1 不支持复用 external 网络/卷或其他项目的显式资源名称；遇到这些配置，应先在制包阶段适配，不能在客户现场默认连到旧资源。首次安装若发现同名项目或同名网络/卷，会停止，交由管理员确认已有归属。

`site_notes` 是可选的中文字符串数组，用于在制包前编制应用专有的安装顺序、外部准备和成功判据。它会进入现场手册，不得包含真实凭据。检查手册后需要修订时修改此配置并重新制包，不直接改动已校验的暂存目录。

顶层 `documents` 是可选字符串数组，指定仓库内交付文件或目录，目录递归收集、保留原相对路径。支持 Markdown、文本、PDF、Word `.docx`、PNG/JPEG/GIF/WebP/SVG 图片；不支持的格式和旧 `.doc` 在制包前明确报错。每文件最多 20 MiB，Word 需为正常 OOXML ZIP 且满足展开体积/文件数限制。路径、链接、凭据及文件完整性检查与部署资源同样严格。

文档库位于 `opskit/management/documents/{delivery,project,builtin}/<应用ID>/`；project 下继续保留所选原仓库路径。自动手册与内置说明即使没有 documents 也进入文档库。`manifest.documents` 记录每个文档的 path/group/source/sha256/bytes，并一并纳入全包 SHA256SUMS。Hub 只读挂载文档库，网页预览和终端读取同一批普通文件。

`parameters.type` 支持 text、port、host、integer，secret 参数不显示默认值，默认不允许制包时填入真实密码。required=true 且无默认值时现场必填；非交互执行若缺参数必须在部署前失败。

`hooks` 的 install_before/install_after/upgrade_before/upgrade_after/check/backup/restore 值为业务仓库内脚本相对路径，制包器复制到角色 hooks/，输出 manifest 改为包内相对路径。脚本在现场通过 bash 执行；必须由 Skill 结合真实应用生成和验证，不得使用 exit 0 充当备份或验收。运行时注入 OPSKIT_APP_DIR、OPSKIT_ENV_FILE、OPSKIT_BACKUP_DIR、OPSKIT_ACTION 和 COMPOSE_PROJECT_NAME。不打印 site.env 内容。

stateful=true 时必须提供 backup hook；rollback=restore-hook 必须提供 restore hook。app-only 只用于制包阶段明确确认数据库及数据格式向后兼容的版本。unsupported 明确阻止回退，不得承诺一键回滚。命名卷/可写业务挂载不得伪装为无状态。每角色必须有业务 HTTP checks 或 check hook。

输出目录：

```text
<应用id>-<版本>-anolis<版本>-<架构>/
  install.sh                 # 离线引导、Python 选择，进入 delivery/site.py
  delivery/site.py           # 现场交互菜单和角色部署/备份/回退
  delivery/manifest.json     # 下述 manifest
  application/<role>/compose.json
  application/<role>/hooks/...
  application/<role>/...     # 显式 files
  images/*.tar
  prerequisites/manifest.json
  prerequisites/bootstrap.sh
  prerequisites/rpms/*.rpm
  prerequisites/rpms/repodata/*
  prerequisites/keys/*
  opskit/                    # 经白名单过滤的 OpsKit 运行源码，无真实配置和数据
  docs/现场操作.md
  docs/Spug操作.md
  SHA256SUMS                 # 除自身外全部普通文件的 sha256，包内相对路径
```

输出 tar.gz 根目录仅上述一个目录，包内不允许链接及绝对路径。运行包不得包含 Skill、Git 目录、dist 或制包机配置备份。

manifest 必须包含 schema_version=1、application、target、roles、images、opskit_path="opskit"、prerequisites="prerequisites/manifest.json"、verification。role.compose、role.hooks 全为包内相对路径，role.directory="application/<id>"；保留参数、检查、状态和回退策略。镜像记录为：

`kind` 为 `application`（旧配置缺省值）或 `opskit`。业务包必须至少含一个业务角色；运维工具独立气隙包必须为 `kind="opskit"`、`roles=[]`，不创建占位业务服务，由 `build-opskit` 生成配置并复用同一制包管道。后者仍必须包含七个内置运维镜像及完整 `opskit_roles`，验收报告的业务 `roles=[]` 不会免除管理端、采集端和依赖的真实断网验收。

```json
{"reference":"opskit/college-app-web-web:1.0.0-<短ID>","source_reference":"原镜像引用","image_id":"sha256:<源引擎ID>","config_digest":"sha256:<归档配置摘要>","rootfs_diff_ids":["sha256:<层摘要>"],"platform":"linux/amd64","archive":"images/<ID>.tar","sha256":"<归档SHA256>"}
```

业务 Compose image 重写为该交付版唯一别名，避免改动其他应用的镜像标签。OpsKit 镜像同样使用唯一别名，分别写入 `opskit/management/image-defaults.json` 和 `opskit/collector/image-defaults.json`（键为现有镜像环境变量名）；基础 OpsKit 安装器读取这些默认值。导入镜像按归档去重，核对 image_id/平台后才建立别名，始终不拉镜像、不构建。

源引擎可能使用 OCI 索引 ID，经典 Docker 引擎可能使用 config ID。运行时只接受本次 `docker load` 输出的身份，同时核对归档配置摘要、平台、运行配置和文件层，再建立交付别名。`opskit_roles` 标明各内置角色所需镜像，`opskit_images` 保存原镜像变量到唯一别名的映射；业务角色的 `image_references` 与自己的 Compose 对应。

verification 记录真实的 images_exported、prerequisites_verified、target_smoke 状态，未实测不得写通过。缺镜像、依赖或业务验收时构建失败，不能生成标为可直接交付的正式包。允许独立 inspect 命令输出待补事项；正式 build 不以占位文件或空依赖目录代替完成。

现场 install.sh 支持 `--role <角色id|management|collector|hub>`、`--action install|upgrade|check|backup|rollback`、`--non-interactive`、`--env-file <本地配置>`、`--state-root <目录>`。无参数使用中文菜单。业务状态默认 `/opt/opskit-apps/<应用id>/<角色id>/`，项目名稳定，数据卷不随版本变换。OpsKit 原安装入口始终传 --offline，并使用包内锁定镜像；首次管理中心配置仍由现有中文向导填写。

内置 OpsKit 角色只提供 install/check，跨版本维护依各模块原有说明执行；不得将复制配置当成 MariaDB/SQLite 的一致性备份。业务角色根据其 hooks 和 rollback 策略开放维护动作。

另提供 `--role management|hub --action sync-docs [--documents-dir <已有库>]`：只同步文档，不连接 Docker、不启动或升级 OpsKit。默认定位本应用已安装的管理端；自定义目录用于将多套业务资料汇入一个已有 Hub。来源记录、校验后的备份及本地编辑冲突保护必须保留。
