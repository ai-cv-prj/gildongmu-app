"""4차 현장 피드백: 이동 가능성, 지속 혼잡, 멀어지는 사람의 회귀 검증."""

from copy import deepcopy
from unittest.mock import patch

import numpy as np
import pytest
import yaml

from src.risk import receding_person_clearance
from src.walking_direction import direction_ground, direction_safety
from src.walking_voice import WalkingVoice, walking_action
from test_risk import FRAME, detection, engine
from test_walking_voice import danger_item, caution_item, prediction


def crowd():
    return prediction(danger_item(1, [10, 20, 30, 75]),
                      danger_item(2, [45, 20, 55, 75]),
                      danger_item(3, [70, 20, 90, 75]))


def test_road_gap_blocks_even_when_almost_all_sweep_pixels_are_walkable():
    mask = np.ones((100, 100), np.uint8)
    mask[:, 30] = 0  # 평균 97% 이상이어도 중간 도로 틈을 넘는 지시는 금지.
    ground = direction_ground(mask, {"walkable": 1}, mask.shape)
    assert ground["fractions"]["left"] > .95
    assert not ground["left"] and ground["right"]


def test_missing_ground_does_not_delay_warning_or_allow_direction():
    result = prediction(danger_item(1, [55, 20, 70, 70]))
    result.pop("direction_ground")
    assert WalkingVoice().observe(result, 100, 0)[0] == "전방 장애물"


def test_diagonal_nonwalkable_gap_cannot_join_disconnected_ground():
    mask = np.ones((640, 360), np.uint8)
    for row in range(round(640 * .88), round(640 * .96)):
        mask[row, 95 + row - round(640 * .88)] = 0
    ground = direction_ground(mask, {"walkable": 1}, mask.shape)
    assert ground["fractions"]["left"] > .95
    assert not ground["left"] and ground["right"]


def test_clip002_left_candidate_uses_clear_right_when_left_route_is_occupied():
    # 현장 19.49초에는 두 사람 모두 중앙이고, 왼쪽 가까운 공간에 의자도 있었다.
    # 중앙 동행인 박스만으로 왼쪽 전체를 막던 기존 축약 테스트를 보완한다.
    right = danger_item(357, [51, 42, 62, 58], geometry={"immediate_overlap": 0},
                        reasons=["approaching_near_path"], motion={"quality": "valid"})
    left = caution_item(403, [42, 42, 52, 57])
    chair = caution_item(523, [10.28, 53.79, 22.40, 60.37], name="chair")
    result = prediction(right, left, chair)
    result["roi"] = {"immediate_polygon": [[.02, .6], [.98, .6], [.98, 1], [.02, 1]]}
    assert walking_action(result, 100) == "left"
    assert WalkingVoice().observe(result, 100, 0)[0] == "오른쪽 한 걸음"
    assert result["voice_diagnostics"]["direction_safety"]["left"]["blockers"] == [523]
    assert left["risk_level"] == "caution"


def test_distant_caution_does_not_block_but_predicted_entry_does():
    obstacle = danger_item(1, [20, 20, 40, 70])
    other = caution_item(2, [65, 20, 80, 40],
                         voice_suppressed_reason="low_walkable_surroundings")
    result = prediction(obstacle, other)
    assert direction_safety(result, 100)["right"]["allowed"]
    other["motion"] = {"quality": "valid", "velocity_norm_per_s": [0, .4]}
    assert not direction_safety(result, 100)["right"]["allowed"]


def test_clip002_center_people_do_not_veto_clear_right_step():
    # GPU 20.19초: 중앙 사람의 오른쪽 끝은 59.5%, 기존 추가 검사는 55%부터 막았다.
    center = danger_item(151, [43.7, 40.2, 59.5, 62.4],
                         motion={"quality": "valid", "velocity_norm_per_s": [-.08, .062]})
    left = danger_item(194, [28.7, 40.1, 42.3, 62.7],
                       motion={"quality": "valid", "velocity_norm_per_s": [-.158, .073]})
    edge = caution_item(249, [85.1, 37.6, 100, 56.2], name="movable_obstacle",
                        motion={"quality": "valid", "velocity_norm_per_s": [-.002, .008]})
    result = prediction(center, left, edge)
    assert walking_action(result, 100) == "right"
    assert WalkingVoice().observe(result, 100, 0)[0] == "오른쪽 한 걸음"
    assert result["voice_diagnostics"]["direction_safety"]["right"]["allowed"]


