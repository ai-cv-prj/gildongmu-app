<!--
file_path: README.md

길동무 통합 추론과 휴대폰 테스트 앱의 설치·실행·결과 확인 방법을 안내한다.
-->

# gildongmu-app

보행가능영역, 장애물, 보행자 신호등을 같은 화면에서 분석하는 프로젝트입니다.

- Mask2Former: 보행가능영역·횡단보도 분할
- YOLO: 보행 장애물 검출
- YOLO + MobileNetV3-Small: 보행자 신호등 검출·색상 분류
- BoT-SORT: 객체 추적
- FastAPI: 휴대폰 카메라 실시간 테스트
- 서울 버스 API: GPS 기반 주변 정류장·방향 선택과 도착 음성 안내
- 버스 YOLO + PARSeq: 목표 노선 번호 인식

저장 영상 추론과 휴대폰 실시간 테스트를 지원합니다. 판단 좌표는 영상 기준이며 실제 거리나
사용자의 횡단 안전을 보장하지 않습니다.

## 1. 실행 준비

### 가상환경과 패키지

Linux/WSL과 Python 3.12 기준입니다. 프로젝트 루트에서 실행하세요.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

NVIDIA GPU를 사용한다면 CUDA 13.0용 PyTorch를 먼저 설치합니다.

```bash
python -m pip install torch==2.14.0 torchvision==0.29.0 \
  --index-url https://download.pytorch.org/whl/cu130
```

CPU만 사용한다면 CPU용 PyTorch를 설치합니다.

```bash
python -m pip install torch==2.14.0 torchvision==0.29.0 \
  --index-url https://download.pytorch.org/whl/cpu
```

이후 나머지 패키지를 설치합니다. 앞에서 설치한 PyTorch는 같은 버전이므로 다시 설치되지 않습니다.

```bash
python -m pip install -r requirements.txt
```

GPU 사용 가능 여부는 다음 명령으로 확인합니다.

```bash
python -c "import torch; print(torch.cuda.is_available())"
```

### 가중치와 데이터 배치

`weights/`와 `data/`는 Git에서 제외되므로 별도로 준비해야 합니다.

```text
gildongmu-app/
├── weights/
│   ├── walking/
│   │   ├── mask2former/
│   │   │   ├── config.json
│   │   │   ├── preprocessor_config.json
│   │   │   └── model.safetensors
│   │   └── yolo/
│   │       └── yolo26n_pedestrian29_stop_v1.pt
│   └── traffic/
│       ├── best_YOLO_v2.pt
│       └── best_MobileNet.pt
└── data/
    ├── samples/
    │   ├── input/                  # 직접 준비
    │   │   └── sample1/
    │   │       └── input.mp4
    │   └── output/                 # 영상 추론 시 자동 생성
    └── sessions/                   # 휴대폰 세션 시작 시 자동 생성
```

| 파일 | 용도 |
| --- | --- |
| `weights/walking/mask2former/` | 3클래스 분할 모델과 전처리 설정 |
| `weights/walking/yolo/yolo26n_pedestrian29_stop_v1.pt` | 장애물 검출 모델 |
| `weights/traffic/best_YOLO_v2.pt` | 보행자 신호등·횡단보도 검출 모델 |
| `weights/traffic/best_MobileNet.pt` | 신호등 색상 분류 모델 |
| `data/samples/input/` | 추론할 MP4 입력 폴더 |

Mask2Former는 가중치 파일뿐 아니라 `config.json`과 `preprocessor_config.json`도 필요합니다.

## 2. 저장 영상 추론

### 샘플 폴더 실행

```bash
source .venv/bin/activate
python -m scripts.run_video_inference --sample-dir data/samples/input/sample1
```

지정한 폴더 바로 아래의 MP4만 파일명 순서대로 처리합니다. 하위 폴더는 탐색하지 않습니다.

### 영상 한 개 실행

```bash
python -m scripts.run_video_inference \
  --video-path data/samples/input/sample1/input.mp4
```

### 추론 모드

