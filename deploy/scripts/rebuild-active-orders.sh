#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${SIM_TRADE_ENV_FILE:-$PROJECT_ROOT/.env.production}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "生产环境文件不存在: $ENV_FILE" >&2
  exit 1
fi

docker compose \
  --project-directory "$PROJECT_ROOT" \
  --env-file "$ENV_FILE" \
  -f "$PROJECT_ROOT/docker-compose.yml" \
  run --rm api python -m app.workers.active_order_rebuild_worker
