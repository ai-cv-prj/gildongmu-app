"""
file_path: tests/test_realtime_app.py

휴대폰 테스트 API의 세션 경계와 통합 응답을 확인한다.
"""

import io
import json
import subprocess
from datetime import datetime

import cv2
import numpy as np
import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

from backend.app import RecordingEventRequest, create_app
from backend.session import SessionManager
from src.video_audio import ffmpeg_executable


class FakeModels:
    """모델 가중치 없이 API 데이터 계약을 검증하는 추론 대역이다."""

    # 세션 초기화 횟수 기록
    def __init__(self):
        """첫 세션 모델 로딩을 나타낸다."""
        self.resets = 0

    # 두 번째 세션에서 상태 초기화
    def reset(self):
        """새 세션마다 추적이 초기화되는지 기록한다."""
        self.resets += 1

    # 보행과 신호 결과 동시 반환
    def predict(self, frame, frame_id, captured_at_ms):
        """음성 이벤트와 신호 대상이 포함된 같은 프레임 결과를 돌려준다."""
        risk = {
            "detections": [{"xyxy": [8, 10, 30, 40], "class_name": "person",
                            "alert_level": "danger", "detection_index": 0}],
            "level": "danger", "warning_text": "위험! 사람이 있음.",
            "roi": {"corridor_polygon": [[0.2, 0.3], [0.8, 0.3], [0.8, 1], [0.2, 1]],
                    "immediate_polygon": [[0.2, 0.7], [0.8, 0.7], [0.8, 1], [0.2, 1]]},
            "camera_view": {"status": "clear"}, "voice_event_ids": [3],
            "voice_text": "왼쪽에 사람.",
            "stop_proximity": {"status": "candidate", "nearby": False,
                               "newly_nearby": False, "observations": 1,
                               "required_observations": 3, "confidence": 0.7,
                               "xyxy": [0.1, 0.2, 0.6, 0.9], "track_id": 7},
        }
        signal = {
            "detections": [{"xyxy": [50, 5, 70, 30], "class_name": "pedestrian_signal",
                            "track_id": 12, "signal_state": "red", "selection_status": "selected"}],
            "crosswalks": [], "signal_state": "red", "selected_detection_index": 0,
            "candidate_detection_index": None,
        }
        return risk, signal, np.ones(frame.shape[:2], dtype=np.uint8), {
            "walkable": 1, "crosswalk": 2,
        }, 35


