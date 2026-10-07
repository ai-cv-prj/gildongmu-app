"""Record a small, explicitly labelled disk snapshot once per server process.

This is evidence about deployed files, not a claim about the exact weights or
Python modules held in memory. Secrets and file contents are never exported.
"""

from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import logging
from pathlib import Path
import platform
import subprocess


ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger(__name__)
CODE_DIRS = ("backend", "src", "frontend", "scripts", "configs")
CODE_SUFFIXES = {".py", ".js", ".css", ".html", ".yaml", ".yml", ".json"}
MODEL_SUFFIXES = {".pt", ".pth", ".ckpt", ".safetensors"}


def _regular(root, path):
    if not path.is_file() or path.is_symlink():
        return False
    return not any((root / parent).is_symlink() for parent in path.relative_to(root).parents)


def _git(root, *args):
    try:
        completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                   text=True, timeout=1, check=False)
        return completed.stdout.strip() if completed.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def collect_snapshot(root):
    root = Path(root)
    paths = []
    for directory in CODE_DIRS:
        base = root / directory
        if base.is_dir() and not base.is_symlink():
            paths.extend(path for path in base.rglob("*")
                         if path.suffix in CODE_SUFFIXES and "__pycache__" not in path.parts and _regular(root, path))
    requirements = root / "requirements.txt"
    if _regular(root, requirements):
        paths.append(requirements)
    files = {}
    skipped = []
    aggregate = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if path.stat().st_size > 2 * 1024 * 1024:
            skipped.append(relative)
            continue
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files[relative] = digest
        aggregate.update(relative.encode() + b"\0" + digest.encode() + b"\n")
    models = []
    weights = root / "weights"
    if weights.is_dir() and not weights.is_symlink():
        for path in sorted(weights.rglob("*")):
            if path.suffix in MODEL_SUFFIXES and _regular(root, path):
                stat = path.stat()
                models.append({"path": path.relative_to(root).as_posix(),
                               "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    status = _git(root, "status", "--porcelain", "--untracked-files=normal")
    return {
        "schema_version": 1,
        "snapshot_basis": "server_process_initialization_disk_snapshot",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "git": {"commit": _git(root, "rev-parse", "HEAD"),
                "dirty": bool(status) if status is not None else None},
        "source": {"sha256": aggregate.hexdigest(), "file_sha256": files, "skipped_large_files": skipped},
        "models": {"basis": "observed_file_metadata", "contents_hashed": False, "files": models},
    }


@lru_cache(maxsize=1)
def process_snapshot():
    try:
        return collect_snapshot(ROOT)
    except Exception:
        log.warning("실행 환경 기록을 만들지 못했습니다. 촬영은 계속합니다.")
        return None


def write_snapshot(folder, snapshot):
    if snapshot is None:
        return
    try:
        (Path(folder) / "provenance.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.warning("세션 실행 환경 기록을 저장하지 못했습니다. 촬영은 계속합니다.")
