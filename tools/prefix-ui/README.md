# OpsKit 固定版本子路径前端

本目录是制包机生成的架构无关静态产物；客户现场只读挂载，不安装 Node、Yarn 或前端依赖，不执行构建。本目录提供子路径前端及配套运行超时文件，不改变上游业务 API 或鉴权模型。

## 支持的精确上游

| 组件 | 版本 / 源码 | OCI 多架构 index digest | 只读挂载目标 |
| --- | --- | --- | --- |
| Nginx Proxy Manager | 2.16.0 / build commit `a29eae9d` | `sha256:4393e642e233e5efd5adeb7f10918d4aaea71c9ed875b94190273fc8e87f9594` | `/app/frontend` |
| Spug | 4.1.1 / `049984e01373fc1cd97b94b60c0b3124036b0f47` | `sha256:13fe7c547932e9479dc8181648ec257b91a18e941ef3f2687d9aeb79882de90c` | `/opt/spug/spug_web/build` |

`ghcr.io/openspug/spug:4.1` 是系列标签，本次缓存镜像实际为 4.1.1。不得把此适配自动套用到其他版本。`versions.json` 记录源码压缩包、补丁与依赖锁的 SHA-256；`management/ui/static-hashes.json` 逐文件记录交付字节。运行 `python3 management/scripts/verify_ui.py`（读取管理端 .env），也可由安装器调用 `verify(v, hub_only=False)` 同时检查交付物和本地镜像，未知 digest、版本或 revision 明确报错。某些旧引擎离线加载后可能丢失 digest；此时在断网、只读、不挂载任何宿主目录的临时容器中读取原始 index.html，比对固定上游 SHA-256。digest 和原始 index 均不匹配，或者版本/revision 不匹配，均停止。

NPM 生产配置直接读取 `/app/frontend`，启动时的 ownership 列表不包含该目录。Spug 每次启动从 `/opt/spug` 同步至 `/data/spug`；只读覆盖源 build 目录允许其正常同步，不能只修改持久卷中的 build。两处挂载的启动兼容结论已按精确镜像脚本核查；真实启动及浏览器业务验收由集成验证完成。

## 路径适配范围

NPM：Vite base `/npm/`、BrowserRouter basename、统一 `/npm/api/`、延迟加载资源与 CSS、图片/图标/manifest、Dashboard 裸链接、NavLink 的真实 href（包括新窗打开）。日志是 HTTP 轮询，不存在管理前端 WebSocket。NPM 所需 `Authorization: Bearer` 必须由代理保留；`/npm/api/` 不能再用同一个 Authorization 做外层 Basic 校验。

Spug：CRA `PUBLIC_URL=/spug`、history basename、统一 axios 内部 API 前缀、全部 9 处 WebSocket、SSH 新窗与裸链接、resource 下载与图标、旧数据库导航图标、侧栏当前路径、复制出的 Webhook URL。接口判断和 X-Token/X-Language 在 URL 改写前执行，外部 URL 不被改写。未增加共享的根 `/api`、`/static`、`/images` 等路由。

Spug 原生任务的“执行测试”页面最多等待一小时：仅该请求设为 `timeout: 3600000`，中英文提示同步更新。`runtime/nginx.conf` 与 `runtime/start-api.sh` 来自同版上游，分别仅将内部 `/api/` 的 `proxy_read_timeout` 从180秒改为3600秒、给原Gunicorn命令加上 `--timeout 3600`；管理端对应副本位于 `management/spug/`，制包校验两份字节一致。部署时只读挂载到 `/etc/nginx/nginx.conf` 与 `/opt/spug/spug_api/tools/start-api.sh`，外层代理读取超时也需3600秒。这两份运行源码及其校验和包含在源码下载包内。

两者当前登录使用 localStorage 令牌；NPM 键为 `authentications`，Spug 键为 `token`，没有待迁移的会话 Cookie Path。同域路径共享浏览器 origin；原独立端口的登录状态不会自动迁移，首次访问需重新登录。代理应验证实际响应中的 Cookie/Location；本补丁不凭空创建 Cookie，也不把路径当作同源隔离边界。

代理须先剥 `/npm/` 或 `/spug/` 再交给原上游 HTTP 入口，并独立验证尾斜杠、Location、WebSocket Upgrade、超时与身份头。NPM 后端 `/api/schema` 仍会在 OpenAPI `servers[0].url` 写入 `origin/api`，完整 API 契约若包含该接口，需由集成层精确适配；这不是静态前端可修改的内容。

## 制包机重建

使用 Node **24.21.0**、npm 11.19.0、Python 3、tar、patch 和联网依赖访问，在仓库根目录运行：

```sh
python3 tools/prefix-ui/build_frontends.py --output management/ui
python3 tools/prefix-ui/verify.py --output management/ui
```

脚本先核验压缩包/补丁/锁文件，任何不匹配均停止；源码与依赖只在临时目录展开。NPM 使用原始 `yarn.lock` 和固定 Yarn 1.22.22，先编译上游 locale 再执行 TypeScript+Vite。Spug 上游未附依赖锁，本次将真实解析结果保存为 `spug-package-lock.json`，重建使用 `npm ci --legacy-peer-deps`。Spug 构建保留上游既有 `useEffect` 依赖警告（AutoDeploy.js），不修改该业务行为。客户交付目录不包含 node_modules、测试文件或 source maps。

输出为 `management/ui/npm/`、`management/ui/spug/`、逐文件校验和以及制包工具目录内的构建日志。源码获取下载包位于 `management/ui/spug/opskit-corresponding-source.tar.gz`，包含完整精确上游源码压缩包、补丁、依赖锁、许可证和重建脚本；Spug 登录页、管理页和终端页提供“源码与修改”链接。源码包包含上游原有文件，运行时静态目录只包含构建产物、许可证及该源码包。

## 上游与许可证

- [NPM 2.16.0 官方源码](https://github.com/NginxProxyManager/nginx-proxy-manager/tree/v2.16.0)：MIT，保留 `npm-LICENSE.txt`；源码和小补丁随包提供。
- [Spug 精确 revision](https://github.com/openspug/spug/tree/049984e01373fc1cd97b94b60c0b3124036b0f47)：AGPL-3.0，保留 `spug-LICENSE.txt`、原有声明和构建生成的第三方许可文件；修改标识、对应源码及重建材料同包提供，下载入口必须随部署保留。
- [Vite base](https://vite.dev/config/shared-options.html#base)、[CRA 相对路径构建](https://create-react-app.dev/docs/deployment/#building-for-relative-paths)、[Nginx WebSocket 代理](https://nginx.org/en/docs/http/websocket.html)。

源码下载包独立解压后，可使用其中 `python3 build_frontends.py --output output` 重建；不依赖 OpsKit 仓库的其他文件。构建工具可联网获取依赖，现场安装不得执行构建。
