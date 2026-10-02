"""Keep visual stop proximity stable through brief detection or view loss.

Proximity is image based, independent of collision safety. A session records
one arrival event for a future bus-number interaction; losing a box does not
erase that interaction or establish that the user has left the stop.
"""

from collections import deque
import math


def _iou(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    overlap = max(0.0, right - left) * max(0.0, bottom - top)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return overlap / (area_a + area_b - overlap) if area_a + area_b > overlap else 0.0


class StopProximity:
    """Confirm a current stop with bounded memory and latch one session arrival."""

    def __init__(self, config=None):
        cfg = {
            "min_box_height": 0.20,
            "min_box_width": 0.05,
            "min_box_bottom": 0.60,
            "side_center_limit": 0.25,
            "side_min_box_height": 0.30,
            "side_min_box_width": 0.08,
            "side_min_box_bottom": 0.45,
            "confirm_frames": 3,
            "confirm_s": 0.4,
            "confirm_window_s": 2.0,
            "candidate_hold_s": 1.0,
            "nearby_hold_s": 3.0,
            "min_confidence": 0.30,
            "match_iou": 0.10,
            "max_gap_s": 2.0,
        }
        cfg.update(config or {})
        for key in ("min_box_height", "min_box_width", "min_box_bottom",
                    "side_center_limit", "side_min_box_height", "side_min_box_width",
                    "side_min_box_bottom", "min_confidence", "match_iou"):
            value = cfg[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError(f"stop_proximity.{key} must be between 0 and 1")
        if cfg["side_center_limit"] > 0.5:
            raise ValueError("stop_proximity.side_center_limit must be at most 0.5")
        if (isinstance(cfg["confirm_frames"], bool) or not isinstance(cfg["confirm_frames"], int)
                or cfg["confirm_frames"] < 1):
            raise ValueError("stop_proximity.confirm_frames must be a positive integer")
        for key in ("confirm_s", "confirm_window_s", "candidate_hold_s",
                    "nearby_hold_s", "max_gap_s"):
            value = cfg[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"stop_proximity.{key} must be positive")
        if cfg["confirm_s"] > cfg["confirm_window_s"]:
            raise ValueError("stop_proximity.confirm_s must not exceed confirm_window_s")
        if cfg["candidate_hold_s"] > cfg["confirm_window_s"]:
            raise ValueError("stop_proximity.candidate_hold_s must not exceed confirm_window_s")
        self.cfg = cfg
        self.reset()

    def reset(self):
        """Start a new session, including the future bus-number interaction."""
        self.arrival_recorded = False
        self.previous_update = None
        self._clear_evidence()

    def _clear_evidence(self):
        """Discard visual evidence without forgetting a recorded arrival."""
        self.previous_box = None
        self.previous_track_id = None
        self.previous_time = None
        self.candidate = None
        self.observed_times = deque()
        self.observations = 0
        self.nearby = False

    def _result(self, status, timestamp=None, *, observed=False, reason=None,
                newly_nearby=False, arrival_event=None):
        # A held box belongs to an old image, so do not draw it on the live view.
        candidate = self.candidate
        return {
            "status": status,
            "nearby": status == "nearby",
            "newly_nearby": newly_nearby,
            "observations": self.observations,
            "required_observations": self.cfg["confirm_frames"],
            "confidence": candidate["confidence"] if candidate else None,
            "xyxy": candidate["box"] if observed and candidate else None,
            "track_id": candidate["track_id"] if candidate else None,
            "basis": candidate["basis"] if candidate else None,
            "observed": observed,
            "held": status in ("candidate", "nearby") and not observed,
            "reason": reason,
            "last_seen_age_s": (max(0.0, timestamp - self.previous_time)
                                if timestamp is not None and self.previous_time is not None
                                else None),
            "arrival_recorded": self.arrival_recorded,
            "arrival_event_id": 1 if self.arrival_recorded else None,
            "arrival_event": arrival_event,
        }

    def update(self, detections, shape, timestamp_s, *, camera_view="clear", state_reset=False):
        """Update proximity only; arrival neither permits walking nor cancels warnings."""
        valid_time = (type(timestamp_s) in (int, float) and math.isfinite(timestamp_s))
        if state_reset or not valid_time:
            self._clear_evidence()
            self.previous_update = timestamp_s if valid_time else None
            return self._result("unavailable" if camera_view != "clear" or not valid_time
                                else "not_detected", reason="observation_reset")
        if self.previous_update is not None and (
                timestamp_s <= self.previous_update
                or timestamp_s - self.previous_update > self.cfg["max_gap_s"]):
            self._clear_evidence()
        self.previous_update = timestamp_s
        if self.previous_time is not None:
            hold = self.cfg["nearby_hold_s"] if self.nearby else self.cfg["candidate_hold_s"]
            if timestamp_s - self.previous_time > hold:
                self._clear_evidence()
        candidates = self._candidates(detections, shape) if camera_view == "clear" else []
        if not candidates:
            reason = "detection_missing" if camera_view == "clear" else f"camera_{camera_view}"
            status = ("nearby" if self.nearby else "candidate" if self.candidate
                      else "not_detected" if camera_view == "clear" else "unavailable")
            return self._result(status, timestamp_s, reason=reason)

        matches = [item for item in candidates if self.previous_box is not None and (
            (item["track_id"] is not None and item["track_id"] == self.previous_track_id)
            or _iou(item["box"], self.previous_box) >= self.cfg["match_iou"])]
        matched = max(matches, key=lambda item: _iou(item["box"], self.previous_box)) if matches else None
        candidate = matched or max(candidates, key=lambda item: (
            (item["box"][2] - item["box"][0]) * (item["box"][3] - item["box"][1]),
            item["confidence"],
        ))
        if matched is None:
            self._clear_evidence()
        self.observed_times.append(timestamp_s)
        while timestamp_s - self.observed_times[0] > self.cfg["confirm_window_s"]:
            self.observed_times.popleft()
        self.observations = len(self.observed_times)
        confirmed = (self.observations >= self.cfg["confirm_frames"]
                     # Epoch seconds lose sub-microsecond precision when subtracted.
                     and timestamp_s - self.observed_times[0] + 1e-6 >= self.cfg["confirm_s"])
        newly_nearby = confirmed and not self.nearby
        self.nearby = self.nearby or confirmed
        self.candidate = candidate
        self.previous_box = candidate["box"]
        self.previous_track_id = candidate["track_id"]
        self.previous_time = timestamp_s
        arrival_event = None
        if self.nearby and not self.arrival_recorded:
            self.arrival_recorded = True
            arrival_event = {"type": "stop_arrival", "event_id": 1,
                             "timestamp_s": timestamp_s, "basis": "visual_proximity"}
        return self._result("nearby" if self.nearby else "candidate", timestamp_s,
                            observed=True, reason="visual_proximity",
                            newly_nearby=newly_nearby, arrival_event=arrival_event)

    def _candidates(self, detections, shape):
        """Keep the geometric proximity rule separate from temporal evidence."""
        height, width = shape[:2]
        if height <= 0 or width <= 0:
            return []
        candidates = []
        for item in detections:
            if item.get("class_name") != "transit_stop":
                continue
            box = item.get("xyxy")
            confidence = item.get("confidence")
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                    or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence)
                    or not self.cfg["min_confidence"] <= confidence <= 1):
                continue
            x1, y1, x2, y2 = (
                max(0.0, min(1.0, v / scale))
                for v, scale in zip(box, (width, height, width, height))
            )
            box_width, box_height = x2 - x1, y2 - y1
            if (box_width < self.cfg["min_box_width"]
                    or box_height < self.cfg["min_box_height"]):
                continue
            if y2 >= self.cfg["min_box_bottom"]:
                basis = "bottom"
            elif (box_width >= self.cfg["side_min_box_width"]
                  and box_height >= self.cfg["side_min_box_height"]
                  and y2 >= self.cfg["side_min_box_bottom"]):
                center_x = (x1 + x2) / 2
                if center_x <= self.cfg["side_center_limit"]:
                    basis = "left"
                elif center_x >= 1 - self.cfg["side_center_limit"]:
                    basis = "right"
                else:
                    continue
            else:
                continue
            candidates.append({
                "box": [x1, y1, x2, y2],
                "confidence": float(confidence),
                "track_id": item.get("track_id"),
                "basis": basis,
            })

        return candidates
