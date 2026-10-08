"""Tall static obstacles retain ground prediction when only their top is cropped."""

import numpy as np
import pytest
import yaml

from src.walking_voice import (
    WalkingVoice, apply_obstacle_voice_suppression, rapid_approach_hazard)
from test_risk import FRAME, detection, engine


SETTINGS = yaml.safe_load(open("configs/inference.yaml"))
CFG = SETTINGS["risk"]
LABELS = {"non_walkable": 0, "walkable": 1, "crosswalk": 2}


def approach(*, name="tree_trunk", identity=1, stable=True, config=None,
             bounds=(40, 60), tops=(0, 0, 0), bottoms=(70, 72, 74),
             times=(0, .1, .2), timestamp_valid=True, mask=None):
    assessor = engine(identity, stable, {**CFG, "camera_view_guard_enabled": False,
                                        **(config or {})})
    for timestamp, top, bottom in zip(times, tops, bottoms):
        result = assessor.update(
            FRAME, [detection((bounds[0], top, bounds[1], bottom), name)], timestamp,
            timestamp_valid=timestamp_valid,
            class_map=mask, label_ids=LABELS,
        )
    return result


@pytest.mark.parametrize("name", ["tree_trunk", "pole"])
@pytest.mark.parametrize("tops", [(0, 0, 0), (0, 30, 0)])
def test_visible_static_contact_warns_before_near_threshold_and_reaches_voice(name, tops):
    result = approach(name=name, tops=tops)
    item = result["detections"][0]
    assert item["geometry"]["clipped"]
    assert item["geometry"]["ground_contact_visible"]
    assert item["proximity"]["band"] == "middle"
    assert item["motion"]["quality"] == ("valid" if tops == (0, 0, 0) else "unstable")
    assert item["motion"]["time_to_near_s"] is None
    assert item["motion"]["ground_approach"]["quality"] == "valid"
    assert item["motion"]["ground_approach"]["time_to_near_s"] == pytest.approx(.2)
    assert item["motion"]["ttc_scale_s"] is None
    assert item["motion"]["ttc_invalid_reason"] == (
        "clipped_box" if tops == (0, 0, 0) else "unstable_motion")
    assert item["alert_level"] == "danger"
    assert "ground_approaching_near_path" in item["reasons"]
    apply_obstacle_voice_suppression(
        result, {}, np.ones(FRAME.shape[:2], np.uint8), LABELS, FRAME.shape,
        SETTINGS["crosswalk_safety"],
    )
    assert WalkingVoice().observe(result, 100, .2) is not None


@pytest.mark.parametrize("kwargs", [
    {"bounds": (0, 60)},
    {"bounds": (40, 100)},
    {"bounds": (1, 60)},
    {"bounds": (40, 99)},
    {"bottoms": (100, 100, 100)},
    {"bottoms": (100, 72, 74)},
    {"bottoms": (70, 70, 70)},
    {"bottoms": (74, 72, 70)},
    {"bottoms": (60, 62, 64)},
    {"bounds": (5, 15)},
    {"identity": None},
    {"stable": False},
    {"timestamp_valid": False},
    {"times": (0, .1, .8)},
    {"config": {"approach_danger_enabled": False}},
    {"bottoms": (70, 55, 74)},
])
def test_unreliable_or_nonconflicting_contact_does_not_gain_early_danger(kwargs):
    item = approach(**kwargs)["detections"][0]
    assert item["motion"]["time_to_near_s"] is None
    assert item["motion"]["ground_approach"]["time_to_near_s"] is None
    assert item["motion"]["ttc_scale_s"] is None
    assert item["alert_level"] != "danger"


@pytest.mark.parametrize("name", ["person", "car", "bicycle"])
def test_cropped_mobile_objects_keep_existing_motion_gate(name):
    item = approach(name=name, tops=(0, 30, 0))["detections"][0]
    assert item["motion"]["quality"] == "unstable"
    assert item["motion"]["time_to_near_s"] is None
    assert item["motion"]["ttc_scale_s"] is None


def test_top_only_prediction_requires_visible_contact_throughout_history():
    assessor = engine(config={**CFG, "camera_view_guard_enabled": False})
    for timestamp, left, bottom in ((0, 0, 70), (.1, 40, 72), (.2, 40, 74)):
        result = assessor.update(
            FRAME, [detection((left, 0, 60, bottom), "tree_trunk")], timestamp,
        )
    item = result["detections"][0]
    assert item["geometry"]["ground_contact_visible"]
    assert item["motion"]["time_to_near_s"] is None
    assert item["alert_level"] != "danger"


@pytest.mark.parametrize("policy", ["nonwalkable", "stationary", "crosswalk"])
def test_early_static_prediction_respects_existing_voice_suppression(policy):
    result = approach()
    mask = np.full(FRAME.shape[:2], 0 if policy == "nonwalkable" else 1, np.uint8)
    apply_obstacle_voice_suppression(
        result, {}, mask, LABELS, FRAME.shape, SETTINGS["crosswalk_safety"],
    )
    if policy == "stationary":
        result["stationarity"] = {"status": "stationary"}
    assert WalkingVoice().observe(
        result, 100, .2, crossing_active=policy == "crosswalk",
    ) is None
    if policy == "nonwalkable":
        assert result["detections"][0]["voice_suppressed_reason"] == "low_walkable_surroundings"


