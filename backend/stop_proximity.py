"""Estimate visual proximity to a stop from repeated YOLO detections.

This has no depth or location input. "nearby" means that a sufficiently
large transit_stop box persisted in the camera view.
"""

import math


def _iou(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    overlap = max(0.0, right - left) * max(0.0, bottom - top)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return overlap / (area_a + area_b - overlap) if area_a + area_b > overlap else 0.0


class StopProximity:
    """Track one current stop candidate across camera frames."""

    def __init__(self, config=None):
        cfg = {
            "min_box_height": 0.20,
            "min_box_width": 0.05,
            "min_box_bottom": 0.60,
            "confirm_frames": 3,
            "match_iou": 0.10,
            "max_gap_s": 2.0,
        }
        cfg.update(config or {})
        for key in ("min_box_height", "min_box_width", "min_box_bottom", "match_iou"):
            value = cfg[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError(f"stop_proximity.{key} must be between 0 and 1")
        if (isinstance(cfg["confirm_frames"], bool) or not isinstance(cfg["confirm_frames"], int)
                or cfg["confirm_frames"] < 1):
            raise ValueError("stop_proximity.confirm_frames must be a positive integer")
        value = cfg["max_gap_s"]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError("stop_proximity.max_gap_s must be positive")
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.previous_box = None
        self.previous_track_id = None
        self.previous_time = None
        self.observations = 0
        self.nearby = False

    def _result(self, status, candidate=None):
        return {
            "status": status,
            "nearby": status == "nearby",
            "newly_nearby": status == "nearby" and not self.nearby,
            "observations": self.observations,
            "required_observations": self.cfg["confirm_frames"],
            "confidence": candidate["confidence"] if candidate else None,
            "xyxy": candidate["box"] if candidate else None,
            "track_id": candidate["track_id"] if candidate else None,
        }

    def update(self, detections, shape, timestamp_s, *, camera_view="clear", state_reset=False):
        """Return a diagnostic event without changing walking or traffic warnings."""
        if state_reset or camera_view != "clear" or not math.isfinite(timestamp_s):
            self.reset()
            return self._result("unavailable" if camera_view != "clear" else "not_detected")
        if self.previous_time is not None and (
                timestamp_s <= self.previous_time
                or timestamp_s - self.previous_time > self.cfg["max_gap_s"]):
            self.reset()

        height, width = shape[:2]
        candidates = []
        for item in detections:
            if item.get("class_name") != "transit_stop":
                continue
            box = item.get("xyxy")
            confidence = item.get("confidence")
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                    or not isinstance(confidence, (int, float)) or not math.isfinite(confidence)):
                continue
            x1, y1, x2, y2 = (
                max(0.0, min(1.0, v / scale))
                for v, scale in zip(box, (width, height, width, height))
            )
            if (x2 - x1 < self.cfg["min_box_width"]
                    or y2 - y1 < self.cfg["min_box_height"]
                    or y2 < self.cfg["min_box_bottom"]):
                continue
            candidates.append({
                "box": [x1, y1, x2, y2],
                "confidence": float(confidence),
                "track_id": item.get("track_id"),
            })

        if not candidates:
            self.reset()
            return self._result("not_detected")

        matched = None
        if self.previous_box is not None:
            matches = [
                item for item in candidates
                if ((item["track_id"] is not None
                     and item["track_id"] == self.previous_track_id)
                    or _iou(item["box"], self.previous_box) >= self.cfg["match_iou"])
            ]
            if matches:
                matched = max(matches, key=lambda item: _iou(item["box"], self.previous_box))
        candidate = matched or max(candidates, key=lambda item: (
            (item["box"][2] - item["box"][0]) * (item["box"][3] - item["box"][1]),
            item["confidence"],
        ))
        self.observations = self.observations + 1 if matched else 1
        status = "nearby" if self.observations >= self.cfg["confirm_frames"] else "candidate"
        result = self._result(status, candidate)
        self.previous_box = candidate["box"]
        self.previous_track_id = candidate["track_id"]
        self.previous_time = timestamp_s
        self.nearby = status == "nearby"
        return result
