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
