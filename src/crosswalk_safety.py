"""
file_path: src/crosswalk_safety.py

횡단보도 마스크로 진입 정렬과 횡단 상태 및 좌우 이탈을 판단한다.
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
    "exit_roi_left": 0.02,
    "exit_roi_right": 0.98,
    "exit_roi_top": 0.70,
    "exit_roi_bottom": 1.00,
    "exit_roi_crosswalk_threshold": 0.05,
    "entry_confirm_s": 0.50,
    "geometry_entry_confirm_s": 0.30,
    "entry_occlusion_hold_s": 0.50,
    "edge_confirm_s": 0.20,
    "exit_confirm_s": 0.25,
    "exit_candidate_hold_s": 0.50,
    "return_confirm_s": 0.40,
    "finish_confirm_s": 0.40,
    "outside_finish_confirm_s": 0.80,
    "uncertainty_hold_s": 1.50,
    "max_gap_s": 2.00,
    "edge_margin": 0.08,
    "exit_margin": 0.03,
    "max_boundary_shift": 0.12,
    "boundary_hold_s": 0.50,
    "boundary_candidate_confirm_s": 0.30,
    "boundary_smooth_s": 0.30,
    "min_mask_area": 0.006,
    "min_row_width": 0.10,
    "min_valid_rows": 8,
    "finish_walkable_fraction": 0.60,
    "outside_finish_walkable_fraction": 0.80,
    "non_green_obstacle_voice_suppression": True,
    "non_green_obstacle_crosswalk_threshold": 0.20,
    "non_green_obstacle_crosswalk_contact_half_height": 0.02,
    "obstacle_surrounding_voice_suppression": True,
    "obstacle_surrounding_walkable_threshold": 0.30,
    "obstacle_surrounding_partial_walkable_threshold": 0.05,
    "obstacle_surrounding_side_width_ratio": 0.15,
    "obstacle_surrounding_side_height_ratio": 0.20,
    "obstacle_surrounding_bottom_height_ratio": 0.10,
    "obstacle_surrounding_max_side_width_ratio": 0.02,
    "obstacle_surrounding_max_bottom_height_ratio": 0.02,
    "obstacle_surrounding_min_region_pixels": 4,
    "walking_direction_edge_width_ratio": 0.20,
    "walking_direction_min_walkable_ratio": 0.30,
}


# 횡단보도 판정용 촬영 안정 상태 확인
def crosswalk_camera_stable(prediction):
    """순간 움직임값 대신 시간 안정화된 촬영 상태가 clear인지 확인한다."""
    camera_view = (prediction or {}).get("camera_view") or {}
    return camera_view.get("status", "clear") == "clear"


# 횡단보도 안전 설정 검증
def crosswalk_safety_config(value=None):
    """알 수 없는 키와 범위를 벗어난 값을 거부하고 전체 설정을 반환한다."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CROSSWALK_SAFETY):
        raise ValueError("crosswalk_safety: unknown keys or invalid mapping")
    cfg = {**deepcopy(DEFAULT_CROSSWALK_SAFETY), **deepcopy(value)}
    for key in (
        "enabled", "non_green_obstacle_voice_suppression",
        "obstacle_surrounding_voice_suppression",
    ):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"crosswalk_safety.{key} must be boolean")
    unit_keys = (
        "near_zone_top", "foot_x", "foot_y", "foot_half_width", "foot_half_height",
        "edge_margin", "exit_margin", "max_boundary_shift", "min_mask_area",
        "min_row_width", "finish_walkable_fraction", "roi_left",
        "roi_right", "roi_top", "roi_bottom", "roi_crosswalk_threshold",
        "roi_occlusion_threshold", "outside_finish_walkable_fraction",
        "exit_roi_left", "exit_roi_right", "exit_roi_top", "exit_roi_bottom",
        "exit_roi_crosswalk_threshold",
        "non_green_obstacle_crosswalk_threshold",
        "non_green_obstacle_crosswalk_contact_half_height",
        "obstacle_surrounding_walkable_threshold",
        "obstacle_surrounding_partial_walkable_threshold",
        "obstacle_surrounding_side_width_ratio",
        "obstacle_surrounding_side_height_ratio",
        "obstacle_surrounding_bottom_height_ratio",
        "obstacle_surrounding_max_side_width_ratio",
        "obstacle_surrounding_max_bottom_height_ratio",
        "walking_direction_edge_width_ratio",
        "walking_direction_min_walkable_ratio",
    )
    for key in unit_keys:
        item = cfg[key]
        if (isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item) or item <= 0 or item > 1):
            raise ValueError(f"crosswalk_safety.{key} must be in (0,1]")
    time_keys = (
        "entry_confirm_s", "edge_confirm_s", "exit_confirm_s", "return_confirm_s",
        "finish_confirm_s", "uncertainty_hold_s", "max_gap_s", "boundary_smooth_s",
        "outside_finish_confirm_s", "geometry_entry_confirm_s", "entry_occlusion_hold_s",
        "exit_candidate_hold_s", "boundary_hold_s", "boundary_candidate_confirm_s",
    )
    for key in time_keys:
        item = cfg[key]
        if (isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item) or item <= 0):
            raise ValueError(f"crosswalk_safety.{key} must be positive")
    if (isinstance(cfg["min_valid_rows"], bool) or not isinstance(cfg["min_valid_rows"], int)
            or cfg["min_valid_rows"] < 3):
        raise ValueError("crosswalk_safety.min_valid_rows must be an integer of at least 3")
    pixels = cfg["obstacle_surrounding_min_region_pixels"]
    if isinstance(pixels, bool) or not isinstance(pixels, int) or pixels <= 0:
        raise ValueError(
            "crosswalk_safety.obstacle_surrounding_min_region_pixels must be a positive integer")
    if cfg["exit_margin"] >= cfg["edge_margin"]:
        raise ValueError("crosswalk exit margin must be below edge margin")
    if cfg["roi_left"] >= cfg["roi_right"] or cfg["roi_top"] >= cfg["roi_bottom"]:
        raise ValueError("crosswalk ROI bounds must increase from left/top to right/bottom")
    if (cfg["exit_roi_left"] >= cfg["exit_roi_right"]
            or cfg["exit_roi_top"] >= cfg["exit_roi_bottom"]):
        raise ValueError("crosswalk exit ROI bounds must increase from left/top to right/bottom")
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


