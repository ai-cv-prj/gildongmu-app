"""이동 후보의 지면과 객체 점유 검사. 좌표는 영상 비율이며 실제 거리가 아니다."""

import math

import cv2
import numpy as np
from src.settings import load_audio_settings


GUIDANCE = load_audio_settings()["guidance"]


def warning_directions(item, image_width):
    """행동 선택과 이동 검사가 같은 좌·중·우 경계 및 침범 기준을 사용한다."""
    try:
        box = item["xyxy"]
        left = max(0.0, min(float(image_width), float(box[0])))
        right = max(0.0, min(float(image_width), float(box[2])))
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        return set()
    if image_width <= 0 or not all(math.isfinite(v) for v in (left, right)) or right <= left:
        return set()
    boundaries = {
        "left": (0.0, image_width * GUIDANCE["walking_left_max_ratio"]),
        "center": (image_width * GUIDANCE["walking_left_max_ratio"],
                   image_width * GUIDANCE["walking_right_min_ratio"]),
        "right": (image_width * GUIDANCE["walking_right_min_ratio"], float(image_width)),
    }
    overlaps = {side: max(0.0, min(right, end) - max(left, start))
                for side, (start, end) in boundaries.items()}
    largest = max(overlaps.values())
    directions = {side for side, overlap in overlaps.items() if overlap > 0 and overlap == largest}
    for side, overlap in overlaps.items():
        start, end = boundaries[side]
        threshold = GUIDANCE["walking_center_intrusion_ratio" if side == "center"
                             else "walking_side_intrusion_ratio"]
        if overlap >= (end - start) * threshold:
            directions.add(side)
    return directions


def direction_ground(class_map, label_ids, shape):
    """발 높이에서 좌우로 이어지는 폭 있는 보행 띠를 프레임당 한 번 검사한다.

    전체 영역 평균만으로 도로 틈을 통과시키지 않도록 각 행·열도 검사한다.
    계량 거리 근거가 없으므로 검증된 방향도 한 걸음까지만 허용한다.
    """
    result = {"status": "unavailable", "left": False, "right": False,
              "max_steps": 1, "image_height": shape[0], "image_width": shape[1]}
    if (class_map is None or not label_ids or "walkable" not in label_ids
            or class_map.shape != tuple(shape[:2])):
        return result
    height, width = shape[:2]
    if height < 20 or width < 20:
        return result
    result["status"] = "available"
    result["fractions"] = {}
    top, bottom = round(height * .88), round(height * .96)
    walkable = (class_map[top:bottom] == label_ids["walkable"]).astype(np.uint8)
    _, labels = cv2.connectedComponents(walkable, connectivity=4)
    foot_label = int(labels[round(height * .92) - top, round(width * .50)])
    if foot_label == 0:
        return result
    # 발이 놓인 지면과 연결되지 않은 건너편 보행 영역은 후보에 포함하지 않는다.
    connected = labels == foot_label
    # 가상 발(.50, .92)에서 좌우 후보까지 발 폭·높이를 포함한 이동 띠.
    for side, (left, right) in {"left": (.20, .54), "right": (.46, .80)}.items():
        patch = connected[:, round(width * left):round(width * right)]
        if not patch.size:
            continue
        fraction = float(patch.mean())
        result["fractions"][side] = float(walkable[:, round(width * left):round(width * right)].mean())
        result[side] = bool(fraction >= .95 and patch.mean(axis=0).min() >= .90
                            and patch.mean(axis=1).min() >= .90)
    return result


def direction_safety(prediction, image_width):
    """음성 대상 여부와 별개로 검출된 객체가 이동 후보를 막는지 확인한다.

    근거리 ROI의 측면 및 발 높이의 이동 띠를 검사한다. 중앙 사람의 박스나
    먼 측면 객체만으로 회피 방향을 막지 않는다. 유효한 운동 이력이 있으면
    0.8초까지의 진입도 검사하지만 새 프레임을 기다리지는 않는다.
    """
    ground = prediction.get("direction_ground") or {}
    result = {side: {"allowed": False, "reason": "ground_unavailable", "blockers": []}
              for side in ("left", "right")}
    if ground.get("status") != "available":
        return result
    height = ground.get("image_height", 0)
    if height <= 0 or image_width <= 0:
        return result
    near_top = min((point[1] for point in (prediction.get("roi") or {}).get(
        "immediate_polygon", [[0, .65]])), default=.65)
    # 중앙 경계는 행동 판단과 동일하게 두고, 화면 맨 끝의 물체는 한 걸음
    # 이동할 후보 띠(.15~.85)와 실제로 겹칠 때만 점유로 판단한다.
    for side, (left, right) in {
            "left": (.15, GUIDANCE["walking_left_max_ratio"]),
            "right": (GUIDANCE["walking_right_min_ratio"], .85)}.items():
        state = result[side]
        state.update(allowed=bool(ground.get(side)), reason="clear" if ground.get(side)
                     else "ground_blocked")
        edge = prediction.get("direction_walkability") or {}
        fraction, minimum = edge.get(side), edge.get("minimum")
        if (edge.get("status") == "available"
                and isinstance(fraction, (int, float)) and isinstance(minimum, (int, float))
                and math.isfinite(fraction) and math.isfinite(minimum) and fraction < minimum):
            state.update(allowed=False, reason="edge_not_walkable")
        for index, item in enumerate(prediction.get("detections", [])):
            if item.get("class_name") == "traffic_light":
                continue
            try:
                x1, _, x2, bottom = map(float, item["xyxy"])
                x1, x2, bottom = x1 / image_width, x2 / image_width, bottom / height
                if not all(math.isfinite(v) for v in (x1, x2, bottom)) or x1 >= x2:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            motion = item.get("motion") or {}
            velocity = motion.get("velocity_norm_per_s")
            vx = vy = 0.0
            if motion.get("quality") == "valid" and velocity is not None:
                vx, vy = velocity
                if not all(math.isfinite(v) for v in (vx, vy)):
                    vx = vy = 0.0
            for time in (0, .4, .8):
                start, end, foot_y = x1 + vx * time, x2 + vx * time, bottom + vy * time
                occupied = warning_directions({"xyxy": [start, 0, end, foot_y]}, 1)
                side_overlap = min(end, right) - max(start, left)
                sweep_left, sweep_right = (.20, .54) if side == "left" else (.46, .80)
                foot_overlap = min(end, sweep_right) - max(start, sweep_left)
                if (side in occupied and side_overlap > .01 and foot_y >= near_top
                        or foot_overlap > .01 and foot_y >= .88):
                    state.update(allowed=False, reason="object_in_route")
                    state["blockers"].append(item.get("hazard_id") or item.get("event_id")
                                             or item.get("track_id") or f"detection:{index}")
                    break
    return result
