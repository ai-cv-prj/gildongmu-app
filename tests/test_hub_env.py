"""Credential initialization preserves secrets and host bind permissions."""

import json
import os
import stat

import pytest

from scripts import init_hub_env


@pytest.fixture(autouse=True)
def normal_user(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 2345, raising=False)


def read_env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if line and not line.startswith("#"))


def test_private_credentials_are_distinct_and_not_printed(tmp_path, capsys):
    env_path = init_hub_env.initialize_hub_env(tmp_path, port=8088, username="review-team")
    values = read_env(env_path)
    tokens = json.loads(values["HUB_SOURCE_TOKENS_JSON"].strip("'"))
    assert set(tokens) == {"member1", "member2", "member3", "member4"}
    secrets = [values["HUB_VIEWER_PASSWORD"], values["HUB_ADMIN_PASSWORD"], *tokens.values()]
    assert len(set(secrets)) == 6
    assert all(len(value) >= 32 for value in secrets)
    assert values["HUB_UID"] == "1234"
    assert values["HUB_GID"] == "2345"
    assert values["HUB_PORT"] == "8088"
    assert values["HUB_VIEWER_USERNAME"] == "review-team"
    assert (tmp_path / "data" / "result-hub").is_dir()
    if os.name == "posix":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""


def test_existing_credentials_are_never_overwritten(tmp_path):
    env_path = tmp_path / ".env.hub"
    original = "HUB_VIEWER_PASSWORD=already-in-use\n"
    env_path.write_text(original)
    with pytest.raises(FileExistsError):
        init_hub_env.initialize_hub_env(tmp_path)
    assert env_path.read_text() == original
    assert not (tmp_path / "data").exists()


@pytest.mark.skipif(os.name != "posix", reason="Unix symlink behavior")
def test_symlink_is_not_followed_even_when_target_is_missing(tmp_path):
    target = tmp_path / "other-secret"
    env_path = tmp_path / ".env.hub"
    env_path.symlink_to(target)
    with pytest.raises(FileExistsError):
        init_hub_env.initialize_hub_env(tmp_path)
    assert env_path.is_symlink()
    assert not target.exists()


def test_root_is_rejected_before_creating_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(ValueError, match="without sudo"):
        init_hub_env.initialize_hub_env(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_unwritable_storage_does_not_leave_credentials(tmp_path, monkeypatch):
    def cannot_write(**kwargs):
        raise PermissionError("storage is not writable")

    monkeypatch.setattr(init_hub_env.tempfile, "TemporaryFile", cannot_write)
    with pytest.raises(PermissionError):
        init_hub_env.initialize_hub_env(tmp_path)
    assert not (tmp_path / ".env.hub").exists()


@pytest.mark.parametrize("kwargs", [
    {"port": 0}, {"port": 65536}, {"port": True},
    {"username": "team:password"}, {"username": "team\nEVIL=value"}, {"username": ""},
])
def test_invalid_settings_do_not_create_secrets(tmp_path, kwargs):
    with pytest.raises(ValueError):
        init_hub_env.initialize_hub_env(tmp_path, **kwargs)
    assert list(tmp_path.iterdir()) == []


def test_cli_does_not_reveal_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(init_hub_env, "ROOT", tmp_path)
    assert init_hub_env.main([]) == 0
    values = read_env(tmp_path / ".env.hub")
    captured = capsys.readouterr()
    assert "Created .env.hub" in captured.out
    assert values["HUB_VIEWER_PASSWORD"] not in captured.out + captured.err
    assert values["HUB_ADMIN_PASSWORD"] not in captured.out + captured.err
    for token in json.loads(values["HUB_SOURCE_TOKENS_JSON"].strip("'")).values():
        assert token not in captured.out + captured.err
