"""
file_path: src/risk.py

Experimental obstacle risk assessment with preserved detections and optional IDs.
"""
import math
from copy import deepcopy
from src.risk_config import risk_config
from src.risk_geometry import geometry, sidewalk_context, surrounding_walkability
from src.risk_motion import CameraMotionGuard, MotionHistory
from src.tracking import DetectionTracker
from src.alert_policy import AlertPolicy
from src.path_roi import SidewalkGuidedROI
from src.risk_proximity import proximity
from src.surface_risk import SurfaceRisk
from src.warning_groups import group_warnings
from src.hazard_labels import LabelMemory
from src.warning_summary import WarningSelector
from src.camera_view import CameraViewGuard

class VideoClock:
    """Use source PTS. Nominal FPS fallback is explicitly invalid for motion."""
    # 영상 타임스탬프 판독기 초기화
    def __init__(self, fps):
        """PTS가 없을 때 쓸 명목 FPS를 저장한다."""
        self.fps, self.previous = fps, None

    # 프레임 시간 판독
    def read(self, pts_ms, index):
        """유효한 PTS를 우선 사용하고 대체 시간의 운동 판정은 비활성화한다."""
        valid = (isinstance(pts_ms, (int,float)) and math.isfinite(pts_ms) and pts_ms >= 0
                 and (self.previous is None or pts_ms > self.previous))
        if valid:
            self.previous = pts_ms
            return pts_ms/1000, True, "pts"
        return index/self.fps, False, "nominal_fps_motion_disabled"

