"""Pitch-preserving MP3 rates for browsers that cannot rate-shift captured audio."""
from collections import OrderedDict
from hashlib import sha256
import math
from pathlib import Path
import subprocess
from threading import BoundedSemaphore, Lock

from src.video_audio import ffmpeg_executable


_GUIDANCE_CLIPS = frozenset({
    "red", "green-initial-wait", "green", "green-changed", "red-changed", "missing",
    "crosswalk-exit-right", "crosswalk-exit-left", "crosswalk-align-right", "crosswalk-align-left",
    "walking-move-left-one", "walking-move-left-two", "walking-straight",
    "walking-move-right-one", "walking-move-right-two", "walking-stop",
    "walkway-exit-right", "walkway-exit-left",
})
GUIDANCE_CLIPS = _GUIDANCE_CLIPS | {f"mock-{name}" for name in _GUIDANCE_CLIPS}


class AudioPlaybackError(RuntimeError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class AudioPlaybackService:
    """Bound conversion work and cache by content so replaced voices take effect immediately."""

    max_audio_bytes = 2 * 1024 * 1024
    max_cache_entries = 128
    max_cache_bytes = 16 * 1024 * 1024

    def __init__(self, directory: Path, *, timeout=5, queue_timeout=0.25):
        self.directory = Path(directory).resolve()
        self.timeout = timeout
        self.queue_timeout = queue_timeout
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = Lock()
        self._conversions = BoundedSemaphore(2)

    @staticmethod
    def validate_rate(rate):
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not 0.75 <= rate <= 2:
            raise AudioPlaybackError(422, "음성 속도는 0.75배 이상 2배 이하여야 합니다.")
        return float(rate)

    def guidance(self, clip, rate=1):
        rate = self.validate_rate(rate)
        if clip not in GUIDANCE_CLIPS:
            raise AudioPlaybackError(404, "안내 음원을 찾을 수 없습니다.")
        path = (self.directory / f"{clip}.mp3").resolve()
        if path.parent != self.directory:
            raise AudioPlaybackError(404, "안내 음원을 찾을 수 없습니다.")
        try:
            with path.open("rb") as source:
                original = source.read(self.max_audio_bytes + 1)
        except FileNotFoundError as error:
            raise AudioPlaybackError(404, "안내 음원을 찾을 수 없습니다.") from error
        except OSError as error:
            raise AudioPlaybackError(503, "안내 음원을 읽을 수 없습니다.") from error
        return self.adjust(original, rate)

    def _cached(self, key):
        with self._cache_lock:
            result = self._cache.get(key)
            if result is not None:
                self._cache.move_to_end(key)
            return result

    def adjust(self, original: bytes, rate=1):
        rate = self.validate_rate(rate)
        if not original or len(original) > self.max_audio_bytes:
            raise AudioPlaybackError(422, "안내 음원의 크기가 올바르지 않습니다.")
        if rate == 1:
            return original
        key = (sha256(original).digest(), rate)
        cached = self._cached(key)
        if cached is not None:
            return cached
        if not self._conversions.acquire(timeout=self.queue_timeout):
            raise AudioPlaybackError(503, "안내 음성을 준비하고 있습니다. 잠시 후 다시 시도해 주세요.")
        try:
            cached = self._cached(key)
            if cached is not None:
                return cached
            try:
                result = subprocess.run([
                    ffmpeg_executable(), "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "pipe",
                    "-f", "mp3", "-i", "pipe:0", "-vn", "-filter:a", f"atempo={rate}",
                    "-map_metadata", "-1", "-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame",
                    "-b:a", "96k", "-fs", str(self.max_audio_bytes + 1), "-f", "mp3", "pipe:1",
                ], input=original, check=True, capture_output=True, timeout=self.timeout)
            except (subprocess.SubprocessError, OSError) as error:
                raise AudioPlaybackError(503, "안내 음성의 속도를 변환하지 못했습니다.") from error
            converted = result.stdout
            if not converted or len(converted) > self.max_audio_bytes:
                raise AudioPlaybackError(503, "변환된 안내 음원의 크기가 올바르지 않습니다.")
            with self._cache_lock:
                previous = self._cache.pop(key, None)
                self._cache_bytes -= len(previous) if previous is not None else 0
                self._cache[key] = converted
                self._cache_bytes += len(converted)
                while len(self._cache) > self.max_cache_entries or self._cache_bytes > self.max_cache_bytes:
                    _, evicted = self._cache.popitem(last=False)
                    self._cache_bytes -= len(evicted)
            return converted
        finally:
            self._conversions.release()
