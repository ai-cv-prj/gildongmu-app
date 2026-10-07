# 팀 결과 중앙 조회 서버

각 팀원은 기존 서버에서 앱과 모델을 실행하고 코드를 수정합니다. 별도로 실행하는 동기화 프로그램이
`test-result`의 영상·분석 결과·로그를 사용자 PC의 Docker 조회 서버로 복사합니다. 팀원들은 같은
조회 주소에 접속해 네 서버의 결과를 확인합니다. PC의 조회 서버에는 GPU나 모델 가중치가 필요하지 않습니다.

```text
팀원 1 서버: 앱 + 동기화 ─┐
팀원 2 서버: 앱 + 동기화 ─┤ HTTPS 업로드
팀원 3 서버: 앱 + 동기화 ─┼─────────> 사용자 PC: Docker 결과 조회 서버
팀원 4 서버: 앱 + 동기화 ─┘                       │
                                    ./data/result-hub에 영구 저장
                                               │
                                      팀원 공통 브라우저 조회
```

조회 서버와 분석 서버는 독립적입니다. 분석 앱을 수정·재시작해도 중앙에 업로드된 결과는 남고,
중앙 서버가 잠시 꺼져도 각 서버의 로컬 결과는 계속 저장됩니다. 중앙 서버가 다시 연결되면
동기화 프로그램이 다음 주기에 재시도합니다. 각 분석 앱의 한 번에 한 세션 제한은 그대로입니다.

## 1. 사용자 PC에서 한 번 설정

Docker Engine 또는 Docker Desktop과 Compose 플러그인이 필요합니다. WSL에서 실행한다면
Docker Desktop의 해당 배포판 WSL Integration을 먼저 켜고 `docker version`과
`docker compose version`이 정상인지 확인합니다. 프로젝트는 현재 사용자 계정으로 접근·쓰기
가능한 폴더에 둡니다. 다음 명령은 프로젝트 루트에서 실행합니다.

초기화 프로그램에는 Python 3 표준 라이브러리만 필요합니다. 기존 가상환경을 활성화하거나,
Python 실행 명령이 `python3`인 환경에서는 아래 `python`을 `python3`로 바꿉니다.

```bash
python -m scripts.init_hub_env
docker compose --env-file .env.hub -f compose.hub.yaml up -d --build
```

초기화 프로그램은 현재 사용자 소유의 `data/result-hub` 폴더와 권한 `0600`인 `.env.hub`를 만듭니다.
브라우저 조회 계정 `team`의 무작위 비밀번호와 `member1`~`member4`의 서로 다른 업로드 토큰을
생성하며 비밀값을 터미널에 출력하지 않습니다. 이미 있는 `.env.hub`는 덮어쓰지 않습니다.
일반 사용자로 실행해야 하며 `sudo`로 초기화하지 않습니다. 포트나 조회 계정 이름을 바꾸려면
최초 생성 시 `--port 8088 --username review-team`을 사용할 수 있습니다.

`.env.hub`는 기존 분석 앱의 `.env`와 별개이며 Git에서 제외됩니다. 설정 형식은
[`configs/hub.env.example`](../configs/hub.env.example)을 참고합니다. `.env.hub`는 Docker Compose가
읽는 파일이므로 셸에서 `source`하지 않습니다. 파일 전체를 팀원에게 배포하지 않습니다.

기본 주소는 `http://127.0.0.1:8080`입니다. 브라우저에서 이 주소를 열고 `.env.hub`의
`HUB_VIEWER_USERNAME`과 `HUB_VIEWER_PASSWORD`로 로그인합니다. 조회 계정은 읽기 전용입니다.
상태 확인과 컨테이너 로그는 다음 명령으로 볼 수 있습니다.

```bash
docker compose --env-file .env.hub -f compose.hub.yaml ps
docker compose --env-file .env.hub -f compose.hub.yaml logs --tail 100 result-hub
```

Compose는 컨테이너를 현재 호스트의 UID/GID로 실행합니다. 저장 폴더는 초기화 프로그램이
미리 만들며, 폴더가 없으면 시작을 거부해 Docker가 root 소유 폴더를 자동 생성하지 않도록 합니다.
다른 PC로 옮길 때는 저장 폴더 소유권과 `.env.hub`의 `HUB_UID`·`HUB_GID`를 새 PC의 사용자에 맞춥니다.

## 2. 팀이 접속할 HTTPS 주소

컨테이너 포트는 PC의 `127.0.0.1`에만 열립니다. 외부 팀원과 분석 서버는 이 PC의 localhost 주소로
접속할 수 없으므로 PC에서 HTTPS 터널 또는 HTTPS 역방향 프록시를 연결합니다.

Docker에서 임시 HTTPS 주소를 만들고 백그라운드로 유지하려면 다음 명령을 실행합니다.
`quick-tunnel`은 명시적으로 켜는 선택 프로필이며, 기본 허브 실행만으로 외부 주소가 생기지는 않습니다.

```bash
docker compose --env-file .env.hub -f compose.hub.yaml --profile quick-tunnel up -d quick-tunnel
docker compose --env-file .env.hub -f compose.hub.yaml logs --tail 60 quick-tunnel
```

