"""
file_path: tests/test_walking_risk_sync.py

test-app의 보행 위험 판정과 영상 추론 설정을 이식한 결과를 검증한다.
넓은 ROI의 측면·중앙 판정과 촬영 불가 상태를 확인한다.
"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np
import yaml

from src.risk import RiskEngine
from src.risk_config import risk_config
from src.hazard_labels import LabelMemory
from src.surface_risk import SurfaceRisk
from src.warning_summary import WarningSelector


PROJECT_DIR = Path(__file__).resolve().parents[1]
SETTINGS = yaml.safe_load((PROJECT_DIR / "configs/inference.yaml").read_text())
LABELS = {"non_walkable": 0, "walkable": 1, "crosswalk": 2}


class FixedTracker:
    """시험용 검출에 일정한 ID를 부여하는 추적기이다."""

    status = "active"

    # 시험용 추적기 초기화
    def reset(self):
        """이 시험에서는 유지할 추적 상태가 없다."""

    # 검출에 고정 ID 부여
    def attach(self, detections, frame):
        """입력 순서에 따라 안정적인 추적 ID를 붙인다."""
        return [{**deepcopy(item), "track_id": index + 1, "detection_index": index}
                for index, item in enumerate(detections)]


# 테스트용 검출 생성
def detection(box, name="person"):
    """지정한 박스와 이름을 가진 장애물 검출을 만든다."""
    return {"xyxy": list(box), "class_name": name, "class_id": 0, "confidence": .9}


# 테스트용 위험 판정기 생성
def engine():
    """모델 대신 고정 추적기와 안정적인 촬영 상태를 사용한다."""
    stable = SimpleNamespace(update=lambda frame, timestamp: True, reset=lambda: None)
    return RiskEngine(SETTINGS["risk"], SETTINGS["tracking"],
                      tracker=FixedTracker(), camera_guard=stable)


class WalkingRiskSyncTests(unittest.TestCase):
    """영상 추론의 ROI 설정과 보행 위험 판정 회귀를 확인한다."""

    # 기본 통합 모드와 ROI 모양 검증
    def test_default_all_mode_and_roi_geometry(self):
        """기본 모드는 all이며 하단 즉시 위험 영역은 직사각형이다."""
        self.assertEqual(SETTINGS["mode"], "all")
        cfg = risk_config(SETTINGS["risk"])
        self.assertEqual(cfg["immediate_polygon"],
                         [[.02, .70], [.98, .70], [.98, 1.], [.02, 1.]])
        self.assertEqual(cfg["corridor_polygon"],
                         [[.38, .30], [.62, .30], [.98, .70],
                          [.98, 1.], [.02, 1.], [.02, .70]])

    # 넓은 하단 ROI의 측면·중앙 위험도 검증
    def test_wide_side_is_caution_and_center_is_danger(self):
        """같은 깊이에서 측면 접촉은 주의, 중앙 접촉은 위험으로 판정한다."""
        frame = np.zeros((100, 100, 3), np.uint8)
        walkable = np.ones((100, 100), np.uint8)
        side = engine().update(frame, [detection((78, 50, 95, 85))], 0,
                               class_map=walkable, label_ids=LABELS)["detections"][0]
        center = engine().update(frame, [detection((42, 50, 58, 85))], 0,
                                 class_map=walkable, label_ids=LABELS)["detections"][0]
        self.assertEqual(side["risk_level"], "caution")
        self.assertEqual(center["risk_level"], "danger")
        self.assertIn("near_path_side_candidate", side["reasons"])

    # 촬영 불가 상태의 경고 검증
    def test_obscured_view_suppresses_object_warning(self):
        """화면이 가려지면 객체 경고를 확정하지 않고 촬영 상태를 안내한다."""
        frame = np.zeros((320, 180, 3), np.uint8)
        walkable = np.ones((320, 180), np.uint8)
        assessor = engine()
        for timestamp in (0, .2, .4):
            result = assessor.update(frame, [detection((72, 190, 108, 300))],
                                     timestamp, class_map=walkable, label_ids=LABELS)
        self.assertEqual(result["camera_view"]["status"], "unavailable")
        self.assertEqual(result["warning"]["source"], "camera_view")
        self.assertIsNone(result["detections"][0]["risk_level"])

    # 보행가능영역이 가려졌을 때 ROI 유지 검증
    def test_ground_occlusion_holds_roi_top(self):
        """보도가 잠시 사라져도 넓은 ROI의 상단이 즉시 줄어들지 않는다."""
        assessor = engine()
        frame = np.zeros((100, 100, 3), np.uint8)
        visible = np.zeros((100, 100), np.uint8)
        visible[30:] = 1
        first = assessor.update(frame, [], 0, class_map=visible,
                                label_ids=LABELS)["roi"]
        occluded = np.zeros((100, 100), np.uint8)
        held = assessor.update(frame, [], .1, class_map=occluded,
                               label_ids=LABELS)["roi"]
        self.assertAlmostEqual(held["path_top_y"], first["path_top_y"])
        self.assertEqual(held["ground_extent"]["reason"], "held_unavailable_ground")

    # 시간 공백의 운동 판정 차단 검증
    def test_short_gap_keeps_surface_warning_and_clears_motion(self):
        """짧은 프레임 공백은 운동 근거만 비우고 보행불가 경고는 유지한다."""
        assessor = engine()
        frame = np.zeros((100, 100, 3), np.uint8)
        mask = np.ones((100, 100), np.uint8)
        mask[45:70, 35:55] = 0
        for timestamp in (0, .1, .2):
            assessor.update(frame, [], timestamp, class_map=mask, label_ids=LABELS)
        result = assessor.update(frame, [], .8, timestamp_valid=False,
                                 class_map=mask, label_ids=LABELS)
        self.assertFalse(result["state_reset"])
        self.assertTrue(result["motion_gap"])
        self.assertEqual(result["surface"]["alert_level"], "caution")

    # 객체 이름의 충돌 처리 검증
    def test_conflicting_names_use_generic_label(self):
        """안정화된 이름이 갑자기 바뀌면 일반 장애물로 표시한다."""
        memory = LabelMemory(risk_config(SETTINGS["risk"]))
        names = []
        for index, name in enumerate(("person", "person", "person", "tree_trunk")):
            item = {"class_name": name, "confidence": .9, "track_id": 4,
                    "event_id": 1, "risk_level": "caution"}
            memory.update([item], index * .1)
            names.append(item["display_label"])
        self.assertEqual(names, ["obstacle", "obstacle", "person", "obstacle"])

    # 별도 보행불가 영역 경고 보존 검증
    def test_unmatched_surface_region_is_not_suppressed(self):
        """검출 객체와 겹치지 않는 보행불가 영역의 경고를 유지한다."""
        cfg = risk_config(SETTINGS["risk"])
        roi = {"corridor_polygon": cfg["corridor_polygon"],
               "immediate_polygon": cfg["immediate_polygon"]}
        mask = np.ones((100, 100), np.uint8)
        mask[47:68, 34:48] = 0
        mask[47:68, 58:72] = 0
        matched = {"alert_level": "caution", "label_status": "reliable",
                   "detection_index": 0,
                   "geometry": {"box_norm": [.32, .44, .50, .71]}}
        surface = SurfaceRisk(cfg)
        for timestamp in (0, .1, .2):
            result, _ = surface.update(mask, LABELS, (100, 100, 3), roi,
                                       timestamp, True, True, [matched])
        self.assertEqual(result["matched_region_count"], 1)
        self.assertEqual(result["unmatched_region_count"], 1)
        self.assertFalse(result["suppressed_duplicate"])
        self.assertEqual(WarningSelector(cfg).update([], result, [], .2)["source"], "surface")


if __name__ == "__main__":
    unittest.main()
