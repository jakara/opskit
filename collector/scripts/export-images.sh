#!/usr/bin/env bash
# 在与目标业务机 CPU 架构相同的联网机器上导出本模块镜像。
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-offline-images}"
mkdir -p "$OUT"
ENV_FILE=.env.example
if [[ -f .env ]]; then ENV_FILE=.env; fi
docker compose --env-file "$ENV_FILE" config --images | sort -u > "$OUT/images-collector.txt"
IMAGES=()
while IFS= read -r img; do IMAGES+=("$img"); docker pull "$img"; done < "$OUT/images-collector.txt"
docker image save -o "$OUT/opskit-collector-images.tar" "${IMAGES[@]}"
(cd "$OUT" && if command -v sha256sum >/dev/null; then sha256sum ./*.tar; else shasum -a 256 ./*.tar; fi > SHA256SUMS.txt)
echo "采集端镜像已导出至 ${OUT}。不要将 .env 或备份凭据打入交付包。"
