"""
file_path: backend/app.py

휴대폰 브라우저에 실시간 테스트 화면과 추론 API를 제공한다.
"""

from pathlib import Path
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import subprocess
import threading
import time
from typing import Annotated, Literal

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.session import SessionError, SessionManager
from backend.clips import ClipError, ClipStore, MAX_CHUNK_BYTES
from backend.logger import configure_app_logging
from backend.boarding import BoardingError
from backend.bus.arrival import BusArrivalError, BusArrivalService
from backend.bus.config import BusServiceSettings, load_bus_config
from backend.bus.speech import BusSpeechError, BusSpeechService
from src.audio_config import audio_directory
from src.settings import (
    DEFAULT_APP_CONFIG, DEFAULT_AUDIO_CONFIG, DEFAULT_PATHS_CONFIG,
    browser_settings, load_app_config, load_audio_settings, load_paths, resolve_path,
)
from src.video_audio import ffmpeg_executable


ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger(__name__)


class StartRequest(BaseModel):
    """휴대폰 모델과 테스트 메모를 받는다."""

    device_name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)
    bus_highres: bool = False


class StopRequest(BaseModel):
    """종료할 세션 번호를 받는다."""

    session_id: str


class BoardingRequest(BaseModel):
    action: Literal["arrive", "stop_announced", "submit", "cancel", "reopen"]
    arrival_event_id: int | None = Field(default=None, ge=1)
    bus_number: str | None = Field(default=None, max_length=30)


class RecordingEventRequest(BaseModel):
    """브라우저에서 감지한 녹화·업로드 실패를 받는다."""

    kind: Literal["camera", "overlay"]
    status: Literal["empty", "failed"]
    detail: str = Field(max_length=500)


class ClientTimingRecord(BaseModel):
    frame_id: int = Field(ge=1)
    kind: Literal["frame", "overlay", "audio"]
    captured_at_ms: int = Field(gt=0)
    occurred_at_ms: int | None = Field(default=None, gt=0)
    capture_ms: float | None = Field(default=None, ge=0)
    bus_capture_ms: float | None = Field(default=None, ge=0)
    bus_capture_error: str | None = Field(default=None, max_length=80)
    bus_capture_gap_ms: float | None = Field(default=None, ge=0)
    bus_result_frame_id: int | None = Field(default=None, ge=1)
    bus_result_age_ms: float | None = Field(default=None, ge=0)
    bus_jpeg_bytes: int | None = Field(default=None, ge=0)
    bus_long_side: int | None = Field(default=None, ge=0)
    camera_long_side: int | None = Field(default=None, ge=0)
    round_trip_ms: float | None = Field(default=None, ge=0)
    result_ms: float | None = Field(default=None, ge=0)
    overlay_delay_ms: float | None = Field(default=None, ge=0)
    audio_delay_ms: float | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=40)
    source: str | None = Field(default=None, max_length=20)
    action: Literal["left", "straight", "right", "stop"] | None = None
    event_ids: list[int] = Field(default_factory=list, max_length=20)
    recording_active: bool = False


class ClientTimingRequest(BaseModel):
    records: list[ClientTimingRecord] = Field(min_length=1, max_length=100)


class ClipCompleteRequest(BaseModel):
    mime_type: str = Field(min_length=1, max_length=100)
    chunk_count: int
    size_bytes: int
    started_at_ms: int
    ended_at_ms: int


