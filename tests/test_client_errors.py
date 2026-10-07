"""Network diagnostics remain writable when there is no active camera session."""
import json

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.client_errors import ClientErrorStore, ClientFetchError
from backend.session import SessionManager


def record(**fields):
    return {"event_id": "error-12345678", "request_id": "request-12345678",
            "occurred_at_ms": 1791339525269, "path": "/api/sessions/stop",
            "method": "POST", "phase": "fetch", "error_name": "TypeError",
            "message": "Failed to fetch", "duration_ms": 200, **fields}


def test_fetch_errors_persist_without_session_and_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_SOURCE_ID", "member-test")
    monkeypatch.setenv("HUB_SOURCE_TOKEN", "must-not-be-exposed")
    app = create_app(SessionManager(tmp_path))
    client = TestClient(app)
    public = client.get("/api/config", headers={"X-Client-Request-ID": "request-12345678"})
    assert public.headers["x-hub-source-id"] == "member-test"
    assert "must-not-be-exposed" not in public.text + str(public.headers)
    payload = {"records": [record(session_id="finished-session", source_id="client-claim")]}
    assert client.post("/api/client-errors", json=payload).json() == {"saved_ids": ["error-12345678"]}
    restarted = TestClient(create_app(SessionManager(tmp_path)))
    assert restarted.post("/api/client-errors", json=payload).status_code == 200
    files = list((tmp_path / "logs").glob("client-errors-*.jsonl"))
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["source_id"] == "member-test"
    assert rows[0]["client_source_id"] == "client-claim"
    assert rows[0]["session_id"] == "finished-session"
    assert rows[0]["server_at"]


def test_invalid_fetch_batch_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_SOURCE_ID", "member-test")
    client = TestClient(create_app(SessionManager(tmp_path)))
    bad = {"records": [record(), record(event_id="error-87654321", message="x" * 501)]}
    assert client.post("/api/client-errors", json=bad).status_code == 422
    assert not list((tmp_path / "logs").glob("client-errors-*.jsonl"))
    assert client.post("/api/client-errors", json={"records": []}).status_code == 422
    assert client.post("/api/client-errors", json={"records": [record(path="/api/foo?token=secret")]}).status_code == 422


def test_duplicate_ids_in_same_batch_are_written_once(tmp_path):
    store = ClientErrorStore(tmp_path, "member-test")
    item = ClientFetchError.model_validate(record())
    store.append([item, item])
    files = list((tmp_path / "logs").glob("client-errors-*.jsonl"))
    assert len(files[0].read_text().splitlines()) == 1
