"""
file_path: tests/test_settings.py

YAML 경로 해석·잘못된 설정 거부·서버 환경변수 우선순위와 공개 범위를 확인한다.
"""

import pytest
import yaml

from src.settings import (
    PROJECT_DIR, browser_settings, load_app_config, load_audio_settings,
    load_paths, resolve_path, server_address,
)


# 다른 작업 디렉터리에서 경로 읽기
def test_paths_are_independent_of_working_directory(tmp_path, monkeypatch):
    """실행 위치를 바꿔도 기본 경로는 프로젝트 내부를 가리킨다."""
    monkeypatch.chdir(tmp_path)
    paths = load_paths()
    assert resolve_path(paths["sample_dir"]) == PROJECT_DIR / "data/samples/input"
    assert resolve_path(paths["session_dir"]) == PROJECT_DIR / "test-result"
    assert resolve_path(str(tmp_path)) == tmp_path


# 설정 오류를 초기화 단계에서 거부
@pytest.mark.parametrize("group,key,value", [
    ("recording", "fps", 0), ("recording", "fps", float("nan")),
    ("recording", "preset", "unknown"), ("server", "port", True),
    ("camera", "bus_led_exposure_enabled", "true"),
    ("camera", "bus_led_exposure_time_us", 0), ("camera", "bus_led_exposure_time_us", 100000),
    ("camera", "bus_led_exposure_time_us", True),
    ("camera", "jpeg_quality", 2), ("camera", "capture_max_side", 4000),
    ("upload", "max_jpeg_bytes", -1), ("upload", "min_frame_side", 3000),
    ("session", "timezone", "missing/timezone"),
])
def test_invalid_app_settings_are_rejected(tmp_path, group, key, value):
    """잘못된 FPS·품질·용량·시간대를 설정 이름과 함께 알린다."""
    config = load_app_config()
    config[group][key] = value
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match=key):
        load_app_config(path)


# 서버와 터널이 공유하는 설정 우선순위
def test_server_environment_overrides_yaml(tmp_path):
    """환경변수가 없으면 YAML을 쓰고 있으면 검증 후 해당 값을 적용한다."""
    config = load_app_config()
    config["server"] = {"host": "127.0.0.2", "port": 8123}
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert server_address(path, {}) == config["server"]
    assert server_address(path, {"APP_PORT": "8124"}) == {"host": "127.0.0.2", "port": 8124}
    for value in ("0", "65536", "abc", "1.5"):
        with pytest.raises(ValueError, match="PORT|port"):
            server_address(path, {"APP_PORT": value})


# 공개 설정 범위와 원본 격리
def test_public_settings_exclude_paths_and_private_options():
    """추가한 비공개 항목이 브라우저로 흘러가지 않고 공개 값만 복사된다."""
    app, audio = load_app_config(), load_audio_settings()
    app["camera"]["private_token"] = "private"
    public = browser_settings(app, audio)
    assert set(public) == {"camera", "recording", "audio"}
    assert "private_token" not in public["camera"]
    assert public["camera"]["bus_led_exposure_enabled"] is True
    assert public["camera"]["bus_led_exposure_time_us"] == 16667
    assert "preset" not in public["recording"]
    assert public["recording"]["fps"] == app["recording"]["fps"]
    public["audio"]["guidance"]["stable_frames"] = 99
    assert audio["guidance"]["stable_frames"] == 3
