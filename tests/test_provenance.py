"""Runtime labels preserve observed deployment identity without exporting secrets."""

import json
from types import SimpleNamespace

from backend import provenance
from backend.session import SessionManager


def test_snapshot_excludes_secrets_and_does_not_hash_weights(tmp_path, monkeypatch):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/app.py").write_text("print('version1')")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/app.yaml").write_text("setting: 1")
    (tmp_path / ".env").write_text("SECRET=must-not-export")
    (tmp_path / "configs/private.env").write_text("SECRET=must-not-export")
    (tmp_path / "weights").mkdir()
    weight = tmp_path / "weights/model.pt"
    weight.write_bytes(b"model-bytes-must-not-read")
    (tmp_path / "backend/linked.py").symlink_to(tmp_path / ".env")
    monkeypatch.setattr(provenance, "_git", lambda *args: None)
    original = type(weight).open
    def guarded(path, *args, **kwargs):
        assert path != weight, "large model contents must not be read"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(weight), "open", guarded)
    snapshot = provenance.collect_snapshot(tmp_path)
    assert snapshot["snapshot_basis"] == "server_process_initialization_disk_snapshot"
    assert snapshot["models"]["contents_hashed"] is False
    assert snapshot["models"]["files"][0]["path"] == "weights/model.pt"
    assert set(snapshot["source"]["file_sha256"]) == {"backend/app.py", "configs/app.yaml"}
    serialized = json.dumps(snapshot)
    assert "must-not-export" not in serialized
    assert "version1" not in serialized
    assert snapshot["git"] == {"commit": None, "dirty": None}
    before = snapshot["source"]["sha256"]
    (tmp_path / "backend/app.py").write_text("print('version2')")
    assert provenance.collect_snapshot(tmp_path)["source"]["sha256"] != before


def test_sessions_preserve_initial_snapshot_when_stopped(tmp_path, monkeypatch):
    snapshot = {"snapshot_basis": "server_process_initialization_disk_snapshot", "git": {"commit": "old"}}
    monkeypatch.setattr("backend.session.process_snapshot", lambda: snapshot)
    models = SimpleNamespace(reset=lambda: None)
    manager = SessionManager(tmp_path, model_factory=lambda: models)
    try:
        started = manager.start("Phone")
        folder = tmp_path / started["date"] / started["folder_name"]
        manager.stop(started["session_id"])
        assert json.loads((folder / "provenance.json").read_text()) == snapshot
        assert json.loads((folder / "session.json").read_text())["ended_at"]
    finally:
        manager.close()


def test_metadata_write_failure_does_not_break_test(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("read only")
    monkeypatch.setattr(type(tmp_path), "write_text", fail)
    provenance.write_snapshot(tmp_path, {"git": {"commit": "test"}})


def test_process_snapshot_is_cached_and_capture_failure_is_nonfatal(monkeypatch):
    provenance.process_snapshot.cache_clear()
    calls = []
    def capture(root):
        calls.append(root)
        return {"captured_at": "initial"}
    monkeypatch.setattr(provenance, "collect_snapshot", capture)
    try:
        assert provenance.process_snapshot() == provenance.process_snapshot()
        assert len(calls) == 1
        provenance.process_snapshot.cache_clear()
        def fail(root):
            raise OSError("unavailable")
        monkeypatch.setattr(provenance, "collect_snapshot", fail)
        assert provenance.process_snapshot() is None
    finally:
        provenance.process_snapshot.cache_clear()