class RiskEngine:
    # 위험 판정 모듈 초기화
    def __init__(self, config=None, tracking=None, tracker=None, camera_guard=None):
        """추적·ROI·촬영 상태·경고 선택기를 같은 설정으로 준비한다."""
        self.config = risk_config(config)
        self.tracker = tracker if tracker is not None else DetectionTracker(tracking)
        self.camera_guard = camera_guard if camera_guard is not None else CameraMotionGuard(self.config)
        self.motion = MotionHistory(self.config)
        self.alerts = AlertPolicy(self.config)
        self.path_roi = SidewalkGuidedROI(self.config)
        self.surface = SurfaceRisk(self.config)
        self.labels = LabelMemory(self.config)
        self.warning_selector = WarningSelector(self.config)
        self.camera_view = CameraViewGuard(self.config)
        self.reset()

    # 영상별 위험 판정 상태 초기화
    def reset(self):
        """새 영상에서 이전 영상의 추적과 경고 상태를 지운다."""
        self.tracker.reset()
        self.camera_guard.reset()
        self.motion.reset()
        self.alerts.reset()
        self.path_roi.reset()
        self.surface.reset()
        self.labels.reset()
        self.warning_selector.reset()
        self.camera_view.reset()
        self.last_trusted_danger_time = None
        self.previous_time, self.previous_shape = None, None
        self.previous_tracker_status = self.tracker.status
        self.epoch = getattr(self, "epoch", -1) + 1

    # 프레임 위험도 판정
    def update(self, frame, detections, timestamp_s, timestamp_valid=True, class_map=None, label_ids=None):
        """프레임의 장애물과 보행 영역에서 위험도와 대표 경고를 고른다."""
        shape = frame.shape[:2]
        frame_gap_s = None if self.previous_time is None else timestamp_s - self.previous_time
        shape_changed = self.previous_shape is not None and shape != self.previous_shape
        discontinuity = (shape_changed or frame_gap_s is not None and
                         (frame_gap_s <= 0 or frame_gap_s > self.config["hard_reset_gap_s"]))
        motion_gap = (frame_gap_s is not None and
                      frame_gap_s > self.config["reset_gap_s"])
        if discontinuity:
            self.reset()
        elif motion_gap:
            self.motion.reset()
        self.previous_time, self.previous_shape = timestamp_s, shape
        # No motion quantities are trusted after timestamp loss.
        if not timestamp_valid:
            self.motion.reset()
        camera_stable = bool(self.camera_guard.update(frame))
        previous_view_status = self.camera_view.status
        camera_view = self.camera_view.update(frame, detections, class_map, label_ids,
                                              timestamp_s, camera_stable)
        view_unavailable = camera_view["status"] == "unavailable"
        view_recovered = previous_view_status == "unavailable" and camera_view["status"] == "clear"
        if view_recovered:
            # Tracks and path geometry observed while the lens was away are stale.
            self.tracker.reset()
            self.motion.reset()
            self.alerts.reset()
            self.path_roi.reset()
            self.surface.reset()
            self.labels.reset()
            self.warning_selector.reset()
            self.previous_tracker_status = self.tracker.status
            self.last_trusted_danger_time = None
            self.epoch += 1
        if not camera_stable or camera_view["status"] != "clear":
            self.motion.reset()
        self.motion.prune(timestamp_s)
        tracked = self.tracker.attach(detections, frame)
        tracking_reset = self.tracker.status == "failed" and self.previous_tracker_status != "failed"
        if tracking_reset:
            self.motion.reset()
            self.epoch += 1
        self.previous_tracker_status = self.tracker.status
        roi = self.path_roi.update(class_map if not view_unavailable else None,
                                   label_ids if not view_unavailable else None,
                                   shape, timestamp_s, timestamp_valid)
        results = []
        for detection in tracked:
            item = {**deepcopy(detection), "risk_level": "monitor", "assessment_quality": "limited",
                    "reasons": [], "observed": True, "geometry": None, "motion": None,
                    "sidewalk": {"status":"unavailable","walkable_fraction":None}}
            # Traffic-light detection/selection/display remain entirely in the existing modules.
            if detection["class_name"] == "traffic_light":
                item.update(risk_level=None, assessment_quality="not_applicable",
                            reasons=["traffic_light_not_assessed"])
                results.append(item)
                continue
            g = geometry(detection, shape, self.config, roi)
            if g is None:
                item.update(assessment_quality="unknown", reasons=["invalid_box"])
                results.append(item)
                continue
            m = self.motion.update(detection, g, timestamp_s, timestamp_valid and camera_stable
                                   and not motion_gap and not view_unavailable,
                                   roi.get("corridor_polygons",roi["corridor_polygon"]))
            p = proximity(detection, g, self.config)
            item.update(geometry=g, motion=m, proximity=p, release_evidence=None)
            threshold = self.config["overlap_threshold"]
            ground_reason = roi.get("ground_extent", {}).get("reason")
            ground_visible = ground_reason in (
                "connected_walkable_extent", "held_possible_occlusion",
                "held_unavailable_ground")
            entry_y = (max(self.config["lateral_near_y"], roi.get("path_top_y", 0)+.10)
                       if self.config["roi_ground_adapt_enabled"] and ground_visible
                       else self.config["lateral_near_y"])
            related = max(g["corridor_overlap"],g["immediate_overlap"]) >= threshold
            static = p["policy"] == "static_ground"
            # The nearly full-width lower ROI observes side hazards without
            # declaring each side overlap an imminent collision.
            immediate_danger = (not self.config["wide_roi_priority_enabled"] or
                g["central_immediate_overlap"] >= threshold or
                (g["point"][1] >= self.config["side_danger_y"] and g["close_candidate"]))
            if static:
                if related and p["band"] == "near" and immediate_danger:
                    item["risk_level"] = "danger"
                    item["reasons"].append("static_near_contact")
                elif related and p["band"] in ("middle","unknown","near"):
                    item["risk_level"] = "caution"
                    item["reasons"].append("static_path_candidate")
                elif related:
                    if g["close_candidate"]:
                        item["risk_level"] = "caution"
                        item["reasons"].append("large_static_candidate")
                    else:
                        item["reasons"].append("far_static_path_candidate")
            else:
                if g["immediate_overlap"] >= threshold and immediate_danger:
                    item["risk_level"] = "danger"
                    item["reasons"].append("near_path_occupied")
                elif related:
                    item["risk_level"] = "caution"
                    item["reasons"].append("near_path_side_candidate" if
                        g["immediate_overlap"] >= threshold else "path_occupied")
            if g["side_proximity"]:
                if item["risk_level"] == "monitor":
                    item["risk_level"] = "caution"
                item["reasons"].append("side_close_candidate")
            if g["edge_contact"]:
                item["reasons"].append("edge_candidate")
            if m["time_to_corridor_s"] is not None and g["point"][1] >= entry_y:
                if item["risk_level"] == "monitor":
                    item["risk_level"] = "caution"
                item["reasons"].append("lateral_entry")
            future_related = (self.config["relative_entry_enabled"] and m.get("time_to_path_s") is not None
                              and g["point"][1]>=entry_y)
            if future_related and static:
                if item["risk_level"] == "monitor":
                    item["risk_level"] = "caution"
                item["reasons"].append("relative_path_entry")
            if (self.config["approach_danger_enabled"] and related
                    and m["time_to_near_s"] is not None):
                item["risk_level"] = "danger"
                item["reasons"].append("approaching_near_path")
            if self.config["ttc_alerts"] and m["ttc_scale_s"] is not None and (related or future_related):
                ttc = m["ttc_scale_s"]
                vx = m["velocity_norm_per_s"][0]
                x = g["point"][0]
                moving_outward = (g["central_immediate_overlap"] < threshold and
                    ((x < self.config["central_danger_left"] and vx <= -self.config["min_lateral_speed"])
                     or (x > self.config["central_danger_right"] and vx >= self.config["min_lateral_speed"])))
                if ttc <= self.config["ttc_danger_s"] and not moving_outward:
                    item["risk_level"] = "danger"
                    item["reasons"].append("short_ttc")
                elif moving_outward:
                    item["reasons"].append("lateral_departure")
                elif ttc <= self.config["ttc_caution_s"]:
                    if item["risk_level"] == "monitor":
                        item["risk_level"] = "caution"
                    item["reasons"].append("approaching")
            surroundings = surrounding_walkability(g,class_map,label_ids,shape,self.config)
            item["surrounding_walkability"] = surroundings
            if (self.config["walkable_surroundings_filter_enabled"]
                    and item["risk_level"] == "danger"
                    and surroundings["status"] == "available"
                    and surroundings["all_non_walkable"]):
                item["risk_level"] = "caution"
                item["reasons"].append("nonwalkable_surroundings")
                item["release_evidence"] = "nonwalkable_surroundings"
            clear = (timestamp_valid and camera_stable and not g["clipped"] and not roi["changed"])
            if clear and item["risk_level"] == "monitor":
                if (max(g["corridor_overlap"],g["immediate_overlap"]) < self.config["exit_overlap_threshold"]
                        and g["horizontal_path_gap"] >= self.config["exit_margin"]
                        and not g["side_proximity"] and not future_related
                        and m["quality"] == "valid"):
                    item["release_evidence"] = "image_path_exit"
            elif clear and item["risk_level"] == "caution" and m["quality"] == "valid":
                item["release_evidence"] = "lower_proximity_or_urgency"
            if m["quality"] == "valid" and not g["clipped"]:
                item["assessment_quality"] = "valid"
            results.append(item)
        if view_unavailable:
            # Preserve detector output and raw geometric assessment for audit.
            # Neither is suitable as a new object-specific warning in this view.
            for item in results:
                if item["risk_level"] is not None:
                    item["untrusted_risk_level"] = item["risk_level"]
                    item["risk_level"] = None
                    item["assessment_quality"] = "view_unavailable"
                    item["reasons"].append("camera_view_unavailable")
                item["alert_level"] = None
            events = self.alerts.update([], timestamp_s)
            surface, _ = self.surface.update(None, None, shape, roi, timestamp_s,
                                             False, False, [])
            surface.update(status="unavailable", alert_level=None, regions=[],
                           reasons=["camera_view_unavailable"])
        else:
            events = self.alerts.update(results, timestamp_s)
            self.labels.update(results, timestamp_s)
            surface, surface_events = self.surface.update(class_map,label_ids,shape,roi,timestamp_s,
                                                         timestamp_valid,camera_stable,results)
            events.extend(surface_events)
        if camera_view["changed"]:
            events.append({"source": "camera_view",
                           "type": ("cleared" if camera_view["status"] == "clear"
                                    else "raised" if previous_view_status == "clear"
                                    else "changed"),
                           "level": ("monitor" if camera_view["status"] == "clear"
                                     else "caution"),
                           "reason": camera_view["reason"],
                           "message": camera_view["message"],
                           "safety_confirmed": False})
        # A blocked semantic region may be a class absent from YOLO training.
        # Keep the detector result for audit, but display the hazard generically.
        matched_indices = {index for region in surface.get("regions", [])
                           for index in region.get("matched_detection_indices", [])}
        if surface.get("alert_level"):
            for index, item in enumerate(results):
                if item.get("detection_index", index) in matched_indices:
                    item["semantic_path_overlap"] = True
                    item["display_label"] = "obstacle"
                    item.setdefault("reasons", []).append("non_walkable_in_path")
        warning_groups=group_warnings(results,self.config)
        warning = self.warning_selector.update(
            results, surface, [] if view_unavailable else self.alerts.advisories,
            timestamp_s, camera_view)
        if view_unavailable and self.last_trusted_danger_time is not None:
            if timestamp_s - self.last_trusted_danger_time <= self.config["uncertainty_hold_s"]:
                warning = {"level": "danger", "source": "camera_view",
                           "hazard_id": "camera_view:previous_hazard",
                           "detection_index": None, "priority": 30,
                           "label": "obstacle",
                           "text": "DANGER | previous hazard unverified; point camera forward",
                           "reasons": ["previous_hazard_unverified"]}
        elif camera_view["status"] == "clear" and warning["level"] == "danger":
            self.last_trusted_danger_time = timestamp_s
        return {"timestamp_s":timestamp_s, "timestamp_valid":timestamp_valid,
                "state_epoch":self.epoch, "state_reset":bool(discontinuity or tracking_reset or view_recovered),
                "view_recovered":view_recovered, "camera_view":camera_view,
                "motion_gap":bool(motion_gap), "frame_gap_s":frame_gap_s,
                "camera_motion_stable":camera_stable, "tracker_status":self.tracker.status,
                "roi":roi, "surface":surface, "advisories":self.alerts.advisories,
                "warning_groups":warning_groups, "warning":warning,
                "warning_text":warning["text"], "level":warning["level"],
                "detections":results, "events":events}

    # 보행가능영역 설명 부착
    def add_sidewalk_context(self, prediction, class_map, label_ids, shape):
        """검출 객체에 같은 프레임의 보행가능영역 문맥을 붙인다."""
        # Same-frame context only. An absent/non-walkable mask never vetoes a warning.
        for item in prediction["detections"]:
            if item["geometry"] is not None:
                item["sidewalk"] = sidewalk_context(item["geometry"], class_map, label_ids, shape)
        return prediction
