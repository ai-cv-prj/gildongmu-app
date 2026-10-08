"""
file_path: src/traffic_voice_gate.py

파란 예상 경로 ROI 안의 횡단보도 마스크 비율로 신호 음성 허용 여부를 계산한다.
실시간과 저장 영상이 같은 원본 프레임의 ROI·분할 결과를 사용한다.
"""

import cv2
import numpy as np

from src.settings import load_audio_settings


MIN_CROSSWALK_FRACTION = load_audio_settings()["guidance"]["traffic_crosswalk_roi_min_fraction"]


# 파란 ROI의 횡단보도 비율로 신호 음성 허용 여부 계산
def traffic_voice_gate(class_map, label_ids, shape, roi):
    """파란 ROI의 합집합에서 횡단보도 픽셀 비율을 구하고 근거가 없으면 음성을 막는다."""
    result = {"allowed": False, "crosswalk_fraction": None,
              "threshold": MIN_CROSSWALK_FRACTION, "reason": "mask_unavailable"}
    if (class_map is None or not label_ids or "crosswalk" not in label_ids
            or class_map.shape != tuple(shape[:2])):
        return result
    result["reason"] = "roi_unavailable"
    polygons = (roi or {}).get("corridor_polygons")
    if polygons is None:
        polygons = [(roi or {}).get("corridor_polygon")]
    if not polygons:
        return result
    height, width = shape[:2]
    if height <= 0 or width <= 0:
        return result
    roi_mask = np.zeros((height, width), dtype=np.uint8)
    for polygon in polygons:
        try:
            points = np.asarray(polygon, dtype=float)
        except (TypeError, ValueError):
            return result
        if (points.ndim != 2 or points.shape[1] != 2 or len(points) < 3
                or not np.isfinite(points).all() or (points < 0).any() or (points > 1).any()):
            return result
        pixels = np.rint(points * [width - 1, height - 1]).astype(np.int32)
        if cv2.contourArea(pixels) <= 0:
            return result
        # 겹치는 후보 ROI를 여러 번 세거나 다각형 사이에 구멍을 만들지 않는다.
        cv2.fillPoly(roi_mask, [pixels], 1)
    inside = roi_mask.astype(bool)
    if not inside.any():
        return result
    fraction = float(np.mean(class_map[inside] == label_ids["crosswalk"]))
    allowed = fraction >= MIN_CROSSWALK_FRACTION
    result.update(allowed=allowed, crosswalk_fraction=fraction,
                  reason="crosswalk_in_blue_roi" if allowed else "insufficient_crosswalk_in_blue_roi")
    return result