로그에 표시되는 `https://임의이름.trycloudflare.com`이 팀 공통 접속 주소입니다.
Cloudflare 계정·도메인·API 토큰을 추가로 넣을 필요가 없습니다. 조회 시에는 허브의 팀 계정과
비밀번호, 서버 전송에는 서버별 Bearer 토큰을 사용합니다. 외부 인증 옵션인 `--allowed-mail`은
자동 전송기의 비대화형 접속을 막으므로 이 구성에서는 사용하지 않습니다.
자세한 동작은 [Cloudflare Quick Tunnels 공식 안내](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)를 참고합니다.

외부 연결만 종료하려면 다음 명령을 사용합니다. 로컬 결과 허브와 저장 자료는 유지됩니다.

```bash
docker compose --env-file .env.hub -f compose.hub.yaml --profile quick-tunnel stop quick-tunnel
```

Docker 터널 대신 이미 설치한 `cloudflared`를 별도 터미널에서 직접 실행할 수도 있습니다.

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

터널이 출력한 HTTPS 주소가 공통 조회 주소이자 각 분석 서버의 `HUB_URL`입니다. 임시 터널을
종료·재시작하면 주소가 바뀔 수 있어 팀원 서버 설정도 바꿔야 합니다. 지속 운영에는 고정 주소의
터널 또는 HTTPS 프록시를 사용합니다. 프록시가 `Authorization` 헤더와 영상 업로드의 PUT 요청을
전달하도록 설정합니다. 업로드 크기·요청 시간 제한도 저장할 클립 크기를 수용해야 합니다.

브라우저는 HTTP Basic 계정으로 조회하고, 동기화 프로그램은 담당 서버의 Bearer 토큰으로만
업로드합니다. 운영자는 조회용 계정과 각 팀원의 개별 업로드 토큰을 비공개로 전달합니다.
Basic 계정과 토큰이 오가는 외부 연결에는 HTTPS를 사용합니다. Docker 터널 컨테이너는 PC/Docker
재시작 뒤 다시 실행될 수 있으며 그때 임시 주소도 바뀝니다. 재접속할 때 로그에서 현재 주소를
확인하고 분석 서버의 `HUB_URL`을 갱신한 뒤 동기화 프로그램도 재시작하세요.
위 `stop` 명령으로 멈춘 터널은 직접 다시 시작할 때까지 유지됩니다.

## 3. 팀원 서버마다 동기화 실행

운영자는 `member1`~`member4`를 각 서버에 고정 배정합니다. 같은 source ID를 여러 서버가
함께 사용하지 않습니다. 분석 서버의 기존 `.env`에 다음 세 값만 추가하거나 프로세스 환경변수로
설정합니다. 아래 값은 예시이므로 실제 HTTPS 주소와 담당 토큰으로 바꿉니다.

```dotenv
HUB_URL=https://YOUR-HUB-HOST
HUB_SOURCE_ID=member1
HUB_SOURCE_TOKEN=YOUR_MEMBER1_TOKEN
```

`HUB_SOURCE_TOKEN`에는 PC의 `.env.hub` 안 `HUB_SOURCE_TOKENS_JSON`에서 본인의 source ID에
해당하는 토큰만 넣습니다. 다른 팀원의 토큰이나 브라우저 조회 비밀번호를 넣지 않습니다.
동기화 명령은 프로젝트 루트에서 기존 앱과 별도 터미널/프로세스로 실행합니다.

```bash
python -m scripts.sync_results
```

기본적으로 15초마다 결과를 확인하고 변경 파일을 전송합니다. 한 번만 전송하거나 사용자 지정
저장 폴더를 사용하려면 다음과 같이 실행합니다.

```bash
python -m scripts.sync_results --once
python -m scripts.sync_results --output-dir /absolute/path/to/test-result --interval 15
```

`--once`는 전송 실패가 있으면 종료 코드 1을 반환합니다. 반복 모드는 실패한 파일을 다음 주기에
다시 시도합니다. 토큰은 명령행 인수로 받지 않으며 환경변수와 프로젝트 루트 `.env`만 사용합니다.
프로세스 환경변수가 `.env`보다 우선합니다. `--hub-url`·`--source-id`로 해당 설정을 지정할 수도 있습니다.

동일 PC에서 검증할 때는 `http://127.0.0.1:8080`을 사용할 수 있습니다. 기본적으로 HTTPS와
로컬 루프백 HTTP만 허용하며, 사설망 HTTP는 별도로 구성한 환경에서 `--allow-http`를 명시해야 합니다.
현재 Compose는 루프백에만 포트를 열므로 옵션 하나로 다른 서버에서 바로 접속할 수 있지는 않습니다.

## 4. 모이는 자료와 조회 범위

| 자료 | 중앙에 복사하는 내용 |
| --- | --- |
| 세션 정보 | 날짜·기종·메모·처리 프레임 수 등 `session.json` |
| 프레임별 분석 | `results.jsonl` |
| 이벤트 | `events.jsonl`의 지연·안내·탑승·GPS/OCR·녹화 기록 |
| 선택 구간 영상 | 클립 원본 WebM/MP4, `inference.mp4`, `manifest.json` |
| 기존 녹화 형식 | 세션의 `camera.mp4`, `camera_overlay.mp4` |
| 서버 로그 | `logs/app.log`와 회전된 로그 |

