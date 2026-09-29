"""
file_path: src/video_audio.py

보행 위험 MP3를 영상 시간에 맞춰 음성 트랙으로 만들고 결과 MP4에 합친다.
새 위험 안내가 시작되면 이전 안내를 중단한다.
"""

import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg


AUDIO_DIR = Path(__file__).resolve().parents[1] / "assets" / "audio" / "ko-v1"
SAMPLE_RATE = 16000


# 동봉된 FFmpeg 실행 파일 조회
def ffmpeg_executable():
    """시스템 설치 여부와 관계없이 의존성 패키지의 FFmpeg를 사용한다."""
    return imageio_ffmpeg.get_ffmpeg_exe()


# 음성 MP3를 단일 채널 PCM으로 해독
def decode_clip(filename):
    """동봉된 한국어 MP3를 16 kHz PCM 바이트로 바꾼다."""
    path = AUDIO_DIR / filename
    if path.name != filename or not path.is_file():
        raise FileNotFoundError(f"위험 안내 음원이 없습니다: {path}")
    result = subprocess.run(
        [ffmpeg_executable(), "-nostdin", "-v", "error", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        check=True, capture_output=True,
    )
    return result.stdout


# 비어 있는 음성 구간 기록
def write_silence(output, frame_count):
    """긴 영상에서도 음성 전체를 메모리에 올리지 않고 무음 구간을 쓴다."""
    while frame_count > 0:
        count = min(frame_count, SAMPLE_RATE)
        output.writeframesraw(b"\0\0" * count)
        frame_count -= count


# 영상 길이만큼 음성 트랙 만들기
def render_voice_track(events, duration_s, wav_path):
    """새 위험이 시작되면 앞 음성을 끊고 다음 음성을 이어 붙인다."""
    total_frames = round(duration_s * SAMPLE_RATE)
    clips = {name: decode_clip(name) for _, name in events}
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        cursor = 0
        for index, (time_s, name) in enumerate(events):
            start = min(total_frames, max(cursor, round(time_s * SAMPLE_RATE)))
            if start >= total_frames:
                break
            write_silence(output, start - cursor)
            next_start = (round(events[index + 1][0] * SAMPLE_RATE)
                          if index + 1 < len(events) else total_frames)
            available = max(0, min(total_frames, next_start) - start)
            clip = clips[name][:available * 2]
            output.writeframesraw(clip)
            cursor = start + len(clip) // 2
        write_silence(output, total_frames - cursor)


# 영상과 음성 결합
def mux_voice(video_path, wav_path, output_path):
    """영상 프레임을 복사하고 안내 음성을 AAC로 인코딩해 MP4에 저장한다."""
    subprocess.run(
        [ffmpeg_executable(), "-nostdin", "-y", "-v", "error", "-i", str(video_path),
         "-i", str(wav_path), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(output_path)],
        check=True, capture_output=True,
    )
