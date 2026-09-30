#!/usr/bin/env bash
# file_path: scripts/tunnel.sh
# 실행 중인 PC 서버에 휴대폰용 HTTPS 터널을 연결한다.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
APP_PORT="${APP_PORT:-8001}"
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "[tunnel] cloudflared 명령을 찾지 못했습니다. README의 설치 안내를 확인하세요." >&2
  exit 1
fi
if ! curl -fsS --max-time 3 "http://127.0.0.1:$APP_PORT/api/health" >/dev/null; then
  echo "[tunnel] 서버에 연결할 수 없습니다. 먼저 다른 터미널에서 ./scripts/run.sh 를 실행하세요." >&2
  exit 1
fi
echo "[tunnel] 출력되는 https://...trycloudflare.com 주소를 휴대폰 브라우저에서 여세요."
exec cloudflared tunnel --url "http://127.0.0.1:$APP_PORT"
