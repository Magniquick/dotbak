from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from dotbak.cli import app

runner = CliRunner()


def _write_minimal_config(config_dir: Path, base_path: Path, managed_dir: Path, manifest_path: Path) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "dotbak.toml"
    config_path.write_text(
        f"""
[groups.user]
base = "{base_path}"
entries = ["sample"]

[settings]
managed_root = "{managed_dir}"
manifest_path = "{manifest_path}"
"""
    )
    return config_path


def test_cli_full_cycle(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    sources = project / "sources"
    managed = project / "managed"
    manifest = managed / "manifest.toml"

    sample_dir = sources / "sample"
    sample_dir.mkdir(parents=True)
    (sample_dir / "config.txt").write_text("hello world\n")

    config_path = _write_minimal_config(project, sources, managed, manifest)

    apply_result = runner.invoke(app, ["apply", "--config", str(config_path), "--group", "user"])
    assert apply_result.exit_code == 0
    status_result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert status_result.exit_code == 0
    assert "in_sync" in status_result.stdout

    doctor_result = runner.invoke(app, ["doctor", "--config", str(config_path)])
    assert doctor_result.exit_code == 0


def test_cli_detects_drift_and_restore(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    sources = project / "sources"
    managed = project / "managed"
    manifest = managed / "manifest.toml"

    sample_dir = sources / "sample"
    sample_dir.mkdir(parents=True)
    config_file = sample_dir / "config.txt"
    config_file.write_text("hello world\n")

    config_path = _write_minimal_config(project, sources, managed, manifest)

    runner.invoke(app, ["apply", "--config", str(config_path), "--group", "user"])

    # Introduce drift by replacing the symlinked directory with a real copy containing modified data.
    sample_dir.unlink()
    sample_dir.mkdir()
    config_file = sample_dir / "config.txt"
    config_file.write_text("manual override\n")

    status_result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert "some entries are out of sync" in status_result.stdout.lower()

    restore_result = runner.invoke(app, ["restore", "--config", str(config_path), "--group", "user"])
    assert restore_result.exit_code == 0
    assert config_file.is_file()
    assert config_file.read_text() == "hello world\n"


def test_cli_symlink_integrity(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / "project"
    sources = project / "sources"
    managed = project / "managed"
    manifest = managed / "manifest.toml"

    files_group = sources / "files"
    files_group.mkdir(parents=True)
    sample_file = files_group / "notes.txt"
    sample_file.write_text("dotbak keeps me safe\n")

    nested_dir = sources / "configs"
    subdir = nested_dir / "shell"
    subdir.mkdir(parents=True)
    (subdir / "zshrc").write_text("alias ll='ls -al'\n")

    config_path = project / "dotbak.toml"
    config_path.write_text(
        f"""
[groups.files]
base = "{files_group}"
entries = ["notes.txt"]

[groups.configs]
base = "{nested_dir}"
entries = ["shell"]

[settings]
managed_root = "{managed}"
manifest_path = "{manifest}"
"""
    )

    apply_result = runner.invoke(app, ["apply", "--config", str(config_path)])
    assert apply_result.exit_code == 0

    managed_file = managed / "files" / "notes.txt"
    managed_dir = managed / "configs" / "shell"

    assert sample_file.is_symlink()
    assert sample_file.resolve(strict=True) == managed_file.resolve(strict=True)
    assert subdir.is_symlink()
    assert subdir.resolve(strict=True) == managed_dir.resolve(strict=True)

    assert managed_file.read_text() == "dotbak keeps me safe\n"
    assert (managed_dir / "zshrc").read_text() == "alias ll='ls -al'\n"

    doctor_result = runner.invoke(app, ["doctor", "--config", str(config_path)])
    assert doctor_result.exit_code == 0
