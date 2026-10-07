"""The uploader observes files without importing or pausing GPU inference."""

import hashlib
import json

import httpx
import pytest

from scripts import sync_results as module
from scripts.sync_results import CHUNK_SIZE, DeferredFile, ResultSync, snapshot_file, validate_url


class Hub:
    def __init__(self):
        self.files = {}
        self.requests = []
        self.offline = False
        self.fail_paths = set()
        self.deleted_paths = set()
        self.delete_on_put = set()

    def handle(self, request):
        assert request.headers["Authorization"] == "Bearer test-secret"
        self.requests.append((request.method, request.url.path))
        if self.offline:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path in self.fail_paths:
            return httpx.Response(503)
        if (request.url.path in self.deleted_paths
                or request.method == "PUT" and request.url.path in self.delete_on_put):
            return httpx.Response(410, headers={"X-Hub-Deleted": "true"})
        if request.method == "HEAD":
            content = self.files.get(request.url.path)
            if content is None:
                return httpx.Response(404)
            return httpx.Response(200, headers={"X-Content-SHA256": hashlib.sha256(content).hexdigest()})
        assert request.method == "PUT"
        content = request.read()
        digest = hashlib.sha256(content).hexdigest()
        assert len(content) == int(request.headers["Content-Length"])
        assert digest == request.headers["X-Content-SHA256"]
        self.files[request.url.path] = content
        return httpx.Response(200, json={"ok": True, "sha256": digest, "size": len(content)})


@pytest.fixture
def setup_sync(tmp_path):
    hub = Hub()
    with httpx.Client(transport=httpx.MockTransport(hub.handle)) as client:
        sync = ResultSync(tmp_path, "https://hub.example", "alice", "test-secret", client=client)
        yield tmp_path, hub, sync


def session(root, sid="a" * 32, *, ended=True, folder="Phone", alternate_id=False):
    directory = root / "20261007" / folder
    directory.mkdir(parents=True, exist_ok=True)
    document = {"session_id" if alternate_id else "id": sid, "device_name": folder}
    if ended:
        document["ended_at"] = "2026-10-07T00:00:00+00:00"
    (directory / "session.json").write_text(json.dumps(document))
    return directory


def clip(folder, state="pending"):
    directory = folder / "clips" / "clip_001"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "original.webm").write_bytes(b"original")
    (directory / "manifest.json").write_text(json.dumps({"clip_id": 1, "state": state}))
    return directory


def key(relative, source="alice"):
    return f"/api/ingest/{source}/{relative}"


def test_late_ready_video_arrives_after_session_ended(setup_sync):
    root, hub, sync = setup_sync
    folder = session(root)
    sync.run_once()
    assert len(hub.files) == 1

    directory = clip(folder)
    (directory / "inference.mp4").write_bytes(b"incomplete")
    sync.run_once()
    prefix = f"sessions/{'a' * 32}/clips/clip_001"
    assert key(f"{prefix}/original.webm") in hub.files
    assert key(f"{prefix}/inference.mp4") not in hub.files
    assert json.loads(hub.files[key(f"{prefix}/manifest.json")])["state"] == "pending"

    (directory / "inference.mp4").write_bytes(b"final video")
    (directory / "manifest.json").write_text('{"clip_id": 1, "state": "ready"}')
    hub.requests.clear()
    sync.run_once()
    assert hub.files[key(f"{prefix}/inference.mp4")] == b"final video"
    assert json.loads(hub.files[key(f"{prefix}/manifest.json")])["state"] == "ready"
    puts = [path for method, path in hub.requests if method == "PUT"]
    assert puts.index(key(f"{prefix}/inference.mp4")) < puts.index(key(f"{prefix}/manifest.json"))


def test_network_failure_retries_without_losing_results(setup_sync, caplog):
    root, hub, sync = setup_sync
    session(root)
    hub.offline = True
    assert sync.run_once().failed == 1
    assert not hub.files
    assert "test-secret" not in caplog.text
    hub.offline = False
    assert sync.run_once().uploaded == 1


