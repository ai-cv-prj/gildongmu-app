"""
file_path: src/walking_surface.py

가상 발 주변의 보행가능 마스크로 보행로 좌우 이탈을 판정한다.
횡단보도 안내 중에는 판정을 멈추고 확실한 복귀 방향만 음성 이벤트로 만든다.
"""

from copy import deepcopy
import math

import cv2
import numpy as np


DEFAULT_WALKING_SURFACE = {
    "enabled": True,
    "foot_x": 0.50,
    "foot_y": 0.92,
    "foot_half_width": 0.04,
    "foot_half_height": 0.04,
    "exit_fraction": 0.40,
    "inside_fraction": 0.60,
    "exit_confirm_s": 0.30,
    "return_confirm_s": 0.50,
    "max_gap_s": 2.00,
    "stale_after_s": 1.50,
    "min_component_fraction": 0.003,
    "direction_deadband": 0.01,
}

PAUSED_CROSSWALK_STATUSES = frozenset({
    "approach", "align_left", "align_right", "crossing", "edge",
    "outside_left", "outside_right",
})


# 보행로 이탈 설정 검증
def walking_surface_config(value=None):
    """알 수 없는 키와 잘못된 임계값을 거부하고 전체 설정을 반환한다."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULT_WALKING_SURFACE):
        raise ValueError("walking_surface: unknown keys or invalid mapping")
    config = {**deepcopy(DEFAULT_WALKING_SURFACE), **deepcopy(value)}
    if not isinstance(config["enabled"], bool):
        raise ValueError("walking_surface.enabled must be boolean")
    unit_keys = (
        "foot_x", "foot_y", "foot_half_width", "foot_half_height",
        "exit_fraction", "inside_fraction", "min_component_fraction",
        "direction_deadband",
    )
    for key in unit_keys:
        item = config[key]
        if (isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item) or item <= 0 or item > 1):
            raise ValueError(f"walking_surface.{key} must be in (0,1]")
    for key in ("exit_confirm_s", "return_confirm_s", "max_gap_s", "stale_after_s"):
        item = config[key]
        if (isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item) or item <= 0):
            raise ValueError(f"walking_surface.{key} must be positive")
    if config["exit_fraction"] >= config["inside_fraction"]:
        raise ValueError("walking_surface exit fraction must be below inside fraction")
    return config


# 가상 발 ROI 좌표 계산
def foot_roi(shape, config):
    """정규화된 가상 발 설정을 픽셀 및 응답용 좌표로 반환한다."""
    height, width = shape[:2]
    left = max(0.0, config["foot_x"] - config["foot_half_width"])
    right = min(1.0, config["foot_x"] + config["foot_half_width"])
    top = max(0.0, config["foot_y"] - config["foot_half_height"])
    bottom = min(1.0, config["foot_y"] + config["foot_half_height"])
    return {
        "left": left, "right": right, "top": top, "bottom": bottom,
        "x1": max(0, round(left * width)), "x2": min(width, round(right * width)),
        "y1": max(0, round(top * height)), "y2": min(height, round(bottom * height)),
    }


# 발이 밟은 보행로 연결 영역 선택
def _foot_component(walkable, roi, minimum_area):
    """발 ROI와 가장 많이 겹치는 충분한 크기의 보행가능 연결 영역을 반환한다."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        walkable.astype(np.uint8), connectivity=8)
    patch = labels[roi["y1"]:roi["y2"], roi["x1"]:roi["x2"]]
    if patch.size == 0:
        return None
    candidates = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        overlap = int(np.count_nonzero(patch == label))
        if area >= minimum_area and overlap:
            candidates.append((overlap, area, label))
    if not candidates:
        return None
    return labels == max(candidates)[2]


# 이전 보행로와 이어진 현재 연결 영역 선택
def _matching_component(walkable, previous, minimum_area):
    """직전에 발이 있던 영역과 겹치는 현재 보행가능 연결 영역을 반환한다."""
    if previous is None or previous.shape != walkable.shape:
        return None
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        walkable.astype(np.uint8), connectivity=8)
    candidates = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < minimum_area:
            continue
        overlap = int(np.count_nonzero(previous & (labels == label)))
        if overlap:
            candidates.append((overlap, area, label))
    if not candidates:
        return None
    return labels == max(candidates)[2]


# 저장한 보행로를 향한 복귀 방향 계산
def _return_direction(component, shape, config):
    """가상 발에서 가장 가까운 이전 보행로 픽셀의 좌우 위치로 이탈 방향을 정한다."""
    if component is None or component.shape != tuple(shape[:2]):
        return None
    height, width = shape[:2]
    foot_x = config["foot_x"] * width
    foot_y = config["foot_y"] * height
    ys, xs = np.nonzero(component)
    if not len(xs):
        return None
    distances = (xs - foot_x) ** 2 + (ys - foot_y) ** 2
    nearest_x = float(xs[int(np.argmin(distances))]) / max(1, width - 1)
    delta = nearest_x - config["foot_x"]
    if delta > config["direction_deadband"]:
        return "left"
    if delta < -config["direction_deadband"]:
        return "right"
    return None