# 실제 JPEG 디코딩부터 API 응답·로그까지 확인
def test_mobile_session_flow(tmp_path):
    """추론 결과와 원본·오버레이 영상이 저장되고 프레임 이미지는 남지 않는다."""
    models = FakeModels()
    manager = SessionManager(tmp_path, model_factory=lambda: models)
    client = TestClient(create_app(manager))
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/").status_code == 200
    assert client.get("/static/audio/ko-v1/red.mp3").status_code == 200
    assert client.post("/api/sessions", json={"device_name": "  "}).status_code == 422
    start = client.post("/api/sessions", json={"device_name": "Galaxy S24+"})
    assert start.status_code == 200
    session_id = start.json()["session_id"]
    assert client.post("/api/sessions", json={"device_name": "iPhone"}).status_code == 409
    success, jpeg = cv2.imencode(".jpg", np.zeros((80, 100, 3), dtype=np.uint8))
    assert success
    url = f"/api/sessions/{session_id}/frames"
    payload = {"frame_id": "1", "captured_at_ms": "1000"}
    result = client.post(url, data=payload,
                         files={"image": ("frame.jpg", io.BytesIO(jpeg.tobytes()), "image/jpeg")})
    assert result.status_code == 200
    body = result.json()
    assert body["walking"]["event"]["voice_text"] == "왼쪽에 사람."
    assert body["traffic"]["event"]["signal_state"] == "red"
    assert body["walking"]["detections"][0]["xyxy"] == [0.08, 0.125, 0.3, 0.5]
    assert body["walking"]["mask_png"]
    assert body["stop_proximity"]["status"] == "candidate"
    assert client.post(url, data=payload,
                       files={"image": ("frame.jpg", jpeg.tobytes(), "image/jpeg")}).status_code == 409
    webm = tmp_path / "sample.webm"
    subprocess.run([
        ffmpeg_executable(), "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", "color=size=64x64:rate=10:duration=0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
        "-c:v", "libvpx", "-c:a", "libopus", "-shortest", str(webm),
    ], check=True, capture_output=True)
    recorded = client.post(f"/api/sessions/{session_id}/recording",
                           files={"video": ("recording.webm", webm.read_bytes(), "video/webm")})
    assert recorded.status_code == 200
    camera = client.post(f"/api/sessions/{session_id}/camera",
                         files={"video": ("camera.webm", webm.read_bytes(), "video/webm")})
    assert camera.status_code == 200
    stopped = client.post("/api/sessions/stop", json={"session_id": session_id})
    assert stopped.json()["frame_count"] == 1
    folder_name = start.json()["folder_name"]
    assert folder_name.startswith("Galaxy S24+_" + start.json()["date"] + "_")
    assert session_id not in folder_name
    folder = tmp_path / start.json()["date"] / folder_name
    logged = json.loads((folder / "results.jsonl").read_text(encoding="utf-8"))
    assert "mask_png" not in logged["walking"]
    assert logged["stop_proximity"] == body["stop_proximity"]
    assert not (folder / "frames").exists()
    assert (folder / "camera_overlay.mp4").is_file()
    assert (folder / "camera.mp4").is_file()
    events = [json.loads(line) for line in
              (folder / "recording_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(event["kind"], event["status"], event["source"]) for event in events] == [
        ("overlay", "saved", "server"), ("camera", "saved", "server")]
    assert not (folder / "camera_overlay.upload.webm").exists()
    assert not (folder / "camera.upload.webm").exists()
    probe = subprocess.run([
        ffmpeg_executable(), "-nostdin", "-v", "error", "-i", str(folder / "camera_overlay.mp4"),
        "-map", "0:v:0", "-f", "null", "-", "-map", "0:a:0", "-f", "null", "-",
    ], check=True, capture_output=True)
    assert probe.returncode == 0
    silent = subprocess.run([
        ffmpeg_executable(), "-nostdin", "-v", "error", "-i", str(folder / "camera.mp4"),
        "-map", "0:a:0", "-f", "null", "-",
    ], capture_output=True)
    assert silent.returncode != 0
    assert client.post("/api/sessions", json={"device_name": "iPhone"}).status_code == 200
    assert models.resets == 1


# 기종명과 한국 촬영시각으로 세션 폴더 생성 확인
def test_session_folder_uses_device_and_capture_time(tmp_path, monkeypatch):
    """기종과 초 단위 촬영시각을 사용하고 같은 이름에는 번호를 붙인다."""
    class FixedDatetime(datetime):
        """두 테스트가 같은 초에 시작한 상황을 재현한다."""

        # 지정된 촬영시각 반환
        @classmethod
        def now(cls, tz=None):
            """항상 같은 한국 시각을 반환한다."""
            return cls(2026, 9, 29, 18, 6, 48, tzinfo=tz)

    monkeypatch.setattr("backend.session.datetime", FixedDatetime)
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    first = manager.start("Galaxy/S24")
    assert first["folder_name"].startswith("Galaxy_S24_" + first["date"] + "_")
    assert len(first["folder_name"].removeprefix("Galaxy_S24_")) == 15
    assert (tmp_path / first["date"] / first["folder_name"] / "session.json").is_file()
    assert first["session_id"] not in first["folder_name"]
    stopped = manager.stop(first["session_id"])
    assert stopped["folder_name"] == first["folder_name"]
    second = manager.start("Galaxy/S24")
    assert first["folder_name"] == "Galaxy_S24_20260929_180648"
    assert second["folder_name"] == first["folder_name"] + "_2"


# 손상 이미지와 큰 업로드 거부 확인
def test_invalid_camera_frame(tmp_path):
    """서버가 디코딩할 수 없는 입력을 추론 모델에 전달하지 않는다."""
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    client = TestClient(create_app(manager))
    session_id = client.post("/api/sessions", json={"device_name": "Phone"}).json()["session_id"]
    url = f"/api/sessions/{session_id}/frames"
    result = client.post(url, data={"frame_id": "1", "captured_at_ms": "1000"},
                         files={"image": ("frame.jpg", b"broken", "image/jpeg")})
    assert result.status_code == 422
    oversized = client.post(url, data={"frame_id": "1", "captured_at_ms": "1000"},
                            files={"image": ("frame.jpg", b"x" * 3_000_001, "image/jpeg")})
    assert oversized.status_code == 413


def test_recording_failures_are_logged(tmp_path):
    """브라우저의 빈 Blob과 서버의 빈·손상 업로드 사유를 세션에 남긴다."""
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    app = create_app(manager)
    started = manager.start("Phone")
    session_id = started["session_id"]
    folder = tmp_path / started["date"] / started["folder_name"]
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}

    report = routes["/api/sessions/{session_id}/recording-events"]
    assert report(session_id, RecordingEventRequest(
        kind="overlay", status="empty", detail="녹화 파일이 비어 있습니다."
    )) == {"recorded": True}
    with pytest.raises(HTTPException) as empty:
        routes["/api/sessions/{session_id}/camera"](
            session_id, UploadFile(file=io.BytesIO(b""), filename="camera.webm")
        )
    assert empty.value.status_code == 422
    with pytest.raises(HTTPException) as broken:
        routes["/api/sessions/{session_id}/recording"](
            session_id, UploadFile(file=io.BytesIO(b"invalid webm"), filename="recording.webm")
        )
    assert broken.value.status_code == 422

    sample = tmp_path / "sample.webm"
    subprocess.run([
        ffmpeg_executable(), "-nostdin", "-y", "-v", "error", "-f", "lavfi",
        "-i", "color=size=64x64:rate=10:duration=0.2", "-c:v", "libvpx", str(sample),
    ], check=True, capture_output=True)
    saved = routes["/api/sessions/{session_id}/camera"](
        session_id, UploadFile(file=io.BytesIO(sample.read_bytes()), filename="camera.webm")
    )
    assert saved["saved"] is True

    events = [json.loads(line) for line in
              (folder / "recording_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(event["kind"], event["status"], event["source"]) for event in events] == [
        ("overlay", "empty", "browser"), ("camera", "empty", "server"),
        ("overlay", "failed", "server"), ("camera", "saved", "server")]
    assert all(event["detail"] and event["at"] for event in events)
    assert not (folder / "camera.upload.webm").exists()
    assert not (folder / "camera_overlay.upload.webm").exists()
