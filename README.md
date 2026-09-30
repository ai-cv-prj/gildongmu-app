<!-- file_path: README.md
Mask2Former·YOLO 통합 추론의 준비, 실행 및 검증 방법 안내.
-->

# gildongmu-integration

보행 영역을 찾는 **Mask2Former**, 장애물 **YOLO**, 보행자 신호등 **YOLO + MobileNetV3-Small**의
결과를 같은 프레임에 표시하는 프로젝트입니다. 저장된 영상 추론과 휴대폰 카메라 실시간 테스트를 지원합니다.

| 탐지 결과 | 영상 표시 |
| --- | --- |
| 보행가능 영역 | 반투명 초록색 |
| 횡단보도 | 반투명 핑크색 |
| 보행불가 영역 | 색칠하지 않음 |
| YOLO 객체 | 클래스별 고정 색상의 박스 + 같은 색의 영문 이름·신뢰도 |
| 미선택 보행자 신호등 | 파란 박스 + UNSELECTED + 검출 신뢰도 |
| 연결 확인 중인 신호등 | 노란 박스 + CANDIDATE + 검출 신뢰도; 색상 보류 |
| 선택된 보행자 신호등 | 굵은 빨강·초록·회색 박스 + TARGET + 색상 분류 신뢰도·대상 번호 |
| 신호등 YOLO의 횡단보도 | 보라 박스 + 신뢰도·탈락 사유, 연결 판단에 사용한 박스는 청록색 |

영상 추론 결과 MP4에는 보행 장애물 위험과 신호등 안내 음성을 저장합니다.
휴대폰 실시간 테스트에서는 PC 서버가 같은 모델을 실행하고 휴대폰 브라우저가 카메라·오버레이·음성을 담당합니다.
파인튜닝, BEV와 실제 거리 추정은 포함하지 않습니다. 휴대폰 실시간 테스트에서는 횡단보도
가장자리 접근과 측면 이탈에 진동을 사용합니다.
2026-09-21부터 실험용 장애물 위험 판단(ROI·추적·측방 진입)과 JSONL 기록을 지원합니다.
초기값과 수식, 실행 방법은 [위험 판단 MVP](docs/risk_mvp_260929.md)를 참고하세요.
현재 설정에서 활성화되어 있으며 `--no-risk`로 기존 탐지 표시만 사용할 수 있습니다.
위험 표시는 채운 ROI·카드 라벨·하단 요약 패널로 그립니다. `risk.review_overlay: false`면 간략 표시로 바뀝니다.
신호등 색상과 횡단보도 연결은 추정 결과이며, 사용자의 실제 횡단 의도나 횡단 안전성을 보장하지 않습니다.
YOLO는 초록·핑크 영역에 한정하지 않고 전체 화면에서 객체를 탐지합니다.
색칠된 영역이 실제로 안전하다는 뜻은 아니며, 탐지 결과만으로 횡단·이동 여부를 판단하지 않습니다.

## 휴대폰에서 실시간 테스트

PC의 이 레포에서 두 터미널을 열어 순서대로 실행하세요. `.venv`가 없으면 `run.sh`가 만들고
패키지를 설치합니다. 기존 `.venv`에는 웹 서버 패키지만 부족할 때 설치합니다.

```bash
# 터미널 1: PC 추론 서버
./scripts/run.sh

# 터미널 2: 휴대폰용 HTTPS 주소
./scripts/tunnel.sh
```

두 번째 터미널에 나오는 `https://...trycloudflare.com` 주소를 휴대폰 브라우저에서 여세요.
휴대폰 기종 선택 → **카메라 켜기** → **테스트 시작** 순서입니다. 신호등과 보행 위험을 한 화면에서
함께 확인합니다. 버스 전용 기능과 가중치 선택 메뉴는 없습니다. 장애물 모델의 버스 검출은
보행 위험 판단의 일부로 유지됩니다. 화면을 켠 상태에서 테스트하세요.

첫 시작에는 세 모델을 로딩하므로 시간이 걸릴 수 있습니다. 가중치는
`configs/inference.yaml`에 정해진 경로에서 읽으며, 영상 추론과 동일한 파일을 사용합니다.
신호 음성은 테스트앱의 3프레임·400ms 확정과 2초 소실 안내를 사용하며, 세 모델의 처리 시간을
감안해 프레임 연속성 허용 간격만 2초로 늘렸습니다. 처리·전송이 1.5초 이상 걸린 오래된 프레임은
안내하지 않습니다.
마이크 권한은 요청하지 않습니다. 테스트를 시작하면 보행 장애물·신호등 안내가 자동으로 켜지며,
시작 안내 음성은 재생하지 않습니다.
테스트를 종료하면 카메라와 함께 꺼집니다.

횡단보도가 보이는 것만으로는 알리지 않습니다. 가까운 횡단보도 검출과 핑크 마스크 안의 사용자
위치가 0.5초 확인된 뒤에만 `횡단 중`으로 전환합니다. 좌우 경계 접근은 짧은 진동 1회이고,
경계를 0.25초 벗어나면 Ava 여성 음성으로 “위험! 횡단보도 이탈! 오른쪽/왼쪽으로 이동하세요!”와
강한 진동을 복귀할 때까지 반복합니다. 짧은 카메라·경계 불확실 상태에서는 마지막 이탈 안내를
유지하고, 복귀 또는 반대편 보행가능영역 도착이 확인되면 중단합니다. 현재 임계값은 영상 좌표 기반
초기값이므로 실제 촬영 영상으로 조정해야 합니다.

| 우선순위 | 관련 태그 | 재생 정책 |
| --- | --- | --- |
| 1 | 횡단보도 이탈 | 현재 음성을 즉시 취소하고 복귀까지 Ava 음성·진동 반복 |
| 2 | 장애물 `danger` | 하위 신호 안내를 중단하며 같은 위험은 반복 억제 |
| 3 | 신호등 색상 변경 | 상위 경고가 없을 때 1회, 상위 경고 중이면 폐기 |
| 4 | 최초·재확인·소실 신호 | 상위 안내가 없을 때만 1회 |

