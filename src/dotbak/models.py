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


class ApplyAction(str, Enum):
    """Outcome of an apply operation for an entry."""

    COPIED = "copied"
    UPDATED = "updated"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Result emitted when syncing an entry."""

    path: ManagedPath
    source: Path
    managed: Path
    action: ApplyAction


class StatusState(str, Enum):
    """High-level states reported by ``dotbak status``."""

    IN_SYNC = "in_sync"
    NOT_TRACKED = "not_tracked"
    SOURCE_MISMATCH = "source_mismatch"
    MANAGED_MISSING = "managed_missing"
    CONTENT_DIFFER = "content_different"
    ORPHANED = "orphaned"


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