def test_distinct_sessions_and_sources_keep_their_own_files(setup_sync):
    root, hub, sync = setup_sync
    first = session(root, "a" * 32)
    second = session(root, "b" * 32, folder="Other phone", alternate_id=True)
    (first / "results.jsonl").write_bytes(b'{"frame_id": 1}\n')
    (second / "results.jsonl").write_bytes(b'{"frame_id": 2}\n')
    assert sync.run_once().uploaded == 4
    other_sync = ResultSync(root, "https://hub.example", "bob", "test-secret", client=sync.client)
    assert other_sync.run_once().uploaded == 4
    for source in ("alice", "bob"):
        assert hub.files[key(f"sessions/{'a' * 32}/results.jsonl", source)] == b'{"frame_id": 1}\n'
        assert hub.files[key(f"sessions/{'b' * 32}/results.jsonl", source)] == b'{"frame_id": 2}\n'


def test_hash_dedup_still_checks_hub_and_restores_missing_video(setup_sync, monkeypatch):
    root, hub, sync = setup_sync
    folder = session(root)
    clip(folder, "no_frames")
    first = sync.run_once()
    assert first.uploaded == 3
    original_snapshot = module.snapshot_file
    binary_reads = []

    def tracked_snapshot(root, path, kind):
        if kind == "binary":
            binary_reads.append(path)
        return original_snapshot(root, path, kind)

    monkeypatch.setattr(module, "snapshot_file", tracked_snapshot)
    hub.requests.clear()
    assert sync.run_once().unchanged == 3
    assert not binary_reads
    assert all(method == "HEAD" for method, _ in hub.requests)
    video_key = key(f"sessions/{'a' * 32}/clips/clip_001/original.webm")
    del hub.files[video_key]
    assert sync.run_once().uploaded == 1
    assert len(binary_reads) == 1
    assert hub.files[video_key] == b"original"


def test_ready_manifest_waits_for_successful_video_transfer(setup_sync):
    root, hub, sync = setup_sync
    directory = clip(session(root), "ready")
    (directory / "inference.mp4").write_bytes(b"video")
    prefix = f"sessions/{'a' * 32}/clips/clip_001"
    hub.fail_paths.add(key(f"{prefix}/inference.mp4"))
    assert sync.run_once().failed == 1
    assert key(f"{prefix}/manifest.json") not in hub.files
    hub.fail_paths.clear()
    sync.run_once()
    assert key(f"{prefix}/manifest.json") in hub.files


def test_symlinks_unsafe_session_ids_and_unlisted_files_are_skipped(setup_sync, tmp_path):
    root, hub, sync = setup_sync
    folder = session(root)
    outside = root / "outside.txt"
    outside.write_bytes(b"private")
    (folder / "results.jsonl").symlink_to(outside)
    (folder / "provenance.json").symlink_to(outside)
    (root / "linked").symlink_to(folder, target_is_directory=True)
    (folder / "frames").mkdir()
    session(folder / "frames", "c" * 32)
    session(root, "../escape", folder="Invalid")
    (folder / "secrets.env").write_text("secret")
    directory = clip(folder)
    (directory / "original.webm").unlink()
    (directory / "original.webm").symlink_to(outside)
    result = sync.run_once()
    assert result.uploaded == 1
    assert list(hub.files) == [key(f"sessions/{'a' * 32}/session.json")]
    with pytest.raises(DeferredFile):
        snapshot_file(root, root / ".." / "anything", "binary")