| 모드 | 실행 모델 |
| --- | --- |
| `all` | 보행 영역 + 장애물 + 신호등 |
| `both` | 보행 영역 + 장애물 |
| `sidewalk` | 보행 영역 |
| `obstacle` | 장애물 |
| `traffic` | 신호등 |

기본값은 `all`입니다.

```bash
python -m scripts.run_video_inference \
  --mode traffic \
  --video-path data/samples/input/sample1/input.mp4 \
  --output-path data/samples/output/sample1/result_traffic.mp4
```

주요 옵션은 다음 명령으로 확인할 수 있습니다.

```bash
python -m scripts.run_video_inference --help
```

## 3. 휴대폰 실시간 테스트

PC에서 두 터미널을 열어 실행합니다.

Python 3.12가 필요합니다. `python3.12 --version`으로 확인하세요. 최초 서버 실행 시
`.venv`를 생성하고 `requirements.txt`의 패키지를 설치하므로 시간이 걸립니다.
실행 전 가상환경을 직접 활성화할 필요는 없습니다.

```bash
# 터미널 1: API 서버
./scripts/run.sh
```

```bash
# 터미널 2: 휴대폰용 HTTPS 터널
./scripts/tunnel.sh
```

두 번째 터미널에 표시되는 `https://...trycloudflare.com` 주소를 휴대폰 브라우저에서 엽니다.
코드나 추론 설정을 변경했다면 실행 중인 서버에는 자동 반영되지 않으므로 `run.sh`와
`tunnel.sh`를 종료한 뒤 다시 실행합니다. 휴대폰 화면도 한 번 새로고침해야 합니다.

1. 출발 화면에서 음성 속도와 글자 크기를 설정한 뒤 **시작**을 누릅니다.
2. 카메라 권한을 허용하면 기존 보행·신호등·횡단보도 안내가 시작됩니다.
3. 정류장 근접 안내 또는 **정류장 도착** 버튼으로 번호 입력을 엽니다. 멈춤 안내가 끝난 뒤
   `143`, `N26`, `마포07`처럼 입력하고 확인 화면에서 **맞아요**를 누릅니다.
4. 같은 목표 노선으로 GPS 도착정보 조회와 카메라 번호 인식을 함께 시작합니다.
   정류장·방향 후보가 여러 개면 목록에서 확인할 수 있습니다. 위치 권한 오류는 카메라 번호 인식을 중단시키지 않습니다.
5. **일시중지**는 카메라 전송·녹화·위치 조회·음성을 멈추며 **재개**하면 새 관측으로 이어집니다.
   화면을 숨겼다가 돌아왔을 때도 같은 방식으로 처리합니다. **종료**를 누르면 기록을 저장합니다.

휴대폰 기종·메모·프레임 지표는 화면 아래 **테스트 설정 및 상태**에서 확인합니다.
음성 입력은 브라우저의 음성 인식 기능을 지원할 때 사용할 수 있으며, 미지원·권한 거부 시 직접 입력합니다.
글자 크기와 음성 속도는 이 브라우저에 저장됩니다.

### 버스 기능 준비

서버 `.env`에 `SEOUL_BUS_API_KEY`를 설정합니다. 현재 도착정보 구현은 서울시 버스 API를 사용합니다.
키는 브라우저로 보내지 않으며 `scripts/run.sh`가 환경변수로 읽습니다. 직접 uvicorn을 실행할 때는
같은 환경변수를 서버 프로세스에 전달해야 합니다.

버스 번호 인식에는 `configs/bus.yaml`에서 지정한 다음 파일이 필요합니다.

```text
weights/bus/
├── route-display-b.pt
├── yolo11n.pt
├── parseq-bb5792a6.pt
└── parseq-src/                 # 고정 PARSeq 소스 전체, hubconf.py 포함
```

