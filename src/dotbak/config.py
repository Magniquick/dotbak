"""TOML configuration loading for dotbak."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Dict, Mapping

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_FILENAME = "dotbak.toml"


class ConfigError(RuntimeError):
    """Raised when a configuration file cannot be parsed or validated."""


def _expand_path(raw: str | os.PathLike[str] | Path, *, base_dir: Path) -> Path:
    """Return an absolute ``Path`` by expanding env vars and user segments."""

    text = str(raw)
    expanded = Path(os.path.expandvars(text)).expanduser()
    if expanded.is_absolute():
        return expanded.resolve(strict=False)
    return (base_dir / expanded).resolve(strict=False)


class Settings(BaseModel):
    """Global configuration options."""

    model_config = ConfigDict(frozen=True)

    managed_root: Path = Field(default_factory=lambda: Path("./managed"))
    manifest_path: Path

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, base_dir: Path) -> "Settings":
        managed = _expand_path(raw.get("managed_root", Path("./managed")), base_dir=base_dir)
        manifest_raw = raw.get("manifest_path")
        manifest = (
            _expand_path(manifest_raw, base_dir=base_dir) if manifest_raw is not None else managed / "manifest.toml"
        )
        return cls(managed_root=managed, manifest_path=manifest)


class GroupConfig(BaseModel):
    """Configuration for a group of dotfiles rooted at a base path."""

    model_config = ConfigDict(frozen=True)

    name: str
    base_path: Path
    entries: tuple[Path, ...]

    @classmethod
    def from_raw(
        cls,
        name: str,
        base_path: Path,
        raw: Mapping[str, Any],
    ) -> "GroupConfig":
        entries_raw = raw.get("entries")
        if not entries_raw:
            raise ConfigError(f"Group '{name}' must define at least one entry")

        entries: list[Path] = []
        for entry in entries_raw:
            candidate = Path(str(entry))
            if candidate.is_absolute():
                raise ConfigError(f"Group '{name}' entry '{candidate}' must be relative to the base path")
            if ".." in candidate.parts:
                raise ConfigError(f"Group '{name}' entry '{candidate}' must not escape its base path")
            entries.append(candidate)

        return cls(name=name, base_path=base_path, entries=tuple(entries))

    def destination_path(self, managed_root: Path, entry: Path) -> Path:
        """Return the managed directory path for ``entry``."""

        return managed_root / self.name / entry

    def source_path(self, entry: Path) -> Path:
        """Return the source path for ``entry`` under the base path."""

        return self.base_path / entry


class Config(BaseModel):
    """Fully parsed configuration file."""

    model_config = ConfigDict(frozen=True)

    config_path: Path
    settings: Settings
    groups: Dict[str, GroupConfig]

    def group(self, name: str) -> GroupConfig:
        try:
            return self.groups[name]
        except KeyError as exc:  # pragma: no cover - defensive programming
            raise ConfigError(f"Unknown group '{name}'") from exc


def load_config(path: Path | None = None) -> Config:
    """Load and validate a configuration file.

    Args:
        path: Optional path to the TOML file. Defaults to ``dotfile-backup.toml`` in the
            current working directory.
    """

    config_path = _resolve_config_path(path)
    base_dir = config_path.parent

    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    paths_section = data.get("paths") or {}
    base_paths: Dict[str, Path] = {
        name: _expand_path(raw_value, base_dir=base_dir) for name, raw_value in paths_section.items()
    }

    groups_section = data.get("groups")
    if not groups_section:
        raise ConfigError("Configuration must define at least one [groups.<name>] table")

    groups: Dict[str, GroupConfig] = {}
    for group_name, group_body in groups_section.items():
        base_raw = group_body.get("base") or paths_section.get(group_name)
        if base_raw is None:
            raise ConfigError(
                f"Group '{group_name}' must define a 'base' setting or have a matching entry under [paths]"
            )
        base_path = _expand_path(base_raw, base_dir=base_dir)
        groups[group_name] = GroupConfig.from_raw(group_name, base_path, group_body)

    settings = Settings.from_raw(data.get("settings", {}), base_dir=base_dir)

    return Config(config_path=config_path, settings=settings, groups=groups)


def _resolve_config_path(path: Path | None) -> Path:
    if path is None:
        path = Path.cwd() / DEFAULT_CONFIG_FILENAME
    else:
        path = Path(path)

    if not path.exists():
        raise ConfigError(f"Configuration file '{path}' does not exist")
    if path.is_dir():
        candidate = path / DEFAULT_CONFIG_FILENAME
        if not candidate.exists():
            raise ConfigError(f"Expected to find '{DEFAULT_CONFIG_FILENAME}' inside '{path}', but none was located")
        path = candidate

    return path.resolve(strict=False)
