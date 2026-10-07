"""Response loss must not repeat inference or corrupt frame sequencing."""

import cv2
import numpy as np
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.session import SessionManager
from test_bus_api import FakeBus
from test_realtime_app import FakeModels


class CountingModels(FakeModels):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def predict(self, *args):
        self.calls += 1
        return super().predict(*args)


def jpeg(value=0):
    ok, encoded = cv2.imencode(".jpg", np.full((80, 100, 3), value, np.uint8))
    assert ok
    return encoded.tobytes()


def send_frame(client, session_id, frame_id=1, timestamp=1000, image=None,
               bus_image=None, bus_timestamp=1010):
    data = {"frame_id": frame_id, "captured_at_ms": timestamp}
    files = {"image": ("frame.jpg", jpeg() if image is None else image, "image/jpeg")}
    if bus_image is not None:
        files["bus_image"] = ("bus.jpg", bus_image, "image/jpeg")
        data["bus_captured_at_ms"] = bus_timestamp
    return client.post(f"/api/sessions/{session_id}/frames", data=data, files=files)


def test_latest_identical_frame_replays_full_response_once(tmp_path):
    bus = FakeBus()
    manager = SessionManager(tmp_path, model_factory=CountingModels, bus_recognizer=bus)
    with TestClient(create_app(manager)) as client:
        started = client.post("/api/sessions", json={"device_name": "Phone"}).json()
        session_id = started["session_id"]
        # Simulate a processed request whose response the browser never received.
        first = send_frame(client, session_id)
        retry = send_frame(client, session_id)
        assert first.status_code == retry.status_code == 200
        assert retry.json() == first.json()
        assert retry.json()["walking"]["mask_png"]
        assert manager.models.calls == 1
        assert len(bus.frames) == 1
        folder = tmp_path / started["date"] / started["folder_name"]
        assert len((folder / "results.jsonl").read_text().splitlines()) == 1
        assert manager.session["frame_count"] == 1

        assert send_frame(client, session_id, image=jpeg(255)).status_code == 409
        assert send_frame(client, session_id, timestamp=1001).status_code == 409
        assert send_frame(client, session_id, frame_id=3, timestamp=1200).status_code == 409
        assert manager.models.calls == 1
        assert send_frame(client, session_id, frame_id=2, timestamp=1100).status_code == 200
        assert send_frame(client, session_id).status_code == 409  # older than latest
        assert manager.models.calls == 2


def test_replay_identity_includes_bus_image_and_capture_metadata(tmp_path):
    bus = FakeBus()
    manager = SessionManager(tmp_path, model_factory=CountingModels, bus_recognizer=bus)
    with TestClient(create_app(manager)) as client:
        session_id = client.post("/api/sessions", json={"device_name": "Phone", "bus_highres": True}).json()["session_id"]
        first = send_frame(client, session_id, bus_image=jpeg(100))
        assert first.status_code == 200
        assert send_frame(client, session_id, bus_image=jpeg(100)).json() == first.json()
        assert send_frame(client, session_id, bus_image=jpeg(101)).status_code == 409
        assert send_frame(client, session_id, bus_image=jpeg(100), bus_timestamp=1020).status_code == 409
        assert send_frame(client, session_id).status_code == 409
        assert manager.models.calls == 1
        assert len(bus.frames) == 2  # high-res observation and cached bus response


def test_stopped_restarted_and_new_sessions_cannot_replay_old_results(tmp_path):
    manager = SessionManager(tmp_path, model_factory=CountingModels, bus_recognizer=FakeBus())
    with TestClient(create_app(manager)) as client:
        first_id = client.post("/api/sessions", json={"device_name": "Phone"}).json()["session_id"]
        assert send_frame(client, first_id).status_code == 200
        client.post("/api/sessions/stop", json={"session_id": first_id}).raise_for_status()
        assert manager._last_frame_result is None
        assert send_frame(client, first_id).status_code == 409
        second_id = client.post("/api/sessions", json={"device_name": "Phone"}).json()["session_id"]
        assert send_frame(client, first_id).status_code == 409
        response = send_frame(client, second_id)
        assert response.status_code == 200
        assert response.json()["session_id"] == second_id
        assert manager.models.calls == 2
    restarted = SessionManager(tmp_path, model_factory=CountingModels, bus_recognizer=FakeBus())
    with TestClient(create_app(restarted)) as client:
        assert send_frame(client, first_id).status_code == 409
        assert send_frame(client, second_id).status_code == 409
        assert restarted.models is None
