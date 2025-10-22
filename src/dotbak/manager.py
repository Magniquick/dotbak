"""High level orchestration for dotbak operations."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from .config import Config, GroupConfig
from .filesystem import (
    collect_metadata,
    copy_entry,
    detect_entry_type,
    ensure_symlink,
    hash_path,
    symlink_points_to,
)
from .manifest import Manifest
from .models import (
    ApplyAction,
    ApplyResult,
    EntryType,
    ManifestEntry,
    ManagedPath,
    StatusEntry,
    StatusReport,
    StatusState,
)


class DotbakError(RuntimeError):
    """Raised when dotbak encounters an unrecoverable state."""


class DotbakManager:
    """Coordinates apply and status operations using the manifest."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.manifest = Manifest.load(config.settings.manifest_path)
        self.config.settings.managed_root.mkdir(parents=True, exist_ok=True)

    def apply(self, groups: Iterable[str] | None = None) -> list[ApplyResult]:
        selected = self._select_groups(groups)
        results: list[ApplyResult] = []

        for group in selected:
            for entry in group.entries:
                results.append(self._apply_entry(group, entry))

        self.manifest.save()
        return results

    def status(self, groups: Iterable[str] | None = None) -> StatusReport:
        selected = self._select_groups(groups)
        entries: list[StatusEntry] = []
        seen_keys: set[tuple[str, str]] = set()

        for group in selected:
            for entry in group.entries:
                managed_path = ManagedPath(group.name, entry)
                seen_keys.add(managed_path.key())
                entries.append(self._status_for_entry(group, entry))

        for key, manifest_entry in self.manifest.items():
            if key not in seen_keys:
                entries.append(
                    StatusEntry(
                        path=manifest_entry.path,
                        state=StatusState.ORPHANED,
                        details="Entry present in manifest but missing from configuration",
                    )
                )

        entries.sort(key=lambda item: item.path.key())
        return StatusReport(entries=tuple(entries))

    # ------------------------------------------------------------------
    # Internal helpers

    def _select_groups(self, groups: Iterable[str] | None) -> Sequence[GroupConfig]:
        if groups is None:
            return list(self.config.groups.values())

        selected: list[GroupConfig] = []
        for name in groups:
            if name not in self.config.groups:
                raise DotbakError(f"Unknown group '{name}'")
            selected.append(self.config.groups[name])
        return selected

    def _apply_entry(self, group: GroupConfig, entry: Path) -> ApplyResult:
        source = group.source_path(entry)
        if not source.exists() and not source.is_symlink():
            raise DotbakError(f"Source path '{source}' does not exist")

        managed = group.destination_path(self.config.settings.managed_root, entry)
        managed_path = ManagedPath(group.name, entry)

        existing_entry = self.manifest.get(group.name, entry)
        managed_exists = managed.exists() or managed.is_symlink()
        source_points_to_managed = source.is_symlink() and symlink_points_to(source, managed)

        if source_points_to_managed and managed_exists:
            entry_type = detect_entry_type(managed)
            digest = hash_path(managed)
            action = (
                ApplyAction.SKIPPED
                if existing_entry and existing_entry.digest == digest
                else (ApplyAction.UPDATED if existing_entry else ApplyAction.COPIED)
            )
            metadata_path = managed
        else:
            entry_type = detect_entry_type(source)
            digest = hash_path(source)
            metadata_path = source
            need_copy = True

            if existing_entry and managed_exists:
                managed_digest = hash_path(managed)
                if managed_digest == digest == existing_entry.digest:
                    need_copy = False
                    action = ApplyAction.SKIPPED
                else:
                    action = ApplyAction.UPDATED
            else:
                action = ApplyAction.COPIED if existing_entry is None else ApplyAction.UPDATED

            if need_copy:
                entry_type = copy_entry(source, managed)
                digest = hash_path(managed)

            metadata_path = source

        size, mode, mtime_ns, symlink_target = collect_metadata(metadata_path, entry_type=entry_type)
        manifest_entry = ManifestEntry(
            path=managed_path,
            digest=digest,
            size=size,
            mode=mode,
            mtime_ns=mtime_ns,
            entry_type=entry_type,
            symlink_target=symlink_target,
        )
        self.manifest.upsert(manifest_entry)

        ensure_symlink(source, managed)

        return ApplyResult(
            path=managed_path,
            source=source,
            managed=managed,
            action=action,
        )

    def _status_for_entry(self, group: GroupConfig, entry: Path) -> StatusEntry:
        managed_path = ManagedPath(group.name, entry)
        manifest_entry = self.manifest.get(group.name, entry)
        source = group.source_path(entry)
        managed = group.destination_path(self.config.settings.managed_root, entry)

        if manifest_entry is None:
            return StatusEntry(
                path=managed_path,
                state=StatusState.NOT_TRACKED,
                details="Entry has not been applied",
            )

        if not managed.exists() and not managed.is_symlink():
            return StatusEntry(
                path=managed_path,
                state=StatusState.MANAGED_MISSING,
                details="Managed copy is missing",
            )

        managed_digest = hash_path(managed)
        if managed_digest != manifest_entry.digest:
            return StatusEntry(
                path=managed_path,
                state=StatusState.CONTENT_DIFFER,
                details="Managed copy differs from manifest",
            )

        if not source.exists() and not source.is_symlink():
            return StatusEntry(
                path=managed_path,
                state=StatusState.SOURCE_MISMATCH,
                details="Source path is missing",
            )

        if not source.is_symlink():
            return StatusEntry(
                path=managed_path,
                state=StatusState.SOURCE_MISMATCH,
                details="Source is not a symlink",
            )

        if not symlink_points_to(source, managed):
            return StatusEntry(
                path=managed_path,
                state=StatusState.SOURCE_MISMATCH,
                details="Source symlink does not point to managed copy",
            )

        return StatusEntry(
            path=managed_path,
            state=StatusState.IN_SYNC,
        )
