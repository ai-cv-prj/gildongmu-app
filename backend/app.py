"""
file_path: backend/app.py

휴대폰 브라우저에 실시간 테스트 화면과 추론 API를 제공한다.
"""

from pathlib import Path
import subprocess

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.session import SessionError, SessionManager
from src.video_audio import ffmpeg_executable


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
MAX_JPEG_BYTES = 3_000_000
MAX_RECORDING_BYTES = 250_000_000


class StartRequest(BaseModel):
    """휴대폰 모델과 테스트 메모를 받는다."""

    device_name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class StopRequest(BaseModel):
    """종료할 세션 번호를 받는다."""

    session_id: str


# FastAPI 앱과 테스트용 세션 저장소 생성
def create_app(manager=None):
    """테스트에서는 모델 저장소를 교체할 수 있는 API 앱을 반환한다."""
    app = FastAPI(title="길동무 실시간 테스트")
    sessions = manager or SessionManager(ROOT / "outputs" / "result_realtime")
    app.mount("/static/audio", StaticFiles(directory=ROOT / "assets" / "audio"),
              name="audio")
    app.mount("/static", StaticFiles(directory=FRONTEND), name="frontend")

    # 실행 상태 확인
    @app.get("/api/health")
    def health():
        """터널 스크립트가 서버 작동 여부를 확인한다."""
        return {"ok": True}

    # 휴대폰 화면 제공
    @app.get("/")
    def index():
        """한 화면에서 보행과 신호 결과를 볼 수 있는 페이지를 반환한다."""
        return FileResponse(FRONTEND / "index.html")

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
        content = image.file.read(MAX_JPEG_BYTES + 1)
        if not content or len(content) > MAX_JPEG_BYTES:
            raise HTTPException(413, "JPEG 크기가 허용 범위를 벗어났습니다.")
        decoded = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None or not 64 <= decoded.shape[0] <= 2160 or not 64 <= decoded.shape[1] <= 2160:
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
                    if total > MAX_RECORDING_BYTES:
                        raise HTTPException(413, "녹화 파일이 250MB를 초과했습니다.")
                    target.write(chunk)
            if total == 0:
                raise HTTPException(422, "녹화 파일이 비어 있습니다.")
            command = [
                ffmpeg_executable(), "-nostdin", "-y", "-v", "error", "-i", str(source),
                "-map", "0:v:0",
            ]
            command += ["-map", "0:a:0?", "-c:a", "aac"] if include_audio else ["-an"]
            command += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
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
            return sessions.stop(request.session_id)
        except SessionError as error:
            raise HTTPException(409, str(error)) from error

    return app


app = create_app()
