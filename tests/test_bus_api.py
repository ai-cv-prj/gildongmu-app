"""Public bus endpoints and their integration with the existing session API."""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.boarding import Boarding, BoardingError
from backend.bus.arrival import BusArrivalError
from backend.bus.speech import BusSpeechError
from backend.session import SessionManager
from test_realtime_app import FakeModels


class FakeBus:
    def __init__(self, *, fail=False):
        self.targets = []
        self.frames = []
        self.prewarms = 0
        self.fail = fail
        self.closed = False

    def set_target(self, session_id, route):
        self.targets.append((session_id, route))

    def prewarm(self):
        self.prewarms += 1

    def observe(self, session_id, frame, frame_id, captured_at_ms):
        self.frames.append((None if frame is None else frame.shape[:2], frame_id, captured_at_ms))
        if self.fail:
            raise RuntimeError("optional worker unavailable")
        return {"status": "searching", "event": None, "detections": [],
                "captured_at_ms": None, "frame_id": None}

    def close(self):
        self.closed = True


def test_highres_bus_frame_is_separate_from_walking_frame(tmp_path):
    import cv2

    bus = FakeBus()
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=bus)
    with TestClient(create_app(manager)) as client:
        started = client.post("/api/sessions", json={"device_name": "Phone", "bus_highres": True})
        started.raise_for_status()
        session_id = started.json()["session_id"]
        assert bus.prewarms == 1
        ok, walking_jpeg = cv2.imencode(".jpg", np.zeros((80, 100, 3), np.uint8))
        assert ok
        ok, bus_jpeg = cv2.imencode(".jpg", np.zeros((160, 200, 3), np.uint8))
        assert ok
        url = f"/api/sessions/{session_id}/frames"
        result = client.post(url, data={"frame_id": 1, "captured_at_ms": 1000,
                                         "bus_captured_at_ms": 1010}, files={
            "image": ("walk.jpg", walking_jpeg.tobytes(), "image/jpeg"),
            "bus_image": ("bus.jpg", bus_jpeg.tobytes(), "image/jpeg"),
        })
        result.raise_for_status()
        assert "bus_input" not in result.json()
        assert bus.frames == [((160, 200), 1, 1010), (None, 1, 1010)]
        folder = tmp_path / started.json()["date"] / started.json()["folder_name"]
        assert not (folder / "frames").exists()  # Live bus JPEGs are not written to disk.
        result = client.post(url, data={"frame_id": 2, "captured_at_ms": 1100}, files={
            "image": ("walk.jpg", walking_jpeg.tobytes(), "image/jpeg"),
        })
        result.raise_for_status()
        assert bus.frames[-1] == (None, 2, 1100)
        assert not any(shape == (80, 100) for shape, _, _ in bus.frames)


def test_manual_stop_uses_unique_arrival_and_requires_completed_stop():
    state = Boarding()
    with pytest.raises(BoardingError):
        state.act("arrive", crossing_active=True)
    assert state.status == "searching"
    arrival = state.act("arrive")
    assert arrival["arrival_source"] == "user_confirmed"
    assert arrival["status"] == "awaiting_stop"
    assert not arrival["assumed_stationary"]
    assert state.act("arrive") == arrival
    with pytest.raises(BoardingError):
        state.act("submit", arrival["arrival_event_id"], "701")
    state.act("stop_announced", arrival["arrival_event_id"])
    assert state.stationary
    state.act("submit", arrival["arrival_event_id"], "701")
    newer = state.act("arrive")
    assert newer["arrival_event_id"] > arrival["arrival_event_id"]
    with pytest.raises(BoardingError):
        state.act("stop_announced", arrival["arrival_event_id"])
    assert state.status == "awaiting_stop"


@pytest.mark.parametrize("action", ["submit", "cancel"])
def test_reopen_visual_arrival_is_user_confirmed_without_visible_stop(action):
    state = Boarding()
    arrival = state.observe({"nearby": True, "arrival_event_id": 7})
    arrival_id = arrival["arrival_event_id"]
    assert arrival["arrival_source"] == "visual_proximity"
    state.act("stop_announced", arrival_id)
    state.act(action, arrival_id, "701" if action == "submit" else None)
    state.observe({"nearby": False})
    before = state.snapshot()
    with pytest.raises(BoardingError):
        state.act("reopen", arrival_id, crossing_active=True)
    assert state.snapshot() == before
    reopened = state.act("reopen", arrival_id)
    assert reopened["arrival_source"] == "user_confirmed"
    assert reopened["arrival_event_id"] == arrival_id
    assert reopened["status"] == "awaiting_stop"
    assert not reopened["assumed_stationary"]
    assert state.observe(None) == reopened
    assert state.act("stop_announced", arrival_id)["status"] == "pending"
    assert state.act("submit", arrival_id, "604")["bus_number"] == "604"


