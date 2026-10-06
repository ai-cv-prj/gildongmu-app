"""Configuration and local asset readiness for optional bus features."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUS_CONFIG = ROOT / "configs" / "bus.yaml"


def _path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("bus model paths must be nonempty strings")
    raw = Path(value).expanduser()
    return raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()


@dataclass(frozen=True)
class BusConfig:
    route_display: Path
    bus_detector: Path
    parseq_weights: Path
    parseq_source: Path
    interval_ms: int
    max_result_age_ms: int
    station_radius_m: int
    max_station_distance_m: float
    arrival_timeout_sec: float
    speech_timeout_sec: float

    def model_files(self) -> dict[str, bool]:
        return {
            "route_display": self.route_display.is_file(),
            "bus_detector": self.bus_detector.is_file(),
            "parseq_weights": self.parseq_weights.is_file(),
            "parseq_source": (self.parseq_source / "hubconf.py").is_file(),
        }

    def readiness(self, *, api_key_configured: bool) -> dict[str, object]:
        files = self.model_files()
        return {
            "api_key_configured": api_key_configured,
            "arrival_ready": api_key_configured,
            "model_files": files,
            "recognition_ready": all(files.values()),
        }


def load_bus_config(path: str | Path = DEFAULT_BUS_CONFIG) -> BusConfig:
    with _path(str(path)).open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError("bus config must be a mapping")
    models = raw.get("models")
    recognition = raw.get("recognition")
    arrivals = raw.get("arrivals")
    speech = raw.get("speech")
    if not all(isinstance(section, dict) for section in (models, recognition, arrivals, speech)):
        raise ValueError("bus config needs models, recognition, arrivals and speech sections")

    def positive_number(section: dict, key: str, *, integer: bool = False):
        value = section.get(key)
        valid = type(value) is int if integer else type(value) in (int, float)
        if not valid or not math.isfinite(value) or value <= 0:
            raise ValueError(f"bus config {key} must be positive")
        return value

    return BusConfig(
        route_display=_path(models.get("route_display")),
        bus_detector=_path(models.get("bus_detector")),
        parseq_weights=_path(models.get("parseq_weights")),
        parseq_source=_path(models.get("parseq_source")),
        interval_ms=positive_number(recognition, "interval_ms", integer=True),
        max_result_age_ms=positive_number(recognition, "max_result_age_ms", integer=True),
        station_radius_m=positive_number(arrivals, "station_radius_m", integer=True),
        max_station_distance_m=positive_number(arrivals, "max_station_distance_m"),
        arrival_timeout_sec=positive_number(arrivals, "timeout_sec"),
        speech_timeout_sec=positive_number(speech, "timeout_sec"),
    )


@dataclass(frozen=True)
class BusServiceSettings:
    seoul_bus_api_key: str
    seoul_bus_api_timeout_sec: float
    seoul_bus_station_radius_m: int
    seoul_bus_max_station_distance_m: float

    @classmethod
    def from_config(cls, config: BusConfig) -> "BusServiceSettings":
        return cls(
            seoul_bus_api_key=os.getenv("SEOUL_BUS_API_KEY", ""),
            seoul_bus_api_timeout_sec=config.arrival_timeout_sec,
            seoul_bus_station_radius_m=config.station_radius_m,
            seoul_bus_max_station_distance_m=config.max_station_distance_m,
        )
