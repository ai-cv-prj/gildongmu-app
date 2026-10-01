"""
file_path: src/audio_config.py

영상 추론과 실시간 앱이 함께 사용할 안내 음원 폴더 설정을 읽는다.
프로젝트 상대 경로와 절대 경로를 모두 실제 디렉터리로 변환한다.
"""

from src.settings import (
    DEFAULT_AUDIO_CONFIG, DEFAULT_PATHS_CONFIG, PROJECT_DIR,
    load_audio_settings, load_paths, resolve_path,
)


# 안내 음원 설정 읽기
def load_audio_config(config_path=DEFAULT_AUDIO_CONFIG, paths_config=DEFAULT_PATHS_CONFIG):
    """음성 옵션과 paths.yaml의 실제 음원 디렉터리를 함께 반환한다."""
    audio = load_audio_settings(config_path)
    directory = resolve_path(load_paths(paths_config)["voice_dir"])
    if not directory.is_dir():
        raise FileNotFoundError(f"안내 음원 폴더가 없습니다: {directory}")
    return {**audio, "voice_dir": directory}


# 안내 음원 폴더 조회
def audio_directory(config_path=DEFAULT_AUDIO_CONFIG, paths_config=DEFAULT_PATHS_CONFIG):
    """설정 파일에 지정된 실제 안내 음원 디렉터리를 반환한다."""
    return load_audio_config(config_path, paths_config)["voice_dir"]
