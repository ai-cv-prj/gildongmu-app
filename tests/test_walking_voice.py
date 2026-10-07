"""
file_path: tests/test_walking_voice.py

보행 위험 분포의 이동 행동 선택과 결과 MP4 음성 합성을 검증한다.
"""

import io
import json
import subprocess
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from src.pipeline import process_video
from src.risk_log import risk_log_path
from src.video_audio import SAMPLE_RATE, ffmpeg_executable, render_voice_track
from src.risk_visualization import action_status_text, risk_identity
from src.walking_voice import (
    WalkingVoice,
    center_occupancy_ratio,
    movement_steps,
    repeat_none_s,
    suppress_non_green_crosswalk_voice,
    transition_confirm_s,
    walking_action,
    warning_directions,
)


# 테스트용 위험 객체 만들기
def danger_item(event_id, box, name="person", **extra):
    """신뢰할 수 있는 객체 이름과 위험 이벤트 ID를 가진 검출을 반환한다."""
    return {"event_id": event_id, "detection_index": event_id - 1,
            "xyxy": box, "alert_level": "danger", "risk_level": "danger",
            "class_name": name, "label_status": "reliable", "display_label": name,
            "warning_primary": True, "geometry": {"immediate_overlap": 1.0}, **extra}


# 테스트용 주의 객체 만들기
def caution_item(event_id, box, **extra):
    """좌우 안전도 비교에 사용하는 주의 객체를 반환한다."""
    return danger_item(event_id, box, **extra) | {
        "alert_level": "caution", "risk_level": "caution",
    }


# 테스트용 프레임 위험 결과 만들기
def prediction(*items, level="danger", source="object", epoch=0):
    """첫 번째 객체가 화면의 대표 경고인 프레임 결과를 반환한다."""
    index = items[0]["detection_index"] if items else None
    return {"warning": {"level": level, "source": source, "detection_index": index},
            "detections": list(items), "state_epoch": epoch}


