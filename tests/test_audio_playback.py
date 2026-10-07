"""The iOS capture workaround changes tempo without changing the recorded voice's pitch."""
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.audio_playback import AudioPlaybackError, AudioPlaybackService
from backend.session import SessionManager
from src.video_audio import ffmpeg_executable
from test_realtime_app import FakeModels


@pytest.fixture(scope="module")
def sine_mp3():
    return subprocess.run([
        ffmpeg_executable(), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=2:sample_rate=24000", "-codec:a", "libmp3lame",
        "-b:a", "96k", "-f", "mp3", "pipe:1",
    ], check=True, capture_output=True, timeout=10).stdout


def decode(audio):
    result = subprocess.run([
        ffmpeg_executable(), "-nostdin", "-v", "error", "-f", "mp3", "-i", "pipe:0",
        "-f", "f32le", "-ac", "1", "-ar", "24000", "pipe:1",
    ], input=audio, check=True, capture_output=True, timeout=10)
    return np.frombuffer(result.stdout, dtype="<f4")


@pytest.mark.parametrize("rate", [0.75, 1, 1.5, 2])
def test_adjusted_audio_has_requested_duration_and_original_pitch(tmp_path, sine_mp3, rate):
    service = AudioPlaybackService(tmp_path)
    converted = service.adjust(sine_mp3, rate)
    samples = decode(converted)
    # Allow MP3 encoder padding and atempo's short overlap window.
    assert len(samples) / 24000 == pytest.approx(len(decode(sine_mp3)) / 24000 / rate, abs=0.08)
    middle = samples[len(samples) // 4:3 * len(samples) // 4]
    magnitudes = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    frequencies = np.fft.rfftfreq(len(middle), 1 / 24000)
    assert frequencies[np.argmax(magnitudes)] == pytest.approx(440, abs=4)
    if rate == 1:
        assert converted is sine_mp3


def test_content_version_rate_and_lru_determine_cache_entries(tmp_path, monkeypatch):
    calls = []

    def convert(command, **kwargs):
        calls.append((kwargs["input"], command[command.index("-filter:a") + 1]))
        return SimpleNamespace(stdout=f"converted-{len(calls)}".encode())

    monkeypatch.setattr("backend.audio_playback.subprocess.run", convert)
    service = AudioPlaybackService(tmp_path)
    service.max_cache_entries = 2
    clip = tmp_path / "walking-stop.mp3"
    clip.write_bytes(b"version-one")
    first = service.guidance("walking-stop", 1.5)
    second = service.guidance("walking-stop", 2)
    assert service.guidance("walking-stop", 1.5) == first
    assert len(calls) == 2
    # Same name and size with different bytes must invalidate the generated variant.
    clip.write_bytes(b"version-two")
    assert service.guidance("walking-stop", 1.5) != first
    assert len(calls) == 3
    assert service.adjust(b"version-one", 1.5) == first  # recently used entry survives
    assert service.adjust(b"version-one", 2) != second  # least recently used was evicted
    assert len(calls) == 4
    assert len(service._cache) == 2


def test_cache_is_bounded_by_bytes_as_well_as_entry_count(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.audio_playback.subprocess.run",
                        lambda *_args, **_kwargs: SimpleNamespace(stdout=b"converted"))
    service = AudioPlaybackService(tmp_path)
    service.max_cache_bytes = 10
    service.adjust(b"one", 1.5)
    service.adjust(b"two", 1.5)
    assert len(service._cache) == 1
    assert service._cache_bytes == 9


@pytest.mark.parametrize("rate", [0, 0.74, 2.01, float("inf"), float("nan"), "1.5", True])
def test_service_rejects_invalid_rates_before_conversion(tmp_path, rate):
    with pytest.raises(AudioPlaybackError) as error:
        AudioPlaybackService(tmp_path).adjust(b"source", rate)
    assert error.value.status_code == 422


@pytest.mark.parametrize("clip", ["../walking-stop", "walking-stop.mp3", "/etc/passwd", "arbitrary"])
def test_guidance_only_reads_allowlisted_names(tmp_path, clip):
    with pytest.raises(AudioPlaybackError) as error:
        AudioPlaybackService(tmp_path).guidance(clip)
    assert error.value.status_code == 404


def test_guidance_rejects_missing_and_escaping_symlink(tmp_path):
    directory = tmp_path / "voices"
    directory.mkdir()
    service = AudioPlaybackService(directory)
    with pytest.raises(AudioPlaybackError) as error:
        service.guidance("walking-stop")
    assert error.value.status_code == 404
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    (directory / "walking-stop.mp3").symlink_to(outside)
    with pytest.raises(AudioPlaybackError) as error:
        service.guidance("walking-stop")
    assert error.value.status_code == 404


def test_conversion_capacity_timeout_and_failed_output_are_bounded(tmp_path, monkeypatch):
    service = AudioPlaybackService(tmp_path, timeout=0.2, queue_timeout=0)
    service._conversions.acquire()
    service._conversions.acquire()
    with pytest.raises(AudioPlaybackError) as error:
        service.adjust(b"source", 1.5)
    assert error.value.status_code == 503
    service._conversions.release()
    service._conversions.release()

    def timeout(_command, **kwargs):
        assert kwargs["timeout"] == 0.2
        raise subprocess.TimeoutExpired("ffmpeg", kwargs["timeout"])

    monkeypatch.setattr("backend.audio_playback.subprocess.run", timeout)
    with pytest.raises(AudioPlaybackError) as error:
        service.adjust(b"source", 1.5)
    assert error.value.status_code == 503
    assert service._conversions.acquire(blocking=False)
    assert service._conversions.acquire(blocking=False)
    service._conversions.release()
    service._conversions.release()
    monkeypatch.setattr("backend.audio_playback.subprocess.run",
                        lambda *_args, **_kwargs: SimpleNamespace(stdout=b""))
    with pytest.raises(AudioPlaybackError) as error:
        service.adjust(b"source", 1.5)
    assert error.value.status_code == 503


def test_public_endpoints_preserve_default_bus_response_and_validate_requests(tmp_path, monkeypatch, sine_mp3):
    manager = SessionManager(tmp_path / "sessions", model_factory=FakeModels)
    app = create_app(manager)
    calls = []

    def synthesize(text):
        calls.append(text)
        return sine_mp3

    monkeypatch.setattr(app.state.bus_speech, "synthesize", synthesize)
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "walking-stop.mp3").write_bytes(sine_mp3)
    app.state.audio_playback.directory = voices
    with TestClient(app) as client:
        response = client.get("/api/bus-arrival-speech", params={"text": "143번 버스 확인함."})
        assert response.status_code == 200
        assert response.content == sine_mp3
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["cache-control"] == "public, max-age=3600"
        assert calls == ["143번 버스 확인함."]
        response = client.get("/api/bus-arrival-speech", params={"text": "143번 버스 확인함.", "rate": 2})
        assert response.status_code == 200
        assert len(decode(response.content)) / 24000 == pytest.approx(1, abs=0.12)
        response = client.get("/api/guidance-speech", params={"clip": "walking-stop"})
        assert response.content == sine_mp3
        response = client.get("/api/guidance-speech", params={"clip": "walking-stop", "rate": 1.5})
        assert response.status_code == 200
        assert len(decode(response.content)) / 24000 == pytest.approx(2 / 1.5, abs=0.12)
        assert response.headers["cache-control"] == "no-cache"
        for rate in ["nan", "inf", "-inf", "0.5", "2.1", "oops"]:
            for endpoint, params in [("guidance-speech", {"clip": "walking-stop"}),
                                     ("bus-arrival-speech", {"text": "버스 안내"})]:
                assert client.get(f"/api/{endpoint}", params={**params, "rate": rate}).status_code == 422
        for clip in ["../walking-stop", "walking-stop.mp3", "missing", "unknown"]:
            response = client.get("/api/guidance-speech", params={"clip": clip})
            assert response.status_code == 404
            assert "detail" in response.json()
        assert len(calls) == 2  # Validation failures must not invoke the remote speech provider.
