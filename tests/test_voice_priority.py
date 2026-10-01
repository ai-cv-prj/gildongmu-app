"""
file_path: tests/test_voice_priority.py

영상 음성 시간축에서 횡단보도 이탈 반복과 안내 우선순위를 검증한다.
"""

from unittest.mock import patch

from src.voice_priority import CrosswalkVoice, prioritize_voice_events


# 횡단보도 이탈 구간 반복 확인
def test_crosswalk_voice_repeats_until_return():
    """이탈 구간에는 음원을 연달아 배치하고 복귀 시각에 중단 표식을 둔다."""
    voice = CrosswalkVoice()
    outside = {"repeat": True, "voice_clip": "crosswalk-exit-right.mp3"}
    voice.observe(outside, 1.0, 0.1)
    voice.observe(outside, 1.5, 0.1)
    voice.observe({"repeat": False}, 2.0, 0.1)
    with patch("src.voice_priority.clip_duration", return_value=0.4):
        events = voice.events(3.0)
    assert [round(item[0], 1) for item in events] == [1.0, 1.4, 1.8, 2.0]
    assert events[-1][1] is None


# 전역 음성 우선순위 확인
def test_priority_drops_lower_audio_during_crosswalk_exit():
    """이탈 음성 중 장애물과 신호 안내를 폐기하고 복귀 후 최신 신호는 허용한다."""
    crosswalk = [(1.0, "crosswalk-exit-right.mp3", 1, "crosswalk"),
                 (2.0, None, 1, "crosswalk_stop")]
    with patch("src.voice_priority.clip_duration", return_value=0.8):
        events = prioritize_voice_events(
            [(1.2, "walking-move-left.mp3")],
            [(1.3, "red-changed.mp3"), (2.1, "green.mp3")],
            crosswalk,
        )
    assert events == [(1.0, "crosswalk-exit-right.mp3"), (2.0, None), (2.1, "green.mp3")]


# 장애물과 빨간불 우선순위 확인
def test_red_traffic_preempts_walking_without_queue():
    """장애물 음성 도중 빨간불은 즉시 시작하고 하위 안내를 다시 쌓지 않는다."""
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        events = prioritize_voice_events(
            [(0.0, "walking-straight.mp3"), (0.3, "walking-move-left.mp3")],
            [(0.2, "red.mp3")],
            [],
        )
    assert events == [(0.0, "walking-straight.mp3"), (0.2, "red.mp3")]
