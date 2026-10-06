"""Early moving traffic warnings require a path conflict and independent motion."""

import numpy as np
import pytest
import yaml

from src.risk import RiskEngine, predicted_moving_conflict
from src.risk_config import risk_config
from src.risk_motion import MotionHistory
from src.walking_voice import WalkingVoice
from test_walking_voice import danger_item, prediction
from test_risk import FRAME, FixedTracker, detection


CFG = risk_config(yaml.safe_load(open("configs/inference.yaml"))["risk"])
GEOMETRY = {
    "clipped": False, "point": [0.5, 0.6], "footprint": [0.42, 0.58, 0.58, 0.6],
    "immediate_top_y": 0.7, "corridor_overlap": 0.5,
}
MOTION = {
    "quality": "valid", "independent_velocity_norm_per_s": [0.1, 0.08],
    "velocity_norm_per_s": [0.0, 0.1], "ttc_scale_s": 2.0,
}


def test_approaching_vehicle_reaches_walking_corridor_early():
    assert predicted_moving_conflict(
        {"class_name": "car", "track_id": 7}, GEOMETRY, MOTION, 0.5, CFG
    ) == pytest.approx((1.0, "projected_near_path"))


def test_lateral_vehicle_crosses_centre_before_near_zone():
    geometry = {**GEOMETRY, "point": [0.47, 0.565],
                "footprint": [0.29, 0.545, 0.65, 0.565]}
    motion = {**MOTION, "velocity_norm_per_s": [0.12, 0.037],
              "independent_velocity_norm_per_s": [0.1, 0.04],
              "ttc_scale_s": 2.2}
    time, basis = predicted_moving_conflict(
        {"class_name": "car", "track_id": 7}, geometry, motion, 0.5, CFG)
    assert time == pytest.approx(0.25)
    assert basis == "projected_center_crossing"


@pytest.mark.parametrize("change", [
    {"class_name": "person"}, {"track_id": None},
])
def test_rule_applies_only_to_tracked_moving_traffic(change):
    target = {"class_name": "car", "track_id": 7, **change}
    assert predicted_moving_conflict(target, GEOMETRY, MOTION, 0.5, CFG) is None


def test_parked_or_passing_road_traffic_stays_out_of_early_stop():
    target = {"class_name": "bicycle", "track_id": 7}
    for motion in (
        {**MOTION, "independent_velocity_norm_per_s": [0.02, 0.01]},
        {**MOTION, "independent_velocity_norm_per_s": [0.1, -0.02]},
        {**MOTION, "ttc_scale_s": None},
        {**MOTION, "quality": "unstable"},
    ):
        assert predicted_moving_conflict(target, GEOMETRY, motion, 0.5, CFG) is None
    roadside = {**GEOMETRY, "footprint": [0.85, 0.58, 0.95, 0.6],
                "corridor_overlap": 0.0}
    assert predicted_moving_conflict(target, roadside, MOTION, 0.5, CFG) is None


def test_background_transform_removes_stationary_bicycle_motion():
    history = MotionHistory(CFG)
    detection = {"class_name": "bicycle", "class_id": 1, "track_id": 7}
    geometry = {**GEOMETRY, "height": 0.2}
    transform = np.asarray([[1, 0, 0], [0, 1, 0.02]])
    for t, bottom in ((0.0, 0.56), (0.1, 0.58), (0.2, 0.6)):
        current = {**geometry, "point": [0.5, bottom]}
        motion = history.update(detection, current, t, True,
                                background_transform=transform)
    assert motion["quality"] == "valid"
    assert motion["independent_velocity_norm_per_s"][1] == pytest.approx(0, abs=1e-6)
    assert predicted_moving_conflict(detection, current, motion, 0.5, CFG) is None


def test_only_confirmed_crossing_overrides_nonwalkable_road_filter():
    class IdentityCamera:
        last_transform_norm = np.asarray([[1., 0., 0.], [0., 1., 0.]])

        def reset(self):
            pass

        def update(self, *args):
            return True

    labels = {"non_walkable": 0, "walkable": 1, "crosswalk": 2}
    boxes = [(25, 44, 55, 55), (27, 43, 59, 56), (29, 43, 65, 57)]
    for enabled, wanted in ((False, "caution"), (True, "danger")):
        engine = RiskEngine({**CFG, "camera_view_guard_enabled": False,
                             "moving_conflict_enabled": enabled},
                            tracker=FixedTracker(17), camera_guard=IdentityCamera())
        for t, box in zip((0.0, 0.1, 0.2), boxes):
            result = engine.update(FRAME, [detection(box, "car", 3)], t,
                                   class_map=np.zeros(FRAME.shape[:2], np.uint8),
                                   label_ids=labels)
        item = result["detections"][0]
        assert item["risk_level"] == wanted
        assert item["alert_level"] == wanted
        assert ("predicted_moving_conflict" in item["reasons"]) is enabled


def test_predicted_bicycle_can_trigger_stop_during_crosswalk_approach():
    item = danger_item(7, [20, 20, 34, 64], "bicycle",
                       geometry={"immediate_overlap": 0},
                       reasons=["predicted_moving_conflict"],
                       motion={"quality": "valid"})
    result = prediction(item)
    assert WalkingVoice().observe(result, 100, 0.0, crosswalk_status="approach")[0] == "멈추세요"
    assert result["voice_event"]["urgency"] == "emergency"


def test_moving_conflict_settings_are_validated():
    for invalid in ({"moving_conflict_enabled": 1},
                    {"moving_conflict_min_independent_speed": 0},
                    {"moving_conflict_lateral_max_gap_y": 1.1}):
        with pytest.raises(ValueError):
            risk_config(invalid)
