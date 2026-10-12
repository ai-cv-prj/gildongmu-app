"""Apply configured temporal confirmation before creating hazard events."""

import math

from src.alert_policy import iou


class HazardConfirmation:
    def __init__(self, config):
        self.policies = config["hazard_confirmation"]
        self.reset()

    def reset(self):
        self.previous_by_class = {}
        self.previous_time = None

    def update(self, detections, timestamp, timestamp_valid=True):
        if not self.policies:
            return
        if (not timestamp_valid or self.previous_time is None
                or timestamp - self.previous_time <= 0):
            self.previous_by_class = {}
        current_by_class = {}
        for item in detections:
            name = item["class_name"]
            if name not in self.policies:
                continue
            box = item["xyxy"]
            if (len(box) != 4 or not all(math.isfinite(v) for v in box)
                    or box[2] <= box[0] or box[3] <= box[1]):
                continue
            current_by_class.setdefault(name, []).append(
                (item, {"box": list(box), "count": 1, "first_seen": timestamp}))
        next_previous = {}
        for name, current in current_by_class.items():
            policy = self.policies[name]
            previous = self.previous_by_class.get(name, [])
            if (self.previous_time is None or timestamp - self.previous_time
                    > policy["max_gap_s"]):
                previous = []
            pairs = sorted(
                ((iou(state["box"], old["box"]), index, previous_index)
                 for index, (_, state) in enumerate(current)
                 for previous_index, old in enumerate(previous)), reverse=True)
            used_current, used_previous = set(), set()
            for overlap, index, previous_index in pairs:
                if overlap < policy["match_iou"]:
                    break
                if index in used_current or previous_index in used_previous:
                    continue
                current[index][1]["count"] = previous[previous_index]["count"] + 1
                current[index][1]["first_seen"] = previous[previous_index]["first_seen"]
                used_current.add(index)
                used_previous.add(previous_index)
            for item, state in current:
                immediate = item["confidence"] >= policy["immediate_confidence"]
                duration = max(0., timestamp - state["first_seen"]) if timestamp_valid else 0.
                confirmed = (timestamp_valid and state["count"] >= policy["min_frames"]
                             and duration + 1e-6 >= policy["min_duration_s"])
                item.update(confirmation_status=("immediate" if immediate else
                                                 "confirmed" if confirmed else "candidate"),
                            confirmation_count=state["count"],
                            confirmation_duration_s=duration)
            next_previous[name] = [state for _, state in current]
        self.previous_by_class = next_previous if timestamp_valid else {}
        self.previous_time = timestamp if timestamp_valid else None