이 로컬 작업본에는 test-app에서 가져온 버스 가중치와 PARSeq 소스가 배치되어 있습니다.
`weights/`는 Git에서 제외되므로 새 clone에는 별도로 준비해야 합니다. `requirements.txt`의
OCR 의존성도 설치합니다. 기존 환경은 `python -m pip install -r requirements.txt`로 갱신하세요.
`GET /api/bus/status`는 API 키와 모델 파일의 준비 여부만 반환하며 모델의 실제 로딩 성공을 뜻하지는 않습니다.

OCR은 목표 노선 확정 후 별도 작업 스레드에서 최신 프레임을 처리합니다. 이전 노선·세션의 결과는 폐기하고
보행 프레임 응답에는 OCR의 원래 촬영 시각을 함께 보냅니다. 모델 누락·OCR 오류는 별도 상태로 표시하며
기존 보행·신호 응답은 계속 제공합니다. API 키가 없어도 카메라 번호 인식은 사용할 수 있습니다.

GPS의 도착정보와 OCR 번호 확인은 독립적입니다. API의 해당 정류장 도착 상태만 도착정보로 표시하고,
OCR 한 번 관측과 반복 확인은 각각 번호 후보와 번호 확인으로 안내합니다. OCR로 번호를 읽었다는 이유만으로
버스가 정류장에 도착했다고 판단하지 않습니다. GPS는 10초, OCR은 3초가 지난 관측으로 새 음성을 시작하지 않습니다.

버스 음성은 기존 보행 위험·횡단보도·신호 안내보다 낮은 우선순위로 재생합니다. 동적 버스 안내는 서버 MP3를
우선 사용하고 실패하면 브라우저 한국어 합성을 시도합니다. 브라우저 합성은 오버레이 녹화의 MP3 오디오 트랙에
포함되지 않을 수 있습니다. 정류장 선택과 GPS/OCR 안내 이력은 세션의 `bus_events.jsonl`에 기록합니다.

정류장 근접은 화면 기준의 임시 시험 기능입니다. 모델이 정류장(`transit_stop`)으로 감지한 박스가
화면 하단 또는 좌우 가장자리에서 충분히 크고 검출 신뢰도가 0.30 이상일 때, 최근 2초 안에
같은 정류장을 3회 이상, 최소 0.4초에 걸쳐 관측하면 **정류장 근접 추정**으로 확정합니다.
중간의 미검출·카메라 흔들림에는 후보를 최대 1초, 확정된 근접 상태를 최대 3초 유지합니다.
유지 중에는 **재확인 중**을 표시하며 지난 프레임의 정류장 박스를 현재 화면에 그리지 않습니다.
유지 시간이 지나면 현재 근접 여부는 다시 확인하지만, 세션의 근접 확인 기록은 남습니다.

결과는 `results.jsonl`의 `stop_proximity`에도 기록합니다. 첫 근접 확정 때만
`arrival_event: {type: "stop_arrival", event_id: 1, ...}`가 발생하고 이후에는 `null`입니다.
버스 번호 입력은 `(session_id, arrival_event_id)`를 기준으로 한 번만 시작합니다.
`arrival_recorded`는 세션에서 근접을 확인한 기록이며 현재도 근처에 있다는 뜻은 아닙니다.
새 테스트 세션에서 이 기록이 초기화됩니다. 근접 도착 확인 후 멈춤 음원 재생이 끝나면
**탑승할 버스 번호** 입력 화면을 엽니다. `7016`, `N26`, `마을버스 021`처럼 문자로 입력할 수 있고,
**이전**으로 질문을 종료할 수 있습니다. 취소·제출 후에는 같은 도착으로 자동 재질문하지
않으며 보행 화면의 **정류장 도착** 또는 버스 탐색 화면의 **이전** 버튼으로 다시 열 수 있습니다. 횡단 중에는 도착 질문을 시작하지 않습니다.

