"""
file_path: tests/test_audio_config.py

안내 음원 YAML의 상대·절대 경로 해석과 필수 설정 검증을 확인한다.
"""

from pathlib import Path

import pytest
import yaml

from src.audio_config import PROJECT_DIR, audio_directory, load_audio_config
from src.settings import load_paths


# 실제 기본 음원 경로 확인
def test_default_voice_dir_comes_from_yaml():
    """기본 설정의 voice_dir이 이동한 프런트엔드 음원 폴더를 가리킨다."""
    assert audio_directory() == PROJECT_DIR / "frontend" / "audio" / "voice"


# FastAPI 음원 제공 경로 확인
def test_backend_mount_uses_configured_voice_dir():
    """실시간 앱의 /audio 주소가 YAML에 지정된 실제 폴더를 제공한다."""
    from backend.app import create_app

    app = create_app()
    route = next(item for item in app.routes if getattr(item, "path", None) == "/audio")
    path, stat = route.app.lookup_path("red.mp3")
    assert stat is not None
    assert Path(path) == audio_directory() / "red.mp3"


# 절대 경로 설정 확인
def test_absolute_voice_dir_is_supported(tmp_path):
    """다른 음원 폴더로 교체할 수 있도록 절대 경로도 그대로 사용한다."""
    voice = tmp_path / "voices"
    voice.mkdir()
    config = tmp_path / "paths.yaml"
    paths = {**load_paths(), "voice_dir": str(voice)}
    config.write_text(yaml.safe_dump(paths), encoding="utf-8")
    assert load_audio_config(paths_config=config)["voice_dir"] == voice


# 잘못된 음원 설정 거부 확인
def test_missing_voice_dir_is_rejected(tmp_path):
    """voice_dir이 없거나 실제 폴더가 아니면 서버 시작 전에 오류를 알린다."""
    config = tmp_path / "paths.yaml"
    paths = {**load_paths(), "voice_dir": ""}
    config.write_text(yaml.safe_dump(paths), encoding="utf-8")
    with pytest.raises(ValueError, match="voice_dir"):
        load_audio_config(paths_config=config)
    paths["voice_dir"] = str(tmp_path / "missing")
    config.write_text(yaml.safe_dump(paths), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="음원 폴더"):
        load_audio_config(paths_config=config)
