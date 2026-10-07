"""Add a private administrator password without replacing existing hub credentials."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SETTING = re.compile(r"^\s*(?:export\s+)?(HUB_[A-Z0-9_]+)\s*=(.*)$")


def _value(raw):
    value = raw.strip()
    if value.startswith("#"):
        return ""
    if value.startswith("'"):
        closing = value.find("'", 1)
        if closing == -1 or value[closing + 1:].strip().split("#", 1)[0]:
            raise ValueError("Invalid quoted setting in .env.hub; no credentials were changed.")
        return value[1:closing]
    if value.startswith('"'):
        try:
            decoded, end = json.JSONDecoder().raw_decode(value)
        except ValueError:
            raise ValueError("Invalid quoted setting in .env.hub; no credentials were changed.") from None
        if not isinstance(decoded, str) or value[end:].strip().split("#", 1)[0]:
            raise ValueError("Invalid quoted setting in .env.hub; no credentials were changed.")
        return decoded
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def initialize_hub_admin(project_dir=ROOT):
    """Return whether a password was added; never print or regenerate a secret."""
    if hasattr(os, "getuid") and os.getuid() == 0:
        raise ValueError("Run as your normal account without sudo.")
    project_dir = Path(project_dir)
    env_path = project_dir / ".env.hub"
    lock_path = project_dir / ".env.hub.admin.lock"
    # Serialize this migration so concurrent runs cannot generate different secrets.
    lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    temporary = None
    try:
        before = env_path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(".env.hub must be a regular file, not a symbolic link.")
        descriptor = os.open(env_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _signature(opened) != _signature(before):
                raise ValueError(".env.hub changed during setup; retry after other edits finish.")
            original = stream.read(128 * 1024 + 1)
            if len(original) > 128 * 1024:
                raise ValueError(".env.hub is unexpectedly large; no credentials were changed.")
            try:
                content = original.decode("utf-8")
            except UnicodeError:
                raise ValueError(".env.hub must use UTF-8; no credentials were changed.") from None
            lines = content.splitlines(keepends=True)
            values = {}
            admin_line = None
            for index, line in enumerate(lines):
                match = SETTING.fullmatch(line.rstrip("\r\n"))
                if match is None or match[1] not in {
                    "HUB_ADMIN_PASSWORD", "HUB_VIEWER_PASSWORD", "HUB_SOURCE_TOKENS_JSON"
                }:
                    continue
                key = match[1]
                if key in values:
                    raise ValueError("Duplicate credential setting in .env.hub; no credentials were changed.")
                values[key] = _value(match[2])
                if key == "HUB_ADMIN_PASSWORD":
                    admin_line = index
            if _signature(os.fstat(stream.fileno())) != _signature(before):
                raise ValueError(".env.hub changed during setup; retry after other edits finish.")
            if values.get("HUB_ADMIN_PASSWORD"):
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), 0o600)
                return False

        try:
            source_tokens = json.loads(values.get("HUB_SOURCE_TOKENS_JSON", ""))
        except ValueError:
            raise ValueError("Valid existing hub credentials are required; run scripts.init_hub_env for a new hub.") from None
        if (not values.get("HUB_VIEWER_PASSWORD") or not isinstance(source_tokens, dict)
                or not source_tokens or any(not isinstance(token, str) or not token for token in source_tokens.values())):
            raise ValueError("Valid existing hub credentials are required; no credentials were changed.")
        used = {values["HUB_VIEWER_PASSWORD"], *source_tokens.values()}
        password = secrets.token_urlsafe(32)
        while password in used:
            password = secrets.token_urlsafe(32)
        newline = "\r\n" if "\r\n" in content else "\n"
        if admin_line is None:
            updated = content + ("" if not content or content.endswith("\n") else newline)
            updated += f"HUB_ADMIN_PASSWORD={password}{newline}"
        else:
            lines[admin_line] = f"HUB_ADMIN_PASSWORD={password}{newline}"
            updated = "".join(lines)

        with tempfile.NamedTemporaryFile(mode="wb", dir=project_dir, prefix=".env.hub.admin-", delete=False) as stream:
            temporary = Path(stream.name)
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            stream.write(updated.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if _signature(env_path.lstat()) != _signature(before):
            raise ValueError(".env.hub changed during setup; retry after other edits finish.")
        os.replace(temporary, env_path)
        temporary = None
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(lock)
        lock_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        added = initialize_hub_admin(ROOT)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Hub administrator setup failed: {error}\n")
    print("Added HUB_ADMIN_PASSWORD to .env.hub." if added else
          "HUB_ADMIN_PASSWORD is already configured; existing credentials were not changed.")
    print("Secrets were not printed. Existing viewer and upload credentials were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
