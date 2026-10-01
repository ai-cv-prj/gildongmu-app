"""
file_path: src/walking_voice.py

보행 장애물 위험을 test-app과 같은 방향별 음성 안내로 바꾼다.
같은 위험은 한 번만 안내하고 새 위험이 확인되면 안내를 갱신한다.
"""

from math import isfinite

import numpy as np
from src.settings import load_audio_settings


VEHICLE_CLASSES = {"car", "bus", "truck", "motorcycle"}
DIRECTION_NAMES = {"left": "왼쪽", "center": "가운데", "right": "오른쪽"}
WARNING_NAMES = {"person": "사람", "vehicle": "차량", "obstacle": "장애물"}
WALKING_CLEAR_SECONDS = load_audio_settings()["guidance"]["walking_clear_ms"] / 1000


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


# 신뢰할 수 있는 물체 이름을 음성용 범주로 묶기
def warning_category(item):
    """확정된 이름만 사람·차량으로 사용하고 나머지는 장애물로 안내한다."""
    name = item.get("display_label") if item.get("label_status") == "reliable" else None
    return "person" if name == "person" else "vehicle" if name in VEHICLE_CLASSES else "obstacle"


# 객체 중심의 화면 방향 판정
def warning_direction(item, image_width):
    """화면 가로의 왼쪽 40%, 가운데 20%, 오른쪽 40%를 구분한다."""
    try:
        box = item["xyxy"]
        ratio = (float(box[0]) + float(box[2])) / (2 * image_width)
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        return None
    if not isfinite(ratio):
        return None
    return "left" if ratio < .4 else "center" if ratio < .6 else "right"


# 위험 객체의 안정적인 식별자 선택
def voice_event_id(item):
    """위험 이벤트 ID, 추적 ID, 검출 순서 중 사용 가능한 값을 고른다."""
    for key in ("event_id", "track_id", "detection_index"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


# 대표 위험의 안내 범주 확인
def danger_voice_target(prediction):
    """화면의 대표 경고가 가리키는 위험 객체와 음성 범주를 반환한다."""
    selected = prediction.get("warning") or {}
    if selected.get("level") != "danger":
        return None
    index = selected.get("detection_index")
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    item = next((d for d in prediction.get("detections", [])
                 if d.get("detection_index") == index), None)
    if (item is None or item.get("alert_level", item.get("risk_level")) != "danger"
            or item.get("voice_suppressed_reason")):
        return None
    category = ("obstacle" if selected.get("source") in ("surface", "surface_object", "advisory")
                or item.get("semantic_path_overlap") else warning_category(item))
    return {"category": category, "event_id": voice_event_id(item)}


# 한 프레임의 안내 대상 수집
def danger_voice_targets(prediction, image_width):
    """대표 위험과 같은 장면의 기본 위험 객체를 방향과 ID로 정리한다."""
    warning = prediction.get("warning") or {}
    if warning.get("level") != "danger":
        return []
    selected = danger_voice_target(prediction)
    selected_index = warning.get("detection_index")
    if not isinstance(selected_index, int) or isinstance(selected_index, bool):
        return []
    targets, seen = [], set()
    for item in prediction.get("detections", []):
        if (item.get("alert_level", item.get("risk_level")) != "danger"
                or not item.get("warning_primary", True)
                or item.get("voice_suppressed_reason")):
            continue
        event_id = voice_event_id(item)
        direction = warning_direction(item, image_width)
        if event_id is None or direction is None or event_id in seen:
            continue
        seen.add(event_id)
        category = (selected["category"] if selected is not None
                    and event_id == selected["event_id"] else warning_category(item))
        targets.append({"event_id": event_id, "direction": direction, "category": category})
    return targets


# 위험 수와 방향에 맞는 음원 선택
def danger_voice_message(targets):
    """test-app의 문장과 대응하는 MP3 파일명을 반환한다."""
    if not targets:
        return None
    directions = {target["direction"] for target in targets}
    if len(targets) > 1:
        if len(directions) > 1:
            return "여러 방향에 장애물.", "danger-multiple-directions.mp3"
        direction = targets[0]["direction"]
        return f"{DIRECTION_NAMES[direction]}에 여러 장애물.", f"danger-{direction}-multiple.mp3"
    target = targets[0]
    direction, category = target["direction"], target["category"]
    return (f"{DIRECTION_NAMES[direction]}에 {WARNING_NAMES[category]}.",
            f"danger-{direction}-{category}.mp3")


class WalkingVoice:
    """영상 프레임마다 새 위험만 골라 재생 시점과 음원을 기록한다."""

    # 영상별 안내 상태 초기화
    def __init__(self):
        """이미 알린 객체와 오디오 이벤트를 빈 상태로 시작한다."""
        self.announced_ids = set()
        self.last_danger_at = None
        self.state_epoch = None
        self.events = []

    # 현재 프레임의 신규 위험 안내 기록
    def observe(self, prediction, image_width, output_time_s):
        """대표 경고가 위험일 때 새 객체가 있으면 안내 음원을 예약한다."""
        epoch = prediction.get("state_epoch")
        if epoch != self.state_epoch or (self.announced_ids and self.last_danger_at is not None
                                         and output_time_s - self.last_danger_at >= WALKING_CLEAR_SECONDS):
            self.announced_ids.clear()
        self.state_epoch = epoch
        targets = danger_voice_targets(prediction, image_width)
        message = danger_voice_message(targets)
        if message is None:
            return None
        self.last_danger_at = output_time_s
        ids = {target["event_id"] for target in targets}
        prediction["voice_event_ids"] = sorted(ids)
        prediction["voice_text"] = message[0]
        if ids.issubset(self.announced_ids):
            return None
        self.announced_ids.update(ids)
        self.events.append((output_time_s, message[1]))
        prediction["voice_clip"] = message[1]
        return message
