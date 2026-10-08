"""
file_path: src/settings.py

경로·앱·음성 YAML을 읽고 실행 전에 설정 형식과 범위를 검증한다.
모델을 불러오지 않으므로 서버 실행 스크립트에서도 사용할 수 있다.
"""

import math
import os
from copy import deepcopy
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PATHS_CONFIG = PROJECT_DIR / "configs/paths.yaml"
DEFAULT_APP_CONFIG = PROJECT_DIR / "configs/app.yaml"
DEFAULT_AUDIO_CONFIG = PROJECT_DIR / "configs/audio.yaml"


# 프로젝트 기준 경로 해석
def resolve_path(value):
    """상대 경로와 홈 경로를 프로젝트 루트 기준 절대 경로로 변환한다."""
    return (PROJECT_DIR / Path(value).expanduser()).resolve()


# YAML 문서 읽기
def read_yaml(config_path):
    """최상위가 매핑인 YAML 문서를 읽고 잘못된 형식은 파일명과 함께 알린다."""
    path = resolve_path(config_path)
    with path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError(f"{path.name}: 설정은 키와 값으로 작성하세요.")
    return config


# 필수 설정 묶음 확인
def section(config, key):
    """누락되거나 매핑이 아닌 설정 묶음을 거부한다."""
    value = config.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key}: 설정 묶음이 필요합니다.")
    return value


# 숫자 설정 검증
def number(config, key, minimum, maximum, integer=False):
    """불리언·비유한 수·허용 범위 밖의 숫자를 설정명과 함께 거부한다."""
    value = config.get(key)
    valid_type = type(value) is int if integer else type(value) in (int, float)
    if not valid_type or not math.isfinite(value) or not minimum <= value <= maximum:
        kind = "정수" if integer else "숫자"
        raise ValueError(f"{key}: {minimum}~{maximum} 범위의 {kind}를 지정하세요.")
    return value


# 경로 설정 읽기
def load_paths(config_path=DEFAULT_PATHS_CONFIG):
    """필수 경로의 빈 값과 형식을 확인하고 원래 경로 문자열을 반환한다."""
    config = read_yaml(config_path)
    required = ("sample_dir", "output_dir", "session_dir", "frontend_dir", "voice_dir",
                "mask2former_weights", "yolo_weights", "traffic_weights", "traffic_classifier_weights")
    for key in required:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"{key}: 비어 있지 않은 경로를 지정하세요.")
    return config


