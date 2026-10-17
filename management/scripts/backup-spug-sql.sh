#!/usr/bin/env bash
# 只备份 Spug 的 MariaDB；其他持久卷需要单独备份。
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups
chmod 700 backups
NOW=$(date +%Y%m%d-%H%M%S)
OUT="backups/spug-sql-${NOW}-$$.sql"
TEMP="${OUT}.partial"
trap 'rm -f "$TEMP"' EXIT
docker compose exec -T spugdb sh -c 'exec mariadb-dump -u spug --password="$MARIADB_PASSWORD" --single-transaction --quick spug' > "$TEMP"
mv "$TEMP" "$OUT"
echo "已创建 ${OUT}；此文件不包含其他持久卷，不能替代整套备份。"
