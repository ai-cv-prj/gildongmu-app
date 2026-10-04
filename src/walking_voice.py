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
ACTION_MESSAGES = {
    "left": ("왼쪽 이동.", "walking-move-left.mp3"),
    "straight": ("서행하세요.", "walking-straight.mp3"),
    "right": ("오른쪽 이동.", "walking-move-right.mp3"),
    "stop": ("멈추세요.", "walking-stop.mp3"),
}
VEHICLE_CLASSES = frozenset({"car", "bus", "truck", "motorcycle"})
NON_GREEN_SIGNAL_STATES = frozenset({"red", "unknown"})
CROSSWALK_WAIT_STATUSES = frozenset({"used", "eligible"})


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


# 비초록 신호의 횡단보도·보행불가 장애물 음성 제외 표시
def suppress_non_green_crosswalk_voice(prediction, signal, class_map, label_ids, shape, config):
    """비초록 신호에서 횡단보도나 보행불가 영역 위 위험 객체를 음성에서 제외한다."""
    for item in prediction.get("detections", []):
        item.pop("voice_suppressed_reason", None)
        item.pop("crosswalk_contact_fraction", None)
        item.pop("nonwalkable_contact_fraction", None)
    signal = signal or {}
    selected = signal.get("selected_detection_index")
    state = signal.get("signal_state")
    if (not config.get("non_green_obstacle_voice_suppression", True)
            or not isinstance(selected, int) or isinstance(selected, bool)
            or state not in ("red", "unknown")):
        return prediction
    crosswalk_threshold = config.get("non_green_obstacle_crosswalk_threshold", .20)
    nonwalkable_threshold = config.get("non_green_obstacle_nonwalkable_threshold", .20)
    half_height = config.get("non_green_obstacle_contact_half_height", .02)
    for item in prediction.get("detections", []):
        if item.get("alert_level", item.get("risk_level")) != "danger":
            continue
        crosswalk_fraction = crosswalk_contact_fraction(
            item, class_map, label_ids, shape, half_height)
        nonwalkable_fraction = label_contact_fraction(
            item, class_map, label_ids, shape, half_height, "non_walkable")
        if crosswalk_fraction is not None:
            item["crosswalk_contact_fraction"] = crosswalk_fraction
        if nonwalkable_fraction is not None:
            item["nonwalkable_contact_fraction"] = nonwalkable_fraction
        if crosswalk_fraction is not None and crosswalk_fraction >= crosswalk_threshold:
            item["voice_suppressed_reason"] = "non_green_signal_crosswalk_obstacle"
        elif nonwalkable_fraction is not None and nonwalkable_fraction >= nonwalkable_threshold:
            item["voice_suppressed_reason"] = "non_green_signal_nonwalkable_obstacle"
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
    """근거리와 확인된 예측 위험을 음성에 연결한다."""
    warning = prediction.get("warning") or {}
    if warning.get("source") == "camera_view":
        return []
    return [item for item in prediction.get("detections", [])
            if item.get("alert_level", item.get("risk_level")) == level
            and item.get("warning_primary", True)
            and not item.get("voice_suppressed_reason")
            and (not crossing_active or item.get("class_name") in VEHICLE_CLASSES)
            and not item.get("risk_suppressed_reason")
            and (level != "danger" or inside_voice_roi(item) or rapid_approach_hazard(item))]


def rapid_approach_hazard(item):
    """안정적으로 확인된 빠른 접근·짧은 TTC 위험인지 반환한다."""
    reasons = item.get("reasons", [])
    return ((item.get("motion") or {}).get("quality") == "valid"
            and bool({"approaching_near_path", "short_ttc"}.intersection(reasons)))


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


# 횡단보도 대기 중 직진 음성 제외 조건 확인
def suppress_waiting_straight(signal, action):
    """빨간불 또는 확인 불가 신호에서 전방 횡단보도 직진 안내를 제외한다."""
    if action != "straight" or not isinstance(signal, dict):
        return False
    if signal.get("signal_state") not in NON_GREEN_SIGNAL_STATES:
        return False
    diagnostics = signal.get("crosswalk_diagnostics") or {}
    if diagnostics.get("eligible_count", 0) > 0:
        return True
    return any(item.get("crosswalk_status") in CROSSWALK_WAIT_STATUSES
               for item in signal.get("crosswalks", []))


