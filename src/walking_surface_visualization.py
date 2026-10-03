"""
file_path: src/walking_surface_visualization.py

보행로 이탈 판정에 사용하는 가상 발 ROI와 현재 상태를 결과 영상에 표시한다.
녹색 ROI 테두리와 오른쪽 위 상태 배지를 실시간 화면 구성과 맞춘다.
"""

import cv2


FOOT_ROI_COLOR = (80, 230, 90)


# 보행로 판정 오버레이
def draw_walking_surface(frame, event):
    """가상 발의 녹색 ROI 테두리와 보행로 상태 배지를 그린다."""
    result = frame.copy()
    height, width = result.shape[:2]
    roi = (event or {}).get("roi") or {}
    values = (roi.get("left"), roi.get("top"), roi.get("right"), roi.get("bottom"))
    if all(isinstance(value, (int, float)) for value in values):
        left, top, right, bottom = values
        cv2.rectangle(
            result, (round(left * width), round(top * height)),
            (round(right * width), round(bottom * height)), FOOT_ROI_COLOR,
            max(2, round(width / 320)),
        )
    status = (event or {}).get("status")
    if status and status != "disabled":
        text = f"WALKWAY: {status}"
        font, scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, max(.45, width / 760), 1
        (text_width, text_height), baseline = cv2.getTextSize(text, font, scale, thickness)
        padding = 8
        left = max(0, width - text_width - padding * 2 - 8)
        top = max(42, round(height / 24) + 16)
        bottom = top + text_height + baseline + padding * 2
        cv2.rectangle(result, (left, top), (width - 8, bottom), (24, 24, 24), -1)
        cv2.putText(result, text, (left + padding, bottom - baseline - padding),
                    font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return result
