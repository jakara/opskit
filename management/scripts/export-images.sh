#!/usr/bin/env bash
# 在与目标机器 CPU 架构相同的联网机器上执行，只导出管理模块镜像。
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo '请先安装 Docker Engine 和 Compose 插件。' >&2; exit 1; }
OUT="${1:-offline-images}"
mkdir -p "$OUT"
ENV_FILE=.env.example
if [[ -f .env ]]; then ENV_FILE=.env; fi
docker compose --env-file "$ENV_FILE" config --images | sort -u > "$OUT/images-management.txt"
MGMT_IMAGES=()
while IFS= read -r img; do MGMT_IMAGES+=("$img"); done < "$OUT/images-management.txt"
for img in "${MGMT_IMAGES[@]}"; do docker pull "$img"; done
docker image save -o "$OUT/opskit-management-images.tar" "${MGMT_IMAGES[@]}"
(cd "$OUT" && if command -v sha256sum >/dev/null; then sha256sum ./*.tar; else shasum -a 256 ./*.tar; fi > SHA256SUMS.txt)
echo "管理端镜像已导出至 ${OUT}。不要将 .env、collector.env 或备份凭据打入交付包。"
