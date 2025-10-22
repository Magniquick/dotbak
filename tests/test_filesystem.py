from __future__ import annotations

import os
from pathlib import Path

import pytest

from dotbak.filesystem import (
    EntryType,
    collect_metadata,
    copy_entry,
    ensure_parent,
    ensure_symlink,
    hash_path,
    remove_path,
    symlink_points_to,
)


@pytest.mark.usefixtures("fake_home")
def test_copy_entry_overwrites_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("hello\n")
    destination = tmp_path / "dest.txt"
    destination.write_text("old\n")

    entry_type = copy_entry(source, destination)

    assert entry_type is EntryType.FILE
    assert destination.read_text() == "hello\n"


@pytest.mark.usefixtures("fake_home")
def test_copy_entry_copies_directory(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    (source_dir / "nested").mkdir(parents=True)
    (source_dir / "nested" / "file.txt").write_text("data\n")
    dest_dir = tmp_path / "dst"
    (dest_dir / "old").mkdir(parents=True)
    (dest_dir / "old" / "data.txt").write_text("old\n")

    entry_type = copy_entry(source_dir, dest_dir)

    assert entry_type is EntryType.DIRECTORY
    assert (dest_dir / "nested" / "file.txt").read_text() == "data\n"


@pytest.mark.usefixtures("fake_home")
def test_copy_entry_preserves_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("content\n")
    symlink = tmp_path / "link.txt"
    symlink.symlink_to(target)
    dest = tmp_path / "symlink.txt"

    entry_type = copy_entry(symlink, dest)

    assert entry_type is EntryType.SYMLINK
    assert dest.is_symlink()
    assert os.readlink(dest) == os.readlink(symlink)


@pytest.mark.usefixtures("fake_home")
def test_hash_path_handles_directory(tmp_path: Path) -> None:
    directory = tmp_path / "dir"
    directory.mkdir()
    (directory / "file1.txt").write_text("one\n")
    sub = directory / "sub"
    sub.mkdir()
    (sub / "file2.txt").write_text("two\n")
    (sub / "link.txt").symlink_to(directory / "file1.txt")

    digest_dir = hash_path(directory)
    digest_file = hash_path(directory / "file1.txt")

    assert digest_dir != digest_file


@pytest.mark.usefixtures("fake_home")
def test_hash_path_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("value\n")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    digest = hash_path(link)
    assert isinstance(digest, str)


@pytest.mark.usefixtures("fake_home")
def test_collect_metadata_and_remove(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("abc\n")

    metadata = collect_metadata(file_path)
    assert metadata.size == len("abc\n")

    remove_path(file_path)
    assert not file_path.exists()


@pytest.mark.usefixtures("fake_home")
def test_collect_metadata_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("123\n")
    link = tmp_path / "link"
    link.symlink_to(target)

    metadata = collect_metadata(link)
    assert metadata.symlink_target == os.readlink(link)


@pytest.mark.usefixtures("fake_home")
def test_ensure_symlink_relative(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("value\n")
    link = tmp_path / "link.txt"

    changed = ensure_symlink(link, target)
    assert changed is True
    assert symlink_points_to(link, target)

    changed_second = ensure_symlink(link, target)
    assert changed_second is False


@pytest.mark.usefixtures("fake_home")
def test_ensure_symlink_overwrites_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("val\n")
    link = tmp_path / "link"
    link.mkdir()

    ensure_symlink(link, target)
    assert link.is_symlink()


@pytest.mark.usefixtures("fake_home")
def test_ensure_symlink_value_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_text("val\n")
    link = tmp_path / "link"

    def raise_value_error(*_args, **_kwargs):
        raise ValueError()

    monkeypatch.setattr("dotbak.filesystem.os.path.relpath", raise_value_error)

    ensure_symlink(link, target)
    assert link.is_symlink()


@pytest.mark.usefixtures("fake_home")
def test_ensure_parent(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "file.txt"
    ensure_parent(target)
    assert target.parent.exists()


@pytest.mark.usefixtures("fake_home")
def test_remove_path_directory(tmp_path: Path) -> None:
    directory = tmp_path / "dir"
    (directory / "child").mkdir(parents=True)
    (directory / "child" / "data").write_text("x")

    remove_path(directory)
    assert not directory.exists()


@pytest.mark.usefixtures("fake_home")
def test_remove_path_missing_noop(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    remove_path(missing)