브라우저에는 전역 음성 관리자가 하나만 있으며 음성을 대기열에 오래 쌓지 않습니다. 이탈 중 발생한
장애물·신호 안내는 이탈 반복 사이에 끼워 넣지 않고 폐기합니다.
테스트 결과는 한국 날짜 기준 `outputs/result_realtime/YYYYMMDD/<기종명_촬영시각_테스트메모>/`에 저장합니다.
테스트 메모가 없으면 폴더명에서 생략하고, 폴더명에 붙는 메모는 사용할 수 없는 문자를 `_`로 바꾼 뒤 40자까지만 사용합니다.
촬영시각은 테스트 시작 시각이며 `YYYYMMDD_HHMMSS` 형식입니다. 같은 시각에 시작한 테스트는 폴더명 뒤에 `_2`, `_3`을 붙입니다. 세션 ID는 내부 요청에만 사용합니다.
테스트 시작 버튼부터 종료까지의 카메라 원본은 오버레이·현장 소리 없이 10FPS `camera.mp4`에 저장합니다.
추론 결과는 `results.jsonl`에 기록하며, 추론용 JPEG는 저장하지 않습니다.
오버레이와 안내 음성이 포함된 10FPS `camera_overlay.mp4`도 항상 저장합니다. 마이크 소리는 녹음하지 않습니다.

`cloudflared`가 없으면 WSL/Ubuntu에서 설치하세요.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared
chmod +x /tmp/cloudflared
sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
```

기본 PC 포트는 8001입니다. 변경하려면 `.env.example`을 `.env`로 복사하고 `APP_PORT`를 바꾸세요.
두 스크립트가 같은 값을 읽습니다. 터널 주소는 실행할 때마다 달라집니다. 접속 주소를 아는 사람은
테스트 서버에 접근할 수 있으므로 주소를 공개하지 말고 테스트가 끝나면 두 터미널에서 `Ctrl+C`로 종료하세요.

기존 저장 영상 추론은 아래 명령을 그대로 사용합니다.

```bash
python -m scripts.run_video_inference --sample-dir data/samples/sample1
```

## 1. 실행 준비

### 가상환경과 패키지

아래 명령어는 Linux/WSL 기준입니다. 먼저 터미널에서 `gildongmu-integration` 폴더로 이동하세요.
Python 3.12가 설치되어 있어야 합니다.

```bash
# .venv가 없을 때만 생성
python3.12 -m venv .venv

# 터미널을 새로 열었다면 활성화
source .venv/bin/activate

# 버전 확인 및 패키지 설치
python --version
python -m pip install -r requirements.txt
```

이미 환경을 준비했다면 가상환경 활성화만 하면 됩니다.
OpenCV는 `opencv-python`만 사용합니다. 기존 `opencv-python-headless`가 있다면 먼저 제거하고 설치하세요.
두 패키지를 함께 설치하면 같은 `cv2` 모듈을 공유해 문제가 생길 수 있습니다.

GPU를 사용할 수 있는지 확인하려면:

```bash
python -c "import torch; print(torch.cuda.is_available())"
```

`True`면 PyTorch에서 CUDA를 사용할 수 있습니다. `False`면 기본 설정에서 CPU로 실행됩니다.
GPU를 쓰려면 NVIDIA 드라이버와 CUDA 지원 PyTorch 설치 구성을 확인해야 합니다.

### 가중치와 영상 배치

아래는 파일을 배치하는 예시입니다. 가중치·영상·결과 폴더는 Git에서 제외되어 있으므로
레포를 clone한 것만으로 준비되지 않습니다. 필요한 파일과 입출력 폴더는 구글 드라이브 '모델' 폴더에서 다운로드 받으세요.

```text
gildongmu-integration/
├── weights/
│   ├── walking/
│   │   ├── mask2former/
│   │   │   ├── config.json
│   │   │   ├── preprocessor_config.json
│   │   │   └── model.safetensors
│   │   └── yolo/
│   │       └── finetune_v2_exp02_stage2_best.pt
│   └── traffic/
│       ├── best_YOLO.pt
│       └── best_MobileNet.pt
├── data/
│   └── samples/
│       └── sample1/
│           └── OBS_260914_G24P_001.mp4
└── outputs/
    └── videos/
```

- **Mask2Former:** 학습 결과의 `model` 폴더 내용 전체를 넣습니다. 가중치 파일만 넣으면 안 됩니다.
  분할 저장된 모델이라면 모든 가중치 조각과 index 파일도 필요합니다.
  `non_walkable`, `walkable`, `crosswalk`의 3클래스 모델을 사용합니다.
- **YOLO:** 팀에서 파인튜닝한 `.pt` 파일을 넣습니다. 클래스 번호·이름이 팀의 32클래스 정의와 일치해야 합니다.
  신뢰할 수 있는 가중치만 사용하세요.
- **신호등 YOLO:** class 0 `pedestrian_signal`, class 1 `crosswalk`인 2클래스 검출기입니다.
  장애물 YOLO와는 별도 모델입니다.
- **색상 분류기:** 입력 224의 `mobilenet_v3_small` 체크포인트입니다. 체크포인트에 저장된
  `class_names` 순서를 사용하므로 `[green, red]`를 임의로 `[red, green]`으로 바꾸지 않습니다.
- **영상:** 폴더 일괄 처리는 MP4만 지원합니다. 하위 폴더까지 자동 탐색하지 않습니다.
- **출력:** `outputs/result_samples/sample1/`처럼 입력 샘플 폴더 이름으로 결과 폴더를 자동 생성합니다. 입력 영상 폴더는 미리 준비해야 합니다.

## 2. 영상 실행하기

모든 실행 명령어는 **통합 레포 루트에서, 가상환경을 활성화한 상태**로 실행합니다.
아래 예시의 영상명은 본인이 준비한 파일명에 맞추세요.

### sample1의 모든 영상 처리

```bash
python -m scripts.run_video_inference --sample-dir data/samples/sample1
```

현재 기본 설정은 `mode: all`이므로 보도·장애물·신호등을 함께 추론합니다.
지정 폴더 바로 아래의 MP4를 파일명 순서대로 처리합니다.
`sample2`를 쓰려면 `--sample-dir data/samples/sample2`로 바꾸면 됩니다.

주의: 옵션을 생략하면 YAML의 `sample_dir: data/samples`를 사용합니다.
영상이 `sample1` 안에 있다면 위처럼 하위 폴더를 지정하거나 YAML의 `sample_dir`를 바꿔야 합니다.

### 영상 하나만 처리할 때

같은 영상으로 재실행하면 기존 MP4와 JSONL을 새 결과로 교체합니다.

```bash
python -m scripts.run_video_inference \
  --video-path data/samples/sample1/OBS_260914_G24P_001.mp4
