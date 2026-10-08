"""
file_path: src/walking_voice.py

보행 위험 객체의 분포를 왼쪽·가운데·오른쪽으로 나눠 안전 행동을 안내한다.
객체 위치가 아니라 최종 이동 행동이 변경될 때만 새 음성을 기록한다.
"""

from math import isfinite
from copy import deepcopy

import numpy as np
from src.settings import load_audio_settings


GUIDANCE = load_audio_settings()["guidance"]
LEFT_MAX_RATIO = GUIDANCE["walking_left_max_ratio"]
RIGHT_MIN_RATIO = GUIDANCE["walking_right_min_ratio"]
CENTER_INTRUSION_RATIO = GUIDANCE["walking_center_intrusion_ratio"]
SIDE_INTRUSION_RATIO = GUIDANCE["walking_side_intrusion_ratio"]
VOICE_IMMEDIATE_OVERLAP_RATIO = GUIDANCE["walking_voice_immediate_overlap_ratio"]
DISTANCE_TIE_RATIO = GUIDANCE["walking_distance_tie_ratio"]
WALKABLE_SIDE_TIE_RATIO = GUIDANCE["walking_walkable_side_tie_ratio"]
TWO_STEP_ENTER_RATIO = GUIDANCE["walking_two_step_enter_ratio"]
TWO_STEP_EXIT_RATIO = GUIDANCE["walking_two_step_exit_ratio"]
LATERAL_CONFIRM_S = GUIDANCE.get("walking_lateral_confirm_ms", 200) / 1000
FROM_STOP_CONFIRM_S = GUIDANCE.get("walking_from_stop_confirm_ms", 500) / 1000
SAME_DIRECTION_REPEAT_S = GUIDANCE.get("walking_same_direction_repeat_ms", 1500) / 1000
CROWDED_REDIRECT_S = GUIDANCE.get("walking_crowded_redirect_ms", 3000) / 1000
REPEAT_NONE_S = GUIDANCE.get("walking_repeat_none_ms", 3000) / 1000
STOP_REPEAT_NONE_S = GUIDANCE.get("walking_stop_repeat_none_ms", 1000) / 1000
ACTION_MESSAGES = {
    ("left", 1): ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"),
    ("left", 2): ("왼쪽으로 두 걸음", "walking-move-left-two.mp3"),
    ("right", 1): ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"),
    ("right", 2): ("오른쪽으로 두 걸음", "walking-move-right-two.mp3"),
    "crowded": ("전방 혼잡 주의하세요", "walking-crowded.mp3"),
    "blocked": ("전방 장애물 주의하세요", "walking-obstacle.mp3"),
    "stop": ("멈추세요", "walking-stop.mp3"),
}
VEHICLE_CLASSES = frozenset({"car", "bus", "truck", "motorcycle"})


# 이전 행동과 다음 행동에 맞는 음성 확정 시간 선택
def transition_confirm_s(previous_action, next_action):
    """
    긴급 정지는 즉시 안내하고 나머지 행동 전환에 지정된 안정화 시간을 반환한다.
    """
    if previous_action is None or next_action == "stop":
        return 0.0
    if previous_action == "stop":
        return FROM_STOP_CONFIRM_S
    return LATERAL_CONFIRM_S


# 같은 음성을 다시 허용할 none 유지 시간 선택
def repeat_none_s(previous_action):
    """
    멈춤과 일반 이동 안내에 각각 지정된 무음 유지 시간을 반환한다.
    """
    return STOP_REPEAT_NONE_S if previous_action == "stop" else REPEAT_NONE_S


