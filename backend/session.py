"""
file_path: backend/session.py

실시간 테스트 세션과 프레임별 추론 기록을 관리한다.
"""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.inference import RealtimeInference
from backend.response import make_response


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
    def __init__(self, output_dir, model_factory=RealtimeInference):
        """모델은 첫 세션 시작 시에만 로딩하고 이후 세션에서 재사용한다."""
        self.output_dir = Path(output_dir)
        self.model_factory = model_factory
        self.models = None
        self.session = None
        self.lock = threading.Lock()

    # 휴대폰 테스트 시작
    def start(self, device_name, note=""):
        """새 세션을 만들고 모델 추적 상태를 초기화한다."""
        with self.lock:
            if self.session is not None:
                raise SessionError("이미 진행 중인 테스트가 있습니다. 먼저 종료하세요.")
            if self.models is None:
                self.models = self.model_factory()
            else:
                self.models.reset()
            session_id = uuid4().hex
            safe_device = safe_folder_part(device_name, "기종미상")
            safe_note = safe_folder_part(note, max_length=40)
            started_at = datetime.now(ZoneInfo("Asia/Seoul"))
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
            self.session = {
                "id": session_id, "device_name": device_name, "note": note,
                "date": date, "folder_name": folder_name,
                "started_at": started_at.astimezone(timezone.utc).isoformat(),
                "frame_count": 0, "last_frame_id": 0, "last_capture_ms": None,
                "folder": folder,
            }
            (folder / "session.json").write_text(json.dumps({
                key: value for key, value in self.session.items() if key != "folder"
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"session_id": session_id, "device_name": device_name,
                    "date": date, "folder_name": folder_name}

    # 한 프레임 처리와 JSONL 기록
    def process(self, session_id, frame_id, captured_at_ms, frame):
        """요청 순서를 검증하고 세 모델 결과를 저장한다."""
        with self.lock:
            session = self.session
            if session is None or session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다. 다시 시작하세요.")
            if frame_id != session["last_frame_id"] + 1:
                raise SessionError("프레임 번호가 연속적이지 않습니다.")
            if session["last_capture_ms"] is not None and captured_at_ms <= session["last_capture_ms"]:
                raise SessionError("촬영 시간이 이전 프레임보다 늦지 않습니다.")
            risk, signal, class_map, label_ids, elapsed = self.models.predict(
                frame, frame_id, captured_at_ms,
            )
            result = make_response(session_id, frame_id, captured_at_ms, frame,
                                   risk, signal, class_map, label_ids, elapsed)
            # 응답의 큰 PNG는 로그에서 제외하고 판단 결과와 선택 대상만 기록한다.
            record = {
                **{key: value for key, value in result.items() if key not in ("walking", "traffic")},
                "walking": {key: value for key, value in result["walking"].items() if key != "mask_png"},
                "traffic": result["traffic"],
            }
            with (session["folder"] / "results.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            session["frame_count"] += 1
            session["last_frame_id"] = frame_id
            session["last_capture_ms"] = captured_at_ms
            return result

    # 휴대폰 테스트 종료
    def stop(self, session_id):
        """세션 종료 시각과 처리 프레임 수를 저장한다."""
        with self.lock:
            session = self.session
            if session is None or session["id"] != session_id:
                raise SessionError("진행 중인 세션이 없습니다.")
            summary = {
                "session_id": session_id, "device_name": session["device_name"],
                "date": session["date"], "folder_name": session["folder_name"],
                "note": session["note"], "started_at": session["started_at"],
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "frame_count": session["frame_count"],
            }
            (session["folder"] / "session.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            self.session = None
            return summary

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
