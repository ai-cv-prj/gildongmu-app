"""Client failures remain inspectable after stop/restart without raw payloads."""

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.diagnostics import RequestCorrelationMiddleware
from backend.session import SessionManager
from test_bus_api import FakeBus
from test_realtime_app import FakeModels


def event(session_id=None, **extra):
    return {"event_id": "event-1", "type": "request_failed", "session_id": session_id,
            "occurred_at_ms": 1791338032902, "request_id": "frame-1439-attempt-1",
            "method": "POST", "path": "/api/sessions/test/frames", "http_status": 0,
            "elapsed_ms": 152.5, "error_name": "TypeError", "error_message": "Failed to fetch",
            **extra}


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_diagnostics_persist_before_session_while_active_after_stop_and_restart(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    global_log = tmp_path / "logs" / "client-events.jsonl"
    with TestClient(create_app(manager)) as client:
        assert client.post("/api/client-events", json={"events": [event()]}).json() == {"accepted": 1}
        started = client.post("/api/sessions", json={"device_name": "Phone"}).json()
        session_id = started["session_id"]
        folder = tmp_path / started["date"] / started["folder_name"]
        client.post("/api/client-events", json={"events": [event(session_id, event_id="active")]}).raise_for_status()
        client.post("/api/sessions/stop", json={"session_id": session_id}).raise_for_status()
        client.post("/api/client-events", json={"events": [event(session_id, event_id="stopped")]}).raise_for_status()
        unknown = "a" * 32
        client.post("/api/client-events", json={"events": [event(unknown, event_id="unknown")]}).raise_for_status()
        assert len(records(global_log)) == 4
        assert [r["event_id"] for r in records(folder / "events.jsonl")] == ["active", "stopped"]
    restarted = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    with TestClient(create_app(restarted)) as client:
        client.post("/api/client-events", json={"events": [event(session_id, event_id="restarted")]}).raise_for_status()
    saved = records(folder / "events.jsonl")
    assert [r["event_id"] for r in saved] == ["active", "stopped", "restarted"]
    assert all(r["event_group"] == "client_diagnostic" and r["server_at"].endswith("+00:00") for r in saved)
    assert records(global_log)[-1] == saved[-1]


def test_diagnostics_resolve_session_manifest_after_unclean_restart(tmp_path):
    old = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    started = old.start("Phone")
    restarted = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    with TestClient(create_app(restarted)) as client:
        response = client.post("/api/client-events", json={"events": [event(started["session_id"])]})
        assert response.status_code == 200
    folder = tmp_path / started["date"] / started["folder_name"]
    assert records(folder / "events.jsonl")[0]["session_id"] == started["session_id"]


def test_diagnostics_sanitize_metadata_and_reject_entire_bad_batch(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    with TestClient(create_app(manager)) as client:
        noisy = event(path="https://example.org/api/clips?latitude=37&token=secret#fragment",
                      error_message="Failed https://example.org/api/clips?token=secret\nagain",
                      headers={"Authorization": "Bearer secret"}, body="secret", latitude=37,
                      server_at="fake", event_group="fake", expected_clip_ids=[1, 2],
                      screen="x" * 200, user_agent="u" * 400,
                      monotonic_clock_ms=10.5, time_origin_ms=1791338032000, connection_lost=True)
        assert client.post("/api/client-events", json={"events": [noisy]}).status_code == 200
        log_path = tmp_path / "logs/client-events.jsonl"
        saved = records(log_path)[0]
        assert saved["path"] == "/api/clips"
        assert "secret" not in log_path.read_text()
        assert not {"headers", "body", "latitude"} & saved.keys()
        assert saved["event_group"] == "client_diagnostic"
        assert len(saved["screen"]) == 120 and len(saved["user_agent"]) == 300
        assert saved["expected_clip_ids"] == [1, 2]
        assert saved["monotonic_clock_ms"] == 10.5 and saved["connection_lost"] is True
        before = log_path.read_bytes()
        malformed = [None, {}, {"events": []}, {"events": [event()] * 26},
                     {"events": [event(), event(event_id="contains a newline\n")]},
                     {"events": [event(session_id="../../bad")]},
                     {"events": [event(occurred_at_ms=True)]},
                     {"events": [event(expected_clip_ids=[1] * 101)]},
                     {"events": [event(expected_clip_ids=[True])]}]
        for payload in malformed:
            assert client.post("/api/client-events", content=json.dumps(payload)).status_code == 422
        for payload in (b'{"events": [NaN]}', b'{"events": [1e999]}', b'{', b'\xff'):
            assert client.post("/api/client-events", content=payload).status_code == 422
        assert client.post("/api/client-events", content=b" " * (64 * 1024 + 1)).status_code == 413
        assert log_path.read_bytes() == before


def test_request_ids_correlate_success_rejection_and_unhandled_error_without_queries(tmp_path, caplog):
    manager = SessionManager(tmp_path, model_factory=FakeModels, bus_recognizer=FakeBus())
    app = create_app(manager)

    @app.get("/api/test-error")
    def fail():
        raise RuntimeError("exception detail must not reach the response")

    with TestClient(app, raise_server_exceptions=False) as client:
        caplog.set_level(logging.INFO, logger="backend.diagnostics")
        response = client.get("/api/health?token=supersecret", headers={"X-Request-ID": "test-123", "CF-Ray": "ray-ICN"})
        assert response.headers["x-request-id"] == "test-123"
        messages = [r.message for r in caplog.records if r.name == "backend.diagnostics"]
        assert any("request_received request_id=test-123" in m and "cf_ray=ray-ICN" in m for m in messages)
        assert any("response_emitted request_id=test-123" in m and "status=200" in m for m in messages)
        assert all("supersecret" not in m and "?token" not in m for m in messages)
        rejected = client.post("/api/sessions", json={}, headers={"X-Request-ID": "invalid with spaces"})
        assert rejected.status_code == 422
        assert len(rejected.headers["x-request-id"]) == 32
        failed = client.get("/api/test-error", headers={"X-Request-ID": "crashed-456"})
        assert failed.status_code == 500 and failed.headers["x-request-id"] == "crashed-456"
        assert failed.text == "Internal Server Error"
        assert any("request_failed request_id=crashed-456" in r.message and "error_name=RuntimeError" in r.message for r in caplog.records)
        assert any("response_emitted request_id=crashed-456" in r.message and "status=500" in r.message for r in caplog.records)
        assert not any("request_incomplete request_id=crashed-456" in r.message for r in caplog.records)


def test_correlation_observes_disconnect_and_preserves_cancellation(caplog):
    async def cancelled(scope, receive, send):
        assert (await receive())["type"] == "http.disconnect"
        raise asyncio.CancelledError()

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise AssertionError("must not emit a response")

    caplog.set_level(logging.INFO, logger="backend.diagnostics")
    scope = {"type": "http", "path": "/api/frames", "method": "POST", "headers": []}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(RequestCorrelationMiddleware(cancelled)(scope, receive, send))
    assert any("request_disconnected" in r.message for r in caplog.records)
    assert any("error_name=CancelledError" in r.message and "disconnected=True" in r.message for r in caplog.records)
