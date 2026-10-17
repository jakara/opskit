# 验收与现场手册

## 制包阶段需要留下的证据

正式封包的验收 JSON 由 `build_bundle.py seal` 校验，具体字段以 `build_bundle.py --help` 和生成的 manifest 为准。最低字段如下：

```json
{
  "schema_version": 1,
  "artifact_id": "暂存 manifest 中的 artifact_id",
  "target": {"os": "anolis", "version": "8.9", "arch": "amd64"},
  "network": "disabled",
  "method": "anolis-container",
  "roles": [
    {"id": "web", "passed": true, "action": "install", "checks_passed": true}
  ],
  "opskit": {"management": true, "collector": true},
  "prerequisites": {"verified": true},
  "notes": "注明真实证据位置、容器/VM边界以及未覆盖的外部集成"
}
```

这只是字段示例，不是通过证据。真实结果未通过时写 false，封包应被拒绝。有状态角色需额外 `stateful_backup_passed=true`，只能在隔离恢复检查通过后填写。完整业务升级还应保留前后版本、备份、升级与回退记录；不兼容的数据迁移不因普通 HTTP 200 就算通过。

测试环境不得占用原有 OpsKit 的默认项目或端口。使用显式临时 `COMPOSE_PROJECT_NAME`、管理 HTTP/NPM/Spug 端口及独立状态目录，记录测试前后非测试容器的 ID 和状态。不删除或更改其他项目、用户数据库或共享数据卷。清理仅限本次测试项目。

断网依赖安装用干净目标容器 `--network none`。业务应用测试使用无外网的内部 Docker 网络；若测试器必须通过本地 Docker socket 控制隔离项目，应如实记载“目标容器无外网、Docker daemon 复用宿主机”，不能写成完整虚拟机验收。确认运行时未执行 pull/build，容器不能在启动后下载缺失代码。

## 现场操作.md 必须具体

1. 文件名、包版本、目标 OS/CPU、预期占用空间，校验命令及期望校验成功输出。
2. 解压到哪个目录、哪台主机选择哪个菜单项。没有真实客户 IP 时用“现场填写”，不虚构客户地址。
3. 每个中文参数的含义、是否必填、默认值、连接的是哪一台主机；密码输入不回显。
4. 管理中心、各业务角色、采集端的推荐执行顺序。数据库未就绪不能继续部署依赖它的角色。
5. 每个角色的成功判据、最终入口、日志路径和失败时要交给上级的诊断信息。
6. 升级与回退前置条件，真实备份路径；不能承诺未实现的数据恢复、跨主机事务或零停机。

## Spug操作.md 必须具体

为每个业务角色给出应用名称/标识、现场环境、应选主机、发布方式=自定义发布、串行发布、目标主机动作及可复制命令。动作直接调用本包 install.sh 的非交互角色命令，不要求现场人员写脚本。

首次安装先在各目标机运行向导，持久配置保存在状态目录；后续通过 Spug 复用该配置。需要数据传输时可选择发布时上传，或先把大包受控复制到目标机。不能声称本工具自动创建了 Spug 发布配置；没有真实 Spug API 集成验证时必须给出人工可复制字段。只要前一主机失败就停止后续主机。

## 相关官方能力

- [Docker 镜像导出](https://docs.docker.com/reference/cli/docker/image/save/) / [导入](https://docs.docker.com/reference/cli/docker/image/load/)：镜像不包含业务卷数据。
- [Compose 配置解析](https://docs.docker.com/reference/cli/docker/compose/config/)：制包不能意外读取开发机 .env 或展开真实凭据。
- [Buildx 镜像索引](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/)：按目标平台锁定 digest。
- [DNF download](https://dnf-plugins-core.readthedocs.io/en/latest/download.html)：`--resolve --alldeps` 包含已装依赖，仍须干净环境断网安装验收。
- [Spug 自定义发布](https://ops.spug.cc/docs/deploy-config/) / [发布申请](https://ops.spug.cc/docs/deploy-request/)：提供动作执行和分发，应用备份与迁移逻辑由交付脚本定义。