def test_center_person_crossing_into_near_right_still_vetoes_direction():
    result = prediction(danger_item(1, [20, 20, 40, 75]),
                        caution_item(2, [45, 30, 60, 60],
                                     motion={"quality": "valid", "velocity_norm_per_s": [.2, .1]}))
    assert walking_action(result, 100) == "right"
    assert WalkingVoice().observe(result, 100, 0)[0] == "혼잡 주의"
    assert result["voice_diagnostics"]["direction_safety"]["right"]["blockers"] == [2]


def test_distant_side_person_does_not_block_until_predicted_near_entry():
    person = caution_item(2, [70, 30, 80, 57],
                          motion={"quality": "valid", "velocity_norm_per_s": [0, .01]})
    result = prediction(danger_item(1, [20, 20, 40, 70]), person)
    assert direction_safety(result, 100)["right"]["allowed"]
    person["motion"]["velocity_norm_per_s"] = [0, .12]
    assert not direction_safety(result, 100)["right"]["allowed"]


def test_people_on_both_sides_use_crowd_without_waiting_for_both_to_be_danger():
    result = prediction(danger_item(1, [20, 20, 43, 75]),
                        caution_item(2, [70, 20, 85, 75]))
    assert walking_action(result, 100) == "right"
    assert WalkingVoice().observe(result, 100, 0)[0] == "혼잡 주의"


def test_crowd_keeps_one_message_when_distribution_becomes_blocked():
    voice = WalkingVoice()
    voice.observe(crowd(), 100, 0)
    # 양옆 사람은 멀어졌지만 가운데 위험이 남고 회피 방향을 고를 수 없다.
    blocked = prediction(danger_item(2, [45, 20, 55, 75]),
                         caution_item(1, [10, 20, 30, 40]),
                         caution_item(3, [70, 20, 90, 40]))
    events = []
    for index in range(1, 36):
        result = deepcopy(blocked)
        if voice.observe(result, 100, index * .2):
            events.append((index * .2, result["voice_action"]))
        assert result["voice_action"] == "crowded"
    assert events == [(3, "crowded"), (6, "crowded")]
    # 위험 해제가 확인된 다음 장면까지 혼잡을 계속 붙이지 않는다.
    cleared = prediction(*(caution_item(i, [10, 20, 90, 40]) for i in (1, 2, 3)))
    for timestamp in (7.2, 8, 8.8):
        assert voice.observe(deepcopy(cleared), 100, timestamp) is None
    assert voice.observe(deepcopy(blocked), 100, 9)[0] == "전방 장애물"


def test_unsafe_previous_direction_is_retracted_before_opposite_confirmation():
    voice = WalkingVoice()
    assert voice.observe(prediction(danger_item(1, [20, 20, 43, 80])), 100, 0)[0] == "오른쪽 한 걸음"
    opposite = danger_item(1, [57, 20, 80, 80])
    assert voice.observe(prediction(opposite), 100, .1)[0] == "전방 장애물"
    assert voice.observe(prediction(opposite), 100, .5) is None
    assert voice.observe(prediction(opposite), 100, .7)[0] == "왼쪽 한 걸음"


def test_unsafe_direction_is_cancelled_even_when_no_voice_candidate_remains():
    voice = WalkingVoice()
    voice.observe(prediction(danger_item(1, [20, 20, 43, 80])), 100, 0)
    result = prediction(caution_item(1, [60, 20, 80, 80]))
    assert voice.observe(result, 100, .1) is None
    assert result["voice_clear"] is True
    assert voice.events[-1] == (.1, None)


def test_confirmed_image_route_only_authorizes_one_step():
    result = prediction(danger_item(1, [20, 20, 55, 70]))
    assert WalkingVoice().observe(result, 100, 0)[0] == "오른쪽 한 걸음"


