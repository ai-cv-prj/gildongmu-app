"""
file_path: tests/test_crosswalk_safety.py

횡단보도 상태 판정의 진입·가장자리·이탈·복귀·정상 도착을 검증한다.
"""

import cv2
import numpy as np

from src.crosswalk_safety import (
    CrosswalkSafetyEngine, crosswalk_camera_stable, crosswalk_safety_config,
)
from src.video_audio import decode_clip


SHAPE = (100, 100, 3)
LABELS = {"non_walkable": 0, "walkable": 1, "crosswalk": 2}
SIGNAL = {"crosswalks": [{"xyxy": [15, 30, 85, 95], "bottom_ratio": .95,
                           "crosswalk_status": "used"}]}


# 지정한 좌우 경계의 횡단보도 마스크 생성
def mask(left=.20, right=.80, top=.30, bottom=1.0, fill=0):
    """정규화된 사각형을 횡단보도 클래스로 채운 합성 마스크를 반환한다."""
    result = np.full(SHAPE[:2], fill, np.uint8)
    points = np.rint(np.array([
        [left * 99, top * 99], [right * 99, top * 99],
        [right * 99, bottom * 99], [left * 99, bottom * 99],
    ])).astype(np.int32)
    cv2.fillPoly(result, [points], LABELS["crosswalk"])
    return result


# 빠른 확인 시간을 사용하는 테스트 판정기 생성
def engine():
    """합성 프레임에서 상태 전환을 짧게 확인할 설정을 반환한다."""
    return CrosswalkSafetyEngine({"entry_confirm_s": .2, "edge_confirm_s": .1,
                                  "exit_confirm_s": .2, "return_confirm_s": .2,
                                  "finish_confirm_s": .2, "boundary_smooth_s": .01,
                                  "max_boundary_shift": .5})


# 순간적인 카메라 움직임 실패 완화 확인
def test_crosswalk_camera_stability_uses_debounced_view_status():
    """원시 움직임 한 프레임이 실패해도 촬영 상태가 clear이면 판정을 계속한다."""
    assert crosswalk_camera_stable({
        "camera_motion_stable": False,
        "camera_view": {"status": "clear"},
    })
    assert not crosswalk_camera_stable({"camera_view": {"status": "uncertain"}})
    assert not crosswalk_camera_stable({"camera_view": {"status": "unavailable"}})


# ROI 밖 횡단보도 무음 확인
def test_crosswalk_outside_roi_never_starts_crossing():
    """핑크 마스크가 보여도 하단 ROI에 5% 미만이면 횡단을 시작하지 않는다."""
    item = engine()
    result = item.update(mask(top=.15, bottom=.55), LABELS, SHAPE, SIGNAL, 0)
    result = item.update(mask(top=.15, bottom=.55), LABELS, SHAPE, SIGNAL, .3)
    assert result["status"] == "search"
    assert not result["crossing_active"]
    assert result["voice_text"] is None


# 신호등 검출 없이 ROI 마스크만 사용하는 진입 확인
def test_crossing_requires_only_sustained_roi_mask_and_inside_foot():
    """YOLO 횡단보도 검출 없이도 ROI 마스크와 발 경계 조건만으로 횡단한다."""
    item = engine()
    no_signal = {"crosswalks": []}
    first = item.update(mask(), LABELS, SHAPE, no_signal, 0)
    result = item.update(mask(), LABELS, SHAPE, no_signal, .2)
    assert first["status"] == "approach"
    assert result["status"] == "crossing"
    assert result["crossing_active"]


# 보라색 ROI 밖의 안정적인 횡단보도 경계 진입 확인
def test_stable_near_geometry_can_confirm_entry_before_bottom_roi_fills():
    """보라색 ROI까지 닿지 않아도 가까운 횡단보도 경계가 안정적이면 진입을 확정한다."""
    item = engine()
    near = mask(bottom=.85)
    first = item.update(near, LABELS, SHAPE, SIGNAL, 0)
    result = item.update(near, LABELS, SHAPE, SIGNAL, .3)
    assert first["status"] == "search"
    assert first["crosswalk_roi"]["crosswalk_fraction"] == 0
    assert result["status"] == "crossing"
    assert result["reasons"] == ["geometry_entry_confirmed"]


