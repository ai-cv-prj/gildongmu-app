"""
file_path: backend/inference.py

기존 영상 추론 모델과 위험 판정을 휴대폰 카메라 프레임에 재사용한다.
모델은 한 번 로딩하고 세션마다 추적 상태를 초기화한다.
"""

from time import perf_counter

from src.obstacle import ObstacleDetector, validate_yolo_config
from src.pipeline import load_config, resolve_path
from src.risk import RiskEngine
from src.risk_config import risk_config, tracking_config
from src.sidewalk import SidewalkSegmenter
from src.traffic import TrafficSignalPipeline, validate_traffic_config
from src.walking_voice import WalkingVoice


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
        self.reset()

    # 새 휴대폰 세션의 이전 추적 상태 제거
    def reset(self):
        """위험·음성·신호등 추적 기록을 새 세션 기준으로 비운다."""
        self.risk = RiskEngine(self.risk_settings, self.tracking_settings)
        self.voice = WalkingVoice()
        self.traffic.reset()

    # 같은 원본 프레임의 세 모델 추론
    def predict(self, frame, frame_id, captured_at_ms):
        """원본 BGR 프레임에서 각 모델 결과와 처리 시간을 반환한다."""
        started = perf_counter()
        detections = self.detector.predict(frame)
        class_map = self.segmenter.predict(frame)
        height, width = frame.shape[:2]
        risk = self.risk.update(frame, detections, captured_at_ms / 1000,
                                True, class_map, self.segmenter.label_ids)
        self.risk.add_sidewalk_context(risk, class_map, self.segmenter.label_ids, frame.shape)
        self.voice.observe(risk, width, captured_at_ms / 1000)
        signal = self.traffic.predict(frame, frame_id=frame_id,
                                      captured_at_ms=captured_at_ms)
        # 영상 출력과 마찬가지로 일반 장애물 모델의 신호등 박스는 중복 표시하지 않는다.
        risk["detections"] = [item for item in risk["detections"]
                              if item.get("class_name") != "traffic_light"]
        return risk, signal, class_map, self.segmenter.label_ids, round((perf_counter() - started) * 1000)
