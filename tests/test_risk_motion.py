"""
file_path: tests/test_risk_motion.py

카메라 이동 계산 실패 유예와 실제 임계값 초과 처리를 검증한다.
실시간 프레임 간격이 길어도 짧은 계산 공백만으로 불안정 상태가 되지 않게 한다.
"""

import cv2
import numpy as np
import cv2
import pytest

from src.risk_config import risk_config
from src.risk_motion import (
    BackgroundStationarityGuard,
    CameraMotionGuard,
    camera_motion_inliers_valid,
)


FRAME = np.zeros((180, 320, 3), dtype=np.uint8)


class StubCameraMotionGuard(CameraMotionGuard):
    """영상 처리 대신 준비한 측정 결과를 차례로 반환하는 테스트 판정기이다."""

    # 테스트 측정 결과 준비
    def __init__(self, measurements):
        """기본 설정과 순서가 정해진 측정 결과를 저장한다."""
        super().__init__(risk_config({"camera_motion_failure_hold_s": .75}))
        self.measurements = iter(measurements)

    # 준비한 측정 결과 반환
    def _measure(self, frame):
        """실제 광학 흐름 대신 다음 안정·불안정·계산 불가 결과를 반환한다."""
        return next(self.measurements)


class StubBackgroundStationarityGuard(BackgroundStationarityGuard):
    """준비한 배경 이동량으로 3초 정지 전환을 검사하는 테스트 판정기이다."""

    # 테스트 측정 결과 준비
    def __init__(self, measurements):
        """광학 흐름 대신 사용할 측정 결과를 순서대로 저장한다."""
        super().__init__(risk_config({"stationary_voice_enabled": True}))
        self.measurements = iter(measurements)

    # 준비한 배경 이동량 반환
    def _measure(self, frame, detections, elapsed_s):
        """실제 프레임 계산 없이 다음 배경 이동량을 반환한다."""
        return next(self.measurements)


STILL = {
    "median_motion_per_s": .010,
    "p80_motion_per_s": .020,
    "scale_change_per_s": .010,
    "point_count": 20,
    "middle_point_count": 10,
    "lower_point_count": 10,
}


# 최초 계산 실패 유예 확인
def test_initial_motion_measurement_failure_is_held_briefly():
    """초기 변환 계산이 없더라도 0.75초까지 안정 상태로 유지한다."""
    guard = StubCameraMotionGuard([None, None, None])
    assert guard.update(FRAME, 0.0) is True
    assert guard.update(FRAME, .75) is True
    assert guard.update(FRAME, .76) is False


# 안정 판정 이후 계산 공백 확인
def test_stable_motion_survives_short_unavailable_gap():
    """안정 변환 뒤 짧은 계산 실패는 유지하고 유예시간 초과 시 불안정으로 바꾼다."""
    guard = StubCameraMotionGuard([True, None, None])
    assert guard.update(FRAME, 1.0) is True
    assert guard.update(FRAME, 1.7) is True
    assert guard.update(FRAME, 1.8) is False


# 실제 임계값 초과의 즉시 처리 확인
def test_explicit_unstable_motion_is_not_held():
    """계산된 이동이 임계값을 넘으면 유예하지 않고 이후 실패도 불안정으로 유지한다."""
    guard = StubCameraMotionGuard([True, False, None, True])
    assert guard.update(FRAME, 0.0) is True
    assert guard.update(FRAME, .1) is False
    assert guard.update(FRAME, .2) is False
    assert guard.update(FRAME, .3) is True


# 계산 실패 유예 설정 검증
def test_motion_failure_hold_must_be_positive():
    """계산 실패 유예시간에 0 이하 값을 허용하지 않는다."""
    with pytest.raises(ValueError, match="camera_motion_failure_hold_s"):
        risk_config({"camera_motion_failure_hold_s": 0})


# RANSAC 최소 일치 비율과 개수 확인
def test_ransac_requires_thirty_percent_and_twelve_points():
    """30% 이상이어도 12개 미만이거나 12개여도 30% 미만이면 거부한다."""
    config = risk_config()
    assert camera_motion_inliers_valid(
        np.asarray([[1]] * 12 + [[0]] * 28, dtype=np.uint8), config)
    assert not camera_motion_inliers_valid(
        np.asarray([[1]] * 11 + [[0]] * 19, dtype=np.uint8), config)
    assert not camera_motion_inliers_valid(
        np.asarray([[1]] * 12 + [[0]] * 38, dtype=np.uint8), config)