# 진입 확인 중 짧은 가림 허용 확인
def test_short_occlusion_does_not_restart_bottom_roi_entry_timer():
    """보라색 ROI 확인 중 짧은 객체 가림이 생겨도 진입 확인 시간을 유지한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    hidden = np.full(SHAPE[:2], LABELS["non_walkable"], np.uint8)
    vehicle = [{"class_name": "car", "xyxy": [10, 80, 90, 100], "observed": True}]
    held = item.update(hidden, LABELS, SHAPE, SIGNAL, .1, detections=vehicle)
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    assert held["status"] == "search"
    assert held["crosswalk_roi"]["occlusion_fraction"] >= .08
    assert result["status"] == "crossing"
    assert result["reasons"] == ["entry_confirmed"]


# 횡단 진입 전 오른쪽 정렬 안내 확인
def test_pre_entry_left_side_guides_right_until_foot_is_inside():
    """가상 발 왼쪽에 횡단보도가 있으면 오른쪽 정렬을 안내하고 진입 시 중단한다."""
    item = engine()
    outside = mask(left=.58, right=.95)
    result = item.update(outside, LABELS, SHAPE, {"crosswalks": []}, 0)
    assert result["status"] == "align_right"
    assert result["voice_text"] == "횡단보도 앞, 오른쪽 이동"
    assert result["voice_clip"] == "crosswalk-align-right.mp3"
    assert not result["crossing_active"]
    item.update(outside, LABELS, SHAPE, {"crosswalks": []}, .2)
    result = item.update(mask(), LABELS, SHAPE, {"crosswalks": []}, .21)
    assert result["status"] == "crossing"
    assert result["voice_text"] is None


# 횡단 진입 전 왼쪽 정렬 안내 확인
def test_pre_entry_right_side_guides_left():
    """가상 발 오른쪽에 횡단보도가 있으면 왼쪽 정렬을 안내한다."""
    item = engine()
    result = item.update(mask(left=.05, right=.42), LABELS, SHAPE,
                         {"crosswalks": []}, 0)
    assert result["status"] == "align_left"
    assert result["direction"] == "left"
    assert result["voice_clip"] == "crosswalk-align-left.mp3"


# 진입 뒤 가장자리 무음 확인
def test_crossing_entry_and_edge_is_silent():
    """가까운 횡단보도 안에 진입한 뒤 가장자리에서는 음성과 진동을 요청하지 않는다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    assert result["status"] == "crossing"
    item.update(mask(left=.43, right=.90), LABELS, SHAPE, SIGNAL, .3)
    result = item.update(mask(left=.43, right=.90), LABELS, SHAPE, SIGNAL, .41)
    assert result["status"] == "edge"
    assert "vibration" not in result
    assert result["voice_text"] is None


# 횡단 중 안정된 새 경계 승격 확인
def test_stable_boundary_jump_is_held_then_promoted():
    """크게 이동한 경계가 안정적으로 유지되면 잠시 보류한 뒤 정상 경계로 승격한다."""
    item = CrosswalkSafetyEngine({"entry_confirm_s": .2, "geometry_entry_confirm_s": .2,
                                  "max_boundary_shift": .1, "boundary_hold_s": .5,
                                  "boundary_candidate_confirm_s": .3,
                                  "boundary_smooth_s": .01})
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    held = item.update(mask(left=.58, right=.95), LABELS, SHAPE, SIGNAL, .3)
    promoted = item.update(mask(left=.58, right=.95), LABELS, SHAPE, SIGNAL, .6)
    assert held["status"] == "crossing"
    assert held["geometry"]["held"] is True
    assert held["voice_text"] is None
    assert promoted["geometry"]["promoted"] is True
    assert promoted["status"] != "uncertain"


# 계속 흔들리는 경계의 판단 보류 확인
def test_unstable_boundary_candidates_become_uncertain_after_hold():
    """서로 다른 새 경계가 반복되면 정상 경계로 승격하지 않고 판단을 보류한다."""
    item = CrosswalkSafetyEngine({"entry_confirm_s": .2, "geometry_entry_confirm_s": .2,
                                  "max_boundary_shift": .1, "boundary_hold_s": .5,
                                  "boundary_candidate_confirm_s": .3,
                                  "boundary_smooth_s": .01})
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    item.update(mask(left=.58, right=.95), LABELS, SHAPE, SIGNAL, .3)
    item.update(mask(left=.05, right=.42), LABELS, SHAPE, SIGNAL, .4)
    item.update(mask(left=.58, right=.95), LABELS, SHAPE, SIGNAL, .5)
    result = item.update(mask(left=.05, right=.42), LABELS, SHAPE, SIGNAL, .8)
    assert result["status"] == "uncertain"
    assert result["reasons"] == ["boundary_jump"]


