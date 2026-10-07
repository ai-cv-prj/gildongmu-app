"""Administrator credential migration preserves a running team's access."""

import json
import os
import stat

import pytest

from scripts import init_hub_admin


@pytest.fixture(autouse=True)
def normal_user(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)


def existing_env(tmp_path, *, extra="", newline="\n"):
    path = tmp_path / ".env.hub"
    content = (
        "# Existing team settings\n"
        "HUB_VIEWER_USERNAME=team\n"
        "HUB_VIEWER_PASSWORD=existing-viewer-password\n"
        "HUB_SOURCE_TOKENS_JSON='{\"member1\":\"existing-token-1\",\"member2\":\"existing-token-2\"}'\n"
        "HUB_PORT=8088\n" + extra
    ).replace("\n", newline)
    path.write_bytes(content.encode())
    return path, content.encode()


def test_adds_distinct_private_password_preserving_existing_settings(tmp_path, monkeypatch, capsys):
    path, original = existing_env(tmp_path, newline="\r\n")
    candidates = iter(["existing-viewer-password", "existing-token-1", "a" * 43])
    monkeypatch.setattr(init_hub_admin.secrets, "token_urlsafe", lambda size: next(candidates))
    assert init_hub_admin.initialize_hub_admin(tmp_path) is True
    assert path.read_bytes() == original + b"HUB_ADMIN_PASSWORD=" + b"a" * 43 + b"\r\n"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""
    assert sorted(item.name for item in tmp_path.iterdir()) == [".env.hub"]


def test_existing_admin_is_never_regenerated(tmp_path, monkeypatch):
    path, original = existing_env(tmp_path, extra="HUB_ADMIN_PASSWORD='existing-private-admin' # keep\n")
    monkeypatch.setattr(init_hub_admin.secrets, "token_urlsafe", lambda size: pytest.fail("regenerated secret"))
    assert init_hub_admin.initialize_hub_admin(tmp_path) is False
    assert path.read_bytes() == original
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("line", ["HUB_ADMIN_PASSWORD=\n", "HUB_ADMIN_PASSWORD=''\n", "HUB_ADMIN_PASSWORD= # disabled\n"])
def test_empty_admin_setting_is_replaced_once(tmp_path, line):
    path, original = existing_env(tmp_path, extra=line)
    assert init_hub_admin.initialize_hub_admin(tmp_path) is True
    content = path.read_text()
    assert content.count("HUB_ADMIN_PASSWORD=") == 1
    assert len(content.split("HUB_ADMIN_PASSWORD=")[1].strip()) >= 32
    assert content.startswith(original.decode().removesuffix(line))


@pytest.mark.skipif(os.name != "posix", reason="Unix symlink behavior")
@pytest.mark.parametrize("exists", [False, True])
def test_symlinks_are_rejected_without_touching_target(tmp_path, exists):
    target = tmp_path / "target"
    if exists:
        target.write_text("private original")
    (tmp_path / ".env.hub").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        init_hub_admin.initialize_hub_admin(tmp_path)
    assert (tmp_path / ".env.hub").is_symlink()
    if exists:
        assert target.read_text() == "private original"
    else:
        assert not target.exists()
    assert not (tmp_path / ".env.hub.admin.lock").exists()


def test_atomic_replace_failure_preserves_original_and_cleans_temporary_secret(tmp_path, monkeypatch):
    path, original = existing_env(tmp_path)

    def fail_replace(*args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(init_hub_admin.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failure"):
        init_hub_admin.initialize_hub_admin(tmp_path)
    assert path.read_bytes() == original
    assert sorted(item.name for item in tmp_path.iterdir()) == [".env.hub"]


def test_concurrent_edit_is_preserved(tmp_path, monkeypatch):
    path, _ = existing_env(tmp_path)

    def concurrent_edit(size):
        path.write_text("# concurrent edit\n")
        return "new-secret" * 5

    monkeypatch.setattr(init_hub_admin.secrets, "token_urlsafe", concurrent_edit)
    with pytest.raises(ValueError, match="changed during setup"):
        init_hub_admin.initialize_hub_admin(tmp_path)
    assert path.read_text() == "# concurrent edit\n"
    assert sorted(item.name for item in tmp_path.iterdir()) == [".env.hub"]


def test_existing_lock_blocks_second_setup(tmp_path):
    path, original = existing_env(tmp_path)
    lock = tmp_path / ".env.hub.admin.lock"
    lock.write_text("another setup")
    with pytest.raises(FileExistsError):
        init_hub_admin.initialize_hub_admin(tmp_path)
    assert path.read_bytes() == original
    assert lock.read_text() == "another setup"


@pytest.mark.parametrize("extra", [
    "HUB_VIEWER_PASSWORD=duplicate-secret\n",
    "HUB_ADMIN_PASSWORD='unclosed-secret\n",
])
def test_invalid_settings_do_not_change_file_or_leak_secrets(tmp_path, extra, monkeypatch, capsys):
    path, original = existing_env(tmp_path, extra=extra)
    monkeypatch.setattr(init_hub_admin, "ROOT", tmp_path)
    with pytest.raises(SystemExit) as error:
        init_hub_admin.main([])
    assert error.value.code == 1
    assert path.read_bytes() == original
    captured = capsys.readouterr()
    assert "duplicate-secret" not in captured.err
    assert "unclosed-secret" not in captured.err
    assert "existing-token" not in captured.err


def test_cli_does_not_print_old_or_new_credentials(tmp_path, monkeypatch, capsys):
    path, _ = existing_env(tmp_path)
    monkeypatch.setattr(init_hub_admin, "ROOT", tmp_path)
    assert init_hub_admin.main([]) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))
    for value in [values["HUB_VIEWER_PASSWORD"], values["HUB_ADMIN_PASSWORD"],
                  *json.loads(values["HUB_SOURCE_TOKENS_JSON"].strip("'")).values()]:
        assert value not in output
    assert "Added HUB_ADMIN_PASSWORD" in output


def test_missing_file_and_root_do_not_create_credentials(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        init_hub_admin.initialize_hub_admin(tmp_path)
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(ValueError, match="without sudo"):
        init_hub_admin.initialize_hub_admin(tmp_path)
    assert list(tmp_path.iterdir()) == []
