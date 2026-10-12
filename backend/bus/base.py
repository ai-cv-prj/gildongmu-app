"""버스 추론 파이프라인의 모델 정보, 프레임 입력과 응답 인터페이스."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

import numpy as np


@dataclass
class InferenceContext:
    session_id: str
    frame_id: int
    captured_at_ms: int
    confidence: float = 0.4


@dataclass
class ModelSpec:
    """모델 식별 정보와 로컬 가중치 준비 상태."""

    id: str
    mode: str
    name: str
    version: str
    is_mock: bool = False
    weights: Optional[Path] = None  # 실제 모델이면 가중치 경로. 없으면 available=False
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.is_mock or (self.weights is not None and self.weights.exists())


@runtime_checkable
class InferencePipeline(Protocol):
    spec: ModelSpec

    def load(self) -> None:
        """추론에 필요한 모델 가중치를 로딩한다."""

    def infer(self, frame_bgr: np.ndarray, context: InferenceContext) -> dict[str, Any]:
        """한 프레임 추론.

        반환 형식:
        {
          "detections": [
            {"class_id": int, "class_name": str, "confidence": float,
             "box": {"x1": 0~1, "y1": 0~1, "x2": 0~1, "y2": 0~1},
             "track_id": int | None, "extra": {...}}
          ],
          "event": {"type": str, ...기능별 필드}
        }
        좌표는 픽셀이 아니라 0~1 정규화 값이다.
        """

    def reset_session(self, session_id: str) -> None:
        """세션 시작 시 추적/누적 상태 초기화."""

    def close_session(self, session_id: str) -> None:
        """세션 종료 시 상태 정리."""


def clamp01(v: float) -> float:
    return float(min(1.0, max(0.0, v)))


def normalize_box(x1: float, y1: float, x2: float, y2: float, width: int, height: int) -> dict[str, float]:
    """픽셀 xyxy 좌표를 0~1 범위의 화면 비율로 정규화한다."""
    return {
        "x1": clamp01(x1 / width),
        "y1": clamp01(y1 / height),
        "x2": clamp01(x2 / width),
        "y2": clamp01(y2 / height),
    }
