#!/usr/bin/env bash
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
fixed_url="${CLOUDFLARE_TUNNEL_URL:-}"
token_file="${CLOUDFLARE_TUNNEL_TOKEN_FILE:-}"
if [ -n "$fixed_url" ] || [ -n "$token_file" ]; then
  if [ -z "$fixed_url" ] || [ -z "$token_file" ]; then
    echo "[tunnel] 고정 주소에는 CLOUDFLARE_TUNNEL_URL과 CLOUDFLARE_TUNNEL_TOKEN_FILE이 모두 필요합니다." >&2
    exit 1
  fi
  if [[ ! "$fixed_url" =~ ^https://[[:alnum:]]([[:alnum:].-]*[[:alnum:]])?/?$ ]]; then
    echo "[tunnel] CLOUDFLARE_TUNNEL_URL은 https://app.example.com 형식이어야 합니다." >&2
    exit 1
  fi
  if [ ! -f "$token_file" ] || [ ! -r "$token_file" ] || [ ! -s "$token_file" ]; then
    echo "[tunnel] CLOUDFLARE_TUNNEL_TOKEN_FILE에 읽을 수 있는 토큰 파일을 지정하세요." >&2
    exit 1
  fi
  fixed_url="${fixed_url%/}"
fi
if ! curl -fsS --max-time 3 "$server_url/api/health" >/dev/null; then
  echo "[tunnel] 서버에 연결할 수 없습니다. 먼저 다른 터미널에서 ./scripts/run.sh 를 실행하세요." >&2
  exit 1
fi
if [ -n "$fixed_url" ]; then
  echo "[tunnel] 고정 HTTPS 터널에 연결하는 중입니다."
  echo "[tunnel] Cloudflare에 등록한 서비스 주소가 $server_url 인지 확인하세요."
else
  echo "[tunnel] HTTPS 터널 주소를 생성하는 중입니다."
fi

# 연결 오류를 서버 로그와 함께 확인할 수 있도록 터널 로그를 보관한다.
tunnel_log_dir="$(.venv/bin/python -c 'from src.settings import load_paths, resolve_path; print(resolve_path(load_paths()["session_dir"]) / "logs")')"
mkdir -p "$tunnel_log_dir"
tunnel_log="$(mktemp "$tunnel_log_dir/cloudflared-$(date +%Y%m%d-%H%M%S)-XXXXXX.log")"
echo "[tunnel] 연결 로그: $tunnel_log"
tunnel_pid=""
ready_file="$tunnel_log.pid"

# 실행 중인 터널 프로세스를 정리하고 연결 로그는 보존한다.
cleanup() {
  if [ -n "$tunnel_pid" ] && kill -0 "$tunnel_pid" 2>/dev/null; then
    kill "$tunnel_pid" 2>/dev/null || true
    wait "$tunnel_pid" 2>/dev/null || true
  fi
  rm -f "$ready_file"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "$fixed_url" ]; then
  # 주소·원본 서버 경로는 Cloudflare에서 관리하며, 토큰은 명령행에 노출하지 않는다.
  # 상위 셸의 TUNNEL_TOKEN이 지정한 토큰 파일보다 우선하지 않도록 제거한다.
  env -u TUNNEL_TOKEN cloudflared tunnel --no-autoupdate --pidfile "$ready_file" \
    run --token-file "$token_file" >"$tunnel_log" 2>&1 &
else
  cloudflared tunnel --url "$server_url" >"$tunnel_log" 2>&1 &
fi
tunnel_pid="$!"
mobile_url=""

# 고정 터널은 첫 연결 완료를, 임시 터널은 새 HTTPS 주소를 최대 30초 기다린다.
for ((attempt = 0; attempt < 300; attempt++)); do
  if ! kill -0 "$tunnel_pid" 2>/dev/null; then
    echo "[tunnel] HTTPS 터널에 연결하지 못했습니다." >&2
    cat "$tunnel_log" >&2
    wait "$tunnel_pid" || true
    exit 1
  fi
  if [ -n "$fixed_url" ]; then
    if [ -s "$ready_file" ]; then
      mobile_url="$fixed_url"
      break
    fi
  elif mobile_url="$(grep -Eom1 'https://[[:alnum:].-]+\.trycloudflare\.com' "$tunnel_log")"; then
    break
  fi
  sleep 0.1
done

if [ -z "$mobile_url" ]; then
  echo "[tunnel] 30초 안에 HTTPS 터널 준비를 확인하지 못했습니다." >&2
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