def test_active_logs_and_jsonl_upload_only_complete_lines(setup_sync):
    root, hub, sync = setup_sync
    folder = session(root, ended=False)
    (folder / "results.jsonl").write_bytes(b'{"frame_id":1}\n{"frame_')
    logs = root / "logs"
    logs.mkdir()
    (logs / "app.log").write_bytes(b"started\nin pro")
    (logs / "app.log.2026-10-06").write_bytes(b"yesterday\n")
    (logs / "app.log.secret").write_bytes(b"do not send\n")
    sync.run_once()
    assert hub.files[key("logs/app.log")] == b"started\n"
    assert hub.files[key(f"sessions/{'a' * 32}/results.jsonl")] == b'{"frame_id":1}\n'
    assert key("logs/app.log.secret") not in hub.files
    with (logs / "app.log").open("ab") as file:
        file.write(b"gress\n")
    with (folder / "results.jsonl").open("ab") as file:
        file.write(b'id":2}\n')
    assert sync.run_once().uploaded == 2
    assert hub.files[key("logs/app.log")] == b"started\nin progress\n"
    assert hub.files[key(f"sessions/{'a' * 32}/results.jsonl")] == b'{"frame_id":1}\n{"frame_id":2}\n'


def test_half_written_json_retries_next_cycle(setup_sync):
    root, hub, sync = setup_sync
    folder = session(root)
    original = (folder / "session.json").read_bytes()
    (folder / "session.json").write_bytes(b'{"id":')
    assert sync.run_once().deferred == 1
    assert not hub.files
    (folder / "session.json").write_bytes(original)
    assert sync.run_once().uploaded == 1


def test_video_snapshot_never_reads_unbounded_data(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    content = b"v" * (CHUNK_SIZE * 3 + 13)
    path.write_bytes(content)
    original_open = module.open_source
    read_sizes = []

    class BoundedReader:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self, size):
            assert 0 < size <= CHUNK_SIZE
            read_sizes.append(size)
            return self.file.read(size)

    monkeypatch.setattr(module, "open_source", lambda root, path: BoundedReader(original_open(root, path)))
    snapshot = snapshot_file(tmp_path, path, "binary")
    try:
        assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
        assert snapshot.size == len(content)
        assert len(read_sizes) == 4
    finally:
        snapshot.close()


def test_append_during_snapshot_is_limited_to_initial_complete_prefix(tmp_path, monkeypatch):
    path = tmp_path / "app.log"
    path.write_bytes(b"old line\n")
    original_open = module.open_source

    class GrowingReader:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self, size):
            content = self.file.read(size)
            with path.open("ab") as file:
                file.write(b"new line\n")
            return content

    monkeypatch.setattr(module, "open_source", lambda root, path: GrowingReader(original_open(root, path)))
    snapshot = snapshot_file(tmp_path, path, "lines")
    try:
        assert snapshot.file.read() == b"old line\n"
    finally:
        snapshot.close()


@pytest.mark.parametrize("address", ["https://hub.example", "http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"])
def test_allowed_hub_addresses(address):
    assert validate_url(address) == address


@pytest.mark.parametrize("address", ["http://192.168.0.5:8080", "https://user:secret@example.com", "https://hub.example?token=secret", "https://hub.example#secret", "ftp://example.com", "https://example.com:invalid"])
def test_unsafe_hub_addresses_are_rejected(address):
    with pytest.raises(ValueError):
        validate_url(address)


def test_private_http_requires_explicit_option():
    assert validate_url("http://192.168.0.5:8080/", allow_http=True) == "http://192.168.0.5:8080"


def test_redirect_never_receives_credentials(tmp_path):
    session(tmp_path)
    requests = []

    def redirect(request):
        requests.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://other.example/steal"})

    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        sync = ResultSync(tmp_path, "https://hub.example", "alice", "test-secret", client=client)
        assert sync.run_once().failed == 1
    assert requests == ["hub.example"]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-number"])
def test_interval_must_be_positive_and_finite(value):
    with pytest.raises(module.argparse.ArgumentTypeError):
        module.positive_interval(value)


def test_once_exit_code_reports_failed_transfer(tmp_path, monkeypatch):
    session(tmp_path)
    monkeypatch.setenv("HUB_URL", "https://hub.example")
    monkeypatch.setenv("HUB_SOURCE_ID", "alice")
    monkeypatch.setenv("HUB_SOURCE_TOKEN", "test-secret")
    hub = Hub()
    hub.offline = True
    client_type = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(hub.handle)))
    assert module.main(["--once", "--output-dir", str(tmp_path)]) == 1
    hub.offline = False
    assert module.main(["--once", "--output-dir", str(tmp_path)]) == 0