# 왼쪽 이탈과 오른쪽 복귀 안내 확인
def test_left_exit_repeats_right_guidance_until_return_confirmed():
    """왼쪽 경계를 벗어나면 오른쪽 이동을 안내하고 복귀 확인 전까지 유지한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    item.update(mask(left=.58, right=.95), LABELS, SHAPE, SIGNAL, .3)
    result = item.update(mask(left=.58, right=.95), LABELS, SHAPE, SIGNAL, .51)
    assert result["status"] == "outside_left"
    assert result["direction"] == "right"
    assert result["repeat"]
    assert "vibration" not in result
    assert result["voice_text"] == "횡단보도 이탈, 오른쪽 이동"
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .6)
    assert result["status"] == "outside_left"
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .81)
    assert result["status"] == "crossing"
    assert result["voice_text"] is None


# 오른쪽 이탈 방향 확인
def test_right_exit_guides_left():
    """오른쪽 경계를 벗어나면 왼쪽 이동 음성을 선택한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    item.update(mask(left=.05, right=.42), LABELS, SHAPE, SIGNAL, .3)
    result = item.update(mask(left=.05, right=.42), LABELS, SHAPE, SIGNAL, .51)
    assert result["status"] == "outside_right"
    assert result["voice_clip"] == "crosswalk-exit-left.mp3"


# 반대편 보행가능영역 정상 도착 확인
def test_walkable_destination_finishes_without_exit_warning():
    """횡단보도 끝에서 보행가능영역이 이어지면 정상 도착으로 종료한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    walkable = np.full(SHAPE[:2], LABELS["walkable"], np.uint8)
    item.update(walkable, LABELS, SHAPE, {"crosswalks": []}, .3)
    result = item.update(walkable, LABELS, SHAPE, {"crosswalks": []}, .51)
    assert result["status"] == "finished"
    assert not result["crossing_active"]
    assert result["voice_text"] is None


# 하단 ROI에서 횡단보도가 사라진 경우의 무음 보류 확인
def test_crosswalk_roi_loss_is_uncertain_without_unknown_exit_warning():
    """횡단 중 마스크만 사라지면 방향을 추측하지 않고 무음 보류한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    missing = np.full(SHAPE[:2], LABELS["non_walkable"], np.uint8)
    result = item.update(missing, LABELS, SHAPE, SIGNAL, .61)
    assert result["status"] == "uncertain"
    assert result["crosswalk_roi"]["crosswalk_fraction"] == 0
    assert result["voice_text"] is None
    assert result["voice_clip"] is None
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .7)
    assert result["status"] == "crossing"


# 횡단 중 하단 ROI 소실 뒤 이탈 확정 확인
def test_confirmed_crossing_uses_pink_roi_to_finish_pending_exit():
    """정상 진입 뒤 보라색 ROI가 사라져도 분홍색 ROI와 직전 방향으로 이탈을 확정한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    visible_exit = mask(left=.58, right=.95, top=.70, bottom=1.0)
    pink_only_exit = mask(left=.58, right=.95, top=.70, bottom=.89)
    confirming = item.update(visible_exit, LABELS, SHAPE, SIGNAL, .3)
    missing = np.full(SHAPE[:2], LABELS["non_walkable"], np.uint8)
    held = item.update(missing, LABELS, SHAPE, SIGNAL, .5)
    result = item.update(pink_only_exit, LABELS, SHAPE, SIGNAL, .71)
    assert confirming["status"] == "crossing"
    assert held["status"] == "uncertain"
    assert held["reasons"] == ["pending_exit_roi_missing"]
    assert held["voice_text"] is None
    assert result["crosswalk_roi"]["crosswalk_fraction"] == 0
    assert result["exit_crosswalk_roi"]["crosswalk_fraction"] >= .05
    assert result["status"] == "outside_left"
    assert result["direction"] == "right"
    assert result["voice_text"] == "횡단보도 이탈, 오른쪽 이동"


# 진입 전 분홍색 ROI 단독 마스크 무음 확인
def test_pink_roi_alone_never_creates_exit_before_crossing_entry():
    """정상 횡단 진입 이력이 없으면 분홍색 ROI만으로 이탈 음성을 만들지 않는다."""
    item = engine()
    pink_only = mask(left=.58, right=.95, top=.70, bottom=.89)
    item.update(pink_only, LABELS, SHAPE, SIGNAL, 0)
    result = item.update(pink_only, LABELS, SHAPE, SIGNAL, .3)
    assert not result["crossing_active"]
    assert result["voice_text"] is None
    assert result["voice_clip"] is None


# 객체 가림 중 ROI 손실 보류 확인
def test_crosswalk_roi_loss_is_uncertain_when_object_occludes_roi():
    """차량 박스가 하단 ROI를 가리면 핑크 마스크 손실만으로 이탈을 확정하지 않는다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    missing = np.full(SHAPE[:2], LABELS["non_walkable"], np.uint8)
    vehicle = [{"class_name": "car", "xyxy": [20, 60, 80, 100], "observed": True}]
    item.update(missing, LABELS, SHAPE, SIGNAL, .3, detections=vehicle)
    result = item.update(missing, LABELS, SHAPE, SIGNAL, .7, detections=vehicle)
    assert result["status"] == "uncertain"
    assert result["reasons"] == ["crosswalk_roi_occluded"]
    assert result["crosswalk_roi"]["occlusion_fraction"] >= .08
    assert result["voice_text"] is None


