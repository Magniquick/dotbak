from __future__ import annotations

import os
from pathlib import Path

import pytest

from dotbak.config import DEFAULT_CONFIG_FILENAME, load_config
from dotbak.manager import DotbakError, DotbakManager
from dotbak.models import ApplyAction, RestoreAction, StatusState


def _write_config(config_dir: Path, body: str) -> Path:
    config_path = config_dir / DEFAULT_CONFIG_FILENAME
    config_path.write_text(body)
    return config_path


def _setup_config(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    project_dir = tmp_path / "project"
    base_dir = project_dir / "base"
    managed_dir = project_dir / "managed"
    manifest_path = project_dir / "managed" / "manifest.toml"
    base_dir.mkdir(parents=True)
    managed_dir.mkdir(parents=True, exist_ok=True)
    return project_dir, base_dir, managed_dir, manifest_path


def test_apply_initial_and_idempotent(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "wezterm.lua"
    source_file.write_text("return {}\n")

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["wezterm.lua"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    config = load_config(config_path)
    manager = DotbakManager(config)

    results = manager.apply()
    assert [(result.path.group, result.action) for result in results] == [("user", ApplyAction.COPIED)]

    managed_file = managed_dir / "user" / "wezterm.lua"
    assert managed_file.read_text() == "return {}\n"
    assert source_file.is_symlink()
    assert os.path.samefile(source_file, managed_file)

    manifest_entry = manager.manifest.get("user", Path("wezterm.lua"))
    assert manifest_entry is not None

    second_results = manager.apply()
    assert [(result.path.group, result.action) for result in second_results] == [("user", ApplyAction.SKIPPED)]


def test_apply_updates_when_source_changes(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "wezterm.lua"
    source_file.write_text("return {}\n")

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["wezterm.lua"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))
    manager.apply()

    manifest_entry = manager.manifest.get("user", Path("wezterm.lua"))
    assert manifest_entry is not None
    original_digest = manifest_entry.digest

    # Break the symlink and change the file contents.
    source_file.unlink()
    source_file.write_text("return {color = 'blue'}\n")

    results = manager.apply()
    assert results[0].action == ApplyAction.UPDATED

    managed_file = managed_dir / "user" / "wezterm.lua"
    assert managed_file.read_text() == "return {color = 'blue'}\n"
    updated_entry = manager.manifest.get("user", Path("wezterm.lua"))
    assert updated_entry is not None
    assert updated_entry.digest != original_digest


def test_status_reports_sync_and_drift(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "wezterm.lua"
    source_file.write_text("return {}\n")

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["wezterm.lua"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))
    manager.apply()

    report = manager.status()
    assert report.entries[0].state is StatusState.IN_SYNC

    # Break the symlink and replace with a file to simulate drift.
    source_file.unlink()
    source_file.write_text("return {mismatch = true}\n")
    drift_report = manager.status()
    assert drift_report.entries[0].state is StatusState.SOURCE_MISMATCH

    # Restore and resync, then remove the managed copy to trigger MANAGED_MISSING.
    manager.apply()
    managed_file = managed_dir / "user" / "wezterm.lua"
    managed_file.unlink()
    missing_report = manager.status()
    assert missing_report.entries[0].state is StatusState.MANAGED_MISSING


def test_status_marks_orphaned_entries(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "wezterm.lua"
    source_file.write_text("return {}\n")

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["wezterm.lua"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))
    manager.apply()

    # Create a new config that forgets the original group, but reuses the same manifest.
    new_config_dir = project_dir / "alt"
    new_config_dir.mkdir(parents=True)

    new_config_body = f"""
[groups.other]
base = "{base_dir}"
entries = ["dummy"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    new_config_path = _write_config(new_config_dir, new_config_body)
    new_manager = DotbakManager(load_config(new_config_path))
    report = new_manager.status()

    assert any(entry.state is StatusState.ORPHANED for entry in report.entries)


def test_restore_forget_removes_manifest(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "wezterm.lua"
    source_file.write_text("return {}\n")

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["wezterm.lua"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))
    manager.apply()

    results = manager.restore(forget=True)
    assert results[0].action is RestoreAction.RESTORED
    assert not source_file.is_symlink()
    assert manager.manifest.get("user", Path("wezterm.lua")) is None
    assert not (managed_dir / "user" / "wezterm.lua").exists()


def test_restore_backs_up_existing_file(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "wezterm.lua"
    source_file.write_text("return {}\n")

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["wezterm.lua"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))
    manager.apply()

    # Replace symlink with a regular file that will be backed up.
    source_file.unlink()
    source_file.write_text("manual override\n")

    results = manager.restore()
    result = results[0]
    assert result.action is RestoreAction.RESTORED
    assert result.details is not None and ".dotbak-backup" in result.details

    backups = list(base_dir.glob("wezterm.lua.dotbak-backup*"))
    assert backups, "Expected a backup file to be created"


def test_apply_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    target_file = base_dir / "file"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("data\n")

    monkeypatch.setattr("os.access", lambda path, mode: False)

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["file"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))

    with pytest.raises(DotbakError):
        manager.apply()
