"""
file_path: tests/test_traffic_voice.py

테스트앱의 신호 안내 규칙과 결과 영상 음성 합성을 검증한다.
"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from src.pipeline import process_video
from src.traffic_voice import TrafficVoice
from src.video_audio import ffmpeg_executable
import subprocess


# 선택된 신호등 결과 구성
def signal(color="red", target=1, allowed=True):
    """선택된 추적 ID와 색상을 가진 결과를 반환한다."""
    if target is None:
        return {"selected_detection_index": None, "detections": [], "signal_state": "unknown",
                "voice_gate": {"allowed": allowed}}
    return {"selected_detection_index": 0, "detections": [{"track_id": target}],
            "signal_state": color, "voice_gate": {"allowed": allowed}}


class TrafficVoiceTests(unittest.TestCase):
    """3프레임·400ms 확인, 전환, 소실 및 영상 저장을 검증한다."""

    # 연속 프레임 관측
    def observe(self, voice, color, times, target=1, first_frame=1):
        """지정한 시간에 같은 신호를 연속 관측한다."""
        for index, time_s in enumerate(times):
            voice.observe(signal(color, target), first_frame + index, time_s)

    # 최초 색상과 전환 구분
    def test_initial_green_and_color_change(self):
        """처음 초록불은 대기 문구, 같은 대상의 변화는 전환 문구를 사용한다."""
        voice = TrafficVoice()
        start = .1
        self.observe(voice, "green", [start, start + .2, start + .4])
        self.observe(voice, "red", [start + .6, start + .8, start + 1], first_frame=4)
        self.assertEqual([name for _, name in voice.events],
                         ["green-initial-wait.mp3", "red-changed.mp3"])
        self.assertAlmostEqual(voice.events[0][0], start + .4)
        self.assertAlmostEqual(voice.events[1][0], start + 1)

    # 짧은 후보와 대상 변경 확인
    def test_stability_and_target_change(self):
        """짧은 후보는 침묵하고 다른 대상의 같은 색은 한 번 다시 읽는다."""
        voice = TrafficVoice()
        start = .1
        self.observe(voice, "red", [start, start + .2])
        self.assertEqual(voice.events, [])
        voice.observe(signal("red", 1), 3, start + .4)
        self.observe(voice, "red", [start + .6, start + .8, start + 1], target=2, first_frame=4)
        self.assertEqual([name for _, name in voice.events],
                         ["red.mp3", "red.mp3"])

    # 소실 이후 복구
    def test_missing_once_then_same_color_reannounced(self):
        """확인된 신호가 2초 사라지면 한 번 알리고 복구된 색을 다시 읽는다."""
        voice = TrafficVoice()
        start = .1
        self.observe(voice, "red", [start, start + .2, start + .4])
        for index, time_s in enumerate([start + 1, start + 2.4, start + 2.6, start + 2.8, start + 3]):
            color = None if index < 2 else "red"
            voice.observe(signal(color, None if color is None else 1), 4 + index, time_s)
        self.assertEqual([name for _, name in voice.events],
                         ["red.mp3", "missing.mp3", "red.mp3"])

    # 실제 파일의 영상 음성 저장
    def test_traffic_only_video_without_mask_has_no_audio(self):
        """횡단보도 마스크와 파란 ROI가 없는 단독 신호 추론은 음성을 만들지 않는다."""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            output = Path(folder) / "result.mp4"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
            for _ in range(30):
                writer.write(np.full((48, 64, 3), 100, dtype=np.uint8))
            writer.release()
            traffic = SimpleNamespace(reset=Mock(), predict=Mock(return_value=signal()), device="cpu")
            with patch("src.pipeline.draw_traffic", side_effect=lambda frame, *_: frame), redirect_stdout(io.StringIO()):
                self.assertEqual(process_video(source, output, traffic=traffic), 30)
            probe = subprocess.run([ffmpeg_executable(), "-v", "error", "-i", str(output),
                                    "-map", "0:a:0", "-f", "s16le", "-ac", "1", "pipe:1"],
                                   capture_output=True)
            self.assertNotEqual(probe.returncode, 0)
            self.assertIn("matches no streams", probe.stderr.decode(errors="replace"))

    # 장애물과 신호 음성이 같은 시점에 존재하는 영상
    def test_all_mode_mixes_walking_and_traffic_audio(self):
        """위험 음성과 신호 안내가 겹쳐도 결과 MP4의 오디오 트랙을 생성한다."""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            output = Path(folder) / "result.mp4"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
            for _ in range(20):
                writer.write(np.full((48, 64, 3), 100, dtype=np.uint8))
            writer.release()
            item = {"event_id": 1, "detection_index": 0, "xyxy": [5, 0, 28, 40],
                    "alert_level": "danger", "label_status": "reliable",
                    "display_label": "person", "class_name": "person", "warning_primary": True,
                    "geometry": {"immediate_overlap": 1.0}}
            prediction = {"warning": {"level": "danger", "source": "object", "detection_index": 0},
                          "detections": [item], "state_epoch": 0,
                          "roi": {"corridor_polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}}
            engine = SimpleNamespace(update=Mock(side_effect=lambda *args: dict(prediction)),
                                     add_sidewalk_context=Mock())
            detector = SimpleNamespace(predict=Mock(return_value=[]))
            traffic = SimpleNamespace(reset=Mock(), predict=Mock(return_value=signal()), device="cpu")
            segmenter = SimpleNamespace(
                predict=Mock(return_value=np.full((48, 64), 2, dtype=np.uint8)),
                label_ids={"walkable": 1, "crosswalk": 2, "non_walkable": 0})
            with patch("src.pipeline.RiskEngine", return_value=engine), patch(
                "src.pipeline.draw_risk", side_effect=lambda frame, *_: frame
            ), patch("src.pipeline.draw_traffic", side_effect=lambda frame, *_: frame), redirect_stdout(io.StringIO()):
                self.assertEqual(process_video(source, output, detector=detector, traffic=traffic,
                                               segmenter=segmenter,
                                               crosswalk_config={"enabled": False,
                                                   "non_green_obstacle_voice_suppression": False},
                                               walking_surface_config_value={"enabled": False},
                                               risk_config={"enabled": True, "log_jsonl": False}), 20)
            probe = subprocess.run([ffmpeg_executable(), "-v", "error", "-i", str(output),
                                    "-map", "0:a:0", "-f", "s16le", "-ac", "1", "pipe:1"],
                                   capture_output=True)
            self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors="replace"))
            self.assertTrue(np.any(np.frombuffer(probe.stdout, dtype="<i2")))
