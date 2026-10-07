"""Bounded client diagnostics and request correlation without request contents."""

import logging
import math
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4


log = logging.getLogger(__name__)
MAX_EVENT_BYTES = 64 * 1024
MAX_EVENTS = 25
TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")
TEXT_FIELDS = {
    "method", "operation", "error_name", "error_message", "visibility_state",
    "reason", "stage", "screen", "upload_stage", "state", "abort_reason",
    "connection_type", "effective_type", "user_agent",
}
TOKEN_FIELDS = {"request_id", "server_request_id", "cf_ray"}
SESSION_FIELDS = {"pending_stop_session_id", "pending_check_session_id"}
NUMBER_FIELDS = {
    "http_status", "elapsed_ms", "duration_ms", "attempt", "frame_id", "clip_id",
    "last_frame_id", "buffered_bytes", "clip_count", "chunk_index", "retry_in_ms",
    "monotonic_clock_ms", "time_origin_ms",
}
BOOL_FIELDS = {
    "online", "running", "starting", "stopping", "paused", "camera_active",
    "recording_active", "retryable", "timed_out", "aborted", "connection_lost",
}
ID_LIST_FIELDS = {"pending_clip_ids", "expected_clip_ids", "server_clip_ids"}


def safe_text(value, limit=500):
    """Drop control characters and URL query strings from bounded error text."""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"https?://[^\s]+", lambda m: m[0].split("?", 1)[0].split("#", 1)[0], value)
    return value[:limit]


def sanitize_events(payload):
    """Validate a complete batch before retaining only documented metadata."""
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError("events must contain 1 to 25 objects")
    sanitized = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        for key in ("event_id", "type"):
            if not isinstance(event.get(key), str) or not TOKEN.fullmatch(event[key]):
                raise ValueError(f"invalid {key}")
        session_id = event.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)):
            raise ValueError("invalid session_id")
        occurred = event.get("occurred_at_ms")
        if type(occurred) is not int or not 0 < occurred <= 2**53 - 1:
            raise ValueError("invalid occurred_at_ms")
        record = {key: event[key] for key in ("event_id", "type", "occurred_at_ms")}
        record["session_id"] = session_id
        for key, value in event.items():
            if key in TEXT_FIELDS and isinstance(value, str):
                record[key] = safe_text(value, 500 if key == "error_message" else 300 if key == "user_agent" else 120)
            elif key in TOKEN_FIELDS and isinstance(value, str) and TOKEN.fullmatch(value):
                record[key] = value
            elif key in SESSION_FIELDS and (value is None or isinstance(value, str) and SESSION_ID.fullmatch(value)):
                record[key] = value
            elif key in NUMBER_FIELDS and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2**53 - 1:
                record[key] = value
            elif key in BOOL_FIELDS and isinstance(value, bool):
                record[key] = value
            elif key in ID_LIST_FIELDS and isinstance(value, list):
                if len(value) > 100 or any(type(item) is not int or not 0 < item <= 2**53 - 1 for item in value):
                    raise ValueError(f"invalid {key}")
                record[key] = value
            elif key == "path" and isinstance(value, str):
                path = urlsplit(value).path
                if path.startswith("/api/"):
                    record[key] = safe_text(path, 300)
        sanitized.append(record)
    return sanitized


class RequestCorrelationMiddleware:
    """Record ASGI receipt and response emission; neither proves client receipt."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        candidate = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = candidate if TOKEN.fullmatch(candidate) else uuid4().hex
        cf_ray = headers.get(b"cf-ray", b"").decode("latin-1")
        cf_ray = cf_ray if TOKEN.fullmatch(cf_ray) else "-"
        scope.setdefault("state", {})["request_id"] = request_id
        path = safe_text(scope["path"], 300)
        method = safe_text(scope.get("method", ""), 20)
        start = time.perf_counter()
        status = None
        emitted = False
        disconnected = False
        log.info("request_received request_id=%s method=%s path=%s cf_ray=%s", request_id, method, path, cf_ray)

        async def correlated_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                log.info("request_disconnected request_id=%s method=%s path=%s", request_id, method, path)
            return message

        async def correlated_send(message):
            nonlocal status, emitted
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"x-request-id"]
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": response_headers}
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                emitted = True
                log.info("response_emitted request_id=%s method=%s path=%s status=%s elapsed_ms=%.1f",
                         request_id, method, path, status, (time.perf_counter() - start) * 1000)

        try:
            await self.app(scope, correlated_receive, correlated_send)
        except BaseException as error:
            log.warning("request_failed request_id=%s method=%s path=%s status=%s elapsed_ms=%.1f error_name=%s response_emitted=%s disconnected=%s",
                        request_id, method, path, status, (time.perf_counter() - start) * 1000,
                        type(error).__name__, emitted, disconnected)
            raise
        finally:
            if not emitted:
                log.info("request_incomplete request_id=%s method=%s path=%s status=%s elapsed_ms=%.1f disconnected=%s",
                         request_id, method, path, status, (time.perf_counter() - start) * 1000, disconnected)
