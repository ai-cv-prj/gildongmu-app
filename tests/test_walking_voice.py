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
    apply_obstacle_voice_suppression,
    movement_steps,
    repeat_none_s,
    transition_confirm_s,
    walkable_side_fractions,
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


# 음성 제외 테스트용 U자 띠 마스크 만들기
def voice_u_strip_map(walkable_pixels=0, label=1):
    """100x100 프레임의 테스트 bbox 바깥 U자 띠에 지정 픽셀 수만큼 라벨을 채운다."""
    class_map = np.zeros((100, 100), np.uint8)
    strip = np.zeros_like(class_map, dtype=bool)
    strip[68:80, 36:40] = True
    strip[68:80, 60:64] = True
    strip[80:84, 40:60] = True
    coordinates = np.argwhere(strip)
    selected = coordinates[:walkable_pixels]
    if selected.size:
        class_map[selected[:, 0], selected[:, 1]] = label
    return class_map


# 화면 하단에 잘린 bbox의 음성 제외 테스트용 띠 마스크 만들기
def partial_voice_u_strip_map(walkable_pixels=0):
    """아래 띠가 보이지 않는 테스트 bbox의 좌우 띠에 walkable을 채운다."""
    class_map = np.zeros((100, 100), np.uint8)
    strip = np.zeros_like(class_map, dtype=bool)
    strip[84:100, 36:40] = True
    strip[84:100, 60:64] = True
    coordinates = np.argwhere(strip)
    selected = coordinates[:walkable_pixels]
    if selected.size:
        class_map[selected[:, 0], selected[:, 1]] = 1
    return class_map


