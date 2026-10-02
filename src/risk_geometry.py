"""
file_path: src/risk_geometry.py

Image-space path occupancy. Sidewalk labels supply context, never a hard gate.
"""
import cv2
import numpy as np

# 박스를 다각형으로 변환
def rectangle(box):
    """객체 박스를 사각형 꼭짓점 배열로 변환한다."""
    x1, y1, x2, y2 = box
    return np.asarray([[x1,y1],[x2,y1],[x2,y2],[x1,y2]], dtype=np.float32)

# ROI와 객체 발자국의 겹침 계산
def overlap(box, polygon):
    """박스 면적 중 ROI와 겹치는 비율을 계산한다."""
    area = (box[2]-box[0]) * (box[3]-box[1])
    if area <= 0:
        return 0.0
    intersection, _ = cv2.intersectConvexConvex(rectangle(box), np.asarray(polygon, np.float32))
    return float(np.clip(intersection / area, 0, 1))

# 위험 판정용 화면 기하 계산
def geometry(detection, shape, cfg, roi=None):
    """객체 발자국과 복도·즉시 위험 ROI의 관계를 계산한다."""
    height, width = shape[:2]
    box = np.asarray(detection["xyxy"], dtype=float) / [width, height, width, height]
    if not np.isfinite(box).all() or box[2] <= box[0] or box[3] <= box[1]:
        return None
    box = np.clip(box, 0, 1)
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    x1, y1, x2, y2 = map(float, box)
    strip_height = min((y2-y1)*cfg["footprint_height_ratio"], cfg["footprint_max_height"])
    left,right = x1,x2
    if detection["class_name"] in cfg["static_ground_classes"] and not cfg["full_static_footprint"]:
        center=(x1+x2)/2
        radius=max(.002,(x2-x1)*.25)
        left,right=max(0,center-radius),min(1,center+radius)
    strip = [left, y2-strip_height, right, y2]
    corridor = roi["corridor_polygon"] if roi else cfg["corridor_polygon"]
    immediate = roi["immediate_polygon"] if roi else cfg["immediate_polygon"]
    central_immediate = [[cfg["central_danger_left"], min(p[1] for p in immediate)],
                         [cfg["central_danger_right"], min(p[1] for p in immediate)],
                         [cfg["central_danger_right"], 1.0],
                         [cfg["central_danger_left"], 1.0]]
    corridors = roi.get("corridor_polygons",[corridor]) if roi else [corridor]
    # 객체와 ROI 경계 사이의 수평 간격 계산
    def gap(polygon):
        """객체 발자국에서 다각형 경계까지의 수평 간격을 구한다."""
        intersections=[]
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):
            if abs(a[1]-b[1])<1e-9:
                if abs(y2-a[1])<1e-6: intersections.extend([a[0],b[0]])
            elif min(a[1],b[1])<=y2<=max(a[1],b[1]):
                intersections.append(a[0]+(y2-a[1])*(b[0]-a[0])/(b[1]-a[1]))
        return max(0,min(intersections)-right,left-max(intersections)) if intersections else 1.0
    top_y = roi.get("path_top_y",min(p[1] for p in corridor)) if roi else min(p[1] for p in corridor)
    ground_reason = (roi or {}).get("ground_extent", {}).get("reason")
    ground_visible = ground_reason in ("connected_walkable_extent",
                                       "held_possible_occlusion",
                                       "held_unavailable_ground")
    close_y = (max(cfg["side_near_y"], top_y+.20)
               if cfg["roi_ground_adapt_enabled"] and ground_visible
               else cfg["side_near_y"])
    margin = cfg["edge_margin_ratio"]
    edges = [name for name, yes in (
        ("left", x1 <= margin), ("right", x2 >= 1-margin),
        ("top", y1 <= margin), ("bottom", y2 >= 1-margin)) if yes]
    return {
        "box_norm": list(map(float, box)), "footprint": strip,
        "point": [(x1+x2)/2, y2], "height": y2-y1, "width": x2-x1,
        "immediate_top_y": min(point[1] for point in immediate),
        "close_candidate": bool(cfg["side_proximity_enabled"] and y2>=close_y
            and (y2-y1)>=cfg["side_min_height"] and (x2-x1)>=cfg["side_min_width"]),
        "side_proximity": bool(cfg["side_proximity_enabled"] and y2>=close_y
            and max(overlap(strip,poly) for poly in corridors)<cfg["overlap_threshold"]
            and (y2-y1)>=cfg["side_min_height"] and (x2-x1)>=cfg["side_min_width"]),
        "side_direction": "left" if (x1+x2)/2<.5 else "right",
        "corridor_overlap": max(overlap(strip, poly) for poly in corridors),
        "immediate_overlap": overlap(strip, immediate),
        "central_immediate_overlap": overlap(strip, central_immediate),
        "edge_contact": edges,
        "horizontal_path_gap": min([gap(poly) for poly in corridors]+[gap(immediate)]),
        "bottom_clipped": y2 >= 1-1/height,
        # One original-image pixel at the frame edge makes scale censored.
        "clipped": x1 <= 1/width or y1 <= 1/height or x2 >= 1-1/width or y2 >= 1-1/height,
    }