# 이탈 상태에서 정상 도착 복구 확인
def test_outside_state_finishes_on_strong_walkable_destination():
    """이탈 경고 중이어도 발과 하단 ROI가 충분히 보행 가능하면 정상 도착으로 종료한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    item.update(mask(left=.58, right=.95), LABELS, SHAPE, SIGNAL, .3)
    result = item.update(mask(left=.58, right=.95), LABELS, SHAPE, SIGNAL, .51)
    assert result["status"] == "outside_left"
    walkable = np.full(SHAPE[:2], LABELS["walkable"], np.uint8)
    confirming = item.update(walkable, LABELS, SHAPE, {"crosswalks": []}, .6)
    assert confirming["status"] == "outside_left"
    assert confirming["voice_text"] == "횡단보도 이탈, 오른쪽 이동"
    result = item.update(walkable, LABELS, SHAPE, {"crosswalks": []}, 1.41)
    assert result["status"] == "finished"
    assert not result["crossing_active"]
    assert result["voice_text"] is None


# 하단 ROI 기본 높이 확인
def test_default_crosswalk_roi_uses_bottom_ten_percent():
    """기본 횡단보도 ROI는 가로 10~90%, 세로 90~100%를 사용한다."""
    config = crosswalk_safety_config()
    assert config["roi_left"] == .10
    assert config["roi_right"] == .90
    assert config["roi_top"] == .90
    assert config["roi_bottom"] == 1.0


# 횡단보도 ROI 좌표 검증 확인
def test_crosswalk_config_rejects_reversed_roi_bounds():
    """왼쪽·위쪽보다 작거나 같은 오른쪽·아래쪽 ROI 경계를 거부한다."""
    try:
        crosswalk_safety_config({"roi_left": .9, "roi_right": .1})
    except ValueError as error:
        assert "ROI bounds" in str(error)
    else:
        raise AssertionError("reversed crosswalk ROI was accepted")


# 불안정한 카메라에서 새 횡단 상태 억제 확인
def test_unstable_camera_cannot_confirm_crossing():
    """카메라가 불안정하면 가까운 횡단보도가 있어도 횡단 중으로 확정하지 않는다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0, camera_stable=False)
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .4, camera_stable=False)
    assert result["status"] == "uncertain"
    assert not result["crossing_active"]


# 잘못된 임계값 거부 확인
def test_crosswalk_config_rejects_invalid_margins():
    """실제 이탈 여유가 가장자리 주의 여유보다 크거나 같으면 설정을 거부한다."""
    try:
        crosswalk_safety_config({"exit_margin": .1, "edge_margin": .08})
    except ValueError as error:
        assert "exit margin" in str(error)
    else:
        raise AssertionError("invalid crosswalk margins were accepted")


# Ava 횡단보도 음원 파일 확인
def test_crosswalk_ava_clips_are_decodable():
    """진입 정렬과 방향 확정 이탈 안내 MP3가 실제 PCM으로 해독되는지 확인한다."""
    for filename in ("crosswalk-exit-left.mp3", "crosswalk-exit-right.mp3",
                     "crosswalk-align-left.mp3", "crosswalk-align-right.mp3"):
        assert decode_clip(filename)
