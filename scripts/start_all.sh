#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SIM_TRADE_ENV_FILE:-$PROJECT_ROOT/.env.production}"
START_WAIT_TIMEOUT_SECONDS="${START_WAIT_TIMEOUT_SECONDS:-120}"

APP_SERVICES=(
  api
  outbox-publisher
  order-event-consumer
  market-data-subscriber
  matching
  trade-event-pnl
  realtime-pnl
  pnl-snapshot-persistence
  cash-valuation-tick
  cash-valuation-fact
  cash-valuation-persistence
  risk-monitor
  realtime-event-projection
  websocket-gateway
  reference-sync
  user-registration
)

if [[ ! -f "$ENV_FILE" ]]; then
  echo "生产环境文件不存在: $ENV_FILE" >&2
  exit 1
fi

COMPOSE=(
  docker compose
  --project-directory "$PROJECT_ROOT"
  --env-file "$ENV_FILE"
  -f "$PROJECT_ROOT/docker-compose.yml"
)

CA_OVERRIDE="$PROJECT_ROOT/deploy/compose.ca.yml"
if [[ -f "$CA_OVERRIDE" ]]; then
  COMPOSE+=(-f "$CA_OVERRIDE")
fi

for dependency in postgres redis; do
  container_id="$("${COMPOSE[@]}" ps -q "$dependency")"
  if [[ -z "$container_id" ]]; then
    echo "$dependency 尚未创建或启动；请先启动基础设施。" >&2
    exit 1
  fi
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
  if [[ "$health" != "healthy" ]]; then
    echo "$dependency 当前状态不是healthy: $health" >&2
    exit 1
  fi
done

echo "PostgreSQL与Redis健康，启动全部业务服务。"
"${COMPOSE[@]}" up \
  -d \
  --no-deps \
  --wait \
  --wait-timeout "$START_WAIT_TIMEOUT_SECONDS" \
  "${APP_SERVICES[@]}"

echo "业务服务启动完成；PostgreSQL与Redis未被重建。"
"${COMPOSE[@]}" ps "${APP_SERVICES[@]}"
