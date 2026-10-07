"""An allowlisted file archive with atomic publication and a small SQLite index."""

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


SOURCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
SESSION_PATTERN = re.compile(r"[0-9a-f]{32}")
ARTIFACT_PATTERN = re.compile(
    r"(?:logs/app\.log(?:\.\d{4}-\d{2}-\d{2})?|"
    r"sessions/[0-9a-f]{32}/(?:session\.json|provenance\.json|results\.jsonl|events\.jsonl|"
    r"camera\.mp4|camera_overlay\.mp4|"
    r"clips/clip_[0-9]{3}/(?:manifest\.json|original\.(?:webm|mp4)|inference\.mp4)))"
)


def validate_path(source_id, path):
    if not SOURCE_PATTERN.fullmatch(source_id) or not ARTIFACT_PATTERN.fullmatch(path):
        raise ValueError("허용되지 않은 결과 경로입니다.")


def validate_json(path, temporary):
    """Small metadata must be valid before replacing a previously usable copy."""
    if not path.endswith(".json"):
        return
    try:
        value = json.loads(temporary.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ValueError("JSON 메타데이터가 올바르지 않습니다.") from error
    if not isinstance(value, dict):
        raise ValueError("JSON 메타데이터는 객체여야 합니다.")
    if path.endswith("/session.json"):
        session_id = path.split("/")[1]
        if value.get("session_id", value.get("id")) != session_id:
            raise ValueError("파일 경로와 세션 번호가 일치하지 않습니다.")
        for key in ("device_name", "note", "started_at", "ended_at"):
            if key in value and value[key] is not None and not isinstance(value[key], str):
                raise ValueError(f"session.{key}는 문자열이어야 합니다.")
        if "frame_count" in value and (type(value["frame_count"]) is not int or value["frame_count"] < 0):
            raise ValueError("frame_count는 0 이상의 정수여야 합니다.")
    if path.endswith("/manifest.json"):
        clip_id = int(path.split("/")[3].removeprefix("clip_"))
        if type(value.get("clip_id")) is not int or value["clip_id"] != clip_id:
            raise ValueError("파일 경로와 영상 구간 번호가 일치하지 않습니다.")
        if value.get("state") not in ("uploading", "pending", "rendering", "ready", "failed", "no_frames"):
            raise ValueError("영상 변환 상태가 올바르지 않습니다.")


class Archive:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = self.root / "sources"
        self.files.mkdir(exist_ok=True)
        self.database = self.root / "index.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
                source_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, received_at TEXT NOT NULL,
                PRIMARY KEY (source_id, path))""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def file(self, source_id, path, *, create_parent=False):
        validate_path(source_id, path)
        target = self.files / source_id / path
        # Never follow symlinks in mounted storage, even within the archive.
        for part in (self.files, *target.relative_to(self.files).parents):
            if part == self.files:
                candidate = part
            else:
                candidate = self.files / part
            if candidate.is_symlink():
                raise ValueError("심볼릭 링크는 결과 저장 경로로 사용할 수 없습니다.")
        if target.is_symlink() or not target.resolve().is_relative_to(self.files.resolve()):
            raise ValueError("허용되지 않은 결과 경로입니다.")
        if create_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def record(self, source_id, path, sha256, *, file_stat=None):
        target = self.file(source_id, path)
        stat = target.stat() if file_stat is None else file_stat
        received_at = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute("""INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, path) DO UPDATE SET
                sha256=excluded.sha256, size=excluded.size, mtime_ns=excluded.mtime_ns,
                received_at=excluded.received_at""",
                (source_id, path, sha256, stat.st_size, stat.st_mtime_ns, received_at))
        return {"sha256": sha256, "size": stat.st_size, "received_at": received_at}

    def info(self, source_id, path):
        target = self.file(source_id, path)
        if not target.is_file():
            return None
        with self.connection() as db:
            row = db.execute("SELECT * FROM artifacts WHERE source_id=? AND path=?",
                             (source_id, path)).fetchone()
        stat = target.stat()
        if row is not None and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
            return dict(row)
        # Repairs the index after a crash between atomic rename and index commit.
        with target.open("rb") as stream:
            file_stat = os.fstat(stream.fileno())
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        # A concurrent upload may replace the path while this descriptor still
        # refers to the previous file. Cache only the metadata we actually hashed.
        return {"source_id": source_id, "path": path,
                **self.record(source_id, path, digest, file_stat=file_stat)}

    def entries(self, source_id=None):
        with self.connection() as db:
            if source_id is None:
                rows = db.execute("SELECT * FROM artifacts ORDER BY received_at DESC").fetchall()
            else:
                rows = db.execute("SELECT * FROM artifacts WHERE source_id=? ORDER BY received_at DESC",
                                  (source_id,)).fetchall()
        return [dict(row) for row in rows]

    def read_json(self, source_id, path):
        try:
            value = json.loads(self.file(source_id, path).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, UnicodeError):
            return None
