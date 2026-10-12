"""Selected mobile clips are stored after inference and remain bounded."""

import base64
import hashlib
import io
import json
import logging
import subprocess
import threading
import time

import cv2
import numpy as np
import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

from backend.app import ClipCompleteRequest, StartRequest, StopRequest, create_app
from backend.clips import ClipError, ClipStore
from backend.logger import configure_app_logging
from backend.session import SessionManager
from src.video_audio import ffmpeg_executable
from test_realtime_app import FakeModels


def endpoints(app):
    return {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}


def sample_webm(path, *, duration=.4):
    subprocess.run([
        ffmpeg_executable(), "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
        f"color=size=64x64:rate=10:duration={duration}", "-c:v", "libvpx", str(path),
    ], check=True, capture_output=True)
    return path.read_bytes()


def mark_legacy_session(manager, session_id):
    """Reproduce a stopped session saved before the single-video policy existed."""
    path = manager.completed_folder(session_id) / "session.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    del saved["clip_policy"]
    path.write_text(json.dumps(saved), encoding="utf-8")


def test_new_session_uploads_one_real_90_second_video_with_late_frames(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    app = create_app(manager)
    recording = sample_webm(tmp_path / "ninety-seconds.webm", duration=90)
    image = np.zeros((80, 100, 3), dtype=np.uint8)
    success, encoded = cv2.imencode(".jpg", image)
    assert success
    jpeg = encoded.tobytes()
    with TestClient(app) as client:
        session_id = manager.start("Android 90s")["session_id"]
        for frame_id, timestamp in ((1, 1100), (2, 90000)):
            manager.process(session_id, frame_id, timestamp, image,
                            image_bytes=jpeg, save_live_frame=False)
        manager.stop(session_id)
        base = f"/api/sessions/{session_id}/clips"
        policy = client.get(base).json()
        assert policy["max_clips"] == 1 and policy["max_clip_ms"] == 90000
        files = {"video": ("camera.webm", recording, "video/webm")}
        assert client.post(f"{base}/1/chunks/0", files=files).status_code == 200
        for frame_id, timestamp in ((1, 1100), (2, 90000)):
            response = client.post(f"{base}/1/frames/{frame_id}",
                                   data={"captured_at_ms": timestamp},
                                   files={"image": ("frame.jpg", jpeg, "image/jpeg")})
            assert response.status_code == 200, response.text
        payload = {"mime_type": "video/webm", "chunk_count": 1, "size_bytes": len(recording),
                   "started_at_ms": 1000, "ended_at_ms": 91000}
        response = client.post(f"{base}/1/complete", json={**payload, "ended_at_ms": 91001})
        assert response.status_code == 422
        response = client.post(f"{base}/1/complete", json=payload)
        assert response.status_code == 200, response.text
        manifest = response.json()
        assert manifest["duration_ms"] == 90000
        assert 89900 <= manifest["original_duration_ms"] <= 90500
        assert manifest["frame_count"] == 2
        for response in (
            client.post(f"{base}/2/chunks/0", files=files),
            client.post(f"{base}/2/frames/1", data={"captured_at_ms": 1100},
                        files={"image": ("frame.jpg", jpeg, "image/jpeg")}),
            client.post(f"{base}/2/complete", json=payload),
        ):
            assert response.status_code == 422
            assert "영상 1개" in response.json()["detail"]
    # The application lifespan waits for the real MP4 export to finish.
    manifest = app.state.clip_store.status(session_id)["clips"][0]
    assert manifest["state"] == "ready", manifest.get("error")
    assert [frame["clip_offset_ms"] for frame in manifest["frames"]] == [100, 89000]
    original = manager.completed_folder(session_id) / manifest["original_path"]
    assert original.read_bytes() == recording


def test_inference_export_keeps_screen_overlay_and_heard_audio(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    started = manager.start("Phone")
    session_id = started["session_id"]
    image = np.zeros((80, 100, 3), dtype=np.uint8)
    success, encoded = cv2.imencode(".jpg", image)
    assert success
    jpeg = encoded.tobytes()
    manager.process(session_id, 1, 1100, image, image_bytes=jpeg, save_live_frame=False)
    manager.stop(session_id)
    store = ClipStore(manager, max_jpeg_bytes=3_000_000)
    original = tmp_path / "with_audio.webm"
    subprocess.run([
        ffmpeg_executable(), "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
        "color=size=100x80:rate=10:duration=0.4", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=0.4", "-c:v", "libvpx", "-c:a", "libopus",
        "-shortest", str(original),
    ], check=True, capture_output=True)
    recording = original.read_bytes()
    store.add_chunk(session_id, 1, 0, recording)
    overlay = np.zeros((160, 200, 4), dtype=np.uint8)
    overlay[40:80, 60:100] = (0, 0, 255, 255)
    success, png = cv2.imencode(".png", overlay)
    assert success
    store.add_frame(session_id, 1, 1, 1100, jpeg, overlay_png=base64.b64encode(png).decode("ascii"))
    store.complete(session_id, 1, mime_type="video/webm", chunk_count=1,
                   size_bytes=len(recording), started_at_ms=1000, ended_at_ms=1400)
    store.export(session_id, 1)
    clip = manager.completed_folder(session_id) / "clips" / "clip_001"
    assert store.status(session_id)["clips"][0]["state"] == "ready"
    assert json.loads((clip / "manifest.json").read_text())["frames"][0]["overlay"] is True
    capture = cv2.VideoCapture(str(clip / "inference.mp4"))
    try:
        success, rendered = capture.read()
        assert success
        assert rendered[30, 40, 2] > 180  # 실제 화면에서 저장한 붉은 오버레이
        assert rendered[5, 5, 2] < 50
    finally:
        capture.release()
    decoded = subprocess.run([
        ffmpeg_executable(), "-nostdin", "-v", "error", "-i", str(clip / "inference.mp4"),
        "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1",
    ], check=True, capture_output=True).stdout
    assert np.max(np.abs(np.frombuffer(decoded, dtype=np.int16))) > 100


def test_no_clip_session_has_no_video_and_stop_retries(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    app = create_app(manager)
    routes = endpoints(app)
    started = manager.start("Phone")
    session_id = started["session_id"]
    first = routes["/api/sessions/stop"](StopRequest(session_id=session_id))
    again = routes["/api/sessions/stop"](StopRequest(session_id=session_id))
    assert first == again
    folder = manager.completed_folder(session_id)
    assert not list(folder.glob("**/*.mp4"))
    assert routes["/api/sessions/{session_id}/clips"](session_id)["clips"] == []
    restarted = SessionManager(tmp_path, model_factory=FakeModels)
    assert restarted.completed_folder(session_id) == folder


def test_legacy_clips_remain_uploadable_listed_and_retryable_after_restart(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    app = create_app(manager)
    routes = endpoints(app)
    started = manager.start("Phone")
    session_id = started["session_id"]
    jpeg = []
    masks = []
    frame_route = routes["/api/sessions/{session_id}/frames"]
    for frame_id, captured_at_ms in ((1, 1000), (2, 2500)):
        success, encoded = cv2.imencode(".jpg", np.zeros((80, 100, 3), dtype=np.uint8))
        assert success
        data = encoded.tobytes()
        jpeg.append(data)
        result = frame_route(session_id, frame_id, captured_at_ms,
                             UploadFile(file=io.BytesIO(data), filename="frame.jpg"))
        masks.append(result["walking"]["mask_png"])
        assert result["frame_file"] is None
    folder = tmp_path / started["date"] / started["folder_name"]
    assert not (folder / "frames").exists()
    routes["/api/sessions/stop"](StopRequest(session_id=session_id))
    mark_legacy_session(manager, session_id)
    recording = sample_webm(tmp_path / "sample.webm")
    chunk_route = routes["/api/sessions/{session_id}/clips/{clip_id}/chunks/{index}"]
    selected_route = routes["/api/sessions/{session_id}/clips/{clip_id}/frames/{frame_id}"]
    complete_route = routes["/api/sessions/{session_id}/clips/{clip_id}/complete"]
    for clip_id, frame_id, start, end in ((1, 1, 900, 1400), (5, 2, 2400, 2900)):
        upload = lambda: UploadFile(file=io.BytesIO(recording), filename="camera.webm")
        assert chunk_route(session_id, clip_id, 0, upload())["saved"]
        assert chunk_route(session_id, clip_id, 0, upload())["saved"]
        assert selected_route(session_id, clip_id, frame_id, [1000, 2500][frame_id - 1],
                              UploadFile(file=io.BytesIO(jpeg[frame_id - 1]), filename="frame.jpg"),
                              masks[frame_id - 1])["saved"]
        request = ClipCompleteRequest(mime_type="video/webm", chunk_count=1,
                                      size_bytes=len(recording), started_at_ms=start, ended_at_ms=end)
        assert complete_route(session_id, clip_id, request)["clip_id"] == clip_id
        assert complete_route(session_id, clip_id, request)["clip_id"] == clip_id
        assert chunk_route(session_id, clip_id, 0, upload())["saved"]
        assert selected_route(session_id, clip_id, frame_id, [1000, 2500][frame_id - 1],
                              UploadFile(file=io.BytesIO(jpeg[frame_id - 1]), filename="frame.jpg"),
                              masks[frame_id - 1])["saved"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = routes["/api/sessions/{session_id}/clips"](session_id)
        if [clip["state"] for clip in status["clips"]] == ["ready", "ready"]:
            break
        time.sleep(.02)
    assert [clip["state"] for clip in status["clips"]] == ["ready", "ready"]
    assert [clip["clip_id"] for clip in status["clips"]] == [1, 5]
    assert status["max_clips"] == 5
    for clip_id in (1, 5):
        clip = folder / "clips" / f"clip_{clip_id:03d}"
        assert (clip / "original.webm").read_bytes() == recording
        assert (clip / "inference.mp4").stat().st_size > 0
        assert {path.name for path in clip.iterdir()} == {"original.webm", "inference.mp4", "manifest.json"}
        timeline = json.loads((clip / "manifest.json").read_text(encoding="utf-8"))
        assert timeline["first_frame_offset_ms"] == 100
        assert timeline["frames"][0]["clip_offset_ms"] == 100
    restarted = SessionManager(tmp_path, model_factory=FakeModels)
    store = ClipStore(restarted, max_jpeg_bytes=3_000_000)
    assert store.status(session_id)["clips"][0]["state"] == "ready"
    assert store.add_frame(session_id, 1, 1, 1000, jpeg[0], masks[0])["saved"]
    assert store.add_chunk(session_id, 1, 0, recording)["saved"]
    assert store.add_frame(session_id, 5, 2, 2500, jpeg[1], masks[1])["saved"]
    assert store.add_chunk(session_id, 5, 0, recording)["saved"]
    manifest, retry = store.complete(session_id, 5, mime_type="video/webm", chunk_count=1,
                                      size_bytes=len(recording), started_at_ms=2400, ended_at_ms=2900)
    assert manifest["state"] == "ready" and not retry
    store.export(session_id, 1)
    clip = folder / "clips" / "clip_001"
    assert {path.name for path in clip.iterdir()} == {"original.webm", "inference.mp4", "manifest.json"}


def test_clip_window_hash_and_overlap_are_rejected(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    started = manager.start("Phone")
    session_id = started["session_id"]
    success, encoded = cv2.imencode(".jpg", np.zeros((80, 100, 3), dtype=np.uint8))
    assert success
    data = encoded.tobytes()
    manager.process(session_id, 1, 1000, cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR),
                    image_bytes=data, save_live_frame=False)
    manager.stop(session_id)
    mark_legacy_session(manager, session_id)
    store = ClipStore(manager, max_jpeg_bytes=3_000_000)
    with pytest.raises(ClipError):
        store.add_frame(session_id, 1, 1, 1000, data + b"altered")
    recording = sample_webm(tmp_path / "sample.webm")
    store.add_chunk(session_id, 1, 0, recording)
    with pytest.raises(ClipError):
        store.complete(session_id, 1, mime_type="video/webm", chunk_count=1, size_bytes=len(recording),
                       started_at_ms=900, ended_at_ms=90_901)
    manifest, _ = store.complete(session_id, 1, mime_type="video/webm", chunk_count=1,
                                 size_bytes=len(recording), started_at_ms=900, ended_at_ms=1400)
    assert manifest["state"] == "pending"
    store.export(session_id, 1)
    clip = manager.completed_folder(session_id) / "clips" / "clip_001"
    assert store.status(session_id)["clips"][0]["state"] == "no_frames"
    assert {path.name for path in clip.iterdir()} == {"original.webm", "manifest.json"}
    store.add_chunk(session_id, 2, 0, recording)
    with pytest.raises(ClipError):
        store.complete(session_id, 2, mime_type="video/webm", chunk_count=1,
                       size_bytes=len(recording), started_at_ms=1300, ended_at_ms=1600)


def test_export_cannot_overlap_new_inference_session(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    app = create_app(manager)
    routes = endpoints(app)
    session_id = manager.start("Phone")["session_id"]
    manager.stop(session_id)
    store = app.state.clip_store
    recording = sample_webm(tmp_path / "sample.webm")
    store.add_chunk(session_id, 1, 0, recording)
    entered = threading.Event()
    release = threading.Event()
    original_export = store.export

    def blocked_export(sid, clip_id):
        entered.set()
        assert release.wait(2)
        original_export(sid, clip_id)

    store.export = blocked_export
    complete = routes["/api/sessions/{session_id}/clips/{clip_id}/complete"]
    complete(session_id, 1, ClipCompleteRequest(mime_type="video/webm", chunk_count=1,
                                                size_bytes=len(recording), started_at_ms=1000, ended_at_ms=1500))
    assert entered.wait(2)
    with pytest.raises(HTTPException) as busy:
        routes["/api/sessions"](StartRequest(device_name="Another Phone"))
    assert busy.value.status_code == 409
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            new = routes["/api/sessions"](StartRequest(device_name="Another Phone"))
            break
        except HTTPException:
            time.sleep(.02)
    assert new["session_id"] != session_id
    with pytest.raises(ClipError):
        store.add_chunk(session_id, 2, 0, recording)
    manager.stop(new["session_id"])


def test_daily_app_log_is_created_once(tmp_path):
    target = configure_app_logging(tmp_path)
    configure_app_logging(tmp_path)
    logging.getLogger("backend.app").info("clip logging check")
    assert target == tmp_path / "logs" / "app.log"
    assert target.read_text(encoding="utf-8").count("clip logging check") == 1


def test_mp4_camera_and_encoded_duration_limit(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    session_id = manager.start("Phone")["session_id"]
    success, encoded = cv2.imencode(".jpg", np.zeros((80, 100, 3), dtype=np.uint8))
    assert success
    frame_bytes = encoded.tobytes()
    manager.process(session_id, 1, 1100, cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR),
                    image_bytes=frame_bytes, save_live_frame=False)
    manager.stop(session_id)
    store = ClipStore(manager, max_jpeg_bytes=3_000_000)
    mp4 = tmp_path / "sample.mp4"
    subprocess.run([ffmpeg_executable(), "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=size=64x64:rate=10:duration=0.4", "-c:v", "libx264", str(mp4)],
                   check=True, capture_output=True)
    data = mp4.read_bytes()
    store.add_chunk(session_id, 1, 0, data)
    store.add_frame(session_id, 1, 1, 1100, frame_bytes)
    manifest, _ = store.complete(session_id, 1, mime_type="video/mp4", chunk_count=1,
                                 size_bytes=len(data), started_at_ms=1000, ended_at_ms=1500)
    assert manifest["original_path"].endswith("original.mp4")
    assert (manager.completed_folder(session_id) / manifest["original_path"]).read_bytes() == data
    store.export(session_id, 1)
    assert store.status(session_id)["clips"][0]["state"] == "ready"
    assert (manager.completed_folder(session_id) / "clips/clip_001/inference.mp4").is_file()
    long_video = tmp_path / "long.webm"
    subprocess.run([ffmpeg_executable(), "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=size=64x64:rate=1:duration=91", "-c:v", "libvpx", str(long_video)],
                   check=True, capture_output=True)
    too_long = long_video.read_bytes()
    session_id = manager.start("Long recording")["session_id"]
    manager.stop(session_id)
    store.add_chunk(session_id, 1, 0, too_long)
    with pytest.raises(ClipError, match="90초"):
        store.complete(session_id, 1, mime_type="video/webm", chunk_count=1,
                       size_bytes=len(too_long), started_at_ms=2000, ended_at_ms=92_000)


def test_frame_retry_recovers_uncommitted_files_after_restart(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    session_id = manager.start("Phone")["session_id"]
    success, encoded = cv2.imencode(".jpg", np.zeros((80, 100, 3), dtype=np.uint8))
    assert success
    data = encoded.tobytes()
    manager.process(session_id, 1, 1000, cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR),
                    image_bytes=data, save_live_frame=False)
    manager.stop(session_id)
    restarted = ClipStore(SessionManager(tmp_path, model_factory=FakeModels), max_jpeg_bytes=3_000_000)
    clip = restarted._clip(restarted._session(session_id), 1, create=True)
    orphan = clip / "frames" / "000001.jpg"
    orphan.write_bytes(b"incomplete")
    (clip / "masks" / "000001.png").write_bytes(b"incomplete")
    assert restarted.add_frame(session_id, 1, 1, 1000, data)["saved"]
    assert orphan.read_bytes() == data
    assert not (clip / "masks" / "000001.png").exists()
    assert json.loads(orphan.with_suffix(".json").read_text())["sha256"] == hashlib.sha256(data).hexdigest()


def test_failed_render_preserves_inputs_until_successful_retry(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    session_id = manager.start("Phone")["session_id"]
    frame = np.zeros((80, 100, 3), dtype=np.uint8)
    success, encoded = cv2.imencode(".jpg", frame)
    assert success
    data = encoded.tobytes()
    result = manager.process(session_id, 1, 1100, frame, image_bytes=data)
    manager.stop(session_id)
    store = ClipStore(manager, max_jpeg_bytes=3_000_000)
    recording = sample_webm(tmp_path / "sample.webm")
    store.add_chunk(session_id, 1, 0, recording)
    store.add_frame(session_id, 1, 1, 1100, data, result["walking"]["mask_png"])
    options = dict(mime_type="video/webm", chunk_count=1, size_bytes=len(recording),
                   started_at_ms=1000, ended_at_ms=1500)
    store.complete(session_id, 1, **options)
    with monkeypatch.context() as patch:
        def fail(*args):
            raise RuntimeError("simulated render failure")
        patch.setattr(store, "_draw", fail)
        store.export(session_id, 1)
    clip = manager.completed_folder(session_id) / "clips" / "clip_001"
    assert store.status(session_id)["clips"][0]["state"] == "failed"
    assert (clip / "frames" / "000001.jpg").read_bytes() == data
    assert (clip / "frames" / "000001.json").is_file()
    assert (clip / "masks" / "000001.png").is_file()
    assert (clip / "original.webm").read_bytes() == recording
    assert not list(clip.glob(".inference-*"))
    manifest, retry = store.complete(session_id, 1, **options)
    assert retry and manifest["state"] == "pending"
    store.export(session_id, 1)
    assert store.status(session_id)["clips"][0]["state"] == "ready"
    assert {path.name for path in clip.iterdir()} == {"original.webm", "inference.mp4", "manifest.json"}

    # Recover a crash after the final manifest was saved but before input cleanup.
    (clip / "frames").mkdir()
    (clip / "frames" / "000001.jpg").write_bytes(data)
    restarted = ClipStore(SessionManager(tmp_path, model_factory=FakeModels), max_jpeg_bytes=3_000_000)
    assert list(restarted.pending_exports()) == []
    assert not (clip / "frames").exists()


def test_session_events_share_one_log_and_keep_their_payloads(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    session_id = manager.start("Phone")["session_id"]
    manager.process(session_id, 1, 1000, np.zeros((80, 100, 3), dtype=np.uint8))
    manager.record_client_timings(session_id, [{"kind": "audio", "status": "started", "frame_id": 1}])
    manager.record_bus_events(session_id, [{"type": "gps_poll", "event_group": "spoofed"}])
    manager.record_video_event(session_id, "camera", "saved")
    manager.session["crossing_active"] = False
    manager.update_boarding(session_id, "arrive")
    manager.stop(session_id)
    folder = manager.completed_folder(session_id)
    assert {path.name for path in folder.iterdir()} == {"session.json", "results.jsonl", "events.jsonl", "provenance.json"}
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert [event["event_group"] for event in events] == ["client_timing", "bus", "recording", "boarding"]
    assert events[0]["kind"] == "audio" and events[0]["frame_id"] == 1
    assert events[1]["type"] == "gps_poll"
    assert events[2]["status"] == "saved"
    assert events[3]["action"] == "arrive"
