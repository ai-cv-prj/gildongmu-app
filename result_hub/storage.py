"""An allowlisted file archive with atomic publication and a small SQLite index."""

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4


CATEGORIES = ("obstacle", "traffic_light", "bus")


class CategoryConflict(Exception):
    """Another teammate changed the classification since it was loaded."""


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
        self.trash = self.root / "trash"
        self.database = self.root / "index.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
                source_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, received_at TEXT NOT NULL,
                PRIMARY KEY (source_id, path))""")
            db.execute("""CREATE TABLE IF NOT EXISTS trash (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, kind TEXT NOT NULL,
                path TEXT NOT NULL, deleted_at TEXT NOT NULL, manifest TEXT NOT NULL,
                state TEXT NOT NULL, UNIQUE(source_id, path))""")
            db.execute("""CREATE TABLE IF NOT EXISTS clip_categories (
                source_id TEXT NOT NULL, session_id TEXT NOT NULL, clip_key TEXT NOT NULL,
                categories TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY (source_id, session_id, clip_key))""")
        self._recover_trash()

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
            # A HEAD hashing an old descriptor can finish after deletion. Acquire
            # the write transaction before checking its tombstone and indexing.
            db.execute("BEGIN IMMEDIATE")
            key = "/".join(path.split("/")[:2]) if path.startswith("sessions/") else path
            if db.execute("SELECT 1 FROM trash WHERE source_id=? AND path=?",
                          (source_id, key)).fetchone() is not None:
                raise FileNotFoundError("휴지통에 있는 결과는 다시 수집할 수 없습니다.")
            db.execute("""INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, path) DO UPDATE SET
                sha256=excluded.sha256, size=excluded.size, mtime_ns=excluded.mtime_ns,
                received_at=excluded.received_at""",
                (source_id, path, sha256, stat.st_size, stat.st_mtime_ns, received_at))
        return {"sha256": sha256, "size": stat.st_size, "received_at": received_at}

    def info(self, source_id, path):
        if self.is_deleted(source_id, path):
            return None
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
                "mtime_ns": file_stat.st_mtime_ns,
                **self.record(source_id, path, digest, file_stat=file_stat)}

    def entries(self, source_id=None):
        with self.connection() as db:
            visible = """SELECT a.* FROM artifacts a WHERE NOT EXISTS (
                SELECT 1 FROM trash t WHERE t.source_id=a.source_id AND
                (a.path=t.path OR substr(a.path, 1, length(t.path)+1)=t.path || '/'))"""
            if source_id is None:
                rows = db.execute(visible + " ORDER BY a.received_at DESC").fetchall()
            else:
                rows = db.execute(visible + " AND a.source_id=? ORDER BY a.received_at DESC",
                                  (source_id,)).fetchall()
        return [dict(row) for row in rows]

    def read_json(self, source_id, path):
        try:
            if self.is_deleted(source_id, path):
                return None
            value = json.loads(self.file(source_id, path).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, UnicodeError):
            return None

    def category_index(self):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM clip_categories").fetchall()
        return {(row["source_id"], row["session_id"], row["clip_key"]): {
            "categories": json.loads(row["categories"]), "category_revision": row["revision"]
        } for row in rows}

    def set_categories(self, source_id, session_id, clip_key, categories, revision):
        # The API publication lock serializes this with uploads/deletion/restoration.
        prefix = f"sessions/{session_id}/"
        validate_path(source_id, prefix + "session.json")
        if self.read_json(source_id, prefix + "session.json") is None:
            raise FileNotFoundError("테스트 기록을 찾을 수 없습니다.")
        if clip_key == "legacy":
            exists = any(self.file(source_id, prefix + name).is_file()
                         for name in ("camera.mp4", "camera_overlay.mp4"))
        elif re.fullmatch(r"clip_[0-9]{3}", clip_key):
            exists = self.read_json(source_id, prefix + f"clips/{clip_key}/manifest.json") is not None
        else:
            raise ValueError("클립 번호가 올바르지 않습니다.")
        if not exists:
            raise FileNotFoundError("영상 클립을 찾을 수 없습니다.")
        if (not isinstance(categories, list) or len(categories) > 3
                or any(not isinstance(value, str) or value not in CATEGORIES for value in categories)
                or type(revision) is not int or revision < 0):
            raise ValueError("영상 분류 값이 올바르지 않습니다.")
        values = [value for value in CATEGORIES if value in categories]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM clip_categories WHERE source_id=? AND session_id=? AND clip_key=?",
                             (source_id, session_id, clip_key)).fetchone()
            if revision != (row["revision"] if row else 0):
                raise CategoryConflict()
            db.execute("""INSERT INTO clip_categories VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id, session_id, clip_key) DO UPDATE SET
                categories=excluded.categories, revision=excluded.revision""",
                       (source_id, session_id, clip_key, json.dumps(values), revision + 1))
        return {"category_key": clip_key, "categories": values, "category_revision": revision + 1}

    def is_deleted(self, source_id, path):
        """A deletion intent is also a tombstone, including during crash recovery."""
        validate_path(source_id, path)
        key = "/".join(path.split("/")[:2]) if path.startswith("sessions/") else path
        with self.connection() as db:
            return db.execute("SELECT 1 FROM trash WHERE source_id=? AND path=?",
                              (source_id, key)).fetchone() is not None

    def _target(self, item):
        if item["kind"] == "session":
            if not re.fullmatch(r"sessions/[0-9a-f]{32}", item["path"]):
                raise ValueError("허용되지 않은 세션 경로입니다.")
            return self.file(item["source_id"], item["path"] + "/session.json").parent
        if item["kind"] != "log" or not re.fullmatch(r"logs/app\.log\.\d{4}-\d{2}-\d{2}", item["path"]):
            raise ValueError("날짜별 서버 로그만 휴지통으로 이동할 수 있습니다.")
        return self.file(item["source_id"], item["path"])

    def _payload(self, trash_id):
        if not isinstance(trash_id, str) or not SESSION_PATTERN.fullmatch(trash_id):
            raise ValueError("휴지통 항목 번호가 올바르지 않습니다.")
        target = self.trash / trash_id / "data"
        for candidate in (self.trash, target.parent, target):
            if candidate.is_symlink():
                raise ValueError("심볼릭 링크는 휴지통 경로로 사용할 수 없습니다.")
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("허용되지 않은 휴지통 경로입니다.")
        return target

    def _tree_files(self, item, target):
        """Check every moved entry: moving a directory must not smuggle in symlinks."""
        if target.is_symlink():
            raise ValueError("심볼릭 링크는 휴지통으로 이동할 수 없습니다.")
        if item["kind"] == "log":
            if not target.exists():
                raise FileNotFoundError("서버 로그를 찾을 수 없습니다.")
            if not stat.S_ISREG(target.stat().st_mode):
                raise ValueError("일반 로그 파일만 이동할 수 있습니다.")
            return [(item["path"], target)]
        if not target.is_dir():
            raise FileNotFoundError("테스트 세션을 찾을 수 없습니다.")
        found = []
        for directory, folders, filenames in os.walk(target, followlinks=False):
            folder = Path(directory)
            for name in folders:
                child = folder / name
                relative = child.relative_to(target).as_posix()
                if child.is_symlink() or not re.fullmatch(r"clips(?:/clip_[0-9]{3})?", relative):
                    raise ValueError("세션에 허용되지 않은 폴더가 있습니다.")
            for name in filenames:
                child = folder / name
                relative = item["path"] + "/" + child.relative_to(target).as_posix()
                validate_path(item["source_id"], relative)
                if child.is_symlink() or not stat.S_ISREG(child.stat().st_mode):
                    raise ValueError("일반 결과 파일만 이동할 수 있습니다.")
                found.append((relative, child))
        if not found:
            raise FileNotFoundError("테스트 결과 파일을 찾을 수 없습니다.")
        return sorted(found)

    @staticmethod
    def _public_trash(item):
        manifest = json.loads(item["manifest"])
        return {key: item[key] for key in ("id", "source_id", "kind", "path", "deleted_at")} | {
            "label": manifest["label"], "file_count": len(manifest["entries"]),
            "size": sum(entry["size"] for entry in manifest["entries"]),
        }

    def trash_entries(self, source_id=None):
        with self.connection() as db:
            if source_id is None:
                rows = db.execute("SELECT * FROM trash ORDER BY deleted_at DESC").fetchall()
            else:
                rows = db.execute("SELECT * FROM trash WHERE source_id=? ORDER BY deleted_at DESC",
                                  (source_id,)).fetchall()
        return [self._public_trash(row) for row in rows]

    def delete_session(self, source_id, session_id):
        if not isinstance(session_id, str) or not SESSION_PATTERN.fullmatch(session_id):
            raise ValueError("세션 번호가 올바르지 않습니다.")
        return self._delete(source_id, "session", f"sessions/{session_id}")

    def delete_log(self, source_id, path):
        if path == "logs/app.log":
            raise ValueError("현재 수집 중인 app.log는 삭제할 수 없습니다. 날짜별 로그를 선택하세요.")
        return self._delete(source_id, "log", path)

    def _delete(self, source_id, kind, path):
        """Caller serializes this with uploads and restores using the publication lock."""
        item = {"id": uuid4().hex, "source_id": source_id, "kind": kind, "path": path,
                "deleted_at": datetime.now(timezone.utc).isoformat(), "state": "deleting"}
        target = self._target(item)
        with self.connection() as db:
            existing = db.execute("SELECT * FROM trash WHERE source_id=? AND path=?",
                                  (source_id, path)).fetchone()
        if existing is not None:
            return self._public_trash(existing)
        files = self._tree_files(item, target)
        entries = [self.info(source_id, relative) for relative, _ in files]
        if any(entry is None for entry in entries):
            raise OSError("삭제 중에 결과 파일이 변경되었습니다. 다시 시도하세요.")
        label = path.rsplit("/", 1)[-1]
        if kind == "session":
            metadata = self.read_json(source_id, path + "/session.json") or {}
            label = metadata.get("note") or metadata.get("device_name") or label
        item["manifest"] = json.dumps({"label": label, "entries": entries}, ensure_ascii=False)
        payload = self._payload(item["id"])
        payload.parent.mkdir(parents=True)
        try:
            with self.connection() as db:
                db.execute("INSERT INTO trash VALUES (?, ?, ?, ?, ?, ?, ?)",
                           tuple(item[key] for key in ("id", "source_id", "kind", "path", "deleted_at", "manifest", "state")))
            # Recheck mounted paths immediately before the atomic directory/file move.
            self._target(item)
            self._payload(item["id"])
            if self._tree_files(item, target) != files:
                raise OSError("삭제 중에 결과 파일 목록이 변경되었습니다. 다시 시도하세요.")
            for entry, (_, file) in zip(entries, files):
                current = file.stat()
                if (current.st_size, current.st_mtime_ns) != (entry["size"], entry["mtime_ns"]):
                    raise OSError("삭제 중에 결과 파일이 변경되었습니다. 다시 시도하세요.")
            target.rename(payload)
            self._finish_delete(item)
        except Exception:
            # A failed index commit must not strand files when rollback is possible.
            if payload.exists() and not target.exists():
                self._target(item)
                payload.rename(target)
            if target.exists() and not payload.exists():
                with self.connection() as db:
                    db.execute("DELETE FROM trash WHERE id=?", (item["id"],))
                payload.parent.rmdir()
            raise
        return self._public_trash(item)

    @staticmethod
    def _remove_index(db, item):
        db.execute("""DELETE FROM artifacts WHERE source_id=? AND
            (path=? OR substr(path, 1, length(?)+1)=? || '/')""",
                   (item["source_id"], item["path"], item["path"], item["path"]))

    def _finish_delete(self, item):
        with self.connection() as db:
            self._remove_index(db, item)
            db.execute("UPDATE trash SET state='deleted' WHERE id=?", (item["id"],))

    def _finish_restore(self, item):
        entries = json.loads(item["manifest"])["entries"]
        with self.connection() as db:
            for entry in entries:
                validate_path(entry["source_id"], entry["path"])
                if entry["source_id"] != item["source_id"] or not (
                    entry["path"] == item["path"] or entry["path"].startswith(item["path"] + "/")
                ):
                    raise ValueError("휴지통의 복원 목록이 올바르지 않습니다.")
                db.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?, ?)",
                           tuple(entry[key] for key in ("source_id", "path", "sha256", "size", "mtime_ns", "received_at")))
            db.execute("DELETE FROM trash WHERE id=?", (item["id"],))

    def restore(self, trash_id):
        payload = self._payload(trash_id)
        with self.connection() as db:
            item = db.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
        if item is None:
            raise FileNotFoundError("휴지통 항목을 찾을 수 없습니다.")
        if item["state"] != "deleted":
            raise OSError("휴지통 작업 복구가 필요합니다. 결과 서버를 다시 시작하세요.")
        target = self._target(item)
        if target.exists():
            raise FileExistsError("같은 경로에 새 결과가 있어 덮어쓰지 않았습니다.")
        files = self._tree_files(item, payload)
        expected = {entry["path"]: entry for entry in json.loads(item["manifest"])["entries"]}
        if {relative for relative, _ in files} != set(expected):
            raise ValueError("휴지통 파일 목록이 변경되어 복원할 수 없습니다.")
        for relative, file in files:
            with file.open("rb") as stream:
                before = os.fstat(stream.fileno())
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                after = os.fstat(stream.fileno())
            if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or digest != expected[relative]["sha256"]):
                raise ValueError("휴지통 파일 내용이 변경되어 복원할 수 없습니다.")
        with self.connection() as db:
            db.execute("UPDATE trash SET state='restoring' WHERE id=?", (trash_id,))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._target(item)
            self._payload(trash_id)
            if target.exists():
                raise FileExistsError("같은 경로에 새 결과가 있어 덮어쓰지 않았습니다.")
            payload.rename(target)
            self._finish_restore(item)
        except Exception:
            if target.exists() and not payload.exists():
                self._target(item)
                target.rename(payload)
            with self.connection() as db:
                db.execute("UPDATE trash SET state='deleted' WHERE id=?", (trash_id,))
            raise
        # The data and database are already restored; empty-directory cleanup is best effort.
        try:
            payload.parent.rmdir()
        except OSError:
            pass
        return self._public_trash(item)

    def _recover_trash(self):
        """Complete or roll back an interrupted rename before serving requests."""
        with self.connection() as db:
            pending = db.execute("SELECT * FROM trash WHERE state != 'deleted'").fetchall()
        for item in pending:
            target, payload = self._target(item), self._payload(item["id"])
            at_source, at_trash = target.exists(), payload.exists()
            if at_source == at_trash:
                raise OSError("중단된 휴지통 작업의 파일 위치를 확인할 수 없습니다.")
            if item["state"] == "deleting":
                if at_trash:
                    self._finish_delete(item)
                else:
                    with self.connection() as db:
                        db.execute("DELETE FROM trash WHERE id=?", (item["id"],))
            elif item["state"] == "restoring":
                if at_source:
                    self._finish_restore(item)
                else:
                    with self.connection() as db:
                        db.execute("UPDATE trash SET state='deleted' WHERE id=?", (item["id"],))
            else:
                raise ValueError("휴지통 작업 상태가 올바르지 않습니다.")
