"""
file_path: src/crosswalk_visualization.py

횡단보도 좌우 경계와 가상 사용자 위치 및 판정 상태를 결과 영상에 표시한다.
영상으로 임계값을 조정할 때 판정 근거를 함께 확인하기 위한 디버그 화면이다.
"""

import cv2


BOTTOM_ROI_COLOR = (229, 93, 155)
SAFE_BOUNDARY_COLOR = (224, 107, 241)
EXIT_BOUNDARY_COLOR = (91, 67, 255)


# 횡단보도 안전 판정 오버레이
def draw_crosswalk_safety(frame, event):
    """정규화된 발 위치와 좌우 경계 및 현재 상태를 원본 크기에 맞춰 그린다."""
    result = frame.copy()
    geometry = (event or {}).get("geometry") or {}
    height, width = result.shape[:2]
    roi = (event or {}).get("crosswalk_roi") or {}
    roi_values = (roi.get("left"), roi.get("top"), roi.get("right"), roi.get("bottom"))
    if all(isinstance(value, (int, float)) for value in roi_values):
        left, top, right, bottom = roi_values
        first = (round(left * width), round(top * height))
        second = (round(right * width), round(bottom * height))
        tint = result.copy()
        cv2.rectangle(tint, first, second, BOTTOM_ROI_COLOR, -1)
        result = cv2.addWeighted(tint, 0.05, result, 0.95, 0)
        cv2.rectangle(result, first, second, BOTTOM_ROI_COLOR, max(2, round(width / 320)))
    foot_y = geometry.get("foot_y")
    values = (geometry.get("left_x"), geometry.get("right_x"), geometry.get("foot_x"), foot_y)
    if all(isinstance(value, (int, float)) for value in values):
        left, right, foot, y = values
        y_px = round(y * height)
        color = (EXIT_BOUNDARY_COLOR if str(event.get("status", "")).startswith("outside")
                 else SAFE_BOUNDARY_COLOR)
        cv2.line(result, (round(left * width), y_px), (round(right * width), y_px), color, 4)
        cv2.circle(result, (round(foot * width), y_px), 7, (255, 255, 255), -1)
    status = (event or {}).get("status")
    if status and status != "disabled":
        text = f"CROSSWALK: {status}"
        font, scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, max(.45, width / 760), 1
        (text_width, text_height), baseline = cv2.getTextSize(
            text, font, scale, thickness)
        padding = 8
        left = max(0, width - text_width - padding * 2 - 8)
        top = 8
        bottom = top + text_height + baseline + padding * 2
        cv2.rectangle(result, (left, top), (width - 8, bottom), (24, 24, 24), -1)
        cv2.putText(result, text, (left + padding, bottom - baseline - padding),
                    font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return result
