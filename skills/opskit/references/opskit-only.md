# OpsKit 运维工具独立气隙包

仅在用户选择“运维工具在制包时打齐离线资源”时使用。本流程不需要业务代码仓库，不创建空业务应用；管理中心、采集端及精简管理端仍是原有模块。

## 选择目标

集中确认下列缺失信息，用户已给出的直接采用：

| 选择项 | 当前完整离线目标 |
| --- | --- |
| OS | Anolis OS（龙蜥） |
| 发行版本 | 8.9、8.10 |
| CPU 架构 | x86_64 对应 amd64；aarch64/ARM64 对应 arm64 |

不能根据制包机推断客户架构。ARM Mac 可以制作 x86_64 包，但下载、依赖解析、镜像导出与验收必须使用目标平台。其他 OS 不套用龙蜥 RPM 包；先明确暂不支持其完整离线依赖，不能改一个字段就交付。原联网安装版继续支持已有的 Linux、macOS、Windows 入口。

## 准备资源

1. 获取 Skill 自带的 OpsKit 固定快照或用户明确指定的可信版本，不携带开发机 `.env`、安装草稿、密码、数据卷或历史备份。
2. 按 [依赖收集说明](prerequisites.md) 准备目标 OS/CPU 的 Python、Docker/Compose 和系统工具。可复用同一目标的已核验闭包，必须重新核对清单与文件哈希。

```text
python3 <技能目录>/scripts/collect_prerequisites.py --os anolis --version 8.9 --arch amd64 --output <工作目录>/prerequisites
```

3. 生成完整暂存包，不需要 `--repo` 或业务配置：

```text
python3 <技能目录>/scripts/build_bundle.py build-opskit --os anolis --version 8.9 --arch amd64 --prerequisites-dir <工作目录>/prerequisites --output <工作目录>/staging
```

按所选目标替换版本与架构。输出目录名含 OpsKit 版本、龙蜥版本和 CPU 架构；发行清单的 `kind` 为 `opskit`，业务 `roles` 为空，运维角色在 `opskit_roles` 中。包内包含七个固定版本运维镜像、目标离线 RPM、中文安装向导、文档中心资源及 SHA-256 清单。

制包器锁定远程平台 digest 后导出镜像；只有已经构建/核验目标架构的本地镜像才可用 `--use-local-images`。如公司已同步到可信仓库，可重复传 `--image SPUG_IMAGE=registry.example/opskit/spug:4.1` 等已知镜像变量。不得用 Spug 3.x 或其他应用替代缺失的 4.1 镜像，也不以换架构解决拉取失败。

没有镜像、RPM 或未通过依赖离线验证时，应修复后重试；不生成名字像正式包的空壳。暂存目录只是待验收材料，不能当作正式交付。

## 断网验收与封包

使用与客户匹配的独立龙蜥 VM 或受控目标容器环境，断开外网后验证：

- 离线依赖签名、完整 RPM 事务和 Python/Docker/Compose 可用；保留检查方法和实际环境限制。
- 管理中心从包内导入镜像、按中文向导配置并启动；Hub、NPM、Spug、DBX、日志查询及鉴权可用。
- 另一目标采集端只导入其 Fluent Bit 镜像，追加日志能在管理端按标签查到。
- 页面文档可离线预览。镜像、依赖和安装脚本全过程没有外网拉取。

验收报告沿用 [交付契约](bundle-contract.md) 的 schema；`roles` 为 `[]`，`opskit.management`、`opskit.collector` 和 `prerequisites.verified` 必须由实际验证得到，不能因为没有业务角色而省略。仅验证 manifest、Compose 或模拟 Docker 返回值不能写成通过。缺真实运行环境时保留待验收目录并说明缺项。

```text
python3 <技能目录>/scripts/build_bundle.py seal --bundle <暂存目录/实际包目录> --acceptance <断网验收报告.json> --output <交付目录>
```

封包后复核 `.tar.gz`、同名 `.sha256`、解压文件校验和脚本权限。最终交付整个发行包，现场保持包内文件不变，解压后运行根目录 `./install.sh`，选择安装操作及管理端/采集端角色。脚本自动从包内准备依赖、导入镜像并以 `--offline` 运行 OpsKit；不要求现场工程师手工执行 `docker load`、配置公共镜像源或安装 Python。

安装完成后，工程师在 Spug 中纳管真实主机，在 DBX 中填写真实数据库连接。制作独立工具包不代表已经在客户环境完成主机纳管或数据库接入。
