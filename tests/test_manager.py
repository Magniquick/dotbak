from __future__ import annotations

import os
from pathlib import Path

import pytest

from dotbak.config import DEFAULT_CONFIG_FILENAME, load_config
from dotbak.manager import DotbakError, DotbakManager
from dotbak.models import ApplyAction, EntryType, ManagedPath, ManifestEntry, RestoreAction, StatusState


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
    assert result.details is None
    assert source_file.read_text() == "return {}\n"
    assert not list(base_dir.glob("wezterm.lua.dotbak-backup*"))


def test_apply_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    target_file = base_dir / "file"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("data\n")

    monkeypatch.setattr("dotbak.manager.os.access", lambda path, mode: False)

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


def test_apply_force_skips_permission_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    target_file = base_dir / "file"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("data\n")

    monkeypatch.setattr("dotbak.manager.os.access", lambda path, mode: False)

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

    manager.apply(force=True)


def test_apply_directory_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    dir_entry = base_dir / "dir"
    dir_entry.mkdir(parents=True)
    blocked_file = dir_entry / "blocked"
    blocked_file.write_text("data\n")

    def fake_access(path, mode):
        if Path(path) == blocked_file:
            return False
        if Path(path) == dir_entry:
            return False
        return True

    monkeypatch.setattr("dotbak.manager.os.access", fake_access)

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["dir"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))

    with pytest.raises(DotbakError):
        manager.apply()

    issues = manager.permission_issues()
    assert issues


