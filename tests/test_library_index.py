"""Tests for library_index.LibraryIndex — when each song joined the library
and whether it is still "new" (not opened since it arrived)."""

import os

from library_index import LibraryIndex


def _touch(path, mtime):
    with open(path, "wb") as fh:
        fh.write(b"x")
    os.utime(path, (mtime, mtime))
    return str(path)


def test_first_sync_seeds_from_mtime_and_marks_nothing_new(tmp_path):
    a = _touch(tmp_path / "a.mp3", 1_000)
    b = _touch(tmp_path / "b.mp3", 2_000)
    index = LibraryIndex(str(tmp_path / "index.json"))
    index.load()

    index.sync([a, b], now=9_999)

    assert index.added_at(a) == 1_000
    assert index.added_at(b) == 2_000
    assert not index.is_new(a)
    assert not index.is_new(b)


def test_later_sync_marks_only_unknown_files_new(tmp_path):
    a = _touch(tmp_path / "a.mp3", 1_000)
    index = LibraryIndex(str(tmp_path / "index.json"))
    index.load()
    index.sync([a], now=5_000)
    c = _touch(tmp_path / "c.mp3", 3_000)

    index.sync([a, c], now=6_000)

    assert index.is_new(c)
    assert index.added_at(c) == 6_000
    assert not index.is_new(a)


def test_sync_forgets_files_that_are_gone(tmp_path):
    a = _touch(tmp_path / "a.mp3", 1_000)
    b = _touch(tmp_path / "b.mp3", 1_000)
    index = LibraryIndex(str(tmp_path / "index.json"))
    index.load()
    index.sync([a, b], now=5_000)

    index.sync([a], now=6_000)

    assert b not in index.known_paths()


def test_add_marks_new_and_keeps_existing_entries(tmp_path):
    a = _touch(tmp_path / "a.mp3", 1_000)
    index = LibraryIndex(str(tmp_path / "index.json"))
    index.load()
    index.sync([a], now=5_000)
    d = _touch(tmp_path / "d.mp3", 1_000)

    index.add([a, d], now=7_000)

    assert index.added_at(a) == 1_000 and not index.is_new(a)
    assert index.added_at(d) == 7_000 and index.is_new(d)


def test_rename_keeps_date_and_new_flag(tmp_path):
    a = _touch(tmp_path / "a.mp3", 1_000)
    index = LibraryIndex(str(tmp_path / "index.json"))
    index.load()
    index.sync([], now=1)            # index exists from now on
    index.add([a], now=4_000)
    new_path = str(tmp_path / "Artist - Title.mp3")

    index.rename(a, new_path)

    assert index.added_at(new_path) == 4_000
    assert index.is_new(new_path)
    assert a not in index.known_paths()


def test_mark_seen_clears_new_flag(tmp_path):
    index = LibraryIndex(str(tmp_path / "index.json"))
    index.load()
    index.sync([], now=1)
    index.add(["/x/song.mp3"], now=2)

    index.mark_seen(["/x/song.mp3"])

    assert not index.is_new("/x/song.mp3")


def test_save_and_load_round_trip(tmp_path):
    path = str(tmp_path / "index.json")
    index = LibraryIndex(path)
    index.load()
    index.sync([], now=1)
    index.add(["/x/song.mp3"], now=42)
    index.save()

    again = LibraryIndex(path)
    again.load()

    assert again.added_at("/x/song.mp3") == 42
    assert again.is_new("/x/song.mp3")


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "index.json"
    path.write_text("{not json", encoding="utf-8")
    index = LibraryIndex(str(path))

    index.load()

    assert index.known_paths() == set()


def test_unknown_path_is_not_new_and_has_zero_date():
    index = LibraryIndex("/nonexistent/index.json")
    assert not index.is_new("/nope.mp3")
    assert index.added_at("/nope.mp3") == 0.0
