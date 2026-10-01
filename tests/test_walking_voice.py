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
from src.video_audio import SAMPLE_RATE, ffmpeg_executable, render_voice_track
from src.risk_visualization import risk_identity
from src.walking_voice import (
    WalkingVoice,
    suppress_non_green_crosswalk_voice,
    walking_action,
    warning_directions,
)


# 테스트용 위험 객체 만들기
def danger_item(event_id, box, name="person", **extra):
    """신뢰할 수 있는 객체 이름과 위험 이벤트 ID를 가진 검출을 반환한다."""
    return {"event_id": event_id, "detection_index": event_id - 1,
            "xyxy": box, "alert_level": "danger", "risk_level": "danger",
            "label_status": "reliable", "display_label": name,
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

    # 핑크 ROI 내부 위험만 음성 행동에 포함
    def test_only_danger_inside_pink_roi_triggers_guidance(self):
        """위험이어도 핑크 ROI 겹침이 20% 미만이면 음성 행동을 만들지 않는다."""
        outside = danger_item(1, [5, 0, 15, 20], geometry={"immediate_overlap": .19})
        inside = danger_item(2, [5, 0, 15, 20], geometry={"immediate_overlap": .20})
        self.assertIsNone(walking_action(prediction(outside), 100))
        self.assertEqual(walking_action(prediction(inside), 100), "straight")

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
            (prediction(left, center, right), "stop"),
            (prediction(center), "stop"),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(walking_action(result, 100), expected)

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
                         ("직진하세요.", "walking-straight.mp3"))
        same = prediction(danger_item(3, [10, 0, 20, 20]))
        self.assertIsNone(voice.observe(same, 100, .3))
        self.assertEqual(same["last_action"], "straight")
        self.assertEqual(voice.observe(prediction(left, center), 100, .4),
                         ("오른쪽으로 이동하세요.", "walking-move-right.mp3"))
        self.assertIsNone(voice.observe(prediction(left, center), 100, .5))
        cleared = prediction(left, level="caution")
        self.assertIsNone(voice.observe(cleared, 100, .6))
        self.assertIsNone(cleared["last_action"])
        self.assertEqual(voice.observe(prediction(left), 100, .7),
                         ("직진하세요.", "walking-straight.mp3"))
        self.assertEqual(len(voice.events), 3)

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

    # 초록불·신호 미선택·마스크 미확인 시 음성 유지 확인
    def test_voice_is_not_suppressed_without_selected_non_green_evidence(self):
        """초록불이거나 신호·횡단보도 근거가 없으면 위험 음성을 유지한다."""
        config = {
            "non_green_obstacle_voice_suppression": True,
            "non_green_obstacle_crosswalk_threshold": .20,
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
        self.assertEqual(risk_identity({"track_id": 12, "event_id": 34}), "T12/E34")

    # 새 이벤트가 이전 음성을 끊는 PCM 결과 확인
    def test_voice_track_starts_at_frame_time_and_is_video_length(self):
        """새 안내 시점에 이전 음성을 끊고 영상 끝에서 정확히 종료한다."""
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "voice.wav"
            events = [(0.5, "walking-move-left.mp3"),
                      (0.6, "walking-move-right.mp3")]
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
            lines = output.with_suffix(".risk.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 20)
            self.assertEqual(json.loads(lines[0])["voice_text"], "직진하세요.")
            self.assertEqual(json.loads(lines[0])["voice_clip"], "walking-straight.mp3")
            self.assertEqual(json.loads(lines[0])["last_action"], "straight")
            self.assertNotIn("voice_clip", json.loads(lines[1]))
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
            self.assertFalse(output.with_suffix(".risk.jsonl").exists())
            self.assertEqual(list(Path(folder).glob("*.partial.*")), [])
