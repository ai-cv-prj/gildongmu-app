#!/usr/bin/env python3
"""추론 프로세스와 별도로 실행하는 공용 결과 서버 전송기.

실행: python scripts/sync_results.py --once
HUB_URL, HUB_SOURCE_ID, HUB_SOURCE_TOKEN은 프로젝트 .env 또는 환경변수로 설정한다.
완료된 세션도 계속 확인하므로 촬영 종료 후 도착하는 구간 영상이 누락되지 않는다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import BinaryIO
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHUNK_SIZE = 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")
CLIP_NAME = re.compile(r"clip_[0-9]{3}\Z")
LOG_NAME = re.compile(r"app\.log(?:\.[0-9]{4}-[0-9]{2}-[0-9]{2})?\Z")
CLIP_STATES = {"pending", "rendering", "ready", "failed", "no_frames"}
LOG = logging.getLogger("result_sync")


class DeferredFile(Exception):
    """작성 중이거나 안전하게 읽을 수 없는 파일은 다음 주기에 다시 확인한다."""


class DeletedAtHub(Exception):
    """관리자가 중앙에서 삭제한 파일은 복원될 때까지 다시 전송하지 않는다."""


@dataclass
class Snapshot:
    file: BinaryIO
    sha256: str
    size: int
    signature: tuple
    document: dict | None = None

    def close(self):
        self.file.close()


@dataclass
class SyncStats:
    uploaded: int = 0
    unchanged: int = 0
    deferred: int = 0
    failed: int = 0
    deleted: int = 0


def file_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def validate_url(value: str, allow_http: bool = False) -> str:
    """토큰은 URL에 넣지 않고, 리다이렉트를 따라 다른 호스트에 전송하지 않는다."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port  # Reject malformed ports before issuing any request.
    except ValueError:
        raise ValueError("HUB_URL 주소가 올바르지 않습니다.") from None
    if (not host or parsed.scheme not in {"https", "http"} or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("HUB_URL에는 http(s) 주소만 입력하세요. 인증정보·쿼리·프래그먼트는 허용하지 않습니다.")
    loopback = host.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme == "http" and not (loopback or allow_http):
        raise ValueError("외부 서버에는 HTTPS를 사용하세요. 신뢰하는 사설망 HTTP는 --allow-http로 허용할 수 있습니다.")
    return value.rstrip("/")


def safe_file(root: Path, path: Path) -> bool:
    """허용한 루트 아래의 실제 일반 파일만 취급한다."""
    try:
        relative = path.relative_to(root)
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            return False
        current = root
        if current.is_symlink():
            return False
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        return stat.S_ISREG(path.stat().st_mode)
    except (ValueError, OSError):
        return False


def open_source(root: Path, path: Path) -> BinaryIO:
    """Linux에서는 각 경로 요소도 no-follow로 열어 심볼릭 링크 교체를 막는다."""
    if not safe_file(root, path):
        raise DeferredFile()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if os.open in os.supports_dir_fd:
        directory = os.open(root, flags | os.O_DIRECTORY)
        try:
            parts = path.relative_to(root).parts
            for part in parts[:-1]:
                next_directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
                os.close(directory)
                directory = next_directory
            descriptor = os.open(parts[-1], flags, dir_fd=directory)
        finally:
            os.close(directory)
    else:
        descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise DeferredFile()
    return os.fdopen(descriptor, "rb")


def snapshot_file(root: Path, path: Path, kind: str) -> Snapshot:
    """디스크 임시파일에 제한된 크기로 복사한다. 원본 파일에 잠금을 걸지 않는다.

    로그/JSONL은 읽기 시작 시점의 마지막 완성된 줄까지만 복사한다.
    JSON/영상은 읽는 동안 변경되면 폐기하며 다음 주기에 다시 시도한다.
    """
    target = tempfile.TemporaryFile(mode="w+b")
    try:
        with open_source(root, path) as source:
            before = os.fstat(source.fileno())
            if kind == "json" and before.st_size > MAX_JSON_BYTES:
                raise DeferredFile()
            remaining = before.st_size
            last_newline = 0
            while remaining:
                chunk = source.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise DeferredFile()
                start = target.tell()
                target.write(chunk)
                if kind == "lines" and b"\n" in chunk:
                    last_newline = start + chunk.rfind(b"\n") + 1
                remaining -= len(chunk)
            after = os.fstat(source.fileno())
            if kind == "lines":
                # Growth is normal; truncation or rewriting of the same length is not.
                if after.st_size < before.st_size or (
                    after.st_size == before.st_size and file_signature(after) != file_signature(before)
                ):
                    raise DeferredFile()
                target.truncate(last_newline)
            elif file_signature(before) != file_signature(after):
                raise DeferredFile()
        size = target.seek(0, os.SEEK_END)
        target.seek(0)
        document = None
        if kind == "json":
            try:
                document = json.loads(target.read(MAX_JSON_BYTES + 1))
            except (ValueError, UnicodeError):
                raise DeferredFile() from None
            if not isinstance(document, dict):
                raise DeferredFile()
            target.seek(0)
        digest = hashlib.sha256()
        while chunk := target.read(CHUNK_SIZE):
            digest.update(chunk)
        target.seek(0)
        return Snapshot(target, digest.hexdigest(), size, file_signature(before), document)
    except BaseException:
        target.close()
        raise


def chunks(file: BinaryIO):
    while chunk := file.read(CHUNK_SIZE):
        yield chunk


class ResultSync:
    def __init__(self, output_dir, hub_url, source_id, token, *, allow_http=False, client=None):
        if not SOURCE_ID.fullmatch(source_id):
            raise ValueError("HUB_SOURCE_ID는 영문·숫자·밑줄·하이픈 1~64자여야 합니다.")
        if not token or any(not 33 <= ord(character) <= 126 for character in token):
            raise ValueError("HUB_SOURCE_TOKEN을 공백 없는 ASCII 문자로 설정하세요.")
        self.output_dir = Path(os.path.abspath(output_dir))
        self.hub_url = validate_url(hub_url, allow_http)
        self.source_id = source_id
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = client or httpx.Client(timeout=httpx.Timeout(120, connect=10), follow_redirects=False)
        self.owns_client = client is None
        self.file_cache = {}
        self.stats = SyncStats()
        self.unavailable = False

    def close(self):
        if self.owns_client:
            self.client.close()

    def _url(self, relative):
        return f"{self.hub_url}/api/ingest/{self.source_id}/{relative}"

    def _matches(self, relative, digest):
        response = self.client.head(self._url(relative), headers=self.headers, follow_redirects=False)
        self._check_deleted(response)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Unexpected HEAD response")
        return response.headers.get("X-Content-SHA256") == digest

    @staticmethod
    def _check_deleted(response):
        if response.status_code == 410 and response.headers.get("X-Hub-Deleted", "").lower() == "true":
            raise DeletedAtHub()

    def _send(self, relative, snapshot):
        if self._matches(relative, snapshot.sha256):
            self.stats.unchanged += 1
            return
        snapshot.file.seek(0)
        response = self.client.put(
            self._url(relative), headers={**self.headers,
                "X-Content-SHA256": snapshot.sha256, "Content-Length": str(snapshot.size),
                "Content-Type": "application/octet-stream"},
            content=chunks(snapshot.file), follow_redirects=False,
        )
        self._check_deleted(response)
        response.raise_for_status()
        receipt = response.json()
        if (receipt.get("ok") is not True or receipt.get("sha256") != snapshot.sha256
                or receipt.get("size") != snapshot.size):
            raise ValueError("Invalid upload receipt")
        self.stats.uploaded += 1

    def _sync(self, path, relative, kind, snapshot=None):
        if self.unavailable:
            return False
        owned = snapshot is None
        try:
            if snapshot is None and safe_file(self.output_dir, path):
                cached = self.file_cache.get(path)
                if cached and cached[0] == file_signature(path.stat()):
                    # Always HEAD again: the hub may have lost or restored its data.
                    if self._matches(relative, cached[1]):
                        self.stats.unchanged += 1
                        return True
            if snapshot is None:
                snapshot = snapshot_file(self.output_dir, path, kind)
            self._send(relative, snapshot)
            self.file_cache[path] = (snapshot.signature, snapshot.sha256)
            return True
        except DeletedAtHub:
            # Keep checking HEAD in later cycles so an administrator's restore
            # immediately permits syncing again. This count is per file.
            self.stats.deleted += 1
            return True
        except (DeferredFile, OSError):
            self.stats.deferred += 1
            return False
        except httpx.TransportError:
            self.stats.failed += 1
            self.unavailable = True
            LOG.warning("공용 서버 연결이 끊겼습니다. 다음 주기에 다시 연결합니다.")
            return False
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            self.stats.failed += 1
            # Do not log exception details or response bodies: either may contain credentials.
            LOG.warning("전송 실패; 다음 주기에 재시도합니다: %s", relative)
            return False
        finally:
            if owned and snapshot is not None:
                snapshot.close()

    def _clip(self, folder, prefix):
        path = folder / "manifest.json"
        try:
            manifest = snapshot_file(self.output_dir, path, "json")
        except (DeferredFile, OSError):
            self.stats.deferred += 1
            return
        try:
            state = manifest.document.get("state")
            if not isinstance(state, str) or state not in CLIP_STATES:
                self.stats.deferred += 1
                return
            originals = [folder / name for name in ("original.webm", "original.mp4")
                         if safe_file(self.output_dir, folder / name)]
            # A manifest can be seen immediately after an atomic video rename.
            if not originals:
                self.stats.deferred += 1
                return
            complete = True
            for video in originals:
                complete = self._sync(video, f"{prefix}/{video.name}", "binary") and complete
            if state == "ready":
                video = folder / "inference.mp4"
                complete = self._sync(video, f"{prefix}/inference.mp4", "binary") and complete
            if complete:
                # Publish exactly the manifest whose state was used above, after its videos.
                self._sync(path, f"{prefix}/manifest.json", "json", snapshot=manifest)
        finally:
            manifest.close()

    def _session(self, folder, snapshot):
        sid = snapshot.document.get("id") or snapshot.document.get("session_id")
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            self.stats.deferred += 1
            return
        prefix = f"sessions/{sid}"
        self._sync(folder / "session.json", f"{prefix}/session.json", "json", snapshot=snapshot)
        for name, kind in (("results.jsonl", "lines"), ("events.jsonl", "lines"), ("provenance.json", "json")):
            path = folder / name
            if safe_file(self.output_dir, path):
                self._sync(path, f"{prefix}/{name}", kind)
        if snapshot.document.get("ended_at"):
            for name in ("camera.mp4", "camera_overlay.mp4"):
                path = folder / name
                if safe_file(self.output_dir, path):
                    self._sync(path, f"{prefix}/{name}", "binary")
        clips = folder / "clips"
        if clips.is_dir() and not clips.is_symlink():
            for clip in sorted(clips.iterdir()):
                if CLIP_NAME.fullmatch(clip.name) and clip.is_dir() and not clip.is_symlink():
                    self._clip(clip, f"{prefix}/clips/{clip.name}")

    def run_once(self):
        self.stats = SyncStats()
        self.unavailable = False
        if self.output_dir.is_symlink():
            self.stats.failed += 1
            return self.stats
        # Do not descend into large frame caches or partially uploaded recordings.
        for directory, directories, filenames in os.walk(self.output_dir, followlinks=False):
            if self.unavailable:
                break
            folder = Path(directory)
            directories[:] = sorted(name for name in directories
                if name not in {"clips", "frames", "chunks", "logs", "tmp", "temp", "temporary"} and not name.startswith(".")
                and not (folder / name).is_symlink())
            if "session.json" not in filenames:
                continue
            snapshot = None
            try:
                snapshot = snapshot_file(self.output_dir, folder / "session.json", "json")
                self._session(folder, snapshot)
            except (DeferredFile, OSError):
                self.stats.deferred += 1
            finally:
                if snapshot is not None:
                    snapshot.close()
        logs = self.output_dir / "logs"
        if logs.is_dir() and not logs.is_symlink():
            for path in sorted(logs.iterdir()):
                if self.unavailable:
                    break
                if LOG_NAME.fullmatch(path.name) and safe_file(self.output_dir, path):
                    self._sync(path, f"logs/{path.name}", "lines")
        return self.stats


def positive_interval(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("주기는 0보다 큰 초 단위 숫자여야 합니다.") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("주기는 0보다 큰 초 단위 숫자여야 합니다.")
    return number


def configured_output_dir():
    with (PROJECT_ROOT / "configs" / "paths.yaml").open(encoding="utf-8") as file:
        settings = yaml.safe_load(file)
    directory = Path(settings.get("session_dir", "test-result"))
    return directory if directory.is_absolute() else PROJECT_ROOT / directory


def main(argv=None):
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="영상 분석 결과와 로그를 공용 서버에 자동 전송합니다.")
    parser.add_argument("--hub-url", default=os.getenv("HUB_URL"), help="공용 결과 서버 주소 (HUB_URL)")
    parser.add_argument("--source-id", default=os.getenv("HUB_SOURCE_ID"), help="팀원별 고유 서버 이름 (HUB_SOURCE_ID)")
    parser.add_argument("--output-dir", type=Path, help="전송할 결과 폴더; 기본값: configs/paths.yaml의 session_dir")
    parser.add_argument("--interval", type=positive_interval, default=15, help="반복 확인 주기(초), 기본 15")
    parser.add_argument("--once", action="store_true", help="한 번만 확인; 전송 실패 시 종료 코드 1")
    parser.add_argument("--allow-http", action="store_true", help="신뢰하는 사설망에서 암호화 없는 HTTP 연결 허용")
    args = parser.parse_args(argv)
    token = os.getenv("HUB_SOURCE_TOKEN")
    if not args.hub_url or not args.source_id or not token:
        parser.error("HUB_URL, HUB_SOURCE_ID, HUB_SOURCE_TOKEN을 환경변수 또는 .env에 설정하세요.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # The HTTP library's INFO request logs are unnecessary for long-running syncing.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        sync = ResultSync(args.output_dir or configured_output_dir(), args.hub_url, args.source_id,
                          token, allow_http=args.allow_http)
    except (ValueError, OSError, yaml.YAMLError) as error:
        parser.error(str(error))
    try:
        while True:
            try:
                result = sync.run_once()
            except OSError:
                LOG.warning("결과 폴더를 읽을 수 없습니다. 다음 주기에 다시 확인합니다.")
                result = SyncStats(failed=1)
            LOG.info("동기화: 전송 %d / 동일 %d / 삭제 건너뜀 %d / 다음 확인 %d / 실패 %d (파일)",
                     result.uploaded, result.unchanged, result.deleted, result.deferred, result.failed)
            if args.once:
                return 1 if result.failed else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sync.close()


if __name__ == "__main__":
    raise SystemExit(main())
