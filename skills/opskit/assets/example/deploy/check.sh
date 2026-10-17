#!/usr/bin/env bash
# 经 Docker socket 进行容器内业务检查，测试控制器无需连接现场发布端口。
set -euo pipefail
for attempt in {1..20}; do
  if docker compose --project-name "$COMPOSE_PROJECT_NAME" \
    --env-file "$OPSKIT_ENV_FILE" -f "$OPSKIT_APP_DIR/compose.json" \
    exec -T web python -c 'import json,os,urllib.request; result=json.load(urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=5)); assert result["ok"] is True and result["version"] == os.environ["APP_VERSION"]; print("通过：演练服务返回本次配置的版本")'; then
    exit 0
  fi
  sleep 1
done
printf '演练服务未在规定时间内返回正确版本。\n' >&2
exit 1