# 객체 바닥에서 지정 영역 픽셀 비율 계산
def label_contact_fraction(item, class_map, label_ids, shape, half_height, label_name):
    """객체 박스 바닥의 좁은 접촉 영역에서 지정한 라벨의 픽셀 비율을 계산한다."""
    if (class_map is None or not label_ids or label_name not in label_ids
            or class_map.shape != tuple(shape[:2])):
        return None
    box = item.get("xyxy")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x1, _, x2, y2 = map(float, box)
    except (TypeError, ValueError):
        return None
    if not all(isfinite(value) for value in (x1, x2, y2)):
        return None
    height, width = shape[:2]
    if height <= 0 or width <= 0:
        return None
    left = max(0, min(width - 1, int(np.floor(x1))))
    right = max(left + 1, min(width, int(np.ceil(x2))))
    radius = max(1, round(height * half_height))
    top = max(0, min(height - 1, round(y2) - radius))
    bottom = max(top + 1, min(height, round(y2) + radius))
    patch = class_map[top:bottom, left:right]
    if patch.size == 0:
        return None
    return float(np.mean(patch == label_ids[label_name]))


# 객체 바닥에서 횡단보도 픽셀 비율 계산
def crosswalk_contact_fraction(item, class_map, label_ids, shape, half_height):
    """객체 박스 바닥의 좁은 접촉 영역에서 횡단보도 픽셀 비율을 계산한다."""
    return label_contact_fraction(
        item, class_map, label_ids, shape, half_height, "crosswalk")


# 음성 판정용 bbox 바깥 U자 띠의 보행가능 비율 계산
def voice_surrounding_walkability(item, class_map, label_ids, shape, config):
    """bbox 좌우 하단 20%와 바로 아래 띠에서 walkable 픽셀의 통합 비율을 계산한다."""
    unavailable = {
        "status": "unavailable", "walkable_fraction": None,
        "pixel_count": 0, "visible_region_count": 0,
        "expected_region_count": 3, "regions": {},
    }
    if (class_map is None or not label_ids or "walkable" not in label_ids
            or class_map.shape != tuple(shape[:2])):
        return unavailable
    try:
        x1, y1, x2, y2 = map(float, item["xyxy"])
    except (KeyError, TypeError, ValueError):
        return unavailable
    if not all(isfinite(value) for value in (x1, y1, x2, y2)):
        return unavailable
    height, width = shape[:2]
    left = max(0, min(width, int(np.floor(x1))))
    top = max(0, min(height, int(np.floor(y1))))
    right = max(0, min(width, int(np.ceil(x2))))
    bottom = max(0, min(height, int(np.ceil(y2))))
    if right <= left or bottom <= top:
        return unavailable
    box_width, box_height = right - left, bottom - top
    minimum = config.get("obstacle_surrounding_min_region_pixels", 4)
    side_width = max(minimum, min(
        round(box_width * config.get("obstacle_surrounding_side_width_ratio", .15)),
        round(width * config.get("obstacle_surrounding_max_side_width_ratio", .02)),
    ))
    side_height = max(
        minimum,
        round(box_height * config.get("obstacle_surrounding_side_height_ratio", .20)),
    )
    bottom_height = max(minimum, min(
        round(box_height * config.get("obstacle_surrounding_bottom_height_ratio", .10)),
        round(height * config.get("obstacle_surrounding_max_bottom_height_ratio", .02)),
    ))
    candidates = {
        "left": (left - side_width, bottom - side_height, left, bottom),
        "right": (right, bottom - side_height, right + side_width, bottom),
        "bottom": (left, bottom, right, bottom + bottom_height),
    }
    walkable_id = label_ids["walkable"]
    regions = {}
    total_pixels = 0
    walkable_pixels = 0
    for name, (rx1, ry1, rx2, ry2) in candidates.items():
        rx1, rx2 = max(0, rx1), min(width, rx2)
        ry1, ry2 = max(0, ry1), min(height, ry2)
        if rx2 <= rx1 or ry2 <= ry1:
            continue
        patch = class_map[ry1:ry2, rx1:rx2]
        if patch.size < minimum:
            continue
        region_walkable = int(np.count_nonzero(patch == walkable_id))
        regions[name] = {
            "walkable_fraction": region_walkable / int(patch.size),
            "pixel_count": int(patch.size),
        }
        walkable_pixels += region_walkable
        total_pixels += int(patch.size)
    if not regions:
        return {**unavailable, "status": "clipped"}
    return {
        "status": "available" if len(regions) == 3 else "partial",
        "walkable_fraction": walkable_pixels / total_pixels,
        "pixel_count": total_pixels,
        "visible_region_count": len(regions),
        "expected_region_count": 3,
        "regions": regions,
    }