class WalkingVoiceTests(unittest.TestCase):
    """보행 위험의 행동 음성 선택 및 영상 저장 규칙을 확인한다."""

    # 모든 직접 행동 전환의 안정화 시간 확인
    def test_all_direct_action_transition_timings(self):
        """
        이전·다음 행동 조합에 200ms, 500ms, 3000ms 정책을 적용한다.
        """
        actions = (None, "left", "right", "straight", "stop")
        for previous in actions:
            for current in ("left", "right", "straight", "stop"):
                if previous is None or current == "stop":
                    expected = 0.0
                elif previous == "stop":
                    expected = 0.5
                elif current == "straight":
                    expected = 3.0
                else:
                    expected = 0.2
                with self.subTest(previous=previous, current=current):
                    self.assertEqual(transition_confirm_s(previous, current), expected)

    # 모든 직접 행동 전환의 실제 음성 발생 확인
    def test_all_direct_action_transitions_follow_timing_policy(self):
        """
        같은 행동은 침묵하고 다른 행동은 전환별 시간 후에 한 번 안내한다.
        """
        messages = {
            "left": ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"),
            "right": ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"),
            "straight": ("천천히 가세요.", "walking-straight.mp3"),
            "stop": ("멈추세요", "walking-stop.mp3"),
        }
        actions = ("left", "right", "straight", "stop")
        for previous in actions:
            for current in actions:
                with self.subTest(previous=previous, current=current):
                    voice = WalkingVoice()
                    voice.last_action = previous
                    voice.last_steps = 1 if previous in ("left", "right") else None
                    voice.last_action_since = 1.0
                    with patch("src.walking_voice.walking_action", return_value=current):
                        first = voice.observe(prediction(level="monitor"), 100, 1.0)
                        if current == previous:
                            self.assertIsNone(first)
                            continue
                        delay = transition_confirm_s(previous, current)
                        if delay == 0:
                            self.assertEqual(first, messages[current])
                            continue
                        self.assertIsNone(first)
                        if delay > 2.0:
                            for timestamp in (2.0, 3.0):
                                self.assertIsNone(voice.observe(
                                    prediction(level="monitor"), 100, timestamp))
                        self.assertIsNone(voice.observe(
                            prediction(level="monitor"), 100, 1.0 + delay - .001))
                        confirmed = voice.observe(
                            prediction(level="monitor"), 100, 1.0 + delay)
                        self.assertEqual(confirmed, messages[current])

    # 좌우 확정 시점 기준 직진 전환 확인
    def test_lateral_to_straight_uses_lateral_confirmation_time(self):
        """
        좌우 확정 후 3초가 되는 시점까지 직진이면 유지 시작 시점과 무관하게 안내한다.
        """
        message = ("천천히 가세요.", "walking-straight.mp3")
        for previous in ("left", "right"):
            with self.subTest(previous=previous, straight_before_threshold=True):
                voice = WalkingVoice()
                voice.last_action, voice.last_steps = previous, 1
                voice.last_action_since = 0.0
                with patch("src.walking_voice.walking_action", return_value="straight"):
                    self.assertIsNone(
                        voice.observe(prediction(level="monitor"), 100, 2.0)
                    )
                    self.assertIsNone(
                        voice.observe(prediction(level="monitor"), 100, 2.999)
                    )
                    self.assertEqual(
                        voice.observe(prediction(level="monitor"), 100, 3.0), message
                    )
            with self.subTest(previous=previous, straight_after_threshold=True):
                voice = WalkingVoice()
                voice.last_action, voice.last_steps = previous, 1
                voice.last_action_since = 0.0
                with patch("src.walking_voice.walking_action", return_value="straight"):
                    self.assertEqual(
                        voice.observe(prediction(level="monitor"), 100, 3.0), message
                    )

    # 같은 행동의 none 후 재생 기준 확인
    def test_same_action_replay_requires_configured_none_duration(self):
        """
        좌·우·직진은 3초, 정지는 0.5초 none 후에만 같은 음성을 재생한다.
        """
        messages = {
            "left": ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"),
            "right": ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"),
            "straight": ("천천히 가세요.", "walking-straight.mp3"),
            "stop": ("멈추세요", "walking-stop.mp3"),
        }
        for action in ("left", "right", "straight", "stop"):
            threshold = repeat_none_s(action)
            steps = 1 if action in ("left", "right") else None
            with self.subTest(action=action, interval="short"):
                voice = WalkingVoice()
                voice.last_action, voice.last_steps = action, steps
                with patch("src.walking_voice.walking_action", return_value=None):
                    self.assertIsNone(voice.observe(prediction(level="monitor"), 100, 1.0))
                    for timestamp in (2.0, 3.0):
                        if timestamp < 1.0 + threshold - .001:
                            self.assertIsNone(voice.observe(
                                prediction(level="monitor"), 100, timestamp))
                with patch("src.walking_voice.walking_action", return_value=action):
                    self.assertIsNone(voice.observe(
                        prediction(level="monitor"), 100, 1.0 + threshold - .001))
            with self.subTest(action=action, interval="confirmed"):
                voice = WalkingVoice()
                voice.last_action, voice.last_steps = action, steps
                with patch("src.walking_voice.walking_action", return_value=None):
                    self.assertIsNone(voice.observe(prediction(level="monitor"), 100, 1.0))
                    for timestamp in (2.0, 3.0):
                        if timestamp < 1.0 + threshold:
                            self.assertIsNone(voice.observe(
                                prediction(level="monitor"), 100, timestamp))
                with patch("src.walking_voice.walking_action", return_value=action):
                    self.assertEqual(
                        voice.observe(prediction(level="monitor"), 100, 1.0 + threshold),
                        messages[action],
                    )

    # none 후 일반적인 다른 행동의 즉시 재생 확인
    def test_different_action_after_none_is_announced_immediately(self):
        """
        좌우에서 직진하는 예외를 제외하고 다른 행동은 첫 감지 시 바로 안내한다.
        """
        messages = {
            "left": ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"),
            "right": ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"),
            "straight": ("천천히 가세요.", "walking-straight.mp3"),
            "stop": ("멈추세요", "walking-stop.mp3"),
        }
        actions = ("left", "right", "straight", "stop")
        for previous in actions:
            for current in actions:
                if (current == previous
                        or (previous in ("left", "right") and current == "straight")):
                    continue
                with self.subTest(previous=previous, current=current):
                    voice = WalkingVoice()
                    voice.last_action = previous
                    voice.last_steps = 1 if previous in ("left", "right") else None
                    with patch("src.walking_voice.walking_action", return_value=None):
                        self.assertIsNone(
                            voice.observe(prediction(level="monitor"), 100, 1.0)
                        )
                    with patch("src.walking_voice.walking_action", return_value=current):
                        self.assertEqual(
                            voice.observe(prediction(level="monitor"), 100, 1.001),
                            messages[current],
                        )

    # 좌우 안내 후 직진의 none 유지 기준 확인
    def test_straight_after_lateral_requires_three_seconds_of_none(self):
        """
        좌우 안내 뒤에는 none이 3초 이상 유지된 경우에만 직진을 다시 안내한다.
        """
        straight_message = ("천천히 가세요.", "walking-straight.mp3")
        for previous in ("left", "right"):
            with self.subTest(previous=previous, interval="short"):
                voice = WalkingVoice()
                voice.last_action, voice.last_steps = previous, 1
                with patch("src.walking_voice.walking_action", return_value=None):
                    for timestamp in (1.0, 2.0, 3.0):
                        self.assertIsNone(
                            voice.observe(prediction(level="monitor"), 100, timestamp)
                        )
                with patch("src.walking_voice.walking_action", return_value="straight"):
                    self.assertIsNone(
                        voice.observe(prediction(level="monitor"), 100, 3.999)
                    )
            with self.subTest(previous=previous, interval="confirmed"):
                voice = WalkingVoice()
                voice.last_action, voice.last_steps = previous, 1
                with patch("src.walking_voice.walking_action", return_value=None):
                    for timestamp in (1.0, 2.0, 3.0):
                        self.assertIsNone(
                            voice.observe(prediction(level="monitor"), 100, timestamp)
                        )
                with patch("src.walking_voice.walking_action", return_value="straight"):
                    self.assertEqual(
                        voice.observe(prediction(level="monitor"), 100, 4.0),
                        straight_message,
                    )

    # 하단 발자국의 35·30·35 구역 침범 확인
    def test_direction_uses_footprint_overlap_with_intrusion_thresholds(self):
        """최대 겹침 구역과 기준 이상 침범한 인접 구역을 모두 반환한다."""
        self.assertEqual(warning_directions(danger_item(1, [10, 0, 20, 20]), 100), {"left"})
        self.assertEqual(warning_directions(danger_item(1, [45, 0, 55, 20]), 100), {"center"})
        self.assertEqual(warning_directions(danger_item(1, [80, 0, 90, 20]), 100), {"right"})
        self.assertEqual(warning_directions(danger_item(1, [50, 0, 90, 20]), 100),
                         {"center", "right"})

    # 경계의 작은 겹침 무시 확인
    def test_direction_ignores_small_adjacent_zone_overlap(self):
        """가장 많이 겹친 구역은 유지하고 임계값 미만의 경계 침범은 무시한다."""
        self.assertEqual(warning_directions(danger_item(1, [20, 0, 36, 20]), 100), {"left"})
        self.assertEqual(warning_directions(danger_item(1, [64, 0, 80, 20]), 100), {"right"})

    # 긴 장애물의 복수 위험 방향 확인
    def test_wide_right_obstacle_blocks_center_and_guides_left(self):
        """오른쪽 중심의 긴 장애물이 가운데를 침범하면 왼쪽 이동을 안내한다."""
        result = prediction(danger_item(1, [50, 0, 90, 20]))
        self.assertEqual(walking_action(result, 100), "left")

    # 가운데 점유율의 중복 제거 확인
    def test_center_occupancy_uses_union_of_overlapping_obstacles(self):
        """겹치는 여러 객체의 가운데 점유 구간을 한 번만 계산한다."""
        items = [danger_item(1, [35, 0, 50, 20]), danger_item(2, [45, 0, 55, 20])]
        self.assertAlmostEqual(center_occupancy_ratio(items, 100), 2 / 3)

    # 한 걸음·두 걸음 경계와 유지 구간 확인
    def test_movement_steps_uses_center_occupancy_hysteresis(self):
        """45% 이하는 한 걸음, 55% 이상은 두 걸음이며 중간 구간은 직전 값을 유지한다."""
        one = [danger_item(1, [20, 0, 48.5, 20])]
        middle = [danger_item(2, [20, 0, 50, 20])]
        two = [danger_item(3, [20, 0, 51.5, 20])]
        self.assertEqual(movement_steps(one, "right", 100), 1)
        self.assertEqual(movement_steps(two, "right", 100), 2)
        self.assertEqual(movement_steps(middle, "right", 100, previous_steps=1), 1)
        self.assertEqual(movement_steps(middle, "right", 100, previous_steps=2), 2)
        self.assertIsNone(movement_steps(two, "straight", 100))

    # 핑크 ROI 내부 위험만 음성 행동에 포함
    def test_only_danger_inside_pink_roi_triggers_guidance(self):
        """위험이어도 핑크 ROI 겹침이 10% 미만이면 음성 행동을 만들지 않는다."""
        outside = danger_item(1, [5, 0, 15, 20], geometry={"immediate_overlap": .09})
        inside = danger_item(2, [5, 0, 15, 20], geometry={"immediate_overlap": .10})
        self.assertIsNone(walking_action(prediction(outside), 100))
        self.assertEqual(walking_action(prediction(inside), 100), "straight")

    # 정지 중 분홍색 ROI 하단 절반 음성 제한 확인
    def test_stationary_guidance_requires_lower_half_of_pink_roi(self):
        """사람과 차량 모두 정지 중에는 분홍색 ROI 하단 절반에 들어와야 안내한다."""
        for class_name in ("person", "car"):
            with self.subTest(class_name=class_name):
                upper = danger_item(
                    1, [45, 0, 55, 20], class_name,
                    geometry={"immediate_overlap": 1.0,
                              "stationary_voice_eligible": False},
                    motion={"quality": "valid"}, reasons=["short_ttc"])
                lower = danger_item(
                    2, [45, 0, 55, 20], class_name,
                    geometry={"immediate_overlap": 1.0,
                              "stationary_voice_eligible": True})
                upper_prediction = prediction(upper)
                upper_prediction["stationarity"] = {"status": "stationary"}
                lower_prediction = prediction(lower)
                lower_prediction["stationarity"] = {"status": "stationary"}
                self.assertIsNone(walking_action(upper_prediction, 100, stationary_voice=True))
                self.assertEqual(
                    walking_action(lower_prediction, 100, stationary_voice=True), "stop")

    # 이동 중 기존 음성 범위 유지 확인
    def test_moving_guidance_keeps_existing_pink_roi_rule(self):
        """정지하지 않은 상태에서는 분홍색 ROI 상단 객체도 기존처럼 안내한다."""
        item = danger_item(
            1, [45, 0, 55, 20], geometry={"immediate_overlap": 1.0,
                                         "stationary_voice_eligible": False})
        result = prediction(item)
        result["stationarity"] = {"status": "moving"}
        self.assertEqual(walking_action(result, 100, stationary_voice=True), "stop")

    # 정지 중 화면 행동과 모든 장애물 음성 제한 확인
    def test_stationary_filter_keeps_action_but_also_limits_crossing_vehicle_voice(self):
        """화면 ACTION은 유지하고 횡단 중 차량도 하단 절반 밖에서는 음성을 내지 않는다."""
        vehicle = danger_item(
            1, [45, 0, 55, 20], "car",
            geometry={"immediate_overlap": 1.0, "stationary_voice_eligible": False})
        result = prediction(vehicle)
        result["stationarity"] = {"status": "stationary"}
        self.assertEqual(walking_action(result, 100), "stop")
        self.assertIsNone(walking_action(result, 100, True, True))

    # 정지 중 화면 상태와 실제 음성 분리 확인
    def test_stationary_upper_half_keeps_action_but_mutes_voice(self):
        """상단 위험 표시는 유지하면서 분홍색 ROI 하단 밖의 실제 음성만 억제한다."""
        item = danger_item(
            1, [45, 0, 55, 20], "car",
            geometry={"immediate_overlap": 1.0, "stationary_voice_eligible": False})
        result = prediction(item)
        result["stationarity"] = {"status": "stationary"}
        voice = WalkingVoice()
        active = prediction(danger_item(2, [45, 0, 55, 20]))
        self.assertEqual(voice.observe(active, 100, 3.0), ("멈추세요", "walking-stop.mp3"))
        self.assertIsNone(voice.observe(result, 100, 3.1))
        self.assertEqual(result["last_action"], "stop")
        self.assertIsNone(result["voice_action"])
        self.assertTrue(result["voice_clear"])
        self.assertEqual(voice.events[-1], (3.1, None))

    # 기본 위험 분포의 네 행동 확인
    def test_danger_distribution_selects_basic_action(self):
        """세 방향의 모든 위험 조합을 직진·좌우 이동·정지로 바꾼다."""
        left = danger_item(1, [5, 0, 15, 20])
        center = danger_item(2, [45, 0, 55, 20])
        right = danger_item(3, [85, 0, 95, 20])
        cases = [
            (prediction(level="monitor"), None),
            (prediction(left), "straight"),
            (prediction(right), "straight"),
            (prediction(left, center), "right"),
            (prediction(center, right), "left"),
            (prediction(left, right), "straight"),
            (prediction(left, center, right), "straight"),
            (prediction(center), "stop"),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(walking_action(result, 100), expected)

    # 세 방향 위험의 사람·장애물 구분 확인
    def test_all_direction_people_slow_down_but_other_obstacles_stop(self):
        """세 방향이 모두 막혀도 전부 사람이면 서행하고 다른 객체가 섞이면 정지한다."""
        left = danger_item(1, [5, 0, 15, 20])
        center = danger_item(2, [45, 0, 55, 20])
        right_person = danger_item(3, [85, 0, 95, 20])
        right_bollard = danger_item(4, [85, 0, 95, 20], "bollard")
        self.assertEqual(
            walking_action(prediction(left, center, right_person), 100), "straight")
        self.assertEqual(
            walking_action(prediction(left, center, right_bollard), 100), "stop")

    # 가운데 위험의 거리 우선 비교
    def test_center_danger_chooses_farther_side_after_five_percent_tie(self):
        """가운데 위험이 치우치면 5% 동률 범위를 넘어서 먼 방향을 선택한다."""
        self.assertEqual(walking_action(prediction(danger_item(1, [35, 0, 45, 20])), 100),
                         "right")
        self.assertEqual(walking_action(prediction(danger_item(1, [55, 0, 65, 20])), 100),
                         "left")
        self.assertEqual(walking_action(prediction(danger_item(1, [47, 0, 53, 20])), 100),
                         "stop")

    # 동률에서 주의 객체로 좌우 비교
    def test_center_danger_uses_caution_count_then_distance(self):
        """위험 거리가 같으면 주의 개수와 가장 가까운 주의 거리를 차례로 비교한다."""
        center = danger_item(1, [45, 0, 55, 20])
        left_caution = caution_item(2, [5, 0, 15, 20])
        right_near = caution_item(3, [70, 0, 80, 20])
        right_far = caution_item(4, [88, 0, 98, 20])
        self.assertEqual(walking_action(
            prediction(center, left_caution, right_near, right_far), 100), "left")
        self.assertEqual(walking_action(
            prediction(center, caution_item(5, [15, 0, 25, 20]), right_far), 100), "right")

    # 최종 행동 변경에만 음성 생성
    def test_only_changed_action_is_announced(self):
        """객체 ID가 달라도 행동이 같으면 침묵하고 행동 변경과 해제 뒤에만 안내한다."""
        voice = WalkingVoice()
        left = danger_item(1, [5, 0, 15, 20])
        center = danger_item(2, [45, 0, 55, 20])
        self.assertEqual(voice.observe(prediction(left), 100, .2),
                         ("천천히 가세요.", "walking-straight.mp3"))
        same = prediction(danger_item(3, [10, 0, 20, 20]))
        self.assertIsNone(voice.observe(same, 100, .3))
        self.assertEqual(same["last_action"], "straight")
        self.assertIsNone(voice.observe(prediction(left, center), 100, .4))
        self.assertIsNone(voice.observe(prediction(left, center), 100, .5))
        self.assertEqual(voice.observe(prediction(left, center), 100, .86),
                         ("오른쪽으로 한 걸음",
                          "walking-move-right-one.mp3"))
        cleared = prediction(level="monitor")
        self.assertIsNone(voice.observe(cleared, 100, 1.8))
        self.assertIsNone(voice.observe(cleared, 100, 4.8))
        self.assertIsNone(cleared["last_action"])
        self.assertEqual(voice.observe(prediction(left), 100, 4.9),
                         ("천천히 가세요.", "walking-straight.mp3"))
        self.assertEqual(len(voice.events), 3)

    # 비초록 신호의 횡단보도 대기 중 직진 음성 제외 확인
    def test_waiting_at_non_green_crosswalk_suppresses_only_straight(self):
        """빨간불과 확인 불가 신호에서는 직진만 침묵하고 다른 행동은 유지한다."""
        crosswalk = {"crosswalks": [{"crosswalk_status": "eligible"}]}
        left = danger_item(1, [5, 0, 15, 20])
        center = danger_item(2, [45, 0, 55, 20])
        for state in ("red", "unknown"):
            with self.subTest(state=state):
                voice = WalkingVoice()
                signal = crosswalk | {"signal_state": state}
                straight = prediction(left)
                self.assertIsNone(voice.observe(straight, 100, .1, signal=signal))
                self.assertEqual(straight["last_action"], "straight")
                self.assertIsNone(straight["voice_action"])
                self.assertNotIn("voice_text", straight)
                self.assertEqual(
                    voice.observe(prediction(left, center), 100, .2, signal=signal),
                    ("오른쪽으로 한 걸음",
                     "walking-move-right-one.mp3"),
                )

    # 같은 방향의 걸음 수 변경 중복 안내 방지 확인
    def test_step_count_change_does_not_repeat_same_direction(self):
        """같은 이동 방향이면 걸음 수가 달라져도 새 음성을 내지 않는다."""
        voice = WalkingVoice()
        one_step = prediction(danger_item(1, [20, 0, 48, 20]))
        two_steps = prediction(danger_item(1, [20, 0, 55, 20]))
        self.assertEqual(
            voice.observe(one_step, 100, .1),
            ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"),
        )
        self.assertIsNone(voice.observe(two_steps, 100, .2))
        self.assertEqual(two_steps["voice_steps"], 1)
        confirmed = prediction(danger_item(1, [20, 0, 55, 20]))
        self.assertIsNone(voice.observe(confirmed, 100, 1.2))
        self.assertEqual(confirmed["voice_action"], "right")
        self.assertEqual(confirmed["voice_steps"], 1)

    # 대기 해제 후 직진 음성 복구 확인
    def test_waiting_straight_is_announced_after_green_signal(self):
        """대기 중 억제한 직진 행동은 초록불이 확인되면 새로 안내한다."""
        voice = WalkingVoice()
        left = danger_item(1, [5, 0, 15, 20])
        crosswalk = {"crosswalks": [{"crosswalk_status": "used"}]}
        self.assertIsNone(voice.observe(
            prediction(left), 100, .1,
            signal=crosswalk | {"signal_state": "red"},
        ))
        self.assertEqual(voice.observe(
            prediction(left), 100, .2,
            signal=crosswalk | {"signal_state": "green"},
        ), ("천천히 가세요.", "walking-straight.mp3"))

    # 전방 횡단보도 근거 없는 직진 음성 유지 확인
    def test_non_green_signal_without_front_crosswalk_keeps_straight(self):
        """횡단보도가 없거나 위치에서 탈락했으면 비초록 신호도 직진을 안내한다."""
        cases = (
            {"signal_state": "red", "crosswalks": []},
            {"signal_state": "unknown", "crosswalks": [
                {"crosswalk_status": "position_rejected"},
            ]},
        )
        for signal in cases:
            with self.subTest(signal=signal):
                voice = WalkingVoice()
                self.assertEqual(
                    voice.observe(prediction(danger_item(1, [5, 0, 15, 20])),
                                  100, .1, signal=signal),
                    ("천천히 가세요.", "walking-straight.mp3"),
                )

    # 횡단 중 차량 외 장애물 음성 제외 확인
    def test_crossing_announces_only_vehicle_obstacles(self):
        """횡단 중 사람과 일반 장애물은 침묵하고 네 차량 클래스만 안내한다."""
        for name in ("person", "pole"):
            with self.subTest(suppressed=name):
                voice = WalkingVoice()
                result = prediction(danger_item(1, [45, 0, 55, 20], name))
                self.assertIsNone(voice.observe(result, 100, .1, crossing_active=True))
                self.assertEqual(result["last_action"], "stop")
                self.assertIsNone(result["voice_action"])
                self.assertNotIn("voice_text", result)
        for name in ("car", "bus", "truck", "motorcycle"):
            with self.subTest(allowed=name):
                voice = WalkingVoice()
                result = prediction(danger_item(1, [45, 0, 55, 20], name))
                self.assertEqual(
                    voice.observe(result, 100, .1, crossing_active=True),
                    ("멈추세요", "walking-stop.mp3"),
                )
                self.assertEqual(result["last_action"], "stop")
                self.assertEqual(result["voice_action"], "stop")

    # 횡단 접근 중 차량 외 장애물 음성 제외 확인
    def test_approach_announces_only_vehicle_obstacles(self):
        """approach에서는 사람과 일반 장애물은 침묵하고 차량만 안내한다."""
        for name in ("person", "bicycle", "bollard"):
            with self.subTest(suppressed=name):
                voice = WalkingVoice()
                result = prediction(danger_item(1, [45, 0, 55, 20], name))
                self.assertIsNone(voice.observe(
                    result, 100, .1, crosswalk_status="approach"))
                self.assertEqual(result["last_action"], "stop")
                self.assertIsNone(result["voice_action"])
                self.assertNotIn("voice_text", result)
        for name in ("car", "bus", "truck", "motorcycle"):
            with self.subTest(allowed=name):
                voice = WalkingVoice()
                result = prediction(danger_item(1, [45, 0, 55, 20], name))
                self.assertEqual(
                    voice.observe(result, 100, .1, crosswalk_status="approach"),
                    ("멈추세요", "walking-stop.mp3"),
                )

    # 횡단 접근 시 기존 비차량 음성 중단 확인
    def test_approach_suppression_stops_previous_walking_audio(self):
        """approach 진입 전에 시작한 사람 안내는 즉시 중단 이벤트를 기록한다."""
        voice = WalkingVoice()
        person = prediction(danger_item(1, [5, 0, 15, 20]))
        self.assertIsNotNone(voice.observe(person, 100, .1))
        self.assertIsNone(voice.observe(
            person, 100, .2, crosswalk_status="approach"))
        self.assertEqual(voice.events[-1], (.2, None))

    # 횡단 진입 시 기존 비차량 음성 중단 확인
    def test_crossing_suppression_stops_previous_walking_audio(self):
        """진입 전에 시작한 사람 안내는 횡단 상태에서 중단 이벤트를 기록한다."""
        voice = WalkingVoice()
        person = prediction(danger_item(1, [5, 0, 15, 20]))
        self.assertIsNotNone(voice.observe(person, 100, .1))
        self.assertIsNone(voice.observe(person, 100, .2, crossing_active=True))
        self.assertEqual(voice.events[-1], (.2, None))

    # 객체 없는 위험 처리
    def test_non_object_warning_is_silent(self):
        """대표 객체가 없는 촬영 상태 위험에는 이동 행동을 안내하지 않는다."""
        item = danger_item(1, [45, 0, 55, 20])
        result = prediction(item, source="camera_view")
        result["warning"]["detection_index"] = None
        self.assertIsNone(walking_action(result, 100))

    # 비초록 신호의 횡단보도 장애물 음성 제외 확인
    def test_non_green_signal_suppresses_only_crosswalk_obstacle_voice(self):
        """빨간불과 확인 중 신호에는 횡단보도 위 위험만 음성에서 제외한다."""
        on_crosswalk = danger_item(1, [40, 20, 60, 80])
        outside = danger_item(2, [80, 20, 95, 80], "car")
        result = prediction(on_crosswalk, outside)
        class_map = np.zeros((100, 100), np.uint8)
        class_map[76:84, 35:65] = 2
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_nonwalkable_threshold": .50,
            "non_green_obstacle_contact_half_height": .02,
        }
        for state in ("red", "unknown"):
            on_crosswalk.pop("voice_suppressed_reason", None)
            suppress_non_green_crosswalk_voice(
                result, {"signal_state": state, "selected_detection_index": 0},
                class_map, {"crosswalk": 2}, (100, 100, 3), config,
            )
            self.assertEqual(on_crosswalk["voice_suppressed_reason"],
                             "non_green_signal_crosswalk_obstacle")
        self.assertNotIn("voice_suppressed_reason", outside)
        self.assertEqual(walking_action(result, 100), "straight")

    # 비초록 신호의 보행불가 영역 장애물 음성 제외 확인
    def test_non_green_signal_suppresses_nonwalkable_obstacle_voice(self):
        """빨간불과 확인 중 신호에는 보행불가 영역 위 위험도 음성에서 제외한다."""
        item = danger_item(1, [40, 20, 60, 80])
        result = prediction(item)
        class_map = np.zeros((100, 100), np.uint8)
        class_map[76:84, 35:65] = 3
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_nonwalkable_threshold": .50,
            "non_green_obstacle_contact_half_height": .02,
        }
        for state in ("red", "unknown"):
            suppress_non_green_crosswalk_voice(
                result, {"signal_state": state, "selected_detection_index": 0},
                class_map, {"crosswalk": 2, "non_walkable": 3}, (100, 100, 3), config,
            )
            self.assertEqual(item["voice_suppressed_reason"],
                             "non_green_signal_nonwalkable_obstacle")
            self.assertEqual(item["nonwalkable_contact_fraction"], 1.0)
            self.assertIsNone(walking_action(result, 100))

    # 보행불가 접촉 비율 50% 경계 확인
    def test_nonwalkable_obstacle_voice_threshold_is_fifty_percent(self):
        """발밑 보행불가 비율이 50% 이상일 때만 위험 음성을 제외한다."""
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_nonwalkable_threshold": .50,
            "non_green_obstacle_contact_half_height": .02,
        }
        for nonwalkable_pixels, suppressed in ((39, False), (40, True)):
            with self.subTest(nonwalkable_pixels=nonwalkable_pixels):
                item = danger_item(1, [40, 20, 60, 80])
                result = prediction(item)
                class_map = np.zeros((100, 100), np.uint8)
                contact_patch = class_map[78:82, 40:60]
                contact_patch.flat[:nonwalkable_pixels] = 3
                suppress_non_green_crosswalk_voice(
                    result, {"signal_state": "red", "selected_detection_index": 0},
                    class_map, {"crosswalk": 2, "non_walkable": 3},
                    (100, 100, 3), config,
                )
                self.assertAlmostEqual(
                    item["nonwalkable_contact_fraction"], nonwalkable_pixels / 80)
                if suppressed:
                    self.assertEqual(
                        item["voice_suppressed_reason"],
                        "non_green_signal_nonwalkable_obstacle",
                    )
                    self.assertIsNone(walking_action(result, 100))
                else:
                    self.assertNotIn("voice_suppressed_reason", item)
                    self.assertEqual(walking_action(result, 100), "stop")

    # 초록불·신호 미선택·마스크 미확인 시 음성 유지 확인
    def test_voice_is_not_suppressed_without_selected_non_green_evidence(self):
        """초록불이거나 신호·횡단보도 근거가 없으면 위험 음성을 유지한다."""
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_nonwalkable_threshold": .50,
            "non_green_obstacle_contact_half_height": .02,
        }
        cases = (
            ({"signal_state": "green", "selected_detection_index": 0}, np.full((100, 100), 2)),
            ({"signal_state": "red", "selected_detection_index": None}, np.full((100, 100), 2)),
            ({"signal_state": "red", "selected_detection_index": 0}, None),
        )
        for signal, class_map in cases:
            with self.subTest(signal=signal["signal_state"], mask=class_map is not None):
                item = danger_item(1, [40, 20, 60, 80])
                result = prediction(item)
                suppress_non_green_crosswalk_voice(
                    result, signal, class_map, {"crosswalk": 2}, (100, 100, 3), config)
                self.assertNotIn("voice_suppressed_reason", item)
                self.assertEqual(walking_action(result, 100), "stop")

    # 저장 영상 식별자 문자열 확인
    def test_overlay_identity_contains_track_and_event_ids(self):
        """저장 영상 장애물 라벨에 추적 ID와 위험 이벤트 ID를 함께 표시한다."""
        self.assertEqual(risk_identity({"track_id": 12, "event_id": 34}), "T12 · E34")

    # 저장 영상의 행동·음성 배지 문구 확인
    def test_overlay_action_status_distinguishes_voice_and_muted(self):
        """화면 행동과 선택 음성 또는 무음 상태를 서로 다른 줄로 표시한다."""
        self.assertEqual(action_status_text(
            {"last_action": "left", "voice_playback_action": "right"}),
            ("ACTION: left", "VOICE: right", "MOTION: unavailable"))
        self.assertEqual(action_status_text(
            {"last_action": "stop", "voice_action": None}),
            ("ACTION: stop", "VOICE: none", "MOTION: unavailable"))
        self.assertEqual(action_status_text(
            {"stationarity": {"status": "stationary"}}),
            ("ACTION: none", "VOICE: none", "MOTION: stationary"))

    # 새 이벤트가 이전 음성을 끊는 PCM 결과 확인
    def test_voice_track_starts_at_frame_time_and_is_video_length(self):
        """새 안내 시점에 이전 음성을 끊고 영상 끝에서 정확히 종료한다."""
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "voice.wav"
            events = [(0.5, "walking-move-left-one.mp3"),
                      (0.6, "walking-move-right-one.mp3")]
            render_voice_track(events, 1.0, output)
            with wave.open(str(output), "rb") as sound:
                self.assertEqual(sound.getframerate(), SAMPLE_RATE)
                self.assertEqual(sound.getnframes(), SAMPLE_RATE)
                samples = np.frombuffer(sound.readframes(SAMPLE_RATE), dtype="<i2")
            self.assertFalse(np.any(samples[:SAMPLE_RATE // 2]))
            self.assertTrue(np.any(samples[SAMPLE_RATE // 2:]))

    # 실제 영상에 음성 트랙 합성
    def test_result_mp4_contains_voice_and_log(self):
        """위험 안내를 MP4 오디오 스트림에 넣고 JSONL에 문장을 기록한다."""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            output = Path(folder) / "result.mp4"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
            self.assertTrue(writer.isOpened())
            for _ in range(20):
                writer.write(np.full((48, 64, 3), 100, dtype=np.uint8))
            writer.release()
            item = danger_item(1, [5, 10, 15, 40])
            engine = SimpleNamespace(update=Mock(side_effect=lambda *args: prediction(item)),
                                     add_sidewalk_context=Mock())
            detector = SimpleNamespace(predict=Mock(return_value=[]))
            with patch("src.pipeline.RiskEngine", return_value=engine), patch(
                "src.pipeline.draw_risk", side_effect=lambda frame, *_: frame
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(process_video(source, output, detector=detector,
                                               risk_config={"enabled": True}), 20)
            probe = subprocess.run(
                [ffmpeg_executable(), "-v", "error", "-i", str(output),
                 "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
                capture_output=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors="replace"))
            self.assertTrue(np.any(np.frombuffer(probe.stdout, dtype="<i2")))
            capture = cv2.VideoCapture(str(output))
            self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_COUNT)), 20)
            capture.release()
            lines = risk_log_path(output).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 20)
            self.assertEqual(json.loads(lines[0])["voice_text"], "천천히 가세요.")
            self.assertEqual(json.loads(lines[0])["voice_clip"], "walking-straight.mp3")
            self.assertEqual(json.loads(lines[0])["last_action"], "straight")
            self.assertEqual(json.loads(lines[0])["voice_playback_action"], "straight")
            self.assertNotIn("voice_clip", json.loads(lines[1]))
            self.assertIsNone(json.loads(lines[-1])["voice_playback_action"])
            self.assertEqual(list(Path(folder).glob("*.partial.*")), [])

    # 음성 합성 오류 시 결과 파일 정리
    def test_audio_mux_failure_does_not_publish_result(self):
        """음성 합성에 실패하면 MP4와 JSONL 및 임시 파일을 모두 정리한다."""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            output = Path(folder) / "result.mp4"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
            self.assertTrue(writer.isOpened())
            writer.write(np.full((48, 64, 3), 100, dtype=np.uint8))
            writer.release()
            item = danger_item(1, [5, 10, 15, 40])
            engine = SimpleNamespace(update=Mock(side_effect=lambda *args: prediction(item)),
                                     add_sidewalk_context=Mock())
            detector = SimpleNamespace(predict=Mock(return_value=[]))
            with patch("src.pipeline.RiskEngine", return_value=engine), patch(
                "src.pipeline.draw_risk", side_effect=lambda frame, *_: frame
            ), patch("src.pipeline.mux_voice", side_effect=RuntimeError("합성 오류")), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "합성 오류"):
                    process_video(source, output, detector=detector, risk_config={"enabled": True})
            self.assertFalse(output.exists())
            self.assertFalse(risk_log_path(output).exists())
            self.assertEqual(list(Path(folder).glob("*.partial.*")), [])
