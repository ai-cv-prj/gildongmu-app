"""
file_path: src/crosswalk_safety.py

횡단보도 마스크와 검출 결과를 결합해 횡단 상태와 좌우 이탈을 판단한다.
정규화된 영상 좌표만 사용하며 실제 거리나 횡단 안전을 보장하지 않는다.
"""

from copy import deepcopy
import math

import cv2
import numpy as np


DEFAULT_CROSSWALK_SAFETY = {
    "enabled": True,
    "near_zone_top": 0.72,
    "foot_x": 0.50,
    "foot_y": 0.92,
    "foot_half_width": 0.04,
    "foot_half_height": 0.04,
    "roi_left": 0.10,
    "roi_right": 0.90,
    "roi_top": 0.90,
    "roi_bottom": 1.00,
    "roi_crosswalk_threshold": 0.05,
    "roi_occlusion_threshold": 0.08,
    "roi_exit_confirm_s": 0.30,
    "entry_confirm_s": 0.50,
    "edge_confirm_s": 0.20,
    "exit_confirm_s": 0.25,
    "return_confirm_s": 0.40,
    "finish_confirm_s": 0.40,
    "outside_finish_confirm_s": 0.80,
    "uncertainty_hold_s": 1.50,
    "max_gap_s": 2.00,
    "edge_margin": 0.08,
    "exit_margin": 0.03,
    "max_boundary_shift": 0.12,
    "boundary_smooth_s": 0.30,
    "min_mask_area": 0.006,
    "min_row_width": 0.10,
    "min_valid_rows": 8,
    "finish_walkable_fraction": 0.60,
    "outside_finish_walkable_fraction": 0.80,
    "entry_box_bottom": 0.70,
    "non_green_obstacle_voice_suppression": True,
    "non_green_obstacle_crosswalk_threshold": 0.20,
    "non_green_obstacle_contact_half_height": 0.02,
}


