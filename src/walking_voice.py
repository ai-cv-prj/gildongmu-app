"""
file_path: src/walking_voice.py

보행 위험 객체의 분포를 왼쪽·가운데·오른쪽으로 나눠 안전 행동을 안내한다.
객체 위치가 아니라 최종 이동 행동이 변경될 때만 새 음성을 기록한다.
"""

from math import isfinite

import numpy as np
from src.settings import load_audio_settings


GUIDANCE = load_audio_settings()["guidance"]
LEFT_MAX_RATIO = GUIDANCE["walking_left_max_ratio"]
RIGHT_MIN_RATIO = GUIDANCE["walking_right_min_ratio"]
CENTER_INTRUSION_RATIO = GUIDANCE["walking_center_intrusion_ratio"]
SIDE_INTRUSION_RATIO = GUIDANCE["walking_side_intrusion_ratio"]
VOICE_IMMEDIATE_OVERLAP_RATIO = GUIDANCE["walking_voice_immediate_overlap_ratio"]
DISTANCE_TIE_RATIO = GUIDANCE["walking_distance_tie_ratio"]
ACTION_MESSAGES = {
    "left": ("왼쪽으로 이동하세요.", "walking-move-left.mp3"),
    "straight": ("직진하세요.", "walking-straight.mp3"),
    "right": ("오른쪽으로 이동하세요.", "walking-move-right.mp3"),
    "stop": ("멈추세요.", "walking-stop.mp3"),
}
VEHICLE_CLASSES = frozenset({"car", "bus", "truck", "motorcycle"})


# 객체 바닥에서 횡단보도 픽셀 비율 계산
def crosswalk_contact_fraction(item, class_map, label_ids, shape, half_height):
    """객체 박스 바닥의 좁은 접촉 영역에서 횡단보도 픽셀 비율을 계산한다."""
    if (class_map is None or not label_ids or "crosswalk" not in label_ids
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
    return float(np.mean(patch == label_ids["crosswalk"]))


# 비초록 신호의 횡단보도 장애물 음성 제외 표시
def suppress_non_green_crosswalk_voice(prediction, signal, class_map, label_ids, shape, config):
    """선택 신호가 초록불이 아니며 횡단보도 위로 확인된 위험 객체를 음성에서 제외한다."""
    for item in prediction.get("detections", []):
        item.pop("voice_suppressed_reason", None)
        item.pop("crosswalk_contact_fraction", None)
    signal = signal or {}
    selected = signal.get("selected_detection_index")
    state = signal.get("signal_state")
    if (not config.get("non_green_obstacle_voice_suppression", True)
            or not isinstance(selected, int) or isinstance(selected, bool)
            or state not in ("red", "unknown")):
        return prediction
    threshold = config.get("non_green_obstacle_crosswalk_threshold", .20)
    half_height = config.get("non_green_obstacle_contact_half_height", .02)
    for item in prediction.get("detections", []):
        if item.get("alert_level", item.get("risk_level")) != "danger":
            continue
        fraction = crosswalk_contact_fraction(
            item, class_map, label_ids, shape, half_height)
        if fraction is not None:
            item["crosswalk_contact_fraction"] = fraction
        if fraction is not None and fraction >= threshold:
            item["voice_suppressed_reason"] = "non_green_signal_crosswalk_obstacle"
    return prediction


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
def guidance_items(prediction, level, crossing_active=False):
    """횡단 중에는 차량만 남겨 핑크 ROI 음성 조건의 객체를 반환한다."""
    warning = prediction.get("warning") or {}
    index = warning.get("detection_index")
    if (warning.get("level") != "danger" or not isinstance(index, int) or isinstance(index, bool)):
        return []
    return [item for item in prediction.get("detections", [])
            if item.get("alert_level", item.get("risk_level")) == level
            and item.get("warning_primary", True)
            and not item.get("voice_suppressed_reason")
            and (not crossing_active or item.get("class_name") in VEHICLE_CLASSES)
            and (level != "danger" or inside_voice_roi(item))]


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
def safer_side(dangers, cautions, image_width):
    """위험 거리, 주의 개수, 주의 거리 순서로 왼쪽과 오른쪽을 비교한다."""
    tie = image_width * DISTANCE_TIE_RATIO
    left_distance = nearest_distance(dangers, "left", image_width)
    right_distance = nearest_distance(dangers, "right", image_width)
    if abs(left_distance - right_distance) > tie:
        return "left" if left_distance > right_distance else "right"
    by_direction = {direction: [item for item in cautions
                                if direction in warning_directions(item, image_width)]
                    for direction in ("left", "right")}
    if len(by_direction["left"]) != len(by_direction["right"]):
        return "left" if len(by_direction["left"]) < len(by_direction["right"]) else "right"
    left_caution = nearest_distance(by_direction["left"], "left", image_width)
    right_caution = nearest_distance(by_direction["right"], "right", image_width)
    if left_caution == right_caution or abs(left_caution - right_caution) <= tie:
        return None
    return "left" if left_caution > right_caution else "right"


# 위험 분포를 최종 이동 행동으로 변환
def walking_action(prediction, image_width, crossing_active=False):
    """횡단 상태에 맞는 위험 분포와 좌우 안전도를 하나의 행동으로 바꾼다."""
    dangers = guidance_items(prediction, "danger", crossing_active)
    directions = {direction for item in dangers
                  for direction in warning_directions(item, image_width)}
    if not directions:
        return None
    if directions in ({"left"}, {"right"}, {"left", "right"}):
        return "straight"
    if directions == {"left", "center"}:
        return "right"
    if directions == {"center", "right"}:
        return "left"
    if directions == {"left", "center", "right"}:
        return "stop"
    if directions == {"center"}:
        cautions = guidance_items(prediction, "caution", crossing_active)
        return safer_side(dangers, cautions, image_width) or "stop"
    return "stop"


class WalkingVoice:
    """영상 프레임마다 최종 이동 행동이 달라질 때만 음성을 기록한다."""

    # 영상별 안내 상태 초기화
    def __init__(self):
        """마지막 안내 행동과 저장 영상용 오디오 이벤트를 빈 상태로 시작한다."""
        self.last_action = None
        self.events = []

    # 현재 프레임의 신규 위험 안내 기록
    def observe(self, prediction, image_width, output_time_s, crossing_active=False):
        """횡단 중 차량만으로 행동을 계산하고 변경된 음원만 예약한다."""
        prediction.pop("voice_text", None)
        prediction.pop("voice_clip", None)
        display_action = walking_action(prediction, image_width)
        voice_action = walking_action(prediction, image_width, crossing_active)
        prediction["last_action"] = display_action
        prediction["voice_action"] = voice_action
        if voice_action is None:
            if crossing_active and self.last_action is not None:
                self.events.append((output_time_s, None))
            self.last_action = None
            return None
        if voice_action == self.last_action:
            return None
        self.last_action = voice_action
        message = ACTION_MESSAGES[voice_action]
        prediction["voice_text"] = message[0]
        self.events.append((output_time_s, message[1]))
        prediction["voice_clip"] = message[1]
        return message
