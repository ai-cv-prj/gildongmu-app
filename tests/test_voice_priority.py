"""
file_path: tests/test_voice_priority.py

영상 음성 시간축에서 횡단보도 이탈 반복과 안내 우선순위를 검증한다.
"""

from unittest.mock import patch

from src.voice_priority import CrosswalkVoice, prioritize_voice_events


# 횡단보도 이탈 구간 반복 확인
def test_crosswalk_voice_repeats_until_return():
    """이탈 구간에는 음원을 연달아 배치하고 복귀 후 새 반복만 중단한다."""
    voice = CrosswalkVoice()
    outside = {"repeat": True, "voice_clip": "crosswalk-exit-right.mp3"}
    voice.observe(outside, 1.0, 0.1)
    voice.observe(outside, 1.5, 0.1)
    voice.observe({"repeat": False}, 2.0, 0.1)
    with patch("src.voice_priority.clip_duration", return_value=0.4):
        events = voice.events(3.0)
    assert [round(item[0], 1) for item in events] == [1.0, 1.4, 1.8]
    assert all(item[1] == "crosswalk-exit-right.mp3" for item in events)


# 횡단 완료 일회 안내 확인
def test_crosswalk_voice_records_finished_once():
    """정상 횡단 완료 음원은 반복 구간이 아닌 단일 이벤트로 기록한다."""
    voice = CrosswalkVoice()
    voice.observe({"repeat": False, "voice_clip": "crosswalk-finished.mp3"}, 2.0, 0.1)
    assert voice.events(3.0) == [
        (2.0, "crosswalk-finished.mp3", 1, "crosswalk"),
    ]


# 짧은 복귀 뒤 같은 방향 이탈의 연속 재생 확인
def test_same_direction_reexit_continues_current_crosswalk_clip():
    """현재 문장이 끝나기 전 같은 방향으로 재이탈하면 원래 반복 박자를 유지한다."""
    voice = CrosswalkVoice()
    outside = {"repeat": True, "voice_clip": "crosswalk-exit-right.mp3"}
    voice.observe(outside, 1.0, 0.1)
    voice.observe(outside, 1.5, 0.1)
    voice.observe({"repeat": False}, 2.0, 0.1)
    voice.observe(outside, 2.1, 0.1)
    voice.observe(outside, 2.5, 0.1)
    voice.observe({"repeat": False}, 2.6, 0.1)
    with patch("src.voice_priority.clip_duration", return_value=0.4):
        events = voice.events(3.0)
    assert [round(item[0], 1) for item in events] == [1.0, 1.4, 1.8, 2.2]




# 반대 방향 재이탈의 즉시 교체 확인
def test_opposite_crosswalk_exit_interrupts_current_direction():
    """반대 방향 이탈 음성은 이전 방향 문장이 재생 중이어도 즉시 교체한다."""
    crosswalk = [
        (1.0, "crosswalk-exit-right.mp3", 1, "crosswalk"),
        (1.2, "crosswalk-exit-left.mp3", 1, "crosswalk"),
    ]
    with patch("src.voice_priority.clip_duration", return_value=0.8):
        events = prioritize_voice_events([], [], crosswalk)
    assert events == [(1.0, "crosswalk-exit-right.mp3"),
                      (1.2, "crosswalk-exit-left.mp3")]


# 전역 음성 우선순위 확인
def test_priority_drops_lower_audio_during_crosswalk_exit():
    """이탈 음성 중 장애물과 신호 안내를 폐기하고 복귀 후 최신 신호는 허용한다."""
    crosswalk = [(1.0, "crosswalk-exit-right.mp3", 1, "crosswalk"),
                 (2.0, None, 1, "crosswalk_stop")]
    with patch("src.voice_priority.clip_duration", return_value=0.8):
        events = prioritize_voice_events(
            [(1.2, "walking-move-left-one.mp3")],
            [(1.3, "red-changed.mp3"), (2.1, "green.mp3")],
            crosswalk,
        )
    assert events == [(1.0, "crosswalk-exit-right.mp3"), (2.0, None), (2.1, "green.mp3")]


# 장애물과 빨간불 우선순위 확인
def test_red_traffic_preempts_walking_without_queue():
    """장애물 음성 도중 빨간불은 즉시 시작하고 하위 안내를 다시 쌓지 않는다."""
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        events = prioritize_voice_events(
            [(0.0, "walking-move-right-two.mp3"), (0.3, "walking-move-left-one.mp3")],
            [(0.2, "red.mp3")],
            [],
        )
    assert events == [(0.0, "walking-move-right-two.mp3"), (0.2, "red.mp3")]


# 긴급 장애물 정지 음성의 최상위 우선순위 확인
def test_emergency_walking_stop_preempts_crosswalk_and_red_traffic():
    """장애물 멈춤 안내는 횡단보도와 빨간불 음성을 즉시 중단한다."""
    crosswalk = [(0.0, "crosswalk-exit-right.mp3", 1, "crosswalk")]
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        crosswalk_events = prioritize_voice_events(
            [(0.2, "walking-stop.mp3")], [], crosswalk)
        red_events = prioritize_voice_events(
            [(0.2, "walking-stop.mp3")], [(0.0, "red.mp3")], [])
    assert crosswalk_events == [
        (0.0, "crosswalk-exit-right.mp3"),
        (0.2, "walking-stop.mp3"),
    ]
    assert red_events == [(0.0, "red.mp3"), (0.2, "walking-stop.mp3")]