# 장애물 주변과 비초록 신호의 횡단보도 음성 제외 표시
def apply_obstacle_voice_suppression(prediction, signal, class_map, label_ids, shape, config):
    """바깥 띠와 비초록 횡단보도 근거에 따라 위험 객체를 음성에서 제외한다."""
    prediction["walkable_sides"] = walkable_side_fractions(
        class_map, label_ids, shape)
    prediction["direction_walkability"] = direction_edge_walkability(
        class_map, label_ids, shape, prediction.get("roi"),
        config.get("walking_direction_edge_width_ratio", .20),
        config.get("walking_direction_min_walkable_ratio", .30),
    )
    for item in prediction.get("detections", []):
        item.pop("voice_suppressed_reason", None)
        item.pop("crosswalk_contact_fraction", None)
        item.pop("voice_surrounding_walkability", None)
        if item.get("alert_level", item.get("risk_level")) != "danger":
            continue
        surroundings = voice_surrounding_walkability(
            item, class_map, label_ids, shape, config)
        item["voice_surrounding_walkability"] = surroundings
        fraction = surroundings["walkable_fraction"]
        if config.get("obstacle_surrounding_voice_suppression", True):
            if fraction is None:
                item["voice_suppressed_reason"] = "walkable_surroundings_unavailable"
            else:
                threshold = config.get(
                    "obstacle_surrounding_partial_walkable_threshold", .05
                ) if surroundings["status"] == "partial" else config.get(
                    "obstacle_surrounding_walkable_threshold", .30)
                if fraction <= threshold:
                    item["voice_suppressed_reason"] = "low_walkable_surroundings"
    signal = signal or {}
    selected = signal.get("selected_detection_index")
    state = signal.get("signal_state")
    if (not config.get("non_green_obstacle_voice_suppression", True)
            or not isinstance(selected, int) or isinstance(selected, bool)
            or state not in ("red", "unknown")):
        return prediction
    crosswalk_threshold = config.get("non_green_obstacle_crosswalk_threshold", .20)
    half_height = config.get("non_green_obstacle_crosswalk_contact_half_height", .02)
    for item in prediction.get("detections", []):
        if item.get("alert_level", item.get("risk_level")) != "danger":
            continue
        crosswalk_fraction = crosswalk_contact_fraction(
            item, class_map, label_ids, shape, half_height)
        if crosswalk_fraction is not None:
            item["crosswalk_contact_fraction"] = crosswalk_fraction
        if crosswalk_fraction is not None and crosswalk_fraction >= crosswalk_threshold:
            item["voice_suppressed_reason"] = "non_green_signal_crosswalk_obstacle"
    return prediction


# 전체 화면 좌우의 보행 가능 픽셀 비율 계산
def walkable_side_fractions(class_map, label_ids, shape):
    """화면 중앙을 기준으로 왼쪽과 오른쪽의 walkable 라벨 비율을 반환한다."""
    unavailable = {"status": "unavailable", "left": None, "right": None}
    if (class_map is None or not label_ids or "walkable" not in label_ids
            or class_map.shape != tuple(shape[:2])):
        return unavailable
    width = class_map.shape[1]
    middle = width // 2
    if middle <= 0 or middle >= width:
        return unavailable
    walkable_id = label_ids["walkable"]
    left = class_map[:, :middle]
    right = class_map[:, middle:]
    if left.size == 0 or right.size == 0:
        return unavailable
    return {
        "status": "available",
        "left": float(np.mean(left == walkable_id)),
        "right": float(np.mean(right == walkable_id)),
    }


