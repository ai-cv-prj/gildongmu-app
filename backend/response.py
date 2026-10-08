"""
file_path: backend/response.py

추론 결과를 휴대폰 오버레이와 음성 안내에 필요한 응답으로 변환한다.
"""

import base64

import cv2
import numpy as np


# 영상 좌표의 객체 박스를 화면 비율 좌표로 변환
def normalize_detections(items, width, height):
    """원본 픽셀 좌표의 객체 목록을 0~1 화면 좌표로 바꾼다."""
    normalized = []
    for item in items:
        box = item.get("xyxy")
        if box is None:
            continue
        converted = {key: item.get(key) for key in (
            "class_name", "display_label", "confidence", "track_id", "signal_state",
            "selection_status", "alert_level", "risk_level", "detection_index",
            "event_id", "hazard_id", "voice_suppressed_reason",
            "risk_suppressed_reason",
        ) if key in item}
        converted["xyxy"] = [max(0, min(1, float(value) / scale))
                             for value, scale in zip(box, (width, height, width, height))]
        normalized.append(converted)
    return normalized


# 모바일 화면에 합성할 분할 마스크 압축
def encode_mask(class_map, label_ids, width=320):
    """보행가능영역과 횡단보도만 반투명 PNG로 인코딩한다."""
    height, original_width = class_map.shape
    target_width = min(width, original_width)
    target_height = max(1, round(height * target_width / original_width))
    small = cv2.resize(class_map.astype(np.uint8), (target_width, target_height),
                       interpolation=cv2.INTER_NEAREST)
    rgba = np.zeros((target_height, target_width, 4), dtype=np.uint8)
    rgba[small == label_ids["walkable"]] = (50, 185, 85, 140)
    rgba[small == label_ids["crosswalk"]] = (208, 80, 205, 140)
    success, png = cv2.imencode(".png", rgba)
    if not success:
        raise RuntimeError("분할 마스크를 PNG로 인코딩하지 못했습니다.")
    return base64.b64encode(png.tobytes()).decode("ascii")


# 브라우저 안내 정책이 읽을 단일 프레임 응답 작성
def make_response(session_id, frame_id, captured_at_ms, frame, risk, signal, crosswalk,
                  walking_surface, class_map, label_ids, inference_ms):
    """보행·신호·횡단보도 안전 결과와 같은 프레임의 마스크를 포함한다."""
    height, width = frame.shape[:2]
    return {
        "session_id": session_id, "frame_id": frame_id,
        "captured_at_ms": captured_at_ms, "image_width": width, "image_height": height,
        "walking": {
            "detections": normalize_detections(risk["detections"], width, height),
            "event": {
                "type": "walking_warning", "level": risk["level"],
                "enabled": risk.get("enabled", True),
                "warning_text": risk["warning_text"], "roi": risk["roi"],
                "camera_view": risk["camera_view"],
                "stationarity": risk.get("stationarity"),
                "last_action": risk.get("last_action"),
                "voice_action": risk.get("voice_action"),
                "voice_text": risk.get("voice_text"),
                "voice_event": risk.get("voice_event"),
                "voice_clear": risk.get("voice_clear", False),
            },
            "mask_png": encode_mask(class_map, label_ids),
        },
        "traffic": {
            "detections": normalize_detections(signal["detections"], width, height),
            "crosswalks": normalize_detections(signal["crosswalks"], width, height),
            "event": {
                "type": "traffic_signal", "signal_state": signal["signal_state"],
                "selected_detection_index": signal["selected_detection_index"],
                "candidate_detection_index": signal["candidate_detection_index"],
                "voice_gate": signal.get("voice_gate"),
            },
        },
        "crosswalk": {"event": crosswalk},
        "walking_surface": {"event": walking_surface},
        "stop_proximity": risk.get("stop_proximity"),
        "boarding": risk.get("boarding"),
        "inference_ms": inference_ms,
    }
