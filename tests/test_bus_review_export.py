"""Offline review exports preserve evidence and portable video references."""
import csv
import io
import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from scripts.review_bus_results import write_review
from test_bus_review import hub, seed, artifact, result, BASE, CLIP


def test_local_report_embeds_untrusted_text_as_data_and_links_existing_media(hub, tmp_path):
    seed(hub)
    row = result(1, 1100)
    row["bus"]["event"]["target_route"] = "=1+1"
    row["bus"]["event"]["buses"][0]["observations"][0]["text"] = '</script><img src=x onerror="bad()">'
    artifact(hub, BASE + "results.jsonl", [row])
    video = artifact(hub, CLIP + "original.webm", b"original")
    destination = tmp_path / "검토 폴더"
    original_database = (hub / "index.sqlite3").read_bytes()
    report = write_review(hub, destination)
    clip, = report["clips"]
    assert (destination / unquote(clip["original_url"])).resolve() == video
    assert not (destination / "media").exists()
    assert "original_path" not in clip
    html = (destination / "index.html").read_text(encoding="utf-8")
    assert '</script><img' not in html and "\\u003c/script\\u003e" in html
    assert "__BUS_REVIEW_DATA__" not in html
    saved = json.loads((destination / "review.json").read_text(encoding="utf-8"))
    assert saved == report
    rows = list(csv.DictReader(io.StringIO((destination / "samples.csv").read_text(encoding="utf-8-sig"))))
    assert rows[0]["target_route"] == "'=1+1"
    assert json.loads(rows[0]["observations"])[0]["text"].startswith("</script>")
    assert (hub / "index.sqlite3").read_bytes() == original_database


def test_copy_export_keeps_relative_media_after_moving_folder(hub, tmp_path):
    seed(hub)
    artifact(hub, CLIP + "original.webm", b"original")
    artifact(hub, CLIP + "inference.mp4", b"inference")
    destination = tmp_path / "review"
    report = write_review(hub, destination, copy_videos=True)
    moved = tmp_path / "moved"
    destination.rename(moved)
    clip, = report["clips"]
    for kind in ("original", "inference"):
        assert (moved / unquote(clip[f"{kind}_url"])).read_bytes() == kind.encode()


def test_cannot_write_report_into_archive_or_through_existing_symlink(hub, tmp_path):
    seed(hub)
    with pytest.raises(ValueError, match="저장소 밖"):
        write_review(hub, hub / "review")
    destination = tmp_path / "review"
    destination.mkdir()
    protected = artifact(hub, CLIP + "original.webm", b"unchanged")
    (destination / "index.html").symlink_to(protected)
    with pytest.raises(ValueError, match="심볼릭 링크"):
        write_review(hub, destination)
    assert protected.read_bytes() == b"unchanged"


def test_missing_hub_is_an_actionable_error(tmp_path):
    with pytest.raises(ValueError, match="index.sqlite3"):
        write_review(tmp_path / "missing", tmp_path / "review")


def test_source_selection_excludes_other_members_from_all_exported_evidence(hub, tmp_path):
    for source in ("member1", "member2", "member3"):
        seed(hub, source=source)
        artifact(hub, BASE + "results.jsonl", [result(1, 1100)], source=source)
        artifact(hub, CLIP + "original.webm", source.encode(), source=source)
    destination = tmp_path / "review"
    report = write_review(hub, destination, source_ids=["member1", "member3", "member1"], copy_videos=True)
    assert report["source_ids"] == ["member1", "member3"]
    assert {clip["source_id"] for clip in report["clips"]} == {"member1", "member3"}
    saved = json.loads((destination / "review.json").read_text(encoding="utf-8"))
    assert {clip["source_id"] for clip in saved["clips"]} == {"member1", "member3"}
    rows = list(csv.DictReader(io.StringIO((destination / "samples.csv").read_text(encoding="utf-8-sig"))))
    assert {row["source_id"] for row in rows} == {"member1", "member3"}
    assert not (destination / "media" / "member2").exists()
    assert (hub / "sources" / "member2" / CLIP / "original.webm").read_bytes() == b"member2"