# 핑크 ROI 양쪽 끝의 보행 가능 비율 계산
def direction_edge_walkability(class_map, label_ids, shape, roi, edge_ratio, minimum):
    """핑크 ROI 좌우 끝 영역에서 초록 보행 마스크가 차지하는 비율을 반환한다."""
    unavailable = {
        "status": "unavailable", "left": None, "right": None,
        "edge_width_ratio": edge_ratio, "minimum": minimum,
    }
    if (class_map is None or not label_ids or "walkable" not in label_ids
            or class_map.shape != tuple(shape[:2])):
        return unavailable
    polygon = (roi or {}).get("immediate_polygon")
    if not isinstance(polygon, list) or len(polygon) < 4:
        return unavailable
    try:
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
    except (TypeError, ValueError, IndexError):
        return unavailable
    if not all(isfinite(value) for value in xs + ys):
        return unavailable
    height, width = class_map.shape
    left = max(0, min(width, int(np.floor(min(xs) * width))))
    right = max(0, min(width, int(np.ceil(max(xs) * width))))
    top = max(0, min(height, int(np.floor(min(ys) * height))))
    bottom = max(0, min(height, int(np.ceil(max(ys) * height))))
    edge_width = max(1, int(round((right - left) * edge_ratio)))
    if right <= left or bottom <= top or edge_width > right - left:
        return unavailable
    patches = {
        "left": class_map[top:bottom, left:left + edge_width],
        "right": class_map[top:bottom, right - edge_width:right],
    }
    if any(patch.size == 0 for patch in patches.values()):
        return unavailable
    walkable_id = label_ids["walkable"]
    return {
        "status": "available",
        "left": float(np.mean(patches["left"] == walkable_id)),
        "right": float(np.mean(patches["right"] == walkable_id)),
        "edge_width_ratio": edge_ratio,
        "minimum": minimum,
    }


# 이동 후보 방향의 보행 마스크가 부족할 때 장애물 주의 음성으로 대체
def direction_voice_action(action, prediction):
    """판독 가능한 목표 방향의 보행 마스크가 기준 미만이면 blocked를 반환한다."""
    if action not in ("left", "right"):
        return action
    walkability = prediction.get("direction_walkability") or {}
    if walkability.get("status") != "available":
        return action
    fraction = walkability.get(action)
    minimum = walkability.get("minimum")
    if (isinstance(fraction, (int, float)) and not isinstance(fraction, bool)
            and isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
            and isfinite(fraction) and isfinite(minimum) and fraction < minimum):
        return "blocked"
    return action


# 객체 하단 발자국의 화면 방향 판정
def warning_directions(item, image_width):
    """bbox 하단 발자국이 유효하게 침범한 왼쪽·가운데·오른쪽 구역을 반환한다."""
    try:
        box = item["xyxy"]
        left = max(0.0, min(float(image_width), float(box[0])))
        right = max(0.0, min(float(image_width), float(box[2])))
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        return set()
    if image_width <= 0 or not all(isfinite(value) for value in (left, right)) or right <= left:
        return set()
    boundaries = {
        "left": (0.0, image_width * LEFT_MAX_RATIO),
        "center": (image_width * LEFT_MAX_RATIO, image_width * RIGHT_MIN_RATIO),
        "right": (image_width * RIGHT_MIN_RATIO, float(image_width)),
    }
    overlaps = {direction: max(0.0, min(right, end) - max(left, start))
                for direction, (start, end) in boundaries.items()}
    largest = max(overlaps.values())
    directions = {direction for direction, overlap in overlaps.items()
                  if overlap > 0 and overlap == largest}
    for direction, overlap in overlaps.items():
        start, end = boundaries[direction]
        threshold = CENTER_INTRUSION_RATIO if direction == "center" else SIDE_INTRUSION_RATIO
        if overlap >= (end - start) * threshold:
            directions.add(direction)
    return directions


