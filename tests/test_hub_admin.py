"""Admin cleanup must preserve source data, enforce roles, and survive syncing."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from threading import Event

from fastapi.testclient import TestClient
import pytest

from result_hub.app import HubSettings, create_app
from scripts.sync_results import ResultSync


SID = "a" * 32
VIEWER = ("team", "read-only-viewer-password")
ADMIN = "private-administrator-password"
TOKENS = {"member1": "first-source-token-" + "1" * 24,
          "member2": "second-source-token-" + "2" * 24}
ADMIN_HEADERS = {"X-Hub-Admin-Password": ADMIN}


@pytest.fixture
def settings(tmp_path):
    return HubSettings(tmp_path / "hub", *VIEWER, TOKENS, admin_password=ADMIN)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as value:
        yield value


def upload(client, path, data, source="member1"):
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    return client.put(f"/api/ingest/{source}/{path}", content=data,
                      headers={"Authorization": f"Bearer {TOKENS[source]}",
                               "X-Content-SHA256": hashlib.sha256(data).hexdigest()})


def seed_session(client, source="member1"):
    files = {
        f"sessions/{SID}/session.json": {"session_id": SID, "device_name": "Test phone", "frame_count": 1},
        f"sessions/{SID}/events.jsonl": b'{"event":"test"}\n',
        f"sessions/{SID}/clips/clip_001/original.webm": b"recorded video",
        f"sessions/{SID}/clips/clip_001/inference.mp4": b"rendered video",
        f"sessions/{SID}/clips/clip_001/manifest.json": {"clip_id": 1, "state": "ready"},
    }
    for path, data in files.items():
        assert upload(client, path, data, source).status_code == 200
    return files


def remove_session(client, source="member1"):
    return client.delete(f"/api/admin/sessions/{source}/{SID}", auth=VIEWER, headers=ADMIN_HEADERS)


def test_viewer_and_upload_tokens_cannot_manage_records(client):
    seed_session(client)
    url = f"/api/admin/sessions/member1/{SID}"
    assert client.get("/api/capabilities", auth=VIEWER).json() == {"admin_enabled": True}
    assert client.get("/api/capabilities").status_code == 401
    assert client.delete(url, auth=VIEWER).status_code == 403
    assert client.delete(url, auth=VIEWER, headers={"X-Hub-Admin-Password": TOKENS["member1"]}).status_code == 403
    assert client.delete(url, headers=ADMIN_HEADERS).status_code == 401
    assert client.delete(url, headers={**ADMIN_HEADERS, "Authorization": f"Bearer {TOKENS['member1']}"}).status_code == 401
    assert client.delete(url, auth=VIEWER, headers={**ADMIN_HEADERS, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/admin/login", auth=VIEWER, headers=ADMIN_HEADERS).json() == {"ok": True}
    assert client.get("/api/admin/trash", auth=VIEWER).status_code == 403
    assert client.get(f"/api/sessions/member1/{SID}", auth=VIEWER).status_code == 200


def test_optional_admin_configuration_preserves_read_only_deployments(settings):
    with TestClient(create_app(replace(settings, admin_password=""))) as client:
        assert client.get("/api/capabilities", auth=VIEWER).json() == {"admin_enabled": False}
        assert client.post("/api/admin/login", auth=VIEWER, headers=ADMIN_HEADERS).status_code == 403
    for password in ("short", VIEWER[1], TOKENS["member1"], "has spaces in password", "비밀번호-" * 8):
        with pytest.raises(ValueError, match="HUB_ADMIN_PASSWORD"):
            create_app(replace(settings, admin_password=password))


def test_delete_restore_and_restart_preserve_other_source_and_block_reupload(client, settings):
    files = seed_session(client)
    seed_session(client, "member2")
    response = remove_session(client)
    assert response.status_code == 200, response.text
    item = response.json()["item"]
    assert item["kind"] == "session" and item["file_count"] == len(files)
    assert remove_session(client).json()["item"]["id"] == item["id"]
    assert client.get(f"/api/sessions/member1/{SID}", auth=VIEWER).status_code == 404
    assert client.get(f"/api/sessions/member2/{SID}", auth=VIEWER).status_code == 200
    assert {s["source_id"] for s in client.get("/api/sessions", auth=VIEWER).json()["sessions"]} == {"member2"}
    for path in files:
        assert client.get(f"/api/files/member1/{path}", auth=VIEWER).status_code == 404
    assert client.get(f"/api/preview/member1/sessions/{SID}/events.jsonl", auth=VIEWER).status_code == 404
    late = f"sessions/{SID}/clips/clip_002/original.webm"
    with TestClient(create_app(settings)) as restarted:
        for path in [*files, late]:
            head = restarted.head(f"/api/ingest/member1/{path}",
                                  headers={"Authorization": f"Bearer {TOKENS['member1']}"})
            assert head.status_code == 410
            assert head.headers["x-hub-deleted"] == "true"
            put = upload(restarted, path, b"do not recreate")
            assert put.status_code == 410 and put.headers["x-hub-deleted"] == "true"
        trash = restarted.get("/api/admin/trash", auth=VIEWER, headers=ADMIN_HEADERS).json()["items"]
        assert [entry["id"] for entry in trash] == [item["id"]]
        result = restarted.post(f"/api/admin/trash/{item['id']}/restore", auth=VIEWER, headers=ADMIN_HEADERS)
        assert result.status_code == 200, result.text
        assert restarted.get("/api/admin/trash", auth=VIEWER, headers=ADMIN_HEADERS).json() == {"items": []}
        for path, data in files.items():
            original = json.dumps(data).encode() if isinstance(data, dict) else data
            assert restarted.get(f"/api/files/member1/{path}", auth=VIEWER).content == original
        assert upload(restarted, late, b"new clip after restore").status_code == 200


def test_only_archived_logs_can_be_removed(client):
    for path in ("logs/app.log", "logs/app.log.2026-10-06"):
        assert upload(client, path, b"test log\n").status_code == 200
    base = "/api/admin/logs/member1/"
    assert client.delete(base + "logs/app.log", auth=VIEWER, headers=ADMIN_HEADERS).status_code == 400
    removed = client.delete(base + "logs/app.log.2026-10-06", auth=VIEWER, headers=ADMIN_HEADERS)
    assert removed.status_code == 200
    assert [row["path"] for row in client.get("/api/logs", auth=VIEWER).json()["logs"]] == ["logs/app.log"]
    assert upload(client, "logs/app.log", b"test log\nnew log\n").status_code == 200
    assert upload(client, "logs/app.log.2026-10-06", b"test log\n").status_code == 410
    item_id = removed.json()["item"]["id"]
    assert client.post(f"/api/admin/trash/{item_id}/restore", auth=VIEWER, headers=ADMIN_HEADERS).status_code == 200
    assert len(client.get("/api/logs", auth=VIEWER).json()["logs"]) == 2


def test_sync_skips_deleted_records_and_resumes_after_restore(client, tmp_path):
    folder = tmp_path / "local" / "test-session"
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"session_id": SID, "frame_count": 1}))
    (folder / "results.jsonl").write_bytes(b'{"frame_id":1}\n')
    sync = ResultSync(folder.parent, "https://hub.example", "member1", TOKENS["member1"], client=client)
    assert sync.run_once().uploaded == 2
    item = remove_session(client).json()["item"]
    skipped = sync.run_once()
    assert skipped.failed == 0 and skipped.uploaded == 0
    assert client.get("/api/sessions", auth=VIEWER).json() == {"sessions": []}
    assert (folder / "results.jsonl").read_bytes() == b'{"frame_id":1}\n'
    restored = client.post(f"/api/admin/trash/{item['id']}/restore", auth=VIEWER, headers=ADMIN_HEADERS)
    assert restored.status_code == 200
    result = sync.run_once()
    assert result.failed == 0 and result.unchanged == 2


def test_delete_waits_for_inflight_upload_and_cannot_be_undone_by_it(client, monkeypatch):
    seed_session(client)
    publishing, release = Event(), Event()
    archive = client.app.state.archive
    record = archive.record
    path = f"sessions/{SID}/events.jsonl"

    def blocked_record(source_id, relative, digest, **kwargs):
        if relative == path:
            publishing.set()
            assert release.wait(5)
        return record(source_id, relative, digest, **kwargs)

    monkeypatch.setattr(archive, "record", blocked_record)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(upload, client, path, b"new complete log\n")
        assert publishing.wait(5)
        deletion = pool.submit(remove_session, client)
        try:
            assert not deletion.done()
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
        assert deletion.result(timeout=5).status_code == 200
    assert client.get("/api/sessions", auth=VIEWER).json() == {"sessions": []}
    assert not list(archive.files.rglob("events.jsonl"))


def test_head_repair_cannot_reindex_a_deleted_record(client, monkeypatch):
    seed_session(client)
    archive = client.app.state.archive
    path = f"sessions/{SID}/events.jsonl"
    archive.file("member1", path).write_bytes(b"updated log\n")
    hashing, release = Event(), Event()
    file_digest = hashlib.file_digest

    def blocked_digest(stream, algorithm):
        digest = file_digest(stream, algorithm)
        if not hashing.is_set():
            hashing.set()
            assert release.wait(5)
        return digest

    monkeypatch.setattr(hashlib, "file_digest", blocked_digest)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.head, f"/api/ingest/member1/{path}",
                              headers={"Authorization": f"Bearer {TOKENS['member1']}"})
        assert hashing.wait(5)
        try:
            assert remove_session(client).status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 410
    assert archive.entries("member1") == []


@pytest.mark.parametrize("path", [
    "/api/admin/sessions/member1/not-a-session",
    "/api/admin/logs/member1/logs/secrets.txt",
    f"/api/admin/logs/member1/sessions/{SID}/events.jsonl",
    "/api/admin/trash/not-a-trash-id/restore",
])
def test_invalid_delete_paths_do_not_touch_stored_data(client, path):
    seed_session(client)
    method = client.post if path.endswith("/restore") else client.delete
    assert method(path, auth=VIEWER, headers=ADMIN_HEADERS).status_code in (400, 404)
    assert client.get(f"/api/sessions/member1/{SID}", auth=VIEWER).status_code == 200
