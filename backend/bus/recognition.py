"""Run optional bus OCR on recent frames without blocking walking guidance."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import threading
from typing import Any, Callable

import numpy as np

from .base import InferenceContext, ModelSpec
from .config import BusConfig
from .pipeline import BusPipeline
from .bus_runtime.target import route_aliases


log = logging.getLogger(__name__)


def normalize_route(value: str) -> str:
    """Keep the display string in Boarding; use this form for OCR matching."""
    route = re.sub(r"\s+", "", value.strip()).removesuffix("번").upper()
    route_aliases(route)
    return route


@dataclass(frozen=True)
class _Frame:
    generation: int
    session_id: str
    frame_id: int
    captured_at_ms: int
    image: np.ndarray


class BusRecognizer:
    """One newest-frame queue, with generation checks for route/session changes."""

    def __init__(self, config: BusConfig, pipeline_factory: Callable[[], BusPipeline] | None = None):
        self.config = config
        self._factory = pipeline_factory or self._create_pipeline
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._session_id: str | None = None
        self._route: str | None = None
        self._pending: _Frame | None = None
        self._last_queued_at_ms: int | None = None
        self._latest: dict[str, Any] | None = None
        self._error: str | None = None
        self._loading = False
        self._closed = False

    def _create_pipeline(self) -> BusPipeline:
        spec = ModelSpec(id="bus-route-display-b", mode="bus", name="Bus B",
                         version="route-display-b", weights=self.config.route_display)
        return BusPipeline(spec, {
            "bus_detector": self.config.bus_detector,
            "parseq_weights": self.config.parseq_weights,
            "parseq_source": self.config.parseq_source,
        })

    def set_target(self, session_id: str | None, bus_number: str | None) -> None:
        route = None
        error = None
        if bus_number:
            try:
                route = normalize_route(bus_number)
            except ValueError:
                error = "unsupported_target_route"
        with self._condition:
            if self._closed:
                return
            if session_id == self._session_id and route == self._route and error == self._error:
                return
            self._generation += 1
            self._session_id = session_id
            self._route = route
            self._pending = None
            self._latest = None
            self._last_queued_at_ms = None
            self._error = error
            self._loading = bool(route and not error and all(self.config.model_files().values()))
            if self._loading and self._thread is None:
                self._thread = threading.Thread(target=self._run, name="bus-recognition", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def observe(self, session_id: str, frame: np.ndarray, frame_id: int,
                captured_at_ms: int) -> dict[str, Any]:
        """Queue a recent frame and return the latest result with its source timestamp."""
        with self._condition:
            if self._closed or self._session_id != session_id:
                return {"status": "idle", "event": None, "detections": [],
                        "captured_at_ms": None, "frame_id": None}
            files_ready = all(self.config.model_files().values())
            if self._route and files_ready and self._thread is None and not self._error:
                self._loading = True
                self._thread = threading.Thread(target=self._run, name="bus-recognition", daemon=True)
                self._thread.start()
            if self._route and files_ready and not self._error:
                if (self._last_queued_at_ms is None or
                        captured_at_ms - self._last_queued_at_ms >= self.config.interval_ms):
                    self._pending = _Frame(self._generation, session_id, frame_id,
                                           captured_at_ms, frame.copy())
                    self._last_queued_at_ms = captured_at_ms
                    self._condition.notify_all()
            if not self._route and not self._error:
                status = "idle"
            elif self._error:
                status = "error"
            elif not files_ready:
                status = "unavailable"
            elif self._loading:
                status = "loading"
            else:
                status = "searching"
            latest = self._latest
            if latest is not None and self._route:
                age = captured_at_ms - latest["captured_at_ms"]
                if 0 <= age <= self.config.max_result_age_ms:
                    return {**latest, "status": latest["status"] if not self._error else "error",
                            **({"error": self._error} if self._error else {})}
            return {
                "status": status, "event": None, "detections": [],
                "captured_at_ms": latest["captured_at_ms"] if latest else None,
                "frame_id": latest["frame_id"] if latest else None,
                **({"error": self._error} if self._error else {}),
            }

    def close(self, timeout: float = 2.0) -> None:
        """Discard queued/results state and stop the worker after its current call.

        Model calls cannot be safely interrupted; the generation change prevents a
        late model response from being published while shutdown waits are bounded.
        """
        with self._condition:
            self._closed = True
            self._generation += 1
            self._pending = None
            self._latest = None
            self._session_id = None
            self._route = None
            self._loading = False
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))

    def _run(self) -> None:
        pipeline = None
        active_session = None
        generation = -1
        load_failed = False
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._generation != generation
                                         or self._pending is not None)
                closed = self._closed
                current_generation = self._generation
                session_id = self._session_id
                route = self._route
                pending = self._pending
                self._pending = None
            if current_generation != generation or closed:
                if pipeline is not None and active_session is not None:
                    try:
                        pipeline.close_session(active_session)
                    except Exception:
                        log.exception("bus state reset failed")
                active_session = None
                load_failed = False
                generation = current_generation
                if closed:
                    return
            if route and active_session is None and not load_failed:
                if all(self.config.model_files().values()):
                    try:
                        if pipeline is None:
                            pipeline = self._factory()
                            pipeline.load()
                        pipeline.configure_target_route(route)
                        pipeline.reset_session(session_id)
                        active_session = session_id
                        with self._condition:
                            if generation == self._generation:
                                self._loading = False
                                self._condition.notify_all()
                    except Exception as exc:
                        log.exception("bus model initialization failed")
                        pipeline = None
                        load_failed = True
                        with self._condition:
                            if generation == self._generation:
                                self._error = f"bus_model_load_failed:{type(exc).__name__}"
                                self._loading = False
                                self._condition.notify_all()
            with self._condition:
                obsolete = self._closed or generation != self._generation
                # Loading can take longer than several camera frames. Start with
                # the newest queued image instead of processing the pre-load one.
                if not obsolete and self._pending is not None:
                    pending = self._pending
                    self._pending = None
            if (pending is None or pending.generation != generation or not route
                    or active_session is None or load_failed or obsolete):
                continue
            try:
                result = pipeline.infer(
                    pending.image,
                    InferenceContext(pending.session_id, pending.frame_id, pending.captured_at_ms, .25),
                )
                event = result["event"]
                status = ("matched" if event.get("matches") else
                          "error" if event.get("errors") else "searching")
                outcome = {
                    "status": status, "event": event,
                    "detections": result["detections"],
                    "captured_at_ms": pending.captured_at_ms,
                    "frame_id": pending.frame_id,
                }
            except Exception as exc:
                log.exception("bus inference failed")
                outcome = {
                    "status": "error", "event": None, "detections": [],
                    "captured_at_ms": pending.captured_at_ms,
                    "frame_id": pending.frame_id,
                    "error": f"bus_inference_failed:{type(exc).__name__}",
                }
            with self._condition:
                if pending.generation == self._generation and pending.session_id == self._session_id:
                    self._latest = outcome
                    self._condition.notify_all()