# 핑크 근거리 ROI 음성 대상 확인
def inside_voice_roi(item):
    """객체 하단 발자국이 핑크 ROI와 음성 기준 이상 겹치는지 확인한다."""
    try:
        overlap = float(item["geometry"]["immediate_overlap"])
    except (KeyError, TypeError, ValueError):
        return False
    return isfinite(overlap) and overlap >= VOICE_IMMEDIATE_OVERLAP_RATIO


# 행동 안내에 사용할 위험·주의 객체 선별
def guidance_items(prediction, level, crossing_active=False, stationary_voice=False):
    """근거리와 확인된 예측 위험을 음성에 연결한다."""
    warning = prediction.get("warning") or {}
    if warning.get("source") == "camera_view":
        return []
    stationary = (stationary_voice
                  and (prediction.get("stationarity") or {}).get("status") == "stationary")
    if stationary:
        return []
    return [item for item in prediction.get("detections", [])
            if item.get("alert_level", item.get("risk_level")) == level
            and item.get("warning_primary", True)
            and not item.get("voice_suppressed_reason")
            and (not crossing_active or item.get("class_name") in VEHICLE_CLASSES
                 or item.get("class_name") == "bicycle" and rapid_approach_hazard(item))
            and not item.get("risk_suppressed_reason")
            and (level != "danger" or inside_voice_roi(item) or rapid_approach_hazard(item))]


def rapid_approach_hazard(item):
    """안정적으로 확인된 빠른 접근·짧은 TTC 위험인지 반환한다."""
    reasons = item.get("reasons", [])
    motion = item.get("motion") or {}
    ground = motion.get("ground_approach") or {}
    if ("ground_approaching_near_path" in reasons
            and ground.get("quality") == "valid"
            and ground.get("time_to_near_s") is not None):
        return True
    return (motion.get("quality") == "valid"
            and bool({"approaching_near_path", "short_ttc",
                      "predicted_moving_conflict"}.intersection(reasons)))


# 후보 방향과 객체 박스 사이의 가로 거리 계산
def horizontal_distance(item, point_x):
    """후보 방향의 중심점과 객체 박스 사이의 가장 짧은 가로 거리를 반환한다."""
    try:
        left, right = float(item["xyxy"][0]), float(item["xyxy"][2])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not all(isfinite(value) for value in (left, right, point_x)):
        return None
    return left - point_x if point_x < left else point_x - right if point_x > right else 0.0


# 한 방향에서 가장 가까운 객체 거리 계산
def nearest_distance(items, direction, image_width):
    """왼쪽 또는 오른쪽 후보 구역의 중심에서 가장 가까운 객체 거리를 반환한다."""
    ratio = LEFT_MAX_RATIO / 2 if direction == "left" else (1 + RIGHT_MIN_RATIO) / 2
    distances = [horizontal_distance(item, image_width * ratio) for item in items]
    valid = [distance for distance in distances if distance is not None]
    return min(valid) if valid else float("inf")


# 가운데가 막혔을 때 좌우 후보 비교
def safer_side(dangers, image_width, walkable_sides=None):
    """위험 거리 동률이면 전체 화면의 보행 가능 비율이 높은 방향을 반환한다."""
    tie = image_width * DISTANCE_TIE_RATIO
    left_distance = nearest_distance(dangers, "left", image_width)
    right_distance = nearest_distance(dangers, "right", image_width)
    if abs(left_distance - right_distance) > tie:
        return "left" if left_distance > right_distance else "right"
    walkable_sides = walkable_sides or {}
    if walkable_sides.get("status") == "available":
        left_walkable = walkable_sides.get("left")
        right_walkable = walkable_sides.get("right")
        if (isinstance(left_walkable, (int, float))
                and isinstance(right_walkable, (int, float))
                and isfinite(left_walkable) and isfinite(right_walkable)
                and abs(left_walkable - right_walkable) > WALKABLE_SIDE_TIE_RATIO):
            return "left" if left_walkable > right_walkable else "right"
    return None