# FastAPI 앱과 테스트용 세션 저장소 생성
def create_app(manager=None, app_config=DEFAULT_APP_CONFIG, paths_config=DEFAULT_PATHS_CONFIG,
               audio_config=DEFAULT_AUDIO_CONFIG, bus_config=None):
    """테스트에서는 모델 저장소를 교체할 수 있는 API 앱을 반환한다."""
    @asynccontextmanager
    async def lifespan(_app):
        configure_app_logging(sessions.output_dir)
        log.info("mobile server started results=%s", sessions.output_dir)
        for session_id, clip_id in clip_store.pending_exports():
            queue_export(session_id, clip_id)
        try:
            yield
        finally:
            exporter.shutdown(wait=True)
            sessions.close()
            log.info("mobile server stopped")

    app = FastAPI(title="길동무 실시간 테스트", lifespan=lifespan)
    settings = load_app_config(app_config)
    audio = load_audio_settings(audio_config)
    paths = load_paths(paths_config)
    frontend = resolve_path(paths["frontend_dir"])
    upload = settings["upload"]
    recording_settings = settings["recording"]
    bus_settings = bus_config or load_bus_config()
    bus_arrivals = BusArrivalService(BusServiceSettings.from_config(bus_settings))
    bus_speech = BusSpeechService(bus_settings.speech_timeout_sec)
    if manager is None:
        manager = SessionManager(resolve_path(paths["session_dir"]), session_settings=settings["session"],
                                 bus_config=bus_settings)
    sessions = manager
    clip_store = ClipStore(sessions, max_jpeg_bytes=upload["max_jpeg_bytes"],
                           recording_fps=recording_settings["fps"])
    exporter = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip-export")
    export_jobs = set()
    export_jobs_lock = threading.Lock()

    def queue_export(session_id, clip_id):
        future = exporter.submit(clip_store.export, session_id, clip_id)
        with export_jobs_lock:
            export_jobs.add(future)
        def completed(done):
            with export_jobs_lock:
                export_jobs.discard(done)
        future.add_done_callback(completed)

    def export_busy():
        with export_jobs_lock:
            return any(not future.done() for future in export_jobs)
    app.state.bus_arrivals = bus_arrivals
    app.state.bus_speech = bus_speech
    app.state.clip_store = clip_store

    @app.exception_handler(BusArrivalError)
    async def bus_arrival_error(_request: Request, error: BusArrivalError):
        return JSONResponse(status_code=error.status_code,
                            content={"error": {"code": error.code, "message": error.message},
                                     "detail": error.message})

    @app.exception_handler(BusSpeechError)
    async def bus_speech_error(_request: Request, error: BusSpeechError):
        return JSONResponse(status_code=error.status_code,
                            content={"error": {"code": error.code, "message": error.message},
                                     "detail": error.message})

    @app.exception_handler(ClipError)
    async def clip_error(_request: Request, error: ClipError):
        log.warning("clip request rejected: %s", error)
        return JSONResponse(status_code=error.status_code, content={"detail": str(error)})

    @app.middleware("http")
    async def disable_frontend_cache(request, call_next):
        """현장 테스트 중 변경된 HTML·JS·CSS가 휴대폰 캐시에 남지 않게 한다."""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/audio", StaticFiles(directory=audio_directory(audio_config, paths_config)),
              name="audio")
    app.mount("/static", StaticFiles(directory=frontend), name="frontend")

    # 브라우저에 필요한 설정만 공개
    @app.get("/api/config")
    def config():
        """카메라·녹화·음성 옵션을 제공하고 이전 서버의 설정 캐시를 막는다."""
        return JSONResponse(browser_settings(settings, audio), headers={"Cache-Control": "no-store"})

    # 실행 상태 확인
    @app.get("/api/health")
    def health():
        """터널 스크립트가 서버 작동 여부를 확인한다."""
        return {"ok": True}

    @app.get("/api/bus/status")
    def bus_status():
        return bus_settings.readiness(api_key_configured=bool(bus_arrivals.service_key))

    @app.get("/api/nearby-bus-arrival")
    def nearby_bus_arrival(
        bus_number: str = Query(..., min_length=1, max_length=20),
        latitude: float = Query(..., ge=-90, le=90),
        longitude: float = Query(..., ge=-180, le=180),
        accuracy_m: float | None = Query(None, ge=0, le=5000),
    ):
        return bus_arrivals.lookup_nearby(bus_number, latitude, longitude, accuracy_m)

    @app.get("/api/bus-arrival-speech")
    def bus_arrival_speech(text: str = Query(..., min_length=1, max_length=200)):
        return Response(content=bus_speech.synthesize(text), media_type="audio/mpeg",
                        headers={"Cache-Control": "public, max-age=3600"})

    # 휴대폰 화면 제공
    @app.get("/")
    def index():
        """한 화면에서 보행과 신호 결과를 볼 수 있는 페이지를 반환한다."""
        return FileResponse(frontend / "index.html")

    # 세션 시작
    @app.post("/api/sessions")
    def start(request: StartRequest):
        """지정한 휴대폰 모델로 테스트를 시작한다."""
        if not request.device_name.strip():
            raise HTTPException(422, "휴대폰 기종을 입력하세요.")
        try:
            with clip_store.lock:
                if export_busy():
                    raise SessionError("선택 영상 변환이 끝나면 새 테스트를 시작할 수 있습니다.")
                created = sessions.start(request.device_name.strip(), request.note.strip(),
                                         bus_highres=request.bus_highres)
            log.info("session started id=%s", created["session_id"])
            return created
        except SessionError as error:
            log.warning("session start failed: %s", error)
            raise HTTPException(409, str(error)) from error

    # JPEG 한 프레임 추론
    @app.post("/api/sessions/{session_id}/frames")
    def frame(session_id: str, frame_id: int = Form(...), captured_at_ms: int = Form(...),
              image: UploadFile = File(...),
              bus_image: Annotated[UploadFile | None, File()] = None,
              bus_captured_at_ms: Annotated[int | None, Form()] = None):
        """휴대폰 JPEG를 디코딩하고 도보와 신호를 함께 추론한다."""
        request_start_ns = time.perf_counter_ns()
        if frame_id < 1 or captured_at_ms <= 0:
            raise HTTPException(422, "프레임 번호나 촬영 시간이 올바르지 않습니다.")
        content = image.file.read(upload["max_jpeg_bytes"] + 1)
        if not content or len(content) > upload["max_jpeg_bytes"]:
            raise HTTPException(413, "JPEG 크기가 허용 범위를 벗어났습니다.")
        decoded = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        minimum, maximum = upload["min_frame_side"], upload["max_frame_side"]
        if decoded is None or not minimum <= decoded.shape[0] <= maximum or not minimum <= decoded.shape[1] <= maximum:
            raise HTTPException(422, "읽을 수 있는 카메라 JPEG가 아닙니다.")
        if (bus_image is None) != (bus_captured_at_ms is None):
            raise HTTPException(422, "버스 프레임과 촬영 시간을 함께 보내세요.")
        bus_content = None
        bus_decoded = None
        if bus_image is not None:
            if not captured_at_ms <= bus_captured_at_ms <= captured_at_ms + 2000:
                raise HTTPException(422, "버스 프레임 촬영 시간이 올바르지 않습니다.")
            bus_content = bus_image.file.read(upload["max_jpeg_bytes"] + 1)
            if not bus_content or len(bus_content) > upload["max_jpeg_bytes"]:
                raise HTTPException(413, "버스 JPEG 크기가 허용 범위를 벗어났습니다.")
            bus_decoded = cv2.imdecode(np.frombuffer(bus_content, dtype=np.uint8), cv2.IMREAD_COLOR)
            if (bus_decoded is None or not minimum <= bus_decoded.shape[0] <= maximum
                    or not minimum <= bus_decoded.shape[1] <= maximum):
                raise HTTPException(422, "읽을 수 있는 버스 JPEG가 아닙니다.")
        try:
            return sessions.process(session_id, frame_id, captured_at_ms, decoded,
                                    request_start_ns=request_start_ns,
                                    decode_ms=round((time.perf_counter_ns()-request_start_ns)/1e6, 1),
                                    image_bytes=content, save_live_frame=False,
                                    bus_frame=bus_decoded, bus_captured_at_ms=bus_captured_at_ms)
        except SessionError as error:
            log.warning("frame rejected session=%s frame=%d: %s", session_id, frame_id, error)
            raise HTTPException(409, str(error)) from error

    @app.post("/api/sessions/{session_id}/timings")
    def client_timings(session_id: str, request: ClientTimingRequest):
        """프레임 루프와 분리해 모은 지연 기록을 세션에 추가한다."""
        try:
            sessions.record_client_timings(session_id,
                [record.model_dump() for record in request.records])
        except SessionError as error:
            raise HTTPException(409, str(error)) from error
        return {"saved": len(request.records)}

    @app.get("/api/sessions/{session_id}/boarding")
    def boarding_state(session_id: str):
        try:
            return sessions.boarding_state(session_id)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error

    @app.put("/api/sessions/{session_id}/boarding")
    def boarding_action(session_id: str, request: BoardingRequest):
        try:
            return sessions.update_boarding(session_id, request.action,
                                            request.arrival_event_id, request.bus_number)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error
        except BoardingError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/sessions/{session_id}/bus-events")
    async def bus_events(session_id: str, request: Request):
        """Store at most 25 small GPS/OCR guidance events per request."""
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 64 * 1024:
                raise HTTPException(413, "버스 이벤트 요청은 64KB 이하여야 합니다.")
            raw.extend(chunk)
        try:
            payload = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            # Reject overflowed JSON numbers (for example 1e999) before writing
            # any event so malformed batches cannot partially reach the log.
            json.dumps(payload, allow_nan=False)
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise HTTPException(422, "버스 이벤트 JSON이 올바르지 않습니다.") from error
        events = payload.get("events") if isinstance(payload, dict) else None
        if (not isinstance(events, list) or not 1 <= len(events) <= 25
                or any(not isinstance(event, dict) or not isinstance(event.get("type"), str)
                       or not 1 <= len(event["type"]) <= 80 for event in events)):
            raise HTTPException(422, "버스 이벤트 형식이 올바르지 않습니다.")
        try:
            sessions.record_bus_events(session_id, events)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error
        return {"saved": len(events)}

    # 브라우저 녹화물을 MP4로 변환해 저장
    def save_video(session_id, kind, video, path, include_audio):
        """용량을 확인하고 임시 업로드를 영상별 MP4로 변환한다."""
        source = path.with_suffix(".upload.webm")
        partial = path.with_suffix(".partial.mp4")
        total = 0
        try:
            with source.open("wb") as target:
                while chunk := video.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > upload["max_recording_bytes"]:
                        raise HTTPException(413, f"녹화 파일이 {upload['max_recording_bytes'] / 1000000:g}MB를 초과했습니다.")
                    target.write(chunk)
            if total == 0:
                raise HTTPException(422, "녹화 파일이 비어 있습니다.")
            command = [
                ffmpeg_executable(), "-nostdin", "-y", "-v", "error", "-i", str(source),
                "-map", "0:v:0",
            ]
            command += ["-map", "0:a:0?", "-c:a", "aac"] if include_audio else ["-an"]
            command += ["-vf", f"fps={recording_settings['fps']}", "-c:v", "libx264",
                        "-preset", recording_settings["preset"], "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(partial)]
            subprocess.run(command, check=True, capture_output=True)
            partial.replace(path)
        except HTTPException as error:
            status = "empty" if total == 0 else "failed"
            sessions.record_video_event(session_id, kind, status, str(error.detail))
            raise
        except (subprocess.CalledProcessError, OSError) as error:
            if isinstance(error, subprocess.CalledProcessError):
                failure = HTTPException(422, "녹화 영상을 MP4로 변환할 수 없습니다.")
                cause = (error.stderr or b"").decode("utf-8", errors="replace").strip()
            else:
                failure = HTTPException(500, "녹화 파일을 저장할 수 없습니다.")
                cause = str(error)
            detail = f"{failure.detail} {cause[-500:]}".strip()
            sessions.record_video_event(session_id, kind, "failed", detail)
            raise failure from error
        finally:
            source.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
        sessions.record_video_event(session_id, kind, "saved", f"{path.stat().st_size} bytes")
        return {"saved": True, "bytes": path.stat().st_size}

    @app.post("/api/sessions/{session_id}/recording-events")
    def recording_event(session_id: str, event: RecordingEventRequest):
        """브라우저에서 발생한 빈 녹화물과 전송 실패를 기록한다."""
        try:
            sessions.record_video_event(
                session_id, event.kind, event.status, event.detail, source="browser"
            )
        except SessionError as error:
            raise HTTPException(409, str(error)) from error
        return {"recorded": True}

    @app.post("/api/sessions/{session_id}/clips/{clip_id}/chunks/{index}")
    def clip_chunk(session_id: str, clip_id: int, index: int, video: UploadFile = File(...)):
        """Accept a bounded camera WebM or MP4 chunk after the session ends."""
        data = video.file.read(MAX_CHUNK_BYTES + 1)
        return clip_store.add_chunk(session_id, clip_id, index, data)

    @app.post("/api/sessions/{session_id}/clips/{clip_id}/frames/{frame_id}")
    def clip_frame(session_id: str, clip_id: int, frame_id: int,
                   captured_at_ms: int = Form(...), image: UploadFile = File(...),
                   mask_png: str | None = Form(None)):
        """Store one JPEG proven to be the same bytes used in live inference."""
        data = image.file.read(upload["max_jpeg_bytes"] + 1)
        return clip_store.add_frame(session_id, clip_id, frame_id, captured_at_ms, data, mask_png)

    @app.post("/api/sessions/{session_id}/clips/{clip_id}/complete")
    def clip_complete(session_id: str, clip_id: int, request: ClipCompleteRequest):
        """Close a clip and queue offline rendering on a dedicated worker."""
        with clip_store.lock:
            manifest, started = clip_store.complete(session_id, clip_id, **request.model_dump())
            if started:
                queue_export(session_id, clip_id)
        return manifest

    @app.get("/api/sessions/{session_id}/clips")
    def clips(session_id: str):
        return clip_store.status(session_id)

    # 오버레이와 안내 음성이 포함된 선택형 영상 업로드
    @app.post("/api/sessions/{session_id}/recording")
    def recording(session_id: str, video: UploadFile = File(...)):
        """브라우저의 오버레이 영상을 camera_overlay.mp4로 저장한다."""
        try:
            path = sessions.recording_path(session_id)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error
        return save_video(session_id, "overlay", video, path, include_audio=True)

    # 오버레이와 현장 소리가 없는 원본 카메라 영상 업로드
    @app.post("/api/sessions/{session_id}/camera")
    def camera(session_id: str, video: UploadFile = File(...)):
        """카메라 영상에서 오디오를 제거하고 camera.mp4로 저장한다."""
        try:
            path = sessions.camera_path(session_id)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error
        return save_video(session_id, "camera", video, path, include_audio=False)

    # 테스트 종료
    @app.post("/api/sessions/stop")
    def stop(request: StopRequest):
        """세션 결과 요약을 저장하고 반환한다."""
        try:
            summary = sessions.stop(request.session_id)
            log.info("session stopped id=%s frames=%d", request.session_id, summary["frame_count"])
            folder = (sessions.output_dir / summary["date"] / summary["folder_name"]).resolve()
            summary["storage_path"] = (
                folder.relative_to(ROOT).as_posix() if folder.is_relative_to(ROOT) else str(folder)
            )
            return summary
        except SessionError as error:
            raise HTTPException(409, str(error)) from error

    return app


app = create_app()
