"""Authenticated collection and viewing of field-test artifacts; no inference imports."""

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from result_hub.storage import CATEGORIES, CategoryConflict, Archive, SESSION_PATTERN, SOURCE_PATTERN, validate_json, validate_path


STATIC = Path(__file__).parent / "static"


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    categories: list[Literal["obstacle", "traffic_light", "bus"]] = Field(max_length=3)
    revision: int = Field(ge=0)


@dataclass(frozen=True)
class HubSettings:
    storage_dir: Path
    viewer_username: str
    viewer_password: str
    source_tokens: dict[str, str]
    max_upload_bytes: int = 1024 * 1024 * 1024
    admin_password: str = ""

    @classmethod
    def from_env(cls):
        try:
            tokens = json.loads(os.environ.get("HUB_SOURCE_TOKENS_JSON", "{}"))
            limit = int(os.environ.get("HUB_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
        except (ValueError, TypeError) as error:
            raise ValueError("HUB_SOURCE_TOKENS_JSON 또는 HUB_MAX_UPLOAD_BYTES 설정을 확인하세요.") from error
        return cls(Path(os.environ.get("HUB_STORAGE_DIR", "data/result-hub")),
                   os.environ.get("HUB_VIEWER_USERNAME", ""),
                   os.environ.get("HUB_VIEWER_PASSWORD", ""), tokens, limit,
                   os.environ.get("HUB_ADMIN_PASSWORD", ""))

    def validate(self):
        if not self.viewer_username or len(self.viewer_username) > 80 or len(self.viewer_password) < 16:
            raise ValueError("허브 조회 계정과 16자 이상의 HUB_VIEWER_PASSWORD를 설정하세요.")
        if not isinstance(self.source_tokens, dict) or not self.source_tokens:
            raise ValueError("HUB_SOURCE_TOKENS_JSON에 서버별 전송 토큰을 설정하세요.")
        for source_id, token in self.source_tokens.items():
            if not SOURCE_PATTERN.fullmatch(source_id) or not isinstance(token, str) or len(token) < 24:
                raise ValueError("서버 ID는 영문·숫자·밑줄·하이픈 1~64자, 전송 토큰은 24자 이상이어야 합니다.")
        if len(set(self.source_tokens.values())) != len(self.source_tokens):
            raise ValueError("서버별 전송 토큰은 서로 달라야 합니다.")
        if type(self.max_upload_bytes) is not int or self.max_upload_bytes <= 0:
            raise ValueError("HUB_MAX_UPLOAD_BYTES는 양의 정수여야 합니다.")
        if self.admin_password and (
            len(self.admin_password) < 16
            or self.admin_password == self.viewer_password
            or self.admin_password in self.source_tokens.values()
            or any(ord(character) < 33 or ord(character) > 126 for character in self.admin_password)
        ):
            raise ValueError("HUB_ADMIN_PASSWORD는 조회 비밀번호·전송 토큰과 다른 16자 이상의 공백 없는 ASCII 값이어야 합니다.")


def _equal(left, right):
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def file_url(source_id, path):
    return f"/api/files/{quote(source_id, safe='')}/{quote(path, safe='/')}"


def create_app(settings=None):
    settings = settings or HubSettings.from_env()
    settings.validate()
    archive = Archive(settings.storage_dir)
    app = FastAPI(title="길동무 팀 결과 보관함", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.archive = archive
    # A single publication stream keeps bounded memory/disk pressure across four uploaders.
    upload_lock = asyncio.Lock()
    basic = HTTPBasic(auto_error=False)
    bearer = HTTPBearer(auto_error=False)

    def viewer(credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)]):
        if credentials is None:
            raise HTTPException(401, "조회 계정으로 로그인하세요.", headers={"WWW-Authenticate": 'Basic realm="Gildongmu results", charset="UTF-8"'})
        user_ok = _equal(credentials.username, settings.viewer_username)
        password_ok = _equal(credentials.password, settings.viewer_password)
        if not (user_ok and password_ok):
            raise HTTPException(401, "조회 계정을 확인하세요.", headers={"WWW-Authenticate": 'Basic realm="Gildongmu results"'})

    def source(source_id: str, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        expected = settings.source_tokens.get(source_id)
        if credentials is None or expected is None or not _equal(credentials.credentials, expected):
            raise HTTPException(401, "전송 서버 인증에 실패했습니다.")

    def administrator(request: Request):
        if not settings.admin_password:
            raise HTTPException(403, "관리자 기능이 설정되지 않았습니다.")
        # Destructive operations require a separate credential; classification uses
        # the shared viewer account with its own same-origin request header.
        password = request.headers.get("x-hub-admin-password", "")
        if request.headers.get("sec-fetch-site") == "cross-site" or not _equal(password, settings.admin_password):
            raise HTTPException(403, "관리자 비밀번호를 확인하세요.")

    def reject_deleted(source_id, path):
        if archive.is_deleted(source_id, path):
            raise HTTPException(410, "관리자가 중앙 휴지통으로 이동한 기록입니다.",
                                headers={"X-Hub-Deleted": "true"})

    def checked_path(source_id, path, *, create_parent=False):
        try:
            return archive.file(source_id, path, create_parent=create_parent)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.head("/api/ingest/{source_id}/{path:path}", dependencies=[Depends(source)])
    def artifact_head(source_id: str, path: str):
        checked_path(source_id, path)
        reject_deleted(source_id, path)
        try:
            info = archive.info(source_id, path)
        except OSError as error:
            reject_deleted(source_id, path)
            raise HTTPException(503, "저장소를 확인할 수 없습니다.") from error
        reject_deleted(source_id, path)
        if info is None:
            raise HTTPException(404, "아직 수집되지 않은 파일입니다.")
        return Response(headers={"X-Content-SHA256": info["sha256"], "Content-Length": str(info["size"])})

    @app.put("/api/ingest/{source_id}/{path:path}", dependencies=[Depends(source)])
    async def ingest(source_id: str, path: str, request: Request):
        checked_path(source_id, path)
        digest = request.headers.get("x-content-sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(400, "X-Content-SHA256 해시가 필요합니다.")
        limit = min(settings.max_upload_bytes, 2 * 1024 * 1024) if path.endswith(".json") else settings.max_upload_bytes
        content_length = request.headers.get("content-length")
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdecimal():
                raise HTTPException(400, "Content-Length 값이 올바르지 않습니다.")
            if int(content_length) > limit:
                raise HTTPException(413, "파일 크기 제한을 초과했습니다.")
        temporary = None
        try:
            async with upload_lock:
                reject_deleted(source_id, path)
                target = checked_path(source_id, path, create_parent=True)
                temporary = target.parent / f".upload-{uuid4().hex}"
                size = 0
                hasher = hashlib.sha256()
                with temporary.open("xb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > limit:
                            raise HTTPException(413, "파일 크기 제한을 초과했습니다.")
                        hasher.update(chunk)
                        await run_in_threadpool(output.write, chunk)
                    await run_in_threadpool(output.flush)
                    await run_in_threadpool(os.fsync, output.fileno())
                if content_length is not None and int(content_length) != size:
                    raise HTTPException(400, "전송 크기가 Content-Length와 일치하지 않습니다.")
                if hasher.hexdigest() != digest:
                    raise HTTPException(422, "전송한 파일의 SHA-256이 일치하지 않습니다.")
                try:
                    await run_in_threadpool(validate_json, path, temporary)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from error
                await run_in_threadpool(temporary.replace, target)
                info = await run_in_threadpool(archive.record, source_id, path, digest)
                return {"ok": True, **info}
        except OSError as error:
            raise HTTPException(503, "결과 저장에 실패했습니다. 저장 공간과 권한을 확인하세요.") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    read = APIRouter(dependencies=[Depends(viewer)])

    @read.get("/api/capabilities")
    def capabilities():
        return {"admin_enabled": bool(settings.admin_password)}

    admin = APIRouter(prefix="/api/admin", dependencies=[Depends(viewer), Depends(administrator)])

    @admin.post("/login")
    def admin_login():
        return {"ok": True}

    @admin.get("/trash")
    async def trash():
        async with upload_lock:
            return {"items": await run_in_threadpool(archive.trash_entries)}

    async def mutate_archive(action, *args):
        async with upload_lock:
            try:
                item = await run_in_threadpool(action, *args)
            except FileNotFoundError:
                raise HTTPException(404, "해당 기록을 찾을 수 없습니다.") from None
            except FileExistsError:
                raise HTTPException(409, "같은 위치에 기록이 있어 복원할 수 없습니다.") from None
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
            except (OSError, sqlite3.Error):
                raise HTTPException(503, "저장소를 변경하지 못했습니다. 상태를 새로고침한 뒤 다시 시도하세요.") from None
            return {"ok": True, "item": item}

    @admin.delete("/sessions/{source_id}/{session_id}")
    async def delete_session(source_id: str, session_id: str):
        return await mutate_archive(archive.delete_session, source_id, session_id)

    @admin.delete("/logs/{source_id}/{path:path}")
    async def delete_log(source_id: str, path: str):
        return await mutate_archive(archive.delete_log, source_id, path)

    @admin.post("/trash/{trash_id}/restore")
    async def restore(trash_id: str):
        return await mutate_archive(archive.restore, trash_id)

    @read.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @read.get("/static/{name}")
    def static(name: str):
        if name not in ("app.js", "app.css"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @read.get("/api/sources")
    def sources():
        entries = archive.entries()
        source_ids = sorted(set(settings.source_tokens) | {row["source_id"] for row in entries})
        return {"sources": [{
            "source_id": source_id,
            "last_received_at": max((row["received_at"] for row in entries if row["source_id"] == source_id), default=None),
            "session_count": sum(row["source_id"] == source_id and row["path"].endswith("/session.json") for row in entries),
        } for source_id in source_ids]}

    @read.put("/api/sessions/{source_id}/{session_id}/clips/{clip_key}/categories")
    async def update_categories(source_id: str, session_id: str, clip_key: str, payload: CategoryUpdate, request: Request):
        # Basic auth is ambient in browsers; require a non-simple same-origin request.
        if (request.headers.get("x-hub-request") != "categories"
                or request.headers.get("sec-fetch-site") == "cross-site"):
            raise HTTPException(403, "허브 화면에서 분류를 저장하세요.")
        try:
            return await mutate_archive(archive.set_categories, source_id, session_id, clip_key,
                                        payload.categories, payload.revision)
        except CategoryConflict:
            raise HTTPException(409, "다른 팀원이 분류를 변경했습니다. 상세 새로고침 후 다시 선택해 주세요.") from None

    def session_clips(source_id, session_id, entries, classifications):
        paths = {entry["path"] for entry in entries}
        prefix = f"sessions/{session_id}/"
        clips = []
        for entry in sorted(entries, key=lambda row: row["path"]):
            if not entry["path"].endswith("/manifest.json"):
                continue
            manifest = archive.read_json(source_id, entry["path"])
            if manifest is None:
                continue
            folder = entry["path"].rsplit("/", 1)[0] + "/"
            clip_key = entry["path"].split("/")[3]
            original = next((folder + name for name in ("original.webm", "original.mp4") if folder + name in paths), None)
            inference = folder + "inference.mp4"
            clips.append({**manifest, "clip_id": int(clip_key.removeprefix("clip_")),
                          "category_key": clip_key, "legacy": False,
                          **classifications.get((source_id, session_id, clip_key), {"categories": [], "category_revision": 0}),
                          "original_url": file_url(source_id, original) if original else None,
                          "inference_url": file_url(source_id, inference) if inference in paths and manifest.get("state") == "ready" else None,
                          "manifest_url": file_url(source_id, entry["path"])})
        if any(prefix + name in paths for name in ("camera.mp4", "camera_overlay.mp4")):
            clips.append({"clip_id": None, "category_key": "legacy", "legacy": True, "state": "ready",
                          **classifications.get((source_id, session_id, "legacy"), {"categories": [], "category_revision": 0}),
                          "original_url": file_url(source_id, prefix + "camera.mp4") if prefix + "camera.mp4" in paths else None,
                          "inference_url": file_url(source_id, prefix + "camera_overlay.mp4") if prefix + "camera_overlay.mp4" in paths else None})
        return clips

    @read.get("/api/sessions")
    def sessions(source_id: str | None = None, q: str = Query(default="", max_length=200),
                 category: str = Query(default="", max_length=32)):
        if category not in ("", "unclassified", *CATEGORIES):
            raise HTTPException(400, "영상 분류 필터가 올바르지 않습니다.")
        classifications = archive.category_index()
        entries = archive.entries(source_id or None)
        rows = []
        for entry in entries:
            if not entry["path"].endswith("/session.json"):
                continue
            source_id = entry["source_id"]
            value = archive.read_json(source_id, entry["path"])
            if value is None:
                continue
            session_id = entry["path"].split("/")[1]
            if q and q.casefold() not in " ".join(str(value.get(key, "")) for key in (
                    "device_name", "note", "started_at", "date", "folder_name")).casefold() + " " + source_id.casefold():
                continue
            prefix = f"sessions/{session_id}/"
            related = [item for item in entries if item["source_id"] == source_id and item["path"].startswith(prefix)]
            clips = session_clips(source_id, session_id, related, classifications)
            matches = [clip for clip in clips if not category
                       or (category == "unclassified" and not clip["categories"])
                       or category in clip["categories"]]
            if category and not matches:
                continue
            categories = [value for value in CATEGORIES if any(value in clip["categories"] for clip in clips)]
            rows.append({**{key: value.get(key) for key in ("device_name", "note", "started_at", "ended_at", "frame_count")},
                         "source_id": source_id, "session_id": session_id, "categories": categories,
                         "unclassified_clip_count": sum(not clip["categories"] for clip in clips),
                         "matched_clip_count": len(matches),
                         "received_at": max(item["received_at"] for item in related),
                         "clip_count": len(clips),
                         "ready_clip_count": sum(item.get("state") == "ready" for item in clips)})
        rows.sort(key=lambda row: row.get("started_at") or "", reverse=True)
        return {"sessions": rows}

    def public_entry(entry):
        return {key: entry[key] for key in ("path", "size", "received_at")} | {
            "url": file_url(entry["source_id"], entry["path"])}

    @read.get("/api/sessions/{source_id}/{session_id}")
    def session_detail(source_id: str, session_id: str):
        if not SESSION_PATTERN.fullmatch(session_id):
            raise HTTPException(404)
        prefix = f"sessions/{session_id}/"
        checked_path(source_id, prefix + "session.json")
        if archive.is_deleted(source_id, prefix + "session.json"):
            raise HTTPException(404, "휴지통으로 이동한 기록입니다.")
        value = archive.read_json(source_id, prefix + "session.json")
        if value is None:
            raise HTTPException(404, "세션을 찾을 수 없습니다.")
        entries = [entry for entry in archive.entries(source_id) if entry["path"].startswith(prefix)]
        clips = session_clips(source_id, session_id, entries, archive.category_index())
        return {"source_id": source_id, "session_id": session_id, "session": value,
                "files": [public_entry(entry) for entry in entries], "clips": clips,
                "provenance": archive.read_json(source_id, prefix + "provenance.json")}

    @read.get("/api/logs")
    def logs(source_id: str | None = None):
        return {"logs": [{"source_id": entry["source_id"], **public_entry(entry)}
                         for entry in archive.entries(source_id or None) if entry["path"].startswith("logs/")]}

    @read.get("/api/files/{source_id}/{path:path}")
    def artifact(source_id: str, path: str, download: bool = False):
        target = checked_path(source_id, path)
        if archive.is_deleted(source_id, path):
            raise HTTPException(404, "휴지통으로 이동한 기록입니다.")
        if not target.is_file():
            raise HTTPException(404, "아직 수집되지 않은 파일입니다.")
        suffix = target.suffix
        media = {".mp4": "video/mp4", ".webm": "video/webm", ".json": "application/json"}.get(suffix, "text/plain; charset=utf-8")
        return FileResponse(target, media_type=media, filename=target.name,
                            content_disposition_type="attachment" if download else "inline")

    @read.get("/api/preview/{source_id}/{path:path}")
    def preview(source_id: str, path: str, lines: int = Query(default=200, ge=1, le=1000)):
        target = checked_path(source_id, path)
        if archive.is_deleted(source_id, path):
            raise HTTPException(404, "휴지통으로 이동한 기록입니다.")
        if target.suffix in (".mp4", ".webm"):
            raise HTTPException(400, "텍스트 파일만 미리 볼 수 있습니다.")
        if not target.is_file():
            raise HTTPException(404, "아직 수집되지 않은 파일입니다.")
        try:
            with target.open("rb") as stream:
                size = stream.seek(0, 2)
                offset = max(0, size - 1024 * 1024)
                stream.seek(offset)
                data = stream.read(1024 * 1024)
        except FileNotFoundError:
            raise HTTPException(404, "기록이 이동되었습니다. 목록을 새로고침하세요.") from None
        if offset:
            data = data.partition(b"\n")[2]
        text_lines = data.decode("utf-8", errors="replace").splitlines()
        return {"text": "\n".join(text_lines[-lines:]), "truncated": offset > 0 or len(text_lines) > lines}

    app.include_router(read)
    app.include_router(admin)
    return app
