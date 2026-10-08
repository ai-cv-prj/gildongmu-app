"""Read-only, standard-library evidence for reviewing locally archived bus clips.

Counts describe logged observations, not accuracy: there are no ground-truth
labels here. Repeated responses containing the same OCR snapshot count once.
"""
from collections import Counter
from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat


_SOURCE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SESSION = re.compile(r"[0-9a-f]{32}")
_CLIP = re.compile(r"clip_[0-9]{3}|legacy")


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _frame(value):
    return value if type(value) is int and value > 0 else None


def _track(value):
    return value if type(value) is int else None


def _text(value):
    return value if isinstance(value, str) else None


def _objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _safe_file(path):
    """Reject symlink components, including a symlink to the archive itself."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"심볼릭 링크를 읽지 않았습니다: {path}")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"일반 파일이 아닙니다: {path}")
    return path


def _artifact(root, live, source, relative, warnings, *, required=True):
    path = root / "sources" / source / relative
    if (source, relative) not in live:
        if required:
            warnings.append(f"허브 색인에 파일이 없습니다: {relative}")
        return None
    try:
        return _safe_file(path)
    except (OSError, ValueError) as error:
        warnings.append(f"파일을 읽을 수 없습니다: {relative} ({error})")
        return None


def _json(path, warnings):
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
        raise ValueError("JSON 객체가 아닙니다")
    except (OSError, ValueError, UnicodeError) as error:
        warnings.append(f"메타데이터를 읽을 수 없습니다: {path.name} ({error})")
        return {}


def _jsonl(path, warnings):
    if path is None:
        return []
    rows, invalid = [], 0
    try:
        # Decode per line so an interrupted or corrupt line does not hide the rest.
        with path.open("rb") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("JSON 객체가 아닙니다")
                    rows.append(row)
                except (ValueError, UnicodeError):
                    invalid += 1
    except OSError as error:
        warnings.append(f"로그를 읽을 수 없습니다: {path.name} ({error})")
    if invalid:
        warnings.append(f"{path.name}: 손상되거나 미완성인 JSONL {invalid}행을 제외했습니다.")
    return rows


def _stats(values):
    values = sorted(value for value in values if _number(value) is not None and value >= 0)
    def percentile(fraction):
        if not values:
            return None
        position = (len(values) - 1) * fraction
        lo, hi = math.floor(position), math.ceil(position)
        return round(values[lo] + (values[hi] - values[lo]) * (position - lo), 1)
    return {"count": len(values), "p50": percentile(.5), "p95": percentile(.95),
            "max": max(values) if values else None}


def _timings(events):
    frames, overlays, ages = {}, {}, {}
    for row in events:
        frame_id = _frame(row.get("frame_id"))
        if row.get("event_group") != "client_timing" or frame_id is None:
            continue
        if row.get("kind") == "frame":
            frames.setdefault(frame_id, row)
            source_id = _frame(row.get("bus_result_frame_id"))
            age = _number(row.get("bus_result_age_ms"))
            if source_id is not None and age is not None and age >= 0:
                ages.setdefault((frame_id, source_id), age)
        elif row.get("kind") == "overlay":
            overlays.setdefault(frame_id, row)
    return frames, overlays, ages


def _sample(row, bus, event, start, end, timeline, timings):
    frame_id, source_id = row["frame_id"], bus["frame_id"]
    frame_times, overlays, ages = timings
    frame_time, overlay = frame_times.get(frame_id, {}), overlays.get(frame_id, {})
    buses, observations, decisions = _objects(event.get("buses")), [], []
    for detected in buses:
        track = _track(detected.get("track_id"))
        for item in _objects(detected.get("observations")):
            observations.append({"text": _text(item.get("text")),
                                 "eligible": item.get("eligible") if type(item.get("eligible")) is bool else None,
                                 "token_score": _number(item.get("token_score")),
                                 "rejection_reason": _text(item.get("rejection_reason")),
                                 "track_id": _track(item.get("track_id", track))})
        decision = detected.get("decision")
        if isinstance(decision, dict):
            decisions.append({"track_id": track, "state": _text(decision.get("state")),
                              "reason": _text(decision.get("reason")),
                              "support_samples": _number(decision.get("support_samples"))})
    routes, seen_routes = [], set()
    matches = [{**match, "is_target": True} for match in _objects(event.get("matches"))]
    # Match the frontend's preparation/failure gate while retaining raw OCR.
    rejected = _text(bus.get("status")) in {"error", "unavailable", "disabled", "loading", "idle"} or bool(event.get("errors"))
    proposals = [] if rejected else matches + _objects(event.get("recognized_routes"))
    for match in proposals:
        route = _text(match.get("route_number"))
        state, track = _text(match.get("state")), _track(match.get("track_id"))
        key = (route, state, track)
        if route and state in {"recognized_single", "matched_candidate"} and key not in seen_routes:
            seen_routes.add(key)
            routes.append({"route_number": route, "state": state, "track_id": track,
                           "is_target": match.get("is_target") if type(match.get("is_target")) is bool else None})
    captured = _number(row.get("captured_at_ms"))
    return {"frame_id": frame_id, "bus_frame_id": source_id,
            "offset_ms": captured - start if captured is not None and start is not None else None,
            "source_offset_ms": bus["captured_at_ms"] - start if start is not None else None,
            "source_in_clip": start <= bus["captured_at_ms"] < end
                if start is not None and end is not None and end > start else None,
            "target_route": _text(event.get("target_route")), "status": _text(bus.get("status")),
            "bus_count": len(buses), "observations": observations, "decisions": decisions,
            "recognized_routes": routes, "bus_result_age_ms": ages.get((frame_id, source_id)),
            "result_ms": _number(frame_time.get("result_ms")),
            "overlay_status": _text(overlay.get("status")),
            "overlay_delay_ms": _number(overlay.get("overlay_delay_ms")),
            "inference_video_ms": _number(timeline.get(frame_id, {}).get("video_pts_ms"))}


def _evidence(results, events, manifest, warnings, *, legacy=False):
    start, end = _number(manifest.get("started_at_ms")), _number(manifest.get("ended_at_ms"))
    entries = manifest.get("frames")
    timeline = {item["frame_id"]: item for item in _objects(entries) if _frame(item.get("frame_id"))}
    if isinstance(entries, list):
        selected = [row for row in results if _frame(row.get("frame_id")) in timeline]
    elif start is not None and end is not None and end > start:
        selected = [row for row in results if _number(row.get("captured_at_ms")) is not None
                    and start <= row["captured_at_ms"] < end]
    elif legacy:
        selected = results
        warnings.append("기존 전체 녹화에는 클립 시각 정보가 없어 영상과 로그의 자동 위치 맞춤을 제공하지 않습니다.")
    else:
        selected = []
        warnings.append("클립 프레임 목록과 유효한 시작·종료 시각이 없어 로그 구간을 확인할 수 없습니다.")
    # Retransmitted/log-duplicated responses are also counted only once.
    responses = {}
    for row in selected:
        if _frame(row.get("frame_id")):
            responses.setdefault(row["frame_id"], row)
    timings = _timings(events)
    samples, seen, invalid, missing_capture = [], set(), 0, 0
    statuses, errors = Counter(), Counter()
    for row in responses.values():
        bus = row.get("bus")
        if not isinstance(bus, dict):
            continue
        status = _text(bus.get("status"))
        if status:
            statuses[status] += 1
        if _text(bus.get("error")):
            errors[bus["error"]] += 1
        event = bus.get("event")
        # An expired cached result retains its ID with event=None. It is not OCR.
        if event is None:
            continue
        if (not isinstance(event, dict) or event.get("type", "bus_detection") != "bus_detection"
                or not isinstance(event.get("buses"), list)
                or _frame(bus.get("frame_id")) is None or _number(bus.get("captured_at_ms")) is None):
            invalid += 1
            continue
        key = (bus["frame_id"], bus["captured_at_ms"])
        if key not in seen:
            seen.add(key)
            samples.append(_sample(row, bus, event, start, end, timeline, timings))
            if _number(row.get("captured_at_ms")) is None:
                missing_capture += 1
            for error in event.get("errors", []) if isinstance(event.get("errors"), list) else []:
                if _text(error):
                    errors[error] += 1
    for error, count in errors.items():
        warnings.append(f"버스 처리 오류: {error} (로그 {count}건)")
    if invalid:
        warnings.append(f"버스 OCR 식별자 또는 관측 구조가 불완전한 {invalid}응답을 집계에서 제외했습니다.")
    if missing_capture:
        warnings.append(f"응답 원본 시각이 없는 OCR 관측 {missing_capture}건은 영상 위치를 맞출 수 없습니다.")
    carried = sum(sample["source_offset_ms"] is not None and sample["source_offset_ms"] < 0 for sample in samples)
    if carried:
        warnings.append(f"클립 시작 전에 촬영된 OCR 관측 {carried}건이 구간 안 응답에 포함되어 있습니다.")
    after = sum(sample["source_in_clip"] is False and sample["source_offset_ms"] >= 0 for sample in samples)
    if after:
        warnings.append(f"클립 종료 시각 이후에 촬영된 OCR 관측 {after}건은 원본 영상 구간 밖에 있습니다.")
    if not samples:
        warnings.append("이 구간에는 유효한 버스 OCR 관측 로그가 없습니다.")
    frame_times, overlays, _ = timings
    if not any(frame in frame_times for frame in responses):
        warnings.append("이 구간의 클라이언트 응답 지연 기록이 없습니다.")
    if not any(sample["bus_result_age_ms"] is not None for sample in samples):
        warnings.append("OCR 원본 프레임과 연결된 결과 나이 기록이 없습니다.")
    if not any(frame in overlays for frame in responses):
        warnings.append("이 구간의 화면 표시 기록이 없습니다.")
    return samples, {
        "response_frames": len(responses), "unique_ocr_frames": len(samples),
        "frames_with_bus": sum(sample["bus_count"] > 0 for sample in samples),
        "frames_with_text": sum(any(obs["text"] for obs in sample["observations"]) for sample in samples),
        "frames_with_recognition": sum(bool(sample["recognized_routes"]) for sample in samples),
        "bus_status_counts": dict(statuses),
        "overlay_status_counts": dict(Counter(_text(overlays[frame].get("status"))
            for frame in responses if frame in overlays and _text(overlays[frame].get("status")))),
        "bus_age_ms": _stats(sample["bus_result_age_ms"] for sample in samples),
        "result_ms": _stats(frame_times.get(frame, {}).get("result_ms") for frame in responses),
    }


def build_review(hub_dir: Path) -> dict:
    """Return bus-classified live clips without modifying the archive or its DB."""
    root = Path(os.path.abspath(hub_dir))
    report = {"clips": [], "warnings": []}
    try:
        database = _safe_file(root / "index.sqlite3")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")  # Category, artifact and trash reads share one snapshot.
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"clip_categories", "artifacts"}.issubset(tables):
                raise ValueError("클립 분류 또는 파일 색인 테이블이 없습니다")
            live_query = "SELECT source_id, path FROM artifacts a"
            if "trash" in tables:
                live_query += """ WHERE NOT EXISTS (SELECT 1 FROM trash t
                    WHERE t.source_id=a.source_id AND (a.path=t.path OR
                    substr(a.path, 1, length(t.path)+1)=t.path || '/'))"""
            live = set(db.execute(live_query))
            categories = db.execute("SELECT source_id, session_id, clip_key, categories FROM clip_categories").fetchall()
    except (OSError, ValueError, sqlite3.Error) as error:
        report["warnings"].append(f"허브 색인을 읽을 수 없습니다: {error}")
        return report
    sessions = {}
    for source, session, key, encoded in categories:
        if not all(isinstance(value, str) and pattern.fullmatch(value)
                   for value, pattern in ((source, _SOURCE), (session, _SESSION), (key, _CLIP))):
            report["warnings"].append("허용되지 않은 식별자가 있는 클립 분류를 제외했습니다.")
            continue
        try:
            values = json.loads(encoded)
            if not isinstance(values, list):
                raise ValueError("분류가 목록이 아닙니다")
        except (ValueError, TypeError):
            report["warnings"].append(f"클립 분류를 읽을 수 없습니다: {source}/{session}/{key}")
            continue
        if "bus" not in values:
            continue
        prefix = f"sessions/{session}/"
        relative = prefix + f"clips/{key}/"
        required = [prefix + "camera.mp4", prefix + "camera_overlay.mp4"] if key == "legacy" else [relative + "manifest.json"]
        if not any((source, name) in live for name in required):
            continue  # A retained category is not evidence that a deleted clip is live.
        warnings = []
        if (source, session) not in sessions:
            session_warnings = []
            loaded = []
            for name, reader in (("session.json", _json), ("results.jsonl", _jsonl), ("events.jsonl", _jsonl)):
                path = _artifact(root, live, source, prefix + name, session_warnings)
                loaded.append(reader(path, session_warnings))
            sessions[source, session] = (*loaded, session_warnings)
        metadata, results, events, session_warnings = sessions[source, session]
        warnings.extend(session_warnings)
        manifest = {} if key == "legacy" else _json(
            _artifact(root, live, source, relative + "manifest.json", warnings), warnings)
        media = ([prefix + "camera.mp4"], [prefix + "camera_overlay.mp4"]) if key == "legacy" else (
            [relative + "original.mp4", relative + "original.webm"], [relative + "inference.mp4"])
        paths = []
        for candidates in media:
            path = None
            for candidate in candidates:
                path = _artifact(root, live, source, candidate, warnings, required=False)
                if path:
                    break
            paths.append(str(path) if path else None)
        if paths[0] is None:
            warnings.append("원본 영상 파일이 없습니다.")
        if paths[1] is None:
            warnings.append("추론 영상 파일이 없습니다.")
        samples, summary = _evidence(results, events, manifest, warnings, legacy=key == "legacy")
        start, end = _number(manifest.get("started_at_ms")), _number(manifest.get("ended_at_ms"))
        duration = _number(manifest.get("duration_ms"))
        if duration is None and start is not None and end is not None and end >= start:
            duration = end - start
        report["clips"].append({"id": f"{source}/{session}/{key}", "source_id": source,
            "session_id": session, "clip_key": key, "device_name": _text(metadata.get("device_name")),
            "note": _text(metadata.get("note")), "started_at": _text(metadata.get("started_at")),
            "started_at_ms": start, "duration_ms": duration,
            "first_frame_offset_ms": _number(manifest.get("first_frame_offset_ms")),
            "original_path": paths[0], "inference_path": paths[1], "warnings": warnings,
            "summary": summary, "samples": samples})
    report["clips"].sort(key=lambda clip: (clip["started_at"] or "", clip["id"]), reverse=True)
    return report