멈춤 음원 재생 완료를 서버에 확인한 뒤부터 번호 제출·취소까지 사용자가 정지한 것으로 가정합니다.
이 기간에는 `transit_stop`의 장애물 위험과 과거 경보 기억을 제외하지만 검출·근접 상태는 유지합니다.
다른 사람·차량·노면 위험은 계속 평가하며, 입력 중에는 이동 방향 대신 멈춤 행동으로 안내합니다.
제출·취소하면 정류장의 일반 위험 판단도 다시 적용합니다. 사용자가 재입력을 선택해도 멈춤 안내부터 시작합니다.
실제 거리나 버스·택시 정류장 구분, 구조물과의 충돌 안전은 확인할 수 없습니다. 근접 판정은
자체로 충돌 경고를 해제하지 않으며, 정지 가정은 위 입력 대기 상태에만 적용합니다.
근접 기준은 `configs/inference.yaml`의 `stop_proximity`에서 조정합니다.

보행 안내에는 유효한 예측 위험과 객체 없는 노면 경고도 연결합니다. 이동 후보는 위험·주의 객체와
근거리 보행 가능 노면으로 검증하고, 확인되지 않은 후보는 안내하지 않습니다. 잠깐 미검출된
장애물은 유지하며 일반 방향 변경·직진 복귀에 확인 시간을 둡니다. 긴급 멈춤은 즉시 적용합니다.
관련 시간은 `configs/audio.yaml`의 `walking_change_confirm_ms`, `walking_release_confirm_ms`,
`walking_missing_hold_ms`에서 조정합니다. 보행 행동 음원 파일과 문구는 기존 버전을 유지합니다.

화면을 켠 상태에서 사용해야 합니다. 마이크 소리는 녹음하지 않으며, 안내 음성은 결과 영상에
포함됩니다. 버스 번호 질문은 브라우저의 한국어 음성 합성을 사용하므로 오버레이 녹화의 MP3
오디오 트랙에는 포함되지 않습니다. 한국어 합성을 지원하지 않으면 입력 화면으로 안내합니다.
터널 주소는 실행할 때마다 달라지므로 외부에 공개하지 마세요.

`cloudflared`가 없다면 다음과 같이 설치합니다.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /tmp/cloudflared
chmod +x /tmp/cloudflared
sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
```

서버 주소와 포트의 기본값은 `configs/app.yaml`에 있습니다. 로컬에서만 바꾸려면 `.env`에
`APP_HOST`, `APP_PORT`를 지정합니다. `.env` 값이 YAML보다 우선합니다.

## 4. 결과 확인

### 저장 영상 결과

기본 저장 위치는 입력 샘플 폴더 이름을 따른 경로입니다.

```text
data/samples/input/sample1/input.mp4
                         ↓
