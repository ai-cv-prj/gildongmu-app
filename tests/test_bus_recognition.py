"""Bus OCR worker isolation, newest-frame scheduling, and session cleanup."""
from dataclasses import replace
import threading

import numpy as np
import pytest

from backend.bus.config import load_bus_config
from backend.bus.recognition import BusRecognizer, normalize_route


class Pipeline:
    def __init__(self, *, block=False, fail=False):
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.started = threading.Event()
        self.fail = fail
        self.calls = []
        self.closed = []
        self.reset = []
        self.loads = 0
        self.route = None

    def load(self):
        self.loads += 1

    def configure_target_route(self, route):
        self.route = route

    def reset_session(self, session_id):
        self.reset.append((session_id, self.route))

    def close_session(self, session_id):
        self.closed.append(session_id)

    def infer(self, frame, context):
        route = self.route
        self.calls.append((context.frame_id, context.session_id, route, int(frame[0, 0, 0])))
        self.started.set()
        assert self.release.wait(3), "test did not release OCR worker"
        if self.fail:
            raise RuntimeError("test OCR failure")
        return {"event": {"target_route": route, "matches": [{"route_number": route}],
                          "errors": []}, "detections": [{"class_name": "bus"}]}


@pytest.fixture
def ready_config(tmp_path):
    paths = [tmp_path / filename for filename in ("route.pt", "bus.pt", "parseq.pt")]
    source = tmp_path / "parseq-src"
    source.mkdir()
    for path in [*paths, source / "hubconf.py"]:
        path.touch()
    return replace(load_bus_config(), route_display=paths[0], bus_detector=paths[1],
                   parseq_weights=paths[2], parseq_source=source,
                   interval_ms=100, max_result_age_ms=1000)


def wait_result(recognizer, frame_id):
    with recognizer._condition:
        assert recognizer._condition.wait_for(
            lambda: recognizer._latest is not None
            and recognizer._latest["frame_id"] == frame_id, timeout=3)


def test_worker_keeps_newest_frame_and_preserves_ocr_source_time(ready_config):
    pipeline = Pipeline(block=True)
    worker = BusRecognizer(ready_config, lambda: pipeline)
    frame = np.zeros((12, 12, 3), dtype=np.uint8)
    try:
        worker.set_target("first", " 701 번 ")
        assert worker.observe("first", frame, 1, 1000)["event"] is None
        assert pipeline.started.wait(3)
        worker.observe("first", frame, 2, 1200)
        worker.observe("first", frame, 3, 1400)
        frame[:] = 99  # queued pixels are owned by the worker
        pipeline.release.set()
        wait_result(worker, 3)
        result = worker.observe("first", frame, 4, 1450)
        assert [call[0] for call in pipeline.calls] == [1, 3]
        assert pipeline.calls[-1][3] == 0
        assert result["status"] == "matched"
        assert result["captured_at_ms"] == 1400
        assert result["frame_id"] == 3
        assert result["event"]["target_route"] == "701"
        assert worker.observe("different-session", frame, 1, 1450)["event"] is None
        # The old match must not be re-stamped or announced as a current match.
        stale = worker.observe("first", frame, 5, 2501)
        assert stale["event"] is None
        assert stale["detections"] == []
        assert stale["status"] == "searching"
    finally:
        pipeline.release.set()
        worker.close()
    assert not worker._thread.is_alive()


@pytest.mark.parametrize("next_session", ["first", "second"])
def test_target_change_discards_inflight_match_and_resets_history(ready_config, next_session):
    pipeline = Pipeline(block=True)
    worker = BusRecognizer(ready_config, lambda: pipeline)
    frame = np.zeros((12, 12, 3), dtype=np.uint8)
    try:
        worker.set_target("first", "701")
        worker.observe("first", frame, 1, 1000)
        assert pipeline.started.wait(3)
        worker.set_target(next_session, "604")
        assert worker.observe(next_session, frame, 2, 1200)["event"] is None
        pipeline.release.set()
        wait_result(worker, 2)
        result = worker.observe(next_session, frame, 3, 1250)
        assert result["event"]["target_route"] == "604"
        assert result["frame_id"] == 2
        assert pipeline.closed == ["first"]
        assert pipeline.reset == [("first", "701"), (next_session, "604")]
        assert pipeline.loads == 1
        worker.set_target(None, None)
        assert worker.observe(next_session, frame, 4, 1300)["status"] == "idle"
        assert worker._latest is None
        assert worker._pending is None
    finally:
        pipeline.release.set()
        worker.close()
    assert pipeline.closed == ["first", next_session]