class WalkingVoice:
    """영상 프레임마다 최종 이동 행동이 달라질 때만 음성을 기록한다."""

    # 영상별 안내 상태 초기화
    def __init__(self):
        """마지막 안내 행동과 저장 영상용 오디오 이벤트를 빈 상태로 시작한다."""
        self.last_action = None
        self.events = []
        self.pending_action = None
        self.pending_since = None
        self.clear_since = None
        self.previous_time = None
        self.epoch = None
        self.hazards = {}
        self.voice_event_id = 0
        self.change_confirm_s = GUIDANCE.get("walking_change_confirm_ms", 350) / 1000
        self.release_confirm_s = GUIDANCE.get("walking_release_confirm_ms", 600) / 1000
        self.missing_hold_s = GUIDANCE.get("walking_missing_hold_ms", 800) / 1000

    def _evidence(self, prediction, timestamp):
        """짧은 미검출을 안전한 경로라는 증거로 사용하지 않는다."""
        if (self.previous_time is not None and (timestamp <= self.previous_time
                or timestamp - self.previous_time > 2.0)
                or self.epoch is not None and self.epoch != prediction.get("state_epoch", 0)):
            self.hazards.clear()
            self.last_action = self.pending_action = self.pending_since = self.clear_since = None
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
    def observe(self, prediction, image_width, output_time_s, crossing_active=False, signal=None,
                crosswalk_status=None):
        """횡단 상태와 신호 대기 조건을 반영해 변경된 행동 음원만 예약한다."""
        prediction.pop("voice_text", None)
        prediction.pop("voice_clip", None)
        prediction.pop("voice_event", None)
        evidence, retained = self._evidence(prediction, output_time_s)
        previous_action = self.last_action
        display_action = walking_action(evidence, image_width)
        vehicle_only = crossing_active or crosswalk_status == "approach"
        raw_action = walking_action(evidence, image_width, vehicle_only)
        if (prediction.get("boarding") or {}).get("assumed_stationary") and raw_action is not None:
            # Keep the user stopped during input even when a side hazard would
            # ordinarily allow straight movement.
            raw_action = "stop"
        voice_action = raw_action
        if raw_action is None:
            self.pending_action = self.pending_since = None
            if self.clear_since is None:
                self.clear_since = output_time_s
            if output_time_s - self.clear_since + 1e-6 >= self.release_confirm_s or crossing_active:
                self.last_action = None
        else:
            self.clear_since = None
            if voice_action == "stop" or self.last_action is None:
                self.pending_action = self.pending_since = None
            elif voice_action != self.last_action:
                if self.pending_action != voice_action:
                    self.pending_action, self.pending_since = voice_action, output_time_s
                confirmation = self.release_confirm_s if voice_action == "straight" else self.change_confirm_s
                if output_time_s - self.pending_since + 1e-6 < confirmation:
                    voice_action = self.last_action
                else:
                    self.pending_action = self.pending_since = None
            else:
                self.pending_action = self.pending_since = None
        if suppress_waiting_straight(signal, voice_action):
            voice_action = None
        prediction["last_action"] = display_action
        prediction["voice_action"] = voice_action
        prediction["voice_clear"] = voice_action is None and self.last_action is None
        eligible = guidance_items(evidence, "danger", vehicle_only)
        eligible_ids = {id(item) for item in eligible}
        hazard_ids = {f"{item.get('class_name')}:{item.get('hazard_id') or item.get('event_id') or item.get('track_id')}"
                      for item in eligible}
        prediction["voice_diagnostics"] = {"raw_action": raw_action, "action": voice_action,
                                            "retained_hazards": retained,
                                            "pending_action": self.pending_action,
                                            "objects": [{"detection_index": item.get("detection_index"),
                                                "hazard_id": item.get("hazard_id"),
                                                "voice_eligible": id(item) in eligible_ids,
                                                "rapid_approach_hazard": rapid_approach_hazard(item),
                                                "voice_suppressed_reason": item.get("voice_suppressed_reason"),
                                                "risk_suppressed_reason": item.get("risk_suppressed_reason")}
                                                for item in evidence.get("detections", [])]}
        if voice_action is None:
            if vehicle_only and previous_action is not None:
                self.events.append((output_time_s, None))
            return None
        message = ACTION_MESSAGES[voice_action]
        changed = voice_action != self.last_action
        if changed:
            self.voice_event_id += 1
        prediction["voice_event"] = {"action": voice_action, "text": message[0],
                                     "source": "object",
                                     "event_id": self.voice_event_id,
                                     "hazard_ids": sorted(hazard_ids),
                                     "urgency": "emergency" if voice_action == "stop" else "walking"}
        if not changed:
            return None
        self.last_action = voice_action
        prediction["voice_text"] = message[0]
        self.events.append((output_time_s, message[1]))
        prediction["voice_clip"] = message[1]
        return message