def test_crowd_repeats_every_three_seconds_without_extending_redirect_window():
    voice = WalkingVoice()
    events = []
    for index in range(46):
        timestamp = index * .2
        result = crowd()
        # 객체 교체가 반복 주기를 초기화하거나 새 음성을 즉시 만들지 않는다.
        for item in result["detections"]:
            item["event_id"] += 3 * index
        if voice.observe(result, 100, timestamp):
            events.append((timestamp, result["voice_event"]["event_id"]))
        assert voice.crowded_until == 3
    assert events == [(0, 1), (3, 2), (6, 3), (9, 4)]


def test_step_jitter_does_not_restart_direction_confirmation_after_crowd():
    voice = WalkingVoice()
    voice.observe(crowd(), 100, 0)
    for timestamp in (1, 2, 3):
        voice.observe(crowd(), 100, timestamp)
    result = prediction(danger_item(1, [20, 20, 43, 70]),
                        caution_item(2, [45, 20, 55, 40]),
                        caution_item(3, [70, 20, 90, 40]))
    result["direction_ground"]["max_steps"] = 2
    with patch("src.walking_voice.movement_steps", side_effect=[1, 2, 1, 2]):
        for timestamp in (3.1, 3.3, 3.5):
            assert voice.observe(deepcopy(result), 100, timestamp) is None
        assert voice.observe(deepcopy(result), 100, 3.7)[0] == "오른쪽 두 걸음"


def test_no_crowd_repeat_after_hazards_clear_and_stop_is_immediate():
    voice = WalkingVoice()
    voice.observe(crowd(), 100, 0)
    cleared = prediction(*(caution_item(i, [10, 20, 90, 40]) for i in (1, 2, 3)))
    for timestamp in (.5, 1, 2):
        assert voice.observe(deepcopy(cleared), 100, timestamp) is None
    voice.observe(crowd(), 100, 3)
    emergency = prediction(danger_item(1, [40, 20, 60, 70],
                                      reasons=["predicted_moving_conflict"]))
    assert voice.observe(emergency, 100, 3.1)[0] == "멈추세요"


def test_crowd_epoch_and_time_gap_reset_repeat_state():
    voice = WalkingVoice()
    voice.observe(crowd(), 100, 0)
    result = crowd()
    result["state_epoch"] = 1
    assert voice.observe(result, 100, .1) is not None
    assert voice.last_crowded_voice_at == .1
    assert voice.observe(result, 100, 3) is not None
    assert voice.crowded_until == 6


def test_receding_person_relaxes_through_existing_release_policy_then_near_realerts():
    cfg = yaml.safe_load(open("configs/inference.yaml"))["risk"]
    e = engine(config={**cfg, "camera_view_guard_enabled": False})
    for index in range(11):
        t = index * .1
        bottom, height = 75 - 5 * t, 32 - 8 * t
        result = e.update(FRAME, [detection((40, bottom - height, 60, bottom))], t)
        item = result["detections"][0]
        if index == 0:
            assert item["risk_level"] == item["alert_level"] == "danger"
        if index == 3:
            assert item["risk_level"] == "caution" and item["alert_level"] == "danger"
    assert item["motion"]["approach_state"] == "receding"
    assert item["alert_level"] == "caution"
    assert item["release_evidence"] == "receding_person_clearance"
    # 아직 near가 아니어도 크기·발 위치가 다시 증가하면 즉시 완화를 철회한다.
    result = e.update(FRAME, [detection((40, 44, 60, 71))], 1.1)
    assert result["detections"][0]["alert_level"] == "danger"


@pytest.mark.parametrize("change", ["clipped", "near", "unstable", "unknown", "inward", "entry"])
def test_receding_relief_preserves_uncertain_close_and_crossing_hazards(change):
    cfg = yaml.safe_load(open("configs/inference.yaml"))["risk"]
    g = {"clipped": False, "point": [.3, .7]}
    m = {"quality": "valid", "approach_state": "receding", "receding_consistent": True,
         "time_to_path_s": None,
         "time_to_near_s": None, "velocity_norm_per_s": [0, -.1]}
    p = {"band": "middle"}
    if change == "clipped": g["clipped"] = True
    if change == "near": p["band"] = "near"
    if change == "unstable": m["quality"] = "unstable"
    if change == "unknown": m["approach_state"] = "unknown"
    if change == "inward": m["velocity_norm_per_s"] = [.1, -.1]
    if change == "entry": m["time_to_path_s"] = .3
    assert not receding_person_clearance({"class_name": "person"}, g, m, p, cfg)