def test_stable_camera_exposes_normalized_background_transform():
    frame = np.random.default_rng(42).integers(0, 256, (180, 320, 3), dtype=np.uint8)
    moved = cv2.warpAffine(frame, np.asarray([[1, 0, 6], [0, 1, 3]], np.float32),
                           (320, 180))
    guard = CameraMotionGuard(risk_config())
    assert guard.update(frame, 0.0)
    assert guard.last_transform_norm is None
    assert guard.update(moved, 0.2)
    np.testing.assert_allclose(guard.last_transform_norm[:, 2],
                               [6 / 320, 3 / 180], atol=0.002)


# RANSAC 설정 범위 확인
@pytest.mark.parametrize("value", [0, 2, True, 3.5])
def test_ransac_minimum_point_count_is_validated(value):
    """최소 일치점 개수에는 3 이상의 정수만 허용한다."""
    with pytest.raises(ValueError, match="camera_motion_min_inlier_points"):
        risk_config({"camera_motion_min_inlier_points": value})


# 3초 연속 배경 정지 확인
def test_background_stationarity_requires_three_seconds_and_holds_short_motion():
    """3초 뒤 정지를 확정하고 0.8초 미만 흔들림에는 정지 상태를 유지한다."""
    moving = {**STILL, "median_motion_per_s": .04}
    guard = StubBackgroundStationarityGuard([None] + [STILL] * 7 + [moving] * 3)
    assert guard.update(FRAME, [], 0.0)["status"] == "uncertain"
    for timestamp in (.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        assert guard.update(FRAME, [], timestamp)["status"] == "confirming"
    result = guard.update(FRAME, [], 3.5)
    assert result["status"] == "stationary"
    assert result["duration_s"] == pytest.approx(3.0)
    assert guard.update(FRAME, [], 4.0)["status"] == "stationary"
    assert guard.update(FRAME, [], 4.5)["status"] == "stationary"
    assert guard.update(FRAME, [], 5.0)["status"] == "moving"


# 정지 중 짧은 배경 계산 실패 유지 확인
def test_stationary_state_holds_short_background_measurement_failure():
    """확정된 정지 상태에서는 0.8초 미만 특징점 손실로 음성 제한을 풀지 않는다."""
    guard = StubBackgroundStationarityGuard([STILL] * 8 + [None, None, None])
    for timestamp in (0, .5, 1, 1.5, 2, 2.5, 3, 3.5):
        result = guard.update(FRAME, [], timestamp)
    assert result["status"] == "stationary"
    assert guard.update(FRAME, [], 4.0)["status"] == "stationary"
    assert guard.update(FRAME, [], 4.5)["status"] == "stationary"
    assert guard.update(FRAME, [], 5.0)["status"] == "uncertain"


# 배경 계산 실패의 보수적 처리 확인
def test_background_measurement_failure_does_not_assume_stationary():
    """정지 후보 중 특징점 계산에 실패하면 음성 제한 상태로 진입하지 않는다."""
    guard = StubBackgroundStationarityGuard([STILL, STILL, None])
    assert guard.update(FRAME, [], 0.0)["status"] == "confirming"
    assert guard.update(FRAME, [], .5)["status"] == "confirming"
    assert guard.update(FRAME, [], 1.0)["status"] == "uncertain"


# 실제 광학 흐름의 하단 배경 이동 감지 확인
def test_background_flow_distinguishes_still_frame_from_walking_shift():
    """중·하단 무늬가 그대로면 정지 후보이고 크게 이동하면 보행 상태로 판정한다."""
    pattern = np.zeros_like(FRAME)
    for y in range(84, 168, 12):
        for x in range(12, 308, 12):
            cv2.circle(pattern, (x, y), 2, (255, 255, 255), -1)
    guard = BackgroundStationarityGuard(risk_config({"stationary_voice_enabled": True}))
    assert guard.update(pattern, [], 0.0)["status"] == "uncertain"
    assert guard.update(pattern.copy(), [], .5)["status"] == "confirming"
    shifted = cv2.warpAffine(pattern, np.float32([[1, 0, 0], [0, 1, 8]]), (320, 180))
    assert guard.update(shifted, [], 1.0)["status"] == "moving"