# 가운데 영역을 위험 객체가 차지한 가로 비율 계산
def center_occupancy_ratio(items, image_width):
    """여러 객체의 중복을 제거한 가운데 영역 가로 점유율을 반환한다."""
    if image_width <= 0:
        return 0.0
    start = image_width * LEFT_MAX_RATIO
    end = image_width * RIGHT_MIN_RATIO
    intervals = []
    for item in items:
        try:
            left = max(start, min(end, float(item["xyxy"][0])))
            right = max(start, min(end, float(item["xyxy"][2])))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if all(isfinite(value) for value in (left, right)) and right > left:
            intervals.append((left, right))
    occupied = 0.0
    cursor = None
    for left, right in sorted(intervals):
        if cursor is None or left > cursor:
            occupied += right - left
            cursor = right
        elif right > cursor:
            occupied += right - cursor
            cursor = right
    return occupied / (end - start)


# 좌우 이동에 필요한 걸음 수 선택
def movement_steps(items, action, image_width, previous_steps=None):
    """가운데 점유율과 45~55% 히스테리시스로 한 걸음 또는 두 걸음을 선택한다."""
    if action not in ("left", "right"):
        return None
    occupancy = center_occupancy_ratio(items, image_width)
    if occupancy >= TWO_STEP_ENTER_RATIO:
        return 2
    if occupancy <= TWO_STEP_EXIT_RATIO:
        return 1
    if previous_steps in (1, 2):
        return previous_steps
    return 2 if occupancy >= (TWO_STEP_ENTER_RATIO + TWO_STEP_EXIT_RATIO) / 2 else 1


# 위험 분포를 최종 이동 행동으로 변환
def walking_action(prediction, image_width, crossing_active=False, stationary_voice=False):
    """위험 분포에 따라 좌우 회피·정지 또는 안내 없음으로 판단한다."""
    dangers = guidance_items(prediction, "danger", crossing_active, stationary_voice)
    if any("predicted_moving_conflict" in item.get("reasons", []) for item in dangers):
        return "stop"
    directions = {direction for item in dangers
                  for direction in warning_directions(item, image_width)}
    if "center" not in directions:
        return None
    if directions == {"left", "center"}:
        return "right"
    if directions == {"center", "right"}:
        return "left"
    if directions == {"left", "center", "right"}:
        return "crowded"
    if directions == {"center"}:
        return safer_side(
            dangers, image_width, prediction.get("walkable_sides")) or "blocked"
    return "stop"


