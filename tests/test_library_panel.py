"""Tests for lrc_studio/library_panel.py's LibraryPanel — the library
sidebar: scan, search, filters with counts, newest-first sorting with a
"new" marker, multi-selection and the right-click menu."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

import library_panel as library_panel_module
import postprocess
from library_panel import LibraryPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def panel(qapp, settings):
    return LibraryPanel(settings)


def _touch(path, text=None, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8") if text is not None else path.write_bytes(b"")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return str(path)


def _visible(panel):
    """Visible song paths, in on-screen order."""
    return [p for p in panel.paths_in_view_order() if not panel._item_for(p).isHidden()]


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------

def test_starts_with_no_real_files(panel):
    assert panel.real_file_paths() == []


def test_add_files_appends_real_paths(panel, tmp_path):
    a, b = _touch(tmp_path / "a.mp3"), _touch(tmp_path / "b.mp3")

    panel.add_files([a, b])

    assert set(panel.real_file_paths()) == {a, b}


def test_add_files_dedupes_already_present_paths(panel, tmp_path):
    a = _touch(tmp_path / "a.mp3")

    panel.add_files([a])
    panel.add_files([a])

    assert panel.real_file_paths() == [a]


def test_load_library_folder_scans_recursively_and_skips_hidden(qapp, settings, tmp_path):
    lib = tmp_path / "Music"
    a = _touch(lib / "Manu Chao - Clandestino.mp3")
    b = _touch(lib / "Album" / "Rosalía - Malamente.opus")
    _touch(lib / ".stfolder" / "ignored.mp3")
    _touch(lib / "cover.jpg")
    settings.setValue("library_dir", str(lib))
    panel = LibraryPanel(settings)

    assert panel.load_library_folder() == 2
    assert set(panel.real_file_paths()) == {a, b}


def test_search_is_accent_insensitive_and_hides_rows(panel, tmp_path):
    a = _touch(tmp_path / "Rosalía - Malamente.mp3")
    b = _touch(tmp_path / "Madonna - Like a Virgin.flac")
    panel.add_files([a, b])

    panel.search_input.setText("rosalia")

    assert _visible(panel) == [a]


def test_status_filter_and_paths_with_status(panel, tmp_path):
    synced = _touch(tmp_path / "A - Synced.mp3")
    _touch(tmp_path / "A - Synced.lrc", "[00:01.00]one\n[00:05.00]two\n")
    plain = _touch(tmp_path / "B - Plain.mp3")
    _touch(tmp_path / "B - Plain.lrc", "just words\nno timing\n")
    missing = _touch(tmp_path / "C - Missing.mp3")
    panel.add_files([synced, plain, missing])

    assert panel.paths_with_status("synced") == [synced]
    assert panel.paths_with_status("plain") == [plain]
    assert panel.paths_with_status("missing") == [missing]

    panel.status_filter.setCurrentIndex(panel.status_filter.findData("missing"))
    assert _visible(panel) == [missing]


def test_filter_combo_shows_live_counts(panel, tmp_path):
    synced = _touch(tmp_path / "A.mp3")
    _touch(tmp_path / "A.lrc", "[00:01.00]one\n")
    missing = _touch(tmp_path / "B.mp3")
    panel.add_files([synced, missing])     # both just arrived: new

    texts = [panel.status_filter.itemText(i) for i in range(panel.status_filter.count())]

    assert texts == ["All songs (2)", "New (2)", "No lyrics (1)", "Not synced (0)"]


def test_next_path_skips_filtered_out_rows(panel, tmp_path):
    a, b, c = (_touch(tmp_path / n) for n in ("a.mp3", "b.mp3", "c.mp3"))
    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData("name"))
    panel.add_files([a, b, c])
    panel._item_for(b).setHidden(True)

    assert panel.next_path(a) == c
    assert panel.next_path(c) is None


def test_replace_path_keeps_the_row_and_emits_rename(panel, tmp_path):
    old = _touch(tmp_path / "Song.flac")
    new = _touch(tmp_path / "Song.opus")
    panel.add_files([old])
    renamed = []
    panel.file_renamed.connect(lambda o, n: renamed.append((o, n)))

    panel.replace_path(old, new)

    assert panel.real_file_paths() == [new]
    assert renamed == [(old, new)]
    assert panel.is_new(new)


# ---------------------------------------------------------------------------
# New songs: newest first, "New" filter, opening clears the marker
# ---------------------------------------------------------------------------

def _library(settings, tmp_path, names_and_mtimes):
    lib = tmp_path / "Music"
    paths = [_touch(lib / name, mtime=mtime) for name, mtime in names_and_mtimes]
    settings.setValue("library_dir", str(lib))
    return lib, paths


def test_first_scan_marks_nothing_new_and_sorts_by_mtime(qapp, settings, tmp_path):
    _lib, (old, recent) = _library(settings, tmp_path, [("Zed - Old.mp3", 1_000), ("Abba - Recent.mp3", 9_000)])
    panel = LibraryPanel(settings)

    panel.load_library_folder()

    assert _visible(panel) == [recent, old]          # newest first by default
    assert not panel.is_new(old) and not panel.is_new(recent)


def test_song_arriving_later_is_new_and_on_top(qapp, settings, tmp_path):
    lib, (old, recent) = _library(settings, tmp_path, [("Zed - Old.mp3", 1_000), ("Abba - Recent.mp3", 9_000)])
    LibraryPanel(settings).load_library_folder()           # first run seeds the index
    arrived = _touch(lib / "Mmm - Downloaded.mp3", mtime=500)

    panel = LibraryPanel(settings)
    panel.load_library_folder()

    assert panel.is_new(arrived)
    assert _visible(panel)[0] == arrived
    panel.status_filter.setCurrentIndex(panel.status_filter.findData("new"))
    assert _visible(panel) == [arrived]


def test_a_to_z_sort(qapp, settings, tmp_path):
    _lib, (old, recent) = _library(settings, tmp_path, [("Zed - Old.mp3", 1_000), ("Abba - Recent.mp3", 9_000)])
    panel = LibraryPanel(settings)
    panel.load_library_folder()

    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData("name"))

    assert _visible(panel) == [recent, old]   # "Abba" before "Zed"
    assert settings.value("library_sort") == "name"


def test_opening_a_song_clears_its_new_marker(panel, tmp_path):
    a = _touch(tmp_path / "a.mp3")
    panel.add_files([a])
    assert panel.is_new(a)

    panel.set_current(a)

    assert not panel.is_new(a)


# ---------------------------------------------------------------------------
# Selection and the right-click menu
# ---------------------------------------------------------------------------

def test_selected_paths_ignore_rows_hidden_by_the_filter(panel, tmp_path):
    a = _touch(tmp_path / "Rosalía - Malamente.mp3")
    b = _touch(tmp_path / "Madonna - Like a Virgin.mp3")
    panel.add_files([a, b])
    panel.search_input.setText("rosalia")

    panel.select_all_visible()

    assert panel.selected_paths() == [a]


def test_menu_for_one_song(panel, tmp_path, monkeypatch):
    a = _touch(tmp_path / "01 track.mp3")
    panel.add_files([a])
    monkeypatch.setattr(postprocess, "tag_based_stem", lambda p: "Manu Chao - Clandestino")

    labels = panel.menu_actions_for([a])

    assert labels == [
        "Open in Editor", "Find Lyrics…", "Rename to “Manu Chao - Clandestino”",
        "Edit Artist && Title…", "Show in Folder", "Mark as Seen", "Move to Trash…",
    ]


def test_menu_for_one_song_without_tags_disables_rename(panel, tmp_path, monkeypatch):
    a = _touch(tmp_path / "track07.mp3")
    panel.add_files([a])
    panel.set_current(a)                       # opened: no longer new
    monkeypatch.setattr(postprocess, "tag_based_stem", lambda p: None)

    menu = panel.build_context_menu([a])
    rename = next(act for act in menu.actions() if act.text().startswith("Rename"))

    assert not rename.isEnabled()
    assert "Mark as Seen" not in panel.menu_actions_for([a])


def test_menu_for_several_songs(panel, tmp_path):
    synced = _touch(tmp_path / "a.mp3")
    _touch(tmp_path / "a.lrc", "[00:01.00]x\n")
    missing = _touch(tmp_path / "b.flac")
    plain = _touch(tmp_path / "c.mp3")
    _touch(tmp_path / "c.lrc", "words\n")
    panel.add_files([synced, missing, plain])

    labels = panel.menu_actions_for([synced, missing, plain])

    assert labels == [
        "Find Lyrics for 2 Songs", "Rename 3 to “Artist - Title”…",
        "Convert 1 to Opus…", "Mark 3 as Seen", "Move 3 to Trash…",
    ]


def test_find_lyrics_for_several_emits_only_songs_without_synced_lyrics(panel, tmp_path):
    synced = _touch(tmp_path / "a.mp3")
    _touch(tmp_path / "a.lrc", "[00:01.00]x\n")
    missing = _touch(tmp_path / "b.mp3")
    panel.add_files([synced, missing])
    received = []
    panel.lyrics_requested.connect(received.append)

    _trigger(panel, [synced, missing], "Find Lyrics for 1 Songs".replace("Songs", "Song"))

    assert received == [[missing]]


def test_rename_one_song_renames_row_file_and_emits(panel, tmp_path, monkeypatch):
    a = _touch(tmp_path / "01 track.mp3")
    _touch(tmp_path / "01 track.lrc", "[00:01.00]x\n")
    panel.add_files([a])
    monkeypatch.setattr(postprocess, "tag_based_stem", lambda p: "Manu Chao - Clandestino")
    renamed = []
    panel.file_renamed.connect(lambda o, n: renamed.append((o, n)))

    _trigger(panel, [a], "Rename to “Manu Chao - Clandestino”")

    new = str(tmp_path / "Manu Chao - Clandestino.mp3")
    assert renamed == [(a, new)]
    assert panel.real_file_paths() == [new]
    assert os.path.exists(str(tmp_path / "Manu Chao - Clandestino.lrc"))


def test_trash_asks_the_window_with_the_visible_selection(panel, tmp_path):
    a = _touch(tmp_path / "Rosalía - Malamente.mp3")
    b = _touch(tmp_path / "Madonna - Like a Virgin.mp3")
    panel.add_files([a, b])
    panel.search_input.setText("rosalia")
    panel.select_all_visible()
    received = []
    panel.trash_requested.connect(received.append)

    panel.trash_selected()

    assert received == [[a]]


def test_remove_paths_drops_rows(panel, tmp_path):
    a, b = _touch(tmp_path / "a.mp3"), _touch(tmp_path / "b.mp3")
    panel.add_files([a, b])

    panel.remove_paths([a])

    assert panel.real_file_paths() == [b]
    assert not panel.is_new(a)


def _trigger(panel, paths, label):
    menu = panel.build_context_menu(paths)
    action = next(act for act in menu.actions() if act.text() == label)
    action.trigger()