# 횡단보도 안전 설정 검증
def crosswalk_safety_config(value=None):
    """알 수 없는 키와 범위를 벗어난 값을 거부하고 전체 설정을 반환한다."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CROSSWALK_SAFETY):
        raise ValueError("crosswalk_safety: unknown keys or invalid mapping")
    cfg = {**deepcopy(DEFAULT_CROSSWALK_SAFETY), **deepcopy(value)}
    for key in ("enabled", "non_green_obstacle_voice_suppression"):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"crosswalk_safety.{key} must be boolean")
    unit_keys = (
        "near_zone_top", "foot_x", "foot_y", "foot_half_width", "foot_half_height",
        "edge_margin", "exit_margin", "max_boundary_shift", "min_mask_area",
        "min_row_width", "finish_walkable_fraction", "entry_box_bottom", "roi_left",
        "roi_right", "roi_top", "roi_bottom", "roi_crosswalk_threshold",
        "roi_occlusion_threshold", "outside_finish_walkable_fraction",
        "non_green_obstacle_crosswalk_threshold", "non_green_obstacle_contact_half_height",
    )
    for key in unit_keys:
        item = cfg[key]
        if (isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item) or item <= 0 or item > 1):
            raise ValueError(f"crosswalk_safety.{key} must be in (0,1]")
    time_keys = (
        "entry_confirm_s", "edge_confirm_s", "exit_confirm_s", "return_confirm_s",
        "finish_confirm_s", "uncertainty_hold_s", "max_gap_s", "boundary_smooth_s",
        "roi_exit_confirm_s", "outside_finish_confirm_s",
    )
    for key in time_keys:
        item = cfg[key]
        if (isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item) or item <= 0):
            raise ValueError(f"crosswalk_safety.{key} must be positive")
    if (isinstance(cfg["min_valid_rows"], bool) or not isinstance(cfg["min_valid_rows"], int)
            or cfg["min_valid_rows"] < 3):
        raise ValueError("crosswalk_safety.min_valid_rows must be an integer of at least 3")
    if cfg["exit_margin"] >= cfg["edge_margin"]:
        raise ValueError("crosswalk exit margin must be below edge margin")
    if cfg["roi_left"] >= cfg["roi_right"] or cfg["roi_top"] >= cfg["roi_bottom"]:
        raise ValueError("crosswalk ROI bounds must increase from left/top to right/bottom")
    return cfg


# 사용자 위치 주변의 보행 의미 비율 계산
def foot_semantics(class_map, label_ids, shape, cfg):
    """화면 아래 중앙의 가상 발 영역에서 보행가능·횡단보도 비율을 계산한다."""
    if class_map is None or not label_ids or class_map.shape != tuple(shape[:2]):
        return {"walkable_fraction": None, "crosswalk_fraction": None}
    height, width = shape[:2]
    x1 = max(0, round((cfg["foot_x"] - cfg["foot_half_width"]) * width))
    x2 = min(width, round((cfg["foot_x"] + cfg["foot_half_width"]) * width))
    y1 = max(0, round((cfg["foot_y"] - cfg["foot_half_height"]) * height))
    y2 = min(height, round((cfg["foot_y"] + cfg["foot_half_height"]) * height))
    patch = class_map[y1:y2, x1:x2]
    if patch.size == 0:
        return {"walkable_fraction": None, "crosswalk_fraction": None}
    walkable_id = label_ids.get("walkable")
    crosswalk_id = label_ids.get("crosswalk")
    return {
        "walkable_fraction": (float(np.mean(patch == walkable_id))
                              if walkable_id is not None else None),
        "crosswalk_fraction": (float(np.mean(patch == crosswalk_id))
                               if crosswalk_id is not None else None),
    }


# 하단 횡단보도 ROI의 의미 비율 계산
def crosswalk_roi_semantics(class_map, label_ids, shape, cfg):
    """화면 하단의 넓은 ROI에서 횡단보도와 보행가능영역 비율을 계산한다."""
    roi = {
        "left": cfg["roi_left"], "right": cfg["roi_right"],
        "top": cfg["roi_top"], "bottom": cfg["roi_bottom"],
        "crosswalk_fraction": None, "walkable_fraction": None,
    }
    if class_map is None or not label_ids or class_map.shape != tuple(shape[:2]):
        return roi
    height, width = shape[:2]
    x1, x2 = round(cfg["roi_left"] * width), round(cfg["roi_right"] * width)
    y1, y2 = round(cfg["roi_top"] * height), round(cfg["roi_bottom"] * height)
    patch = class_map[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
    if patch.size == 0:
        return roi
    crosswalk_id = label_ids.get("crosswalk")
    walkable_id = label_ids.get("walkable")
    roi["crosswalk_fraction"] = (float(np.mean(patch == crosswalk_id))
                                 if crosswalk_id is not None else None)
    roi["walkable_fraction"] = (float(np.mean(patch == walkable_id))
                                if walkable_id is not None else None)
    return roi


# 횡단보도 ROI를 가린 객체 비율 계산
def crosswalk_roi_occlusion(detections, shape, cfg):
    """현재 객체 박스가 하단 횡단보도 ROI를 가리는 최대 비율을 반환한다."""
    height, width = shape[:2]
    roi_left, roi_right = cfg["roi_left"] * width, cfg["roi_right"] * width
    roi_top, roi_bottom = cfg["roi_top"] * height, cfg["roi_bottom"] * height
    roi_area = (roi_right - roi_left) * (roi_bottom - roi_top)
    if roi_area <= 0:
        return 0.0
    largest = 0.0
    for item in detections or []:
        if item.get("class_name") == "traffic_light" or item.get("observed") is False:
            continue
        box = item.get("xyxy")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            x1, y1, x2, y2 = map(float, box)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
            continue
        overlap_width = max(0.0, min(x2, roi_right) - max(x1, roi_left))
        overlap_height = max(0.0, min(y2, roi_bottom) - max(y1, roi_top))
        largest = max(largest, overlap_width * overlap_height / roi_area)
    return min(1.0, largest)


# 신호등 파이프라인의 가까운 횡단보도 선택
def eligible_crosswalk(signal, shape, cfg):
    """연결 가능한 횡단보도 중 화면 아래에 가장 가까운 후보를 반환한다."""
    height = shape[0]
    candidates = []
    for item in (signal or {}).get("crosswalks", []):
        status = item.get("crosswalk_status")
        if status not in (None, "used", "eligible"):
            continue
        box = item.get("xyxy")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            bottom = float(item.get("bottom_ratio", float(box[3]) / height))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if math.isfinite(bottom) and bottom >= cfg["entry_box_bottom"]:
            candidates.append((bottom, item))
    return max(candidates, key=lambda value: value[0])[1] if candidates else None


# 핑크 마스크에서 횡단보도 좌우 경계 추정
def crosswalk_geometry(class_map, label_ids, shape, cfg):
    """줄무늬 사이를 연결한 뒤 가상 발 높이의 좌우 경계를 정규화해 반환한다."""
    if (class_map is None or not label_ids or "crosswalk" not in label_ids
            or class_map.shape != tuple(shape[:2])):
        return None
    height, width = shape[:2]
    raw = (class_map == label_ids["crosswalk"]).astype(np.uint8)
    if float(raw.mean()) < cfg["min_mask_area"]:
        return None
    kernel_width = max(3, round(width * 0.01))
    kernel_height = max(3, round(height * 0.05))
    kernel = np.ones((kernel_height, kernel_width), np.uint8)
    mask = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, kernel)
    rows, lefts, rights = [], [], []
    minimum_width = max(3, round(width * cfg["min_row_width"]))
    for y in range(height):
        xs = np.flatnonzero(mask[y])
        if len(xs) < minimum_width:
            continue
        rows.append(y / max(1, height - 1))
        lefts.append(float(np.quantile(xs, 0.03)) / max(1, width - 1))
        rights.append(float(np.quantile(xs, 0.97)) / max(1, width - 1))
    if len(rows) < cfg["min_valid_rows"]:
        return None
    left_fit = np.polyfit(rows, lefts, 1)
    right_fit = np.polyfit(rows, rights, 1)
    foot_y = cfg["foot_y"]
    left = float(np.polyval(left_fit, foot_y))
    right = float(np.polyval(right_fit, foot_y))
    left, right = max(0.0, min(1.0, left)), max(0.0, min(1.0, right))
    if right - left < cfg["min_row_width"]:
        return None
    return {
        "left_x": left,
        "right_x": right,
        "foot_x": cfg["foot_x"],
        "foot_y": foot_y,
        "mask_top_y": float(min(rows)),
        "mask_bottom_y": float(max(rows)),
        "mask_fraction": float(raw.mean()),
        "row_support": float(len(rows) / height),
        "near": bool(max(rows) >= cfg["near_zone_top"]),
    }


class CrosswalkSafetyEngine:
    """횡단 진입을 먼저 확인한 뒤 좌우 경계 접근과 이탈을 시간 기준으로 판정한다."""

    # 횡단보도 안전 판정기 초기화
    def __init__(self, config=None):
        """검증된 설정을 저장하고 시간·경계 이력을 초기화한다."""
        self.config = crosswalk_safety_config(config)
        self.reset()

    # 영상 또는 실시간 세션 상태 초기화
    def reset(self):
        """이전 횡단 상태와 확인 대기 시간을 모두 지운다."""
        self.phase = "search"
        self.crossing_active = False
        self.pending_kind = None
        self.pending_since = None
        self.last_timestamp = None
        self.last_valid_at = None
        self.left_x = None
        self.right_x = None
        self.last_edge_direction = None
        self.event_id = 0

    # 연속 시간 조건 확인
    def _confirmed(self, kind, timestamp, duration):
        """같은 후보 상태가 지정 시간 이상 유지됐는지 반환한다."""
        if self.pending_kind != kind:
            self.pending_kind = kind
            self.pending_since = timestamp
            return False
        return timestamp - self.pending_since + 1e-9 >= duration

    # 대기 중인 상태 전환 취소
    def _clear_pending(self):
        """현재 조건과 맞지 않는 이전 상태 전환 후보를 지운다."""
        self.pending_kind = None
        self.pending_since = None

    # 상태 전환과 새 이벤트 번호 기록
    def _transition(self, phase):
        """상태가 실제로 바뀔 때만 이벤트 번호를 증가시킨다."""
        changed = phase != self.phase
        if changed:
            self.phase = phase
            self.event_id += 1
        return changed

    # 프레임별 횡단보도 안전 상태 갱신
    def update(self, class_map, label_ids, shape, signal, timestamp, camera_stable=True,
               detections=None):
        """같은 프레임의 마스크·횡단보도 검출로 상태와 안내 이벤트를 반환한다."""
        result = {
            "enabled": self.config["enabled"], "status": "disabled", "crossing_active": False,
            "direction": None, "voice_text": None, "voice_clip": None, "repeat": False,
            "vibration": None, "event_id": self.event_id, "reasons": [], "geometry": None,
            "stale_after_ms": round(self.config["uncertainty_hold_s"] * 1000),
        }
        if not self.config["enabled"]:
            return result
        if (self.last_timestamp is not None and
                (timestamp <= self.last_timestamp or timestamp - self.last_timestamp > self.config["max_gap_s"])):
            self.reset()
        previous_timestamp = self.last_timestamp
        self.last_timestamp = timestamp
        semantics = foot_semantics(class_map, label_ids, shape, self.config)
        result["foot_semantics"] = semantics
        roi = crosswalk_roi_semantics(class_map, label_ids, shape, self.config)
        roi["occlusion_fraction"] = crosswalk_roi_occlusion(
            detections, shape, self.config)
        result["crosswalk_roi"] = roi
        geometry = crosswalk_geometry(class_map, label_ids, shape, self.config)
        candidate = eligible_crosswalk(signal, shape, self.config)
        if not camera_stable:
            result.update(status="uncertain", crossing_active=self.crossing_active,
                          reasons=["camera_unstable"])
            self._clear_pending()
            return result
        if geometry is not None:
            if self.left_x is not None and self.right_x is not None and self.crossing_active:
                shift = max(abs(geometry["left_x"] - self.left_x),
                            abs(geometry["right_x"] - self.right_x))
                if shift > self.config["max_boundary_shift"]:
                    result.update(status="uncertain", crossing_active=True,
                                  reasons=["boundary_jump"], geometry=geometry)
                    self._clear_pending()
                    return result
                elapsed = max(0.0, timestamp - previous_timestamp) if previous_timestamp is not None else 0.0
                alpha = min(1.0, elapsed / self.config["boundary_smooth_s"])
                geometry["left_x"] = self.left_x + alpha * (geometry["left_x"] - self.left_x)
                geometry["right_x"] = self.right_x + alpha * (geometry["right_x"] - self.right_x)
            self.left_x, self.right_x = geometry["left_x"], geometry["right_x"]
            self.last_valid_at = timestamp
        result["geometry"] = geometry

        # 횡단 전에는 가까운 검출과 중앙 마스크가 함께 확인되어야 한다.
        if not self.crossing_active:
            entry = bool(geometry and geometry["near"] and candidate is not None
                         and geometry["left_x"] <= self.config["foot_x"] <= geometry["right_x"])
            if entry:
                self._transition("approach")
                if self._confirmed("entry", timestamp, self.config["entry_confirm_s"]):
                    self.crossing_active = True
                    self._transition("crossing")
                    self._clear_pending()
            else:
                self._clear_pending()
                self._transition("approach" if geometry is not None or candidate is not None else "search")
            result.update(status=self.phase, crossing_active=self.crossing_active,
                          event_id=self.event_id,
                          reasons=["entry_confirmed"] if self.crossing_active else [])
            return result

        # 횡단보도 끝에서 보행가능영역으로 이어지면 정상 도착으로 처리한다.
        destination_visible = geometry is None or not geometry["near"]
        finish = destination_visible and (
            semantics["walkable_fraction"] is not None
            and semantics["walkable_fraction"] >= self.config["finish_walkable_fraction"]
        )
        outside_phase = self.phase in ("outside_left", "outside_right", "outside_unknown")
        outside_finish = outside_phase and destination_visible and (
            semantics["walkable_fraction"] is not None
            and semantics["walkable_fraction"] >= self.config["outside_finish_walkable_fraction"]
            and roi["walkable_fraction"] is not None
            and roi["walkable_fraction"] >= self.config["outside_finish_walkable_fraction"]
        )
        if (finish and not outside_phase) or outside_finish:
            duration = (self.config["outside_finish_confirm_s"] if outside_finish
                        else self.config["finish_confirm_s"])
            kind = "outside_finish" if outside_finish else "finish"
            if self._confirmed(kind, timestamp, duration):
                self.crossing_active = False
                self._transition("finished")
                self._clear_pending()
            result.update(status=self.phase, crossing_active=self.crossing_active,
                          event_id=self.event_id, reasons=["walkable_destination"])
            if outside_finish and self.crossing_active:
                move = ("right" if self.phase == "outside_left" else
                        "left" if self.phase == "outside_right" else None)
                korean = "오른쪽" if move == "right" else "왼쪽" if move == "left" else None
                text = (f"횡단보도 이탈! {korean}으로 이동하세요!"
                        if korean else "횡단보도 이탈!")
                result.update(direction=move, repeat=True, vibration="danger",
                              voice_text=text,
                              voice_clip=f"crosswalk-exit-{move or 'unknown'}.mp3")
            return result
        roi_missing = (geometry is None and roi["crosswalk_fraction"] is not None
                       and roi["crosswalk_fraction"] < self.config["roi_crosswalk_threshold"])
        if roi_missing:
            if roi["occlusion_fraction"] >= self.config["roi_occlusion_threshold"]:
                self._clear_pending()
                result.update(status="uncertain", crossing_active=True,
                              event_id=self.event_id, reasons=["crosswalk_roi_occluded"])
                return result
            direction = self.last_edge_direction
            exit_phase = (self.phase if self.phase in ("outside_left", "outside_right") else
                          "outside_left" if direction == "right" else
                          "outside_right" if direction == "left" else "outside_unknown")
            if self.phase == exit_phase or self._confirmed(
                    f"roi_{exit_phase}", timestamp, self.config["roi_exit_confirm_s"]):
                self._transition(exit_phase)
                self._clear_pending()
            result.update(status=self.phase, crossing_active=True, event_id=self.event_id,
                          reasons=["crosswalk_roi_missing"])
            if self.phase in ("outside_left", "outside_right", "outside_unknown"):
                move = ("right" if self.phase == "outside_left" else
                        "left" if self.phase == "outside_right" else None)
                korean = "오른쪽" if move == "right" else "왼쪽" if move == "left" else None
                text = (f"횡단보도 이탈! {korean}으로 이동하세요!"
                        if korean else "횡단보도 이탈!")
                result.update(direction=move, repeat=True, vibration="danger", voice_text=text,
                              voice_clip=f"crosswalk-exit-{move or 'unknown'}.mp3")
            return result
        if geometry is None:
            self._clear_pending()
            result.update(status="uncertain", crossing_active=True, event_id=self.event_id,
                          reasons=["crosswalk_boundary_unavailable"])
            return result

        foot_x = self.config["foot_x"]
        left_gap = foot_x - geometry["left_x"]
        right_gap = geometry["right_x"] - foot_x
        geometry.update(left_gap=left_gap, right_gap=right_gap)
        outside = ("outside_left" if foot_x < geometry["left_x"] - self.config["exit_margin"]
                   else "outside_right" if foot_x > geometry["right_x"] + self.config["exit_margin"]
                   else None)
        if outside is not None:
            if self.phase == outside or self._confirmed(outside, timestamp, self.config["exit_confirm_s"]):
                self._transition(outside)
                self._clear_pending()
            result.update(status=self.phase, crossing_active=True, event_id=self.event_id,
                          reasons=["lateral_boundary_exit"])
            if self.phase in ("outside_left", "outside_right"):
                move = "right" if self.phase == "outside_left" else "left"
                korean = "오른쪽" if move == "right" else "왼쪽"
                result.update(direction=move, repeat=True, vibration="danger",
                              voice_text=f"횡단보도 이탈! {korean}으로 이동하세요!",
                              voice_clip=f"crosswalk-exit-{move}.mp3")
            return result

        # 이탈 중에는 안쪽 복귀를 확인할 때까지 기존 방향 안내를 유지한다.
        if self.phase in ("outside_left", "outside_right", "outside_unknown"):
            if not self._confirmed("return", timestamp, self.config["return_confirm_s"]):
                move = ("right" if self.phase == "outside_left" else
                        "left" if self.phase == "outside_right" else None)
                korean = "오른쪽" if move == "right" else "왼쪽" if move == "left" else None
                text = (f"횡단보도 이탈! {korean}으로 이동하세요!"
                        if korean else "횡단보도 이탈!")
                result.update(status=self.phase, crossing_active=True, direction=move,
                              voice_text=text,
                              voice_clip=f"crosswalk-exit-{move or 'unknown'}.mp3", repeat=True,
                              vibration="danger", event_id=self.event_id,
                              reasons=["return_confirming"])
                return result
            self._transition("crossing")
            self._clear_pending()

        edge_direction = ("right" if left_gap <= self.config["edge_margin"]
                          else "left" if right_gap <= self.config["edge_margin"] else None)
        if edge_direction is not None:
            self.last_edge_direction = edge_direction
            edge_kind = f"edge_{edge_direction}"
            if self.phase == "edge" or self._confirmed(edge_kind, timestamp, self.config["edge_confirm_s"]):
                changed = self._transition("edge")
                self._clear_pending()
                result["vibration"] = "edge" if changed else None
            result.update(status=self.phase, crossing_active=True, direction=edge_direction,
                          event_id=self.event_id, reasons=["lateral_edge_near"])
            return result

        self._clear_pending()
        self.last_edge_direction = None
        self._transition("crossing")
        result.update(status="crossing", crossing_active=True, event_id=self.event_id,
                      reasons=["inside_crosswalk"])
        return result
