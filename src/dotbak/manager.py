"""High level orchestration for dotbak operations."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Sequence

from git import InvalidGitRepositoryError, NoSuchPathError, Repo

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
    ApplyConflict,
    ApplyResolution,
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
        self._git_repo = self._load_git_repo()

    def apply(
        self,
        groups: Iterable[str] | None = None,
        *,
        force: bool = False,
        resolver: callable[[ApplyConflict], ApplyResolution] | None = None,
    ) -> list[ApplyResult]:
        selected = self._select_groups(groups)
        results: list[ApplyResult] = []
        self._warnings.clear()

        for group in selected:
            for entry in group.entries:
                source = group.source_path(entry)
                if not force:
                    self._ensure_writable(source, create_missing=True)
                results.append(self._apply_entry(group, entry, resolver=resolver))

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

    def list_orphans(self) -> list[ManifestEntry]:
        active_keys: set[tuple[str, str]] = {
            ManagedPath(group.name, entry).key() for group in self.config.groups.values() for entry in group.entries
        }

        orphans: list[ManifestEntry] = []
        for key, manifest_entry in self.manifest.items():
            if key not in active_keys:
                orphans.append(manifest_entry)
        return orphans

    def prune_orphans(self) -> list[ManifestEntry]:
        orphans = self.list_orphans()
        for entry in orphans:
            managed_path = entry.managed_path(self.config.settings.managed_root, self.config.settings.dot_prefix_style)
            remove_path(managed_path)
            self.manifest.remove(entry)
        if orphans:
            self.manifest.save()
        return orphans

    def remove_entries(
        self,
        entries: Iterable[tuple[GroupConfig, Path]],
        *,
        force: bool = False,
    ) -> list[RestoreResult]:
        results: list[RestoreResult] = []
        for group, entry in entries:
            source = group.source_path(entry)
            if not force:
                self._ensure_writable(source, create_missing=True)
            result = self._restore_entry(group, entry, forget=True)
            if result.action is RestoreAction.SKIPPED:
                managed_path = group.destination_path(
                    self.config.settings.managed_root,
                    entry,
                    dot_prefix_style=self.config.settings.dot_prefix_style,
                )
                remove_path(managed_path)
                self.manifest.remove(ManagedPath(group.name, entry))
            results.append(result)
        if results:
            self.manifest.save()
        return results

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

    @property
    def git_root(self) -> Path | None:
        """Return the detected Git repository root, if any."""

        if self._git_repo is None or self._git_repo.working_tree_dir is None:
            return None
        return Path(self._git_repo.working_tree_dir)

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

    def _apply_entry(
        self,
        group: GroupConfig,
        entry: Path,
        *,
        resolver: Callable[[ApplyConflict], ApplyResolution] | None = None,
    ) -> ApplyResult:
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
                manifest_digest = existing_entry.digest
                if managed_digest == digest == manifest_digest:
                    need_copy = False
                    action = ApplyAction.SKIPPED
                else:
                    managed_changed = managed_digest != manifest_digest
                    source_changed = digest != manifest_digest
                    if managed_changed and source_changed and managed_digest != digest:
                        (
                            action,
                            entry_type,
                            metadata_path,
                            need_copy,
                            digest_override,
                        ) = self._resolve_apply_conflict(
                            group,
                            entry,
                            source,
                            managed,
                            manifest_digest,
                            digest,
                            managed_digest,
                            resolver,
                        )
                        if digest_override is not None:
                            digest = digest_override
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

        git_clean, git_details = self._git_state(managed)
        if git_clean is False:
            return StatusEntry(
                path=managed_path,
                state=StatusState.CONTENT_DIFFER,
                details=git_details or "Managed copy has uncommitted Git changes",
            )

        if git_clean is None:
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

    def _load_git_repo(self) -> Repo | None:
        config_dir = self.config.config_path.parent
        try:
            repo = Repo(config_dir, search_parent_directories=True)
        except (InvalidGitRepositoryError, NoSuchPathError):
            return None

        if repo.working_tree_dir is None:
            return None
        repo.git.update_environment(
            GIT_CONFIG_GLOBAL=os.environ.get("DOTBAK_GIT_CONFIG_GLOBAL", "/dev/null"),
            GIT_CONFIG_SYSTEM=os.environ.get("DOTBAK_GIT_CONFIG_SYSTEM", "/dev/null"),
            GIT_CONFIG_NOSYSTEM=os.environ.get("DOTBAK_GIT_CONFIG_NOSYSTEM", "1"),
        )
        return repo

    def _git_state(self, managed_path: Path) -> tuple[bool | None, str | None]:
        repo = self._git_repo
        if repo is None or repo.working_tree_dir is None:
            return (None, "Git repository not detected")

        resolved = managed_path.resolve(strict=False)
        root_path = Path(repo.working_tree_dir)

        try:
            relative = resolved.relative_to(root_path)
        except ValueError:
            return (None, "Managed copy lies outside the Git repository")

        rel_posix = relative.as_posix()
        try:
            status_output = repo.git.status("--porcelain", "--ignored=no", "--", rel_posix)
        except Exception:
            return (None, "Unable to determine Git status for managed copy")

        lines = [line for line in status_output.splitlines() if line]
        if not lines:
            return (True, None)

        detail = self._summarise_git_output(lines)
        return (False, detail)

    def _summarise_git_output(self, lines: Sequence[str]) -> str:
        if not lines:
            return "Managed copy has uncommitted Git changes"

        categories: set[str] = set()
        for line in lines:
            code = line[:2]
            if code == "??":
                categories.add("untracked items")
            else:
                staged, workspace = code[0], code[1]
                if staged == "A" or workspace == "A":
                    categories.add("added changes")
                if staged == "M" or workspace == "M":
                    categories.add("modified changes")
                if staged == "D" or workspace == "D":
                    categories.add("deleted changes")
                if staged == "R" or workspace == "R":
                    categories.add("renamed entries")

        summary = ", ".join(sorted(categories)) if categories else "uncommitted changes"
        preview_items: list[str] = []
        for entry in lines[:3]:
            preview_items.append(entry[3:])
        preview = ", ".join(preview_items)
        if len(lines) > 3:
            preview = f"{preview}, …" if preview else "…"

        detail = f"Managed copy has {summary} in Git"
        if preview:
            detail += f" [{preview}]"
        return detail

    def _resolve_apply_conflict(
        self,
        group: GroupConfig,
        entry: Path,
        source: Path,
        managed: Path,
        manifest_digest: str,
        source_digest: str,
        managed_digest: str,
        resolver: Callable[[ApplyConflict], ApplyResolution] | None,
    ) -> tuple[ApplyAction, EntryType, Path, bool, str | None]:
        conflict = ApplyConflict(
            group=group.name,
            entry=entry,
            source_path=source,
            managed_path=managed,
            manifest_digest=manifest_digest,
            source_digest=source_digest,
            managed_digest=managed_digest,
        )

        if resolver is None:
            raise DotbakError(
                f"Detected conflict for '{source}'. Run 'dotbak apply' interactively to resolve managed vs system changes."
            )

        resolution = resolver(conflict)

        if resolution is ApplyResolution.ABORT:
            raise DotbakError("Apply aborted by user during conflict resolution.")

        if resolution is ApplyResolution.USE_SYSTEM:
            return (
                ApplyAction.CONFLICT_SYSTEM,
                detect_entry_type(source),
                source,
                True,
                None,
            )

        return (
            ApplyAction.CONFLICT_MANAGED,
            detect_entry_type(managed),
            managed,
            False,
            managed_digest,
        )