실행 중인 JSONL·로그는 완성된 줄까지 스냅샷으로 전달합니다. 테스트가 끝난 뒤 업로드·변환되어
늦게 생기는 영상도 다음 주기에 전송합니다. 종료된 세션도 계속 확인하므로 먼저 세션 정보가
올라오고 나중에 영상이 추가될 수 있습니다. 원본·추론 영상 전송이 끝나야 해당 클립의
manifest를 전송합니다. 네트워크 오류로 일부만 올라온 경우에도 다음 주기에 재시도합니다.

임시 프레임 JPEG, 마스크·오버레이 PNG, 업로드 청크 등 중간 파일은 보내지 않습니다.
현재 범위는 **실시간 테스트의 `test-result`**이며 저장 영상 CLI의 `data/samples/output`은
포함하지 않습니다. 로컬에서 삭제한 파일은 중앙에서 자동 삭제하지 않습니다.

새 세션의 `provenance.json`에는 **서버 프로세스를 초기화할 때 디스크에서 관측한 정보**가 기록됩니다.
Git 커밋·변경 여부, 코드·설정 파일의 SHA-256, Python 버전, 가중치 파일의 이름·크기·수정 시각을
확인할 수 있습니다. 비밀 환경변수와 파일 내용은 보내지 않으며 큰 가중치의 내용도 해싱하지 않습니다.
이 기록은 실제 메모리에 로딩된 코드·모델의 정확한 버전을 보증하지 않습니다. 프로세스 실행 중
파일을 수정해도 최초 기록을 유지하므로, 수정된 버전으로 테스트하려면 촬영·업로드 종료 후 앱을
재시작하세요. 기존 세션에는 버전을 추정해 채우지 않습니다. 분석 서버에도 새 기록 기능을 적용해야 합니다.

## 5. 코드 변경·재시작·보관

분석 코드는 팀원 서버에서 기존 방식으로 수정합니다. 동기화 프로그램은 저장된 파일을 읽으며
분석 앱을 실행하거나 재시작하지 않습니다. 조회 서버의 코드를 바꿨을 때만 사용자 PC에서 다시 빌드합니다.

```bash
docker compose --env-file .env.hub -f compose.hub.yaml up -d --build
```

조회 서버는 단일 worker로 동작하며 `--reload`를 사용하지 않습니다. 조회 서버를 다시 만드는
동안 일시적으로 접속이 중단되면 동기화 프로그램이 재시도합니다. 컨테이너를 재생성해도 호스트의
`data/result-hub`는 남습니다. `docker compose down`으로 컨테이너를 내려도 이 bind 폴더는 삭제하지 않습니다.

사용자 PC와 Docker·터널이 실행 중이어야 중앙 조회가 가능합니다. PC를 끄거나 절전 상태로 두면
접속이 중단됩니다. 각 서버의 로컬 결과와 중앙 `data/result-hub` 폴더는 별도로 백업합니다.
중앙 결과를 정리하기 전에는 업로더도 계속 확인하므로, 로컬에 남은 파일이 다시 전송될 수 있음을 고려합니다.
자동 보관 기간·용량별 삭제는 하지 않습니다. Docker 자체 서비스 로그는 10MiB씩 최대 3개로
제한하며, 업로드된 분석 로그와 영상의 보관 용량은 운영자가 관리합니다.

## 6. 문제 확인

| 증상 | 확인할 내용 |
| --- | --- |
| WSL에서 Docker 명령 실패 | Docker Desktop 실행 여부와 해당 WSL 배포판 Integration 설정 |
| 초기화 시 `.env.hub already exists` | 기존 설정을 유지하고 실행 단계로 진행. 재생성하면 기존 팀원 토큰이 달라짐 |
| 저장 폴더 permission denied | 현재 사용자, `HUB_UID`·`HUB_GID`, `data/result-hub` 소유권·쓰기 권한 |
| 조회 로그인 실패 | `.env.hub`의 viewer 계정 사용 여부. source 토큰은 브라우저 비밀번호가 아님 |
| 업로더 인증 실패 | source ID와 그 ID에 배정된 토큰의 짝, 환경변수의 `.env` 덮어쓰기 여부 |
| 원격 서버 연결 실패 | PC·Docker·터널 실행 여부, HTTPS 주소 변경 여부, 프록시 PUT/업로드 제한 |
| 영상이 아직 안 보임 | 테스트 종료 후 원본 업로드·서버 변환 완료 여부와 동기화 재시도 기록 |
| 결과가 안 모임 | 동기화 프로그램 실행 여부, `--output-dir`이 실제 `session_dir`를 가리키는지 확인 |

실제 Docker 빌드·실행은 Docker가 사용 가능한 호스트에서 위 명령으로 확인합니다. Python 테스트가
통과한 것만으로 Docker 배포와 외부 팀원 접속까지 검증된 것으로 보지 않습니다.
