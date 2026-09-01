#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${SIM_TRADE_ENV_FILE:-$PROJECT_ROOT/.env.production}"
COMPOSE=(
  docker compose
  --project-directory "$PROJECT_ROOT"
  --env-file "$ENV_FILE"
  -f "$PROJECT_ROOT/docker-compose.yml"
)

if [[ ! -f "$ENV_FILE" ]]; then
  echo "生产环境文件不存在: $ENV_FILE" >&2
  exit 1
fi

echo "检查 API /health"
"${COMPOSE[@]}" exec -T api python -c \
  "import json,os,urllib.request; host=os.getenv('API_BIND_ADDRESS','127.0.0.1'); host='127.0.0.1' if host in {'0.0.0.0','::'} else host; port=os.getenv('API_PUBLISHED_PORT','8000'); d=json.load(urllib.request.urlopen(f'http://{host}:{port}/health', timeout=5)); print(json.dumps(d, ensure_ascii=False)); raise SystemExit(0 if d.get('status') == 'ok' else 1)"

echo "检查 WebSocket Gateway /health"
"${COMPOSE[@]}" exec -T websocket-gateway python -c \
  "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=5)); print(json.dumps(d, ensure_ascii=False)); raise SystemExit(0 if d.get('status') == 'ok' and d.get('single_instance_lease') is True else 1)"

echo "检查用户注册门户 /api/health"
"${COMPOSE[@]}" exec -T user-registration python -c \
  "import json,os,urllib.request; host=os.getenv('PORTAL_HOST','127.0.0.1'); host='127.0.0.1' if host in {'0.0.0.0','::'} else host; port=os.getenv('PORTAL_PORT','8008'); d=json.load(urllib.request.urlopen(f'http://{host}:{port}/api/health', timeout=5)); print(json.dumps(d, ensure_ascii=False)); raise SystemExit(0 if d.get('ok') is True and d.get('sim_trade') == 'ok' else 1)"
