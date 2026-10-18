# 子路径前端

本目录是 NPM 2.16.0 与 Spug 4.1.1 的预编译前端，分别使用 `/npm/` 与 `/spug/`。安装机不需要 Node.js。`versions.json` 记录上游源码及镜像身份，`static-hashes.json` 记录每个运行文件的 SHA256；安装器会在启动容器前校验。

Spug 页面底部提供许可证与完整对应源码下载。离线也可直接解开 `spug/opskit-corresponding-source.tar.gz`，其中包含两套上游源码、修改补丁、依赖锁、许可证、中文构建说明与构建脚本。重新构建在制包机上进行，使用 Node.js 24.21.0 与 npm 11.19.0；安装机只使用构建产物。对应源码含上游开发材料，不参与现场安装执行。

项目仓库的 `tools/prefix-ui/` 保存同一套构建材料。升级上游镜像时须同步适配和验证前端，不能只修改镜像版本。NPM 沿用 MIT，Spug 沿用 AGPL-3.0，具体许可见各自 `LICENSE.txt`。
