"""
file_path: tests/test_traffic_voice_gate.py

파란 ROI 내 횡단보도 면적의 5% 경계와 신호 음성 차단·재개를 검증한다.
신호 탐지 결과를 유지하면서 실시간과 저장 영상에 같은 허용 조건을 적용한다.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest
import yaml

from backend.response import make_response
from src.settings import load_audio_settings
from src.traffic_voice import TrafficVoice
from src.traffic_voice_gate import traffic_voice_gate
from src.voice_priority import prioritize_voice_events
from test_field_guidance import stop_model
from test_risk import FRAME
from test_traffic_voice import signal


LABELS = {"non_walkable": 0, "walkable": 1, "crosswalk": 2}
FULL_ROI = {"corridor_polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}


# 횡단보도 5% 경계 확인
@pytest.mark.parametrize("count,allowed", [(0, False), (4, False), (5, True), (6, True)])
def test_five_percent_threshold(count, allowed):
    """파란 ROI의 100픽셀 중 횡단보도가 정확히 5픽셀일 때부터 허용한다."""
    mask = np.ones((10, 10), dtype=np.uint8)
    mask.flat[:count] = 2
    gate = traffic_voice_gate(mask, LABELS, (10, 10, 3), FULL_ROI)
    assert gate["allowed"] is allowed
    assert gate["crosswalk_fraction"] == count / 100
    assert gate["threshold"] == .05


# 화면 전체가 아닌 파란 ROI 면적을 분모로 사용
def test_crosswalk_outside_blue_roi_does_not_enable_speech():
    """ROI 밖 횡단보도는 무시하고 ROI 내부 면적의 5%만으로 허용한다."""
    mask = np.ones((100, 100), dtype=np.uint8)
    mask[:, 50:] = 2
    roi = {"corridor_polygon": [[0, 0], [49 / 99, 0], [49 / 99, 1], [0, 1]]}
    assert traffic_voice_gate(mask, LABELS, FRAME.shape, roi)["allowed"] is False
    mask[:5, :50] = 2
    gate = traffic_voice_gate(mask, LABELS, FRAME.shape, roi)
    assert gate["crosswalk_fraction"] == .05
    assert gate["allowed"] is True


# ROI 보정 후보의 중복 영역 제거
def test_multiple_blue_polygons_use_union_area():
    """파란 ROI가 두 개일 때 겹친 부분을 중복 계산하거나 구멍으로 처리하지 않는다."""
    roi = {"corridor_polygons": [
        [[0, 0], [49 / 99, 0], [49 / 99, 1], [0, 1]],
        [[25 / 99, 0], [74 / 99, 0], [74 / 99, 1], [25 / 99, 1]],
    ]}
    mask = np.ones((100, 100), dtype=np.uint8)
    mask[:5, :75] = 2
    gate = traffic_voice_gate(mask, LABELS, FRAME.shape, roi)
    assert gate["crosswalk_fraction"] == .05
    assert gate["allowed"] is True


# 마스크와 ROI의 불확실한 입력 차단
@pytest.mark.parametrize("mask,labels,roi", [
    (None, LABELS, FULL_ROI),
    (np.ones((5, 5)), LABELS, FULL_ROI),
    (np.ones((10, 10)), {"walkable": 1}, FULL_ROI),
    (np.full((10, 10), 2), LABELS, None),
    (np.full((10, 10), 2), LABELS, {"corridor_polygons": []}),
    (np.full((10, 10), 2), LABELS, {"corridor_polygon": [[0, 0], [0, 0], [0, 0]]}),
])
def test_unavailable_evidence_blocks_signal_voice(mask, labels, roi):
    """횡단보도나 파란 ROI를 확인할 수 없으면 신호 음성을 허용하지 않는다."""
    gate = traffic_voice_gate(mask, labels, (10, 10, 3), roi)
    assert gate["allowed"] is False
    assert gate["crosswalk_fraction"] is None


# 원거리 신호와 ROI 재진입 처리 확인
@pytest.mark.parametrize("color", ["red", "green"])
def test_signal_requires_gate_then_fresh_confirmation(color):
    """ROI 밖에서는 침묵하고 재진입하면 같은 색도 3프레임·400ms 후 다시 안내한다."""
    voice = TrafficVoice()
    for index in range(3):
        voice.observe(signal(color, allowed=False), index + 1, index * .2)
    assert voice.events == []
    for index in range(3, 6):
        voice.observe(signal(color), index + 1, index * .2)
    clip = "red.mp3" if color == "red" else "green-initial-wait.mp3"
    assert voice.events == [(1.0, clip)]
    voice.observe(signal(color, allowed=False), 7, 1.2)
    voice.observe(signal(target=None, allowed=False), 8, 3.5)
    assert voice.events == [(1.0, clip), (1.2, None)]
    voice.observe(signal(color), 9, 3.6)
    voice.observe(signal(color), 10, 3.8)
    assert len(voice.events) == 2
    voice.observe(signal(color), 11, 4.0)
    assert voice.events[-1] == (4.0, clip)


# 횡단 중 짧은 횡단보도 근거 소실 뒤 신호 기억 유지 확인
def test_crossing_keeps_confirmed_signal_through_short_gate_loss():
    """초록불로 바뀐 뒤 건너는 중 짧게 끊기면 다른 대상의 초록불을 대기 문구 없이 안내한다."""
    voice = TrafficVoice()
    for index in range(3):
        voice.observe(signal("red"), index + 1, index * .2, crossing_active=True)
    for index in range(3, 6):
        voice.observe(signal("green"), index + 1, index * .2, crossing_active=True)
    assert [clip for _, clip in voice.events] == ["red.mp3", "green-changed.mp3"]
    voice.observe(signal("green", allowed=False), 7, 1.2, crossing_active=True)
    voice.observe(signal("green", allowed=False), 8, 1.4, crossing_active=True)
    assert voice.events[-1] == (1.2, None)
    for index in range(8, 11):
        voice.observe(signal("green", target=2), index + 1, index * .2, crossing_active=True)
    assert voice.events[-1] == (pytest.approx(2.0), "green.mp3")


# 빨간불·대기 안내 뒤 횡단 상태에서도 신호 기억 해제 확인
@pytest.mark.parametrize("first,expected", [("red", "green-initial-wait.mp3"),
                                            ("green", "green-initial-wait.mp3")])
def test_red_or_wait_announcement_still_resets_signal(first, expected):
    """빨간불이나 대기 안내 뒤에는 횡단 상태여도 끊긴 뒤 다른 대상의 초록불을 대기 안내한다."""
    voice = TrafficVoice()
    for index in range(3):
        voice.observe(signal(first), index + 1, index * .2, crossing_active=True)
    voice.observe(signal(first, allowed=False), 4, .6, crossing_active=True)
    for index in range(4, 7):
        voice.observe(signal("green", target=2), index + 1, index * .2, crossing_active=True)
    assert voice.events[-1] == (pytest.approx(1.2), expected)


# 횡단 종료 또는 긴 근거 소실 뒤 새 신호 확인
@pytest.mark.parametrize("crossing_active,lost_s", [(False, .2), (True, 5.0)])
def test_finished_crossing_or_long_gate_loss_resets_signal(crossing_active, lost_s):
    """횡단이 끝났거나 근거가 설정 시간 이상 끊기면 다시 잡은 초록불을 처음처럼 대기 안내한다."""
    voice = TrafficVoice()
    for index in range(3):
        voice.observe(signal("red"), index + 1, index * .2, crossing_active=True)
    for index in range(3, 6):
        voice.observe(signal("green"), index + 1, index * .2, crossing_active=True)
    voice.observe(signal("green", allowed=False), 7, 1.2, crossing_active=crossing_active)
    voice.observe(signal("green", allowed=False), 8, 1.2 + lost_s, crossing_active=crossing_active)
    start = 1.4 + lost_s
    for index in range(3):
        voice.observe(signal("green", target=2), index + 9, start + index * .2,
                      crossing_active=crossing_active)
    assert voice.events[-1] == (pytest.approx(start + .4), "green-initial-wait.mp3")


# 순간 오인식이 섞인 색상 다수결 확인
def test_majority_confirms_change_despite_single_flicker():
    """초록 사이에 빨강 한 프레임이 섞여도 최근 다수 색상으로 전환을 확정한다."""
    voice = TrafficVoice()
    for index, color in enumerate(["red", "red", "red", "green", "green", "red", "green"]):
        voice.observe(signal(color), index + 1, index * .2)
    assert [clip for _, clip in voice.events] == ["red.mp3", "green-changed.mp3"]


# 짧은 신호 소실 뒤 직전 색상 기억 확인
@pytest.mark.parametrize("unknown_until,expected", [(2.6, "green-changed.mp3"), (6.0, "green.mp3")])
def test_recent_color_memory_turns_recovery_into_change(unknown_until, expected):
    """신호를 놓쳐도 5초 안에 같은 대상의 다른 색을 확인하면 전환으로 안내한다."""
    voice = TrafficVoice()
    for index in range(3):
        voice.observe(signal("red"), index + 1, index * .2)
    frame, time_s = 4, .6
    while time_s < unknown_until - 1e-9:
        voice.observe(signal(target=None), frame, time_s)
        frame, time_s = frame + 1, round(time_s + .2, 1)
    for index in range(3):
        voice.observe(signal("green"), frame + index, time_s + index * .2)
    assert [clip for _, clip in voice.events] == ["red.mp3", "missing.mp3", expected]


# 빨간불 관측 중 다른 신호등 초록 확인
def test_other_signal_green_after_red_is_unverified():
    """빨간불을 보던 중 다른 대상의 초록을 잡으면 전환을 보지 못한 초록으로 대기 안내한다."""
    voice = TrafficVoice()
    for index in range(3):
        voice.observe(signal("red"), index + 1, index * .2)
    for index in range(3, 6):
        voice.observe(signal("green", target=2), index + 1, index * .2)
    assert [clip for _, clip in voice.events] == ["red.mp3", "green-initial-wait.mp3"]


# ROI 밖 신호색 변화를 재진입 전환으로 오인하지 않기
def test_color_changes_outside_roi_are_not_announced():
    """차단 중 색상 변화와 소실 안내를 생성하지 않고 재진입 시 최초 안내를 사용한다."""
    voice = TrafficVoice()
    for index in range(3):
        voice.observe(signal("red"), index + 1, index * .2)
    voice.observe(signal("green", allowed=False), 4, .6)
    voice.observe(signal(target=None, allowed=False), 5, 3)
    for index, time_s in enumerate((3.2, 3.4, 3.6), 6):
        voice.observe(signal("green"), index, time_s)
    assert [clip for _, clip in voice.events] == ["red.mp3", None, "green-initial-wait.mp3"]


# 누락된 허용 정보는 차단
def test_missing_gate_is_not_implicitly_allowed():
    """횡단보도 판정이 없는 신호 결과는 음성을 생성하지 않는다."""
    voice = TrafficVoice()
    result = signal()
    result.pop("voice_gate")
    for index in range(4):
        voice.observe(result, index + 1, index)
    assert voice.events == []


# 저장 영상 신호 음성만 취소
def test_gate_closure_cancels_only_traffic_source():
    """ROI 소실은 진행 중인 신호 음성을 자르되 장애물 정지는 취소하지 않는다."""
    traffic = [(0, "red.mp3"), (.2, None)]
    with patch("src.voice_priority.clip_duration", return_value=1):
        assert prioritize_voice_events([], traffic, []) == [(0, "red.mp3"), (.2, None)]
        assert prioritize_voice_events([(.1, "walking-stop.mp3")], traffic, []) == [
            (0, "red.mp3"), (.1, "walking-stop.mp3")]


# 실제 실시간 추론과 응답에서 같은 ROI 조건 전달
@pytest.mark.parametrize("count,allowed", [(499, False), (500, True)])
def test_realtime_response_exposes_gate_without_hiding_signal(count, allowed):
    """실시간 모델 연결과 API 응답에 5% 조건을 전달하면서 신호 탐지를 보존한다."""
    model, _, _ = stop_model()
    mask = np.ones(FRAME.shape[:2], dtype=np.uint8)
    mask.flat[:count] = 2
    model.segmenter.predict = Mock(return_value=mask)
    model.risk = SimpleNamespace(
        update=Mock(return_value={"roi": FULL_ROI, "detections": [], "level": "monitor",
                                  "warning_text": "", "camera_view": {"status": "clear"}}),
        add_sidewalk_context=Mock())
    result = signal()
    result.update(crosswalks=[], candidate_detection_index=None)
    result["detections"][0].update(xyxy=[40, 0, 60, 20], class_name="pedestrian_signal")
    model.traffic.predict = Mock(return_value=result)
    risk, traffic, crosswalk, surface, class_map, labels, elapsed = model.predict(FRAME, 1, 1000)
    response = make_response("session", 1, 1000, FRAME, risk, traffic, crosswalk,
                             surface, class_map, labels, elapsed)
    event = response["traffic"]["event"]
    assert event["voice_gate"]["allowed"] is allowed
    assert event["voice_gate"]["crosswalk_fraction"] == count / 10000
    assert event["signal_state"] == "red"
    assert event["selected_detection_index"] == 0
    assert len(response["traffic"]["detections"]) == 1


# YAML 비율 설정의 잘못된 값 거부
@pytest.mark.parametrize("value", [0, -.01, 1.01, True, float("nan")])
def test_invalid_crosswalk_fraction_setting_is_rejected(tmp_path, value):
    """0 초과 1 이하의 유한한 숫자만 횡단보도 비율로 허용한다."""
    config = load_audio_settings()
    config["guidance"]["traffic_crosswalk_roi_min_fraction"] = value
    path = tmp_path / "audio.yaml"
    path.write_text(yaml.safe_dump({"audio": config}), encoding="utf-8")
    with pytest.raises(ValueError, match="traffic_crosswalk_roi_min_fraction"):
        load_audio_settings(path)
