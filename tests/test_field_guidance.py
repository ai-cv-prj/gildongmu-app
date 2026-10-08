"""Regressions for the field failures: early warning, blind routing and stop input."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import yaml

from backend.boarding import Boarding
from backend.inference import RealtimeInference
from backend.stop_proximity import StopProximity
from src.walking_voice import WalkingVoice, walking_action
from src.walking_surface import WalkingSurfaceEngine
from test_risk import FRAME, detection, engine
from test_walking_voice import danger_item, caution_item, prediction

CFG = yaml.safe_load(open("configs/inference.yaml"))["risk"]
LABELS = {"non_walkable": 0, "walkable": 1, "crosswalk": 2}


def test_predicted_person_outside_near_roi_is_spoken():
    item = danger_item(1, [40, 20, 60, 61], geometry={"immediate_overlap": 0},
                       motion={"quality": "valid", "time_to_near_s": .69},
                       reasons=["approaching_near_path"])
    result = prediction(item)
    assert WalkingVoice().observe(result, 100, 0) is not None
    assert result["voice_event"]["action"] == "blocked"


def test_rapid_approach_remains_danger_but_caution_does_not_block_route():
    left = danger_item(1, [20, 40, 41, 73])
    right = danger_item(2, [67, 49, 77, 66], "bicycle",
                        geometry={"immediate_overlap": 0, "corridor_overlap": 1},
                        reasons=["approaching_near_path"], motion={"quality": "valid"})
    assert walking_action(prediction(left, right), 100) == "crowded"
    assert walking_action(prediction(left, caution_item(3, [75, 40, 90, 75])), 100) == "right"


def test_surface_uncertainty_without_any_object_is_silent():
    voice = WalkingVoice()
    result = prediction(level="caution", source="surface")
    result["surface"] = {"alert_level": "caution", "status": "uncertain",
                         "reasons": ["path_observation_uncertain"]}
    assert voice.observe(result, 100, 0) is None
    assert result["voice_action"] is None
    assert "voice_event" not in result


# 측면과 중앙 경계에서 좌우 안내 중복 방지 확인
def test_repeated_boundary_jitter_does_not_repeat_avoidance():
    """측면만 남으면 안내하지 않고 짧은 none 뒤 같은 방향도 반복하지 않는다."""
    voice = WalkingVoice()
    assert voice.observe(prediction(danger_item(1, [20, 20, 43, 80])), 100, 0)[0] == "오른쪽으로 한 걸음"
    for time, box in ((.2, [0, 20, 30, 90]), (.4, [20, 20, 43, 80]),
                      (.6, [0, 20, 30, 90]), (.8, [20, 20, 43, 80])):
        result = prediction(danger_item(1, box))
        assert voice.observe(result, 100, time) is None
        assert result["voice_action"] == ("right" if box[2] == 43 else None)


def test_one_missing_frame_does_not_release_center_obstacle():
    voice = WalkingVoice()
    side = danger_item(2, [5, 20, 15, 90])
    other_side = danger_item(3, [85, 20, 95, 90])
    center = danger_item(1, [45, 20, 55, 90], "bollard")
    assert voice.observe(prediction(center, side, other_side), 100, 0)[0] == "전방 혼잡 주의하세요"
    missing = prediction(side, other_side)
    assert voice.observe(missing, 100, .2) is None
    assert missing["voice_action"] == "crowded"
    assert missing["voice_diagnostics"]["retained_hazards"] == 1


def test_reidentified_tree_replaces_old_position_in_voice_evidence():
    """새 트랙의 나무를 과거 넓은 박스와 함께 혼잡으로 계산하지 않는다."""
    voice = WalkingVoice()
    old = danger_item(1, [20, 20, 80, 95], "tree_trunk", hazard_id="track:1")
    voice.observe(prediction(old), 100, 0)
    new = danger_item(2, [25, 20, 60, 95], "tree_trunk", hazard_id="track:2")
    result = prediction(new)
    voice.observe(result, 100, .2)
    assert result["voice_diagnostics"]["raw_action"] == "right"
    assert result["voice_diagnostics"]["retained_hazards"] == 0
    assert result["voice_event"]["hazard_ids"] == ["tree_trunk:track:2"]


def test_duplicate_tree_boxes_do_not_create_a_crowded_instruction():
    narrow = danger_item(1, [25, 20, 60, 95], "tree_trunk", confidence=.9)
    wide = danger_item(2, [20, 20, 80, 95], "tree_trunk", confidence=.4)
    result = prediction(narrow, wide)
    assert WalkingVoice().observe(result, 100, 0)[0] == "오른쪽으로 두 걸음"
    assert result["voice_diagnostics"]["duplicate_hazards"] == 1
    assert len(result["detections"]) == 2
    assert all(item["alert_level"] == "danger" for item in result["detections"])


def test_distinct_tree_and_overlapping_people_are_not_merged():
    for name, boxes in (("tree_trunk", ([20, 20, 43, 95], [57, 20, 80, 95])),
                        ("person", ([20, 20, 80, 95], [25, 20, 60, 95]))):
        result = prediction(*(danger_item(i, box, name) for i, box in enumerate(boxes, 1)))
        assert WalkingVoice().observe(result, 100, 0)[0] == "전방 혼잡 주의하세요"
        assert result["voice_diagnostics"]["duplicate_hazards"] == 0


def test_observed_tree_release_does_not_keep_old_danger_track():
    voice = WalkingVoice()
    voice.observe(prediction(danger_item(1, [25, 20, 60, 95], "tree_trunk")), 100, 0)
    result = prediction(caution_item(2, [25, 20, 60, 95], class_name="tree_trunk"))
    assert voice.observe(result, 100, .2) is None
    assert result["voice_diagnostics"]["retained_hazards"] == 0
    assert result["voice_action"] is None


def test_emergency_stop_interrupts_pending_direction_change_immediately():
    voice = WalkingVoice()
    voice.observe(prediction(danger_item(1, [20, 20, 43, 95])), 100, 0)
    assert voice.observe(prediction(danger_item(1, [57, 20, 80, 95])), 100, .1) is None
    result = prediction(danger_item(1, [57, 20, 80, 95], reasons=["predicted_moving_conflict"]))
    assert voice.observe(result, 100, .2)[0] == "멈추세요"


def test_repeat_timer_does_not_reannounce_opposite_pending_direction():
    """반대 방향 확인 중 반복 시간이 지나도 이전 방향을 다시 발화하지 않는다."""
    voice = WalkingVoice()
    right = danger_item(1, [20, 20, 43, 95])
    voice.observe(prediction(right), 100, 0)
    for timestamp in (1.0, 2.0, 3.0):
        assert voice.observe(prediction(right), 100, timestamp) is None
    opposite = danger_item(1, [57, 20, 80, 95])
    assert voice.observe(prediction(opposite), 100, 4.0) is None
    assert voice.observe(prediction(opposite), 100, 4.2) is None
    assert voice.observe(prediction(opposite), 100, 4.6)[0] == "왼쪽으로 한 걸음"


def test_side_hazard_never_instructs_typing_user_to_walk():
    result = prediction(danger_item(1, [5, 20, 15, 90]))
    result["boarding"] = {"assumed_stationary": True}
    assert WalkingVoice().observe(result, 100, 0)[0] == "멈추세요"
    assert result["voice_action"] == "stop"


def test_new_hazard_does_not_repeat_an_unchanged_blocked_action():
    voice = WalkingVoice()
    result = prediction(danger_item(1, [45, 20, 55, 90]))
    assert voice.observe(result, 100, 0)[0] == "전방 장애물 주의하세요"
    first = result["voice_event"]["event_id"]
    result["detections"].append(danger_item(2, [48, 20, 58, 90], "car"))
    assert voice.observe(result, 100, .2) is None
    assert result["voice_event"]["event_id"] == first


def stop_model():
    # Exercise the actual realtime orchestration without loading any weights.
    model = RealtimeInference.__new__(RealtimeInference)
    objects = [detection((0, 0, 85, 97), "transit_stop", 20)]
    calls = []
    model.detector = SimpleNamespace(predict=lambda frame: calls.append(True) or objects)
    model._obstacles_suspended = False
    model.segmenter = SimpleNamespace(predict=lambda frame: np.ones(frame.shape[:2], np.uint8), label_ids=LABELS)
    model.risk = engine(None, config={**CFG, "camera_view_guard_enabled": False})
    model.stop_proximity = StopProximity()
    model.boarding = Boarding()
    model.voice = WalkingVoice()
    model.crosswalk_settings = {}
    model.crosswalk = SimpleNamespace(update=lambda *args, **kwargs: {
        "crossing_active": False, "status": "search"})
    model.walking_surface = WalkingSurfaceEngine()
    model.traffic = SimpleNamespace(predict=lambda *args, **kwargs: {
        "detections": [], "signal_state": "unknown", "selected_detection_index": None})
    return model, objects, calls


# 실시간 측면 위험의 신호 상태별 무안내 확인
@pytest.mark.parametrize("state", ["red", "unknown", "green"])
def test_realtime_side_hazard_has_no_action_or_voice_in_any_signal(state):
    """실시간 추론도 신호색이나 전방 횡단보도와 관계없이 측면 위험 음성을 만들지 않는다."""
    model, _, _ = stop_model()
    item = danger_item(1, [5, 20, 15, 90])
    model.risk = SimpleNamespace(update=Mock(side_effect=lambda *args: prediction(item)),
                                 add_sidewalk_context=Mock())
    model.traffic = SimpleNamespace(predict=lambda *args, **kwargs: {
        "detections": [{"track_id": 1}], "signal_state": state,
        "selected_detection_index": 0,
        "crosswalks": [{"crosswalk_status": "used"}],
        "crosswalk_diagnostics": {"eligible_count": 1},
    })
    for index in range(5):
        risk, *_ = model.predict(FRAME, index + 1, 1000 + index * 1000)
        assert risk["last_action"] is None
        assert risk["voice_action"] is None
        assert "voice_event" not in risk
        assert "voice_text" not in risk
        assert risk["detections"][0]["alert_level"] == "danger"
    assert model.voice.events == []


def assert_stopped(risk):
    assert risk["enabled"] is False
    assert risk["detections"] == []
    assert risk["voice_action"] is None
    assert risk["voice_event"] is None
    assert risk["roi"] == {}
    assert risk["boarding"]["obstacle_detection_enabled"] is False


def test_automatic_arrival_disables_obstacles_through_input_and_bus_search():
    model, objects, calls = stop_model()
    for i, ms in enumerate((1000, 1200, 1400), 1):
        risk, _, _, surface, *_ = model.predict(FRAME, i, ms)
    assert model.boarding.status == "awaiting_stop"
    assert_stopped(risk)
    assert surface["enabled"] is False
    assert surface["status"] == "disabled"
    assert surface["voice_text"] is None
    assert surface["roi"] is None
    assert len(calls) == 3
    objects.append(detection((42, 40, 58, 88), "person", 0))
    for i, action in enumerate((None, "stop_announced", "submit", "reopen"), 4):
        if action:
            model.boarding.act(action, 1, "143")
        risk, _, _, surface, *_ = model.predict(FRAME, i, 1000 + i * 200)
        assert_stopped(risk)
        assert surface["enabled"] is False
        assert len(calls) == 3, "Obstacle model must not run at the stop"
    epoch = model.risk.epoch
    model.boarding.act("cancel", 1)
    risk, _, _, surface, *_ = model.predict(FRAME, 8, 2800)
    assert risk["enabled"] is True
    assert surface["enabled"] is True
    assert surface["status"] == "inside"
    assert len(calls) == 4
    assert model.risk.epoch > epoch
    assert risk["detections"][0]["risk_level"] == "danger"


def test_manual_arrival_disables_obstacles_without_detecting_a_stop():
    model, objects, calls = stop_model()
    objects.clear()
    model.boarding.act("arrive")
    risk, _, _, surface, *_ = model.predict(FRAME, 1, 1000)
    assert_stopped(risk)
    assert surface["enabled"] is False
    assert not calls
