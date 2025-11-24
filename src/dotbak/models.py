"""Shared models and enums for dotbak."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class EntryType(str, Enum):
    """Kinds of paths managed by dotbak."""

    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class ManagedPath:
    """Identifies a managed entry by group and relative path."""

    group: str
    relative_path: Path

    def key(self) -> tuple[str, str]:
        return (self.group, self.relative_path.as_posix())


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """Recorded metadata about a managed entry."""

    path: ManagedPath
    digest: str
    size: int
    mode: int
    mtime_ns: int
    entry_type: EntryType
    symlink_target: str | None = None
    uid: int | None = None
    gid: int | None = None

    def managed_path(self, root: Path, style: str | None) -> Path:
        group_dir = root / self.path.group
        parts: list[str] = []
        for part in self.path.relative_path.parts:
            if style == "underscore" and part.startswith(".") and len(part) > 1:
                parts.append(f"dot_{part[1:]}")
            else:
                parts.append(part)
        return group_dir / Path(*parts)


class ApplyAction(str, Enum):
    """Outcome of an apply operation for an entry."""

    COPIED = "copied"
    UPDATED = "updated"
    SKIPPED = "skipped"
    CONFLICT_SYSTEM = "system_preferred"
    CONFLICT_MANAGED = "managed_kept"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Result emitted when syncing an entry."""

    path: ManagedPath
    source: Path
    managed: Path
    action: ApplyAction


class RestoreAction(str, Enum):
    """Outcome of a restore operation."""

    RESTORED = "restored"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Result emitted when restoring an entry."""

    path: ManagedPath
    source: Path
    managed: Path
    action: RestoreAction
    details: str | None = None


class ApplyResolution(str, Enum):
    """User preference for resolving an apply conflict."""

    USE_SYSTEM = "use_system"
    USE_MANAGED = "use_managed"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class ApplyConflict:
    """Describe a divergence between managed and source copies."""

    group: str
    entry: Path
    source_path: Path
    managed_path: Path
    manifest_digest: str
    source_digest: str
    managed_digest: str


@dataclass(frozen=True, slots=True)
class PathMetadata:
    """Metadata captured for a path."""

    size: int
    mode: int
    mtime_ns: int
    symlink_target: str | None
    uid: int | None
    gid: int | None


class StatusState(str, Enum):
    """High-level states reported by ``dotbak status``."""

    IN_SYNC = "in_sync"
    NOT_TRACKED = "not_tracked"
    SOURCE_MISMATCH = "source_mismatch"
    MANAGED_MISSING = "managed_missing"
    CONTENT_DIFFER = "content_different"
    ORPHANED = "orphaned"
    METADATA_DIFFER = "metadata_different"


@dataclass(frozen=True, slots=True)
class StatusEntry:
    """Status information for a managed or tracked path."""

    path: ManagedPath
    state: StatusState
    details: str | None = None


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Collection of status results for a manager run."""

    entries: tuple[StatusEntry, ...]
