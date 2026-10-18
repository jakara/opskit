# 安装界面离线依赖

固定版本的 Rich、markdown-it-py、mdurl、Pygments 均来自官方 PyPI。`manifest.json` 记录官方文件地址和 SHA-256；启动时核验并直接从 wheel 加载，无需 pip、联网或修改系统环境。许可证原文保留在各 wheel 的 dist-info 目录中。
