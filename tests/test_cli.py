from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tomli_w
from typer.testing import CliRunner

from dotbak import cli as cli_module
from dotbak.cli import app
from dotbak.config import DEFAULT_CONFIG_FILENAME
from dotbak.manifest import Manifest
from dotbak.models import ManagedPath, StatusEntry, StatusReport, StatusState

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
    assert "Warning: configuration directory is not inside a Git repository" in doctor_result.stdout


def test_cli_init_interactive(tmp_path: Path, fake_home: Path) -> None:
    config_path = tmp_path / "dotbak.toml"
    managed_dir = tmp_path / "managed"
    manifest = managed_dir / "manifest.toml"

    input_data = "dotfiles\n~/dotfiles\nzsh,wezterm.lua\nN\n"
    result = runner.invoke(
        app,
        [
            "init",
            "--config",
            str(config_path),
            "--managed-root",
            str(managed_dir),
            "--interactive",
        ],
        input=input_data,
    )

    assert result.exit_code == 0
    data = tomllib.loads(config_path.read_text())
    assert data["groups"]["dotfiles"]["entries"] == ["zsh", "wezterm.lua"]
    assert data["groups"]["dotfiles"]["base"] == "~/dotfiles"


def test_cli_init_interactive_conflict(tmp_path: Path, fake_home: Path) -> None:
    config_path = tmp_path / "dotbak.toml"
    result = runner.invoke(
        app,
        [
            "init",
            "--config",
            str(config_path),
            "--interactive",
            "--discover",
            "dot=~/dotfiles",
        ],
    )

    assert result.exit_code == 1
    assert "cannot be combined" in result.stdout


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


