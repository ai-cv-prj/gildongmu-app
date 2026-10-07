"""The shared archive must isolate sources and publish only verified complete files."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Event

from fastapi.testclient import TestClient
import pytest

from result_hub.app import HubSettings, create_app


SESSION = "a" * 32
OTHER_SESSION = "b" * 32
TOKENS = {f"member{number}": f"upload-token-{number}-" + "x" * 32 for number in range(1, 5)}
VIEWER = ("team", "viewer-password-" + "y" * 24)


@pytest.fixture
def settings(tmp_path):
    return HubSettings(tmp_path / "archive", *VIEWER, TOKENS)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as value:
        yield value


def upload(client, path, data, source="member1", *, digest=None):
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False).encode()
    if isinstance(data, str):
        data = data.encode()
    return client.put(f"/api/ingest/{source}/{path}", content=data,
                      headers={"Authorization": f"Bearer {TOKENS[source]}",
                               "X-Content-SHA256": digest or hashlib.sha256(data).hexdigest()})


def session_json(session_id=SESSION, **extra):
    return {"session_id": session_id, "device_name": "테스트 폰", "note": "횡단보도",
            "started_at": "2026-10-07T05:00:00+00:00", "frame_count": 3, **extra}


def test_health_and_read_write_credentials_are_separate(client):
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/sessions", auth=("team", "wrong")).status_code == 401
    assert client.get("/api/sessions", headers={"Authorization": f"Bearer {TOKENS['member1']}"}).status_code == 401
    path = f"/api/ingest/member1/sessions/{SESSION}/session.json"
    assert client.put(path, auth=VIEWER).status_code == 401
    assert client.put(path, headers={"Authorization": f"Bearer {TOKENS['member2']}"}).status_code == 401
    assert client.get("/api/sessions", auth=VIEWER).json() == {"sessions": []}


def test_four_sources_with_same_session_id_remain_separate(client, settings):
    def send(source):
        response = upload(client, f"sessions/{SESSION}/session.json", session_json(note=source), source)
        assert response.status_code == 200, response.text
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(send, TOKENS))
    records = client.get("/api/sessions", auth=VIEWER).json()["sessions"]
    assert len(records) == 4
    assert {row["source_id"] for row in records} == set(TOKENS)
    assert len(client.get("/api/sessions?source_id=member2", auth=VIEWER).json()["sessions"]) == 1
    assert len(client.get("/api/sessions?q=횡단", auth=VIEWER).json()["sessions"]) == 0
    assert len(client.get("/api/sessions?q=member3", auth=VIEWER).json()["sessions"]) == 1
    assert len(client.get("/api/sources", auth=VIEWER).json()["sources"]) == 4
    with TestClient(create_app(settings)) as restarted:
        assert len(restarted.get("/api/sessions", auth=VIEWER).json()["sessions"]) == 4


def test_bad_checksum_and_invalid_metadata_leave_last_good_copy(client, settings):
    path = f"sessions/{SESSION}/session.json"
    assert upload(client, path, session_json()).status_code == 200
    assert upload(client, path, session_json(note="bad"), digest="0" * 64).status_code == 422
    assert upload(client, path, "{incomplete").status_code == 422
    assert upload(client, path, session_json(OTHER_SESSION)).status_code == 422
    assert upload(client, path, session_json(frame_count="three")).status_code == 422
    assert client.get(f"/api/sessions/member1/{SESSION}", auth=VIEWER).json()["session"]["note"] == "횡단보도"
    assert not list(settings.storage_dir.rglob(".upload-*"))


def test_head_fingerprint_and_missing_file_repair(client, settings):
    path = "logs/app.log"
    url = f"/api/ingest/member1/{path}"
    auth = {"Authorization": f"Bearer {TOKENS['member1']}"}
    assert client.head(url, headers=auth).status_code == 404
    assert upload(client, path, "old\n").status_code == 200
    first = client.head(url, headers=auth)
    assert first.headers["x-content-sha256"] == hashlib.sha256(b"old\n").hexdigest()
    target = settings.storage_dir / "sources/member1/logs/app.log"
    target.write_bytes(b"new content\n")
    assert client.head(url, headers=auth).headers["x-content-sha256"] == hashlib.sha256(b"new content\n").hexdigest()
    target.unlink()
    assert client.head(url, headers=auth).status_code == 404


def test_head_repair_does_not_cache_old_hash_with_replacement_metadata(client, settings, monkeypatch):
    path = "logs/app.log"
    target = client.app.state.archive.file("member1", path, create_parent=True)
    target.write_bytes(b"old file\n")  # Simulate an atomic rename without its index commit.
    hashed, replaced = Event(), Event()
    original_digest = hashlib.file_digest

    def pause_after_hash(stream, algorithm):
        digest = original_digest(stream, algorithm)
        hashed.set()
        assert replaced.wait(5)
        return digest

    monkeypatch.setattr(hashlib, "file_digest", pause_after_hash)
    url = f"/api/ingest/member1/{path}"
    auth = {"Authorization": f"Bearer {TOKENS['member1']}"}
    content = b"new complete file\n"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.head, url, headers=auth)
        try:
            assert hashed.wait(5)
            assert upload(client, path, content).status_code == 200
        finally:
            replaced.set()
        assert pending.result(timeout=5).status_code == 200

    response = client.head(url, headers=auth)
    assert response.headers["x-content-sha256"] == hashlib.sha256(content).hexdigest()
    assert int(response.headers["content-length"]) == len(content)


@pytest.mark.parametrize("path", [".env", "weights/model.pt", "logs/secrets.txt", "sessions/not-a-uuid/session.json",
                                  f"sessions/{SESSION}/frames/001.jpg", f"sessions/{SESSION}/clips/clip_001/../../.env",
                                  "logs/%2e%2e%2f.env", "logs/app.log/extra", "logs/app.log.2026-10-07.bak"])
def test_disallowed_paths_are_never_written(client, path):
    assert upload(client, path, b"secret").status_code in (400, 404)


def test_symlink_storage_is_rejected(client, settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    member = settings.storage_dir / "sources/member1"
    member.symlink_to(outside, target_is_directory=True)
    assert upload(client, "logs/app.log", "test").status_code == 400
    assert list(outside.iterdir()) == []


def test_limits_apply_with_and_without_content_length(tmp_path):
    settings = HubSettings(tmp_path, *VIEWER, TOKENS, max_upload_bytes=16)
    with TestClient(create_app(settings)) as client:
        assert upload(client, "logs/app.log", b"a" * 17).status_code == 413
        assert upload(client, "logs/app.log", b"old").status_code == 200
        response = client.put("/api/ingest/member1/logs/app.log", content=iter([b"a" * 10, b"b" * 10]),
                              headers={"Authorization": f"Bearer {TOKENS['member1']}", "X-Content-SHA256": "0" * 64})
        assert response.status_code == 413
        assert client.get("/api/files/member1/logs/app.log", auth=VIEWER).content == b"old"
    assert not list(tmp_path.rglob(".upload-*"))


def test_late_clips_video_range_and_download(client):
    base = f"sessions/{SESSION}"
    assert upload(client, base + "/session.json", session_json(ended_at="2026-10-07T05:01:00+00:00")).status_code == 200
    detail_url = f"/api/sessions/member1/{SESSION}"
    assert client.get(detail_url, auth=VIEWER).json()["clips"] == []
    clip = base + "/clips/clip_001"
    manifest = {"clip_id": 1, "state": "pending", "original_path": "https://evil.invalid/video"}
    assert upload(client, clip + "/original.webm", b"0123456789").status_code == 200
    assert upload(client, clip + "/manifest.json", manifest).status_code == 200
    pending = client.get(detail_url, auth=VIEWER).json()["clips"][0]
    assert pending["inference_url"] is None
    assert pending["original_url"].startswith("/api/files/")
    assert upload(client, clip + "/inference.mp4", b"abcdefghij").status_code == 200
    assert upload(client, clip + "/manifest.json", {**manifest, "state": "ready"}).status_code == 200
    ready = client.get(detail_url, auth=VIEWER).json()["clips"][0]
    ranged = client.get(ready["inference_url"], headers={"Range": "bytes=2-5"}, auth=VIEWER)
    assert ranged.status_code == 206
    assert ranged.content == b"cdef"
    assert ranged.headers["content-range"] == "bytes 2-5/10"
    assert client.get(ready["inference_url"]).status_code == 401
    response = client.get(ready["inference_url"] + "?download=true", auth=VIEWER)
    assert response.headers["content-disposition"].startswith("attachment")
    assert client.get("/api/sessions", auth=VIEWER).json()["sessions"][0]["ready_clip_count"] == 1


def test_log_preview_is_bounded_and_log_names_preserve_source(client):
    for source in ("member1", "member2"):
        assert upload(client, "logs/app.log", "\n".join(str(n) for n in range(1100)), source).status_code == 200
    preview = client.get("/api/preview/member1/logs/app.log?lines=3", auth=VIEWER).json()
    assert preview == {"text": "1097\n1098\n1099", "truncated": True}
    assert client.get("/api/preview/member1/logs/app.log?lines=999999", auth=VIEWER).status_code == 422
    assert len(client.get("/api/logs?source_id=member2", auth=VIEWER).json()["logs"]) == 1
    response = client.get("/api/files/member1/logs/app.log", auth=VIEWER)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_missing_or_shared_credentials_fail_closed(tmp_path, monkeypatch):
    for key in ("HUB_VIEWER_USERNAME", "HUB_VIEWER_PASSWORD", "HUB_SOURCE_TOKENS_JSON"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        create_app()
    with pytest.raises(ValueError):
        create_app(HubSettings(tmp_path, *VIEWER, {"member1": "x" * 32, "member2": "x" * 32}))