class WalkingVoiceTests(unittest.TestCase):
    """보행 위험의 행동 음성 선택 및 영상 저장 규칙을 확인한다."""

    # 모든 직접 행동 전환의 안정화 시간 확인
    def test_all_direct_action_transition_timings(self):
        """
        이전·다음 행동 조합에 즉시 확정 또는 200ms, 500ms 정책을 적용한다.
        """
        actions = (None, "left", "right", "crowded", "blocked", "stop")
        for previous in actions:
            for current in ("left", "right", "crowded", "blocked", "stop"):
                if previous is None or current == "stop":
                    expected = 0.0
                elif previous == "stop":
                    expected = 0.5
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
            "crowded": ("전방 혼잡 주의하세요", "walking-crowded.mp3"),
            "blocked": ("전방 장애물 주의하세요", "walking-obstacle.mp3"),
            "stop": ("멈추세요", "walking-stop.mp3"),
        }
        actions = ("left", "right", "crowded", "blocked", "stop")
        for previous in actions:
            for current in actions:
                with self.subTest(previous=previous, current=current):
                    voice = WalkingVoice()
                    voice.last_action = previous
                    voice.last_steps = 1 if previous in ("left", "right") else None
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
                        self.assertIsNone(voice.observe(
                            prediction(level="monitor"), 100, 1.0 + delay - .001))
                        confirmed = voice.observe(
                            prediction(level="monitor"), 100, 1.0 + delay)
                        self.assertEqual(confirmed, messages[current])

    # 방향 외 같은 행동의 none 후 재생 기준 확인
    def test_non_direction_replay_requires_configured_none_duration(self):
        """
        혼잡·장애물·정지는 1.5초 none 후에만 같은 음성을 재생한다.
        """
        messages = {
            "left": ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"),
            "right": ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"),
            "crowded": ("전방 혼잡 주의하세요", "walking-crowded.mp3"),
            "blocked": ("전방 장애물 주의하세요", "walking-obstacle.mp3"),
            "stop": ("멈추세요", "walking-stop.mp3"),
        }
        for action in ("crowded", "blocked", "stop"):
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

    # 같은 좌우 방향의 1.5초 간격 재안내 확인
    def test_same_direction_repeats_after_one_point_five_seconds_regardless_of_none(self):
        """중간 none과 관계없이 마지막 같은 방향 음성에서 1.5초 뒤 현재 방향을 재안내한다."""
        for action, message in (
            ("left", ("왼쪽으로 한 걸음", "walking-move-left-one.mp3")),
            ("right", ("오른쪽으로 한 걸음", "walking-move-right-one.mp3")),
        ):
            with self.subTest(action=action):
                voice = WalkingVoice()
                with patch("src.walking_voice.walking_action", return_value=action):
                    self.assertEqual(
                        voice.observe(prediction(level="monitor"), 100, 0.0), message)
                with patch("src.walking_voice.walking_action", return_value=None):
                    self.assertIsNone(
                        voice.observe(prediction(level="monitor"), 100, 0.2))
                with patch("src.walking_voice.walking_action", return_value=action):
                    self.assertIsNone(
                        voice.observe(prediction(level="monitor"), 100, 0.6))
                    self.assertEqual(
                        voice.observe(prediction(level="monitor"), 100, 1.5), message)
                    self.assertIsNone(
                        voice.observe(prediction(level="monitor"), 100, 2.0))
                    self.assertEqual(
                        voice.observe(prediction(level="monitor"), 100, 3.0), message)

    # none 후 일반적인 다른 행동의 즉시 재생 확인
    def test_different_action_after_none_is_announced_immediately(self):
        """
        none 뒤의 다른 행동은 첫 감지 시 바로 안내한다.
        """
        messages = {
            "left": ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"),
            "right": ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"),
            "crowded": ("전방 혼잡 주의하세요", "walking-crowded.mp3"),
            "blocked": ("전방 장애물 주의하세요", "walking-obstacle.mp3"),
            "stop": ("멈추세요", "walking-stop.mp3"),
        }
        actions = ("left", "right", "crowded", "blocked", "stop")
        for previous in actions:
            for current in actions:
                if current == previous:
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
        self.assertEqual(warning_directions(danger_item(1, [20, 0, 31, 20]), 100), {"left"})
        self.assertEqual(warning_directions(danger_item(1, [69, 0, 80, 20]), 100), {"right"})

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
        self.assertIsNone(movement_steps(two, "stop", 100))

    # 핑크 ROI 내부 위험만 음성 행동에 포함
    def test_only_danger_inside_pink_roi_triggers_guidance(self):
        """위험이어도 핑크 ROI 겹침이 10% 미만이면 음성 행동을 만들지 않는다."""
        outside = danger_item(1, [45, 0, 55, 20], geometry={"immediate_overlap": .09})
        inside = danger_item(2, [45, 0, 55, 20], geometry={"immediate_overlap": .10})
        self.assertIsNone(walking_action(prediction(outside), 100))
        self.assertEqual(walking_action(prediction(inside), 100), "blocked")

    # 정지 중 분홍색 ROI 장애물 음소거 확인
    def test_stationary_guidance_mutes_all_pink_roi_obstacles(self):
        """정지 중에는 일반 위험과 빠른 접근 위험을 모두 안내하지 않는다."""
        for class_name in ("person", "car"):
            with self.subTest(class_name=class_name):
                rapid = danger_item(
                    1, [45, 0, 55, 20], class_name,
                    geometry={"immediate_overlap": 1.0},
                    motion={"quality": "valid"}, reasons=["short_ttc"])
                regular = danger_item(
                    2, [45, 0, 55, 20], class_name,
                    geometry={"immediate_overlap": 1.0})
                rapid_prediction = prediction(rapid)
                rapid_prediction["stationarity"] = {"status": "stationary"}
                regular_prediction = prediction(regular)
                regular_prediction["stationarity"] = {"status": "stationary"}
                self.assertIsNone(walking_action(rapid_prediction, 100, stationary_voice=True))
                self.assertIsNone(walking_action(regular_prediction, 100, stationary_voice=True))

    # 비정지 상태의 기존 음성 범위 유지 확인
    def test_nonstationary_guidance_keeps_existing_pink_roi_rule(self):
        """실제 비정지 상태에서는 분홍색 ROI 객체를 기존처럼 안내한다."""
        for status in ("moving", "confirming", "uncertain", "disabled"):
            with self.subTest(status=status):
                item = danger_item(
                    1, [45, 0, 55, 20], geometry={"immediate_overlap": 1.0})
                result = prediction(item)
                result["stationarity"] = {"status": status}
                self.assertEqual(walking_action(result, 100, stationary_voice=True), "blocked")

    # 정지 중 화면 행동과 횡단 차량 음성 분리 확인
    def test_stationary_filter_keeps_action_but_mutes_crossing_vehicle_voice(self):
        """화면 ACTION은 유지하고 횡단 중 차량 장애물 음성도 내지 않는다."""
        vehicle = danger_item(
            1, [45, 0, 55, 20], "car",
            geometry={"immediate_overlap": 1.0})
        result = prediction(vehicle)
        result["stationarity"] = {"status": "stationary"}
        self.assertEqual(walking_action(result, 100), "blocked")
        self.assertIsNone(walking_action(result, 100, True, True))

    # 정지 중 화면 상태와 실제 음성 분리 확인
    def test_stationary_keeps_action_but_mutes_active_voice(self):
        """정지 중에도 위험 표시는 유지하면서 재생 중인 장애물 음성을 억제한다."""
        item = danger_item(
            1, [45, 0, 55, 20], "car",
            geometry={"immediate_overlap": 1.0})
        result = prediction(item)
        result["stationarity"] = {"status": "stationary"}
        voice = WalkingVoice()
        active = prediction(danger_item(2, [45, 0, 55, 20]))
        self.assertEqual(voice.observe(active, 100, 3.0),
                         ("전방 장애물 주의하세요", "walking-obstacle.mp3"))
        self.assertIsNone(voice.observe(result, 100, 3.1))
        self.assertEqual(result["last_action"], "blocked")
        self.assertIsNone(result["voice_action"])
        self.assertTrue(result["voice_clear"])
        self.assertEqual(voice.events[-1], (3.1, None))

    # 기본 위험 분포의 안내 없음·회피·정지 확인
    def test_danger_distribution_selects_basic_action(self):
        """세 방향의 위험 조합을 안내 없음·좌우 이동·정지로 바꾼다."""
        left = danger_item(1, [5, 0, 15, 20])
        center = danger_item(2, [45, 0, 55, 20])
        right = danger_item(3, [85, 0, 95, 20])
        cases = [
            (prediction(level="monitor"), None),
            (prediction(left), None),
            (prediction(right), None),
            (prediction(left, center), "right"),
            (prediction(center, right), "left"),
            (prediction(left, right), None),
            (prediction(left, center, right), "crowded"),
            (prediction(center), "blocked"),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(walking_action(result, 100), expected)

    # 세 방향 위험의 객체 종류와 무관한 혼잡 안내 확인
    def test_all_direction_dangers_are_crowded_regardless_of_class(self):
        """세 방향이 모두 위험하면 객체 종류와 관계없이 전방 혼잡으로 판단한다."""
        left = danger_item(1, [5, 0, 15, 20])
        center = danger_item(2, [45, 0, 55, 20])
        right_person = danger_item(3, [85, 0, 95, 20])
        right_bollard = danger_item(4, [85, 0, 95, 20], "bollard")
        self.assertEqual(
            walking_action(prediction(left, center, right_person), 100), "crowded")
        self.assertEqual(
            walking_action(prediction(left, center, right_bollard), 100), "crowded")

    # 가운데 위험의 거리 우선 비교
    def test_center_danger_chooses_farther_side_after_three_percent_tie(self):
        """가운데 위험의 거리 차이가 3% 이하면 정지하고 초과하면 먼 방향을 선택한다."""
        self.assertEqual(walking_action(prediction(danger_item(1, [35, 0, 45, 20])), 100),
                         "right")
        self.assertEqual(walking_action(prediction(danger_item(1, [55, 0, 65, 20])), 100),
                         "left")
        self.assertEqual(walking_action(prediction(danger_item(1, [46, 0, 57, 20])), 100),
                         "blocked")
        self.assertEqual(walking_action(prediction(danger_item(1, [47, 0, 57, 20])), 100),
                         "left")

    # 위험 거리 동률에서 주의 객체를 무시하고 정지
    def test_center_danger_stops_on_distance_tie_regardless_of_cautions(self):
        """위험 거리 차이가 3% 이하면 주의 객체의 분포와 관계없이 정지한다."""
        center = danger_item(1, [45, 0, 55, 20])
        left_caution = caution_item(2, [5, 0, 15, 20])
        right_near = caution_item(3, [70, 0, 80, 20])
        right_far = caution_item(4, [88, 0, 98, 20])
        self.assertEqual(walking_action(
            prediction(center, left_caution, right_near, right_far), 100), "blocked")
        self.assertEqual(walking_action(
            prediction(center, caution_item(5, [15, 0, 25, 20]), right_far), 100), "blocked")

    # 위험 거리 동률에서 전체 화면 보행 가능 비율 비교
    def test_center_danger_uses_more_walkable_screen_half_after_distance_tie(self):
        """위험 거리가 비슷하면 전체 화면에서 walkable 비율이 5% 넘게 큰 쪽을 택한다."""
        center = danger_item(1, [45, 0, 55, 20])
        class_map = np.zeros((100, 100), np.uint8)
        class_map[:30, :50] = 1
        class_map[:20, 50:] = 1
        result = prediction(center)
        result["walkable_sides"] = walkable_side_fractions(
            class_map, {"walkable": 1, "crosswalk": 2}, class_map.shape)
        self.assertEqual(result["walkable_sides"], {
            "status": "available", "left": .30, "right": .20})
        self.assertEqual(walking_action(result, 100), "left")

        class_map[:30, :50] = 0
        class_map[:10, :50] = 1
        result["walkable_sides"] = walkable_side_fractions(
            class_map, {"walkable": 1}, class_map.shape)
        self.assertEqual(walking_action(result, 100), "right")

    # 전체 화면 보행 가능 비율도 비슷하면 장애물 안내 유지
    def test_center_danger_stays_blocked_when_walkable_side_difference_is_small(self):
        """좌우 walkable 비율 차이가 5% 이하면 방향을 추정하지 않는다."""
        class_map = np.zeros((100, 100), np.uint8)
        class_map[:24, :50] = 1
        class_map[:20, 50:] = 1
        result = prediction(danger_item(1, [45, 0, 55, 20]))
        result["walkable_sides"] = walkable_side_fractions(
            class_map, {"walkable": 1}, class_map.shape)
        self.assertEqual(walking_action(result, 100), "blocked")

    # 최종 행동 변경에만 음성 생성
    def test_only_changed_action_is_announced(self):
        """같은 행동은 반복하지 않고 방향 확정과 충분한 none 이후에만 안내한다."""
        voice = WalkingVoice()
        first = prediction(danger_item(1, [20, 0, 43, 20]))
        self.assertEqual(voice.observe(first, 100, .2),
                         ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"))
        same = prediction(danger_item(2, [20, 0, 43, 20]))
        self.assertIsNone(voice.observe(same, 100, .3))
        self.assertEqual(same["last_action"], "right")
        # 같은 객체들이 반대쪽으로 이동하면 새 방향을 200ms 확인한다.
        opposite = [danger_item(i, [57, 0, 80, 20]) for i in (1, 2)]
        self.assertIsNone(voice.observe(prediction(*opposite), 100, .4))
        self.assertIsNone(voice.observe(prediction(*opposite), 100, .5))
        self.assertEqual(voice.observe(prediction(*opposite), 100, .6),
                         ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"))
        for timestamp in (1.5, 2.5, 3.5, 4.5):
            cleared = prediction(level="monitor")
            self.assertIsNone(voice.observe(cleared, 100, timestamp))
        self.assertIsNone(cleared["last_action"])
        self.assertEqual(voice.observe(prediction(*opposite), 100, 4.6),
                         ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"))
        self.assertEqual(len(voice.events), 3)

    # 혼잡 안내 직후 방향 음성을 혼잡 경고로 치환
    def test_crowded_redirects_new_directions_during_protection_window(self):
        """혼잡 안내 후 3초 동안 새 좌우 행동은 방향 대신 혼잡 음성을 다시 낸다."""
        voice = WalkingVoice()
        result = prediction(level="monitor")
        crowded = ("전방 혼잡 주의하세요", "walking-crowded.mp3")
        with patch("src.walking_voice.walking_action", return_value="crowded"):
            self.assertEqual(voice.observe(result, 100, 0.0), crowded)

        with patch("src.walking_voice.walking_action", return_value="left"):
            left = prediction(level="monitor")
            self.assertEqual(voice.observe(left, 100, 1.0), crowded)
            self.assertEqual(left["last_action"], "left")
            self.assertEqual(left["voice_action"], "crowded")
            self.assertIsNone(voice.observe(prediction(level="monitor"), 100, 1.1))

        with patch("src.walking_voice.walking_action", return_value="right"):
            self.assertEqual(voice.observe(prediction(level="monitor"), 100, 1.2), crowded)

        with patch("src.walking_voice.walking_action", return_value=None):
            silent = prediction(level="monitor")
            self.assertIsNone(voice.observe(silent, 100, 1.3))
            self.assertIsNone(silent["voice_action"])

        with patch("src.walking_voice.walking_action", return_value="left"):
            self.assertEqual(voice.observe(prediction(level="monitor"), 100, 1.4), crowded)
            self.assertIsNone(voice.observe(prediction(level="monitor"), 100, 3.1))
            self.assertEqual(
                voice.observe(prediction(level="monitor"), 100, 3.3),
                ("왼쪽으로 한 걸음", "walking-move-left-one.mp3"),
            )

    # 혼잡 안내 후 위험 행동이 없으면 추가 안내하지 않음
    def test_crowded_protection_does_not_announce_during_continuous_none(self):
        """혼잡 보호 시간 동안 none만 이어지면 혼잡 음성을 반복하지 않는다."""
        voice = WalkingVoice()
        with patch("src.walking_voice.walking_action", return_value="crowded"):
            self.assertIsNotNone(voice.observe(prediction(level="monitor"), 100, 0.0))
        with patch("src.walking_voice.walking_action", return_value=None):
            for timestamp in (0.5, 1.5, 2.5, 3.1):
                result = prediction(level="monitor")
                self.assertIsNone(voice.observe(result, 100, timestamp))
                self.assertIsNone(result["voice_action"])
        self.assertEqual(len(voice.events), 1)

    # 측면 위험의 안내 없음 확인
    def test_non_avoidance_hazards_keep_risk_without_action_or_voice(self):
        """위험 표시를 보존하면서 과거 서행 상황의 행동과 음성 이벤트를 만들지 않는다."""
        left = danger_item(1, [5, 0, 15, 20])
        right = danger_item(2, [85, 0, 95, 20])
        for items in ((left,), (right,), (left, right)):
            with self.subTest(objects=len(items)):
                voice = WalkingVoice()
                for timestamp in (0, 1, 2, 3, 4):
                    result = prediction(*items)
                    self.assertIsNone(voice.observe(result, 100, timestamp))
                    self.assertIsNone(result["last_action"])
                    self.assertIsNone(result["voice_action"])
                    self.assertIsNone(result["voice_diagnostics"]["raw_action"])
                    self.assertNotIn("voice_event", result)
                    self.assertNotIn("voice_text", result)
                    self.assertNotIn("voice_clip", result)
                    self.assertEqual(result["warning"]["level"], "danger")
                    self.assertTrue(all(item["alert_level"] == "danger"
                                        for item in result["detections"]))
                self.assertEqual(voice.events, [])

    # 같은 방향의 걸음 수 변경과 1.5초 재안내 확인
    def test_step_count_change_waits_for_same_direction_repeat(self):
        """걸음 수 변화로 즉시 안내하지 않고 1.5초 후 기존 방향·걸음 수를 반복한다."""
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
        self.assertEqual(
            voice.observe(confirmed, 100, 1.6),
            ("오른쪽으로 한 걸음", "walking-move-right-one.mp3"),
        )
        self.assertEqual(confirmed["voice_action"], "right")
        self.assertEqual(confirmed["voice_steps"], 1)

    # 횡단 중 차량 외 장애물 음성 제외 확인
    def test_crossing_announces_only_vehicle_obstacles(self):
        """횡단 중 사람과 일반 장애물은 침묵하고 네 차량 클래스만 안내한다."""
        for name in ("person", "pole"):
            with self.subTest(suppressed=name):
                voice = WalkingVoice()
                result = prediction(danger_item(1, [45, 0, 55, 20], name))
                self.assertIsNone(voice.observe(result, 100, .1, crossing_active=True))
                self.assertEqual(result["last_action"], "blocked")
                self.assertIsNone(result["voice_action"])
                self.assertNotIn("voice_text", result)
        for name in ("car", "bus", "truck", "motorcycle"):
            with self.subTest(allowed=name):
                voice = WalkingVoice()
                result = prediction(danger_item(1, [45, 0, 55, 20], name))
                self.assertEqual(
                    voice.observe(result, 100, .1, crossing_active=True),
                    ("전방 장애물 주의하세요", "walking-obstacle.mp3"),
                )
                self.assertEqual(result["last_action"], "blocked")
                self.assertEqual(result["voice_action"], "blocked")

    # 횡단 접근 중 차량 외 장애물 음성 제외 확인
    def test_approach_announces_only_vehicle_obstacles(self):
        """approach에서는 사람과 일반 장애물은 침묵하고 차량만 안내한다."""
        for name in ("person", "bicycle", "bollard"):
            with self.subTest(suppressed=name):
                voice = WalkingVoice()
                result = prediction(danger_item(1, [45, 0, 55, 20], name))
                self.assertIsNone(voice.observe(
                    result, 100, .1, crosswalk_status="approach"))
                self.assertEqual(result["last_action"], "blocked")
                self.assertIsNone(result["voice_action"])
                self.assertNotIn("voice_text", result)
        for name in ("car", "bus", "truck", "motorcycle"):
            with self.subTest(allowed=name):
                voice = WalkingVoice()
                result = prediction(danger_item(1, [45, 0, 55, 20], name))
                self.assertEqual(
                    voice.observe(result, 100, .1, crosswalk_status="approach"),
                    ("전방 장애물 주의하세요", "walking-obstacle.mp3"),
                )

    # 횡단 접근 시 기존 비차량 음성 중단 확인
    def test_approach_suppression_stops_previous_walking_audio(self):
        """approach 진입 전에 시작한 사람 안내는 즉시 중단 이벤트를 기록한다."""
        voice = WalkingVoice()
        person = prediction(danger_item(1, [45, 0, 55, 20]))
        self.assertIsNotNone(voice.observe(person, 100, .1))
        self.assertIsNone(voice.observe(
            person, 100, .2, crosswalk_status="approach"))
        self.assertEqual(voice.events[-1], (.2, None))

    # 횡단 진입 시 기존 비차량 음성 중단 확인
    def test_crossing_suppression_stops_previous_walking_audio(self):
        """진입 전에 시작한 사람 안내는 횡단 상태에서 중단 이벤트를 기록한다."""
        voice = WalkingVoice()
        person = prediction(danger_item(1, [45, 0, 55, 20]))
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
        outside = danger_item(2, [60, 20, 95, 80], "car")
        result = prediction(on_crosswalk, outside)
        class_map = np.ones((100, 100), np.uint8)
        class_map[76:84, 35:65] = 2
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_crosswalk_contact_half_height": .02,
            "obstacle_surrounding_voice_suppression": True,
            "obstacle_surrounding_walkable_threshold": .30,
            "obstacle_surrounding_partial_walkable_threshold": .05,
        }
        for state in ("red", "unknown"):
            on_crosswalk.pop("voice_suppressed_reason", None)
            apply_obstacle_voice_suppression(
                result, {"signal_state": state, "selected_detection_index": 0},
                class_map, {"walkable": 1, "crosswalk": 2}, (100, 100, 3), config,
            )
            self.assertEqual(on_crosswalk["voice_suppressed_reason"],
                             "non_green_signal_crosswalk_obstacle")
        self.assertNotIn("voice_suppressed_reason", outside)
        self.assertEqual(walking_action(result, 100), "left")

    # 신호와 무관한 bbox 바깥 띠 음성 제외 확인
    def test_low_walkable_surroundings_suppress_voice_for_every_signal(self):
        """바깥 띠의 통합 보행가능 비율이 30% 이하면 신호와 관계없이 음성을 제외한다."""
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_crosswalk_contact_half_height": .02,
            "obstacle_surrounding_voice_suppression": True,
            "obstacle_surrounding_walkable_threshold": .30,
        }
        for state, selected in (("green", 0), ("red", 0), ("unknown", 0), ("red", None)):
            with self.subTest(state=state, selected=selected):
                item = danger_item(1, [40, 20, 60, 80])
                result = prediction(item)
                apply_obstacle_voice_suppression(
                    result, {"signal_state": state, "selected_detection_index": selected},
                    np.zeros((100, 100), np.uint8), {"walkable": 1, "crosswalk": 2},
                    (100, 100, 3), config,
                )
                self.assertEqual(item["voice_suppressed_reason"], "low_walkable_surroundings")
                self.assertEqual(
                    item["voice_surrounding_walkability"]["walkable_fraction"], 0.0)
                self.assertIsNone(walking_action(result, 100))

    # bbox 바깥 띠 통합 비율의 30% 경계 확인
    def test_surrounding_voice_full_strip_threshold(self):
        """세 띠의 통합 비율이 30%를 넘으면 위험 음성을 유지한다."""
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_crosswalk_contact_half_height": .02,
            "obstacle_surrounding_voice_suppression": True,
            "obstacle_surrounding_walkable_threshold": .30,
            "obstacle_surrounding_partial_walkable_threshold": .05,
        }
        item = danger_item(1, [40, 20, 60, 80], "car")
        result = prediction(item)
        apply_obstacle_voice_suppression(
            result, {"signal_state": "green", "selected_detection_index": 0},
            voice_u_strip_map(53), {"walkable": 1, "crosswalk": 2},
            (100, 100, 3), config,
        )
        self.assertNotIn("voice_suppressed_reason", item)
        self.assertEqual(walking_action(result, 100), "blocked")

    # 일부만 보이는 bbox 바깥 띠의 5% 경계 확인
    def test_surrounding_voice_partial_strip_threshold(self):
        """보이는 좌우 띠의 통합 비율이 5% 이하일 때만 음성을 제외한다."""
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_crosswalk_contact_half_height": .02,
            "obstacle_surrounding_voice_suppression": True,
            "obstacle_surrounding_walkable_threshold": .30,
            "obstacle_surrounding_partial_walkable_threshold": .05,
        }
        for pixels, suppressed in ((6, True), (7, False)):
            with self.subTest(walkable_pixels=pixels):
                item = danger_item(1, [40, 20, 60, 100], "car")
                result = prediction(item)
                apply_obstacle_voice_suppression(
                    result, {"signal_state": "green", "selected_detection_index": 0},
                    partial_voice_u_strip_map(pixels),
                    {"walkable": 1, "crosswalk": 2}, (100, 100, 3), config,
                )
                surroundings = item["voice_surrounding_walkability"]
                self.assertEqual(surroundings["status"], "partial")
                self.assertEqual(surroundings["visible_region_count"], 2)
                self.assertAlmostEqual(surroundings["walkable_fraction"], pixels / 128)
                if suppressed:
                    self.assertEqual(
                        item["voice_suppressed_reason"], "low_walkable_surroundings")
                    self.assertIsNone(walking_action(result, 100))
                else:
                    self.assertNotIn("voice_suppressed_reason", item)
                    self.assertEqual(walking_action(result, 100), "blocked")

    # 보행가능 비율 계산 불가 시 음성 제외 확인
    def test_surrounding_voice_unavailable_context_is_silent(self):
        """마스크가 없거나 보이는 띠가 하나도 없으면 위험 음성을 제외한다."""
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_crosswalk_contact_half_height": .02,
            "obstacle_surrounding_voice_suppression": True,
            "obstacle_surrounding_walkable_threshold": .30,
            "obstacle_surrounding_partial_walkable_threshold": .05,
        }
        cases = (
            ("mask_unavailable", [40, 20, 60, 80], None),
            ("no_visible_region", [0, 20, 100, 100], np.zeros((100, 100), np.uint8)),
        )
        for name, box, class_map in cases:
            with self.subTest(name=name):
                item = danger_item(1, box, "car")
                result = prediction(item)
                apply_obstacle_voice_suppression(
                    result, {"signal_state": "green", "selected_detection_index": 0},
                    class_map, {"walkable": 1, "crosswalk": 2},
                    (100, 100, 3), config,
                )
                self.assertEqual(
                    item["voice_suppressed_reason"], "walkable_surroundings_unavailable")
                self.assertIsNone(walking_action(result, 100))

    # 크기가 다른 bbox 바깥 띠의 픽셀 가중 통합 확인
    def test_surrounding_voice_fraction_uses_all_strip_pixels(self):
        """한 띠가 100%여도 전체 176픽셀 중 48픽셀인 27.27%로 음성을 제외한다."""
        item = danger_item(1, [40, 20, 60, 80])
        result = prediction(item)
        class_map = np.zeros((100, 100), np.uint8)
        class_map[68:80, 36:40] = 1
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_crosswalk_contact_half_height": .02,
            "obstacle_surrounding_voice_suppression": True,
            "obstacle_surrounding_walkable_threshold": .30,
        }
        apply_obstacle_voice_suppression(
            result, {"signal_state": "green", "selected_detection_index": 0},
            class_map, {"walkable": 1, "crosswalk": 2}, (100, 100, 3), config,
        )
        self.assertEqual(item["voice_suppressed_reason"], "low_walkable_surroundings")
        self.assertAlmostEqual(
            item["voice_surrounding_walkability"]["walkable_fraction"], 48 / 176)
        self.assertIsNone(walking_action(result, 100))

    # 횡단보도 라벨의 보행가능 비율 제외 확인
    def test_surrounding_voice_fraction_excludes_crosswalk_pixels(self):
        """U자 띠가 전부 crosswalk여도 walkable 비율은 0%로 계산한다."""
        item = danger_item(1, [40, 20, 60, 80])
        result = prediction(item)
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
            "non_green_obstacle_crosswalk_contact_half_height": .02,
            "obstacle_surrounding_voice_suppression": True,
            "obstacle_surrounding_walkable_threshold": .30,
        }
        apply_obstacle_voice_suppression(
            result, {"signal_state": "green", "selected_detection_index": 0},
            voice_u_strip_map(176, label=2), {"walkable": 1, "crosswalk": 2},
            (100, 100, 3), config,
        )
        self.assertEqual(
            item["voice_surrounding_walkability"]["walkable_fraction"], 0.0)
        self.assertEqual(item["voice_suppressed_reason"], "low_walkable_surroundings")

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
            item = danger_item(1, [29, 10, 35, 40])
            engine = SimpleNamespace(update=Mock(side_effect=lambda *args: prediction(item)),
                                     add_sidewalk_context=Mock())
            detector = SimpleNamespace(predict=Mock(return_value=[]))
            with patch("src.pipeline.RiskEngine", return_value=engine), patch(
                "src.pipeline.draw_risk", side_effect=lambda frame, *_: frame
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(process_video(source, output, detector=detector,
                                               risk_config={"enabled": True},
                                               crosswalk_config={
                                                   "obstacle_surrounding_voice_suppression": False,
                                               }), 20)
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
            self.assertEqual(json.loads(lines[0])["voice_text"], "전방 장애물 주의하세요")
            self.assertEqual(json.loads(lines[0])["voice_clip"], "walking-obstacle.mp3")
            self.assertEqual(json.loads(lines[0])["last_action"], "blocked")
            self.assertEqual(json.loads(lines[0])["voice_playback_action"], "blocked")
            self.assertNotIn("voice_clip", json.loads(lines[1]))
            self.assertIsNone(json.loads(lines[-1])["voice_playback_action"])
            self.assertEqual(list(Path(folder).glob("*.partial.*")), [])

    # 측면 위험만 있는 영상의 무안내 확인
    def test_side_only_video_keeps_danger_without_voice(self):
        """저장 영상에서도 측면 위험의 박스·위험 기록만 남기고 음성을 합성하지 않는다."""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            output = Path(folder) / "result.mp4"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
            self.assertTrue(writer.isOpened())
            for _ in range(40):
                writer.write(np.full((48, 64, 3), 100, dtype=np.uint8))
            writer.release()
            item = danger_item(1, [5, 10, 15, 40])
            engine = SimpleNamespace(update=Mock(side_effect=lambda *args: prediction(item)),
                                     add_sidewalk_context=Mock())
            detector = SimpleNamespace(predict=Mock(return_value=[]))
            with patch("src.pipeline.RiskEngine", return_value=engine), patch(
                "src.pipeline.draw_risk", side_effect=lambda frame, *_: frame
            ), patch("src.pipeline.render_voice_track") as render, redirect_stdout(io.StringIO()):
                self.assertEqual(process_video(source, output, detector=detector,
                                               risk_config={"enabled": True}), 40)
            render.assert_not_called()
            self.assertTrue(output.is_file())
            rows = [json.loads(line) for line in risk_log_path(output).read_text().splitlines()]
            self.assertEqual(len(rows), 40)
            for row in rows:
                self.assertIsNone(row["last_action"])
                self.assertIsNone(row["voice_action"])
                self.assertIsNone(row["voice_playback_action"])
                self.assertNotIn("voice_event", row)
                self.assertNotIn("voice_clip", row)
                self.assertEqual(row["detections"][0]["alert_level"], "danger")

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
            item = danger_item(1, [29, 10, 35, 40])
            engine = SimpleNamespace(update=Mock(side_effect=lambda *args: prediction(item)),
                                     add_sidewalk_context=Mock())
            detector = SimpleNamespace(predict=Mock(return_value=[]))
            with patch("src.pipeline.RiskEngine", return_value=engine), patch(
                "src.pipeline.draw_risk", side_effect=lambda frame, *_: frame
            ), patch("src.pipeline.mux_voice", side_effect=RuntimeError("합성 오류")), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "합성 오류"):
                    process_video(
                        source, output, detector=detector, risk_config={"enabled": True},
                        crosswalk_config={"obstacle_surrounding_voice_suppression": False},
                    )
            self.assertFalse(output.exists())
            self.assertFalse(risk_log_path(output).exists())
            self.assertEqual(list(Path(folder).glob("*.partial.*")), [])