# 긴급 장애물 정지 음성의 하위 안내 차단 확인
def test_regular_walking_guidance_does_not_interrupt_emergency_stop():
    """멈춤 음성이 재생 중이면 일반 장애물 방향 안내를 폐기한다."""
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        events = prioritize_voice_events(
            [(0.0, "walking-stop.mp3"), (0.2, "walking-move-right-one.mp3")],
            [],
            [],
        )
    assert events == [(0.0, "walking-stop.mp3")]


# 보행로 이탈 우선순위 확인
def test_walking_surface_sits_between_red_and_obstacle_guidance():
    """보행로 이탈은 장애물 안내를 선점하지만 재생 중인 빨간불은 선점하지 않는다."""
    surface = [(0.1, "walkway-exit-right.mp3", 3, "walking_surface")]
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        events = prioritize_voice_events(
            [(0.0, "walking-move-right-two.mp3")], [(0.2, "red.mp3")], [], surface)
    assert events == [
        (0.0, "walking-move-right-two.mp3"),
        (0.1, "walkway-exit-right.mp3"),
        (0.2, "red.mp3"),
    ]


# 보행로 이탈 방향 변경 즉시 반영 확인
def test_changed_walking_surface_direction_interrupts_previous_clip():
    """보행로 이탈 방향이 바뀌면 같은 우선순위여도 최신 방향으로 교체한다."""
    surface = [
        (0.0, "walkway-exit-right.mp3", 3, "walking_surface"),
        (0.2, "walkway-exit-left.mp3", 3, "walking_surface"),
    ]
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        events = prioritize_voice_events([], [], [], surface)
    assert events == [
        (0.0, "walkway-exit-right.mp3"),
        (0.2, "walkway-exit-left.mp3"),
    ]


# 같은 출처·같은 우선순위 교체 경계 확인
def test_same_source_replacement_matches_realtime_priority_boundary():
    """0~4순위의 다른 음원만 교체하고 같은 음원과 5~6순위는 유지한다."""
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        urgent_red = prioritize_voice_events(
            [], [(0.0, "red.mp3"), (0.2, "red-changed.mp3")], [])
        same_walking = prioritize_voice_events(
            [(0.0, "walking-move-right-two.mp3"), (0.2, "walking-move-right-two.mp3")],
            [],
            [],
        )
        regular_signal = prioritize_voice_events(
            [], [(0.0, "green.mp3"), (0.2, "missing.mp3")], [])
        priority_five = prioritize_voice_events(
            [], [], [(0.0, "change-a.mp3", 5, "traffic"),
                     (0.2, "change-b.mp3", 5, "traffic")])
    assert urgent_red == [(0.0, "red.mp3"), (0.2, "red-changed.mp3")]
    assert same_walking == [(0.0, "walking-move-right-two.mp3")]
    assert regular_signal == [(0.0, "green.mp3")]
    assert priority_five == [(0.0, "change-a.mp3")]


# 장애물 행동 전환 즉시 반영 확인
def test_changed_walking_action_interrupts_previous_walking_clip():
    """새 장애물 행동은 재생 중인 이전 행동과 겹쳐도 폐기하지 않는다."""
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        events = prioritize_voice_events(
            [(0.0, "walking-move-right-two.mp3"),
             (0.3, "walking-move-left-one.mp3"),
             (0.6, "walking-move-right-one.mp3")],
            [],
            [],
        )
    assert events == [
        (0.0, "walking-move-right-two.mp3"),
        (0.3, "walking-move-left-one.mp3"),
        (0.6, "walking-move-right-one.mp3"),
    ]


def test_crossing_suppression_stops_active_walking_clip():
    """횡단 진입의 보행 중단 이벤트는 재생 중인 장애물 음성만 자른다."""
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        events = prioritize_voice_events(
            [(0.0, "walking-move-right-two.mp3"), (0.2, None)], [], [])
    assert events == [(0.0, "walking-move-right-two.mp3"), (0.2, None)]


# 신호 재안내 우선순위 확인
def test_repeated_red_does_not_preempt_walking():
    """같은 빨간불 재안내는 장애물 음성을 끊지 않고, 첫 빨간불만 장애물보다 우선한다."""
    with patch("src.voice_priority.clip_duration", return_value=1.0):
        events = prioritize_voice_events(
            [(0.0, "walking-move-right-two.mp3"), (2.0, "walking-move-right-two.mp3")],
            [(0.2, "red.mp3", "repeat"), (2.2, "red.mp3")],
            [],
        )
    assert events == [(0.0, "walking-move-right-two.mp3"), (2.0, "walking-move-right-two.mp3"),
                      (2.2, "red.mp3")]
