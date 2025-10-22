"""Filesystem helpers for dotbak."""

from __future__ import annotations

import os
import shutil
from hashlib import blake2b
from pathlib import Path

from .models import EntryType


def ensure_parent(path: Path) -> None:
    """Ensure the parent directory exists."""

    path.parent.mkdir(parents=True, exist_ok=True)


def copy_entry(source: Path, destination: Path) -> EntryType:
    """Copy ``source`` into ``destination`` preserving metadata."""

    entry_type = detect_entry_type(source)
    ensure_parent(destination)

    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()

    if entry_type == EntryType.SYMLINK:
        target = os.readlink(source)
        destination.symlink_to(target)
    elif entry_type == EntryType.DIRECTORY:
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            copy_function=shutil.copy2,
            dirs_exist_ok=False,
        )
    else:
        shutil.copy2(source, destination)

    return entry_type


def detect_entry_type(path: Path) -> EntryType:
    """Determine the ``EntryType`` for ``path``."""

    if path.is_symlink():
        return EntryType.SYMLINK
    if path.is_dir():
        return EntryType.DIRECTORY
    return EntryType.FILE


def hash_path(path: Path) -> str:
    """Return a BLAKE2 hash for ``path`` contents and structure."""

    hasher = blake2b(digest_size=32)

    entry_type = detect_entry_type(path)
    hasher.update(entry_type.value.encode())
    if entry_type == EntryType.SYMLINK:
        hasher.update(b"\0")
        hasher.update(os.readlink(path).encode())
        return hasher.hexdigest()

    if entry_type == EntryType.FILE:
        _update_hash_with_file(hasher, path)
        return hasher.hexdigest()

    # directory handling
    for child in sorted(_iter_directory(path)):
        rel = child.relative_to(path).as_posix().encode()
        child_type = detect_entry_type(child)
        hasher.update(child_type.value.encode())
        hasher.update(b"\0")
        hasher.update(rel)
        hasher.update(b"\0")
        if child_type == EntryType.FILE:
            _update_hash_with_file(hasher, child)
        elif child_type == EntryType.SYMLINK:
            hasher.update(os.readlink(child).encode())

    return hasher.hexdigest()


def _update_hash_with_file(hasher, path: Path) -> None:
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)


def _iter_directory(path: Path) -> list[Path]:
    entries: list[Path] = []
    for child in path.iterdir():
        entries.append(child)
        if child.is_dir() and not child.is_symlink():
            entries.extend(_iter_directory(child))
    return sorted(entries)


def collect_metadata(path: Path, *, entry_type: EntryType | None = None) -> tuple[int, int, int, str | None]:
    """Return (size, mode, mtime_ns, symlink_target)."""

    stat_result = path.lstat()
    entry_type = entry_type or detect_entry_type(path)
    mode = stat_result.st_mode & 0o7777
    mtime_ns = stat_result.st_mtime_ns

    if entry_type == EntryType.FILE:
        size = stat_result.st_size
        target = None
    elif entry_type == EntryType.SYMLINK:
        size = 0
        target = os.readlink(path)
    else:
        size = 0
        target = None

    return size, mode, mtime_ns, target


def ensure_symlink(source: Path, target: Path) -> bool:
    """Ensure ``source`` is a symlink to ``target``.

    Returns ``True`` if a change was made.
    """

    if source.exists() or source.is_symlink():
        if source.is_symlink():
            if symlink_points_to(source, target):
                return False
            source.unlink()
        elif source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()

    ensure_parent(source)
    try:
        relative_target = os.path.relpath(target, start=source.parent)
        source.symlink_to(relative_target)
    except ValueError:
        source.symlink_to(target)
    return True


def symlink_points_to(source: Path, target: Path) -> bool:
    """Return ``True`` if ``source`` symlink resolves to ``target``."""

    if not source.is_symlink():
        return False
    current = Path(os.readlink(source))
    current_resolved = (source.parent / current).resolve(strict=False)
    target_resolved = target.resolve(strict=False)
    return current_resolved == target_resolved


def remove_path(path: Path) -> None:
    """Delete ``path`` whether it is a file, directory, or symlink."""

    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)
