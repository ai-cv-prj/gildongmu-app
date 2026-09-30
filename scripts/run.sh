#!/usr/bin/env bash
# file_path: scripts/run.sh
# 실시간 테스트 서버를 PC에서 실행한다.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  python3.12 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi

if ! .venv/bin/python -c 'import fastapi, uvicorn, multipart' >/dev/null 2>&1; then
  .venv/bin/python -m pip install 'fastapi>=0.110,<1' 'uvicorn[standard]>=0.29,<1' 'python-multipart>=0.0.9,<1'
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8001}"
if ! [[ "$APP_PORT" =~ ^[0-9]+$ ]] || [ "$APP_PORT" -lt 1 ] || [ "$APP_PORT" -gt 65535 ]; then
  echo "[run] APP_PORT는 1~65535 숫자여야 합니다." >&2
  exit 1
fi
if curl -fsS --max-time 2 "http://127.0.0.1:$APP_PORT/api/health" >/dev/null 2>&1; then
  echo "[run] 포트 $APP_PORT 에 이미 테스트 서버가 실행 중입니다." >&2
  exit 1
fi
echo "[run] PC 주소: http://127.0.0.1:$APP_PORT"
echo "[run] 휴대폰 연결: 다른 터미널에서 ./scripts/tunnel.sh"
exec .venv/bin/python -m uvicorn backend.app:app --host "$APP_HOST" --port "$APP_PORT" --workers 1
