#!/usr/bin/env bash
# file_path: scripts/run.sh
# 실시간 테스트 서버를 PC에서 실행한다.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  python3.12 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi

if ! .venv/bin/python -c 'import fastapi, uvicorn, multipart, yaml' >/dev/null 2>&1; then
  .venv/bin/python -m pip install 'fastapi>=0.110,<1' 'uvicorn[standard]>=0.29,<1' 'python-multipart>=0.0.9,<1' 'PyYAML==6.0.3'
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
server_settings="$(.venv/bin/python -m scripts.app_settings)"
mapfile -t server_values <<< "$server_settings"
APP_HOST="${server_values[0]}"
APP_PORT="${server_values[1]}"
server_url="${server_values[2]}"
if curl -fsS --max-time 2 "$server_url/api/health" >/dev/null 2>&1; then
  echo "[run] 포트 $APP_PORT 에 이미 테스트 서버가 실행 중입니다." >&2
  exit 1
fi
echo "[run] PC 주소: $server_url"
echo "[run] 휴대폰 연결: 다른 터미널에서 ./scripts/tunnel.sh"
exec .venv/bin/python -m uvicorn backend.app:app --host "$APP_HOST" --port "$APP_PORT" --workers 1
