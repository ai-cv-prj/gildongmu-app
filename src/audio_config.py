"""
file_path: src/audio_config.py

영상 추론과 실시간 앱이 함께 사용할 안내 음원 폴더 설정을 읽는다.
프로젝트 상대 경로와 절대 경로를 모두 실제 디렉터리로 변환한다.
"""

from pathlib import Path

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_AUDIO_CONFIG = PROJECT_DIR / "configs" / "audio.yaml"


# 안내 음원 설정 읽기
def load_audio_config(config_path=DEFAULT_AUDIO_CONFIG):
    """YAML의 audio.voice_dir을 검증하고 절대 경로로 반환한다."""
    path = Path(config_path).expanduser()
    if not path.is_absolute():
        path = (PROJECT_DIR / path).resolve()
    with path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    audio = config.get("audio") if isinstance(config, dict) else None
    voice_dir = audio.get("voice_dir") if isinstance(audio, dict) else None
    if not isinstance(voice_dir, str) or not voice_dir.strip():
        raise ValueError("audio.voice_dir에는 비어 있지 않은 폴더 경로를 지정하세요.")
    directory = Path(voice_dir).expanduser()
    if not directory.is_absolute():
        directory = (PROJECT_DIR / directory).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"안내 음원 폴더가 없습니다: {directory}")
    return {"voice_dir": directory}


# 안내 음원 폴더 조회
def audio_directory(config_path=DEFAULT_AUDIO_CONFIG):
    """설정 파일에 지정된 실제 안내 음원 디렉터리를 반환한다."""
    return load_audio_config(config_path)["voice_dir"]
