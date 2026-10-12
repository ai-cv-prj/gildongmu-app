"""Low-confidence detections must not become remembered walking hazards."""

import pytest

from src.hazard_confirmation import HazardConfirmation
from src.risk_config import risk_config
from src.walking_direction import direction_safety
from src.walking_voice import WalkingVoice
from test_risk import detection, engine, FRAME


POLICY = {"min_frames": 2, "min_duration_s": .30,
          "immediate_confidence": .60, "match_iou": .30, "max_gap_s": .50}
CONFIG = {"hazard_confirmation": {"movable_obstacle": POLICY}}


def obstacle(confidence=.35, box=(40, 55, 60, 90), name="movable_obstacle"):
    return {**detection(box, name=name, cid=25), "confidence": confidence}


def test_single_frame_candidate_has_no_event_voice_or_direction_blocker():
    risk = engine(identity=None, config=CONFIG)
    result = risk.update(FRAME, [obstacle()], 0)
    item = result["detections"][0]
    assert item["confidence"] == .35
    assert item["untrusted_risk_level"] == "danger"
    assert item["confirmation_status"] == "candidate"
    assert item["risk_level"] == item["alert_level"] == "monitor"
    assert item["display_label"] == "obstacle"
    assert item.get("event_id") is None
    assert item["hazard_id"] is None
    assert not risk.alerts.states
    assert not result["events"]
    result["direction_ground"] = {"status": "available", "left": True,
                                  "right": True, "image_height": 100}
    assert all(not side["blockers"] for side in direction_safety(result, 100).values())
    voice = WalkingVoice()
    voice.observe(result, 100, 0)
    assert not voice.events
    assert not voice.hazards
    empty = risk.update(FRAME, [], .2)
    voice.observe(empty, 100, .2)
    assert not empty["advisories"]
    assert not voice.hazards
    assert not voice.events


def test_second_consecutive_detection_confirms_without_tracking_id():
    risk = engine(identity=None, config=CONFIG)
    risk.update(FRAME, [obstacle()], 0)
    result = risk.update(FRAME, [obstacle(box=(41, 55, 61, 90))], .3)
    item = result["detections"][0]
    assert item["confirmation_status"] == "confirmed"
    assert item["confirmation_count"] == 2
    assert item["confirmation_duration_s"] == pytest.approx(.3)
    assert item["alert_level"] == "danger"
    assert item["event_id"] is not None
    assert result["events"][0]["type"] == "raised"
    assert item.get("risk_suppressed_reason") is None


@pytest.mark.parametrize("confidence", [.60, .90])
def test_high_confidence_obstacle_warns_immediately(confidence):
    result = engine(identity=None, config=CONFIG).update(FRAME, [obstacle(confidence)], 0)
    assert result["detections"][0]["confirmation_status"] == "immediate"
    assert result["detections"][0]["alert_level"] == "danger"


@pytest.mark.parametrize("name", ["person", "car", "bicycle", "bollard"])
def test_other_classes_keep_immediate_warning(name):
    result = engine(identity=None, config=CONFIG).update(FRAME, [obstacle(name=name)], 0)
    assert result["detections"][0]["alert_level"] == "danger"
    assert "confirmation_status" not in result["detections"][0]


def test_other_classes_keep_identical_risk_and_alert_results():
    original = engine(identity=None)
    changed = engine(identity=None, config=CONFIG)
    for index in range(3):
        detections = [obstacle(name="person"),
                      obstacle(name="car", box=(10, 20, 30, 40)),
                      obstacle(name="trash_bin", box=(70, 55, 90, 90))]
        timestamp = index * .2
        assert changed.update(FRAME, detections, timestamp) == original.update(
            FRAME, detections, timestamp)


def test_absent_frame_class_change_reset_and_time_gap_break_confirmation():
    risk = engine(identity=None, config=CONFIG)
    for first, middle, last in [(0, .2, .4), (1, 1.2, 1.4)]:
        risk.update(FRAME, [obstacle()], first)
        risk.update(FRAME, [] if first == 0 else [obstacle(name="person")], middle)
        assert risk.update(FRAME, [obstacle()], last)["detections"][0]["confirmation_status"] == "candidate"
    assert risk.update(FRAME, [obstacle()], 2.0)["detections"][0]["confirmation_status"] == "candidate"
    risk.reset()
    assert risk.update(FRAME, [obstacle()], 2.2)["detections"][0]["confirmation_status"] == "candidate"


