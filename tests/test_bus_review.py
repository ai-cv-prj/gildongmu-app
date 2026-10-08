"""Local review uses live classifications and distinct evidence, never accuracy."""
import json
from pathlib import Path
import sqlite3

import pytest

from result_hub.bus_review import build_review


SID = "a" * 32
BASE = f"sessions/{SID}/"
CLIP = BASE + "clips/clip_001/"


@pytest.fixture
def hub(tmp_path):
    root = tmp_path / "hub"
    root.mkdir()
    with sqlite3.connect(root / "index.sqlite3") as db:
        db.executescript("""
            CREATE TABLE artifacts (source_id TEXT, path TEXT);
            CREATE TABLE clip_categories (source_id TEXT, session_id TEXT, clip_key TEXT, categories TEXT);
            CREATE TABLE trash (source_id TEXT, path TEXT);
        """)
    return root


def artifact(hub, relative, data, source="member1"):
    path = hub / "sources" / source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, dict):
        path.write_text(json.dumps(data), encoding="utf-8")
    elif isinstance(data, list):
        path.write_text("".join(json.dumps(row) + "\n" for row in data), encoding="utf-8")
    else:
        path.write_bytes(data)
    with sqlite3.connect(hub / "index.sqlite3") as db:
        db.execute("INSERT INTO artifacts VALUES (?, ?)", (source, relative))
    return path


def classify(hub, categories=("bus",), source="member1", session=SID, key="clip_001"):
    with sqlite3.connect(hub / "index.sqlite3") as db:
        db.execute("INSERT INTO clip_categories VALUES (?, ?, ?, ?)",
                   (source, session, key, json.dumps(categories)))


def seed(hub, *, manifest=None, source="member1"):
    artifact(hub, BASE + "session.json", {"device_name": "테스트 폰", "note": "버스 701",
             "started_at": "2026-10-07T01:00:00+00:00"}, source)
    artifact(hub, CLIP + "manifest.json", manifest if manifest is not None else {
        "clip_id": 1, "started_at_ms": 1000, "ended_at_ms": 2000, "duration_ms": 1000}, source)
    classify(hub, source=source)


def result(frame_id, captured_at_ms, *, source_id=None, source_at=None, status="matched", event=True):
    observation = {"text": "701", "token_score": .99, "eligible": True, "rejection_reason": None}
    detection = {"target_route": "701", "buses": [{"track_id": 4, "observations": [observation],
                  "decision": {"state": "recognized_single", "reason": "full_number", "support_samples": 1}}],
                 "matches": [{"route_number": "701", "state": "recognized_single", "track_id": 4}],
                 "recognized_routes": [{"route_number": "701", "state": "recognized_single", "track_id": 4},
                                       {"route_number": "702", "state": "matched_candidate", "track_id": 5}]}
    return {"frame_id": frame_id, "captured_at_ms": captured_at_ms, "bus": {
        "frame_id": frame_id if source_id is None else source_id,
        "captured_at_ms": captured_at_ms if source_at is None else source_at,
        "status": status, "event": detection if event else None}}


def timing(frame_id, kind="frame", **values):
    return {"event_group": "client_timing", "kind": kind, "frame_id": frame_id, **values}


def test_deduplicates_cached_ocr_and_joins_explicit_client_timing(hub):
    seed(hub, manifest={"started_at_ms": 1000, "ended_at_ms": 2000, "first_frame_offset_ms": 100,
         "frames": [{"frame_id": 10, "video_pts_ms": 0}, {"frame_id": 11, "video_pts_ms": 100},
                    {"frame_id": 12, "video_pts_ms": 200}]})
    row = result(10, 1100, source_id=7, source_at=900)
    artifact(hub, BASE + "results.jsonl", [row, row, result(11, 1200, source_id=7, source_at=900),
                                          result(12, 1300, source_id=7, source_at=900, event=False)])
    artifact(hub, BASE + "events.jsonl", [
        timing(10, result_ms=80, bus_result_frame_id=7, bus_result_age_ms=280),
        timing(11, result_ms=100, bus_result_frame_id=None, bus_result_age_ms=999),
        timing(12, result_ms=120), timing(10, "overlay", status="drawn", overlay_delay_ms=90),
        timing(11, "overlay", status="stale"),
        timing(12, "audio", bus_result_frame_id=7, bus_result_age_ms=1)])
    artifact(hub, CLIP + "original.webm", b"video")
    artifact(hub, CLIP + "inference.mp4", b"overlay")
    clip, = build_review(hub)["clips"]
    sample, = clip["samples"]
    assert sample["frame_id"] == 10 and sample["bus_frame_id"] == 7
    assert sample["offset_ms"] == 100 and sample["source_offset_ms"] == -100
    assert [route["route_number"] for route in sample["recognized_routes"]] == ["701", "702"]
    assert sample["recognized_routes"][0]["is_target"] is True
    assert sample["bus_result_age_ms"] == 280 and sample["result_ms"] == 80
    assert sample["overlay_delay_ms"] == 90 and sample["overlay_status"] == "drawn"
    assert sample["inference_video_ms"] == 0
    assert sample["observations"][0]["track_id"] == 4
    assert sample["decisions"][0]["support_samples"] == 1
    assert clip["summary"] == {
        "response_frames": 3, "unique_ocr_frames": 1, "frames_with_bus": 1,
        "frames_with_text": 1, "frames_with_recognition": 1,
        "bus_status_counts": {"matched": 3}, "overlay_status_counts": {"drawn": 1, "stale": 1},
        "bus_age_ms": {"count": 1, "p50": 280, "p95": 280, "max": 280},
        "result_ms": {"count": 3, "p50": 100, "p95": 118, "max": 120}}
    assert Path(clip["original_path"]).is_absolute()
    assert any("클립 시작 전에" in warning for warning in clip["warnings"])


