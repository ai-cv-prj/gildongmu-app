"""
file_path: backend/session.py

실시간 테스트 세션과 프레임별 추론 기록을 관리한다.
"""

import json
import copy
import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import cv2

from backend.boarding import Boarding
from backend.provenance import process_snapshot, write_snapshot
from backend.bus.config import load_bus_config
from backend.bus.recognition import BusRecognizer
from backend.response import make_response
from src.settings import load_app_config


log = logging.getLogger(__name__)


class SessionError(Exception):
    """세션 상태나 프레임 순서가 올바르지 않을 때 발생한다."""


# 폴더명에 사용할 사용자 입력 정리
def safe_folder_part(value, fallback="", max_length=None):
    """
    파일명에 사용할 수 없는 문자를 바꾸고 선택한 길이만 남기는 함수이다.
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if max_length is not None:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or fallback


class SessionManager:
    """한 서버에서 한 휴대폰 테스트를 순서대로 처리한다."""

    # 모델 저장소와 출력 폴더 준비
    def __init__(self, output_dir, model_factory=None, session_settings=None,
                 bus_recognizer=None, bus_config=None):
        """모델은 첫 세션 시작 시에만 로딩하고 이후 세션에서 재사용한다."""
        self.runtime_snapshot = process_snapshot()
        self.output_dir = Path(output_dir)
        self.settings = session_settings if session_settings is not None else load_app_config()["session"]
        self.model_factory = model_factory
        self.bus_recognizer = bus_recognizer or BusRecognizer(bus_config or load_bus_config())
        self.models = None
        self.session = None
        self.lock = threading.Lock()
        self._completed_folders = {}
        self._completed_summaries = {}
        self._last_frame_key = None
        self._last_frame_result = None

    # 휴대폰 테스트 시작
    def start(self, device_name, note="", bus_highres=False):
        """새 세션을 만들고 모델 추적 상태를 초기화한다."""
        with self.lock:
            if self.session is not None:
                # 브라우저를 강제로 닫으면 종료 요청이 오지 않으므로 응답이 끊긴 세션은 정리하고 새로 시작한다.
                idle_s = time.monotonic() - self.session["last_seen"]
                if idle_s < self.settings.get("stale_after_s", 30):
                    raise SessionError("이미 진행 중인 테스트가 있습니다. 먼저 종료하세요.")
                self._close(self.session)
            if self.models is None:
                if self.model_factory is None:
                    from backend.inference import RealtimeInference
                    self.model_factory = RealtimeInference
                self.models = self.model_factory()
            else:
                self.models.reset()
            self.models.boarding = Boarding()
            self.bus_recognizer.set_target(None, None)
            prewarm = getattr(self.bus_recognizer, "prewarm", None)
            if bus_highres and prewarm is not None:
                try:
                    prewarm()
                except Exception:
                    log.exception("bus model prewarm could not start")
            session_id = uuid4().hex
            safe_device = safe_folder_part(device_name, "기종미상")
            safe_note = safe_folder_part(note, max_length=self.settings["folder_note_max_length"])
            started_at = datetime.now(ZoneInfo(self.settings["timezone"]))
            date = started_at.strftime("%Y%m%d")
            base_name = f"{safe_device}_{started_at.strftime('%Y%m%d_%H%M%S')}"
            if safe_note:
                base_name = f"{base_name}_{safe_note}"
            index = 1
            while True:
                folder_name = base_name if index == 1 else f"{base_name}_{index}"
                folder = self.output_dir / date / folder_name
                try:
                    folder.mkdir(parents=True, exist_ok=False)
                    break
                except FileExistsError:
                    index += 1
            write_snapshot(folder, self.runtime_snapshot)
            self._last_frame_key = None
            self._last_frame_result = None
            self.session = {
                "id": session_id, "device_name": device_name, "note": note,
                "date": date, "folder_name": folder_name,
                "started_at": started_at.astimezone(timezone.utc).isoformat(),
                "frame_count": 0, "last_frame_id": 0, "last_capture_ms": None,
                "bus_highres": bool(bus_highres),
                "folder": folder, "last_seen": time.monotonic(),
            }
            (folder / "session.json").write_text(json.dumps({
                key: value for key, value in self.session.items() if key not in ("folder", "last_seen")
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"session_id": session_id, "device_name": device_name,
                    "date": date, "folder_name": folder_name}

    def update_metadata(self, session_id, device_name, note=""):
        """저장 경로와 추론 상태를 유지하며 진행 중인 테스트 정보를 저장한다."""
        with self.lock:
            session = self.session
            if session is None or session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다. 다시 시작하세요.")
            saved = {key: value for key, value in session.items() if key not in ("folder", "last_seen")}
            saved.update(device_name=device_name, note=note)
            path = session["folder"] / "session.json"
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            session.update(device_name=device_name, note=note)
            return {"session_id": session_id, "device_name": device_name, "note": note,
                    "date": session["date"], "folder_name": session["folder_name"]}

    # 한 프레임 처리와 JSONL 기록
    def process(self, session_id, frame_id, captured_at_ms, frame, request_start_ns=None,
                decode_ms=None, image_bytes=None, save_live_frame=False,
                bus_frame=None, bus_captured_at_ms=None, bus_image_bytes=None):
        """요청 순서를 검증하고 세 모델 결과를 저장한다."""
        with self.lock:
            processing_start_ns = time.perf_counter_ns()
            session = self.session
            if session is None or session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다. 다시 시작하세요.")
            # A lost response can be replayed only for the latest identical input.
            # Keep one response in memory, including mask/audio identifiers, so
            # replay never advances model state or adds a second results record.
            def image_identity(raw, decoded):
                if raw is not None:
                    return ("encoded", hashlib.sha256(raw).hexdigest())
                if decoded is not None:
                    return ("decoded", decoded.shape, str(decoded.dtype),
                            hashlib.sha256(decoded.tobytes()).hexdigest())
                return None
            frame_key = (session_id, frame_id, captured_at_ms, bool(save_live_frame),
                         image_identity(image_bytes, frame), bus_captured_at_ms,
                         image_identity(bus_image_bytes, bus_frame))
            if frame_id == session["last_frame_id"] and frame_key == self._last_frame_key:
                session["last_seen"] = time.monotonic()
                log.info("frame replayed session=%s frame=%d", session_id, frame_id)
                return copy.deepcopy(self._last_frame_result)
            if frame_id != session["last_frame_id"] + 1:
                raise SessionError("프레임 번호가 연속적이지 않습니다.")
            if session["last_capture_ms"] is not None and captured_at_ms <= session["last_capture_ms"]:
                raise SessionError("촬영 시간이 이전 프레임보다 늦지 않습니다.")
            if bus_frame is not None and not session["bus_highres"]:
                raise SessionError("이 세션은 버스 전용 프레임을 사용하지 않습니다.")
            bus_submission_error = None
            if bus_frame is not None:
                # Offer the high-resolution source before the walking inference.
                # The worker can run while the walking response is prepared.
                try:
                    self.bus_recognizer.observe(session_id, bus_frame, frame_id, bus_captured_at_ms)
                except Exception as error:
                    bus_submission_error = f"bus_recognition_unavailable:{type(error).__name__}"
            risk, signal, crosswalk, walking_surface, class_map, label_ids, elapsed = self.models.predict(
                frame, frame_id, captured_at_ms,
            )
            session["crossing_active"] = bool(crosswalk.get("crossing_active", False))
            risk["boarding"] = self.models.boarding.observe(
                risk.get("stop_proximity"), crossing_active=crosswalk.get("crossing_active", False))
            result = make_response(session_id, frame_id, captured_at_ms, frame,
                                   risk, signal, crosswalk, walking_surface,
                                   class_map, label_ids, elapsed)
            # OCR runs on a separate newest-frame worker. Its own source timestamp
            # stays attached to cached results, and it cannot fail walking guidance.
            bus_result = None
            if bus_submission_error is None:
                try:
                    bus_source = None if session["bus_highres"] else frame
                    bus_result = self.bus_recognizer.observe(
                        session_id, bus_source, frame_id, bus_captured_at_ms or captured_at_ms)
                except Exception as error:
                    bus_submission_error = f"bus_recognition_unavailable:{type(error).__name__}"
            if bus_submission_error is not None:
                result["bus"] = {"status": "error", "event": None, "detections": [],
                                 "captured_at_ms": None, "frame_id": None,
                                 "error": bus_submission_error}
            else:
                result["bus"] = bus_result
            if save_live_frame:
                frame_file = Path("frames") / f"{frame_id:06d}.jpg"
                (session["folder"] / "frames").mkdir(exist_ok=True)
                encoded, jpeg = cv2.imencode(".jpg", frame)
                if not encoded:
                    raise SessionError("추론 프레임 이미지를 저장할 수 없습니다.")
                (session["folder"] / frame_file).write_bytes(jpeg.tobytes())
                result["frame_file"] = frame_file.as_posix()
            else:
                result["frame_file"] = None
            result["frame_sha256"] = hashlib.sha256(image_bytes).hexdigest() if image_bytes is not None else None
            result["server_timing"] = {
                "decode_ms": decode_ms,
                "processing_ms": round((time.perf_counter_ns() - processing_start_ns) / 1e6, 1),
                "request_ms": (round((time.perf_counter_ns() - request_start_ns) / 1e6, 1)
                               if request_start_ns is not None else None),
            }
            # 응답의 큰 PNG는 로그에서 제외하고 판단 결과와 선택 대상만 기록한다.
            record = {
                **{key: value for key, value in result.items() if key not in ("walking", "traffic")},
                "walking": {key: value for key, value in result["walking"].items() if key != "mask_png"},
                "traffic": result["traffic"],
                "surface_diagnostics": risk.get("surface"),
                "voice_diagnostics": risk.get("voice_diagnostics"),
                "warning_diagnostics": risk.get("warning"),
                "risk_diagnostics": [{
                    "detection_index": item.get("detection_index"),
                    "track_id": item.get("track_id"),
                    "risk_level": item.get("risk_level"),
                    "alert_level": item.get("alert_level"),
                    "reasons": item.get("reasons", []),
                    "bottom_y": (item.get("geometry") or {}).get("point", [None, None])[1],
                    "corridor_overlap": (item.get("geometry") or {}).get("corridor_overlap"),
                    "time_to_near_s": (item.get("motion") or {}).get("time_to_near_s"),
                    "ground_approach": (item.get("motion") or {}).get("ground_approach"),
                    "time_to_moving_conflict_s": (item.get("motion") or {}).get("time_to_moving_conflict_s"),
                    "moving_conflict_basis": (item.get("motion") or {}).get("moving_conflict_basis"),
                    "ttc_scale_s": (item.get("motion") or {}).get("ttc_scale_s"),
                    "motion_quality": (item.get("motion") or {}).get("quality"),
                    "independent_velocity_norm_per_s": (item.get("motion") or {}).get(
                        "independent_velocity_norm_per_s"),
                    "risk_suppressed_reason": item.get("risk_suppressed_reason"),
                } for item in risk["detections"]],
            }
            with (session["folder"] / "results.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            session["frame_count"] += 1
            session["last_frame_id"] = frame_id
            session["last_capture_ms"] = captured_at_ms
            session["last_seen"] = time.monotonic()
            self._last_frame_key = frame_key
            self._last_frame_result = copy.deepcopy(result)
            return result

    def boarding_state(self, session_id):
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            return self.models.boarding.snapshot()

    def update_boarding(self, session_id, action, arrival_event_id=None, bus_number=None):
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            previous = self.models.boarding.snapshot()
            result = self.models.boarding.act(action, arrival_event_id, bus_number,
                                              crossing_active=self.session.get("crossing_active", False))
            if result != previous and action in ("arrive", "cancel", "reopen", "submit"):
                target = result["bus_number"] if action == "submit" else None
                self.bus_recognizer.set_target(session_id if target else None, target)
            if result != previous:
                record = {"at": datetime.now(timezone.utc).isoformat(),
                          "action": action, **result}
                self._append_events("boarding", [record])
            return result

    def record_bus_events(self, session_id, events):
        """Append bounded client GPS/OCR guidance events to this live session."""
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            self._append_events("bus", [
                {**event, "session_id": session_id, "server_at": datetime.now(timezone.utc).isoformat()}
                for event in events
            ])

    def record_client_diagnostics(self, events):
        """Persist diagnostics globally and in verified current or past sessions.

        Diagnostics survive a stopped session and can be delivered from the
        browser queue after restart. Unknown sessions still have a global log.
        """
        with self.lock:
            server_at = datetime.now(timezone.utc).isoformat()
            records = [{**event, "server_at": server_at, "event_group": "client_diagnostic"}
                       for event in events]
            log_dir = self.output_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "client-events.jsonl").open("a", encoding="utf-8") as file:
                for record in records:
                    file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            folders = {}
            for record in records:
                session_id = record.get("session_id")
                if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
                    continue
                if session_id not in folders:
                    folder = None
                    if self.session is not None and self.session["id"] == session_id:
                        folder = self.session["folder"]
                    elif session_id in self._completed_folders:
                        folder = self._completed_folders[session_id]
                    else:
                        for candidate in self.output_dir.glob("[0-9]" * 8 + "/*"):
                            if not candidate.resolve().is_relative_to(self.output_dir.resolve()):
                                continue
                            try:
                                saved = json.loads((candidate / "session.json").read_text(encoding="utf-8"))
                            except (OSError, ValueError):
                                continue
                            if saved.get("session_id", saved.get("id")) == session_id:
                                folder = candidate
                                break
                    folders[session_id] = folder
                folder = folders[session_id]
                if folder is not None:
                    with (folder / "events.jsonl").open("a", encoding="utf-8") as file:
                        file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def _append_events(self, group, records):
        """Append diagnostics to one file while the caller holds the session lock."""
        with (self.session["folder"] / "events.jsonl").open("a", encoding="utf-8") as file:
            for record in records:
                file.write(json.dumps({**record, "event_group": group}, ensure_ascii=False,
                                      allow_nan=False) + "\n")

    def close(self):
        """Release the optional OCR worker during server shutdown."""
        self.bus_recognizer.close()

    def record_client_timings(self, session_id, records):
        """브라우저의 동일 시계 기준 지연과 실제 음성 시작 이벤트를 저장한다."""
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            self._append_events("client_timing", records)

    def heartbeat(self, session_id):
        """일시중지 중에도 브라우저가 열려 있음을 기록한다."""
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            self.session["last_seen"] = time.monotonic()

    def _close(self, session):
        """lock을 잡은 상태에서 세션 요약을 저장하고 진행 중 세션을 비운다."""
        summary = {
            "session_id": session["id"], "device_name": session["device_name"],
            "date": session["date"], "folder_name": session["folder_name"],
            "note": session["note"], "started_at": session["started_at"],
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "frame_count": session["frame_count"],
        }
        (session["folder"] / "session.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        self._completed_folders[session["id"]] = session["folder"]
        self._completed_summaries[session["id"]] = summary
        self.session = None
        self._last_frame_key = None
        self._last_frame_result = None
        self.bus_recognizer.set_target(None, None)
        return summary

    # 휴대폰 테스트 종료
    def stop(self, session_id):
        """세션 종료 시각과 처리 프레임 수를 저장한다."""
        with self.lock:
            session = self.session
            if session is None or session["id"] != session_id:
                cached = self._completed_summaries.get(session_id)
                if cached is not None:
                    return cached.copy()
                session = None
            if session is not None:
                return self._close(session)
        folder = self.completed_folder(session_id)
        return json.loads((folder / "session.json").read_text(encoding="utf-8"))

    def completed_folder(self, session_id):
        """Return a verified stopped-session folder for deferred clip uploads."""
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise SessionError("세션 번호가 올바르지 않습니다.")
        with self.lock:
            if self.session is not None and self.session["id"] == session_id:
                raise SessionError("녹화물은 테스트 종료 후 전송할 수 있습니다.")
            cached = self._completed_folders.get(session_id)
        candidates = [cached] if cached else self.output_dir.glob("[0-9]" * 8 + "/*")
        for folder in candidates:
            if folder is None:
                continue
            manifest = folder / "session.json"
            try:
                saved = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if saved.get("session_id", saved.get("id")) == session_id and saved.get("ended_at"):
                with self.lock:
                    self._completed_folders[session_id] = folder
                return folder
        raise SessionError("종료된 세션을 찾을 수 없습니다.")

    # 원본 카메라 영상의 세션별 경로 확인
    def camera_path(self, session_id):
        """현재 세션에 속하는 원본 카메라 영상 경로를 반환한다."""
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            return self.session["folder"] / "camera.mp4"

    # 오버레이 녹화 파일의 세션별 경로 확인
    def recording_path(self, session_id):
        """현재 세션에 속하는 오버레이 영상 경로를 반환한다."""
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            return self.session["folder"] / "camera_overlay.mp4"

    def record_video_event(self, session_id, kind, status, detail="", source="server"):
        """녹화·업로드 결과를 세션별 로그에 즉시 남긴다."""
        with self.lock:
            if self.session is None or self.session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            event = {
                "at": datetime.now(timezone.utc).isoformat(),
                "kind": kind, "status": status, "source": source, "detail": detail,
            }
            self._append_events("recording", [event])
