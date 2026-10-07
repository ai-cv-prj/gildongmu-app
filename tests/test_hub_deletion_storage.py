"""Deletion keeps recoverable data, source isolation, and durable upload tombstones."""

import hashlib
import json
from pathlib import Path

import pytest

from result_hub.storage import Archive


SESSION = "a" * 32
OTHER_SESSION = "b" * 32
LOG = "logs/app.log.2026-10-07"


@pytest.fixture
def archive(tmp_path):
    return Archive(tmp_path / "archive")


def put(archive, path, data=b"result\n", source="member1", *, index=True):
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    target = archive.file(source, path, create_parent=True)
    target.write_bytes(data)
    if index:
        archive.record(source, path, hashlib.sha256(data).hexdigest())
    return target


def put_session(archive, source="member1", session_id=SESSION):
    prefix = f"sessions/{session_id}"
    put(archive, prefix + "/session.json", {"session_id": session_id, "note": "실내 테스트"}, source)
    put(archive, prefix + "/results.jsonl", source=source)
    put(archive, prefix + "/events.jsonl", source=source)
    put(archive, prefix + "/clips/clip_001/original.mp4", b"video", source)
    return prefix


def test_session_delete_restore_preserves_every_byte_and_other_sources(archive):
    prefix = put_session(archive)
    put_session(archive, "member2")
    put_session(archive, session_id=OTHER_SESSION)
    put(archive, "logs/app.log")
    before = archive.entries()
    contents = {(row["source_id"], row["path"]): archive.file(row["source_id"], row["path"]).read_bytes()
                for row in before}

    item = archive.delete_session("member1", SESSION)

    assert item["kind"] == "session"
    assert item["path"] == prefix
    assert item["label"] == "실내 테스트"
    assert item["file_count"] == 4
    assert item["size"] == sum(len(data) for (source, path), data in contents.items()
                               if source == "member1" and path.startswith(prefix + "/"))
    assert not archive.file("member1", prefix + "/session.json").exists()
    assert len(archive.entries()) == len(before) - 4
    assert archive.is_deleted("member1", prefix + "/session.json")
    assert archive.is_deleted("member1", prefix + "/clips/clip_999/inference.mp4")
    assert not archive.is_deleted("member2", prefix + "/session.json")
    assert archive.delete_session("member1", SESSION) == item
    assert archive.trash_entries("member2") == []

    restarted = Archive(archive.root)
    assert restarted.trash_entries() == [item]
    assert restarted.restore(item["id"]) == item
    assert restarted.trash_entries() == []
    assert not restarted.is_deleted("member1", prefix + "/session.json")
    assert restarted.entries() == before
    for (source, path), data in contents.items():
        assert restarted.file(source, path).read_bytes() == data


def test_log_delete_restores_and_keeps_current_log_collecting(archive):
    put(archive, LOG, b"old log\n")
    put(archive, "logs/app.log", b"new log\n")
    item = archive.delete_log("member1", LOG)
    assert item["kind"] == "log"
    assert archive.is_deleted("member1", LOG)
    assert archive.delete_log("member1", LOG) == item
    assert not archive.is_deleted("member1", "logs/app.log")
    put(archive, "logs/app.log", b"new log\nmore log\n")
    with pytest.raises(ValueError, match="현재 수집"):
        archive.delete_log("member1", "logs/app.log")
    archive.restore(item["id"])
    assert archive.file("member1", LOG).read_bytes() == b"old log\n"
    assert archive.file("member1", "logs/app.log").read_bytes().endswith(b"more log\n")


def test_deletion_collects_unindexed_files_after_an_interrupted_upload(archive):
    path = f"sessions/{SESSION}/results.jsonl"
    put(archive, path, index=False)
    item = archive.delete_session("member1", SESSION)
    archive.restore(item["id"])
    assert archive.info("member1", path)["size"] == len(b"result\n")


def test_tombstone_hides_a_late_or_manually_recreated_file(archive):
    prefix = put_session(archive)
    archive.delete_session("member1", SESSION)
    path = prefix + "/session.json"
    put(archive, path, {"session_id": SESSION}, index=False)
    assert archive.info("member1", path) is None
    assert archive.read_json("member1", path) is None
    with pytest.raises(FileNotFoundError, match="휴지통"):
        archive.record("member1", path, "a" * 64)
    assert archive.entries() == []


@pytest.mark.parametrize("source,session", [("../member1", SESSION), ("member1", "../" + SESSION),
                                            ("member1", SESSION + "/other")])
def test_session_delete_rejects_nonallowlisted_paths(archive, source, session):
    put_session(archive)
    with pytest.raises(ValueError):
        archive.delete_session(source, session)
    assert archive.trash_entries() == []
    assert len(archive.entries()) == 4


@pytest.mark.parametrize("path", ["../app.log", "logs/app.log.2026-10-07/other", "logs/other.log",
                                  f"sessions/{SESSION}/results.jsonl"])
def test_log_delete_rejects_nonallowlisted_paths(archive, path):
    with pytest.raises(ValueError):
        archive.delete_log("member1", path)