@pytest.mark.parametrize("change_target", [False, True])
def test_slow_model_load_uses_latest_frame_and_current_target(ready_config, change_target):
    loading = threading.Event()
    finish_loading = threading.Event()

    class LoadingPipeline(Pipeline):
        def load(self):
            super().load()
            loading.set()
            assert finish_loading.wait(3), "test did not release model loading"

    pipeline = LoadingPipeline()
    worker = BusRecognizer(ready_config, lambda: pipeline)
    frame = np.zeros((12, 12, 3), dtype=np.uint8)
    try:
        worker.set_target("first", "701")
        worker.observe("first", frame, 1, 1000)
        assert loading.wait(3)
        if change_target:
            worker.set_target("first", "604")
        worker.observe("first", frame, 2, 1200)
        worker.observe("first", frame, 3, 1400)
        finish_loading.set()
        wait_result(worker, 3)
        assert [call[0] for call in pipeline.calls] == [3]
        result = worker.observe("first", frame, 4, 1450)
        assert result["event"]["target_route"] == ("604" if change_target else "701")
        assert result["captured_at_ms"] == 1400
    finally:
        finish_loading.set()
        worker.close()


def test_close_is_bounded_and_late_inference_cannot_publish(ready_config):
    pipeline = Pipeline(block=True)
    worker = BusRecognizer(ready_config, lambda: pipeline)
    frame = np.zeros((12, 12, 3), dtype=np.uint8)
    try:
        worker.set_target("first", "701")
        worker.observe("first", frame, 1, 1000)
        assert pipeline.started.wait(3)
        worker.close(timeout=0)
        assert worker.observe("first", frame, 2, 1200)["status"] == "idle"
        pipeline.release.set()
        worker.close()
        assert not worker._thread.is_alive()
        assert worker._latest is None
        assert pipeline.closed == ["first"]
        worker.set_target("new-session", "604")
        assert worker.observe("new-session", frame, 1, 1300)["status"] == "idle"
    finally:
        pipeline.release.set()
        worker.close()


def test_inference_failure_is_reported_and_next_frame_can_recover(ready_config):
    pipeline = Pipeline(fail=True)
    worker = BusRecognizer(ready_config, lambda: pipeline)
    frame = np.zeros((12, 12, 3), dtype=np.uint8)
    try:
        worker.set_target("first", "701")
        worker.observe("first", frame, 1, 1000)
        wait_result(worker, 1)
        failed = worker.observe("first", frame, 2, 1050)
        assert failed["status"] == "error"
        assert failed["event"] is None
        assert failed["error"] == "bus_inference_failed:RuntimeError"
        pipeline.fail = False
        worker.observe("first", frame, 3, 1200)
        wait_result(worker, 3)
        assert worker.observe("first", frame, 4, 1250)["status"] == "matched"
    finally:
        worker.close()


def test_model_load_failure_does_not_escape_and_target_restart_retries(ready_config):
    calls = []
    pipeline = Pipeline()

    def factory():
        calls.append(True)
        if len(calls) == 1:
            raise ImportError("optional OCR dependency is absent")
        return pipeline

    worker = BusRecognizer(ready_config, factory)
    frame = np.zeros((12, 12, 3), dtype=np.uint8)
    try:
        worker.set_target("first", "701")
        with worker._condition:
            assert worker._condition.wait_for(lambda: worker._error is not None, timeout=3)
        result = worker.observe("first", frame, 1, 1000)
        assert result["error"] == "bus_model_load_failed:ImportError"
        worker.set_target("first", "701")
        worker.observe("first", frame, 2, 1200)
        wait_result(worker, 2)
        assert worker.observe("first", frame, 3, 1250)["status"] == "matched"
        assert len(calls) == 2
    finally:
        worker.close()


def test_missing_models_and_unsupported_route_do_not_start_worker(ready_config):
    ready_config.route_display.unlink()
    worker = BusRecognizer(ready_config, lambda: pytest.fail("should not load"))
    frame = np.zeros((12, 12, 3), dtype=np.uint8)
    try:
        worker.set_target("first", "701")
        assert worker.observe("first", frame, 1, 1000)["status"] == "unavailable"
        assert worker._thread is None
        assert worker._pending is None
        worker.set_target("first", "?")
        assert worker.observe("first", frame, 2, 1200)["error"] == "unsupported_target_route"
        assert worker._thread is None
    finally:
        worker.close()


@pytest.mark.parametrize(("number", "normalized"), [(" 701 번 ", "701"),
                                                   (" n26 ", "N26"),
                                                   ("마포 07", "마포07"),
                                                   ("021", "021"), ("701-1", "701-1")])
def test_route_normalization_preserves_meaning(number, normalized):
    assert normalize_route(number) == normalized