data/samples/output/sample1/result_input.mp4
data/samples/output/sample1/jsonl/result_input.risk.jsonl
```

같은 결과 이름이 있으면 `(1)`, `(2)`를 붙여 기존 결과를 보존합니다. 추론에 실패하면 완성되지
않은 결과는 공개하지 않고 기존 결과를 유지합니다.

### 휴대폰 세션 결과

```text
data/sessions/YYYYMMDD/<기종명_촬영시각_테스트메모>/
├── camera.mp4             # 원본 카메라 영상
├── camera_overlay.mp4     # 오버레이와 안내 음성 포함
├── frames/
│   └── 000001.jpg         # 프레임별 추론 화면
├── results.jsonl          # 프레임별 추론 결과
├── client_timing.jsonl    # 촬영·응답·음성 시작 지연(브라우저 기록)
└── session.json           # 세션 정보
```

날짜는 한국 시간 기준이며 저장 위치는 `configs/paths.yaml`의 `session_dir`로 변경할 수 있습니다.
`results.jsonl`의 `risk_diagnostics`에는 객체별 위험 등급, 판정 이유, 화면 하단 좌표,
근접 경로 진입 예상 시간과 TTC가 기록됩니다. `server_timing`에는 JPEG 디코딩·서버 처리 시간이
밀리초로 기록됩니다. `client_timing.jsonl`의 `frame` 행에는 캡처 호출부터 JPEG 인코딩 완료까지(`capture_ms`),
요청 왕복(`round_trip_ms`), 결과 처리(`result_ms`)가, `overlay` 행에는 실제 박스 그리기까지의
시간(`overlay_delay_ms`)이 기록됩니다. `audio` 행에는 음성 상태와 JPEG 인코딩 완료 후 실제 재생 시작까지의
시간(`audio_delay_ms`)과 보행 안내 행동(`action`)이 기록됩니다. 브라우저와 서버 시계의
절대 시각을 빼서 지연을 계산하지 않습니다.

## 5. 설정 파일

| 파일 | 역할 |
| --- | --- |
| `configs/paths.yaml` | 입력·결과·세션·가중치·화면·음원 경로 |
| `configs/inference.yaml` | 모델 실행·추적·위험 판단 설정 |
| `configs/app.yaml` | 서버·카메라·녹화·업로드·세션 설정 |
| `configs/audio.yaml` | 음성 합성·재생·안내 시간 설정 |
| `configs/bus.yaml` | 버스 OCR 모델 경로·처리 주기·GPS 도착 조회 설정 |

모든 상대 경로는 프로젝트 루트 기준입니다. 절대 경로도 사용할 수 있습니다. 설정을 변경한 뒤에는
서버를 다시 시작하고 휴대폰 화면을 새로고침하세요.

명령행 옵션은 YAML 설정보다 우선합니다.

## 6. 주요 폴더

| 경로 | 역할 |
| --- | --- |
| `backend/` | FastAPI, 세션 저장, 실시간 추론 연결 |
| `configs/` | 실행 설정 |
| `frontend/` | 휴대폰 화면, 카메라, 오버레이, 음성, 녹화 |
| `scripts/` | 서버·터널·영상 추론 실행 진입점 |
| `src/` | 모델 추론, 추적, 위험 판단, 시각화, 음성 합성 |
| `tests/` | Python·프론트엔드 회귀 테스트 |
| `docs/` | 설계와 실험 기록 |

## 7. 테스트

Python 테스트:

```bash
python -m pytest -q
```

프론트엔드 테스트:

```bash
node --test tests/frontend/test_*.js tests/frontend/test_*.cjs
```

자동 테스트는 코드 연결과 입출력 동작을 확인합니다. 실제 모델의 정확도와 GPU 처리 속도는 입력
영상과 가중치를 준비해 별도로 확인해야 합니다.

## 8. 문제 해결

| 문제 | 확인할 내용 |
| --- | --- |
| 샘플 폴더가 없음 | `data/samples/input/sample1/`을 만들고 MP4를 넣었는지 확인 |
| 샘플 MP4가 없음 | `--sample-dir`가 MP4가 직접 들어 있는 폴더인지 확인 |
| 모델 파일이 없음 | `configs/paths.yaml`의 경로와 실제 가중치 위치 확인 |
| Mask2Former 로딩 실패 | 설정·전처리 파일과 모든 가중치 조각이 있는지 확인 |
| CUDA를 사용할 수 없음 | NVIDIA 드라이버와 CUDA 지원 PyTorch 확인 또는 `--device cpu` 사용 |
| 모듈을 찾을 수 없음 | 프로젝트 루트에서 가상환경을 활성화했는지 확인 |
| 터널 연결 실패 | 서버 실행 여부와 `.env`·`configs/app.yaml`의 포트 확인 |

## 9. 상세 문서

| 문서 | 내용 |
| --- | --- |
| [신호등 이식 기록](docs/traffic-signal-port_260929.md) | 신호등 추적·대상 선택·검증 범위 |
| [위험 판단 MVP](docs/risk_mvp_260929.md) | 초기 위험 판단 기준과 실행 방법 |
| [ROI 및 위험 검토](docs/roi_and_risk_review_20260921.md) | ROI 성능 측정과 영상 검토 |
| [위험 판단 개선](docs/risk_revision_implementation_20260921.md) | ROI 확장과 위험 해제 기준 |
| [공통 ROI 설계](docs/risk_shared_profile_20260921.md) | 공통 진행 경로와 측면 위험 판단 |