def test_none_event_is_not_ocr_even_when_identifiers_exist(hub):
    seed(hub)
    artifact(hub, BASE + "results.jsonl", [result(1, 1000, status="searching", event=False),
                                          result(2, 1200, status="unavailable", event=False)])
    clip, = build_review(hub)["clips"]
    assert clip["samples"] == []
    assert clip["summary"]["bus_status_counts"] == {"searching": 1, "unavailable": 1}
    assert clip["summary"]["unique_ocr_frames"] == 0
    assert clip["summary"]["bus_age_ms"] == {"count": 0, "p50": None, "p95": None, "max": None}
    assert any("OCR 관측 로그" in warning for warning in clip["warnings"])


def test_runtime_errors_are_visible_separately_from_ocr_misses(hub):
    seed(hub)
    row = result(1, 1000, status="error", event=False)
    row["bus"]["error"] = "bus_model_load_failed:RuntimeError"
    artifact(hub, BASE + "results.jsonl", [row])
    clip, = build_review(hub)["clips"]
    assert clip["summary"]["bus_status_counts"] == {"error": 1}
    assert any("bus_model_load_failed:RuntimeError" in warning for warning in clip["warnings"])


@pytest.mark.parametrize("status,event_errors", [
    ("error", []), ("unavailable", []), ("disabled", []), ("loading", []), ("idle", []),
    ("matched", ["ocr_failed:RuntimeError"]),
])
def test_frontend_rejected_results_keep_raw_evidence_without_accepted_routes(hub, status, event_errors):
    seed(hub)
    row = result(1, 1100, status=status)
    row["bus"]["event"]["errors"] = event_errors
    artifact(hub, BASE + "results.jsonl", [row])
    clip, = build_review(hub)["clips"]
    sample, = clip["samples"]
    assert sample["recognized_routes"] == []
    assert sample["observations"][0]["text"] == "701"
    assert sample["decisions"][0]["state"] == "recognized_single"
    assert clip["summary"]["frames_with_recognition"] == 0
    assert clip["summary"]["frames_with_text"] == 1
    if event_errors:
        assert any("ocr_failed:RuntimeError" in warning for warning in clip["warnings"])


def test_bus_source_capture_outside_clip_has_no_source_seek(hub):
    seed(hub)
    artifact(hub, BASE + "results.jsonl", [result(1, 1100, source_at=999), result(2, 1200, source_at=1000),
        result(3, 1300, source_at=1999), result(4, 1400, source_at=2000), result(5, 1500, source_at=2001)])
    clip, = build_review(hub)["clips"]
    assert [sample["source_in_clip"] for sample in clip["samples"]] == [False, True, True, False, False]
    assert any("클립 종료 시각 이후" in warning and "2건" in warning for warning in clip["warnings"])


def test_nonstandard_track_values_do_not_break_strict_json_export(hub):
    seed(hub)
    row = result(1, 1100)
    detected = row["bus"]["event"]["buses"][0]
    detected["track_id"] = float("nan")
    detected["observations"][0]["track_id"] = float("inf")
    row["bus"]["event"]["matches"][0]["track_id"] = float("nan")
    artifact(hub, BASE + "results.jsonl", [row])
    report = build_review(hub)
    json.dumps(report, allow_nan=False)
    sample, = report["clips"][0]["samples"]
    assert sample["observations"][0]["track_id"] is None
    assert sample["decisions"][0]["track_id"] is None
    assert sample["recognized_routes"][0]["track_id"] is None


def test_missing_logs_and_videos_keeps_classified_clip_with_null_timings(hub):
    seed(hub)
    before = (hub / "index.sqlite3").read_bytes()
    clip, = build_review(hub)["clips"]
    assert clip["original_path"] is None and clip["inference_path"] is None
    assert clip["summary"]["result_ms"]["p50"] is None
    assert any("results.jsonl" in warning for warning in clip["warnings"])
    assert any("events.jsonl" in warning for warning in clip["warnings"])
    assert (hub / "index.sqlite3").read_bytes() == before


