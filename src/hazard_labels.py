"""
file_path: src/hazard_labels.py

Conservative display names; raw detector classes remain untouched.
"""

class LabelMemory:
    # 객체 이름 판정기 초기화
    def __init__(self, cfg):
        """객체 이름 확정 기준을 저장한다."""
        self.cfg = cfg
        self.reset()

    # 객체 이름 기록 초기화
    def reset(self):
        """프레임 사이에 누적한 객체 이름 기록을 지운다."""
        self.states = {}

    # 객체 이름 안정성 갱신
    def update(self, detections, timestamp):
        """반복 검출을 확인해 표시용 객체 이름을 확정한다."""
        ttl = max(1.0, self.cfg["label_conflict_hold_s"] * 2)
        self.states = {key: state for key, state in self.states.items()
                       if timestamp - state["seen"] <= ttl}
        for item in detections:
            if item.get("risk_level") is None:
                item.update(label_status="not_applicable",
                            display_label=item["class_name"], hazard_id=None)
                continue
            track = item.get("track_id")
            event = item.get("event_id")
            key = ("track", track) if track is not None else (
                ("event", event) if event is not None else None)
            item["hazard_id"] = None if key is None else f"{key[0]}:{key[1]}"
            if key is None:
                item.update(label_status="provisional", display_label="obstacle")
                continue
            state = self.states.get(key)
            name = item["class_name"]
            if state is None:
                state = {"name": name, "count": 1, "conflict_until": -float("inf"),
                         "seen": timestamp}
            elif state["name"] != name:
                state.update(name=name, count=1,
                             conflict_until=timestamp + self.cfg["label_conflict_hold_s"],
                             seen=timestamp)
            else:
                state["count"] += 1
                state["seen"] = timestamp
            self.states[key] = state
            reliable = (state["count"] >= self.cfg["label_confirm_frames"]
                        and item.get("confidence", 0) >= self.cfg["label_confidence"]
                        and timestamp >= state["conflict_until"])
            status = "reliable" if reliable else (
                "conflicting" if timestamp < state["conflict_until"] else "provisional")
            item.update(label_status=status,
                        display_label=name if reliable else "obstacle")
