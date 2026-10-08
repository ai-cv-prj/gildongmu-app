"""Build an offline, read-only review of the hub's bus-classified clips.

python -m scripts.review_bus_results
python -m scripts.review_bus_results --copy-videos --output-dir test-result/bus-export
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import quote

from result_hub.bus_review import build_review


TEMPLATE = Path(__file__).resolve().parents[1] / "result_hub" / "static" / "bus_review.html"


def write_review(hub_dir: Path, output_dir: Path, *, copy_videos: bool = False,
                 source_ids: list[str] | None = None) -> dict:
    if source_ids is not None and any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
                                      for value in source_ids):
        raise ValueError("서버 ID는 영문·숫자·밑줄·하이픈 1~64자로 입력하세요.")
    hub_dir, output_dir = hub_dir.absolute(), output_dir.resolve()
    if output_dir.is_relative_to(hub_dir.resolve()):
        raise ValueError("검토 결과는 허브 저장소 밖에 저장하세요.")
    if not (hub_dir / "index.sqlite3").is_file():
        raise ValueError("허브의 index.sqlite3를 찾을 수 없습니다. --hub-dir 경로를 확인하세요.")
    report = build_review(hub_dir)
    if source_ids is not None:
        selected_sources = set(source_ids)
        report["clips"] = [clip for clip in report["clips"] if clip["source_id"] in selected_sources]
    report["source_ids"] = sorted(set(source_ids) if source_ids is not None else
                                  {clip["source_id"] for clip in report["clips"]})
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "review.json", "samples.csv"):
        if (output_dir / name).is_symlink():
            raise ValueError("검토 파일의 심볼릭 링크를 덮어쓰지 않습니다.")
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    for clip in report["clips"]:
        for kind in ("original", "inference"):
            value = clip.pop(f"{kind}_path", None)
            clip[f"{kind}_url"] = None
            if not value:
                continue
            source = Path(value)
            if copy_videos:
                target = output_dir / "media" / clip["source_id"] / clip["session_id"] / clip["clip_key"] / source.name
                if not target.resolve().is_relative_to(output_dir) or target.is_symlink():
                    raise ValueError("검토 영상의 저장 경로를 확인하세요.")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                target = source
            clip[f"{kind}_url"] = quote(os.path.relpath(target, output_dir).replace(os.sep, "/"), safe="/")
    serialized = json.dumps(report, ensure_ascii=False, allow_nan=False)
    # An uploaded note or OCR string must never terminate the embedded JSON script.
    embedded = serialized.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = TEMPLATE.read_text(encoding="utf-8")
    (output_dir / "index.html").write_text(template.replace("__BUS_REVIEW_DATA__", embedded), encoding="utf-8")
    (output_dir / "review.json").write_text(serialized + "\n", encoding="utf-8")
    buffer = io.StringIO(newline="")
    fields = ["source_id", "session_id", "clip_key", "frame_id", "bus_frame_id", "offset_ms",
              "source_offset_ms", "source_in_clip", "target_route", "status", "bus_count", "observations",
              "decisions", "recognized_routes", "bus_result_age_ms", "result_ms",
              "overlay_status", "overlay_delay_ms", "inference_video_ms"]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for clip in report["clips"]:
        for sample in clip["samples"]:
            row = {**sample, **{key: clip[key] for key in fields[:3]}}
            for key, value in row.items():
                if isinstance(value, (list, dict)):
                    row[key] = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    row[key] = "'" + value
            writer.writerow(row)
    (output_dir / "samples.csv").write_text(buffer.getvalue(), encoding="utf-8-sig")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="허브의 버스 분류 영상과 인식·표시 로그를 로컬에서 함께 검토합니다. 모델 실행은 필요 없습니다.")
    parser.add_argument("--hub-dir", type=Path, default=Path("data/result-hub"), help="index.sqlite3가 있는 허브 저장 폴더")
    parser.add_argument("--output-dir", type=Path, default=Path("test-result/bus-review"))
    parser.add_argument("--copy-videos", action="store_true", help="영상도 복사하여 다른 PC로 옮길 수 있는 폴더 생성")
    parser.add_argument("--source-id", dest="source_ids", action="append",
                        help="분석할 서버 ID. 여러 번 지정 가능. 기본값: member1, member3")
    args = parser.parse_args()
    try:
        report = write_review(args.hub_dir, args.output_dir, copy_videos=args.copy_videos,
                              source_ids=args.source_ids or ["member1", "member3"])
    except (OSError, ValueError) as error:
        parser.exit(1, f"검토 자료 생성 실패: {error}\n")
    print(f"버스 클립 {len(report['clips'])}개: {(args.output_dir / 'index.html').resolve()}")
    print(f"분석 대상: {', '.join(report['source_ids'])}")
    print("브라우저에서 index.html을 여세요. review.json과 samples.csv도 함께 생성했습니다.")
    if not args.copy_videos:
        print("영상은 원래 저장 위치를 참조합니다. 폴더를 옮길 때는 --copy-videos로 다시 생성하세요.")
    if report.get("warnings"):
        print(f"확인할 기록 {len(report['warnings'])}건: 검토 화면의 안내를 확인하세요.")


if __name__ == "__main__":
    main()
