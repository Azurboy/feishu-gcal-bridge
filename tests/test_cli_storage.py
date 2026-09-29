import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import fgbridge.cli as cli
from fgbridge.storage import (
    AlreadyRunning, atomic_private_write, home, load_config, process_lock, save_config,
)


def test_private_configuration_atomic_and_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("FGBRIDGE_HOME", str(tmp_path / "private"))
    save_config({"installation_id": "synthetic", "mode": "busy"})
    assert load_config()["installation_id"] == "synthetic"
    assert os.stat(home()).st_mode & 0o777 == 0o700
    assert os.stat(home() / "config.toml").st_mode & 0o777 == 0o600
    atomic_private_write(home() / "token.json", b"synthetic")
    assert os.stat(home() / "token.json").st_mode & 0o777 == 0o600


def test_data_directory_cannot_be_inside_source_checkout(monkeypatch):
    source_root = Path(cli.__file__).resolve().parents[2]
    monkeypatch.setenv("FGBRIDGE_HOME", str(source_root / "private-data"))
    with pytest.raises(ValueError, match="outside"):
        home()


def test_process_lock_rejects_concurrent_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("FGBRIDGE_HOME", str(tmp_path / "private"))
    with process_lock():
        with pytest.raises(AlreadyRunning):
            with process_lock():
                pass


def test_launchd_uses_locked_absolute_python_and_120_seconds(monkeypatch, tmp_path):
    monkeypatch.setenv("FGBRIDGE_HOME", str(tmp_path / "private"))
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    save_config({"installation_id": "synthetic"})
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._launch_agent("install") == 0
    path = tmp_path / "Library" / "LaunchAgents" / "dev.feishu-gcal-bridge.sync.plist"
    payload = plistlib.loads(path.read_bytes())
    assert payload["StartInterval"] == 120 and payload["RunAtLoad"]
    assert payload["ProgramArguments"] == [cli.sys.executable, "-m", "fgbridge.cli", "sync"]
    assert calls[-1][:2] == ["launchctl", "bootstrap"]
    assert cli._launch_agent("remove") == 0
    assert not path.exists()


def test_macos_system_proxy_replaces_stale_local_proxy(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")

    def unavailable(*_args, **_kwargs):
        raise OSError()

    monkeypatch.setattr(cli.socket, "create_connection", unavailable)
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="HTTPSEnable : 1\nHTTPSProxy : 127.0.0.1\nHTTPSPort : 17891\n"
        )
    )
    cli._use_macos_system_proxy()
    assert os.environ["https_proxy"] == "http://127.0.0.1:17891"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:17891"