def test_october_eighth_tree_track_643_predicts_before_recorded_near_contact():
    # 10:35:59 session, frames 599–601: top cropped, foot still visible.
    samples = [
        (0, (.386777, 0, .625115, .720255)),
        (.181, (.338628, .004627, .593285, .725921)),
        (.361, (.306596, 0, .587802, .765606)),
    ]
    frame = np.zeros((640, 360, 3), np.uint8)
    assessor = engine(config={**CFG, "camera_view_guard_enabled": False})
    for timestamp, box in samples:
        pixels = [value * scale for value, scale in zip(box, (360, 640, 360, 640))]
        result = assessor.update(frame, [detection(pixels, "tree_trunk", 27)], timestamp)
    item = result["detections"][0]
    assert item["geometry"]["point"][1] < CFG["static_danger_y"]
    assert item["motion"]["ground_approach"]["time_to_near_s"] < CFG["approach_danger_s"]
    assert item["motion"]["ttc_scale_s"] is None
    assert item["alert_level"] == "danger"


@pytest.mark.parametrize("enabled", [False, True])
def test_height_jitter_does_not_relax_lateral_entry_or_motion_quality(enabled):
    assessor = engine(config={**CFG, "camera_view_guard_enabled": False,
                               "approach_danger_enabled": enabled})
    for timestamp, box in zip((0, .1, .2), [(2, 0, 12, 45), (4, 30, 14, 45),
                                           (6, 0, 16, 45)]):
        result = assessor.update(FRAME, [detection(box, "tree_trunk", 27)], timestamp)
    item = result["detections"][0]
    assert item["motion"]["quality"] == "unstable"
    assert item["motion"]["velocity_norm_per_s"] is None
    assert item["motion"]["time_to_path_s"] is None
    assert item["motion"]["time_to_corridor_s"] is None
    assert item["risk_level"] == item["alert_level"] == "monitor"
    assert "relative_path_entry" not in item["reasons"]


@pytest.mark.parametrize("tops", [(0, 0, 0), (0, 30, 0)])
def test_ground_approach_on_nonwalkable_surroundings_stays_caution(tops):
    result = approach(tops=tops, mask=np.zeros(FRAME.shape[:2], np.uint8))
    item = result["detections"][0]
    assert item["motion"]["ground_approach"]["time_to_near_s"] is not None
    assert item["surrounding_walkability"]["all_non_walkable"]
    assert item["risk_level"] == item["alert_level"] == "caution"
    assert "ground_approaching_near_path" not in item["reasons"]
    assert item["release_evidence"] is None
    assert WalkingVoice().observe(result, 100, .2) is None


def test_existing_full_box_approach_keeps_its_nonwalkable_exception():
    item = approach(tops=(40, 42, 44), mask=np.zeros(FRAME.shape[:2], np.uint8))["detections"][0]
    assert item["motion"]["time_to_near_s"] == pytest.approx(.2)
    assert item["risk_level"] == "danger"
    assert "approaching_near_path" in item["reasons"]
    assert "ground_approaching_near_path" not in item["reasons"]


def test_ground_approach_does_not_confirm_release_after_full_box_instability():
    result = approach(tops=(0, 30, 35), bottoms=(70, 70, 70))
    item = result["detections"][0]
    assert not item["geometry"]["clipped"]
    assert item["motion"]["quality"] == "unstable"
    assert item["motion"]["ground_approach"]["quality"] == "valid"
    assert item["release_evidence"] is None
    assert item["assessment_quality"] == "limited"


@pytest.mark.parametrize("reason", ["short_ttc", "predicted_moving_conflict", "approaching_near_path"])
def test_ground_motion_cannot_validate_other_rapid_hazard_reasons(reason):
    item = {"reasons": [reason], "motion": {
        "quality": "unstable",
        "ground_approach": {"quality": "valid", "time_to_near_s": .2},
    }}
    assert rapid_approach_hazard(item) is False


def test_ground_prediction_reaches_voice_before_immediate_roi_with_unstable_height():
    result = approach(tops=(0, 30, 0), bottoms=(48, 53, 58))
    item = result["detections"][0]
    assert item["geometry"]["immediate_overlap"] == 0
    assert item["motion"]["quality"] == "unstable"
    assert item["motion"]["ground_approach"]["time_to_near_s"] == pytest.approx(.4)
    assert item["risk_level"] == "danger"
    apply_obstacle_voice_suppression(
        result, {}, np.ones(FRAME.shape[:2], np.uint8), LABELS, FRAME.shape,
        SETTINGS["crosswalk_safety"],
    )
    assert WalkingVoice().observe(result, 100, .2) is not None


@pytest.mark.parametrize("ground", [None, {}, {"quality": "valid"},
                                    {"quality": "unstable", "time_to_near_s": .2}])
def test_ground_voice_requires_its_own_confirmed_prediction(ground):
    item = {"reasons": ["ground_approaching_near_path"], "motion": {
        "quality": "valid", "ground_approach": ground,
    }}
    assert rapid_approach_hazard(item) is False
