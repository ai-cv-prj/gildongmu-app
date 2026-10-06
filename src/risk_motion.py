"""Short image-space histories with timestamp and camera-motion quality gates."""
from collections import deque
import math
import cv2
import numpy as np
from src.risk_geometry import overlap


# RANSAC 카메라 변환 신뢰 조건 확인
def camera_motion_inliers_valid(inliers, cfg):
    """일치점 비율과 실제 개수가 모두 설정 기준 이상인지 반환한다."""
    if inliers is None or not inliers.size:
        return False
    return (float(inliers.mean()) >= cfg["camera_motion_min_inlier_ratio"]
            and int(inliers.sum()) >= cfg["camera_motion_min_inlier_points"])

class CameraMotionGuard:
    """Conservative affine sanity check; this does not measure world velocity."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        """직전 영상과 안정 판정 및 계산 실패 유예 시간을 초기화한다."""
        self.previous = None
        self.last_stable_at = None
        self.unavailable_since = None
        self.last_result = None

    # 인접 프레임의 카메라 변환 측정
    def _measure(self, frame):
        """변환이 안정적이면 참, 임계값 초과면 거짓, 계산할 수 없으면 None을 반환한다."""
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(cv2.resize(frame, (320, max(32, round(h*320/w)))), cv2.COLOR_BGR2GRAY)
        previous, self.previous = self.previous, gray
        if previous is None or previous.shape != gray.shape:
            return None
        points = cv2.goodFeaturesToTrack(previous, 120, .01, 8)
        if points is None or len(points) < 8:
            return None
        following, status, _ = cv2.calcOpticalFlowPyrLK(previous, gray, points, None)
        if following is None or status is None:
            return None
        selected = status.ravel().astype(bool)
        a, b = points[selected], following[selected]
        if len(a) < 8:
            return None
        transform, inliers = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=3)
        if (transform is None or not np.isfinite(transform).all()
                or not camera_motion_inliers_valid(inliers, self.cfg)):
            return None
        scale = math.hypot(transform[0,0], transform[1,0])
        rotation = abs(math.degrees(math.atan2(transform[1,0], transform[0,0])))
        translation = math.hypot(transform[0,2]/gray.shape[1], transform[1,2]/gray.shape[0])
        return (rotation <= self.cfg["camera_max_rotation_deg"]
                and abs(scale-1) <= self.cfg["camera_max_scale_change"]
                and translation <= self.cfg["camera_max_translation"])

    # 카메라 안정 상태 갱신
    def update(self, frame, timestamp):
        """계산 불가를 잠시 유예하되 실제 임계값 초과는 즉시 불안정으로 반환한다."""
        measured = self._measure(frame)
        if measured is True:
            self.last_stable_at = timestamp
            self.unavailable_since = None
            self.last_result = True
            return True
        if measured is False:
            self.unavailable_since = None
            self.last_result = False
            return False
        if self.last_result is False:
            return False
        if self.unavailable_since is None:
            self.unavailable_since = timestamp
        reference = (self.last_stable_at if self.last_stable_at is not None
                     else self.unavailable_since)
        held = timestamp - reference <= self.cfg["camera_motion_failure_hold_s"] + 1e-9
        if not held:
            self.last_result = False
        return held


class BackgroundStationarityGuard:
    """Track middle/lower background flow and confirm a nearly still camera over time."""

    # 배경 정지 판정기 초기화
    def __init__(self, cfg):
        """정지 시간과 배경 움직임 허용치를 저장하고 프레임 이력을 초기화한다."""
        self.cfg = cfg
        self.reset()

    # 영상별 정지 판정 상태 초기화
    def reset(self):
        """이전 영상과 정지 후보 시간을 제거한다."""
        self.previous = None
        self.previous_boxes = []
        self.previous_time = None
        self.still_since = None
        self.moving_since = None
        self.status = "uncertain"

    # 동적 객체를 제외한 중·하단 배경 마스크 생성
    def _background_mask(self, shape, boxes):
        """상단과 화면 끝, 검출 객체를 제외한 광학 흐름 탐색 마스크를 반환한다."""
        height, width = shape
        mask = np.zeros((height, width), dtype=np.uint8)
        top = round(height * self.cfg["stationary_middle_top"])
        bottom = round(height * self.cfg["stationary_bottom"])
        mask[top:bottom] = 255
        for box in boxes:
            try:
                x1, y1, x2, y2 = map(float, box)
            except (TypeError, ValueError):
                continue
            x1 = max(0, min(width, round(x1)))
            x2 = max(0, min(width, round(x2)))
            y1 = max(0, min(height, round(y1)))
            y2 = max(0, min(height, round(y2)))
            if x2 > x1 and y2 > y1:
                cv2.rectangle(mask, (x1, y1), (x2, y2), 0, -1)
        return mask

    # 인접 프레임의 배경 이동량 측정
    def _measure(self, frame, detections, elapsed_s):
        """동적 객체를 제외한 중·하단 특징점의 초당 이동량과 확대율을 계산한다."""
        height, width = frame.shape[:2]
        resized_height = max(32, round(height * 320 / width))
        gray = cv2.cvtColor(cv2.resize(frame, (320, resized_height)), cv2.COLOR_BGR2GRAY)
        scale_x, scale_y = 320 / width, resized_height / height
        boxes = []
        for item in detections or []:
            box = item.get("xyxy") if isinstance(item, dict) else None
            if box is not None and len(box) == 4:
                boxes.append([box[0] * scale_x, box[1] * scale_y,
                              box[2] * scale_x, box[3] * scale_y])
        previous, previous_boxes = self.previous, self.previous_boxes
        self.previous, self.previous_boxes = gray, boxes
        if previous is None or previous.shape != gray.shape or elapsed_s <= 0:
            return None
        mask = self._background_mask(previous.shape, previous_boxes)
        points = cv2.goodFeaturesToTrack(previous, 160, .01, 6, mask=mask)
        if points is None or len(points) < self.cfg["stationary_min_points"]:
            return None
        following, status, _ = cv2.calcOpticalFlowPyrLK(previous, gray, points, None)
        if following is None or status is None:
            return None
        selected = status.ravel().astype(bool)
        before, after = points[selected], following[selected]
        if len(before) < self.cfg["stationary_min_points"]:
            return None
        current_mask = self._background_mask(gray.shape, boxes)
        current_xy = np.rint(after.reshape(-1, 2)).astype(int)
        inside = ((current_xy[:, 0] >= 0) & (current_xy[:, 0] < gray.shape[1])
                  & (current_xy[:, 1] >= 0) & (current_xy[:, 1] < gray.shape[0]))
        accepted = np.zeros(len(after), dtype=bool)
        accepted[inside] = current_mask[current_xy[inside, 1], current_xy[inside, 0]] > 0
        before, after = before[accepted], after[accepted]
        if len(before) < self.cfg["stationary_min_points"]:
            return None
        lower_y = gray.shape[0] * self.cfg["stationary_lower_top"]
        band_minimum = max(3, self.cfg["stationary_min_points"] // 3)
        middle_count = int((before[:, 0, 1] < lower_y).sum())
        lower_count = int((before[:, 0, 1] >= lower_y).sum())
        if min(middle_count, lower_count) < band_minimum:
            return None
        transform, inliers = cv2.estimateAffinePartial2D(
            before, after, method=cv2.RANSAC, ransacReprojThreshold=3)
        if (transform is None or not np.isfinite(transform).all()
                or not camera_motion_inliers_valid(inliers, self.cfg)):
            return None
        usable = inliers.ravel().astype(bool)
        displacement = np.linalg.norm(
            after[usable, 0] - before[usable, 0], axis=1) / gray.shape[0] / elapsed_s
        if len(displacement) < self.cfg["stationary_min_points"]:
            return None
        scale = math.hypot(transform[0, 0], transform[1, 0])
        return {
            "median_motion_per_s": float(np.median(displacement)),
            "p80_motion_per_s": float(np.percentile(displacement, 80)),
            "scale_change_per_s": float(abs(math.log(max(scale, 1e-9))) / elapsed_s),
            "point_count": int(len(displacement)),
            "middle_point_count": middle_count,
            "lower_point_count": lower_count,
        }

    # 카메라 배경 정지 상태 갱신
    def update(self, frame, detections, timestamp, timestamp_valid=True):
        """3초 연속 정지를 확인하고 짧은 흔들림에는 정지 상태를 유지한다."""
        elapsed_s = None if self.previous_time is None else timestamp - self.previous_time
        if (not timestamp_valid or elapsed_s is not None
                and (elapsed_s <= 0 or elapsed_s > self.cfg["hard_reset_gap_s"])):
            self.reset()
            elapsed_s = None
        self.previous_time = timestamp
        metrics = self._measure(frame, detections, elapsed_s or 0)
        if metrics is None:
            if self.status == "stationary":
                if self.moving_since is None:
                    self.moving_since = timestamp
                held = timestamp - self.moving_since < self.cfg["stationary_release_s"]
                if held:
                    return {"status": "stationary", "reason": "background_motion_unavailable_held",
                            "duration_s": max(0.0, timestamp - self.still_since)}
            self.still_since = None
            self.moving_since = None
            self.status = "uncertain"
            return {"status": self.status, "reason": "background_motion_unavailable",
                    "duration_s": 0.0}
        still = (metrics["median_motion_per_s"] <= self.cfg["stationary_median_motion_per_s"]
                 and metrics["p80_motion_per_s"] <= self.cfg["stationary_p80_motion_per_s"]
                 and metrics["scale_change_per_s"] <= self.cfg["stationary_scale_change_per_s"])
        if not still:
            if self.status == "stationary":
                if self.moving_since is None:
                    self.moving_since = timestamp
                moving_duration = timestamp - self.moving_since
                if moving_duration < self.cfg["stationary_release_s"]:
                    return {"status": "stationary", "reason": "background_movement_held",
                            "duration_s": max(0.0, timestamp - self.still_since),
                            "release_candidate_s": moving_duration, **metrics}
            self.still_since = None
            self.moving_since = None
            self.status = "moving"
            return {"status": self.status, "reason": "background_moving",
                    "duration_s": 0.0, **metrics}
        if self.still_since is None:
            self.still_since = timestamp
        self.moving_since = None
        duration = max(0.0, timestamp - self.still_since)
        self.status = ("stationary" if duration + 1e-9 >= self.cfg["stationary_confirm_s"]
                       else "confirming")
        return {"status": self.status,
                "reason": "background_stationary" if self.status == "stationary"
                          else "background_stationary_confirming",
                "duration_s": duration, **metrics}

class MotionHistory:
    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.histories = {}

    def prune(self, timestamp):
        self.histories = {key: history for key, history in self.histories.items()
                          if timestamp-history[-1][0] <= self.cfg["history_window_s"]}

    def update(self, detection, geometry, timestamp, valid, corridor_polygon=None):
        result = {"quality": "insufficient", "velocity_norm_per_s": None,
                  "time_to_corridor_s": None, "time_to_path_s": None, "time_to_near_s": None,
                  "ttc_scale_s": None, "history_s": 0.0,
                  "relative_expansion_per_s": None, "approach_state": "unknown",
                  "ttc_invalid_reason": "insufficient_history"}
        track_id = detection["track_id"]
        if track_id is None:
            return result
        if not valid:
            self.histories.pop(track_id, None)
            result["quality"] = "unstable"
            result["ttc_invalid_reason"] = "unstable_motion"
            return result
        history = self.histories.setdefault(track_id, deque())
        # Do not interpret a changed class or reacquired stale track as continuous motion.
        if history and (history[-1][1] != detection["class_id"]
                        or timestamp <= history[-1][0]
                        or timestamp-history[-1][0] > self.cfg["reset_gap_s"]):
            history.clear()
        history.append((timestamp, detection["class_id"], *geometry["point"],
                        geometry["height"], geometry["clipped"]))
        while history and timestamp-history[0][0] > self.cfg["history_window_s"] + 1e-9:
            history.popleft()
        duration = timestamp-history[0][0]
        result["history_s"] = duration
        if len(history) < 3 or duration + 1e-9 < self.cfg["min_history_s"]:
            return result
        samples = np.asarray([row[:5] for row in history], float)
        times = samples[:,0]-timestamp
        matrix = np.column_stack([times, np.ones(len(times))])
        fitted, *_ = np.linalg.lstsq(matrix, samples[:,2:5], rcond=None)
        residual = float(np.max(np.sqrt(np.mean((matrix @ fitted-samples[:,2:5])**2, axis=0))))
        if residual > self.cfg["max_motion_residual"]:
            result["quality"] = "unstable"
            result["ttc_invalid_reason"] = "unstable_motion"
            return result
        vx, vy, dh = map(float, fitted[0])
        result["quality"] = "valid"
        result["velocity_norm_per_s"] = [vx, vy]
        # Only a tracked, stable image-space approach may anticipate the near zone.
        if (self.cfg["approach_danger_enabled"] and not geometry["clipped"]
                and vy >= self.cfg["min_forward_speed"]):
            near_y = (self.cfg["static_danger_y"] if detection["class_name"] in
                      self.cfg["static_ground_classes"] else
                      geometry["immediate_top_y"])
            if geometry["point"][1] < near_y:
                until_near = (near_y - geometry["point"][1]) / vy
                if until_near <= self.cfg["approach_danger_s"]:
                    left, _, right, _ = geometry["footprint"]
                    left += vx * until_near
                    right += vx * until_near
                    central_left = self.cfg["central_danger_left"]
                    central_right = self.cfg["central_danger_right"]
                    horizontal_overlap = max(0.0, min(right, central_right) - max(left, central_left)) / (right-left)
                    if horizontal_overlap >= self.cfg["overlap_threshold"]:
                        result["time_to_near_s"] = float(until_near)
        # TTC uses relative expansion; do not subtract forward ego-motion.
        expansion = dh / geometry["height"]
        if any(row[5] for row in history):
            result["ttc_invalid_reason"] = "clipped_box"
        else:
            result["relative_expansion_per_s"] = float(expansion)
            threshold = self.cfg["min_expansion_rate"]
            result["approach_state"] = "approaching" if expansion >= threshold else ("receding" if expansion <= -threshold else "steady")
            if expansion >= threshold:
                result["ttc_scale_s"] = float(1 / expansion)
                result["ttc_invalid_reason"] = None
            else:
                result["ttc_invalid_reason"] = "not_expanding"
        # Nine samples (including now) over the configurable short horizon.
        if ((self.cfg["relative_entry_enabled"] or detection["class_name"] not in self.cfg["static_ground_classes"])
                and abs(vx) >= self.cfg["min_lateral_speed"] and geometry["corridor_overlap"] < self.cfg["overlap_threshold"]):
            for future in np.linspace(0, self.cfg["prediction_horizon_s"], 9)[1:]:
                projected = np.asarray(geometry["footprint"]) + [vx*future, vy*future, vx*future, vy*future]
                polygon = corridor_polygon if corridor_polygon is not None else self.cfg["corridor_polygon"]
                polygons = polygon if isinstance(polygon[0][0],(list,tuple,np.ndarray)) else [polygon]
                if max(overlap(projected,p) for p in polygons) >= self.cfg["overlap_threshold"]:
                    result["time_to_path_s"] = float(future)
                    if detection["class_name"] not in self.cfg["static_ground_classes"]:
                        result["time_to_corridor_s"] = float(future)
                    break
        return result