def test_delete_and_restore_reject_symlinks_without_touching_external_data(archive, tmp_path):
    prefix = put_session(archive)
    outside = tmp_path / "external"
    outside.write_bytes(b"preserve me")
    target = archive.file("member1", prefix + "/results.jsonl")
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError):
        archive.delete_session("member1", SESSION)
    assert outside.read_bytes() == b"preserve me"
    assert archive.trash_entries() == []

    target.unlink()
    put(archive, prefix + "/results.jsonl")
    item = archive.delete_session("member1", SESSION)
    payload = archive.trash / item["id"] / "data" / "results.jsonl"
    payload.unlink()
    payload.symlink_to(outside)
    with pytest.raises(ValueError):
        archive.restore(item["id"])
    assert outside.read_bytes() == b"preserve me"
    assert archive.trash_entries() == [item]


def test_trash_root_symlink_is_rejected(archive, tmp_path):
    put(archive, LOG)
    outside = tmp_path / "external"
    outside.mkdir()
    archive.trash.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        archive.delete_log("member1", LOG)
    assert list(outside.iterdir()) == []
    assert archive.file("member1", LOG).exists()


def test_restore_never_overwrites_a_new_file(archive):
    put(archive, LOG)
    item = archive.delete_log("member1", LOG)
    target = put(archive, LOG, b"new contents", index=False)
    with pytest.raises(FileExistsError):
        archive.restore(item["id"])
    assert target.read_bytes() == b"new contents"
    assert archive.trash_entries() == [item]


def test_restore_rejects_modified_payload(archive):
    put(archive, LOG)
    item = archive.delete_log("member1", LOG)
    (archive.trash / item["id"] / "data").write_bytes(b"changed")
    with pytest.raises(ValueError, match="내용"):
        archive.restore(item["id"])
    assert archive.trash_entries() == [item]


def test_delete_rolls_back_if_index_commit_fails(archive, monkeypatch):
    put_session(archive)
    before = archive.entries()
    def fail(_item):
        raise OSError("disk error")
    monkeypatch.setattr(archive, "_finish_delete", fail)
    with pytest.raises(OSError, match="disk error"):
        archive.delete_session("member1", SESSION)
    assert archive.entries() == before
    assert archive.trash_entries() == []
    assert archive.file("member1", f"sessions/{SESSION}/session.json").exists()


def test_restore_rolls_back_if_index_commit_fails(archive, monkeypatch):
    put(archive, LOG)
    item = archive.delete_log("member1", LOG)
    def fail(_item):
        raise OSError("disk error")
    monkeypatch.setattr(archive, "_finish_restore", fail)
    with pytest.raises(OSError, match="disk error"):
        archive.restore(item["id"])
    assert archive.entries() == []
    assert archive.trash_entries() == [item]
    assert not archive.file("member1", LOG).exists()
    assert (archive.trash / item["id"] / "data").read_bytes() == b"result\n"


@pytest.mark.parametrize("move_completed", [False, True])
def test_restart_recovers_interrupted_delete(archive, monkeypatch, move_completed):
    put(archive, LOG)
    if move_completed:
        def crash(_item):
            raise KeyboardInterrupt()
        monkeypatch.setattr(archive, "_finish_delete", crash)
    else:
        def crash(_source, _destination):
            raise KeyboardInterrupt()
        monkeypatch.setattr(Path, "rename", crash)
    with pytest.raises(KeyboardInterrupt):
        archive.delete_log("member1", LOG)
    assert archive.is_deleted("member1", LOG)
    assert archive.entries() == []
    restarted = Archive(archive.root)
    assert restarted.is_deleted("member1", LOG) == move_completed
    assert len(restarted.entries()) == (0 if move_completed else 1)
    assert restarted.file("member1", LOG).exists() != move_completed
    assert len(restarted.trash_entries()) == (1 if move_completed else 0)


@pytest.mark.parametrize("move_completed", [False, True])
def test_restart_recovers_interrupted_restore(archive, monkeypatch, move_completed):
    put(archive, LOG)
    item = archive.delete_log("member1", LOG)
    if move_completed:
        def crash(_item):
            raise KeyboardInterrupt()
        monkeypatch.setattr(archive, "_finish_restore", crash)
    else:
        def crash(_source, _destination):
            raise KeyboardInterrupt()
        monkeypatch.setattr(Path, "rename", crash)
    with pytest.raises(KeyboardInterrupt):
        archive.restore(item["id"])
    restarted = Archive(archive.root)
    assert restarted.is_deleted("member1", LOG) != move_completed
    assert len(restarted.entries()) == (1 if move_completed else 0)
    assert restarted.file("member1", LOG).exists() == move_completed
    assert len(restarted.trash_entries()) == (0 if move_completed else 1)


def test_missing_targets_and_invalid_trash_ids_do_not_create_tombstones(archive):
    with pytest.raises(FileNotFoundError):
        archive.delete_session("member1", SESSION)
    with pytest.raises(FileNotFoundError):
        archive.delete_log("member1", LOG)
    with pytest.raises(FileNotFoundError):
        archive.restore("f" * 32)
    with pytest.raises(ValueError):
        archive.restore("../somewhere")
    assert archive.trash_entries() == []
