"""
file_path: src/roi_recalibration.py

Time-based direction confirmation for a shared image-space camera profile.
"""
from collections import deque
from copy import deepcopy
import numpy as np


class DirectionCalibration:
    # ROI 방향 보정기 초기화
    def __init__(self, cfg):
        """기본 ROI와 중심선 후보 기록을 준비한다."""
        self.cfg = cfg
        self.base = np.asarray(cfg["corridor_polygon"], float)
        self.top = np.flatnonzero(self.base[:, 1] == self.base[:, 1].min())
        self.base_center = float(self.base[self.top, 0].mean())
        self.center = self.base_center
        self.calibrated = False
        self.previous_time = None
        self.samples = deque()
        self.last_valid = None
        self.confidence = 0.0

    # 중심이 이동한 ROI 생성
    def polygon(self, center):
        """중심 이동량을 화면 범위 안으로 제한한 ROI를 만든다."""
        points = self.base.copy()
        shift = np.clip(center - self.base_center,
                        -self.base[self.top, 0].min(), 1-self.base[self.top, 0].max())
        points[self.top, 0] += shift
        return points.tolist()

    # 시간에 따른 ROI 방향 보정
    def update(self, target, info, timestamp, timestamp_valid):
        """확인된 중심선만 반영하고 큰 시간 공백에서 보정을 초기화한다."""
        if (not timestamp_valid or self.previous_time is not None and
                (timestamp <= self.previous_time or
                 timestamp-self.previous_time > self.cfg["hard_reset_gap_s"])):
            self.__init__(self.cfg)
        self.previous_time = timestamp
        before = self.center
        source, pending = ("calibrated" if self.calibrated else "fixed"), None
        if target is None or not timestamp_valid or info.get("confidence", 0) < .35:
            self.samples.clear()
            if self.calibrated:
                age = timestamp-self.last_valid
                source = "held" if age <= self.cfg["roi_hold_s"] else "fallback"
        else:
            self.last_valid = timestamp
            self.confidence = info["confidence"]
            # A new, inconsistent candidate restarts confirmation.
            if self.samples and abs(target-np.median([v for _,v in self.samples])) > self.cfg["roi_candidate_spread"]:
                self.samples.clear()
            self.samples.append((timestamp, target))
            while self.samples and timestamp-self.samples[0][0] > self.cfg["roi_small_confirm_s"] + .15:
                self.samples.popleft()
            candidate = float(np.median([v for _,v in self.samples]))
            displacement = abs(candidate-self.center)
            required = (self.cfg["roi_confirm_s"] if not self.calibrated else
                        self.cfg["roi_change_confirm_s"] if displacement >= self.cfg["roi_large_shift"]
                        else self.cfg["roi_small_confirm_s"])
            if self.calibrated and displacement < self.cfg["roi_jitter_shift"]:
                self.samples.clear()
            elif timestamp-self.samples[0][0]+1e-9 >= required:
                self.center = candidate
                self.calibrated = True
                self.samples.clear()
                source = "calibrated"
            else:
                source, pending = "confirming", candidate
        polygons = [self.polygon(self.center)]
        if pending is not None and abs(pending-self.center) > 1e-5:
            polygons.append(self.polygon(pending))
        elif source == "fallback" and abs(self.center-self.base_center) > 1e-5:
            polygons.append(self.polygon(self.base_center))
        return {**info, "source":source, "center_x":self.center,
                "corridor_polygon":polygons[0], "corridor_polygons":polygons,
                "immediate_polygon":deepcopy(self.cfg["immediate_polygon"]),
                "changed":abs(before-self.center)>1e-5 or len(polygons)>1,
                "direction_confidence":self.confidence if self.calibrated else info.get("confidence",0),
                "calibrated":self.calibrated, "pending_center":pending,
                "profile":"shared_image_coordinates"}
