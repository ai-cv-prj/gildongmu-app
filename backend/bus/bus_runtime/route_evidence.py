"""목표 번호와 독립적인 crop 품질 및 등록번호/전화번호 문맥 검사."""
import re

import cv2
import numpy as np


EVIDENCE_FILTER = {
    'internal_boundary_margin_px': 1,
    'plate_min_relative_y': .55,
    'plate_min_yellow_fraction': .45,
    'plate_min_saturation': 25,
    'plate_min_value': 30,
    'plate_min_light_border_fraction': .6,
    'legacy_plate_min_relative_y': .72,
    'legacy_plate_min_saturation': 100,
    'context_max_relative_height': .065,
    'context_min_relative_y': .25,
    'context_min_numeric_score': .75,
    'context_phone_prefix_and_separator_score': .9,
    'min_aspect_per_character_for_3plus': .28,
    'note': 'target-independent heuristics; context can reject but never supply route evidence',
}


def crop_rejection(rgb, box, bus_box, search_box):
    a, b, c, d = box
    x, y, z, w = search_box
    if min(a-x, b-y, z-c, w-d) <= 1:
        return 'search_boundary_clipped'
    bx, by, bz, bw = bus_box
    crop = rgb[b:d, a:c]
    if not crop.size:
        return 'empty_crop'
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    # 노출 과다/그림자에서 번호판의 노란 바탕은 채도가 크게 낮아진다.
    # 검출 박스가 차체 밑의 그림자/가림까지 포함하므로 하단 28%만 보면 놓친다.
    # 위치만으로 하단 PRINT를 삭제하지 않고 노란 바탕 점유율을 함께 요구한다.
    yellow = ((hsv[:, :, 0] >= 12) & (hsv[:, :, 0] <= 40)
              & (hsv[:, :, 1] >= EVIDENCE_FILTER['plate_min_saturation'])
              & (hsv[:, :, 2] >= EVIDENCE_FILTER['plate_min_value']))
    relative_y = ((b+d)/2-by)/max(1, bw-by)
    # 기존의 진한 노란 하단 번호판 규칙은 유지한다.
    legacy_yellow = yellow & (hsv[:, :, 1] >= EVIDENCE_FILTER['legacy_plate_min_saturation'])
    if relative_y >= .72 and np.mean(legacy_yellow) >= .45:
        return 'likely_registration_plate'
    if (relative_y >= EVIDENCE_FILTER['plate_min_relative_y']
            and np.mean(yellow) >= EVIDENCE_FILTER['plate_min_yellow_fraction']):
        # 낮은 채도 범위를 확장할 때는 밝은 바탕/어두운 글씨인지 확인한다.
        # 녹색 차체의 흰 PRINT, 어두운 바탕의 노란 LED는 경계가 어둡다.
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        threshold, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
        if np.mean(border > threshold) >= EVIDENCE_FILTER['plate_min_light_border_fraction']:
            return 'likely_registration_plate'
    return ''


def context_boxes(prediction, shape):
    """작은 차체 숫자의 좌우 문맥. 버스 박스 밖도 읽되 거절 용도로만 사용한다."""
    p = prediction
    text = p['text']
    if (not p['eligible'] or not re.fullmatch(r'[0-9]{4}', text)
            or not p['token_scores'] or min(p['token_scores']) < EVIDENCE_FILTER['context_min_numeric_score']):
        return []
    a, b, c, d = p['text_box']
    _, by, _, bw = p['bus_box']
    height = d-b
    if (height/max(1, bw-by) > EVIDENCE_FILTER['context_max_relative_height']
            or ((b+d)/2-by)/max(1, bw-by) < EVIDENCE_FILTER['context_min_relative_y']):
        return []
    h, w = shape[:2]
    pad = max(1, round(height*.25))
    return [(max(0, a-left*height), max(0, b-pad), min(w, c+right*height), min(h, d+pad))
            for left, right in [(1, 4), (4, 1)]]


def context_rejection(text, context_text, scores):
    # 전체 전화번호를 확정할 필요는 없다. 원래 4자리와 인접 '-'가 확실하고
    # 반대편에 추가 4자리가 있는 경우 그 4자리를 독립 노선으로 사용할 수 없다.
    # 문맥 OCR에서 나온 숫자는 목표에 맞추거나 노선 후보에 추가하지 않는다.
    match = re.fullmatch(r'([0-9]{4})-([0-9]{4})', context_text)
    if match is None or len(scores) < len(context_text)+1:
        return ''
    for group, start in [(1, 0), (2, 5)]:
        if match[group] == text and min([*scores[start:start+4], scores[4]]) >= .9:
            return 'embedded_phone_number'
    return ''


def text_rejection(text, box):
    if len(text) >= 3 and text.isascii() and text.isalnum():
        a, b, c, d = box
        if (c-a)/max(1, d-b)/len(text) < .28:
            return 'implausible_character_density'
    return ''