class WalkingSurfaceEngine:
    """가상 발이 보행로를 벗어난 시간을 누적해 방향성 이탈 상태를 만든다."""

    # 보행로 판정기 초기화
    def __init__(self, config=None):
        """검증된 설정을 저장하고 프레임 이력을 초기화한다."""
        self.config = walking_surface_config(config)
        self.reset()

    # 세션별 이력 초기화
    def reset(self):
        """이전 상태와 보행로 연결 영역 및 확인 시간을 모두 지운다."""
        self.status = "inside"
        self.pending_kind = None
        self.pending_since = None
        self.last_timestamp = None
        self.last_component = None
        self.direction = None
        self.event_id = 0

    # 연속 시간 조건 확인
    def _confirmed(self, kind, timestamp, duration):
        """같은 후보가 설정 시간 동안 이어졌는지 확인한다."""
        if self.pending_kind != kind:
            self.pending_kind = kind
            self.pending_since = timestamp
            return False
        return timestamp - self.pending_since + 1e-9 >= duration

    # 후보 상태 제거
    def _clear_pending(self):
        """현재 조건과 맞지 않는 전환 후보를 초기화한다."""
        self.pending_kind = None
        self.pending_since = None

    # 확정 상태 전환
    def _transition(self, status):
        """상태가 실제로 바뀐 경우에만 이벤트 번호를 증가시킨다."""
        if status != self.status:
            self.status = status
            self.event_id += 1

    # 프레임별 보행로 상태 갱신
    def update(self, class_map, label_ids, shape, timestamp, camera_stable=True,
               crosswalk_status=None, suspended=False):
        """초록색 보행가능 비율과 이전 연결 영역을 이용해 좌우 이탈을 반환한다."""
        roi = foot_roi(shape, self.config)
        result = {
            "enabled": self.config["enabled"] and not suspended, "status": "disabled",
            "direction": None, "voice_text": None, "voice_clip": None,
            "repeat": False, "event_id": self.event_id, "reasons": [],
            "walkable_fraction": None,
            "roi": {key: roi[key] for key in ("left", "right", "top", "bottom")},
            "stale_after_ms": round(self.config["stale_after_s"] * 1000),
        }
        if suspended:
            self.reset()
            result.update(event_id=self.event_id, roi=None, reasons=["bus_stop"])
            return result
        if not self.config["enabled"]:
            return result
        if (self.last_timestamp is not None and
                (timestamp <= self.last_timestamp
                 or timestamp - self.last_timestamp > self.config["max_gap_s"])):
            self.reset()
        self.last_timestamp = timestamp
        if crosswalk_status in PAUSED_CROSSWALK_STATUSES:
            self._clear_pending()
            self.status = "inside"
            self.direction = None
            self.last_component = None
            result.update(status="paused", reasons=["crosswalk_active"])
            return result
        if (not camera_stable or class_map is None or not label_ids
                or class_map.shape != tuple(shape[:2]) or label_ids.get("walkable") is None):
            self._clear_pending()
            result.update(status="uncertain", reasons=[
                "camera_unstable" if not camera_stable else "walkable_mask_unavailable"])
            return result

        walkable = class_map == label_ids["walkable"]
        patch = walkable[roi["y1"]:roi["y2"], roi["x1"]:roi["x2"]]
        if patch.size == 0:
            self._clear_pending()
            result.update(status="uncertain", reasons=["empty_foot_roi"])
            return result
        fraction = float(np.mean(patch))
        result["walkable_fraction"] = fraction
        minimum_area = max(1, round(walkable.size * self.config["min_component_fraction"]))
        component = _foot_component(walkable, roi, minimum_area)

        if fraction >= self.config["inside_fraction"]:
            if component is not None:
                self.last_component = component
            if self.status.startswith("outside"):
                if self._confirmed("return", timestamp, self.config["return_confirm_s"]):
                    self._transition("inside")
                    self.direction = None
                    self._clear_pending()
                else:
                    result.update(status="returning", direction=self.direction,
                                  reasons=["return_confirming"])
                    return result
            else:
                self._transition("inside")
                self._clear_pending()
        elif fraction < self.config["exit_fraction"]:
            connected = _matching_component(walkable, self.last_component, minimum_area)
            direction = _return_direction(connected, shape, self.config)
            if direction is None:
                self._clear_pending()
                result.update(status="uncertain", reasons=["return_direction_unavailable"])
                return result
            outside_status = f"outside_{direction}"
            if self.status.startswith("outside"):
                self.direction = direction
                self._transition(outside_status)
                self._clear_pending()
            elif self._confirmed(outside_status, timestamp, self.config["exit_confirm_s"]):
                self.direction = direction
                self._transition(outside_status)
                self._clear_pending()
            else:
                result.update(status="exit_confirming", direction=direction,
                              reasons=["exit_confirming"])
                return result
        else:
            self._clear_pending()
            result.update(status=self.status, direction=self.direction,
                          reasons=["boundary_band"])
            return result

        result.update(status=self.status, direction=self.direction, event_id=self.event_id)
        if self.status == "outside_left":
            result.update(voice_text="보행로 이탈, 오른쪽 이동",
                          voice_clip="walkway-exit-right.mp3", repeat=True,
                          reasons=["walkable_left_exit"])
        elif self.status == "outside_right":
            result.update(voice_text="보행로 이탈, 왼쪽 이동",
                          voice_clip="walkway-exit-left.mp3", repeat=True,
                          reasons=["walkable_right_exit"])
        else:
            result["reasons"] = ["inside_walkable"]
        return result
