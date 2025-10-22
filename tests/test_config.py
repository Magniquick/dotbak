from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from dotbak.config import DEFAULT_CONFIG_FILENAME, ConfigError, load_config


def _write_config(tmp_path: Path, body: str) -> Path:
    config_path = tmp_path / DEFAULT_CONFIG_FILENAME
    config_path.write_text(dedent(body))
    return config_path


def test_load_config_happy_path(tmp_path: Path, fake_home: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [paths]
        user_config = "~/dotbak-config"

        [groups.user_config]
        entries = ["zsh", "wezterm.lua"]

        [settings]
        managed_root = "./managed"
        """,
    )

    config = load_config(config_path)

    assert config.config_path == config_path.resolve(strict=False)
    assert config.settings.managed_root == (tmp_path / "managed").resolve(strict=False)
    assert config.settings.manifest_path == config.settings.managed_root / "manifest.toml"

    group = config.group("user_config")
    assert group.base_path == fake_home / "dotbak-config"
    assert group.entries == (Path("zsh"), Path("wezterm.lua"))


def test_load_config_with_override_manifest(tmp_path: Path, fake_home: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [paths]
        user_config = "./relative/base"

        [groups.user_config]
        entries = ["dotfile"]

        [settings]
        managed_root = "~/managed"
        manifest_path = "~/custom/manifest.toml"
        """,
    )

    config = load_config(config_path)

    assert config.settings.managed_root == (fake_home / "managed").resolve(strict=False)
    assert config.settings.manifest_path == (fake_home / "custom/manifest.toml").resolve(strict=False)

    group = config.group("user_config")
    assert group.base_path == (tmp_path / "relative/base").resolve(strict=False)


def test_missing_group_base_path_raises(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [paths]
        user_config = "./config"

        [groups.missing]
        entries = ["file"]
        """,
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_absolute_entry_rejected(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [paths]
        user_config = "./config"

        [groups.user_config]
        entries = ["/etc/passwd"]
        """,
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_directory_argument_resolves_default_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_config(
        config_dir,
        """
        [paths]
        user_config = "./base"

        [groups.user_config]
        entries = ["file"]
        """,
    )

    config = load_config(config_dir)

    assert config.config_path == (config_dir / DEFAULT_CONFIG_FILENAME).resolve(strict=False)