class WalkingVoice:
    """영상 프레임마다 최종 이동 행동이 달라질 때만 음성을 기록한다."""

    # 영상별 안내 상태 초기화
    def __init__(self):
        """마지막 안내 행동과 저장 영상용 오디오 이벤트를 빈 상태로 시작한다."""
        self.last_action = None
        self.last_steps = None
        self.events = []
        self.pending_action = None
        self.pending_since = None
        self.clear_since = None
        self.previous_time = None
        self.epoch = None
        self.hazards = {}
        self.voice_event_id = 0
        self.missing_hold_s = GUIDANCE.get("walking_missing_hold_ms", 800) / 1000
        self.crowded_until = 0.0
        self.crowded_redirect_action = None
        self.last_direction_voice_at = {"left": None, "right": None}

    def _evidence(self, prediction, timestamp):
        """짧은 미검출을 안전한 경로라는 증거로 사용하지 않는다."""
        if (self.previous_time is not None and (timestamp <= self.previous_time
                or timestamp - self.previous_time > 2.0)
                or self.epoch is not None and self.epoch != prediction.get("state_epoch", 0)):
            self.hazards.clear()
            self.last_action = self.last_steps = None
            self.pending_action = self.pending_since = self.clear_since = None
            self.crowded_until = 0.0
            self.crowded_redirect_action = None
            self.last_direction_voice_at = {"left": None, "right": None}
        self.previous_time = timestamp
        self.epoch = prediction.get("state_epoch", 0)
        self.hazards = {key: value for key, value in self.hazards.items()
                        if timestamp - value[0] <= self.missing_hold_s}
        current = prediction.get("detections", [])
        keys = set()
        suppress_stop = (prediction.get("boarding") or {}).get("assumed_stationary", False)
        if suppress_stop:
            self.hazards = {key: value for key, value in self.hazards.items()
                            if value[1].get("class_name") != "transit_stop"}
        for item in current:
            identity = item.get("hazard_id")
            if identity is None:
                identity = ("event", item.get("event_id")) if item.get("event_id") is not None else (
                    "track", item.get("track_id")) if item.get("track_id") is not None else None
            if identity is None:
                continue
            keys.add(identity)
            if (item.get("alert_level", item.get("risk_level")) in ("danger", "caution")
                    and not item.get("risk_suppressed_reason")):
                self.hazards[identity] = (timestamp, deepcopy(item))
            else:
                self.hazards.pop(identity, None)
        retained = [dict(item, observed=False) for key, (_, item) in self.hazards.items() if key not in keys]
        return {**prediction, "detections": current + retained}, len(retained)

    # 현재 프레임의 신규 위험 안내 기록
    def observe(self, prediction, image_width, output_time_s, crossing_active=False,
                crosswalk_status=None):
        """횡단 상태와 음성 후보 조건을 반영해 변경된 회피·정지 음원만 예약한다."""
        prediction.pop("voice_text", None)
        prediction.pop("voice_clip", None)
        prediction.pop("voice_event", None)
        evidence, retained = self._evidence(prediction, output_time_s)
        previous_action = self.last_action
        previous_steps = self.last_steps
        display_action = walking_action(evidence, image_width)
        vehicle_only = crossing_active or crosswalk_status == "approach"
        raw_action = walking_action(evidence, image_width, vehicle_only, True)
        raw_action = direction_voice_action(raw_action, evidence)
        eligible = guidance_items(evidence, "danger", vehicle_only, True)
        if (prediction.get("boarding") or {}).get("assumed_stationary") and eligible:
            # 입력 중에는 측면 위험만 남아도 기존 정지 안내를 유지한다.
            raw_action = "stop"
        raw_steps = movement_steps(
            eligible, raw_action, image_width,
            previous_steps if raw_action == previous_action else None,
        )
        voice_action = raw_action
        voice_steps = raw_steps
        redirected_crowded = False
        if raw_action in ("left", "right") and output_time_s < self.crowded_until:
            redirected_crowded = raw_action != self.crowded_redirect_action
            self.crowded_redirect_action = raw_action
            voice_action = "crowded"
            voice_steps = None
        elif raw_action != "crowded":
            self.crowded_redirect_action = None
        stationary_clear = ((prediction.get("stationarity") or {}).get("status") == "stationary"
                            and raw_action is None)
        if raw_action is None:
            self.pending_action = self.pending_since = None
            if self.clear_since is None:
                self.clear_since = output_time_s
            if (self.last_action is not None
                    and output_time_s - self.clear_since + 1e-6
                    >= repeat_none_s(self.last_action)):
                self.last_action = None
                self.last_steps = None
        else:
            if self.clear_since is not None and self.last_action is not None:
                none_duration = output_time_s - self.clear_since
                same_action = voice_action == self.last_action
                repeat_ready = none_duration + 1e-6 >= repeat_none_s(self.last_action)
                # none 전후 행동이 다르면 새 안내로 보고 안정화 시간 없이 즉시 재생한다.
                if not same_action or repeat_ready:
                    self.last_action = None
                    self.last_steps = None
            self.clear_since = None
            # 같은 방향의 걸음 수 변화도 동일 행동으로 보고 반복 안내하지 않는다.
            if voice_action == self.last_action:
                voice_steps = self.last_steps
            candidate = (voice_action, voice_steps)
            previous = (self.last_action, self.last_steps)
            if voice_action == "stop" or self.last_action is None:
                self.pending_action = self.pending_since = None
            elif candidate != previous:
                if self.pending_action != candidate:
                    self.pending_action, self.pending_since = candidate, output_time_s
                confirmation = transition_confirm_s(self.last_action, voice_action)
                if output_time_s - self.pending_since + 1e-6 < confirmation:
                    voice_action = self.last_action
                    voice_steps = self.last_steps
                else:
                    self.pending_action = self.pending_since = None
            else:
                self.pending_action = self.pending_since = None
        prediction["last_action"] = display_action
        prediction["voice_action"] = voice_action
        prediction["voice_steps"] = voice_steps
        prediction["voice_clear"] = (voice_action is None
                                     and (self.last_action is None
                                          or stationary_clear or vehicle_only))
        eligible_ids = {id(item) for item in eligible}
        hazard_ids = {f"{item.get('class_name')}:{item.get('hazard_id') or item.get('event_id') or item.get('track_id')}"
                      for item in eligible}
        prediction["voice_diagnostics"] = {"raw_action": raw_action, "action": voice_action,
                                            "retained_hazards": retained,
                                            "crowded_until_s": self.crowded_until,
                                            "crowded_redirect_action": self.crowded_redirect_action,
                                            "pending_action": (self.pending_action[0]
                                                if isinstance(self.pending_action, tuple)
                                                else self.pending_action),
                                            "pending_steps": (self.pending_action[1]
                                                if isinstance(self.pending_action, tuple)
                                                else None),
                                            "objects": [{"detection_index": item.get("detection_index"),
                                                "hazard_id": item.get("hazard_id"),
                                                "voice_eligible": id(item) in eligible_ids,
                                                "rapid_approach_hazard": rapid_approach_hazard(item),
                                                "voice_suppressed_reason": item.get("voice_suppressed_reason"),
                                                "risk_suppressed_reason": item.get("risk_suppressed_reason")}
                                                for item in evidence.get("detections", [])]}
        if voice_action is None:
            if (stationary_clear or vehicle_only) and previous_action is not None:
                self.events.append((output_time_s, None))
            return None
        message = ACTION_MESSAGES[(voice_action, voice_steps)] if voice_steps else ACTION_MESSAGES[voice_action]
        last_direction_at = self.last_direction_voice_at.get(voice_action)
        direction_repeat = (voice_action in ("left", "right")
                            and last_direction_at is not None
                            and output_time_s - last_direction_at + 1e-6
                            >= SAME_DIRECTION_REPEAT_S)
        changed = ((voice_action, voice_steps) != (self.last_action, self.last_steps)
                   or redirected_crowded or direction_repeat)
        if changed:
            self.voice_event_id += 1
        prediction["voice_event"] = {"action": voice_action, "text": message[0],
                                     "steps": voice_steps,
                                     "source": "object",
                                     "event_id": self.voice_event_id,
                                     "hazard_ids": sorted(hazard_ids),
                                     "urgency": "emergency" if voice_action == "stop" else "walking"}
        if not changed:
            return None
        self.last_action = voice_action
        self.last_steps = voice_steps
        if voice_action in ("left", "right"):
            self.last_direction_voice_at[voice_action] = output_time_s
        if raw_action == "crowded":
            self.crowded_until = output_time_s + CROWDED_REDIRECT_S
            self.crowded_redirect_action = None
        prediction["voice_text"] = message[0]
        self.events.append((output_time_s, message[1]))
        prediction["voice_clip"] = message[1]
        return message
