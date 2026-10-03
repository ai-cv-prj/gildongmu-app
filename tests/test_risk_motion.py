"""
file_path: tests/test_risk_motion.py

카메라 이동 계산 실패 유예와 실제 임계값 초과 처리를 검증한다.
실시간 프레임 간격이 길어도 짧은 계산 공백만으로 불안정 상태가 되지 않게 한다.
"""

import numpy as np
import pytest

from src.risk_config import risk_config
from src.risk_motion import CameraMotionGuard, camera_motion_inliers_valid


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


# RANSAC 설정 범위 확인
@pytest.mark.parametrize("value", [0, 2, True, 3.5])
def test_ransac_minimum_point_count_is_validated(value):
    """최소 일치점 개수에는 3 이상의 정수만 허용한다."""
    with pytest.raises(ValueError, match="camera_motion_min_inlier_points"):
        risk_config({"camera_motion_min_inlier_points": value})