def test_cli_status_missing_config(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    result = runner.invoke(app, ["status", "--config", str(missing)])
    assert result.exit_code == 1
    assert "Use 'dotbak init" in result.stdout


def test_cli_apply_handles_dotbak_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyManager:
        def apply(self, *_args, **_kwargs):  # noqa: ANN001
            raise cli_module.DotbakError("Insufficient permissions to modify '/tmp/foo'")

    monkeypatch.setattr(cli_module, "_load_manager", lambda _config: DummyManager())

    result = runner.invoke(app, ["apply"])
    assert result.exit_code == 1
    assert "Tip: try rerunning with `sudo`" in result.stdout


def test_cli_add_adds_entry(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    (base / "existing").write_text("data\n")
    new_path = base / "new" / "config.txt"
    new_path.parent.mkdir(parents=True)
    new_path.write_text("hello\n")

    config_body = f"""
[groups.user]
base = "{base}"
entries = ["existing"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)

    result = runner.invoke(app, ["add", str(new_path), "--config", str(config_path)])
    assert result.exit_code == 0
    data = tomllib.loads(config_path.read_text())
    assert data["groups"]["user"]["entries"] == ["existing", "new/config.txt"]


def test_cli_add_prompts_for_group(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    target_path = base / "shared"
    target_path.mkdir()

    config_body = f"""
[groups.alpha]
base = "{base}"
entries = ["existing_a"]

[groups.beta]
base = "{base}"
entries = ["existing_b"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)

    result = runner.invoke(
        app,
        ["add", str(target_path), "--config", str(config_path)],
        input="2\n",
    )
    assert result.exit_code == 0
    data = tomllib.loads(config_path.read_text())
    assert data["groups"]["beta"]["entries"] == ["existing_b", "shared"]


def test_cli_remove_restores_entry(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    keep_path = base / "keep"
    remove_path = base / "remove"
    keep_path.write_text("keep\n")
    remove_path.write_text("remove\n")

    config_body = f"""
[groups.user]
base = "{base}"
entries = ["keep", "remove"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)

    apply_result = runner.invoke(app, ["apply", "--config", str(config_path)])
    assert apply_result.exit_code == 0

    remove_result = runner.invoke(app, ["remove", str(remove_path), "--config", str(config_path)])
    assert remove_result.exit_code == 0
    assert "Removed" in remove_result.stdout

    data = tomllib.loads(config_path.read_text())
    assert data["groups"]["user"]["entries"] == ["keep"]

    assert remove_path.exists()
    assert remove_path.is_file()
    assert not remove_path.is_symlink()
    assert remove_path.read_text() == "remove\n"

    manifest_obj = Manifest.load(manifest)
    assert manifest_obj.get("user", Path("remove")) is None
    assert manifest_obj.get("user", Path("keep")) is not None


def test_cli_remove_missing_source(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    missing_path = base / "missing"
    missing_path.write_text("data\n")

    config_body = f"""
[groups.user]
base = "{base}"
entries = ["missing"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)
    runner.invoke(app, ["apply", "--config", str(config_path)])

    missing_path.unlink()

    result = runner.invoke(app, ["remove", str(missing_path), "--config", str(config_path)])
    assert result.exit_code == 0
    if config_path.exists():
        data = tomllib.loads(config_path.read_text())
        assert data.get("groups", {}).get("user", {}).get("entries", []) == []
    manifest_obj = Manifest.load(manifest)
    assert manifest_obj.get("user", Path("missing")) is None


def test_cli_orphan_lists(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    track_path = base / "tracked"
    orphan_path = base / "orphan"
    track_path.write_text("tracked\n")
    orphan_path.write_text("orphan\n")

    config_body = f"""
[groups.user]
base = "{base}"
entries = ["tracked", "orphan"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)
    runner.invoke(app, ["apply", "--config", str(config_path)])

    data = tomllib.loads(config_path.read_text())
    data["groups"]["user"]["entries"] = ["tracked"]
    config_path.write_text(tomli_w.dumps(data))

    result = runner.invoke(app, ["orphan", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "orphan" in result.stdout
    assert "Use 'dotbak orphan --prune'" in result.stdout


def test_cli_orphan_prune(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    track_path = base / "tracked"
    orphan_path = base / "orphan"
    track_path.write_text("tracked\n")
    orphan_path.write_text("orphan\n")

    config_body = f"""
[groups.user]
base = "{base}"
entries = ["tracked", "orphan"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)
    runner.invoke(app, ["apply", "--config", str(config_path)])

    data = tomllib.loads(config_path.read_text())
    data["groups"]["user"]["entries"] = ["tracked"]
    config_path.write_text(tomli_w.dumps(data))

    result = runner.invoke(app, ["orphan", "--config", str(config_path), "--prune", "--yes"])
    assert result.exit_code == 0
    assert "Pruned 1 orphaned entries" in result.stdout

    managed_orphan = managed / "user" / "orphan"
    assert not managed_orphan.exists()
    manifest_obj = Manifest.load(manifest)
    assert manifest_obj.get("user", Path("orphan")) is None
    assert manifest_obj.get("user", Path("tracked")) is not None


def test_cli_apply_conflict_choose_system(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    source_file = base / "config.txt"
    source_file.write_text("v1\n")

    config_body = f"""
[groups.user]
base = "{base}"
entries = ["config.txt"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)
    runner.invoke(app, ["apply", "--config", str(config_path)])

    managed_file = managed / "user" / "config.txt"
    managed_file.write_text("managed-change\n")

    source_file.unlink()
    source_file.write_text("system-change\n")

    result = runner.invoke(app, ["apply", "--config", str(config_path)], input="s\n")
    assert result.exit_code == 0
    assert "system_preferred" in result.stdout
    assert managed_file.read_text() == "system-change\n"
    assert source_file.is_symlink()
    assert source_file.resolve() == managed_file


def test_cli_apply_conflict_choose_managed(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    base = project / "base"
    managed = project / "managed"
    manifest = managed / "manifest.toml"
    base.mkdir(parents=True)
    managed.mkdir(parents=True)

    source_file = base / "config.txt"
    source_file.write_text("v1\n")

    config_body = f"""
[groups.user]
base = "{base}"
entries = ["config.txt"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""

    config_path = _write_config(project, config_body)
    runner.invoke(app, ["apply", "--config", str(config_path)])

    managed_file = managed / "user" / "config.txt"
    managed_file.write_text("managed-change\n")

    source_file.unlink()
    source_file.write_text("system-change\n")

    result = runner.invoke(app, ["apply", "--config", str(config_path)], input="m\n")
    assert result.exit_code == 0
    assert "managed_kept" in result.stdout
    assert managed_file.read_text() == "managed-change\n"
    assert source_file.is_symlink()
    assert source_file.resolve() == managed_file


def test_cli_doctor_permission_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyManager:
        def status(self, *_args, **_kwargs):  # noqa: ANN001
            return StatusReport(
                entries=(StatusEntry(path=ManagedPath("grp", Path("entry")), state=StatusState.IN_SYNC),)
            )

        def permission_issues(self, *_args, **_kwargs):  # noqa: ANN001
            return [(ManagedPath("grp", Path("entry")), "Cannot write to /etc")]  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "_load_manager", lambda _config: DummyManager())

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Permission preflight warnings" in result.stdout
    assert "Cannot write to /etc" in result.stdout
    assert "Warning: configuration directory is not inside a Git repository" in result.stdout


def test_build_discovery_missing_path(tmp_path: Path) -> None:
    groups = cli_module._build_discovery(tmp_path, ["grp=./missing"])
    assert groups[0].entries == []


def test_build_discovery_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "file.txt").write_text("hi")
    groups = cli_module._build_discovery(tmp_path, [f"grp=./root"])
    assert groups[0].entries == ["file.txt"]


def test_discover_entries_missing_returns_empty(tmp_path: Path) -> None:
    assert cli_module._discover_entries(tmp_path / "nope") == []


def test_init_refuses_without_force(tmp_path: Path, fake_home: Path) -> None:
    config_path = tmp_path / "dotbak.toml"
    config_path.write_text("existing")
    result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "Use --force" in result.stdout


def test_run_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {}

    class DummyApp:
        def __call__(self):
            called["invoked"] = True

    monkeypatch.setattr(cli_module, "app", DummyApp())
    cli_module.run()
    assert called.get("invoked") is True
