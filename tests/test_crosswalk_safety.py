"""
file_path: tests/test_crosswalk_safety.py

횡단보도 상태 판정의 진입·가장자리·이탈·복귀·정상 도착을 검증한다.
"""

import cv2
import numpy as np

from src.crosswalk_safety import CrosswalkSafetyEngine, crosswalk_safety_config
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


# 멀리 보이는 횡단보도 무음 확인
def test_far_crosswalk_never_starts_crossing():
    """화면 아래까지 닿지 않은 핑크 마스크는 횡단 시작 근거로 사용하지 않는다."""
    item = engine()
    result = item.update(mask(top=.15, bottom=.55), LABELS, SHAPE, SIGNAL, 0)
    result = item.update(mask(top=.15, bottom=.55), LABELS, SHAPE, SIGNAL, .3)
    assert result["status"] == "approach"
    assert not result["crossing_active"]
    assert result["voice_text"] is None


# 진입 뒤 가장자리 진동 확인
def test_crossing_entry_and_edge_vibration():
    """가까운 횡단보도 안에 진입한 뒤 가장자리에서는 음성 없이 진동만 요청한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    assert result["status"] == "crossing"
    item.update(mask(left=.43, right=.90), LABELS, SHAPE, SIGNAL, .3)
    result = item.update(mask(left=.43, right=.90), LABELS, SHAPE, SIGNAL, .41)
    assert result["status"] == "edge"
    assert result["vibration"] == "edge"
    assert result["voice_text"] is None


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
    assert result["voice_text"] == "횡단보도 이탈! 오른쪽으로 이동하세요!"
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


# 하단 ROI에서 횡단보도가 사라진 경우의 방향 미확정 이탈 확인
def test_crosswalk_roi_loss_warns_and_recovers_without_direction():
    """횡단 중 하단 ROI의 핑크 마스크가 사라지면 방향 없는 이탈 경고를 유지한다."""
    item = engine()
    item.update(mask(), LABELS, SHAPE, SIGNAL, 0)
    item.update(mask(), LABELS, SHAPE, SIGNAL, .2)
    missing = np.full(SHAPE[:2], LABELS["non_walkable"], np.uint8)
    first = item.update(missing, LABELS, SHAPE, SIGNAL, .3)
    result = item.update(missing, LABELS, SHAPE, SIGNAL, .61)
    assert first["status"] == "crossing"
    assert result["status"] == "outside_unknown"
    assert result["crosswalk_roi"]["crosswalk_fraction"] == 0
    assert result["voice_text"] == "횡단보도 이탈!"
    assert result["voice_clip"] == "crosswalk-exit-unknown.mp3"
    assert result["repeat"]
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .7)
    assert result["status"] == "outside_unknown"
    result = item.update(mask(), LABELS, SHAPE, SIGNAL, .91)
    assert result["status"] == "crossing"


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
    assert confirming["voice_text"] == "횡단보도 이탈! 오른쪽으로 이동하세요!"
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
    """왼쪽·오른쪽·방향 미확정 안내 MP3가 실제 PCM으로 해독되는지 확인한다."""
    for filename in ("crosswalk-exit-left.mp3", "crosswalk-exit-right.mp3",
                     "crosswalk-exit-unknown.mp3"):
        assert decode_clip(filename)