def test_confirmation_matches_location_and_does_not_reuse_previous_box():
    gate = HazardConfirmation(risk_config(CONFIG))
    gate.update([obstacle()], 0)
    detections = [obstacle(), obstacle(), obstacle(box=(70, 55, 90, 90))]
    gate.update(detections, .3)
    assert sum(item["confirmation_status"] == "confirmed" for item in detections) == 1
    assert detections[2]["confirmation_status"] == "candidate"


def test_invalid_timestamp_cannot_supply_confirmation_history():
    gate = HazardConfirmation(risk_config(CONFIG))
    gate.update([obstacle()], 0, False)
    detections = [obstacle()]
    gate.update(detections, .2)
    assert detections[0]["confirmation_status"] == "candidate"


def test_old_config_retains_original_behavior():
    result = engine(identity=None).update(FRAME, [obstacle()], 0)
    assert result["detections"][0]["alert_level"] == "danger"


@pytest.mark.parametrize("interval", [1 / 30, .1, .22])
@pytest.mark.parametrize("origin", [0., 1791769826.110])
def test_confirmation_duration_is_independent_of_frame_rate_and_epoch(interval, origin):
    risk = engine(identity=None, config=CONFIG)
    for index in range(15):
        elapsed = index * interval
        result = risk.update(FRAME, [obstacle()], origin + elapsed)
        item = result["detections"][0]
        if elapsed + 1e-6 < .3:
            assert item["confirmation_status"] == "candidate"
            assert not result["events"]
            assert not risk.alerts.states
        else:
            assert item["confirmation_status"] == "confirmed"
            assert item["alert_level"] == "danger"
            break
    else:
        pytest.fail("A continuously observed real obstacle must eventually confirm")


def test_absence_and_spatial_change_restart_duration_not_just_count():
    gate = HazardConfirmation(risk_config(CONFIG))
    for time, items in [(0, [obstacle()]), (.2, [obstacle()]), (.3, []),
                        (.4, [obstacle()]), (.5, [obstacle()]),
                        (.6, [obstacle(box=(70, 55, 90, 90))])]:
        gate.update(items, time)
        for item in items:
            assert item["confirmation_status"] == "candidate"
    assert items[0]["confirmation_duration_s"] == 0


@pytest.mark.parametrize("invalid_time", [0, -.1, .8])
def test_nonincreasing_or_stale_capture_time_restarts_duration(invalid_time):
    gate = HazardConfirmation(risk_config(CONFIG))
    gate.update([obstacle()], 0)
    items = [obstacle()]
    gate.update(items, invalid_time)
    assert items[0]["confirmation_status"] == "candidate"
    assert items[0]["confirmation_duration_s"] == 0


@pytest.mark.parametrize("policies", [
    True,
    {"movable_obstacle": {**POLICY, "min_frames": 1}},
    {"movable_obstacle": {**POLICY, "min_frames": True}},
    {"movable_obstacle": {**POLICY, "min_duration_s": -.1}},
    {"movable_obstacle": {**POLICY, "min_duration_s": float("nan")}},
    {"movable_obstacle": {**POLICY, "min_duration_s": True}},
    {"movable_obstacle": {**POLICY, "immediate_confidence": 1.1}},
    {"movable_obstacle": {**POLICY, "match_iou": 0}},
    {"movable_obstacle": {**POLICY, "max_gap_s": 0}},
    {"movable_obstacle": {**POLICY, "typo": 1}},
    {"movable_obstacl": POLICY},
])
def test_invalid_confirmation_config_is_rejected(policies):
    with pytest.raises(ValueError):
        risk_config({"hazard_confirmation": policies})


def test_policy_table_can_confirm_another_class_without_code_branches():
    config = {"hazard_confirmation": {"trash_bin": POLICY}}
    risk = engine(identity=None, config=config)
    first = risk.update(FRAME, [obstacle(name="trash_bin")], 0)
    assert first["detections"][0]["confirmation_status"] == "candidate"
    second = risk.update(FRAME, [obstacle(name="trash_bin")], .3)
    assert second["detections"][0]["confirmation_status"] == "confirmed"
    assert second["detections"][0]["alert_level"] == "danger"
