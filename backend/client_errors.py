"""Persist browser fetch failures independently of live inference sessions."""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field


log = logging.getLogger(__name__)


def hub_source_id():
    value = os.getenv("HUB_SOURCE_ID")
    if value is None:
        value = dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("HUB_SOURCE_ID")
    return (value or "").strip()[:80] or None


class ClientFetchError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(pattern=r"^[A-Za-z0-9-]{8,80}$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9-]{8,80}$")
    occurred_at_ms: int = Field(gt=0)
    source_id: str | None = Field(default=None, max_length=80)
    session_id: str | None = Field(default=None, max_length=80)
    frame_id: int | None = Field(default=None, ge=1)
    captured_at_ms: int | None = Field(default=None, gt=0)
    clip_id: int | None = Field(default=None, ge=1)
    path: str = Field(max_length=500, pattern=r"^/api/[^?\r\n]*$")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    phase: Literal["fetch", "response_body"]
    attempt: int = Field(default=1, ge=1, le=100)
    error_name: str = Field(max_length=80)
    message: str = Field(max_length=500)
    http_status: int | None = Field(default=None, ge=100, le=599)
    duration_ms: int = Field(ge=0)
    last_success_at_ms: int | None = Field(default=None, gt=0)
    online: bool | None = None
    visibility: str | None = Field(default=None, max_length=20)
    screen: str | None = Field(default=None, max_length=40)
    ui_action: str | None = Field(default=None, max_length=40)
    running: bool = False
    recording_active: bool = False
    pending_upload_count: int = Field(default=0, ge=0, le=100)


class ClientFetchErrors(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[ClientFetchError] = Field(min_length=1, max_length=20)


class ClientErrorStore:
    def __init__(self, output_dir, source_id):
        self.directory = Path(output_dir) / "logs"
        self.source_id = source_id
        self.lock = threading.Lock()
        self.current_path = None
        self.seen = set()

    def append(self, records):
        now = datetime.now(timezone.utc)
        path = self.directory / f"client-errors-{now:%Y%m%d}.jsonl"
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            if path != self.current_path:
                self.seen = set()
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            self.seen.add(json.loads(line)["event_id"])
                        except (ValueError, KeyError):
                            continue
                self.current_path = path
            with path.open("a", encoding="utf-8") as file:
                for record in records:
                    if record.event_id in self.seen:
                        continue
                    stored = {**record.model_dump(), "client_source_id": record.source_id,
                              "source_id": self.source_id, "server_at": now.isoformat()}
                    file.write(json.dumps(stored, ensure_ascii=False, allow_nan=False) + "\n")
                    file.flush()
                    self.seen.add(record.event_id)
                    log.warning("client fetch failed source=%r event=%s request=%s session=%s "
                                "phase=%s path=%s error=%r", self.source_id, record.event_id,
                                record.request_id, record.session_id, record.phase, record.path,
                                record.message)
        return [record.event_id for record in records]
