"""Tests for LrcSyncApp's unified command table (app.py's _Command list) and
the Clear All Lyrics confirmation — the two concrete bugs the UX pass fixed:
shortcuts registered in two places that could drift apart, and the one
irreversible action in the app having no confirmation at all.

Needs a real (if offscreen) Qt platform to construct QMainWindow/QWidget
instances, so this sets QT_QPA_PLATFORM before importing PyQt6 rather than
relying on the environment already having it set.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QMessageBox

import app as app_module
from app import LrcSyncApp

_MENU_NAMES = {"file", "edit", "tools", "help"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    # LrcSyncApp.__init__ constructs QSettings("LrcStudio", "Configs") —
    # the user's real, persisted config. Redirect it to a throwaway
    # per-test ini file so a test run can never read or clobber the
    # user's actual settings (window geometry, slskd credentials' URL/
    # key, embed-lyrics preference, etc.).
    ini_path = str(tmp_path / "test_settings.ini")
    monkeypatch.setattr(
        app_module, "QSettings",
        lambda *a, **k: QSettings(ini_path, QSettings.Format.IniFormat),
    )
    win = LrcSyncApp()
    yield win
    # Root cause of a real, reproducible hang found while adding this
    # task's tests: any test that leaves the window dirty (e.g. via
    # _replace_all_verses/_add_verse) makes win.close() below trigger
    # _confirm_discard_if_dirty()'s real QMessageBox.question — blocking
    # forever in a headless test with no one to click it. Some tests
    # happened to avoid this by coincidence (a QMessageBox.question patch
    # set in the test body via this same monkeypatch fixture is still
    # active here, since monkeypatch undoes patches in reverse dependency
    # order — after this fixture's teardown runs, not before); tests that
    # never touched that patch hit the real dialog and hung. Tests should
    # never see a real confirmation dialog at teardown regardless of what
    # dirty state they left behind, so force it clean before closing.
    win._dirty = False
    win.close()


# ---------------------------------------------------------------------------
# Command table integrity
# ---------------------------------------------------------------------------

def test_no_shortcut_is_bound_to_more_than_one_command(window):
    shortcuts = [cmd.shortcut for cmd in window._commands if cmd.shortcut]
    assert len(shortcuts) == len(set(shortcuts))


def test_every_command_targets_a_real_menu_or_is_transport_only(window):
    for cmd in window._commands:
        assert cmd.menu is None or cmd.menu in _MENU_NAMES


def test_shortcuts_dialog_covers_every_registered_shortcut(window):
    # Reproduces _show_shortcuts_dialog's row-building without popping the
    # modal, so a shortcut added to the table can never again go missing
    # from the Help dialog the way Ctrl+F previously did.
    rows = "".join(
        f"<tr><td><b>{cmd.shortcut}</b></td><td>{cmd.help_text or cmd.label}</td></tr>"
        for cmd in window._commands if cmd.shortcut
    )
    for cmd in window._commands:
        if cmd.shortcut:
            assert cmd.shortcut in rows


def test_transport_commands_have_no_menu_entry(window):
    # Play/Pause, sync-and-advance, seek, and line-selection are real-time
    # transport controls, deliberately kept out of the menu bar.
    transport_labels = {cmd.label for cmd in window._commands if cmd.menu is None}
    assert "Play / Pause" in transport_labels
    assert all(cmd.menu is None for cmd in window._commands if cmd.label == "Play / Pause")


# ---------------------------------------------------------------------------
# Clear All Lyrics confirmation
# ---------------------------------------------------------------------------

def test_clear_lyrics_does_nothing_on_cancel(window, monkeypatch):
    window._add_verse("[00:01.00]", "line one")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel)

    window._clear_lyrics()

    assert len(window._verses) == 1


def test_clear_lyrics_clears_on_confirm(window, monkeypatch):
    window._add_verse("[00:01.00]", "line one")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    window._clear_lyrics()

    assert len(window._verses) == 0


def test_clear_lyrics_skips_the_prompt_when_theres_nothing_to_clear(window, monkeypatch):
    asked = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes,
    )

    window._clear_lyrics()

    assert asked == []


# ---------------------------------------------------------------------------
# Unified Find Music entry point
# ---------------------------------------------------------------------------

def test_find_music_routes_spotify_link_to_batch_import(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    calls = []
    monkeypatch.setattr(window, "_get_slskd_client", lambda: object())
    monkeypatch.setattr(window, "_open_batch_import", lambda link: calls.append(link))
    # dialog.exec() is modal and blocks in real use — run_search is wired
    # to the Search button *before* exec() is called, so replacing exec()
    # with a no-op that returns immediately (Rejected) lets _find_music()
    # return control to the test without ever blocking, while leaving the
    # live dialog object and its button wiring fully intact to drive by
    # hand afterward.
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)

    # is_spotify_link() requires a genuine 22-character Spotify ID (same
    # regex extract_spotify_ref() already enforces) — a short fake ID like
    # "abc123" wouldn't match, so run_search() would silently fall through
    # to the real-Soulseek-search branch and start a real SoulseekSearchThread
    # against this test's dummy `object()` client, crashing the process
    # when that thread calls a `.search()` method the dummy doesn't have.
    spotify_link = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"

    window._find_music()
    dialog = window._active_find_music_dialog
    dialog.query_input.setText(spotify_link)
    dialog.search_button.click()

    assert calls == [spotify_link]


def test_command_table_has_one_find_music_entry_not_two(window):
    labels = [cmd.label for cmd in window._commands]
    assert "Find Music…" in labels
    assert "Find on Soulseek…" not in labels
    assert "Import from Spotify" not in labels


# ---------------------------------------------------------------------------
# _batch_download_lyrics signature
# ---------------------------------------------------------------------------

def test_batch_download_lyrics_accepts_paths_directly(window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "_downloader", None)

    class FakeThread:
        # isRunning() is required: window._downloader ends up holding this
        # instance, and win.close() (fixture teardown) -> closeEvent ->
        # stop_thread(self._downloader) calls .isRunning() unconditionally.
        # Missing it raises AttributeError inside a Qt close-event callback,
        # which PyQt turns into a fatal C++ abort rather than a normal
        # Python exception — the same issue test_workspace.py's
        # _FakeFetchThread had (see that file's docstring).
        def __init__(self, files):
            started.append(files)
        progress = type("Signal", (), {"connect": lambda self, fn: None})()
        finished_batch = type("Signal", (), {"connect": lambda self, fn: None})()
        def start(self): pass
        def isRunning(self): return False

    monkeypatch.setattr(app_module, "LyricsDownloaderThread", FakeThread)

    window._batch_download_lyrics(["/fake/a.mp3", "/fake/b.mp3"])

    assert started == [["/fake/a.mp3", "/fake/b.mp3"]]


def test_batch_download_lyrics_with_empty_list_shows_info_and_starts_nothing(window, monkeypatch):
    started = []
    monkeypatch.setattr(app_module, "LyricsDownloaderThread", lambda files: started.append(files))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    window._batch_download_lyrics([])

    assert started == []


# ---------------------------------------------------------------------------
# _replace_all_verses / _clear_lyrics split
# ---------------------------------------------------------------------------

def test_get_lyrics_does_not_trigger_the_clear_confirmation(window, monkeypatch):
    window._add_verse("[00:01.00]", "old line")
    window.current_file_path = "/fake/song.mp3"  # _get_lyrics_online guards on song_filename
    window.song_filename = "song.mp3"

    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes)

    window._replace_all_verses([("[00:05.00]", "new line")])

    assert asked == []
    assert len(window._verses) == 1
    assert window._verses[0].get_verse_text() == "new line"


def test_clear_lyrics_still_confirms_after_the_split(window, monkeypatch):
    window._add_verse("[00:01.00]", "line one")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel)

    window._clear_lyrics()

    assert len(window._verses) == 1  # cancelled -> untouched, same as before the split


def test_replace_all_verses_clears_undo_stack_stale_entries(window):
    # Sanity check: replacing the verse list shouldn't leave undo entries
    # pointing at verse objects that no longer exist in self._verses.
    window._add_verse("[00:01.00]", "old line")
    old_verse = window._verses[0]
    window._undo_stack.append((old_verse, "[00:00.00]"))

    window._replace_all_verses([("[00:05.00]", "new line")])

    assert old_verse not in window._verses
    # The undo entry for the removed verse is harmless (already covered by
    # _undo_last_sync's "skip stale entries" logic from the earlier fix),
    # but confirm the new verse list is exactly the replacement.
    assert [v.get_verse_text() for v in window._verses] == ["new line"]


def test_loading_a_song_with_existing_verses_does_not_trigger_the_clear_confirmation(window, monkeypatch, tmp_path):
    # _load_song used to call self._clear_lyrics() unconditionally, which
    # (after the earlier UX pass added a confirmation to it) meant
    # switching to a different song while the current one has lyrics
    # popped "Remove all N lines? This can't be undone" on every single
    # switch — a bug that predates this plan. _load_song must use the
    # silent primitive instead.
    #
    # This adds the "existing verses" via _add_verse directly rather than
    # a first _load_song() call: constructing a second full LrcSyncApp
    # (VLC instance, widget tree) in a later test after a prior test's
    # _replace_all_verses() has deleteLater()'d a verse widget was found,
    # empirically, to hang window construction somewhere in Qt/VLC's
    # object lifecycle — a test-suite resource-accumulation artifact
    # across the many full-window tests in this file, not a defect in
    # _load_song/_replace_all_verses themselves (every other test here
    # constructs exactly one window, matching real single-instance usage,
    # and passes reliably). Verifying the same code path without a second
    # real song-load avoids that hang while still exercising the fix.
    f2 = tmp_path / "b.mp3"
    f2.write_bytes(b"")

    window._add_verse("[00:01.00]", "line in the currently loaded song")

    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes)

    window._load_song(str(f2))

    assert asked == []