```

`--output-path`를 지정하면 해당 경로에 저장하고 기존 결과를 교체합니다.

### 도보·장애물을 따로 확인할 때

비교 결과가 서로 겹치지 않도록 출력 이름을 구분합니다.

```bash
# 도보 마스크만 표시
python -m scripts.run_video_inference --mode sidewalk \
  --video-path data/samples/sample1/OBS_260914_G24P_001.mp4 \
  --output-path outputs/result_samples/sample1/result_OBS_260914_G24P_001_sidewalk.mp4

# 장애물 박스만 표시
python -m scripts.run_video_inference --mode obstacle \
  --video-path data/samples/sample1/OBS_260914_G24P_001.mp4 \
  --output-path outputs/result_samples/sample1/result_OBS_260914_G24P_001_obstacle.mp4
```

단독 모드에서는 해당 모델만 로딩합니다. YAML은 아래의 전체 설정 구조를 유지하세요.

### 신호등 단독 또는 전체 통합 실행

```bash
# 신호등 검출 + 횡단보도 연결 + 색상 분류
python -m scripts.run_video_inference --mode traffic \
  --video-path data/samples/sample1/OBS_260914_G24P_001.mp4 \
  --output-path outputs/result_samples/sample1/result_signal.mp4

# 도보 + 장애물 + 신호등을 한 영상에 표시
python -m scripts.run_video_inference --mode all \
  --video-path data/samples/sample1/OBS_260914_G24P_001.mp4 \
  --output-path outputs/result_samples/sample1/result_all.mp4
```

신호등 가중치를 다른 곳에 두었다면 `--traffic-weights /경로/YOLO.pt`와
`--traffic-classifier-weights /경로/MobileNet.pt`로 지정합니다.
`all`이 기본 모드입니다. `both`를 지정하면 도보+장애물만 실행합니다.
`traffic` 모드에는 도보·장애물 가중치가 필요하지 않습니다.

신호등은 `gildongmu-test-app` SESAC-78(`b3e4707`)과 SESAC-94(`a31ed6f`)의
BoT-SORT 추적·대상 선택·가림 복원 정책을 반영했습니다.
중복 검출을 제거하고 낮은 신뢰도의 검출은 기존 객체 연결에만 사용합니다. 신호등이 하나면
임시 선택하고, 복수 검출 시 횡단보도로 재확인하며 확정 대상은 정상 추적 중 유지합니다.
신호등이 가려지면 촬영 시각 기준 최대 3초 동안 대상 ID를 보관하고, 명확히 다시 검출되면
기존 ID로 복원합니다. 가림 중 과거 박스·색상은 표시하지 않습니다. 모든 현재 검출 신호등에
객체 ID를 표시합니다. 단일 신호로 임시 선택한 대상의 ID가 끊겨도 현재 신호가 하나뿐이면
그 신호를 즉시 새 임시 대상으로 선택합니다. 횡단보도로 확정했거나 복수 후보 확인 중인
대상은 기존 3초 복원 정책을 유지합니다.

`requirements.txt`에 추가된 객체 매칭 의존성 `lap==0.5.13`을 설치해야 합니다.
기존 모델 환경에서는 `python -m pip install lap==0.5.13`로 추가할 수 있습니다.
신호등 안내는 테스트 앱과 같은 MP3 음원·문구를 사용합니다. 선택된 대상의 색을 3프레임·400ms
확인한 뒤 안내하며, 색상 전환·2초간 신호 미확인·복구 후 재안내·같은 색 반복 억제를 적용합니다.
첫 프레임부터 신호를 확인합니다. 모든 음성은 위 우선순위의 단일 시간축으로 합성해 겹치지 않습니다.
판단 기준·반환 필드·FPS 차이·검증 결과는 [신호등 이식 문서](docs/traffic-signal-port_260929.md)에 정리했습니다.

횡단보도 선택은 **신호등 전용 YOLO의 crosswalk 박스**를 사용합니다.
신호등 모듈의 횡단보도 박스·라벨, 소실점·연결선, 횡단보도 진단 패널은 영상에 표시하지 않습니다.
Mask2Former의 핑크 마스크는 화면에 함께 표시하지만 현재 신호등 연결의 입력은 아닙니다.
신호등 모듈이 켜져 있을 때 장애물 모델의 일반 `traffic_light` 박스는 표시에서 제외하여
선택된 보행자 신호등 표시와 혼동되지 않게 합니다. 다른 장애물 박스는 그대로 표시합니다.
영상마다 선택 이력은 초기화하며 모델 가중치는 한 번만 로딩합니다.

2026-09-22 신호등 검출 가중치를 테스트 앱의
`backend/models/traffic/best_YOLO_v2.pt`로 교체했습니다. 통합 프로젝트에서는 기존 설정 경로인
`weights/traffic/best_YOLO.pt`를 유지하며 파일 내용은 **v2**입니다. 이전 v1 파일은 대체되어
통합 프로젝트에 남아 있지 않습니다. 검출 가중치의 SHA-256은
`d530a19b53570988ded114defdbd77af51d0ad06228cad38b7464c9758da42aa`입니다.
색상 분류기 `weights/traffic/best_MobileNet.pt`는 기존 파일을 사용합니다.
가중치는 Git 제외 대상이므로 다른 PC에서는 별도로 배치해야 합니다.

## 3. 결과 확인하기

기본 저장 위치는 `outputs/result_samples/샘플폴더명/`입니다.
예를 들어 `data/samples/sample5/test.mp4`는 `outputs/result_samples/sample5/result_test.mp4`와
`outputs/result_samples/sample5/result_test.risk.jsonl`로 저장됩니다.
`--output-path`를 지정하면 지정한 이름을 그대로 사용합니다.
같은 이름의 MP4나 위험 로그가 이미 있으면 기존 파일을 보존하고 `result_test(1).mp4`,
`result_test(1).risk.jsonl`처럼 다음 번호로 함께 저장합니다.

- 원본 영상 크기와 저장 FPS를 유지하며 MP4로 다시 인코딩합니다. 원본 오디오는 포함하지 않습니다.
- 위험 판정이 켜져 있고 보행 장애물이 `danger`이면 결과 MP4에 한국어 안내 음성이 들어갑니다. 왼쪽·가운데·오른쪽과 한 개·여러 개를 구분하며, 같은 위험을 매 프레임 반복하지 않습니다.
- 같은 방향에 여러 위험 장애물이 있으면 “왼쪽에 여러 장애물.”처럼, 여러 방향에 있으면 “여러 방향에 장애물.”이라고 안내합니다. 위험이 없으면 음성 트랙을 만들지 않습니다.
- `all` 모드에서는 횡단보도 이탈을 판단해 Ava 음성을 복귀까지 반복하고, 장애물·신호 음성과 우선순위에 따라 한 트랙으로 저장합니다.
- 진행 중에는 콘솔에 `영상 처리: 처리한 프레임 수/전체 프레임 수`가 표시됩니다.
- 완료되면 `결과 영상 저장: ...` 메시지가 나옵니다.
- 위험 기능을 켜면 MP4와 같은 폴더의 `.risk.jsonl`에 객체 좌표·선택적 ID·위험 등급·판단 사유·이벤트를 저장합니다. 안내가 시작된 프레임에는 `voice_text`와 `voice_clip`도 기록합니다.
- JSONL은 프레임별 위험 판정 근거를 검토하거나 이전 결과와 비교할 때 사용합니다. MP4 생성 과정에서 다시 읽지는 않습니다. 필요 없으면 `risk.log_jsonl: false`로 끌 수 있습니다.
- 출력 폴더가 없으면 만들고, 같은 이름의 결과가 있으면 추론 완료 후 MP4와 JSONL을 교체합니다. 추론이 실패하면 기존 결과를 유지합니다.
- `--no-risk`에서는 영상만 저장합니다. 성능 평가표나 새 모델 가중치는 생성하지 않습니다.
- 저장 FPS를 유지한다는 뜻이지, 그 속도로 실시간 추론한다는 뜻은 아닙니다. 별도 속도 측정이 필요합니다.

## 4. 설정 바꾸기

기본 설정 파일은 [configs/inference.yaml](configs/inference.yaml)입니다.

```yaml
sample_dir: data/samples
output_dir: outputs/result_samples
device: auto
overlay_alpha: 0.55
mode: all