def test_offline_hub_stops_after_one_connection_attempt_then_retries(setup_sync):
    root, hub, sync = setup_sync
    for number in range(8):
        folder = session(root, sid=f"{number:032x}", folder=f"Phone-{number}")
        (folder / "results.jsonl").write_bytes(b'{"frame_id":1}\n')
    hub.offline = True
    assert sync.run_once().failed == 1
    assert len(hub.requests) == 1
    hub.offline = False
    assert sync.run_once().uploaded == 16


def test_deleted_session_files_are_skipped_without_reupload_and_resume_after_restore(setup_sync):
    root, hub, sync = setup_sync
    folder = session(root)
    directory = clip(folder, "ready")
    (directory / "inference.mp4").write_bytes(b"rendered video")
    (folder / "results.jsonl").write_bytes(b'{"frame_id": 1}\n')
    assert sync.run_once().uploaded == 5
    hub.deleted_paths.update(hub.files)
    hub.files.clear()
    hub.requests.clear()

    result = sync.run_once()
    assert result.deleted == 5
    assert result.failed == result.uploaded == result.unchanged == 0
    assert all(method == "HEAD" for method, _ in hub.requests)
    assert not hub.files
    assert (directory / "original.webm").read_bytes() == b"original"

    # The same long-running uploader must notice restoration next cycle.
    hub.deleted_paths.clear()
    result = sync.run_once()
    assert result.uploaded == 5
    assert result.deleted == result.failed == 0


def test_deletion_between_head_and_put_is_not_a_failure(setup_sync):
    root, hub, sync = setup_sync
    session(root)
    hub.delete_on_put.add(key(f"sessions/{'a' * 32}/session.json"))
    result = sync.run_once()
    assert result.deleted == 1
    assert result.failed == result.uploaded == 0
    assert not hub.files
    assert [method for method, _ in hub.requests] == ["HEAD", "PUT"]


def test_deleted_rotated_log_does_not_block_current_log(setup_sync):
    root, hub, sync = setup_sync
    logs = root / "logs"
    logs.mkdir()
    (logs / "app.log").write_bytes(b"current log\n")
    (logs / "app.log.2026-10-06").write_bytes(b"old log\n")
    hub.deleted_paths.add(key("logs/app.log.2026-10-06"))
    result = sync.run_once()
    assert result.deleted == result.uploaded == 1
    assert result.failed == 0
    assert hub.files == {key("logs/app.log"): b"current log\n"}
    assert (logs / "app.log.2026-10-06").read_bytes() == b"old log\n"


@pytest.mark.parametrize("method", ["HEAD", "PUT"])
@pytest.mark.parametrize("headers", [{}, {"X-Hub-Deleted": "false"}])
def test_unmarked_gone_response_remains_a_transfer_failure(tmp_path, method, headers):
    session(tmp_path)

    def gone(request):
        if request.method == method:
            return httpx.Response(410, headers=headers)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(gone)) as client:
        sync = ResultSync(tmp_path, "https://hub.example", "alice", "test-secret", client=client)
        result = sync.run_once()
    assert result.failed == 1
    assert result.deleted == result.uploaded == 0


def test_once_exit_succeeds_when_admin_deleted_the_only_file(tmp_path, monkeypatch, caplog):
    session(tmp_path)
    monkeypatch.setenv("HUB_URL", "https://hub.example")
    monkeypatch.setenv("HUB_SOURCE_ID", "alice")
    monkeypatch.setenv("HUB_SOURCE_TOKEN", "test-secret")
    hub = Hub()
    hub.deleted_paths.add(key(f"sessions/{'a' * 32}/session.json"))
    client_type = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(hub.handle)))
    caplog.set_level("INFO", logger="result_sync")
    assert module.main(["--once", "--output-dir", str(tmp_path)]) == 0
    assert "삭제 건너뜀 1" in caplog.text
    assert "실패 0 (파일)" in caplog.text