# 앱 설정 읽기
def load_app_config(config_path=DEFAULT_APP_CONFIG):
    """카메라·녹화·업로드 범위와 세션 시간대를 검증한다."""
    config = read_yaml(config_path)
    server = section(config, "server")
    if not isinstance(server.get("host"), str) or not server["host"].strip():
        raise ValueError("server.host: 비어 있지 않은 주소를 지정하세요.")
    number(server, "port", 1, 65535, integer=True)
    camera = section(config, "camera")
    if camera.get("facing_mode") not in ("environment", "user"):
        raise ValueError("camera.facing_mode: environment 또는 user를 지정하세요.")
    for key in ("width", "height", "capture_max_side"):
        number(camera, key, 1, 16384, integer=True)
    number(camera, "jpeg_quality", 0, 1)
    number(camera, "bus_capture_max_side", 1, 16384, integer=True)
    number(camera, "bus_jpeg_quality", 0, 1)
    number(camera, "bus_capture_interval_ms", 100, 5000, integer=True)
    if type(camera.get("bus_led_exposure_enabled")) is not bool:
        raise ValueError("camera.bus_led_exposure_enabled: true 또는 false를 지정하세요.")
    number(camera, "bus_led_exposure_time_us", 1000, 33334, integer=True)
    number(camera, "encoder_timeout_ms", 1, 300000, integer=True)
    number(camera, "resume_delay_ms", 0, 60000, integer=True)
    recording = section(config, "recording")
    number(recording, "fps", 1, 120)
    number(recording, "max_side", 2, 16384, integer=True)
    number(recording, "video_bits_per_second", 1, 1000000000, integer=True)
    number(recording, "chunk_interval_ms", 1, 60000, integer=True)
    if recording.get("preset") not in (
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
    ):
        raise ValueError("recording.preset: 지원하는 libx264 프리셋을 지정하세요.")
    upload = section(config, "upload")
    for key in ("max_jpeg_bytes", "max_recording_bytes"):
        number(upload, key, 1, 10000000000, integer=True)
    for key in ("min_frame_side", "max_frame_side"):
        number(upload, key, 1, 16384, integer=True)
    if upload["min_frame_side"] > upload["max_frame_side"]:
        raise ValueError("upload.min_frame_side는 max_frame_side 이하여야 합니다.")
    if not upload["min_frame_side"] <= camera["capture_max_side"] <= upload["max_frame_side"]:
        raise ValueError("camera.capture_max_side는 업로드 허용 크기 범위여야 합니다.")
    if not upload["min_frame_side"] <= camera["bus_capture_max_side"] <= upload["max_frame_side"]:
        raise ValueError("camera.bus_capture_max_side는 업로드 허용 크기 범위여야 합니다.")
    session = section(config, "session")
    number(session, "folder_note_max_length", 1, 500, integer=True)
    number(session, "stale_after_s", 10, 3600)
    try:
        ZoneInfo(session["timezone"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("session.timezone: 유효한 시간대를 지정하세요.") from error
    return config


# 음성 설정 읽기
def load_audio_settings(config_path=DEFAULT_AUDIO_CONFIG):
    """음성 합성과 재생·관측 시간의 단위와 범위를 확인한다."""
    audio = section(read_yaml(config_path), "audio")
    number(audio, "sample_rate_hz", 8000, 192000, integer=True)
    number(audio, "bitrate_kbps", 8, 512, integer=True)
    number(audio, "volume", 0, 1)
    number(audio, "playback_rate", 0.5, 2)
    for key in ("default_validity_ms", "playback_timeout_ms", "tick_ms", "crosswalk_max_age_ms",
                "video_max_gap_ms", "realtime_max_gap_ms"):
        number(audio, key, 1, 300000, integer=True)
    guidance = section(audio, "guidance")
    if number(guidance, "traffic_crosswalk_roi_min_fraction", 0, 1) <= 0:
        raise ValueError("traffic_crosswalk_roi_min_fraction은 0보다 커야 합니다.")
    number(guidance, "stable_frames", 1, 1000, integer=True)
    for key in ("stable_ms", "max_age_ms", "missing_ms"):
        number(guidance, key, 1, 300000, integer=True)
    left = number(guidance, "walking_left_max_ratio", 0, 1)
    right = number(guidance, "walking_right_min_ratio", 0, 1)
    number(guidance, "walking_center_intrusion_ratio", 0, 1)
    number(guidance, "walking_side_intrusion_ratio", 0, 1)
    number(guidance, "walking_voice_immediate_overlap_ratio", 0, 1)
    number(guidance, "walking_distance_tie_ratio", 0, 1)
    number(guidance, "walking_walkable_side_tie_ratio", 0, 1)
    two_step_enter = number(guidance, "walking_two_step_enter_ratio", 0, 1)
    two_step_exit = number(guidance, "walking_two_step_exit_ratio", 0, 1)
    for key in (
        "walking_lateral_confirm_ms",
        "walking_from_stop_confirm_ms", "walking_same_direction_repeat_ms",
        "walking_crowded_redirect_ms",
        "walking_crowded_repeat_ms",
        "walking_repeat_none_ms",
        "walking_stop_repeat_none_ms", "walking_missing_hold_ms",
    ):
        if key in guidance:
            number(guidance, key, 1, 300000, integer=True)
    if left >= right:
        raise ValueError("walking_left_max_ratio는 walking_right_min_ratio보다 작아야 합니다.")
    if two_step_exit >= two_step_enter:
        raise ValueError("walking_two_step_exit_ratio는 walking_two_step_enter_ratio보다 작아야 합니다.")
    return audio


# 브라우저 공개 설정 만들기
def browser_settings(app, audio):
    """화면에 필요한 허용 항목만 복사하고 서버 경로와 주소는 제외한다."""
    return deepcopy({
        "camera": {key: app["camera"][key] for key in (
            "facing_mode", "width", "height", "capture_max_side", "jpeg_quality",
            "bus_capture_max_side", "bus_jpeg_quality", "bus_capture_interval_ms",
            "bus_led_exposure_enabled", "bus_led_exposure_time_us",
            "encoder_timeout_ms", "resume_delay_ms",
        )},
        "recording": {key: app["recording"][key] for key in (
            "fps", "max_side", "video_bits_per_second", "chunk_interval_ms",
        )},
        "audio": {key: audio[key] for key in (
            "volume", "playback_rate", "default_validity_ms", "playback_timeout_ms", "tick_ms",
            "crosswalk_max_age_ms", "realtime_max_gap_ms", "guidance",
        )},
    })


# 실행 스크립트의 서버 주소 해석
def server_address(config_path=DEFAULT_APP_CONFIG, environ=None):
    """YAML 기본값에 APP_HOST·APP_PORT 환경변수를 적용하고 포트를 검증한다."""
    server = load_app_config(config_path)["server"].copy()
    env = os.environ if environ is None else environ
    server["host"] = env.get("APP_HOST") or server["host"]
    raw_port = env.get("APP_PORT")
    if raw_port:
        if not raw_port.isascii() or not raw_port.isdecimal():
            raise ValueError("APP_PORT는 1~65535 정수여야 합니다.")
        server["port"] = int(raw_port)
    number(server, "port", 1, 65535, integer=True)
    if not server["host"].strip() or any(char.isspace() for char in server["host"]):
        raise ValueError("APP_HOST에 공백을 포함할 수 없습니다.")
    return server
