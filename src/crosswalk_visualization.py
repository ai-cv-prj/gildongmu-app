"""
file_path: src/crosswalk_visualization.py

횡단보도 좌우 경계와 가상 사용자 위치 및 판정 상태를 결과 영상에 표시한다.
영상으로 임계값을 조정할 때 판정 근거를 함께 확인하기 위한 디버그 화면이다.
"""

import cv2


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
        cv2.rectangle(result, (round(left * width), round(top * height)),
                      (round(right * width), round(bottom * height)), (229, 93, 155), 3)
    foot_y = geometry.get("foot_y")
    values = (geometry.get("left_x"), geometry.get("right_x"), geometry.get("foot_x"), foot_y)
    if all(isinstance(value, (int, float)) for value in values):
        left, right, foot, y = values
        y_px = round(y * height)
        color = (30, 30, 245) if str(event.get("status", "")).startswith("outside") else (220, 90, 210)
        cv2.line(result, (round(left * width), y_px), (round(right * width), y_px), color, 4)
        cv2.circle(result, (round(foot * width), y_px), 7, (255, 255, 255), -1)
    status = (event or {}).get("status")
    if status and status not in ("search", "disabled"):
        cv2.putText(result, f"CROSSWALK:{status}", (16, max(28, height - 18)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
    return result