# 분홍색 즉시위험 ROI의 횡단보도 비율 계산
def crosswalk_exit_roi_semantics(class_map, label_ids, shape, cfg):
    """화면 하단의 분홍색 ROI에서 횡단보도 마스크 비율을 계산한다."""
    roi = {
        "left": cfg["exit_roi_left"], "right": cfg["exit_roi_right"],
        "top": cfg["exit_roi_top"], "bottom": cfg["exit_roi_bottom"],
        "crosswalk_fraction": None,
    }
    if class_map is None or not label_ids or class_map.shape != tuple(shape[:2]):
        return roi
    height, width = shape[:2]
    x1 = round(cfg["exit_roi_left"] * width)
    x2 = round(cfg["exit_roi_right"] * width)
    y1 = round(cfg["exit_roi_top"] * height)
    y2 = round(cfg["exit_roi_bottom"] * height)
    patch = class_map[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
    if patch.size == 0:
        return roi
    crosswalk_id = label_ids.get("crosswalk")
    roi["crosswalk_fraction"] = (float(np.mean(patch == crosswalk_id))
                                 if crosswalk_id is not None else None)
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
        self.pending_last_seen = None
        self.last_timestamp = None
        self.left_x = None
        self.right_x = None
        self.last_boundary_at = None
        self.boundary_candidate_since = None
        self.boundary_candidate_left = None
        self.boundary_candidate_right = None
        self.roi_crosswalk_since = None
        self.roi_crosswalk_last_seen = None
        self.entry_geometry_since = None
        self.entry_left_x = None
        self.entry_right_x = None
        self.event_id = 0

    # 연속 시간 조건 확인
    def _confirmed(self, kind, timestamp, duration):
        """같은 후보 상태가 지정 시간 이상 유지됐는지 반환한다."""
        if self.pending_kind != kind:
            self.pending_kind = kind
            self.pending_since = timestamp
            self.pending_last_seen = timestamp
            return False
        self.pending_last_seen = timestamp
        return timestamp - self.pending_since + 1e-9 >= duration

    # 대기 중인 상태 전환 취소
    def _clear_pending(self):
        """현재 조건과 맞지 않는 이전 상태 전환 후보를 지운다."""
        self.pending_kind = None
        self.pending_since = None
        self.pending_last_seen = None

    # 크게 이동한 새 경계 후보 제거
    def _clear_boundary_candidate(self):
        """확정되거나 불연속적인 횡단보도 경계 후보를 초기화한다."""
        self.boundary_candidate_since = None
        self.boundary_candidate_left = None
        self.boundary_candidate_right = None

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
        """같은 프레임의 마스크로 상태와 안내 이벤트를 반환한다."""
        result = {
            "enabled": self.config["enabled"], "status": "disabled", "crossing_active": False,
            "direction": None, "voice_text": None, "voice_clip": None, "repeat": False,
            "event_id": self.event_id, "reasons": [], "geometry": None,
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
        exit_roi = crosswalk_exit_roi_semantics(class_map, label_ids, shape, self.config)
        result["exit_crosswalk_roi"] = exit_roi
        geometry = crosswalk_geometry(class_map, label_ids, shape, self.config)
        if not camera_stable:
            result.update(status="uncertain", crossing_active=self.crossing_active,
                          reasons=["camera_unstable"])
            self._clear_pending()
            self._clear_boundary_candidate()
            return result
        if geometry is not None:
            promoted_boundary = False
            if self.left_x is not None and self.right_x is not None and self.crossing_active:
                shift = max(abs(geometry["left_x"] - self.left_x),
                            abs(geometry["right_x"] - self.right_x))
                if shift > self.config["max_boundary_shift"]:
                    candidate_stable = bool(
                        self.boundary_candidate_left is None
                        or self.boundary_candidate_right is None
                        or max(abs(geometry["left_x"] - self.boundary_candidate_left),
                               abs(geometry["right_x"] - self.boundary_candidate_right))
                        <= self.config["max_boundary_shift"]
                    )
                    if self.boundary_candidate_since is None or not candidate_stable:
                        self.boundary_candidate_since = timestamp
                    self.boundary_candidate_left = geometry["left_x"]
                    self.boundary_candidate_right = geometry["right_x"]
                    candidate_confirmed = bool(
                        timestamp - self.boundary_candidate_since + 1e-9
                        >= self.config["boundary_candidate_confirm_s"]
                    )
                    boundary_age = (timestamp - self.last_boundary_at
                                    if self.last_boundary_at is not None else math.inf)
                    if candidate_confirmed:
                        self.left_x = geometry["left_x"]
                        self.right_x = geometry["right_x"]
                        self.last_boundary_at = timestamp
                        promoted_boundary = True
                        geometry["promoted"] = True
                        self._clear_boundary_candidate()
                    elif boundary_age <= self.config["boundary_hold_s"]:
                        geometry = {**geometry, "left_x": self.left_x,
                                    "right_x": self.right_x, "held": True,
                                    "observed_left_x": geometry["left_x"],
                                    "observed_right_x": geometry["right_x"]}
                    else:
                        result.update(status="uncertain", crossing_active=True,
                                      reasons=["boundary_jump"], geometry=geometry)
                        self._clear_pending()
                        return result
                else:
                    self._clear_boundary_candidate()
                if not promoted_boundary:
                    elapsed = max(0.0, timestamp - previous_timestamp) if previous_timestamp is not None else 0.0
                    alpha = min(1.0, elapsed / self.config["boundary_smooth_s"])
                    geometry["left_x"] = self.left_x + alpha * (geometry["left_x"] - self.left_x)
                    geometry["right_x"] = self.right_x + alpha * (geometry["right_x"] - self.right_x)
            self.left_x, self.right_x = geometry["left_x"], geometry["right_x"]
            if not geometry.get("held"):
                self.last_boundary_at = timestamp
        else:
            self._clear_boundary_candidate()
        result["geometry"] = geometry

        roi_fraction = roi["crosswalk_fraction"]
        roi_visible = bool(roi_fraction is not None
                           and roi_fraction >= self.config["roi_crosswalk_threshold"])
        if roi_visible:
            if self.roi_crosswalk_since is None:
                self.roi_crosswalk_since = timestamp
            self.roi_crosswalk_last_seen = timestamp
        else:
            occlusion_held = bool(
                roi["occlusion_fraction"] >= self.config["roi_occlusion_threshold"]
                and self.roi_crosswalk_since is not None
                and self.roi_crosswalk_last_seen is not None
                and timestamp - self.roi_crosswalk_last_seen
                <= self.config["entry_occlusion_hold_s"]
            )
            if not occlusion_held:
                self.roi_crosswalk_since = None
                self.roi_crosswalk_last_seen = None
        roi_confirmed = bool(self.roi_crosswalk_since is not None
                             and timestamp - self.roi_crosswalk_since + 1e-9
                             >= self.config["entry_confirm_s"])

        # 횡단 전에는 ROI 마스크 지속 시간과 가상 발의 좌우 경계만 확인한다.
        if not self.crossing_active:
            foot_x = self.config["foot_x"]
            inside = bool(geometry and geometry["left_x"] <= foot_x <= geometry["right_x"])
            geometry_candidate = bool(geometry and geometry["near"] and inside)
            if geometry_candidate:
                stable = bool(
                    self.entry_left_x is None or self.entry_right_x is None
                    or max(abs(geometry["left_x"] - self.entry_left_x),
                           abs(geometry["right_x"] - self.entry_right_x))
                    <= self.config["max_boundary_shift"]
                )
                if self.entry_geometry_since is None or not stable:
                    self.entry_geometry_since = timestamp
                self.entry_left_x = geometry["left_x"]
                self.entry_right_x = geometry["right_x"]
            else:
                self.entry_geometry_since = None
                self.entry_left_x = None
                self.entry_right_x = None
            geometry_confirmed = bool(
                self.entry_geometry_since is not None
                and timestamp - self.entry_geometry_since + 1e-9
                >= self.config["geometry_entry_confirm_s"]
            )
            if (roi_confirmed or geometry_confirmed) and inside:
                self.crossing_active = True
                self._transition("crossing")
                self._clear_pending()
                result.update(status="crossing", crossing_active=True,
                              event_id=self.event_id,
                              reasons=["entry_confirmed" if roi_confirmed
                                       else "geometry_entry_confirmed"])
                return result
            if roi_visible and geometry is not None and not inside:
                move = "right" if foot_x < geometry["left_x"] else "left"
                korean = "오른쪽" if move == "right" else "왼쪽"
                self._clear_pending()
                self._transition(f"align_{move}")
                result.update(status=self.phase, direction=move, repeat=True,
                              voice_text=f"횡단보도 앞, {korean} 이동",
                              voice_clip=f"crosswalk-align-{move}.mp3",
                              event_id=self.event_id, reasons=["entry_alignment"])
            else:
                self._clear_pending()
                self._transition("approach" if roi_visible else "search")
                result.update(status=self.phase, event_id=self.event_id,
                              reasons=["entry_confirming"] if roi_visible else [])
            return result

        # 횡단보도 끝에서 보행가능영역으로 이어지면 정상 도착으로 처리한다.
        destination_visible = geometry is None or not geometry["near"]
        finish = destination_visible and (
            semantics["walkable_fraction"] is not None
            and semantics["walkable_fraction"] >= self.config["finish_walkable_fraction"]
        )
        outside_phase = self.phase in ("outside_left", "outside_right")
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
                finished = self._transition("finished")
                self._clear_pending()
                if finished:
                    result.update(voice_text="횡단 완료",
                                  voice_clip="crosswalk-finished.mp3")
            result.update(status=self.phase, crossing_active=self.crossing_active,
                          event_id=self.event_id, reasons=["walkable_destination"])
            if outside_finish and self.crossing_active:
                move = ("right" if self.phase == "outside_left" else
                        "left" if self.phase == "outside_right" else None)
                korean = "오른쪽" if move == "right" else "왼쪽" if move == "left" else None
                text = (f"횡단보도 이탈, {korean} 이동"
                        if korean else "횡단보도 이탈")
                result.update(direction=move, repeat=True,
                              voice_text=text,
                              voice_clip=f"crosswalk-exit-{move}.mp3")
            return result
        roi_missing = not roi_visible
        if roi_missing:
            if roi["occlusion_fraction"] >= self.config["roi_occlusion_threshold"]:
                self._clear_pending()
                result.update(status="uncertain", crossing_active=True,
                              event_id=self.event_id, reasons=["crosswalk_roi_occluded"])
                return result
            exit_roi_fraction = exit_roi["crosswalk_fraction"]
            exit_roi_visible = bool(
                exit_roi_fraction is not None
                and exit_roi_fraction >= self.config["exit_roi_crosswalk_threshold"]
            )
            pending_outside = self.pending_kind in ("outside_left", "outside_right")
            if exit_roi_visible and pending_outside:
                outside = self.pending_kind
                if self._confirmed(outside, timestamp, self.config["exit_confirm_s"]):
                    self._transition(outside)
                    self._clear_pending()
                result.update(status=self.phase, crossing_active=True,
                              event_id=self.event_id,
                              reasons=["lateral_exit_with_bottom_roi_loss"])
                if self.phase in ("outside_left", "outside_right"):
                    move = "right" if self.phase == "outside_left" else "left"
                    korean = "오른쪽" if move == "right" else "왼쪽"
                    result.update(direction=move, repeat=True,
                                  voice_text=f"횡단보도 이탈, {korean} 이동",
                                  voice_clip=f"crosswalk-exit-{move}.mp3")
                return result
            hold_pending_exit = bool(
                pending_outside
                and self.pending_last_seen is not None
                and timestamp - self.pending_last_seen
                <= self.config["exit_candidate_hold_s"]
            )
            if hold_pending_exit:
                result.update(status="uncertain", crossing_active=True,
                              event_id=self.event_id,
                              reasons=["pending_exit_roi_missing"])
                return result
            self._clear_pending()
            result.update(status="uncertain", crossing_active=True, event_id=self.event_id,
                          reasons=["crosswalk_roi_missing"])
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
                result.update(direction=move, repeat=True,
                              voice_text=f"횡단보도 이탈, {korean} 이동",
                              voice_clip=f"crosswalk-exit-{move}.mp3")
            return result

        # 이탈 중에는 안쪽 복귀를 확인할 때까지 기존 방향 안내를 유지한다.
        if self.phase in ("outside_left", "outside_right"):
            if not self._confirmed("return", timestamp, self.config["return_confirm_s"]):
                move = "right" if self.phase == "outside_left" else "left"
                korean = "오른쪽" if move == "right" else "왼쪽"
                result.update(status=self.phase, crossing_active=True, direction=move,
                              voice_text=f"횡단보도 이탈, {korean} 이동",
                              voice_clip=f"crosswalk-exit-{move}.mp3", repeat=True,
                              event_id=self.event_id,
                              reasons=["return_confirming"])
                return result
            self._transition("crossing")
            self._clear_pending()

        edge_direction = ("right" if left_gap <= self.config["edge_margin"]
                          else "left" if right_gap <= self.config["edge_margin"] else None)
        if edge_direction is not None:
            edge_kind = f"edge_{edge_direction}"
            if self.phase == "edge" or self._confirmed(edge_kind, timestamp, self.config["edge_confirm_s"]):
                self._transition("edge")
                self._clear_pending()
            result.update(status=self.phase, crossing_active=True, direction=edge_direction,
                          event_id=self.event_id, reasons=["lateral_edge_near"])
            return result

        self._clear_pending()
        self._transition("crossing")
        result.update(status="crossing", crossing_active=True, event_id=self.event_id,
                      reasons=["inside_crosswalk"])
        return result
