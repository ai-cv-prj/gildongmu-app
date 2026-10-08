"""
file_path: tests/test_walking_surface.py

가상 발의 보행가능 비율과 시간 누적으로 보행로 좌우 이탈을 판정하는지 검증한다.
횡단보도 중지와 복귀 시 반복 종료도 함께 확인한다.
"""

import numpy as np

from src.walking_surface import WalkingSurfaceEngine, walking_surface_config
from src.walking_surface_visualization import FOOT_ROI_COLOR, draw_walking_surface


SHAPE = (100, 100, 3)
LABELS = {"non_walkable": 0, "walkable": 1, "crosswalk": 2}
CONFIG = {
    "exit_confirm_s": .3,
    "return_confirm_s": .5,
    "min_component_fraction": .001,
}


# 좌우 위치가 다른 보행로 마스크 생성
def walkway(left, right):
    """지정한 정규화 좌우 범위를 보행가능으로 채운 테스트 마스크를 만든다."""
    mask = np.zeros(SHAPE[:2], dtype=np.uint8)
    mask[:, round(left * SHAPE[1]):round(right * SHAPE[1])] = LABELS["walkable"]
    return mask


# 왼쪽 이탈과 복귀 반복 중단 확인
def test_left_exit_guides_right_and_return_stops_repeat_immediately():
    """보행로가 발 오른쪽에 남으면 오른쪽 이동을 반복하고 복귀 즉시 반복을 끈다."""
    engine = WalkingSurfaceEngine(CONFIG)
    inside = walkway(.35, .65)
    outside_left = walkway(.55, .80)
    assert engine.update(inside, LABELS, SHAPE, 0.0)["status"] == "inside"
    assert engine.update(outside_left, LABELS, SHAPE, .1)["status"] == "exit_confirming"
    event = engine.update(outside_left, LABELS, SHAPE, .4)
    assert event["status"] == "outside_left"
    assert event["voice_text"] == "보행로 이탈 오른쪽 이동!"
    assert event["repeat"] is True
    returning = engine.update(inside, LABELS, SHAPE, .5)
    assert returning["status"] == "returning"
    assert returning["repeat"] is False
    assert returning["voice_text"] is None
    assert engine.update(inside, LABELS, SHAPE, 1.0)["status"] == "inside"


# 오른쪽 이탈 방향 확인
def test_right_exit_guides_left():
    """보행로가 발 왼쪽에 남으면 오른쪽 이탈로 확정하고 왼쪽 이동을 안내한다."""
    engine = WalkingSurfaceEngine(CONFIG)
    engine.update(walkway(.35, .65), LABELS, SHAPE, 0.0)
    outside_right = walkway(.20, .45)
    engine.update(outside_right, LABELS, SHAPE, .1)
    event = engine.update(outside_right, LABELS, SHAPE, .4)
    assert event["status"] == "outside_right"
    assert event["voice_text"] == "보행로 이탈 왼쪽 이동!"


# 방향 근거 없는 이탈 확인
def test_exit_without_previous_walkway_is_uncertain_and_silent():
    """직전에 밟던 연결 영역이 없으면 별도 unknown 상태 없이 조용히 보류한다."""
    event = WalkingSurfaceEngine(CONFIG).update(
        np.zeros(SHAPE[:2], dtype=np.uint8), LABELS, SHAPE, 0.0)
    assert event["status"] == "uncertain"
    assert event["voice_text"] is None
    assert event["repeat"] is False


# 횡단보도 상태 중 판정 정지 확인
def test_crosswalk_activity_pauses_surface_judgment():
    """횡단보도 접근부터 이탈까지 보행로 이탈 판정과 음성을 중지한다."""
    engine = WalkingSurfaceEngine(CONFIG)
    engine.update(walkway(.35, .65), LABELS, SHAPE, 0.0)
    for index, status in enumerate(("approach", "align_left", "crossing", "edge",
                                    "outside_right"), start=1):
        event = engine.update(np.zeros(SHAPE[:2], dtype=np.uint8), LABELS, SHAPE,
                              index * .5, crosswalk_status=status)
        assert event["status"] == "paused"
        assert event["voice_text"] is None
    resumed = engine.update(np.zeros(SHAPE[:2], dtype=np.uint8), LABELS, SHAPE, 3.0)
    assert resumed["status"] == "uncertain"


def test_bus_stop_suspends_surface_and_clears_previous_exit():
    engine = WalkingSurfaceEngine(CONFIG)
    inside = walkway(.35, .65)
    outside = walkway(.55, .80)
    engine.update(inside, LABELS, SHAPE, 0.0)
    engine.update(outside, LABELS, SHAPE, .1)
    assert engine.update(outside, LABELS, SHAPE, .4)["status"] == "outside_left"
    stopped = engine.update(outside, LABELS, SHAPE, .5, suspended=True)
    assert stopped["enabled"] is False
    assert stopped["status"] == "disabled"
    assert stopped["voice_text"] is None
    assert stopped["repeat"] is False
    assert stopped["roi"] is None
    assert engine.update(outside, LABELS, SHAPE, .6)["status"] == "uncertain"
    assert engine.update(inside, LABELS, SHAPE, .7)["status"] == "inside"


# 설정 오류 확인
def test_surface_threshold_order_is_validated():
    """이탈 임계값이 내부 임계값보다 낮지 않으면 설정 오류를 낸다."""
    try:
        walking_surface_config({"exit_fraction": .7, "inside_fraction": .6})
    except ValueError as error:
        assert "below inside" in str(error)
    else:
        raise AssertionError("invalid thresholds were accepted")


# 녹화 영상의 녹색 ROI 표시 확인
def test_recorded_overlay_draws_green_foot_roi_and_status():
    """결과 영상에 라벨 없는 녹색 발 ROI와 보행로 상태 배지를 함께 그린다."""
    frame = np.zeros(SHAPE, dtype=np.uint8)
    event = {"status": "inside", "roi": {
        "left": .46, "right": .54, "top": .88, "bottom": .96,
    }}
    result = draw_walking_surface(frame, event)
    assert tuple(result[88, 46]) == FOOT_ROI_COLOR
    assert np.any(result[42:80, 50:] != 0)
