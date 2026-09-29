"""
file_path: src/ground_extent.py

Estimate the visible ground top while retaining a stable wide lower ROI.
"""
import math

import cv2
import numpy as np


class GroundExtent:
    # 보행가능영역 상단 추정기 초기화
    def __init__(self, cfg):
        """넓은 하단 ROI를 유지하며 상단 경계를 추정할 설정을 저장한다."""
        self.cfg = cfg
        self.base_y = min(point[1] for point in cfg["corridor_polygon"])
        self.max_y = cfg["roi_top_max_y"]
        self.top_y = self.base_y
        self.previous_time = None
        self.last_good_time = None
        self.pending_shrink_since = None

    # 마스크에서 연결된 지면 경계 추출
    def _candidate(self, class_map, label_ids, shape):
        """화면 아래와 연결된 보행가능영역의 상단 후보를 구한다."""
        if (class_map is None or not label_ids
                or class_map.shape != tuple(shape[:2])):
            return None, "mask_unavailable"
        h, w = shape[:2]
        mh, mw = max(64, round(h * 270 / w)), 270
        mask = cv2.resize(
            np.isin(class_map, [label_ids.get("walkable", -1),
                                 label_ids.get("crosswalk", -1)]).astype(np.uint8),
            (mw, mh), interpolation=cv2.INTER_NEAREST,
        )
        _, components, _, _ = cv2.connectedComponentsWithStats(mask, 8)
        seed = components[round(.80 * mh):round(.98 * mh),
                          round(.2 * mw):round(.8 * mw)]
        ids, counts = np.unique(seed[seed > 0], return_counts=True)
        if not len(ids):
            return None, "ground_extent_uncertain"
        component = components == ids[int(np.argmax(counts))]
        widths = component[:, round(.15 * mw):round(.85 * mw)].sum(axis=1)
        support = widths >= .08 * mw
        length = max(3, round(.04 * mh))
        runs = np.convolve(support.astype(int), np.ones(length, dtype=int), "valid")
        candidates = np.flatnonzero(
            (runs >= length) & (np.arange(len(runs)) >= round(.20 * mh))
        )
        if not len(candidates):
            return None, "ground_extent_uncertain"
        return candidates[0] / mh + .04, "connected_walkable_extent"

    # 지면 경계 안정화
    def update(self, class_map, label_ids, shape, timestamp, valid=True):
        """가림과 관측 누락을 고려해 ROI 상단 경계를 갱신한다."""
        if (self.previous_time is not None
                and (timestamp <= self.previous_time
                     or timestamp - self.previous_time > self.cfg["hard_reset_gap_s"])):
            self.__init__(self.cfg)
        previous_time = self.previous_time
        self.previous_time = timestamp
        before = self.top_y
        candidate, reason = self._candidate(class_map, label_ids, shape) if valid else (
            None, "invalid_timestamp")
        if candidate is None:
            if (self.last_good_time is not None
                    and timestamp - self.last_good_time <= self.cfg["roi_extent_hold_s"]):
                reason = "held_unavailable_ground"
                target = self.top_y
            else:
                # With no ground evidence, retain the broad image-space fallback
                # so a near path entrant is still assessed.
                target = self.base_y
        else:
            target = float(np.clip(candidate, self.base_y, self.max_y))
            if reason == "connected_walkable_extent":
                self.last_good_time = timestamp
            # A sudden loss of walkable ground can be an object occluding the path.
            # Hold the old upper boundary briefly before shrinking the corridor.
            if (previous_time is not None and target - self.top_y > .08
                    and reason == "connected_walkable_extent"):
                if self.pending_shrink_since is None:
                    self.pending_shrink_since = timestamp
                if timestamp - self.pending_shrink_since < self.cfg["roi_extent_hold_s"]:
                    target = self.top_y
                    reason = "held_possible_occlusion"
            else:
                self.pending_shrink_since = None
        if previous_time is None:
            self.top_y = target
        else:
            dt = max(0, timestamp - previous_time)
            if abs(target - self.top_y) > self.cfg["roi_extent_deadband"]:
                blend = 1 - math.exp(-dt / self.cfg["roi_extent_smooth_s"])
                step = float(np.clip(blend * (target - self.top_y),
                                     -self.cfg["roi_extent_max_shift_per_s"] * dt,
                                     self.cfg["roi_extent_max_shift_per_s"] * dt))
                self.top_y = float(np.clip(self.top_y + step,
                                           self.base_y, self.max_y))
        return {"top_y": self.top_y, "reason": reason,
                "changed": abs(before - self.top_y) > .01}
