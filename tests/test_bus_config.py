"""Bus credentials work for script and direct server starts without exposing the key."""
import pytest

from backend.bus import config


@pytest.mark.parametrize("value", ["test+key=", "test%2Bkey%3D"])
def test_direct_start_reads_project_env_from_another_directory(tmp_path, monkeypatch, value):
    settings = config.load_bus_config()
    monkeypatch.delenv("SEOUL_BUS_API_KEY", raising=False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(f'\ufeffSEOUL_BUS_API_KEY="{value}"\n', encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("SEOUL_BUS_API_KEY=wrong-directory\n")
    monkeypatch.chdir(elsewhere)

    assert config.BusServiceSettings.from_config(settings).seoul_bus_api_key == value


@pytest.mark.parametrize("value", ["process-key", ""])
def test_explicit_process_key_overrides_env_file(tmp_path, monkeypatch, value):
    settings = config.load_bus_config()
    monkeypatch.setenv("SEOUL_BUS_API_KEY", value)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("SEOUL_BUS_API_KEY=file-key\n")

    assert config.BusServiceSettings.from_config(settings).seoul_bus_api_key == value


@pytest.mark.parametrize("content", [None, "# optional key\n", "SEOUL_BUS_API_KEY\n", 'SEOUL_BUS_API_KEY="  "\n'])
def test_missing_key_keeps_arrivals_unconfigured(tmp_path, monkeypatch, content):
    settings = config.load_bus_config()
    monkeypatch.delenv("SEOUL_BUS_API_KEY", raising=False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    if content is not None:
        (tmp_path / ".env").write_text(content)

    assert config.BusServiceSettings.from_config(settings).seoul_bus_api_key == ""
