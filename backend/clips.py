"""Deferred, bounded camera clips and rendered inference videos."""

import base64
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from backend.session import SessionError
from src.video_audio import ffmpeg_executable


log = logging.getLogger(__name__)
MAX_CLIPS = 5
MAX_CLIP_MS = 30_000
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 120 * 1024 * 1024
MAX_MASK_BYTES = 2 * 1024 * 1024
MAX_FRAMES_PER_CLIP = 300
MAX_FRAME_BYTES_PER_CLIP = 32 * 1024 * 1024


class ClipError(Exception):
    def __init__(self, message, status_code=422):
        super().__init__(message)
        self.status_code = status_code


def _write_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_bytes(path, value):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(value)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ClipStore:
    """Accept selected clips only after inference has stopped."""

    def __init__(self, sessions, *, max_jpeg_bytes, recording_fps=10):
        self.sessions = sessions
        self.max_jpeg_bytes = max_jpeg_bytes
        self.recording_fps = recording_fps
        self.lock = threading.RLock()
        self._proofs = OrderedDict()

    def _session(self, session_id, *, require_idle=False):
        if require_idle and self.sessions.session is not None:
            raise ClipError("추론 세션이 진행 중일 때는 녹화물을 전송할 수 없습니다.", 409)
        try:
            return self.sessions.completed_folder(session_id)
        except SessionError as error:
            raise ClipError(str(error), 409) from error

    def _clip(self, folder, clip_id, create=False):
        if not 1 <= clip_id <= MAX_CLIPS:
            raise ClipError(f"클립 번호는 1~{MAX_CLIPS}여야 합니다.")
        clip = folder / "clips" / f"clip_{clip_id:03d}"
        if create and not (clip / "manifest.json").is_file():
            (clip / "chunks").mkdir(parents=True, exist_ok=True)
            (clip / "frames").mkdir(exist_ok=True)
            (clip / "masks").mkdir(exist_ok=True)
        return clip

    @staticmethod
    def _cleanup_inputs(clip, manifest):
        """Remove render inputs only after the durable final outputs exist."""
        if manifest.get("storage_version") != 2 or manifest.get("state") not in ("ready", "no_frames"):
            return
        original = clip / Path(manifest["original_path"]).name
        if not original.is_file() or (manifest["state"] == "ready" and not (clip / "inference.mp4").is_file()):
            return
        for name in ("chunks", "frames", "masks"):
            try:
                if (clip / name).exists():
                    shutil.rmtree(clip / name)
            except OSError:
                # A cleanup failure must not turn a playable clip into a failed export.
                log.warning("clip input cleanup failed: %s", clip / name, exc_info=True)

    @staticmethod
    def _manifest(clip):
        path = clip / "manifest.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ClipError("클립 상태 파일을 읽을 수 없습니다.", 500) from error

    @staticmethod
    def _input_size(folder):
        total = 0
        for clip in (folder / "clips").glob("clip_*/*"):
            if clip.is_file() and clip.name in ("original.webm", "original.mp4"):
                total += clip.stat().st_size
        for pattern in ("clip_*/chunks/*.part", "clip_*/frames/*.jpg", "clip_*/masks/*.png",
                        "clip_*/overlays/*.png"):
            total += sum(path.stat().st_size for path in (folder / "clips").glob(pattern))
        return total

    def _capacity(self, folder, path, data):
        previous = path.stat().st_size if path.exists() else 0
        if self._input_size(folder) - previous + len(data) > MAX_TOTAL_BYTES:
            raise ClipError("세션의 선택 영상·프레임 120MiB 저장 한도를 넘었습니다.", 413)

    def _proof(self, folder, frame_id):
        key = str(folder)
        if key not in self._proofs:
            proofs = {}
            result_path = folder / "results.jsonl"
            if result_path.is_file():
                with result_path.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            row = json.loads(line)
                            proofs[row["frame_id"]] = (row["captured_at_ms"], row.get("frame_sha256"))
                        except (ValueError, KeyError, TypeError):
                            continue
            self._proofs[key] = proofs
            if len(self._proofs) > 2:
                self._proofs.popitem(last=False)
        else:
            self._proofs.move_to_end(key)
        return self._proofs[key].get(frame_id)

    def add_chunk(self, session_id, clip_id, index, data):
        if not 0 <= index < 30:
            raise ClipError("청크 번호가 허용 범위를 벗어났습니다.")
        if not data or len(data) > MAX_CHUNK_BYTES:
            raise ClipError("영상 청크는 1바이트~4MiB여야 합니다.", 413)
        with self.lock:
            folder = self._session(session_id, require_idle=True)
            clip = self._clip(folder, clip_id, create=True)
            manifest = self._manifest(clip)
            if manifest:
                if index < manifest["chunk_count"]:
                    expected_size = (MAX_CHUNK_BYTES if index < manifest["chunk_count"] - 1
                                     else manifest["size_bytes"] - index * MAX_CHUNK_BYTES)
                    if len(data) == expected_size:
                        with (folder / manifest["original_path"]).open("rb") as source:
                            source.seek(index * MAX_CHUNK_BYTES)
                            if source.read(len(data)) == data:
                                return {"saved": True, "clip_id": clip_id, "chunk_index": index, "bytes": len(data)}
                raise ClipError("완료된 클립의 영상 청크를 바꿀 수 없습니다.", 409)
            path = clip / "chunks" / f"{index:03d}.part"
            if path.exists():
                if path.read_bytes() == data:
                    return {"saved": True, "clip_id": clip_id, "chunk_index": index, "bytes": len(data)}
                raise ClipError("같은 번호의 영상 청크 내용이 다릅니다.", 409)
            self._capacity(folder, path, data)
            _write_bytes(path, data)
            return {"saved": True, "clip_id": clip_id, "chunk_index": index, "bytes": len(data)}

    def add_frame(self, session_id, clip_id, frame_id, captured_at_ms, image, mask_png=None,
                  overlay_png=None):
        if frame_id < 1 or captured_at_ms <= 0:
            raise ClipError("프레임 번호나 촬영 시각이 올바르지 않습니다.")
        if not image or len(image) > self.max_jpeg_bytes:
            raise ClipError("선택 JPEG 크기가 허용 범위를 벗어났습니다.", 413)
        if not image.startswith(b"\xff\xd8") or cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR) is None:
            raise ClipError("선택 JPEG가 손상되었습니다.")
        digest = hashlib.sha256(image).hexdigest()
        if mask_png and len(mask_png) > (MAX_MASK_BYTES * 4 // 3 + 8):
            raise ClipError("분할 마스크가 너무 큽니다.", 413)
        try:
            mask = base64.b64decode(mask_png, validate=True) if mask_png else None
        except (ValueError, base64.binascii.Error) as error:
            raise ClipError("분할 마스크 형식이 올바르지 않습니다.") from error
        if mask is not None and (len(mask) > MAX_MASK_BYTES or not mask.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ClipError("분할 마스크 형식이나 크기가 올바르지 않습니다.", 413)
        if overlay_png and len(overlay_png) > (MAX_MASK_BYTES * 4 // 3 + 8):
            raise ClipError("화면 오버레이가 너무 큽니다.", 413)
        try:
            overlay = base64.b64decode(overlay_png, validate=True) if overlay_png else None
        except (ValueError, base64.binascii.Error) as error:
            raise ClipError("화면 오버레이 형식이 올바르지 않습니다.") from error
        if overlay is not None and (len(overlay) > MAX_MASK_BYTES or not overlay.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ClipError("화면 오버레이 형식이나 크기가 올바르지 않습니다.", 413)
        if overlay is not None:
            decoded = cv2.imdecode(np.frombuffer(overlay, np.uint8), cv2.IMREAD_UNCHANGED)
            if decoded is None or decoded.ndim != 3 or decoded.shape[2] != 4:
                raise ClipError("화면 오버레이에 투명 채널이 없습니다.")
        with self.lock:
            folder = self._session(session_id, require_idle=True)
            proof = self._proof(folder, frame_id)
            if proof != (captured_at_ms, digest):
                raise ClipError("추론에 사용한 프레임·촬영 시각·JPEG 해시가 일치하지 않습니다.", 409)
            clip = self._clip(folder, clip_id, create=True)
            manifest = self._manifest(clip)
            path = clip / "frames" / f"{frame_id:06d}.jpg"
            meta = path.with_suffix(".json")
            mask_path = clip / "masks" / f"{frame_id:06d}.png"
            overlay_path = clip / "overlays" / f"{frame_id:06d}.png"
            if path.exists() and meta.exists():
                previous = json.loads(meta.read_text(encoding="utf-8"))
                if previous.get("sha256") == digest and previous.get("captured_at_ms") == captured_at_ms:
                    return {"saved": True, "clip_id": clip_id, "frame_id": frame_id}
                raise ClipError("같은 번호의 선택 프레임 내용이 다릅니다.", 409)
            if manifest:
                if any(item["frame_id"] == frame_id and item["captured_at_ms"] == captured_at_ms
                       and item.get("sha256") == digest for item in manifest.get("frames", [])):
                    return {"saved": True, "clip_id": clip_id, "frame_id": frame_id}
                raise ClipError("완료된 클립에 새 선택 프레임을 추가할 수 없습니다.", 409)
            # The metadata is written last. A restart between those writes may
            # leave JPEG/mask files with no committed frame; retry replaces them.
            if path.exists() or meta.exists() or mask_path.exists() or overlay_path.exists():
                path.unlink(missing_ok=True)
                mask_path.unlink(missing_ok=True)
                overlay_path.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
            frame_paths = list((clip / "frames").glob("*.jpg"))
            if len(frame_paths) >= MAX_FRAMES_PER_CLIP:
                raise ClipError("클립당 선택 프레임 300개 한도를 넘었습니다.", 413)
            stored_frame_bytes = sum(item.stat().st_size for item in frame_paths)
            stored_frame_bytes += sum(item.stat().st_size for item in (clip / "masks").glob("*.png"))
            stored_frame_bytes += sum(item.stat().st_size for item in (clip / "overlays").glob("*.png"))
            frame_bytes = len(image) + len(mask or b"") + len(overlay or b"")
            if stored_frame_bytes + frame_bytes > MAX_FRAME_BYTES_PER_CLIP:
                raise ClipError("클립당 선택 프레임 32MiB 한도를 넘었습니다.", 413)
            if self._input_size(folder) + frame_bytes > MAX_TOTAL_BYTES:
                raise ClipError("세션의 선택 영상·프레임 120MiB 저장 한도를 넘었습니다.", 413)
            try:
                _write_bytes(path, image)
                if mask is not None:
                    _write_bytes(mask_path, mask)
                if overlay is not None:
                    overlay_path.parent.mkdir(exist_ok=True)
                    _write_bytes(overlay_path, overlay)
                _write_json(meta, {"frame_id": frame_id, "captured_at_ms": captured_at_ms,
                                   "sha256": digest, "mask": mask is not None,
                                   "overlay": overlay is not None})
            except OSError:
                path.unlink(missing_ok=True)
                mask_path.unlink(missing_ok=True)
                overlay_path.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                raise
            return {"saved": True, "clip_id": clip_id, "frame_id": frame_id}

    @staticmethod
    def _validate_window(start, end):
        if type(start) is not int or type(end) is not int or start <= 0 or end <= start or end - start > MAX_CLIP_MS:
            raise ClipError("클립은 30초 이하의 양수 시각 구간이어야 합니다.")

    @staticmethod
    def _video_duration_ms(path):
        """Decode the uploaded camera track after inference to verify its duration."""
        command = [ffmpeg_executable(), "-nostdin", "-hide_banner", "-v", "error", "-i", str(path),
                   "-map", "0:v:0", "-f", "null", "-", "-progress", "pipe:1", "-nostats"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as error:
            raise ClipError("원본 영상 길이를 확인할 수 없습니다.") from error
        values = re.findall(r"^out_time_us=(\d+)$", result.stdout, re.MULTILINE)
        if result.returncode or not values:
            raise ClipError("원본 영상을 읽을 수 없습니다.")
        return int(values[-1]) // 1000

    def complete(self, session_id, clip_id, *, mime_type, chunk_count, size_bytes, started_at_ms, ended_at_ms):
        self._validate_window(started_at_ms, ended_at_ms)
        container_type = mime_type.split(";", 1)[0]
        if container_type not in ("video/webm", "video/mp4"):
            raise ClipError("WebM 또는 MP4 원본 영상만 받을 수 있습니다.")
        if type(chunk_count) is not int or not 1 <= chunk_count <= 30 or type(size_bytes) is not int or size_bytes <= 0:
            raise ClipError("영상 청크 수나 크기가 올바르지 않습니다.")
        with self.lock:
            folder = self._session(session_id, require_idle=True)
            clip = self._clip(folder, clip_id)
            if not clip.is_dir():
                raise ClipError("업로드된 클립이 없습니다.", 409)
            manifest = self._manifest(clip)
            if manifest:
                same = all(manifest.get(key) == value for key, value in {
                    "mime_type": mime_type, "chunk_count": chunk_count, "size_bytes": size_bytes,
                    "started_at_ms": started_at_ms, "ended_at_ms": ended_at_ms,
                }.items())
                if same:
                    if manifest.get("state") == "failed":
                        manifest["state"] = "pending"
                        manifest["error"] = None
                        _write_json(clip / "manifest.json", manifest)
                        return manifest, True
                    return manifest, False
                raise ClipError("완료된 클립의 내용을 바꿀 수 없습니다.", 409)
            for other_id in range(1, MAX_CLIPS + 1):
                if other_id == clip_id:
                    continue
                other = self._manifest(self._clip(folder, other_id))
                if other and max(started_at_ms, other["started_at_ms"]) < min(ended_at_ms, other["ended_at_ms"]):
                    raise ClipError("클립 촬영 구간이 겹칩니다.", 409)
            chunks = [clip / "chunks" / f"{index:03d}.part" for index in range(chunk_count)]
            if any(not path.is_file() for path in chunks):
                raise ClipError("업로드되지 않은 영상 청크가 있습니다.", 409)
            if any(path.stat().st_size != MAX_CHUNK_BYTES for path in chunks[:-1]):
                raise ClipError("마지막을 제외한 영상 청크는 4MiB여야 합니다.", 409)
            if sum(path.stat().st_size for path in chunks) != size_bytes:
                raise ClipError("영상 청크의 전체 크기가 일치하지 않습니다.", 409)
            frames = sorted((clip / "frames").glob("*.json"))
            for path in frames:
                meta = json.loads(path.read_text(encoding="utf-8"))
                if not started_at_ms <= meta["captured_at_ms"] < ended_at_ms:
                    raise ClipError("선택 프레임이 클립 촬영 구간 밖에 있습니다.", 409)
            extension = "webm" if container_type == "video/webm" else "mp4"
            target = clip / f"original.{extension}"
            temporary = clip / f".original.{uuid4().hex}.tmp"
            try:
                with temporary.open("wb") as destination:
                    for path in chunks:
                        with path.open("rb") as source:
                            while block := source.read(1024 * 1024):
                                destination.write(block)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            try:
                with target.open("rb") as camera_file:
                    header = camera_file.read(16)
                if container_type == "video/webm" and not header.startswith(b"\x1a\x45\xdf\xa3"):
                    raise ClipError("원본 WebM 파일 형식이 올바르지 않습니다.")
                if container_type == "video/mp4" and header[4:8] != b"ftyp":
                    raise ClipError("원본 MP4 파일 형식이 올바르지 않습니다.")
                actual_duration_ms = self._video_duration_ms(target)
                # Container timestamps can extend one final frame past the recorder stop time.
                if actual_duration_ms > MAX_CLIP_MS + 500:
                    raise ClipError("원본 영상이 30초 제한을 넘었습니다.")
            except ClipError:
                target.unlink(missing_ok=True)
                raise
            manifest = {
                "storage_version": 2,
                "clip_id": clip_id, "started_at_ms": started_at_ms, "ended_at_ms": ended_at_ms,
                "duration_ms": ended_at_ms - started_at_ms, "mime_type": mime_type,
                "original_duration_ms": actual_duration_ms,
                "chunk_count": chunk_count, "size_bytes": size_bytes,
                "frame_count": len(frames), "original_path": f"clips/clip_{clip_id:03d}/original.{extension}",
                "inference_path": f"clips/clip_{clip_id:03d}/inference.mp4",
                "state": "pending", "error": None,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_json(clip / "manifest.json", manifest)
            for path in chunks:
                path.unlink()
            log.info("clip accepted session=%s clip=%03d bytes=%d frames=%d", session_id, clip_id, size_bytes, len(frames))
            return manifest, True

    def status(self, session_id):
        with self.lock:
            folder = self._session(session_id)
            clips = []
            for clip_id in range(1, MAX_CLIPS + 1):
                clip = self._clip(folder, clip_id)
                manifest = self._manifest(clip)
                if manifest:
                    clips.append(manifest)
                elif clip.is_dir():
                    clips.append({"clip_id": clip_id, "state": "uploading"})
            return {"session_id": session_id, "clips": clips, "max_clips": MAX_CLIPS,
                    "max_clip_ms": MAX_CLIP_MS, "max_total_bytes": MAX_TOTAL_BYTES}

    def pending_exports(self):
        """Find interrupted post-test jobs after a server restart."""
        for session_file in self.sessions.output_dir.glob("[0-9]" * 8 + "/*/session.json"):
            try:
                session = json.loads(session_file.read_text(encoding="utf-8"))
                session_id = session.get("session_id")
                if not session.get("ended_at") or not session_id:
                    continue
                for clip_id in range(1, MAX_CLIPS + 1):
                    manifest = self._manifest(self._clip(session_file.parent, clip_id))
                    if manifest and manifest.get("state") in ("pending", "rendering"):
                        yield session_id, clip_id
                    elif manifest:
                        self._cleanup_inputs(self._clip(session_file.parent, clip_id), manifest)
            except (OSError, ValueError, ClipError):
                continue

    @staticmethod
    def _point(point, width, height):
        return (int(float(point[0]) * width), int(float(point[1]) * height))

    def _draw(self, frame, row, mask_path, overlay_path=None):
        height, width = frame.shape[:2]
        if overlay_path is not None and overlay_path.is_file():
            overlay = cv2.imdecode(np.frombuffer(overlay_path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
            if overlay is None or overlay.ndim != 3 or overlay.shape[2] != 4:
                raise ValueError("저장된 화면 오버레이를 읽을 수 없습니다.")
            alpha = overlay[:, :, 3:4].astype(np.float32) / 255
            color = overlay[:, :, :3].astype(np.float32) * alpha
            if overlay.shape[:2] != (height, width):
                color = cv2.resize(color, (width, height), interpolation=cv2.INTER_AREA)
                alpha = cv2.resize(alpha, (width, height), interpolation=cv2.INTER_AREA)[:, :, None]
            return np.uint8(frame.astype(np.float32) * (1 - alpha) + color)
        if mask_path.is_file():
            mask = cv2.imdecode(np.frombuffer(mask_path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
            if mask is not None and mask.ndim == 3 and mask.shape[2] == 4:
                mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
                alpha = mask[:, :, 3:4].astype(np.float32) / 255
                frame = np.uint8(frame.astype(np.float32) * (1 - alpha) + mask[:, :, :3].astype(np.float32) * alpha)
        roi = ((row.get("walking") or {}).get("event") or {}).get("roi") or {}
        polygons = roi.get("corridor_polygons") or [roi.get("corridor_polygon")]
        polygons = [(polygon, (50, 210, 220)) for polygon in polygons]
        polygons.append((roi.get("immediate_polygon"), (0, 160, 255)))
        for polygon, color in polygons:
            if isinstance(polygon, list) and len(polygon) >= 3:
                try:
                    points = np.array([self._point(p, width, height) for p in polygon], dtype=np.int32)
                    cv2.polylines(frame, [points], True, color, 2)
                except (TypeError, ValueError, IndexError):
                    pass
        for section, color in (("walking", (0, 150, 255)), ("traffic", (0, 255, 255))):
            for detection in ((row.get(section) or {}).get("detections") or []):
                box = detection.get("xyxy")
                if not isinstance(box, list) or len(box) != 4:
                    continue
                try:
                    x1, y1 = self._point(box[:2], width, height)
                    x2, y2 = self._point(box[2:], width, height)
                except (TypeError, ValueError, IndexError):
                    continue
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                label = str(detection.get("display_label") or detection.get("class_name") or section)
                label = label.encode("ascii", "ignore").decode()[:30] or section
                cv2.putText(frame, label, (x1, max(15, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1)
        event = (row.get("walking") or {}).get("event") or {}
        level = str(event.get("level") or "").upper()[:18]
        signal = str(((row.get("traffic") or {}).get("event") or {}).get("signal_state") or "").upper()[:12]
        cv2.putText(frame, f"F{row.get('frame_id')} {level}  SIGNAL:{signal}", (12, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, .65, (255, 255, 255), 3)
        cv2.putText(frame, f"F{row.get('frame_id')} {level}  SIGNAL:{signal}", (12, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, .65, (0, 0, 0), 1)
        return frame

    def export(self, session_id, clip_id):
        """Render the selected inference frames after the live session is closed."""
        folder = self._session(session_id)
        clip = self._clip(folder, clip_id)
        try:
            with self.lock:
                manifest = self._manifest(clip)
                if not manifest:
                    return
                if manifest["state"] in ("ready", "no_frames"):
                    self._cleanup_inputs(clip, manifest)
                    return
                manifest["state"] = "rendering"
                _write_json(clip / "manifest.json", manifest)
            metadata = sorted((clip / "frames").glob("*.json"), key=lambda path: int(path.stem))
            if not metadata:
                with self.lock:
                    manifest = self._manifest(clip)
                    manifest["state"] = "no_frames"
                    manifest["error"] = "선택 구간에서 처리된 추론 프레임이 없습니다."
                    _write_json(clip / "manifest.json", manifest)
                    self._cleanup_inputs(clip, manifest)
                return
            rows = {}
            with (folder / "results.jsonl").open(encoding="utf-8") as source:
                wanted = {int(path.stem) for path in metadata}
                for line in source:
                    row = json.loads(line)
                    if row.get("frame_id") in wanted:
                        rows[row["frame_id"]] = row
            first = cv2.imread(str((clip / "frames" / metadata[0].name).with_suffix(".jpg")))
            if first is None:
                raise ValueError("첫 선택 프레임을 읽을 수 없습니다.")
            height, width = first.shape[:2]
            width += width % 2
            height += height % 2
            entries = [json.loads(path.read_text(encoding="utf-8")) for path in metadata]
            first_at = entries[0]["captured_at_ms"]
            timeline = []
            with tempfile.TemporaryDirectory(prefix=".inference-", dir=clip) as work:
                work = Path(work)
                concat = ["ffconcat version 1.0"]
                for index, meta in enumerate(entries):
                    frame_id = meta["frame_id"]
                    row = rows.get(frame_id)
                    if row is None or row.get("frame_sha256") != meta["sha256"]:
                        raise ValueError(f"추론 결과와 선택 프레임 {frame_id}이 일치하지 않습니다.")
                    frame = cv2.imread(str(clip / "frames" / f"{frame_id:06d}.jpg"))
                    if frame is None:
                        raise ValueError(f"선택 프레임 {frame_id}을 읽을 수 없습니다.")
                    if frame.shape[:2] != (height, width):
                        frame = cv2.resize(frame, (width, height))
                    rendered = self._draw(frame, row, clip / "masks" / f"{frame_id:06d}.png",
                                          clip / "overlays" / f"{frame_id:06d}.png")
                    name = f"{index:08d}.png"
                    if not cv2.imwrite(str(work / name), rendered):
                        raise RuntimeError("추론 프레임을 임시 저장하지 못했습니다.")
                    if index + 1 < len(entries):
                        duration_ms = max(1, entries[index + 1]["captured_at_ms"] - meta["captured_at_ms"])
                    else:
                        duration_ms = max(1, min(manifest["ended_at_ms"] - meta["captured_at_ms"],
                                                 round(1000 / self.recording_fps)))
                    concat.extend([f"file '{name}'", "option framerate 1000", f"duration {duration_ms / 1000:.6f}"])
                    timeline.append({"frame_id": frame_id, "captured_at_ms": meta["captured_at_ms"],
                                     "sha256": meta["sha256"],
                                     "clip_offset_ms": meta["captured_at_ms"] - manifest["started_at_ms"],
                                     "video_pts_ms": meta["captured_at_ms"] - first_at,
                                     "duration_ms": duration_ms, "mask": meta.get("mask", False),
                                     "overlay": meta.get("overlay", False)})
                concat.extend([f"file '{len(entries)-1:08d}.png'", "option framerate 1000"])
                (work / "input.ffconcat").write_text("\n".join(concat) + "\n", encoding="utf-8")
                temporary = work / "inference.mp4"
                original = folder / manifest["original_path"]
                audio_offset = max(0, first_at - manifest["started_at_ms"]) / 1000
                command = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                           "-f", "concat", "-safe", "0", "-i", str(work / "input.ffconcat"),
                           "-ss", f"{audio_offset:.3f}", "-i", str(original),
                           "-map", "0:v:0", "-map", "1:a:0?", "-fps_mode", "vfr",
                           "-c:v", "libx264", "-c:a", "aac", "-threads", "2",
                           "-preset", "veryfast", "-pix_fmt", "yuv420p",
                           "-video_track_timescale", "1000000", "-movflags", "+faststart", str(temporary)]
                encoded = subprocess.run(command, capture_output=True, text=True, timeout=300)
                if encoded.returncode:
                    raise RuntimeError(encoded.stderr[-500:] or "ffmpeg 변환 실패")
                temporary.replace(clip / "inference.mp4")
            with self.lock:
                manifest = self._manifest(clip)
                manifest["state"] = "ready"
                manifest["error"] = None
                manifest["storage_version"] = 2
                manifest["first_frame_offset_ms"] = first_at - manifest["started_at_ms"]
                manifest["frames"] = timeline
                _write_json(clip / "manifest.json", manifest)
                self._cleanup_inputs(clip, manifest)
            log.info("clip rendered session=%s clip=%03d frames=%d", session_id, clip_id, len(entries))
        except Exception as error:
            log.exception("clip export failed session=%s clip=%03d", session_id, clip_id)
            with self.lock:
                manifest = self._manifest(clip)
                if manifest:
                    manifest["state"] = "failed"
                    manifest["error"] = str(error)[-500:]
                    _write_json(clip / "manifest.json", manifest)
