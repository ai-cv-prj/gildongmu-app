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
if [ ! -x .venv/bin/python ]; then
  echo "[tunnel] 먼저 ./scripts/run.sh로 실행 환경을 준비하세요." >&2
  exit 1
fi
server_settings="$(.venv/bin/python -m scripts.app_settings)"
mapfile -t server_values <<< "$server_settings"
server_url="${server_values[2]}"
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "[tunnel] cloudflared 명령을 찾지 못했습니다. README의 설치 안내를 확인하세요." >&2
  exit 1
fi
if ! curl -fsS --max-time 3 "$server_url/api/health" >/dev/null; then
  echo "[tunnel] 서버에 연결할 수 없습니다. 먼저 다른 터미널에서 ./scripts/run.sh 를 실행하세요." >&2
  exit 1
fi
echo "[tunnel] HTTPS 터널 주소를 생성하는 중입니다."

# Keep transport failures beside app.log so field-test disconnects can be correlated later.
tunnel_log_dir="$(.venv/bin/python -c 'from src.settings import load_paths, resolve_path; print(resolve_path(load_paths()["session_dir"]) / "logs")')"
mkdir -p "$tunnel_log_dir"
tunnel_log="$(mktemp "$tunnel_log_dir/cloudflared-$(date +%Y%m%d-%H%M%S)-XXXXXX.log")"
echo "[tunnel] 연결 로그: $tunnel_log"
tunnel_pid=""

# 실행 중인 터널 프로세스를 정리하고 연결 로그는 보존한다.
cleanup() {
  if [ -n "$tunnel_pid" ] && kill -0 "$tunnel_pid" 2>/dev/null; then
    kill "$tunnel_pid" 2>/dev/null || true
    wait "$tunnel_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cloudflared tunnel --url "$server_url" >"$tunnel_log" 2>&1 &
tunnel_pid="$!"
mobile_url=""

# cloudflared가 생성한 HTTPS 주소를 최대 30초 동안 기다린다.
for ((attempt = 0; attempt < 300; attempt++)); do
  if mobile_url="$(grep -Eom1 'https://[[:alnum:].-]+\.trycloudflare\.com' "$tunnel_log")"; then
    break
  fi

  if ! kill -0 "$tunnel_pid" 2>/dev/null; then
    echo "[tunnel] HTTPS 터널을 생성하지 못했습니다." >&2
    cat "$tunnel_log" >&2
    wait "$tunnel_pid" || true
    exit 1
  fi
  sleep 0.1
done

if [ -z "$mobile_url" ]; then
  echo "[tunnel] 30초 안에 HTTPS 주소를 확인하지 못했습니다." >&2
  cat "$tunnel_log" >&2
  exit 1
fi

echo
echo "🔴 모바일 접속 주소: $mobile_url"

if ! wait "$tunnel_pid"; then
  echo "[tunnel] HTTPS 터널이 종료되었습니다." >&2
  tail -n 20 "$tunnel_log" >&2
  exit 1
fi