def test_age_does_not_join_response_id_or_unrelated_source_id(hub):
    seed(hub)
    artifact(hub, BASE + "results.jsonl", [result(10, 1100, source_id=8, source_at=900),
                                          result(11, 1200, source_id=9, source_at=1000)])
    artifact(hub, BASE + "events.jsonl", [timing(10, bus_result_frame_id=10, bus_result_age_ms=123),
                                          timing(11, bus_result_age_ms=123),
                                          timing(8, bus_result_frame_id=8, bus_result_age_ms=123)])
    clip, = build_review(hub)["clips"]
    assert all(sample["bus_result_age_ms"] is None for sample in clip["samples"])


@pytest.mark.parametrize("manifest, expected", [
    ({"started_at_ms": 1000, "ended_at_ms": 2000}, [2, 3]),
    ({"started_at_ms": 1000, "ended_at_ms": 2000, "frames": [{"frame_id": 1}]}, [1]),
    ({"started_at_ms": 1000, "ended_at_ms": 2000, "frames": []}, []),
    ({}, []),
])
def test_clip_selection_prefers_manifest_ids_then_half_open_window(hub, manifest, expected):
    seed(hub, manifest=manifest)
    artifact(hub, BASE + "results.jsonl", [result(1, 999), result(2, 1000), result(3, 1999), result(4, 2000)])
    clip, = build_review(hub)["clips"]
    assert [sample["frame_id"] for sample in clip["samples"]] == expected


def test_only_bus_categories_with_indexed_live_clips_are_included(hub):
    seed(hub)
    seed(hub, source="trashed")
    artifact(hub, CLIP + "manifest.json", {}, "unclassified")
    classify(hub, categories=["obstacle"], source="unclassified")
    classify(hub, source="stale_category")
    with sqlite3.connect(hub / "index.sqlite3") as db:
        db.execute("INSERT INTO trash VALUES (?, ?)", ("trashed", BASE.rstrip("/")))
    report = build_review(hub)
    assert [clip["source_id"] for clip in report["clips"]] == ["member1"]


def test_malformed_and_incomplete_jsonl_warns_but_valid_lines_survive(hub):
    seed(hub)
    payload = json.dumps(result(1, 1100)).encode() + b'\n{broken\n\xff\n[]\n' + json.dumps(result(2, 1200)).encode() + b'\n{"frame_id":'
    artifact(hub, BASE + "results.jsonl", payload)
    artifact(hub, BASE + "events.jsonl", b'{invalid\n')
    clip, = build_review(hub)["clips"]
    assert clip["summary"]["unique_ocr_frames"] == 2
    assert any("results.jsonl" in warning and "4행" in warning for warning in clip["warnings"])
    assert any("events.jsonl" in warning and "1행" in warning for warning in clip["warnings"])


def test_symlink_media_and_metadata_are_not_read_and_paths_are_not_trusted(hub, tmp_path):
    seed(hub, manifest={"original_path": str(tmp_path / "secret"), "started_at_ms": 1000, "ended_at_ms": 2000})
    outside = tmp_path / "secret"
    outside.write_text(json.dumps(result(1, 1100)), encoding="utf-8")
    for relative in (CLIP + "original.mp4", BASE + "results.jsonl"):
        path = artifact(hub, relative, b"")
        path.unlink()
        path.symlink_to(outside)
    clip, = build_review(hub)["clips"]
    assert clip["original_path"] is None and clip["samples"] == []
    assert any("심볼릭 링크" in warning for warning in clip["warnings"])
    classify(hub, source="../../outside")
    assert any("식별자" in warning for warning in build_review(hub)["warnings"])


def test_missing_hub_does_not_create_database(tmp_path):
    absent = tmp_path / "absent"
    report = build_review(absent)
    assert report["clips"] == [] and report["warnings"]
    assert not absent.exists()


def test_symlink_database_or_archive_is_rejected(hub, tmp_path):
    link = tmp_path / "hub-link"
    link.symlink_to(hub, target_is_directory=True)
    assert build_review(link)["warnings"]
    database = hub / "index.sqlite3"
    moved = tmp_path / "outside.sqlite3"
    database.rename(moved)
    database.symlink_to(moved)
    assert build_review(hub)["clips"] == []


def test_legacy_category_uses_full_session_without_inventing_video_offsets(hub):
    artifact(hub, BASE + "camera.mp4", b"video")
    artifact(hub, BASE + "results.jsonl", [result(1, 1100)])
    classify(hub, key="legacy")
    clip, = build_review(hub)["clips"]
    assert clip["clip_key"] == "legacy"
    assert clip["samples"][0]["offset_ms"] is None
    assert clip["samples"][0]["inference_video_ms"] is None
    assert clip["duration_ms"] is None