def test_reopen_api_preserves_arrival_identity_and_logs_user_confirmation(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    with TestClient(create_app(manager)) as client:
        session = client.post("/api/sessions", json={"device_name": "Phone"}).json()
        url = f"/api/sessions/{session['session_id']}/boarding"
        arrived = manager.models.boarding.observe({"nearby": True, "arrival_event_id": 8})
        arrival_id = arrived["arrival_event_id"]
        client.put(url, json={"action": "stop_announced", "arrival_event_id": arrival_id})
        client.put(url, json={"action": "submit", "arrival_event_id": arrival_id, "bus_number": "701"})
        manager.models.boarding.observe(None)
        manager.session["crossing_active"] = True
        assert client.put(url, json={"action": "reopen", "arrival_event_id": arrival_id}).status_code == 422
        assert manager.models.boarding.status == "submitted"
        manager.session["crossing_active"] = False
        result = client.put(url, json={"action": "reopen", "arrival_event_id": arrival_id}).json()
        assert result["arrival_source"] == "user_confirmed"
        assert result["arrival_event_id"] == arrival_id
        assert result["status"] == "awaiting_stop"
        folder = tmp_path / session["date"] / session["folder_name"]
        records = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
        assert [record["action"] for record in records] == ["stop_announced", "submit", "reopen"]
        assert [record["arrival_source"] for record in records] == ["visual_proximity", "visual_proximity", "user_confirmed"]
        assert all(record["arrival_event_id"] == arrival_id for record in records)
        assert manager.bus_recognizer.targets[-1] == (None, None)


def test_boarding_submit_reopen_stop_and_shutdown_control_worker(tmp_path):
    bus = FakeBus()
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=bus)
    with TestClient(create_app(manager)) as client:
        session = client.post("/api/sessions", json={"device_name": "Phone"}).json()
        session_id = session["session_id"]
        url = f"/api/sessions/{session_id}/boarding"
        arrived = client.put(url, json={"action": "arrive"}).json()
        arrival_id = arrived["arrival_event_id"]
        assert arrived["status"] == "awaiting_stop"
        assert client.put(url, json={"action": "stop_announced", "arrival_event_id": arrival_id}).status_code == 200
        submitted = client.put(url, json={"action": "submit", "arrival_event_id": arrival_id,
                                         "bus_number": " 701 "})
        assert submitted.status_code == 200
        assert submitted.json()["bus_number"] == "701"
        assert bus.targets[-1] == (session_id, "701")
        before = len(bus.targets)
        client.put(url, json={"action": "submit", "arrival_event_id": arrival_id, "bus_number": "701"})
        assert len(bus.targets) == before  # duplicate request cannot reset accumulated OCR evidence
        client.put(url, json={"action": "reopen", "arrival_event_id": arrival_id})
        assert bus.targets[-1] == (None, None)
        client.post("/api/sessions/stop", json={"session_id": session_id})
        assert bus.targets[-1] == (None, None)
        client.post("/api/sessions", json={"device_name": "Phone"})
        assert manager.models.boarding.snapshot()["status"] == "searching"
        assert bus.targets[-1] == (None, None)
    assert bus.closed


