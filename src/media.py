"""앱의 녹화 영상과 음성 재생에 사용할 FFmpeg 실행 경로를 제공한다."""

import imageio_ffmpeg


def ffmpeg_executable():
    """시스템 설치 여부와 관계없이 의존성 패키지의 FFmpeg를 사용한다."""
    return imageio_ffmpeg.get_ffmpeg_exe()
