"""Four independent uploaders share the real authenticated hub API."""

from concurrent.futures import ThreadPoolExecutor
import json

from fastapi.testclient import TestClient
import httpx

from result_hub.app import HubSettings, create_app
from scripts.sync_results import ResultSync


VIEWER = ("team", "integration-viewer-password")
TOKENS = {f"member{number}": f"integration-upload-token-{number}-abcdef" for number in range(1, 5)}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def source_folder(root, sid, device):
    folder = root / "20261007" / device
    folder.mkdir(parents=True)
    write_json(folder / "session.json", {"id": sid, "device_name": device, "note": "동시 테스트",
                                        "started_at": "2026-10-07T05:00:00Z", "frame_count": 0})
    (folder / "results.jsonl").write_bytes(b'{"frame_id":1}\n{"frame')
    (root / "logs").mkdir()
    (root / "logs" / "app.log").write_text(f"{device} started\npartial", encoding="utf-8")
    return folder


def run_together(uploaders):
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda uploader: uploader.run_once(), uploaders))


def test_four_real_uploaders_active_ended_and_late_clips(tmp_path):
    settings = HubSettings(tmp_path / "hub", *VIEWER, TOKENS)
    # Deliberately reuse a session UUID to verify source isolation as well.
    sid = "c" * 32
    folders = {}
    with TestClient(create_app(settings)) as client:
        uploaders = []
        for source_id, token in TOKENS.items():
            root = tmp_path / source_id
            folders[source_id] = source_folder(root, sid, f"Phone-{source_id}")
            uploaders.append(ResultSync(root, "https://hub.example", source_id, token, client=client))

        first = run_together(uploaders)
        assert all(result.uploaded == 3 and result.failed == 0 for result in first)
        sessions = client.get("/api/sessions", auth=VIEWER).json()["sessions"]
        assert len(sessions) == 4
        assert all(row["ended_at"] is None for row in sessions)
        assert {row["source_id"] for row in sessions} == set(TOKENS)
        for source_id in TOKENS:
            preview = client.get(f"/api/preview/{source_id}/logs/app.log", auth=VIEWER).json()
            assert preview["text"] == f"Phone-{source_id} started"
            result = client.get(f"/api/files/{source_id}/sessions/{sid}/results.jsonl", auth=VIEWER)
            assert result.content == b'{"frame_id":1}\n'

        for source_id, folder in folders.items():
            document = json.loads((folder / "session.json").read_text())
            document.update(ended_at="2026-10-07T05:01:00Z", frame_count=2)
            write_json(folder / "session.json", document)
            with (folder / "results.jsonl").open("ab") as stream:
                stream.write(b'_id":2}\n')
            with (tmp_path / source_id / "logs" / "app.log").open("ab") as stream:
                stream.write(b" completed\n")
        assert all(result.uploaded == 3 for result in run_together(uploaders))
        assert all(result.uploaded == 0 and result.unchanged == 3 for result in run_together(uploaders))

        # Clip upload and export happen later, after stop and after prior successful syncs.
        for source_id, folder in folders.items():
            clip = folder / "clips" / "clip_001"
            clip.mkdir(parents=True)
            (clip / "original.webm").write_bytes(f"original {source_id}".encode())
            (clip / "inference.mp4").write_bytes(b"currently rendering")
            write_json(clip / "manifest.json", {"clip_id": 1, "state": "rendering"})
        assert all(result.uploaded == 2 and result.failed == 0 for result in run_together(uploaders))
        for source_id in TOKENS:
            detail = client.get(f"/api/sessions/{source_id}/{sid}", auth=VIEWER).json()
            assert detail["clips"][0]["original_url"]
            assert detail["clips"][0]["inference_url"] is None

        for source_id, folder in folders.items():
            clip = folder / "clips" / "clip_001"
            (clip / "inference.mp4").write_bytes(f"finished {source_id}".encode())
            write_json(clip / "manifest.json", {"clip_id": 1, "state": "ready"})
        assert all(result.uploaded == 2 and result.failed == 0 for result in run_together(uploaders))
        assert all(result.uploaded == 0 and result.unchanged == 6 for result in run_together(uploaders))
        for source_id in TOKENS:
            detail = client.get(f"/api/sessions/{source_id}/{sid}", auth=VIEWER).json()
            url = detail["clips"][0]["inference_url"]
            assert client.get(url, auth=VIEWER).content == f"finished {source_id}".encode()

    # Both the uploaded files and the catalogue survive a hub process restart.
    with TestClient(create_app(settings)) as restarted:
        rows = restarted.get("/api/sessions", auth=VIEWER).json()["sessions"]
        assert len(rows) == 4
        assert all(row["ready_clip_count"] == 1 and row["ended_at"] for row in rows)


def test_real_hub_retry_then_restore_deleted_central_video(tmp_path):
    settings = HubSettings(tmp_path / "hub", *VIEWER, TOKENS)
    root = tmp_path / "source"
    sid = "d" * 32
    folder = source_folder(root, sid, "Phone")
    clip = folder / "clips" / "clip_001"
    clip.mkdir(parents=True)
    (clip / "original.webm").write_bytes(b"original")
    (clip / "inference.mp4").write_bytes(b"finished")
    write_json(clip / "manifest.json", {"clip_id": 1, "state": "ready"})

    with TestClient(create_app(settings)) as client:
        class Connection:
            offline = True

            def head(self, *args, **kwargs):
                if self.offline:
                    raise httpx.ConnectError("hub is offline")
                return client.head(*args, **kwargs)

            def put(self, *args, **kwargs):
                return client.put(*args, **kwargs)

        connection = Connection()
        sync = ResultSync(root, "https://hub.example", "member1", TOKENS["member1"], client=connection)
        assert sync.run_once().failed > 0
        assert client.get("/api/sessions", auth=VIEWER).json()["sessions"] == []
        connection.offline = False
        assert sync.run_once().uploaded == 6
        assert sync.run_once().unchanged == 6
        remote_video = settings.storage_dir / f"sources/member1/sessions/{sid}/clips/clip_001/inference.mp4"
        remote_video.unlink()
        assert sync.run_once().uploaded == 1
        assert remote_video.read_bytes() == b"finished"