mask2former:
  weights: weights/walking/mask2former

yolo:
  weights: weights/walking/yolo/finetune_v2_exp02_stage2_best.pt
  conf: 0.25
  imgsz: 640
  head: nms
  iou: 0.7
  max_det: 300
  rect: true

traffic:
  weights: weights/traffic/best_YOLO.pt
  classifier_weights: weights/traffic/best_MobileNet.pt
  conf: 0.25
  imgsz: 960
  crosswalk_min_confidence: 0.50
  classifier_min_confidence: 0.60
  association_stable_frames: 3
```

| 설정 | 의미 |
| --- | --- |
| `sample_dir` | 처리할 MP4가 직접 들어 있는 폴더 |
| `output_dir` | 샘플 입력 폴더별 결과 폴더의 상위 경로. 없으면 생성 |
| `device` | `auto`: CUDA 가능 시 GPU, 아니면 CPU / `cuda`: GPU 지정 / `cpu`: CPU 지정 |
| `mode` | `both`: 도보+장애물 / `all`: 전체 / `sidewalk`, `obstacle`, `traffic`: 각 단독 |
| `mask2former.weights` | 가중치와 모델·전처리 설정이 있는 **폴더** |
| `yolo.weights` | YOLO 가중치 **파일** |
| `overlay_alpha` | 마스크 색상 비율. `0.55`는 원본 45% + 색상 55%. 클수록 진하게 표시 |
| `yolo.conf` | 객체 신뢰도 기준. 높이면 더 엄격하게 걸러지지만 놓치는 객체가 늘 수 있음 |
| `yolo.imgsz` | YOLO 내부 전처리 크기 기준. 결과 영상의 크기를 바꾸는 값은 아님 |
| `yolo.head` | 현재는 `nms`만 지원. 겹치는 탐지 박스를 정리하는 후처리 사용 |
| `yolo.iou` / `yolo.max_det` / `yolo.rect` | 장애물 YOLO의 중복 판정 기준 / 최대 검출 수 / 입력 크기 정렬 여부 |
| `traffic.weights` / `traffic.classifier_weights` | 신호등 YOLO / MobileNet 가중치 파일 |
| `traffic.conf` / `traffic.imgsz` | 신호등 검출 기준 / YOLO 입력 크기 |
| `traffic.crosswalk_min_confidence` | 연결에 사용할 횡단보도 검출 기준 |
| `traffic.classifier_min_confidence` | 이 값보다 낮으면 색상을 `unknown` 처리 |
| `traffic.association_stable_frames` | 최초 복수 후보 선택·임시 대상 횡단보도 재확인에 필요한 연속 프레임 수. 기본 3, 1이면 첫 후보부터 사용 |
| `crosswalk_safety.entry_confirm_s` | 가까운 횡단보도 안에 있다고 확인할 연속 시간. 기본 0.50초 |
| `crosswalk_safety.edge_margin` / `exit_margin` | 가장자리 진동과 실제 측면 이탈을 구분하는 정규화 영상 여유값 |
| `crosswalk_safety.exit_confirm_s` / `return_confirm_s` | 이탈 확정과 안쪽 복귀 확정 시간. 기본 0.25초 / 0.40초 |
| `crosswalk_safety.roi_left/right/top/bottom` | 횡단보도 유지 판단과 보라색 표시에 사용하는 화면 하단 ROI. 기본 가로 10~90%, 세로 90~100% |
| `crosswalk_safety.roi_crosswalk_threshold` / `roi_exit_confirm_s` | ROI의 횡단보도 비율이 5% 미만인 상태를 이탈 후보로 확인하는 기준 / 확인 시간 0.30초 |
| `crosswalk_safety.roi_occlusion_threshold` | 객체가 ROI의 8% 이상을 가리면 ROI 손실만으로 이탈을 확정하지 않는 기준 |
| `crosswalk_safety.outside_finish_walkable_fraction` / `outside_finish_confirm_s` | 이탈 상태에서도 보행 가능 도착을 복구하는 비율 80% / 확인 시간 0.80초 |
| `crosswalk_safety.red_obstacle_voice_suppression` | 빨간불이고 객체 바닥이 횡단보도로 확인되면 위험 장애물 음성만 제외하는 기능 |
| `crosswalk_safety.max_boundary_shift` | 프레임 사이 경계가 이 값보다 크게 움직이면 이탈 음성을 보류하는 기준 |

`overlay_alpha`는 0~1 범위이며 탐지 성능이나 YOLO 박스에는 영향을 주지 않습니다.
Mask2Former의 전처리는 저장된 `preprocessor_config.json`을 사용합니다.
YOLO 신뢰도 0.25는 시작 설정이며, 실제 영상의 오탐·미탐을 확인해 조정해야 합니다.

안내 음원 폴더는 [configs/audio.yaml](configs/audio.yaml)의 `audio.voice_dir`에서 지정합니다.
상대 경로는 프로젝트 루트 기준이고 절대 경로도 사용할 수 있습니다. 영상 MP4 음성 합성과
실시간 FastAPI 앱이 이 값을 공통으로 읽으며, 변경 후에는 서버를 다시 시작해야 합니다.
브라우저는 실제 폴더 위치와 관계없이 `/audio/<파일명>.mp3` 주소로 음원을 요청합니다.

### 명령어 옵션으로 바꾸기

**명령어 옵션이 YAML보다 우선**합니다. 상대 경로는 모두 통합 레포 루트 기준이며 절대 경로도 가능합니다.
가중치를 생략하면 YAML에 지정된 것을 사용하고, 최신 가중치를 자동 선택하지는 않습니다.

```bash
python -m scripts.run_video_inference \
  --sample-dir data/samples/sample1 \
  --mask2former-weights weights/walking/mask2former \
  --yolo-weights weights/walking/yolo/finetune_v2_exp02_stage2_best.pt \
  --conf 0.25 --imgsz 640 --device cuda
