"""High level orchestration for dotbak operations."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

from .config import Config, GroupConfig
from .filesystem import (
    collect_metadata,
    copy_entry,
    detect_entry_type,
    ensure_parent,
    ensure_symlink,
    hash_path,
    remove_path,
    symlink_points_to,
)
from .manifest import Manifest
from .models import (
    ApplyAction,
    ApplyResult,
    EntryType,
    ManagedPath,
    ManifestEntry,
    RestoreAction,
    RestoreResult,
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
        self._warnings: list[str] = []

    def apply(self, groups: Iterable[str] | None = None, *, force: bool = False) -> list[ApplyResult]:
        selected = self._select_groups(groups)
        results: list[ApplyResult] = []
        self._warnings.clear()

        for group in selected:
            for entry in group.entries:
                source = group.source_path(entry)
                if not force:
                    self._ensure_writable(source, create_missing=True)
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

    def permission_issues(self, groups: Iterable[str] | None = None) -> list[tuple[ManagedPath, str]]:
        self._warnings.clear()
        issues: list[tuple[ManagedPath, str]] = []
        selected = self._select_groups(groups)

        for group in selected:
            for entry in group.entries:
                managed_path = ManagedPath(group.name, entry)
                source = group.source_path(entry)
                before = len(self._warnings)
                try:
                    self._ensure_writable(source, create_missing=False)
                except DotbakError as exc:
                    issues.append((managed_path, str(exc)))
                    continue
                new_messages = self._warnings[before:]
                if new_messages:
                    issues.extend((managed_path, msg) for msg in new_messages)
                    self._warnings = self._warnings[:before]

        return issues

    def restore(
        self,
        groups: Iterable[str] | None = None,
        *,
        forget: bool = False,
        force: bool = False,
    ) -> list[RestoreResult]:
        selected = self._select_groups(groups)
        results: list[RestoreResult] = []

        for group in selected:
            for entry in group.entries:
                source = group.source_path(entry)
                if not force:
                    self._ensure_writable(source, create_missing=True)
                results.append(self._restore_entry(group, entry, forget=forget))

        if forget:
            self.manifest.save()

        return results

    def pull_warnings(self) -> list[str]:
        messages = list(self._warnings)
        self._warnings.clear()
        return messages

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

        managed = group.destination_path(
            self.config.settings.managed_root,
            entry,
            dot_prefix_style=self.config.settings.dot_prefix_style,
        )
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

        metadata = collect_metadata(metadata_path, entry_type=entry_type)
        manifest_entry = ManifestEntry(
            path=managed_path,
            digest=digest,
            size=metadata.size,
            mode=metadata.mode,
            mtime_ns=metadata.mtime_ns,
            entry_type=entry_type,
            symlink_target=metadata.symlink_target,
            uid=metadata.uid,
            gid=metadata.gid,
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
        managed = group.destination_path(
            self.config.settings.managed_root,
            entry,
            dot_prefix_style=self.config.settings.dot_prefix_style,
        )

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

        metadata_target = managed if manifest_entry.entry_type != EntryType.SYMLINK else source
        current_metadata = collect_metadata(metadata_target, entry_type=manifest_entry.entry_type)
        if (
            current_metadata.mode != manifest_entry.mode
            or current_metadata.uid != manifest_entry.uid
            or current_metadata.gid != manifest_entry.gid
        ):
            return StatusEntry(
                path=managed_path,
                state=StatusState.METADATA_DIFFER,
                details="File metadata differs from manifest",
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

    def _restore_entry(self, group: GroupConfig, entry: Path, *, forget: bool) -> RestoreResult:
        managed_path = ManagedPath(group.name, entry)
        source = group.source_path(entry)
        managed = group.destination_path(
            self.config.settings.managed_root,
            entry,
            dot_prefix_style=self.config.settings.dot_prefix_style,
        )
        manifest_entry = self.manifest.get(group.name, entry)

        if manifest_entry is None:
            return RestoreResult(
                path=managed_path,
                source=source,
                managed=managed,
                action=RestoreAction.SKIPPED,
                details="Entry not tracked in manifest",
            )

        if not managed.exists() and not managed.is_symlink():
            return RestoreResult(
                path=managed_path,
                source=source,
                managed=managed,
                action=RestoreAction.SKIPPED,
                details="Managed copy missing",
            )

        entry_type = detect_entry_type(managed)

        if entry_type == EntryType.FILE:
            self._restore_file_entry(managed, source)
        elif entry_type == EntryType.SYMLINK:
            self._restore_symlink_entry(managed, source)
        else:
            self._restore_directory_entry(managed, source)

        self._apply_manifest_metadata(source, manifest_entry)

        if forget:
            self.manifest.remove(manifest_entry)
            remove_path(managed)

        return RestoreResult(
            path=managed_path,
            source=source,
            managed=managed,
            action=RestoreAction.RESTORED,
            details=None,
        )

    def _restore_file_entry(self, managed: Path, destination: Path) -> None:
        if destination.is_dir():
            remove_path(destination)
        if destination.is_symlink():
            remove_path(destination)

        ensure_parent(destination)
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.dotbak-tmp-", dir=destination.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            shutil.copy2(managed, temp_path)
            os.replace(temp_path, destination)
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise

    def _restore_symlink_entry(self, managed: Path, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            remove_path(destination)
        ensure_parent(destination)
        target = os.readlink(managed)
        destination.symlink_to(target)

    def _restore_directory_entry(self, managed: Path, destination: Path) -> None:
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            remove_path(destination)

        ensure_parent(destination)
        if not destination.exists():
            shutil.copytree(managed, destination, symlinks=True, copy_function=shutil.copy2)
            return

        backup: Path | None = None
        prefix = f".{destination.name}.dotbak-tmp-"
        with tempfile.TemporaryDirectory(prefix=prefix, dir=destination.parent) as staging_root_name:
            staging_root = Path(staging_root_name)
            temp_dir = staging_root / "payload"
            shutil.copytree(managed, temp_dir, symlinks=True, copy_function=shutil.copy2)

            if destination.exists():
                backup = destination.parent / f".{destination.name}.dotbak-backup"
                counter = 1
                while backup.exists():
                    counter += 1
                    backup = destination.parent / f".{destination.name}.dotbak-backup{counter}"

                destination.rename(backup)

            try:
                temp_dir.replace(destination)
            except Exception:
                if backup and backup.exists():
                    backup.replace(destination)
                raise

        if backup and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

    def _apply_manifest_metadata(self, path: Path, manifest_entry: ManifestEntry) -> None:
        uid = manifest_entry.uid
        gid = manifest_entry.gid
        try:
            if uid is not None or gid is not None:
                if hasattr(os, "lchown"):
                    os.lchown(path, uid if uid is not None else -1, gid if gid is not None else -1)  # type: ignore[arg-type]
        except PermissionError:
            raise DotbakError(
                f"Unable to set ownership on '{path}'. Re-run with elevated privileges if ownership matters."
            ) from None

    def _ensure_writable(self, path: Path, *, create_missing: bool) -> None:
        parent = path.parent
        existing_ancestor = parent
        while not existing_ancestor.exists() and existing_ancestor != existing_ancestor.parent:
            existing_ancestor = existing_ancestor.parent

        if not os.access(existing_ancestor, os.W_OK | os.X_OK):
            raise DotbakError(
                f"Cannot write to ancestor directory '{existing_ancestor}' for '{path}'. Run with elevated privileges."
            )

        if not parent.exists():
            if create_missing:
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                except PermissionError as exc:
                    raise DotbakError(
                        f"Cannot create parent directory '{parent}' for '{path}'. Run with elevated privileges."
                    ) from exc
            else:
                return

        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                for dirpath, dirnames, filenames in os.walk(path):
                    dirpath_obj = Path(dirpath)
                    if not os.access(dirpath_obj, os.W_OK | os.X_OK):
                        raise DotbakError(
                            f"Insufficient permissions to modify directory '{dirpath_obj}'. Run with elevated privileges."
                        )
                    for name in filenames:
                        file_path = dirpath_obj / name
                        if os.access(file_path, os.W_OK):
                            continue
                        if file_path.is_symlink():
                            target = Path(os.readlink(file_path))
                            target_abs = (file_path.parent / target).resolve(strict=False)
                            self._warn_symlink_shadow(file_path, target_abs)
                            continue
                        raise DotbakError(
                            f"Insufficient permissions to modify file '{file_path}'. Run with elevated privileges."
                        )
            elif not os.access(path, os.W_OK):
                if path.is_symlink():
                    target = Path(os.readlink(path))
                    target_abs = (path.parent / target).resolve(strict=False)
                    self._warn_symlink_shadow(path, target_abs)
                    return
                raise DotbakError(f"Insufficient permissions to modify '{path}'. Run with elevated privileges.")
        else:
            if not os.access(parent, os.W_OK | os.X_OK):
                raise DotbakError(
                    f"Cannot write to parent directory '{parent}' for '{path}'. Run with elevated privileges."
                )

    def _warn_symlink_shadow(self, path: Path, target: Path) -> None:
        self._warnings.append(f"shadowing existing symlink '{path}' pointing to '{target}'. dotbak will manage a copy.")
