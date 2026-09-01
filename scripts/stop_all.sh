#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SIM_TRADE_ENV_FILE:-$PROJECT_ROOT/.env.production}"
STOP_TIMEOUT_SECONDS="${STOP_TIMEOUT_SECONDS:-30}"

APP_SERVICES=(
  api
  websocket-gateway
  market-data-subscriber
  outbox-publisher
  order-event-consumer
  matching
  trade-event-pnl
  realtime-pnl
  pnl-snapshot-persistence
  cash-valuation-tick
  cash-valuation-fact
  cash-valuation-persistence
  risk-monitor
  realtime-event-projection
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

echo "停止全部业务服务，保留PostgreSQL与Redis。"
"${COMPOSE[@]}" stop \
  --timeout "$STOP_TIMEOUT_SECONDS" \
  "${APP_SERVICES[@]}"

echo "业务服务已停止；PostgreSQL与Redis继续运行。"
"${COMPOSE[@]}" ps postgres redis
