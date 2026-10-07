"""Create private Docker hub credentials without printing or replacing secrets."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def initialize_hub_env(project_dir=ROOT, *, port=8080, username="team"):
    """Prepare a host-owned data directory and an exclusive, owner-only env file."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("HUB_PORT must be between 1 and 65535.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username):
        raise ValueError("Username must contain 1–64 letters, digits, underscores or hyphens.")
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    if uid == 0:
        raise ValueError("Run as your normal account without sudo so the container stays non-root.")

    project_dir = Path(project_dir)
    env_path = project_dir / ".env.hub"
    if os.path.lexists(env_path):
        raise FileExistsError(".env.hub already exists; existing credentials were not changed.")
    storage_dir = project_dir / "data" / "result-hub"
    storage_dir.mkdir(parents=True, exist_ok=True)
    # Fail before writing credentials if an older Docker invocation created a
    # root-owned bind directory that the chosen container UID cannot write to.
    with tempfile.TemporaryFile(dir=storage_dir):
        pass

    password = secrets.token_urlsafe(32)
    used = {password}
    admin_password = secrets.token_urlsafe(32)
    while admin_password in used:
        admin_password = secrets.token_urlsafe(32)
    used.add(admin_password)
    source_tokens = {}
    for source in ("member1", "member2", "member3", "member4"):
        token = secrets.token_urlsafe(32)
        while token in used:
            token = secrets.token_urlsafe(32)
        used.add(token)
        source_tokens[source] = token
    tokens_json = json.dumps(source_tokens, separators=(",", ":"))
    content = (
        "# Private hub settings. Do not commit, share this file, or source it in a shell.\n"
        f"HUB_PORT={port}\nHUB_UID={uid}\nHUB_GID={gid}\n"
        f"HUB_VIEWER_USERNAME={username}\nHUB_VIEWER_PASSWORD={password}\n"
        f"HUB_ADMIN_PASSWORD={admin_password}\n"
        f"HUB_SOURCE_TOKENS_JSON='{tokens_json}'\n"
    )
    # O_EXCL also rejects symlinks, including dangling links, and protects
    # concurrent invocations from overwriting an already generated secret.
    descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
    except BaseException:
        env_path.unlink(missing_ok=True)
        raise
    return env_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080, help="Local Docker port (default: 8080)")
    parser.add_argument("--username", default="team", help="Read-only browser account (default: team)")
    args = parser.parse_args(argv)
    try:
        initialize_hub_env(ROOT, port=args.port, username=args.username)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Hub setup failed: {error}\n")
    print("Created .env.hub with private credentials and prepared data/result-hub.")
    print("Secrets were not printed. See docs/result_hub.md for startup and team access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