def test_select_groups_raises_on_unknown(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    (base_dir / "file").write_text("data\n")

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
        manager.status(["missing"])  # type: ignore[arg-type]


def test_apply_missing_source_raises(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["does-not-exist"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))

    with pytest.raises(DotbakError):
        manager.apply()


def test_apply_manifest_metadata_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    target = base_dir / "file"
    target.write_text("content\n")

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

    manifest_entry = ManifestEntry(
        path=ManagedPath("user", Path("file")),
        digest="abc",
        size=0,
        mode=0o644,
        mtime_ns=0,
        entry_type=EntryType.FILE,
        uid=1000,
        gid=1000,
    )

    def raise_perm(*_args, **_kwargs):
        raise PermissionError()

    monkeypatch.setattr("dotbak.manager.os.lchown", raise_perm)

    with pytest.raises(DotbakError):
        manager._apply_manifest_metadata(target, manifest_entry)


def test_status_content_differs(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "file"
    source_file.write_text("data\n")

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["file"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    manager.apply()

    managed_file = managed_dir / "user" / "file"
    managed_file.write_text("modified\n")

    report = manager.status()
    assert report.entries[0].state is StatusState.CONTENT_DIFFER


def test_status_source_missing(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "file"
    source_file.write_text("data\n")

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["file"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    manager.apply()

    source_file.unlink()

    report = manager.status()
    assert report.entries[0].state is StatusState.SOURCE_MISMATCH
    assert "missing" in (report.entries[0].details or "")


def test_status_symlink_wrong_target(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "file"
    source_file.write_text("data\n")
    wrong_target = base_dir / "other"
    wrong_target.write_text("else\n")

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["file"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    manager.apply()

    source_file.unlink()
    source_file.symlink_to(wrong_target)

    report = manager.status()
    assert report.entries[0].state is StatusState.SOURCE_MISMATCH
    assert "does not point" in (report.entries[0].details or "")


def test_apply_skips_when_contents_match(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "file"
    source_file.write_text("data\n")

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
    manager.apply()

    managed_file = managed_dir / "user" / "file"
    source_file.unlink()
    source_file.write_text(managed_file.read_text())

    result = manager.apply()
    assert result[0].action is ApplyAction.SKIPPED


def test_status_metadata_difference(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "file"
    source_file.write_text("data\n")

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
    manager.apply()

    managed_file = managed_dir / "user" / "file"
    os.chmod(managed_file, 0o700)

    report = manager.status()
    assert report.entries[0].state is StatusState.METADATA_DIFFER


def test_restore_skips_when_managed_missing(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "file"
    source_file.write_text("data\n")

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["file"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    manager.apply()

    managed_file = managed_dir / "user" / "file"
    managed_file.unlink()

    result = manager.restore(force=True)
    assert result[0].action is RestoreAction.SKIPPED
    assert "Managed copy missing" in (result[0].details or "")


def test_restore_directory_atomic(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    live_dir = base_dir / "dir"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "config.conf").write_text("managed\n")

    config_body = f"""
[groups.user]
base = "{base_dir}"
entries = ["dir"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""

    config_path = _write_config(project_dir, config_body)
    manager = DotbakManager(load_config(config_path))

    manager.apply()

    managed_dir_path = managed_dir / "user" / "dir"

    (managed_dir_path / "config.conf").write_text("managed\n")
    live_dir.unlink()
    live_dir.mkdir()
    (live_dir / "config.conf").write_text("local\n")
    (live_dir / "extra").write_text("extra\n")

    result = manager.restore()
    assert result[0].action is RestoreAction.RESTORED
    assert (live_dir / "config.conf").read_text() == "managed\n"
    assert not (live_dir / "extra").exists()
    assert not list(base_dir.glob(".dir.dotbak-backup*"))


def test_restore_file_overwrites_directory(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "script"
    source_file.write_text("echo ok\n")

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["script"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    manager.apply()

    source_file.unlink()
    source_file.mkdir()
    (source_file / "old").write_text("old\n")

    result = manager.restore()
    assert result[0].action is RestoreAction.RESTORED
    assert source_file.read_text() == "echo ok\n"


def test_restore_symlink_entry(tmp_path: Path) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    target = base_dir / "target.txt"
    target.write_text("value\n")
    symlink_path = base_dir / "link.txt"
    symlink_path.symlink_to(target)

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["link.txt"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    manager.apply()

    managed_symlink = managed_dir / "user" / "link.txt"
    assert managed_symlink.is_symlink()
    assert os.readlink(managed_symlink) == os.readlink(symlink_path)

    symlink_path.unlink()
    symlink_path.symlink_to(base_dir / "wrong.txt")

    manager.restore()
    assert symlink_path.is_symlink()
    assert os.readlink(symlink_path) == os.readlink(managed_symlink)


def test_permission_issues_ancestor_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    source_file = base_dir / "file"
    source_file.write_text("data\n")

    def fake_access(path, mode):
        if Path(path) == base_dir:
            return False
        return True

    monkeypatch.setattr("dotbak.manager.os.access", fake_access)

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["file"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    issues = manager.permission_issues()
    assert issues


def test_permission_issues_parent_creation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["newdir/file"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    target = base_dir / "newdir" / "file"

    monkeypatch.setattr("dotbak.manager.os.access", lambda *_args, **_kwargs: True)

    def raise_perm(self, *args, **kwargs):  # type: ignore[override]
        raise PermissionError()

    monkeypatch.setattr(Path, "mkdir", raise_perm, raising=False)

    with pytest.raises(DotbakError):
        manager._ensure_writable(target, create_missing=True)


def test_permission_issues_reports_symlink_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    target = tmp_path / "target"
    target.write_text("secret\n")
    target.chmod(0o444)
    link = base_dir / "link"
    link.symlink_to(target)

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["link"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))
    issues = manager.permission_issues()
    assert issues
    assert "shadowing existing symlink" in issues[0][1]


def test_ensure_writable_symlink_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, base_dir, managed_dir, manifest_path = _setup_config(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("data\n")
    base_dir.mkdir(parents=True, exist_ok=True)
    link = base_dir / "file"
    link.symlink_to(target)

    config_path = _write_config(
        project_dir,
        f"""
[groups.user]
base = "{base_dir}"
entries = ["file"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
""",
    )

    manager = DotbakManager(load_config(config_path))

    def fake_access(path, mode):
        path = Path(path)
        if path in {link, target}:
            return False
        return True

    monkeypatch.setattr("dotbak.manager.os.access", fake_access)

    manager._warnings.clear()
    manager._ensure_writable(base_dir, create_missing=True)

    assert manager._warnings