```

| 옵션 | 용도 |
| --- | --- |
| `--config` | 다른 YAML 설정 파일 선택 |
| `--sample-dir` / `--video-path` | 폴더 전체 / 영상 한 개 선택. 둘 중 하나만 지정 |
| `--output-dir` / `--output-path` | 출력 폴더 / 결과 파일명 지정. 둘 중 하나만 지정 |
| `--mask2former-weights` / `--yolo-weights` | 모델별 가중치 경로 지정 |
| `--mode` | `both`, `sidewalk`, `obstacle`, `traffic`, `all` 선택 |
| `--traffic-weights` / `--traffic-classifier-weights` | 신호등 YOLO / MobileNet 경로 지정 |
| `--device` | `auto`, `cpu`, `cuda` 선택 |
| `--conf` / `--imgsz` | YOLO 신뢰도 / 전처리 크기 지정 |

`--output-path`는 처리할 영상이 한 개일 때만 사용할 수 있고 확장자는 `.mp4`여야 합니다.
`--conf`, `--imgsz`는 장애물 YOLO 옵션입니다. 신호등 임계값·입력 크기는 YAML의 `traffic` 항목에서 설정합니다.
기존 `--model-dir`는 `--mask2former-weights`의 별칭으로 사용할 수 있습니다.
이전 YAML의 최상위 `model_dir`는 `mask2former` 아래의 `weights`로 옮겨야 합니다.
전체 옵션은 `python -m scripts.run_video_inference --help`로 확인합니다.

## 5. 자주 발생하는 문제

| 메시지·상황 | 확인할 것 |
| --- | --- |
| 같은 영상 재실행 | 기존 MP4와 JSONL을 새 결과로 교체. 원본 영상을 `--output-path`로 지정할 수 없음 |
| 샘플 MP4가 없음 | `data/samples`가 아니라 실제 영상이 들어 있는 `data/samples/sample1`을 지정했는지 확인 |
| 출력 폴더가 없음 | `outputs/result_samples/샘플폴더명`을 자동 생성하는지 확인 |
| 모델 파일이 없음 | Mask2Former 설정 파일까지 모두 준비했는지, YOLO 파일명이 설정과 같은지 확인 |
| 3클래스·32클래스 오류 | 현재 코드에 맞는 팀 가중치인지 확인. 임의의 기본 모델은 사용할 수 없음 |
| CUDA를 사용할 수 없음 | GPU 환경 확인. CPU로 실행하려면 명령어에 `--device cpu` 추가 |
| `No module named ...` | 통합 레포 루트인지, 가상환경이 활성화됐는지, requirements 설치가 됐는지 확인 |
| 영상 프레임 수 불일치 | 입력 파일의 읽기 오류·메타데이터 확인. 이 경우 최종 결과는 저장하지 않음 |

### 실행 도중 중단하면?

- 처리 중에는 출력 폴더의 고유한 `.partial.mp4` 임시 파일에 저장하고, 완료 후 최종 파일명으로 등록합니다.
- 추론 오류나 `Ctrl+C` 발생 시 이번 작업의 임시 파일을 정리합니다. 영상 중간부터 이어서 처리하지는 않습니다.
- 여러 영상 중 앞서 완료한 결과는 유지됩니다. 실패한 영상은 `--video-path`로 개별 재실행하세요.
- 전체 프레임 수를 알 수 없으면 누락 여부를 검증할 수 없다는 경고 후 읽을 수 있는 프레임을 처리합니다.
- 강제 종료나 전원 종료 시에는 임시 파일이 남을 수 있습니다. 실행 중인 작업의 임시 파일은 건드리지 마세요.
- 새 MP4와 JSONL을 완성한 뒤 기존 결과와 교체합니다. 공개 중 오류가 나면 이전 결과를 복구합니다.

## 6. 코드 구성과 테스트

| 파일 | 역할 |
| --- | --- |
| [scripts/run_video_inference.py](scripts/run_video_inference.py) | 명령어 옵션을 받아 실행 시작 |
| [configs/inference.yaml](configs/inference.yaml) | 가중치·입출력 경로와 추론 설정 |
| [configs/audio.yaml](configs/audio.yaml) | 영상과 실시간 앱이 함께 읽는 안내 음원 폴더 경로 |
| [src/pipeline.py](src/pipeline.py) | 영상 읽기 → 모드별 모델 추론 → 결과 합성 → MP4 저장 |
| [src/sidewalk.py](src/sidewalk.py) | Mask2Former 로딩과 보행 영역 추론 |
| [src/obstacle.py](src/obstacle.py) | YOLO 로딩과 장애물 추론 |
| [src/risk.py](src/risk.py) | 예상보행경로(ROI)와 장애물 위험 등급·대표 경고 판정 |
| [src/walking_voice.py](src/walking_voice.py) | 위험 장애물의 방향·종류·개수에 따른 음성 선택과 반복 억제 |
| [src/traffic_voice.py](src/traffic_voice.py) | 테스트 앱의 신호등 음성 안정화·전환·소실 안내 |
| [src/crosswalk_safety.py](src/crosswalk_safety.py) | 횡단 진입·가장자리·측면 이탈·복귀·정상 도착 상태 판정 |
| [src/voice_priority.py](src/voice_priority.py) | 횡단보도·장애물·신호등 음성을 우선순위에 따라 단일 시간축으로 병합 |
| [src/video_audio.py](src/video_audio.py) | 안내 음원을 영상 시점에 배치하고 결과 MP4에 합성 |
| [src/ground_extent.py](src/ground_extent.py) | 보행가능영역에 따른 ROI 상단 보정과 가림 시 경계 유지 |
| [src/camera_view.py](src/camera_view.py) | 촬영 상태가 불확실할 때 위험 판정 표시 제한 |
| [src/hazard_labels.py](src/hazard_labels.py), [src/warning_summary.py](src/warning_summary.py) | 객체 이름 안정화와 대표 경고 선택 |
| [src/traffic.py](src/traffic.py) | 신호등 YOLO, MobileNet 로딩·선택 대상 색상 분류 |
| [src/traffic_association.py](src/traffic_association.py) | 중복 제거·횡단보도 연결·임시 대상 재확인·확정 대상 유지 |
| [src/traffic_tracker.py](src/traffic_tracker.py) | 영상별 BoT-SORT ID·낮은 신뢰도 연결·3초 가림 복원 |
| [src/traffic_geometry.py](src/traffic_geometry.py) | 도색 줄무늬 경계 기반 횡단보도 방향 추정 |
| [src/traffic_motion.py](src/traffic_motion.py) | 카메라 이동 검증과 이전 박스 좌표 보정 |
| [src/visualization.py](src/visualization.py) | 반투명 마스크와 클래스별 색상의 객체 박스·글자 표시 |
| [tests/test_integration.py](tests/test_integration.py) | 설정·옵션·좌표·클래스별 색상·영상 저장 동작 검증. 일반 추론 실행에는 사용하지 않음 |
| [tests/test_walking_risk_sync.py](tests/test_walking_risk_sync.py) | 넓은 ROI, 측면 위험, 촬영 상태와 보행불가 영역 경고 검증 |
| [tests/test_walking_voice.py](tests/test_walking_voice.py) | 방향·다중 위험 안내와 MP4 오디오 저장 검증 |
| [tests/test_traffic_voice.py](tests/test_traffic_voice.py) | 신호등 안내 규칙과 MP4 오디오 저장 검증 |
| [tests/test_traffic.py](tests/test_traffic.py) | 단일·복수 신호등 선택, 색상 전처리, unknown, 모드 호환성 검증 |
| [tests/test_traffic_tracking.py](tests/test_traffic_tracking.py) | 대상 전환·재확인·색상 보류·흔들림·방향·표시 검증 |
| [tests/test_traffic_botsort.py](tests/test_traffic_botsort.py) | 실제 BoT-SORT ID·낮은 신뢰도 연결·중복 제거·영상별 초기화 검증 |
| [docs/traffic-signal-port_260929.md](docs/traffic-signal-port_260929.md) | 테스트 앱 이식 내역·설정 비교·검증 범위·후속 과제 |
| [backend/](backend/) | 휴대폰 세션 API와 같은 프레임의 통합 추론 |
| [frontend/](frontend/) | 휴대폰 카메라·보행/신호 오버레이·전역 음성 우선순위·진동·녹화 UI |
| [scripts/run.sh](scripts/run.sh), [scripts/tunnel.sh](scripts/tunnel.sh) | PC 서버 실행과 휴대폰용 HTTPS 주소 생성 |

각 모델은 한 번만 로딩하고 모든 영상에서 재사용합니다.
프레임마다 활성화한 모델을 순서대로 실행하며, 모두 색칠 전의 같은 원본을 입력받습니다.
원본 도보·장애물 레포의 코드나 학습 데이터는 실행에 필요하지 않습니다.

개발 시 결과를 연결하는 기준:

- 공통 입력: OpenCV BGR 이미지 `(높이, 너비, 3)`.
- `SidewalkSegmenter.predict(frame)`: 원본 크기의 정수 클래스 지도 반환. 번호는 `label_ids`로 조회.
- `ObstacleDetector.predict(frame)`: `xyxy`, `class_id`, `class_name`, `confidence`를 담은 목록 반환. 탐지 객체가 없으면 빈 목록.
- `TrafficSignalPipeline.predict(frame, frame_id=..., captured_at_ms=...)`: 필터를 통과한 신호등의
  `detections`, 횡단보도 후보 `crosswalks`, 선택 상태·추적 근거 `association`,
  선택/후보 인덱스와 횡단보도 진단을 반환합니다. 최종 선택 대상만 색상을 분류하며
  색상은 `red`, `green`, `unknown`입니다. `reset()`은 다음 영상의 첫 프레임 전에 호출합니다.
- `xyxy`는 원본 픽셀 기준 `[왼쪽, 위, 오른쪽, 아래]`. 다시 크기 비율을 곱하지 않습니다.

자동 테스트 실행:

```bash
python -B -m unittest discover -s tests -p 'test_*.py' -v
```

대부분은 실제 가중치 없이 검사하며, 테스트용 임시 영상은 정리합니다.
이 테스트는 코드 연결과 입출력 동작을 확인하는 것으로, **실제 탐지 정확도나 실시간 속도를 보장하지 않습니다.**
실제 가중치를 사용하는 통합 추론은 별도로 실행하고, 같은 입력에 대한 단독·통합 결과를 비교해야 합니다.

2026-09-22 SESAC-78 반영 후 **87개 자동 테스트 통과**. BoT-SORT의 낮은 신뢰도 연결,
중복 제거, 임시 대상 재확인·확정 대상 유지, 미검출 즉시 해제, 영상별 ID 격리와
기존 도보·장애물·영상 출력 경로를 검증했습니다.

2026-09-29 SESAC-94의 신호등 가림 정책을 추가 이식했습니다. 현재 검출이 사라진 동안에는
박스·색상을 만들지 않고 대상 ID만 최대 3초 보관하며, 복귀 위치가 명확할 때만 기존 ID를
복원합니다. 신호등 회귀 테스트 62개와 전체 자동 테스트 168개가 통과했습니다.
위 2026-09-22 기록의 즉시 해제 정책은 이 변경 이전의 검증 기록입니다.

2026-09-29 SESAC-105에서 단일 임시 대상의 추적 ID가 끊겼을 때 현재 추적 가능한 신호가
하나뿐이면 새 ID를 즉시 선택하도록 보완했습니다. 횡단보도로 확정했거나 복수 후보 확인 중인
대상은 기존 3초 복원 정책을 유지합니다. 같은 실촬영 211프레임을 다시 추론한 결과
`단일 신호 검출·대상 미선택`이 44프레임에서 0프레임으로 줄었고, 전체 168개 테스트가 통과했습니다.

최신 `dev`의 장애물 위험 판단 변경을 반영한 PR 최종 상태에서는 **전체 154개 테스트가 통과**했습니다.
공용 객체 매칭 의존성 `lap`은 중복 버전 지정을 제거하고 0.5.13으로 통일했습니다.

같은 날 저장된 실촬영 211프레임을 실제 YOLO v2·MobileNet으로 CPU 재추론했습니다.
green 83·red 62·미검출 unknown 66프레임이며, 중복 박스 2건과 연결되지 않은 낮은 신뢰도
박스 18건을 제거했습니다. 45초 확인용 영상은 `outputs/traffic-test-20260922/traffic-preview.mp4`에
저장하고 전체 디코딩을 확인했습니다. 산출물은 로컬 전용이며 Git에 포함되지 않습니다.
복수 신호등·낮은 신뢰도의 대상 유지 장면은 이 촬영에 없어 자동 테스트로 확인했습니다.
정답 정확도 평가는 아니며 Python 3.14.4의 기존 환경을 사용했습니다.
입력·집계·재실행 방법과 검증 한계는 [신호등 이식 문서](docs/traffic-signal-port_260929.md)에 기록했습니다.

아래는 이번 정책 변경 이전의 검증 기록입니다. 기존 49개 + 추가 회귀 15개, 총 64개 통과.
실촬영 170프레임의 저장 검출을 재입력해 테스트 앱과 판단 결과가 모두 일치했고,
실제 v2 모델의 3프레임 추론·표시와 교체된 로컬 가중치 로딩을 확인했습니다.
검증 범위와 사용 환경은 [신호등 이식 문서](docs/traffic-signal-port_260929.md)에 기록했습니다.

이전 2026-09-19 연동 검증: 기존 32개 + 신호등 17개, 총 49개 자동 테스트 통과.
기존 학습 환경의 Python 3.14 / PyTorch 2.11.0+cu128과 별도 임시 설치한
Transformers 5.17.0으로 CPU 검증했습니다. 위 Python 3.12 기준 고정 의존성 전체를
새 가상환경에 설치한 검증은 수행하지 않았습니다.
실제 폰 테스트의 81~83번 이미지에서 신호등 추론과 3프레임 MP4 저장을 확인했습니다.
81번은 신호등 미검출, 82·83번은 1개 검출 및 green 분류였습니다.
따라서 이 샘플로 복수 신호등 연결의 실제 정확도를 확인한 것은 아닙니다.
도보·장애물 가중치가 현재 통합 폴더에 없어 세 파트 전체의 실제 추론은 아직 미검증입니다.

## 7. 진행 상황과 남은 문제 (2026-09-22)

### 완료한 작업

| 항목 | 현재 상태 |
| --- | --- |
| 신호등 파이프라인 연동 | 신호등 전용 YOLO → 횡단보도 연결 → MobileNetV3-Small 색상 분류 연결 |
| 대상 선택 | 1개 검출 시 즉시 분류, 2개 이상이면 횡단보도 소실점의 수평 거리와 신호등 크기로 비교 |
| 객체 추적 | 영상별 BoT-SORT, 낮은 신뢰도는 기존 객체 연결에만 사용, IoU 0.60 중복 제거 |
| 선택 안정화 | 단일 검출 대상은 복수 검출 시 횡단보도로 재확인; 확정 대상은 정상 추적 중 유지. 신호등 가림은 최대 3초 복원 대기 |
| 실행 모드 | traffic 단독 및 all 통합 추가, 기존 both·sidewalk·obstacle 유지 |
| 영상 표시 | 전체 신호등의 미선택·후보·최종 대상 구분 및 색상 표시. 횡단보도 검출·연결 진단은 숨김 |
| 구조 확인 | scripts 실행 진입점, src 추론·표시, configs 설정, tests 검증 구조 유지 |
| 입출력 규칙 | 원본 BGR·원본 픽셀 좌표, 모델 1회 로딩, 영상별 선택 이력 초기화, 완료 후 결과 교체 |
| 검증 | 최신 dev 반영 후 전체 154개 테스트 통과, 실제 v2·MobileNet으로 실촬영 211프레임 재추론 및 45초 확인용 영상 검증 |
| 로컬 가중치 | 기존 경로의 v1을 v2로 교체, 원본 v2와 해시 일치. 가중치 파일은 Git 제외 |

### 미해결 문제: 일반 신호등 박스가 숨겨지는 동작

현재 all 모드에서는 신호등 파이프라인이 실행되면 장애물 모델의 traffic_light 결과를
표시 목록에서 모두 제외합니다. 같은 신호등에 두 모델의 박스가 겹치는 것을 막으려는 처리지만,
신호등 전용 모델이 검출·선택에 실패한 경우에도 적용됩니다.

| 장애물 모델 결과 | 신호등 전용 모델 결과 | 현재 all 화면 |
| --- | --- | --- |
| traffic_light 검출 | 대상 선택 및 색상 분류 성공 | 보행자 신호등 전체 표시, 최종 대상만 색상 분류 표시 |
| traffic_light 검출 | 신호등 미검출 | 신호등 박스가 표시되지 않음 |
| traffic_light 검출 | 복수 후보가 모호해 대상 없음 | 일반 신호등 박스는 숨김, 보행자 신호등 후보와 선택 보류 사유 표시 |

이는 검출 모델의 출력을 바꾸는 것이 아니라 src/pipeline.py에서 표시 전에 걸러내는 동작입니다.
차량용 신호등도 traffic_light이면 숨겨지며, 사람·자동차 등 다른 클래스에는 영향을 주지 않습니다.
신호등 모듈이 꺼진 기존 both·obstacle 모드는 영향을 받지 않습니다.
위 표는 코드의 조건별 동작을 설명한 것으로, 같은 실제 프레임에서 장애물 모델만 검출에
성공했다고 확인한 결과는 아닙니다. 이 표시 정책은 현재 유지한 상태이며 수정하지 않았습니다.

### 후속 확인 사항

1. 일반 traffic_light 박스를 유지할지, 표시 여부를 설정으로 분리할지 결정합니다.
   함께 표시한다면 일반 객체 박스와 선택된 보행자 신호등의 색상 결과를 구분해야 합니다.
2. 도보·장애물 가중치를 준비하고 같은 영상으로 각 단독 모드와 all 모드를 비교합니다.
3. 보행자 신호등이 여러 개 검출되는 실제 영상에 정답 대상을 표시하고 선택 정확도를 확인합니다.
   저장 검출 170프레임의 판단 일치는 이식 일관성 검사이며 정답 정확도 검증을 대신하지 않습니다.
4. 프로젝트 기준인 Python 3.12와 requirements.txt의 고정 의존성으로 실행을 검증합니다.
   현재 자동 테스트와 신호등 추론은 앞 절에 기록한 별도 환경에서 수행했습니다.

5. 휴대폰 실시간 신호 음성은 연속 3프레임·최소 400ms를 확인합니다.
   모델과 네트워크 처리 속도에 따라 실제 안내 시작 시각은 달라집니다.
6. 영상 신호등 음성은 테스트 앱의 3프레임·400ms 확인, 2초 소실 안내,
   같은 색 반복 억제·복구 후 재안내와 순차 재생 정책을 적용합니다.

요약: 가상환경 활성화 → 가중치·영상 준비 → 샘플 폴더 선택 → 실행 → outputs/result_samples/샘플폴더명 결과 확인.

## 8. 장애물 위험 판단 MVP (2026-09-21)

[초기 ROI 좌표·임계값·수식·검증 결과](docs/risk_mvp_260929.md)를 참고하세요.
전체 화면 탐지를 유지하며 신호등 탐지·선택·색상 분류·표시 부분은 변경하지 않았습니다.

2026-09-21 전체 화면·ROI 자르기 실측과 8개 영상 검토는
[ROI 비교 및 위험 검토 기록](docs/roi_and_risk_review_20260921.md)을 참고하세요.
당시 별도 검토 설정에서 TTC 위험 반영과 큰 ROI·등급 표시를 켰습니다. 현재 설정은 둘 다 기본으로 켭니다.
거리(m)·접근 속도(m/s)는 아직 추정하지 않습니다. 자동 테스트 72개가 통과했습니다.

### 위험 결과 피드백 반영

하단 ROI 확장, 조건부 보도 방향, 정적 장애물 근접 구간, 경고 해제 확인을 반영했습니다.
[실제 구현·초기값·후속 검증 사항](docs/risk_revision_implementation_20260921.md)을 참고하세요.
자동 테스트 87개가 통과했습니다. 화면 이탈/관측 소실은 실제 신체 주변의 안전 확인을 뜻하지 않습니다.
새 결과와 좌우 비교 영상은 실행한 로컬 환경의 `outputs/experiments/` 아래에 생성합니다.

### 공통 ROI와 보도 기반 경고 (3차)

개인별 신체 치수와 지면 높이 변화는 제외하고, 공통 진행 ROI·측면 근접·보도 기반 경고를 추가했습니다.
[구현 기준과 초기값](docs/risk_shared_profile_20260921.md)을 참고하세요. 자동 테스트 116개가 통과했습니다.
강하게 겹친 같은 클래스의 경고는 한 알림 단위로 표시하되, 모든 탐지·개별 위험 등급·감사 이벤트는 유지합니다.
기존 MP4 8개와 sample2의 MP4 2개를 처리하며 MOV는 제외합니다.
결과 영상과 회차별 안내는 `outputs/` 아래에 생성하며, 용량 때문에 저장소에 포함하지 않습니다.
저장소에서 확인할 수 있는 근거는 위 docs 문서들이고, 영상은 직접 실행해 재현합니다.