# 검출 주변 보행가능영역 설명
def sidewalk_context(item, class_map, label_ids, shape):
    """객체 발자국에서 보행가능영역의 비율을 확인한다."""
    if class_map is None or label_ids is None or class_map.shape != tuple(shape[:2]):
        return {"status": "unavailable", "walkable_fraction": None}
    height, width = shape[:2]
    x1, _, x2, y2 = item["box_norm"]
    # Sample the contact neighbourhood, including ground just below the box.
    left, right = max(0, int(x1*width)), min(width, max(int(x1*width)+1, int(np.ceil(x2*width))))
    top, bottom = max(0, int((y2-.02)*height)), min(height, int(np.ceil((y2+.02)*height)))
    patch = class_map[top:bottom, left:right]
    ids = [label_ids[name] for name in ("walkable", "crosswalk") if name in label_ids]
    if not patch.size or not ids:
        return {"status": "unavailable", "walkable_fraction": None}
    fraction = float(np.isin(patch, ids).mean())
    return {"status": "available", "walkable_fraction": fraction}


# 장애물 bbox 주변의 보행가능영역 검사
def surrounding_walkability(item, class_map, label_ids, shape, cfg):
    """
    bbox의 보이는 왼쪽·오른쪽·아래쪽 영역에서 보행가능 비율을 계산한다.
    화면 밖으로 잘린 영역은 검사 대상에서 제외한다.
    """
    unavailable = {"status":"unavailable", "regions":{}, "all_non_walkable":True}
    if class_map is None or label_ids is None or class_map.shape != tuple(shape[:2]):
        return unavailable
    walkable_ids = [label_ids[name] for name in ("walkable", "crosswalk") if name in label_ids]
    if not walkable_ids:
        return unavailable

    height, width = shape[:2]
    x1, y1, x2, y2 = item["box_norm"]
    left, top = int(np.floor(x1*width)), int(np.floor(y1*height))
    right, bottom = int(np.ceil(x2*width)), int(np.ceil(y2*height))
    box_width, box_height = max(1,right-left), max(1,bottom-top)
    side_width = max(cfg["surrounding_min_region_pixels"],
                     min(round(box_width*cfg["surrounding_side_width_ratio"]),
                         round(width*cfg["surrounding_max_side_width_ratio"])))
    side_top = bottom-max(cfg["surrounding_min_region_pixels"],
                          round(box_height*cfg["surrounding_side_height_ratio"]))
    bottom_height = max(cfg["surrounding_min_region_pixels"],
                        min(round(box_height*cfg["surrounding_bottom_height_ratio"]),
                            round(height*cfg["surrounding_max_bottom_height_ratio"])))
    candidates = {
        "left": (left-side_width, side_top, left, bottom),
        "right": (right, side_top, right+side_width, bottom),
        "bottom": (left, bottom, right, bottom+bottom_height),
    }
    regions = {}
    for name, (rx1,ry1,rx2,ry2) in candidates.items():
        rx1,rx2=max(0,rx1),min(width,rx2)
        ry1,ry2=max(0,ry1),min(height,ry2)
        if rx2<=rx1 or ry2<=ry1:
            continue
        patch=class_map[ry1:ry2,rx1:rx2]
        if patch.size < cfg["surrounding_min_region_pixels"]:
            continue
        regions[name]={"walkable_fraction":float(np.isin(patch,walkable_ids).mean()),
                       "pixel_count":int(patch.size)}
    fractions=[region["walkable_fraction"] for region in regions.values()]
    return {"status":"available" if regions else "clipped",
            "regions":regions,
            "all_non_walkable":bool(not fractions or
                all(value < cfg["surrounding_walkable_threshold"] for value in fractions))}
