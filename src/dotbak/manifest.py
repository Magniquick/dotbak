"""Manifest persistence for dotbak."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from tomli_w import dump as toml_dump

from .models import EntryType, ManagedPath, ManifestEntry


class Manifest:
    """Tracks metadata about managed entries."""

    def __init__(self, path: Path, entries: dict[tuple[str, str], ManifestEntry] | None = None) -> None:
        self.path = path
        self._entries: dict[tuple[str, str], ManifestEntry] = entries or {}

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.exists():
            return cls(path, {})

        with path.open("rb") as handle:
            data = tomllib.load(handle)

        entries: dict[tuple[str, str], ManifestEntry] = {}
        for item in data.get("entries", []):
            managed_path = ManagedPath(
                group=item["group"],
                relative_path=Path(item["relative_path"]),
            )
            entries[managed_path.key()] = ManifestEntry(
                path=managed_path,
                digest=item["digest"],
                size=item["size"],
                mode=item["mode"],
                mtime_ns=item["mtime_ns"],
                entry_type=EntryType(item["type"]),
                symlink_target=item.get("symlink_target"),
                uid=item.get("uid"),
                gid=item.get("gid"),
            )

        return cls(path, entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [
                self._entry_to_dict(entry) for entry in sorted(self._entries.values(), key=lambda e: e.path.key())
            ]
        }
        with self.path.open("wb") as handle:
            toml_dump(payload, handle)

    def get(self, group: str, relative_path: Path | str) -> ManifestEntry | None:
        key = (group, Path(relative_path).as_posix())
        return self._entries.get(key)

    def upsert(self, entry: ManifestEntry) -> None:
        self._entries[entry.path.key()] = entry

    def remove(self, entry: ManifestEntry | ManagedPath) -> None:
        key = entry.path.key() if isinstance(entry, ManifestEntry) else entry.key()
        self._entries.pop(key, None)

    def entries(self) -> Iterable[ManifestEntry]:
        return self._entries.values()

    def items(self) -> Iterable[tuple[tuple[str, str], ManifestEntry]]:
        return self._entries.items()

    @staticmethod
    def _entry_to_dict(entry: ManifestEntry) -> dict[str, object]:
        payload: dict[str, object] = {
            "group": entry.path.group,
            "relative_path": entry.path.relative_path.as_posix(),
            "digest": entry.digest,
            "size": entry.size,
            "mode": entry.mode,
            "mtime_ns": entry.mtime_ns,
            "type": entry.entry_type.value,
        }
        if entry.symlink_target is not None:
            payload["symlink_target"] = entry.symlink_target
        if entry.uid is not None:
            payload["uid"] = entry.uid
        if entry.gid is not None:
            payload["gid"] = entry.gid
        return payload