def test_manual_arrival_api_rejects_crossing_session(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    with TestClient(create_app(manager)) as client:
        session = client.post("/api/sessions", json={"device_name": "Phone"}).json()
        manager.process(session["session_id"], 1, 1000, np.zeros((80, 100, 3), np.uint8))
        response = client.put(f"/api/sessions/{session['session_id']}/boarding", json={"action": "arrive"})
        assert response.status_code == 422
        assert manager.models.boarding.status == "searching"


def test_ocr_worker_error_keeps_walking_and_signal_results(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus(fail=True))
    session = manager.start("Phone")
    try:
        result = manager.process(session["session_id"], 1, 1000, np.zeros((80, 100, 3), np.uint8))
        assert result["walking"]["detections"]
        assert result["crosswalk"]["event"]["status"] == "crossing"
        assert result["bus"]["status"] == "error"
        assert result["bus"]["event"] is None
        assert result["bus"]["error"] == "bus_recognition_unavailable:RuntimeError"
        assert manager.session["frame_count"] == 1
    finally:
        manager.close()


def test_gps_arrival_route_validation_and_structured_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("SEOUL_BUS_API_KEY", "")
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    app = create_app(manager)
    with TestClient(app) as client:
        params = {"bus_number": "701", "latitude": 37.5, "longitude": 127.0, "accuracy_m": 8}
        response = client.get("/api/nearby-bus-arrival", params=params)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "bus_api_unconfigured"
        assert client.get("/api/nearby-bus-arrival", params={**params, "latitude": 91}).status_code == 422
        assert client.get("/api/nearby-bus-arrival", params={**params, "accuracy_m": -1}).status_code == 422
        seen = []

        def lookup(*args):
            seen.append(args)
            return {"status": "ok", "bus_number": "701", "matches": [], "announcement": "버스 조회 중"}

        monkeypatch.setattr(app.state.bus_arrivals, "lookup_nearby", lookup)
        assert client.get("/api/nearby-bus-arrival", params=params).json()["bus_number"] == "701"
        assert seen == [("701", 37.5, 127.0, 8)]

        def fail(*args):
            raise BusArrivalError(502, "test_upstream", "서울 버스 조회 실패")

        monkeypatch.setattr(app.state.bus_arrivals, "lookup_nearby", fail)
        assert client.get("/api/nearby-bus-arrival", params=params).json()["error"]["code"] == "test_upstream"
        readiness = client.get("/api/bus/status").json()
        assert readiness["arrival_ready"] is False
        assert set(readiness["model_files"]) == {"route_display", "bus_detector", "parseq_weights", "parseq_source"}
        assert "seoul_bus_api_key" not in readiness


def test_bus_speech_endpoint_returns_mp3_and_translates_service_error(tmp_path, monkeypatch):
    app = create_app(SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus()))
    with TestClient(app) as client:
        audio = b"ID3" + bytes(32)
        monkeypatch.setattr(app.state.bus_speech, "synthesize", lambda text: audio)
        response = client.get("/api/bus-arrival-speech", params={"text": "701번 버스"})
        assert response.status_code == 200
        assert response.content == audio
        assert response.headers["content-type"] == "audio/mpeg"

        def fail(text):
            raise BusSpeechError(502, "speech_unavailable", "음성 생성 실패")

        monkeypatch.setattr(app.state.bus_speech, "synthesize", fail)
        response = client.get("/api/bus-arrival-speech", params={"text": "701번 버스"})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "speech_unavailable"


def test_bus_event_endpoint_bounds_payload_and_preserves_server_attribution(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    with TestClient(create_app(manager)) as client:
        session = client.post("/api/sessions", json={"device_name": "Phone"}).json()
        url = f"/api/sessions/{session['session_id']}/bus-events"
        response = client.post(url, json={"events": [{"type": "gps_poll", "session_id": "spoofed",
                                                      "server_at": "client time", "latitude": 37.5}]})
        assert response.json() == {"saved": 1}
        path = tmp_path / session["date"] / session["folder_name"] / "events.jsonl"
        record = json.loads(path.read_text())
        assert record["session_id"] == session["session_id"]
        assert record["server_at"] != "client time"
        assert record["type"] == "gps_poll"
        for body in ({"events": []}, {"events": [{"type": "poll"}] * 26},
                     {"events": ["invalid"]}, {"events": [{"type": ""}]}):
            assert client.post(url, json=body).status_code == 422
        for body in ('{"events":[{"type":"poll","latitude":NaN}]}',
                     '{"events":[{"type":"poll","latitude":1e999}]}', '{'):
            assert client.post(url, content=body).status_code == 422
        assert client.post(url, content=" " * (64 * 1024 + 1)).status_code == 413
        assert len(path.read_text().splitlines()) == 1
        client.post("/api/sessions/stop", json={"session_id": session["session_id"]})
        assert client.post(url, json={"events": [{"type": "gps_poll"}]}).status_code == 409
