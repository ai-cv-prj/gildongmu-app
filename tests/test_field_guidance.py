"""Regressions for the field failures: early warning, blind routing and stop input."""
from types import SimpleNamespace

import numpy as np
import yaml

from backend.boarding import Boarding
from backend.inference import RealtimeInference
from backend.stop_proximity import StopProximity
from src.walking_voice import WalkingVoice, walking_action
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
    assert result["voice_event"]["action"] == "stop"


def test_rapid_approach_remains_danger_but_caution_does_not_block_route():
    left = danger_item(1, [28, 40, 41, 73])
    right = danger_item(2, [67, 49, 77, 66], "bicycle",
                        geometry={"immediate_overlap": 0, "corridor_overlap": 1},
                        reasons=["approaching_near_path"], motion={"quality": "valid"})
    assert walking_action(prediction(left, right), 100) == "stop"
    assert walking_action(prediction(left, caution_item(3, [75, 40, 90, 75])), 100) == "right"


def test_surface_uncertainty_without_any_object_is_silent():
    voice = WalkingVoice()
    result = prediction(level="caution", source="surface")
    result["surface"] = {"alert_level": "caution", "status": "uncertain",
                         "reasons": ["path_observation_uncertain"]}
    assert voice.observe(result, 100, 0) is None
    assert result["voice_action"] is None
    assert "voice_event" not in result


def test_repeated_boundary_jitter_does_not_announce_straight_during_avoidance():
    voice = WalkingVoice()
    assert voice.observe(prediction(danger_item(1, [20, 20, 43, 80])), 100, 0)[0] == "오른쪽 이동."
    for time, box in ((.2, [0, 20, 30, 90]), (.4, [20, 20, 43, 80]),
                      (.6, [0, 20, 30, 90]), (.8, [20, 20, 43, 80])):
        result = prediction(danger_item(1, box))
        assert voice.observe(result, 100, time) is None
        assert result["voice_action"] == "right"


def test_one_missing_frame_does_not_release_center_obstacle():
    voice = WalkingVoice()
    side = danger_item(2, [5, 20, 15, 90])
    other_side = danger_item(3, [85, 20, 95, 90])
    assert voice.observe(prediction(danger_item(1, [45, 20, 55, 90]), side, other_side), 100, 0)[0] == "멈추세요."
    missing = prediction(side, other_side)
    assert voice.observe(missing, 100, .2) is None
    assert missing["voice_action"] == "stop"
    assert missing["voice_diagnostics"]["retained_hazards"] == 1


def test_side_hazard_never_instructs_typing_user_to_walk():
    result = prediction(danger_item(1, [5, 20, 15, 90]))
    result["boarding"] = {"assumed_stationary": True}
    assert WalkingVoice().observe(result, 100, 0)[0] == "멈추세요."
    assert result["voice_action"] == "stop"


def test_new_hazard_does_not_repeat_an_unchanged_stop_action():
    voice = WalkingVoice()
    result = prediction(danger_item(1, [45, 20, 55, 90]))
    assert voice.observe(result, 100, 0)[0] == "멈추세요."
    first = result["voice_event"]["event_id"]
    result["detections"].append(danger_item(2, [48, 20, 58, 90], "car"))
    assert voice.observe(result, 100, .2) is None
    assert result["voice_event"]["event_id"] == first


def test_input_suppresses_stop_and_prior_memory_but_keeps_other_hazards():
    # Exercise the actual realtime orchestration without loading any weights.
    model = RealtimeInference.__new__(RealtimeInference)
    objects = [detection((0, 0, 85, 97), "transit_stop", 20)]
    model.detector = SimpleNamespace(predict=lambda frame: objects)
    model.segmenter = SimpleNamespace(predict=lambda frame: np.ones(frame.shape[:2], np.uint8), label_ids=LABELS)
    model.risk = engine(None, config={**CFG, "camera_view_guard_enabled": False})
    model.stop_proximity = StopProximity()
    model.boarding = Boarding()
    model.voice = WalkingVoice()
    model.crosswalk_settings = {}
    model.crosswalk = SimpleNamespace(update=lambda *args, **kwargs: {
        "crossing_active": False, "status": "search"})
    model.walking_surface = SimpleNamespace(update=lambda *args, **kwargs: {})
    model.traffic = SimpleNamespace(predict=lambda *args, **kwargs: {
        "detections": [], "signal_state": "unknown", "selected_detection_index": None})
    for i, ms in enumerate((1000, 1200, 1400), 1):
        risk, *_ = model.predict(FRAME, i, ms)
    assert model.boarding.status == "awaiting_stop"
    assert risk["detections"][0]["risk_level"] == "danger"
    model.boarding.act("stop_announced", 1)
    risk, *_ = model.predict(FRAME, 4, 1600)
    assert risk["detections"][0]["risk_level"] is None
    assert risk["voice_action"] is None
    assert not risk["advisories"]
    objects.append(detection((42, 40, 58, 88), "person", 0))
    risk, *_ = model.predict(FRAME, 5, 1800)
    assert risk["detections"][1]["alert_level"] == "danger"
    assert risk["voice_action"] == "stop"
    model.boarding.act("cancel", 1)
    risk, *_ = model.predict(FRAME, 6, 2000)
    assert risk["detections"][0]["risk_level"] == "danger"
