"""
file_path: backend/inference.py

기존 영상 추론 모델과 위험 판정을 휴대폰 카메라 프레임에 재사용한다.
모델은 한 번 로딩하고 세션마다 추적 상태를 초기화한다.
"""

from time import perf_counter

from backend.boarding import Boarding
from backend.stop_proximity import StopProximity
from src.crosswalk_safety import (
    CrosswalkSafetyEngine, crosswalk_camera_stable, crosswalk_safety_config,
)
from src.obstacle import ObstacleDetector, validate_yolo_config
from src.pipeline import load_config, resolve_path
from src.risk import RiskEngine
from src.risk_config import risk_config, tracking_config
from src.sidewalk import SidewalkSegmenter
from src.traffic import TrafficSignalPipeline, validate_traffic_config
from src.walking_voice import WalkingVoice, suppress_non_green_crosswalk_voice
from src.walking_surface import WalkingSurfaceEngine, walking_surface_config


class RealtimeInference:
    """한 프레임에 도보·장애물·신호등 추론을 적용한다."""

    # 영상 추론 설정과 같은 고정 가중치 로딩
    def __init__(self, config_path="configs/inference.yaml"):
        """기존 설정 파일의 고정 모델과 위험 판정 옵션을 준비한다."""
        config = load_config(config_path)
        device = config["device"]
        yolo = config["yolo"]
        traffic = config["traffic"]
        validate_yolo_config(yolo)
        validate_traffic_config(traffic)
        self.detector = ObstacleDetector(
            resolve_path(yolo["weights"]), device=device, conf=yolo["conf"],
            imgsz=yolo["imgsz"], head=yolo["head"], iou=yolo.get("iou", 0.7),
            max_det=yolo.get("max_det", 300), rect=yolo.get("rect", True),
        )
        self.segmenter = SidewalkSegmenter(resolve_path(config["mask2former"]["weights"]),
                                            device=self.detector.device)
        traffic_options = dict(traffic)
        traffic_options["weights"] = resolve_path(traffic_options["weights"])
        traffic_options["classifier_weights"] = resolve_path(traffic_options["classifier_weights"])
        self.traffic = TrafficSignalPipeline(device=str(self.segmenter.device), **traffic_options)
        self.risk_settings = risk_config(config["risk"])
        self.tracking_settings = tracking_config(config["tracking"])
        self.stop_proximity_settings = config.get("stop_proximity", {})
        self.crosswalk_settings = crosswalk_safety_config(config.get("crosswalk_safety"))
        self.walking_surface_settings = walking_surface_config(config.get("walking_surface"))
        self.reset()

    # 새 휴대폰 세션의 이전 추적 상태 제거
    def reset(self):
        """위험·음성·신호등 추적 기록을 새 세션 기준으로 비운다."""
        self.risk = RiskEngine(self.risk_settings, self.tracking_settings)
        self.voice = WalkingVoice()
        self._obstacles_suspended = False
        self.stop_proximity = StopProximity(self.stop_proximity_settings)
        self.boarding = Boarding()
        self.crosswalk = CrosswalkSafetyEngine(self.crosswalk_settings)
        self.walking_surface = WalkingSurfaceEngine(self.walking_surface_settings)
        self.traffic.reset()

    # 같은 원본 프레임의 세 모델 추론
    def predict(self, frame, frame_id, captured_at_ms):
        """원본 BGR 프레임에서 각 모델 결과와 처리 시간을 반환한다."""
        started = perf_counter()
        at_stop = self.boarding.at_stop
        if not at_stop and self._obstacles_suspended:
            self.risk.reset()
            self.voice = WalkingVoice()
        self._obstacles_suspended = at_stop
        class_map = self.segmenter.predict(frame)
        height, width = frame.shape[:2]
        if at_stop:
            risk = self.stopped_risk()
            risk["stop_proximity"] = {"status": "suspended", "nearby": False}
        else:
            detections = self.detector.predict(frame)
            risk = self.risk.update(frame, detections, captured_at_ms / 1000,
                                    True, class_map, self.segmenter.label_ids)
            risk["enabled"] = True
            camera_view = risk.get("camera_view") or {}
            risk["stop_proximity"] = self.stop_proximity.update(
                risk["detections"], frame.shape, captured_at_ms / 1000,
                camera_view=camera_view.get("status", "clear"),
                state_reset=risk.get("state_reset", False),
            )
            self.risk.add_sidewalk_context(risk, class_map, self.segmenter.label_ids, frame.shape)
        signal = self.traffic.predict(frame, frame_id=frame_id,
                                      captured_at_ms=captured_at_ms)
        if not at_stop:
            suppress_non_green_crosswalk_voice(
                risk, signal, class_map, self.segmenter.label_ids, frame.shape,
                self.crosswalk_settings,
            )
        camera_stable = crosswalk_camera_stable(risk)
        crosswalk = self.crosswalk.update(
            class_map, self.segmenter.label_ids, frame.shape, signal,
            captured_at_ms / 1000, camera_stable=camera_stable,
            detections=risk["detections"],
        )
        risk["boarding"] = self.boarding.observe(
            risk["stop_proximity"], crossing_active=crosswalk["crossing_active"])
        walking_surface = self.walking_surface.update(
            class_map, self.segmenter.label_ids, frame.shape, captured_at_ms / 1000,
            camera_stable=camera_stable, crosswalk_status=crosswalk["status"],
            suspended=self.boarding.at_stop,
        )
        if self.boarding.at_stop:
            # Clear even the frame that first confirms arrival, before publishing it.
            risk = {**self.stopped_risk(), "stop_proximity": risk["stop_proximity"],
                    "boarding": risk["boarding"]}
            self._obstacles_suspended = True
        else:
            self.voice.observe(
                risk, width, captured_at_ms / 1000,
                crossing_active=crosswalk["crossing_active"],
                crosswalk_status=crosswalk["status"],
            )
        # 영상 출력과 마찬가지로 일반 장애물 모델의 신호등 박스는 중복 표시하지 않는다.
        risk["detections"] = [item for item in risk["detections"]
                              if item.get("class_name") != "traffic_light"]
        return (risk, signal, crosswalk, walking_surface, class_map, self.segmenter.label_ids,
                round((perf_counter() - started) * 1000))

    @staticmethod
    def stopped_risk():
        """No obstacle boxes, remembered hazards or walking voice at a bus stop."""
        return {"enabled": False, "detections": [], "level": "safe", "warning_text": "",
                "roi": {}, "camera_view": {"status": "clear"}, "last_action": None,
                "voice_action": None, "voice_text": None, "voice_event": None,
                "voice_clear": True}
