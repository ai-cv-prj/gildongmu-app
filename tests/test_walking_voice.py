"""
file_path: tests/test_walking_voice.py

보행 위험 음성의 방향·다중 객체 선택과 결과 MP4 합성을 검증한다.
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
    danger_voice_message,
    danger_voice_targets,
    suppress_non_green_crosswalk_voice,
)


# 테스트용 위험 객체 만들기
def danger_item(event_id, box, name="person", **extra):
    """신뢰할 수 있는 객체 이름과 위험 이벤트 ID를 가진 검출을 반환한다."""
    return {"event_id": event_id, "detection_index": event_id - 1,
            "xyxy": box, "alert_level": "danger", "risk_level": "danger",
            "label_status": "reliable", "display_label": name,
            "warning_primary": True, **extra}


# 테스트용 프레임 위험 결과 만들기
def prediction(*items, level="danger", source="object", epoch=0):
    """첫 번째 객체가 화면의 대표 경고인 프레임 결과를 반환한다."""
    index = items[0]["detection_index"] if items else None
    return {"warning": {"level": level, "source": source, "detection_index": index},
            "detections": list(items), "state_epoch": epoch}


class WalkingVoiceTests(unittest.TestCase):
    """test-app의 위험 음성 선택 및 영상 저장 규칙을 확인한다."""

    # 한 객체의 방향과 물체 종류 확인
    def test_single_danger_direction_and_category(self):
        """왼쪽·가운데·오른쪽과 사람·차량·일반 장애물을 구분한다."""
        cases = [
            ([5, 0, 15, 20], "person", "danger-left-person.mp3", "왼쪽에 사람."),
            ([45, 0, 55, 20], "car", "danger-center-vehicle.mp3", "가운데에 차량."),
            ([85, 0, 95, 20], "bollard", "danger-right-obstacle.mp3", "오른쪽에 장애물."),
        ]
        for box, name, filename, spoken in cases:
            with self.subTest(name=name):
                targets = danger_voice_targets(prediction(danger_item(1, box, name)), 100)
                self.assertEqual(danger_voice_message(targets), (spoken, filename))

    # 여러 위험 객체의 방향 묶기
    def test_multiple_dangers_in_same_and_different_directions(self):
        """같은 방향과 여러 방향의 복수 위험을 다른 음원으로 안내한다."""
        left = danger_item(1, [4, 0, 12, 20])
        second_left = danger_item(2, [20, 0, 30, 20], "car")
        right = danger_item(3, [80, 0, 90, 20])
        self.assertEqual(danger_voice_message(danger_voice_targets(
            prediction(left, second_left), 100)),
            ("왼쪽에 여러 장애물.", "danger-left-multiple.mp3"))
        self.assertEqual(danger_voice_message(danger_voice_targets(
            prediction(left, right), 100)),
            ("여러 방향에 장애물.", "danger-multiple-directions.mp3"))

    # 위험 수준과 음성 반복 억제
    def test_only_new_primary_danger_is_announced(self):
        """주의·촬영 상태는 침묵하고 새 객체 및 1.5초 뒤의 위험은 안내한다."""
        voice = WalkingVoice()
        person = danger_item(1, [5, 0, 15, 20])
        car = danger_item(2, [45, 0, 55, 20], "car")
        self.assertIsNone(voice.observe(prediction(person, level="caution"), 100, 0))
        camera = prediction(person, source="camera_view")
        camera["warning"]["detection_index"] = None
        self.assertIsNone(voice.observe(camera, 100, .1))
        self.assertEqual(voice.observe(prediction(person), 100, .2),
                         ("왼쪽에 사람.", "danger-left-person.mp3"))
        self.assertIsNone(voice.observe(prediction(person), 100, .3))
        self.assertEqual(voice.observe(prediction(person, car), 100, .4),
                         ("여러 방향에 장애물.", "danger-multiple-directions.mp3"))
        self.assertIsNone(voice.observe(prediction(person, car), 100, .5))
        self.assertEqual(voice.observe(prediction(person), 100, 2.1),
                         ("왼쪽에 사람.", "danger-left-person.mp3"))
        self.assertEqual(len(voice.events), 3)

    # 대상 없는 위험 및 의미 영역 처리
    def test_non_object_warning_and_semantic_object(self):
        """객체 없는 경고는 무음이고 보행불가 객체는 일반 장애물로 안내한다."""
        item = danger_item(1, [45, 0, 55, 20], "person")
        no_object = prediction(item)
        no_object["warning"]["detection_index"] = None
        self.assertEqual(danger_voice_targets(no_object, 100), [])
        targets = danger_voice_targets(prediction(item, source="surface_object"), 100)
        self.assertEqual(danger_voice_message(targets),
                         ("가운데에 장애물.", "danger-center-obstacle.mp3"))

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
        self.assertEqual(danger_voice_message(danger_voice_targets(result, 100)),
                         ("오른쪽에 차량.", "danger-right-vehicle.mp3"))

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
                self.assertEqual(danger_voice_message(danger_voice_targets(result, 100)),
                                 ("가운데에 사람.", "danger-center-person.mp3"))

    # 저장 영상 식별자 문자열 확인
    def test_overlay_identity_contains_track_and_event_ids(self):
        """저장 영상 장애물 라벨에 추적 ID와 위험 이벤트 ID를 함께 표시한다."""
        self.assertEqual(risk_identity({"track_id": 12, "event_id": 34}), "T12/E34")

    # 새 이벤트가 이전 음성을 끊는 PCM 결과 확인
    def test_voice_track_starts_at_frame_time_and_is_video_length(self):
        """새 안내 시점에 이전 음성을 끊고 영상 끝에서 정확히 종료한다."""
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "voice.wav"
            events = [(0.5, "danger-left-person.mp3"),
                      (0.6, "danger-right-obstacle.mp3")]
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
            self.assertEqual(json.loads(lines[0])["voice_text"], "왼쪽에 사람.")
            self.assertEqual(json.loads(lines[0])["voice_clip"], "danger-left-person.mp3")
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
