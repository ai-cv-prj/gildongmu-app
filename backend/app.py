"""
file_path: backend/app.py

휴대폰 브라우저에 실시간 테스트 화면과 추론 API를 제공한다.
"""

from pathlib import Path
import subprocess

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.session import SessionError, SessionManager
from src.audio_config import audio_directory
from src.settings import (
    DEFAULT_APP_CONFIG, DEFAULT_AUDIO_CONFIG, DEFAULT_PATHS_CONFIG,
    browser_settings, load_app_config, load_audio_settings, load_paths, resolve_path,
)
from src.video_audio import ffmpeg_executable


ROOT = Path(__file__).resolve().parents[1]


class StartRequest(BaseModel):
    """휴대폰 모델과 테스트 메모를 받는다."""

    device_name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class StopRequest(BaseModel):
    """종료할 세션 번호를 받는다."""

    session_id: str


# FastAPI 앱과 테스트용 세션 저장소 생성
def create_app(manager=None, app_config=DEFAULT_APP_CONFIG, paths_config=DEFAULT_PATHS_CONFIG,
               audio_config=DEFAULT_AUDIO_CONFIG):
    """테스트에서는 모델 저장소를 교체할 수 있는 API 앱을 반환한다."""
    app = FastAPI(title="길동무 실시간 테스트")
    settings = load_app_config(app_config)
    audio = load_audio_settings(audio_config)
    paths = load_paths(paths_config)
    frontend = resolve_path(paths["frontend_dir"])
    upload = settings["upload"]
    recording_settings = settings["recording"]
    if manager is None:
        manager = SessionManager(resolve_path(paths["session_dir"]), session_settings=settings["session"])
    sessions = manager
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
            return sessions.start(request.device_name.strip(), request.note.strip())
        except SessionError as error:
            raise HTTPException(409, str(error)) from error

    # JPEG 한 프레임 추론
    @app.post("/api/sessions/{session_id}/frames")
    def frame(session_id: str, frame_id: int = Form(...), captured_at_ms: int = Form(...),
              image: UploadFile = File(...)):
        """휴대폰 JPEG를 디코딩하고 도보와 신호를 함께 추론한다."""
        if frame_id < 1 or captured_at_ms <= 0:
            raise HTTPException(422, "프레임 번호나 촬영 시간이 올바르지 않습니다.")
        content = image.file.read(upload["max_jpeg_bytes"] + 1)
        if not content or len(content) > upload["max_jpeg_bytes"]:
            raise HTTPException(413, "JPEG 크기가 허용 범위를 벗어났습니다.")
        decoded = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        minimum, maximum = upload["min_frame_side"], upload["max_frame_side"]
        if decoded is None or not minimum <= decoded.shape[0] <= maximum or not minimum <= decoded.shape[1] <= maximum:
            raise HTTPException(422, "읽을 수 있는 카메라 JPEG가 아닙니다.")
        try:
            return sessions.process(session_id, frame_id, captured_at_ms, decoded)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error

    # 브라우저 녹화물을 MP4로 변환해 저장
    def save_video(video, path, include_audio):
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
        except subprocess.CalledProcessError as error:
            raise HTTPException(422, "녹화 영상을 MP4로 변환할 수 없습니다.") from error
        finally:
            source.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
        return {"saved": True, "bytes": path.stat().st_size}

    # 오버레이와 안내 음성이 포함된 선택형 영상 업로드
    @app.post("/api/sessions/{session_id}/recording")
    def recording(session_id: str, video: UploadFile = File(...)):
        """브라우저의 오버레이 영상을 camera_overlay.mp4로 저장한다."""
        try:
            path = sessions.recording_path(session_id)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error
        return save_video(video, path, include_audio=True)

    # 오버레이와 현장 소리가 없는 원본 카메라 영상 업로드
    @app.post("/api/sessions/{session_id}/camera")
    def camera(session_id: str, video: UploadFile = File(...)):
        """카메라 영상에서 오디오를 제거하고 camera.mp4로 저장한다."""
        try:
            path = sessions.camera_path(session_id)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error
        return save_video(video, path, include_audio=False)

    # 테스트 종료
    @app.post("/api/sessions/stop")
    def stop(request: StopRequest):
        """세션 결과 요약을 저장하고 반환한다."""
        try:
            summary = sessions.stop(request.session_id)
            folder = (sessions.output_dir / summary["date"] / summary["folder_name"]).resolve()
            summary["storage_path"] = (
                folder.relative_to(ROOT).as_posix() if folder.is_relative_to(ROOT) else str(folder)
            )
            return summary
        except SessionError as error:
            raise HTTPException(409, str(error)) from error

    return app


app = create_app()
