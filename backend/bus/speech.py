"""버스 도착 안내 문장을 한국어 MP3로 변환한다."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BusSpeechError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class BusSpeechService:
    tts_url = "https://translate.google.com/translate_tts"
    max_audio_bytes = 2 * 1024 * 1024

    def __init__(self, timeout: float = 10, opener: Callable[..., object] = urlopen):
        self.timeout = timeout
        self.opener = opener
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_lock = Lock()

    def synthesize(self, text: str) -> bytes:
        message = " ".join(text.split())
        if not message or len(message) > 200:
            raise BusSpeechError(400, "invalid_speech_text", "음성 안내 문장은 1자 이상 200자 이하여야 합니다")

        with self._cache_lock:
            cached = self._cache.get(message)
            if cached is not None:
                self._cache.move_to_end(message)
                return cached

        query = urlencode({
            "ie": "UTF-8",
            "client": "tw-ob",
            "tl": "ko",
            "q": message,
        })
        request = Request(
            f"{self.tts_url}?{query}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "audio/mpeg"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                audio = response.read(self.max_audio_bytes + 1)
        except HTTPError as error:
            raise BusSpeechError(
                502,
                "bus_speech_http_error",
                f"버스 음성 생성 서비스가 HTTP {error.code} 오류를 반환했습니다",
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise BusSpeechError(502, "bus_speech_unreachable", "버스 음성 생성 서비스에 연결할 수 없습니다") from error

        if len(audio) > self.max_audio_bytes or not self._is_mp3(audio):
            raise BusSpeechError(502, "bus_speech_invalid_response", "버스 음성 생성 결과가 올바른 MP3가 아닙니다")

        with self._cache_lock:
            self._cache[message] = audio
            self._cache.move_to_end(message)
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
        return audio

    @staticmethod
    def _is_mp3(audio: bytes) -> bool:
        if len(audio) < 3:
            return False
        return audio.startswith(b"ID3") or (audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0)
