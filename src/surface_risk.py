"""
file_path: src/surface_risk.py

Warnings for observed non-walkable path regions, independent of YOLO boxes.

Semantic labels alone indicate a path check, never metric distance or confirmed
collision. Missing/unstable evidence is not a reason to declare the scene safe.
"""
import cv2
import numpy as np


class SurfaceRisk:
    # 보행불가 영역 판정기 초기화
    def __init__(self, cfg):
        """보행불가 영역의 확인 시간과 중복 판정 기준을 저장한다."""
        self.cfg = cfg
        self.reset()

    # 이전 영역 판정 초기화
    def reset(self):
        """이전 영상의 보행불가 영역 상태를 지운다."""
        self.pending_since = None
        self.clear_since = None
        self.active = False
        self.previous = None
        self.last_event = -float("inf")
        self.last_time = None
        self.last_observed = None

    # 보행불가 영역 위험 갱신
    def update(self, class_map, label_ids, shape, roi, timestamp, valid, camera_stable, detections):
        """ROI 안의 보행불가 영역을 평가하고 안정적인 객체 경고와 중복을 확인한다."""
        result = {"enabled":self.cfg["surface_risk_enabled"], "status":"unavailable",
                  "alert_level":None, "regions":[], "reasons":[],
                  "path_nonwalkable_fraction":None, "near_nonwalkable_fraction":None,
                  "suppressed_duplicate":False, "matched_region_count":0,
                  "unmatched_region_count":0}
        if not result["enabled"]:
            result["status"] = "disabled"
            return result, []
        if self.last_time is not None and (timestamp<=self.last_time or timestamp-self.last_time>self.cfg["hard_reset_gap_s"]):
            self.reset()
        self.last_time = timestamp
        if class_map is None or not label_ids or class_map.shape != tuple(shape[:2]):
            self.pending_since = None
            self.clear_since = None
            if self.active and self.last_observed is not None and timestamp-self.last_observed<=self.cfg["uncertainty_hold_s"]:
                result.update(status="uncertain",alert_level="caution",reasons=["mask_unavailable"])
            else:
                self.active = False
            return result, []
        if not all(key in label_ids for key in ("walkable","crosswalk","non_walkable")):
            return result, []
        self.last_observed = timestamp
        h,w = shape[:2]
        sw,sh = 324,max(64,round(h*324/w))
        labels = cv2.resize(class_map.astype(np.uint8),(sw,sh),interpolation=cv2.INTER_NEAREST)
        walk = np.isin(labels,[label_ids["walkable"],label_ids["crosswalk"]])
        path,near = np.zeros((sh,sw),np.uint8),np.zeros((sh,sw),np.uint8)
        def pixels(poly):
            return np.rint(np.asarray(poly)*[sw-1,sh-1]).astype(np.int32)
        for poly in roi.get("corridor_polygons",[roi["corridor_polygon"]]):
            cv2.fillPoly(path,[pixels(poly)],1)
        cv2.fillPoly(near,[pixels(roi["immediate_polygon"])],1)
        # The fixed near region can extend outside a vertically adjusted path.
        path=np.maximum(path,near)
        blocked = ((labels==label_ids["non_walkable"]) & (path>0)).astype(np.uint8)
        path_area = max(1,int(path.sum()))
        fraction = float(blocked.sum()/path_area)
        near_fraction = float((blocked*(near>0)).sum()/max(1,near.sum()))
        change = (0.0 if self.previous is None else
                  float(((walk!=self.previous)&(path>0)).sum()/path_area))
        self.previous = walk.copy()
        n,components,stats,_ = cv2.connectedComponentsWithStats(blocked,8)
        candidates = []
        start = min(p[1] for p in roi["corridor_polygon"])
        bands = np.linspace(start,1,5)
        for component_id in range(1,n):
            x,y,bw,bh,area = map(int,stats[component_id])
            if area/path_area < self.cfg["surface_min_area"]:
                continue
            cmask = components==component_id
            band_fraction = max(float(cmask[round(a*sh):round(b*sh)].sum()/
                                max(1,path[round(a*sh):round(b*sh)].sum()))
                                for a,b in zip(bands,bands[1:]))
            if band_fraction < self.cfg["surface_band_fraction"]:
                continue
            contours,_ = cv2.findContours(cmask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            contour = max(contours,key=cv2.contourArea)
            poly = cv2.approxPolyDP(contour,2,True).reshape(-1,2)/[sw-1,sh-1]
            if len(poly)<3:
                continue
            # Match this connected blocked region to a stable, warned detector box.
            # A detection elsewhere must not hide this independent path hazard.
            matches = []
            for index, item in enumerate(detections):
                if (item.get("label_status") != "reliable" or
                        item.get("alert_level", item.get("risk_level")) not in ("caution", "danger")):
                    continue
                box = (item.get("geometry") or {}).get("box_norm")
                if box is None:
                    continue
                bx1 = max(0, min(sw, int(box[0] * sw)))
                by1 = max(0, min(sh, int(box[1] * sh)))
                bx2 = max(0, min(sw, int(np.ceil(box[2] * sw))))
                by2 = max(0, min(sh, int(np.ceil(box[3] * sh))))
                covered = int(cmask[by1:by2, bx1:bx2].sum()) / max(1, area)
                if covered >= .70:
                    matches.append(item.get("detection_index", index))
            candidates.append({"polygon":poly.tolist(),
                "box_norm":[x/sw,y/sh,(x+bw)/sw,(y+bh)/sh],
                "area_fraction":float(area/path_area), "band_fraction":band_fraction,
                "matched_detection_indices":matches})
        candidates.sort(key=lambda x:x["area_fraction"],reverse=True)
        result.update(path_nonwalkable_fraction=fraction,near_nonwalkable_fraction=near_fraction,
                      label_change_fraction=change,regions=candidates[:3],status="clear")
        unreliable = (not valid or not camera_stable or
                      change>=self.cfg["surface_unstable_change"] or fraction>=.80)
        if fraction>=.80:
            result["regions"]=[]
        observed = bool(candidates)
        if not valid:
            self.pending_since = None
            result["status"] = "uncertain"
            if self.active and self.last_observed is not None and timestamp-self.last_observed<=self.cfg["uncertainty_hold_s"]:
                result["alert_level"] = "caution"
                result["reasons"] = ["path_observation_uncertain"]
            return result, []
        events = []
        if observed:
            self.clear_since = None
            if self.pending_since is None:
                self.pending_since = timestamp
            if timestamp-self.pending_since+1e-9>=self.cfg["surface_confirm_s"]:
                self.active = True
            result["status"] = "uncertain" if unreliable else ("active" if self.active else "confirming")
            result["reasons"] = ["path_observation_uncertain" if unreliable else "non_walkable_in_path"]
        else:
            self.pending_since = None
            if self.active:
                # Do not clear on a shaking camera or a single green frame.
                if unreliable:
                    self.clear_since = None
                    result["status"] = "uncertain"
                else:
                    if self.clear_since is None:
                        self.clear_since = timestamp
                    if timestamp-self.clear_since+1e-9>=self.cfg["surface_clear_s"]:
                        self.active = False
                        events.append({"source":"surface","type":"cleared","level":"monitor",
                                       "safety_confirmed":False,"reason":"image_path_visible"})
                    else:
                        result["status"] = "held"
        if self.active:
            result["alert_level"] = "caution"
            if not result["reasons"]:
                result["reasons"] = ["confirming_path_visibility"]
            # Suppress only when every blocked component has a reliable match.
            matched = sum(bool(r["matched_detection_indices"]) for r in candidates)
            result["matched_region_count"] = matched
            result["unmatched_region_count"] = len(candidates) - matched
            duplicate = bool(candidates) and matched == len(candidates)
            result["suppressed_duplicate"] = duplicate
            if not duplicate and timestamp-self.last_event>=self.cfg["repeat_cooldown_s"]:
                events.append({"source":"surface","type":"raised" if self.last_event==-float("inf") else "repeated",
                               "level":"caution","reasons":result["reasons"]})
                self.last_event = timestamp
        else:
            self.last_event = -float("inf")
        return result,events
