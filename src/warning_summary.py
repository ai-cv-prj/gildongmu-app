"""
file_path: src/warning_summary.py

Choose one current hazard by urgency, position and fresh evidence.
"""
LEVEL = {"monitor": 0, "caution": 1, "danger": 2}


# 객체 위험의 대표 경고 후보 계산
def _object_candidate(item, cfg):
    """객체의 위험도와 위치로 대표 경고 후보를 만든다."""
    level = item.get("alert_level", item.get("risk_level"))
    if level not in ("caution", "danger") or not item.get("warning_primary", True):
        return None
    geometry = item.get("geometry") or {}
    reasons = item.get("reasons", [])
    proximity = item.get("proximity") or {}
    y = (geometry.get("point") or [0, 0])[1]
    related = max(geometry.get("corridor_overlap", 0),
                  geometry.get("immediate_overlap", 0)) >= cfg["overlap_threshold"]
    priority = 20 if level == "danger" else 10
    priority += 5 if "near_path_occupied" in reasons or "static_near_contact" in reasons else 0
    priority += 4 if "short_ttc" in reasons else 0
    priority += 3 if "approaching" in reasons or "lateral_entry" in reasons else 0
    priority += 2 if geometry.get("immediate_overlap", 0) >= cfg["overlap_threshold"] else 0
    priority += 1 if related else 0
    priority += min(2, max(0, y)) * 1.5
    if item.get("alert_status") == "held":
        priority -= 1
    name = item.get("display_label", "obstacle")
    if item.get("label_status") != "reliable":
        name = "obstacle"
    detail = ("close" if priority >= (25 if level == "danger" else 15)
              else "in path")
    semantic = item.get("semantic_path_overlap", False)
    label_text = "non-walkable area in path" if semantic else f"{name} {detail}"
    return {"level": level, "source": "surface_object" if semantic else "object",
            "hazard_id": item.get("hazard_id") or f"event:{item.get('event_id')}",
            "detection_index": item.get("detection_index"),
            "priority": priority, "label": name,
            "text": f"{level.upper()} | {label_text}",
            "reasons": reasons}


# 보행불가 영역의 경고 후보 계산
def _surface_candidate(surface):
    """마스크가 확인한 보행불가 영역의 경고 후보를 만든다."""
    if not surface.get("alert_level") or surface.get("suppressed_duplicate"):
        return None
    regions = [r for r in surface.get("regions", [])
               if not r.get("matched_detection_indices")]
    priority = 13 if regions else 11
    if any(r.get("box_norm", [0, 0, 0, 0])[3] >= .78 for r in regions):
        priority += 2
    detail = ("path visibility uncertain" if surface.get("status") == "uncertain"
              else "non-walkable area in path")
    return {"level": "caution", "source": "surface",
            "hazard_id": "surface", "detection_index": None,
            "priority": priority, "label": "obstacle",
            "text": f"CAUTION | {detail}",
            "reasons": surface.get("reasons", [])}


class WarningSelector:
    # 대표 경고 선택기 초기화
    def __init__(self, cfg):
        """대표 경고를 고르는 기준과 이전 상태를 설정한다."""
        self.cfg = cfg
        self.reset()

    # 이전 경고 초기화
    def reset(self):
        """이전에 선택한 경고의 식별 정보와 시간을 지운다."""
        self.last_id = None
        self.last_time = -float("inf")

    # 현재 프레임의 대표 경고 선택
    def update(self, detections, surface, advisories, timestamp, camera_view=None):
        """객체와 보행 영역 후보에서 현재 대표 경고 하나를 고른다."""
        candidates = [c for item in detections
                      if (c := _object_candidate(item, self.cfg)) is not None]
        scene = _surface_candidate(surface)
        if scene is not None:
            candidates.append(scene)
        for advisory in advisories:
            candidates.append({
                "level": "caution", "source": "advisory",
                "hazard_id": f"advisory:{advisory['event_id']}",
                "detection_index": None, "priority": 15,
                "label": "obstacle", "text": "CAUTION | nearby object out of view",
                "reasons": [advisory["reason"]],
            })
        if camera_view and camera_view.get("status") in ("uncertain", "unavailable"):
            candidates.append({"level": "caution", "source": "camera_view",
                               "hazard_id": "camera_view",
                               "detection_index": None, "priority": 18,
                               "label": "camera", "text": camera_view["message"],
                               "reasons": [camera_view["reason"]]})
        if not candidates:
            self.last_id = None
            return {"level": "monitor", "source": None, "hazard_id": None,
                    "priority": 0, "text": "", "reasons": []}
        candidates.sort(key=lambda c: (-LEVEL[c["level"]], -c["priority"],
                                       str(c["hazard_id"])))
        chosen = candidates[0]
        previous = next((c for c in candidates if c["hazard_id"] == self.last_id),
                        None)
        if (previous is not None and previous["level"] == chosen["level"]
                and timestamp - self.last_time < .35
                and chosen["priority"] - previous["priority"] <= 1.5):
            chosen = previous
        if chosen["hazard_id"] != self.last_id:
            self.last_id = chosen["hazard_id"]
            self.last_time = timestamp
        return chosen
