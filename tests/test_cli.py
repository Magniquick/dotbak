from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dotbak.cli import app
from dotbak.config import DEFAULT_CONFIG_FILENAME
from dotbak.manifest import Manifest

runner = CliRunner()


def _write_config(directory: Path, body: str) -> Path:
    config_path = directory / DEFAULT_CONFIG_FILENAME
    config_path.write_text(body)
    return config_path


def test_cli_apply_and_status_flow(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    (base / "wezterm.lua").write_text("return {}\n")

    config_body = f"""
[paths]
user = "{base}"

[groups.user]
entries = ["wezterm.lua"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)

    apply_result = runner.invoke(app, ["apply", "--config", str(config_path)])
    assert apply_result.exit_code == 0
    assert "copied" in apply_result.stdout

    status_result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert status_result.exit_code == 0
    assert "in_sync" in status_result.stdout


def test_cli_restore_forget(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    (base / "wezterm.lua").write_text("return {}\n")

    config_body = f"""
[paths]
user = "{base}"

[groups.user]
entries = ["wezterm.lua"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)

    apply_result = runner.invoke(app, ["apply", "--config", str(config_path)])
    assert apply_result.exit_code == 0

    restore_result = runner.invoke(app, ["restore", "--config", str(config_path), "--forget"])
    assert restore_result.exit_code == 0
    assert "restored" in restore_result.stdout

    source_file = base / "wezterm.lua"
    assert source_file.exists()
    assert source_file.is_file()
    assert not source_file.is_symlink()

    manifest_obj = Manifest.load(manifest)
    assert list(manifest_obj.entries()) == []


def test_cli_handles_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyManager:
        def apply(self, *_args, **_kwargs):  # noqa: ANN001
            raise PermissionError("mocked")

    monkeypatch.setattr("dotbak.cli._load_manager", lambda _config: DummyManager())

    result = runner.invoke(app, ["apply"])
    assert result.exit_code == 1
    assert "Permission denied" in result.stdout


def test_cli_init_and_doctor(tmp_path: Path, fake_home: Path) -> None:
    config_path = tmp_path / "dotbak.toml"
    result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert result.exit_code == 0
    assert config_path.exists()

    content = config_path.read_text()
    assert "managed_root" in content

    # doctor should fail when run before apply because entries are not tracked.
    doctor_result = runner.invoke(app, ["doctor", "--config", str(config_path)])
    assert doctor_result.exit_code == 1
    assert "not_tracked" in doctor_result.stdout


def test_cli_init_with_discovery_and_bootstrap(tmp_path: Path, fake_home: Path) -> None:
    project_dir = tmp_path / "project"
    config_path = project_dir / "dotbak.toml"
    base_dir = tmp_path / "sources" / ".config"
    base_dir.mkdir(parents=True)
    (base_dir / "wezterm").mkdir()
    (base_dir / "wezterm" / "wezterm.lua").write_text("return {}\n")
    (base_dir / "helix").mkdir()
    (base_dir / "helix" / "config.toml").write_text("theme = 'default'\n")

    discover_arg = f"user_config={base_dir}"
    result = runner.invoke(
        app,
        [
            "init",
            "--config",
            str(config_path),
            "--discover",
            discover_arg,
            "--bootstrap-managed",
        ],
    )

    assert result.exit_code == 0
    data = tomllib.loads(config_path.read_text())

    assert data["paths"]["user_config"] == discover_arg.split("=", 1)[1]
    assert "wezterm" in data["groups"]["user_config"]["entries"]
    assert "helix" in data["groups"]["user_config"]["entries"]
    assert data["settings"]["managed_root"] == "./managed"

    managed_root = (config_path.parent / "managed").resolve()
    assert managed_root.exists()
    assert (managed_root / "user_config").exists()
