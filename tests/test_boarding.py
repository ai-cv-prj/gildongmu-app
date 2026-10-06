"""Arrival stop acknowledgement and input lifecycle, without model weights."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.boarding import Boarding, BoardingError
from backend.app import create_app
from backend.session import SessionManager
from test_realtime_app import FakeModels

ARRIVAL = {"nearby": True, "arrival_event_id": 1}


def test_only_completed_stop_arms_stationary_input_and_loss_does_not_clear_it():
    state = Boarding()
    state.observe(ARRIVAL)
    assert state.status == "awaiting_stop"
    assert not state.stationary
    state.observe({"nearby": False, "arrival_event_id": 1})
    state.act("stop_announced", 1)
    assert state.stationary
    state.observe({"nearby": False, "arrival_event_id": 1})
    assert state.status == "pending"


def test_crossing_and_historical_arrival_do_not_open_form():
    state = Boarding()
    assert state.observe(ARRIVAL, crossing_active=True)["status"] == "searching"
    assert state.observe({"nearby": False, "arrival_event_id": 1})["status"] == "searching"
    assert state.observe(ARRIVAL)["status"] == "awaiting_stop"


@pytest.mark.parametrize("number", ["7016", "N26", "마을버스 021", "  021  "])
def test_submit_preserves_route_string_and_releases_suppression(number):
    state = Boarding()
    state.observe(ARRIVAL)
    state.act("stop_announced", 1)
    submitted = state.act("submit", 1, number)
    assert submitted["bus_number"] == number.strip()
    assert not state.stationary
    assert state.act("submit", 1, number) == submitted
    assert state.act("stop_announced", 1) == submitted


@pytest.mark.parametrize("number", ["", "  ", None, "x" * 31, "12\n3"])
def test_invalid_number_does_not_leave_input_mode(number):
    state = Boarding()
    state.observe(ARRIVAL)
    state.act("stop_announced", 1)
    with pytest.raises(BoardingError):
        state.act("submit", 1, number)
    assert state.stationary


def test_cancel_is_idempotent_and_reopen_requires_another_stop():
    state = Boarding()
    state.observe(ARRIVAL)
    state.act("stop_announced", 1)
    cancelled = state.act("cancel", 1)
    assert not state.stationary
    assert state.act("cancel", 1) == cancelled
    assert state.act("stop_announced", 1) == cancelled
    assert state.observe(ARRIVAL) == cancelled
    assert state.act("reopen", 1)["status"] == "awaiting_stop"
    assert not state.stationary


def test_api_validates_session_arrival_and_logs_only_changed_actions(tmp_path):
    manager = SessionManager(tmp_path, model_factory=FakeModels)
    client = TestClient(create_app(manager))
    session = client.post("/api/sessions", json={"device_name": "Phone"}).json()
    url = f"/api/sessions/{session['session_id']}/boarding"
    assert client.put(url, json={"action": "cancel", "arrival_event_id": 1}).status_code == 422
    manager.models.boarding.observe(ARRIVAL)
    assert client.put(url, json={"action": "stop_announced", "arrival_event_id": 2}).status_code == 422
    pending = client.put(url, json={"action": "stop_announced", "arrival_event_id": 1}).json()
    assert pending["assumed_stationary"]
    assert client.put(url, json={"action": "submit", "arrival_event_id": 1, "bus_number": "  "}).status_code == 422
    for _ in range(2):
        result = client.put(url, json={"action": "cancel", "arrival_event_id": 1}).json()
    assert result["status"] == "cancelled"
    assert not client.get(url).json()["assumed_stationary"]
    folder = tmp_path / session["date"] / session["folder_name"]
    records = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert [record["action"] for record in records] == ["stop_announced", "cancel"]
    client.post("/api/sessions/stop", json={"session_id": session["session_id"]})
    assert client.put(url, json={"action": "cancel", "arrival_event_id": 1}).status_code == 409
    client.post("/api/sessions", json={"device_name": "Phone"})
    assert manager.models.boarding.status == "searching"
