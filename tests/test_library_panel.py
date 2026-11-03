"""Tests for lrc_studio/library_panel.py's LibraryPanel — the persistent
file-list sidebar extracted from app.py's old _build_file_panel.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

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


def test_starts_with_no_real_files(panel):
    assert panel.real_file_paths() == []


def test_add_files_appends_real_paths(panel, tmp_path):
    f1 = tmp_path / "a.mp3"
    f2 = tmp_path / "b.mp3"
    f1.write_bytes(b"")
    f2.write_bytes(b"")

    panel.add_files([str(f1), str(f2)])

    assert set(panel.real_file_paths()) == {str(f1), str(f2)}


def test_add_files_dedupes_already_present_paths(panel, tmp_path):
    f1 = tmp_path / "a.mp3"
    f1.write_bytes(b"")

    panel.add_files([str(f1)])
    panel.add_files([str(f1)])

    assert panel.real_file_paths() == [str(f1)]


def test_add_music_menu_has_two_actions(panel):
    action_texts = [a.text() for a in panel.add_music_button.menu().actions()]
    assert action_texts == ["Add Files", "Find Music"]


def _overflow_action(panel, text):
    return next(a for a in panel.overflow_button.menu().actions() if a.text() == text)


def test_overflow_menu_actions(panel):
    action_texts = [a.text() for a in panel.overflow_button.menu().actions() if a.text()]
    assert action_texts == [
        "Fetch Lyrics for All Missing", "Rescan Library Folder",
        "Standardize Names…", "Convert Lossless Files to Opus…",
    ]


def test_find_music_action_emits_signal(panel):
    received = []
    panel.find_music_requested.connect(lambda: received.append(True))
    panel.add_music_button.menu().actions()[1].trigger()
    assert received == [True]


def test_fetch_missing_lyrics_emits_paths_with_missing_status(panel, tmp_path):
    f1 = tmp_path / "a.mp3"
    f1.write_bytes(b"")
    panel.add_files([str(f1)])   # freshly added file with no .lrc -> COLOR_MISSING

    received = []
    panel.fetch_missing_lyrics_requested.connect(lambda paths: received.append(paths))
    _overflow_action(panel, "Fetch Lyrics for All Missing").trigger()

    assert received == [[str(f1)]]


def test_fetch_missing_lyrics_with_nothing_missing_emits_empty_list(panel):
    received = []
    panel.fetch_missing_lyrics_requested.connect(lambda paths: received.append(paths))
    _overflow_action(panel, "Fetch Lyrics for All Missing").trigger()

    assert received == [[]]


# ---------------------------------------------------------------------------
# Library folder scan, search and status filter
# ---------------------------------------------------------------------------

def _touch(path, text=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text) if text is not None else path.write_bytes(b"")
    return str(path)


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


def test_search_is_accent_insensitive_and_hides_rows(qapp, settings, tmp_path):
    panel = LibraryPanel(settings)
    a = _touch(tmp_path / "Rosalía - Malamente.mp3")
    b = _touch(tmp_path / "Madonna - Like a Virgin.flac")
    panel.add_files([a, b])

    panel.search_input.setText("rosalia")

    visible = [p for p in panel.real_file_paths() if not panel._item_for(p).isHidden()]
    assert visible == [a]


def test_status_filter_and_paths_with_status(qapp, settings, tmp_path):
    panel = LibraryPanel(settings)
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
    visible = [p for p in panel.real_file_paths() if not panel._item_for(p).isHidden()]
    assert visible == [missing]


def test_next_path_skips_filtered_out_rows(qapp, settings, tmp_path):
    panel = LibraryPanel(settings)
    a = _touch(tmp_path / "a.mp3")
    b = _touch(tmp_path / "b.mp3")
    c = _touch(tmp_path / "c.mp3")
    panel.add_files([a, b, c])
    panel._item_for(b).setHidden(True)

    assert panel.next_path(a) == c
    assert panel.next_path(c) is None


def test_replace_path_keeps_the_row_and_emits_rename(qapp, settings, tmp_path):
    panel = LibraryPanel(settings)
    old = _touch(tmp_path / "Song.flac")
    new = _touch(tmp_path / "Song.opus")
    panel.add_files([old])
    renamed = []
    panel.file_renamed.connect(lambda o, n: renamed.append((o, n)))

    panel.replace_path(old, new)

    assert panel.real_file_paths() == [new]
    assert renamed == [(old, new)]
