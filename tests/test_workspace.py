"""Tests for LrcSyncApp's workspace stack (app.py) — the QStackedWidget
that replaced the old Library/Download QTabWidget, holding three pages:
the sync editor, the Spotify batch-import review, and an empty state.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QStackedWidget

import app as app_module
from app import LrcSyncApp, WORKSPACE_ACQUIRE, WORKSPACE_EDITOR, WORKSPACE_EMPTY


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeFetchThread:
    """Stands in for workers.PlaylistFetchThread so _open_batch_import
    (and anything that reaches DownloadView.start_fetch) never starts a
    real thread that hits the network in a test.

    Implements isRunning() because workers.stop_thread() (called from
    DownloadView.shutdown(), in turn called from LrcSyncApp.closeEvent on
    win.close()) calls it unconditionally. Without it, closing the test
    window after a batch import raises AttributeError inside a Qt close
    event, which PyQt turns into a fatal C++ abort instead of a normal
    Python traceback — not a hang, a hard crash of the whole process.
    """
    fetched = type("Signal", (), {"connect": lambda self, fn: None})()
    fetch_failed = type("Signal", (), {"connect": lambda self, fn: None})()

    def __init__(self, *a, **k) -> None: ...
    def start(self) -> None: ...
    def isRunning(self) -> bool: return False


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    ini_path = str(tmp_path / "test_settings.ini")
    monkeypatch.setattr(
        app_module, "QSettings",
        lambda *a, **k: QSettings(ini_path, QSettings.Format.IniFormat),
    )
    import download_view as download_view_module
    monkeypatch.setattr(download_view_module, "PlaylistFetchThread", _FakeFetchThread)
    # _open_batch_import -> DownloadView.start_fetch -> _on_fetch_clicked
    # calls get_client(), the bound _get_slsk_client method DownloadView
    # captures at construction time. With no slskd_url/slskd_api_key in
    # this test's fresh settings, the real method pops a blocking
    # QMessageBox.information ("Soulseek Not Configured") and hangs the
    # test. Must be patched on the class before LrcSyncApp() constructs
    # DownloadView — patching the instance afterward wouldn't affect the
    # bound-method object DownloadView already captured.
    monkeypatch.setattr(app_module.LrcSyncApp, "_get_slsk_client", lambda self: object())

    win = LrcSyncApp()
    yield win
    # A dirty window (e.g. from a test verifying the C1 fix below) makes
    # win.close() trigger a real, unmocked QMessageBox.question via
    # _confirm_discard_if_dirty — see test_app_commands.py's window
    # fixture for the full root-cause writeup of this exact hang.
    win._dirty = False
    # Closing asks before stopping running downloads; a test that leaves a
    # fake batch "active" must not block teardown on that modal dialog.
    from PyQt6.QtWidgets import QMessageBox as _QMB
    monkeypatch.setattr(_QMB, "question", lambda *a, **k: _QMB.StandardButton.Yes)
    win.close()


def test_workspace_is_a_stacked_widget_with_five_pages(window):
    assert isinstance(window.workspace, QStackedWidget)
    assert window.workspace.count() == 5


def test_tab_bar_follows_programmatic_page_changes(window):
    window._show_page(app_module.WORKSPACE_TRANSFERS)
    assert window.tabs.currentIndex() == app_module.TAB_DOWNLOADS

    # The Spotify review has no tab of its own: it lives under Search.
    window._show_page(WORKSPACE_ACQUIRE)
    assert window.tabs.currentIndex() == app_module.TAB_SEARCH
    assert window.tabs.count() == 3


def test_editor_tab_shows_empty_state_until_a_song_is_loaded(window):
    window.tabs.setCurrentIndex(app_module.TAB_SEARCH)
    window.tabs.setCurrentIndex(app_module.TAB_EDITOR)
    assert window.workspace.currentIndex() == WORKSPACE_EMPTY


def test_downloads_tab_shows_active_count(window):
    window._on_active_downloads_changed(3)
    assert window.tabs.tabText(app_module.TAB_DOWNLOADS) == "Downloads (3)"
    window._on_active_downloads_changed(0)
    assert window.tabs.tabText(app_module.TAB_DOWNLOADS) == "Downloads"


def test_starts_on_the_empty_page_with_no_song_loaded(window):
    assert window.workspace.currentIndex() == WORKSPACE_EMPTY


def test_selecting_a_song_swaps_to_the_editor_page(window, tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    window.library_panel.add_files([str(f)])

    window.library_panel.song_selected.emit(str(f))

    assert window.workspace.currentIndex() == WORKSPACE_EDITOR


def test_open_batch_import_swaps_to_the_acquire_page(window):
    window._open_batch_import("https://open.spotify.com/playlist/abc")
    assert window.workspace.currentIndex() == WORKSPACE_ACQUIRE


def test_open_batch_import_does_not_refetch_while_a_batch_is_active(window, monkeypatch):
    # Review finding I1: re-entering Find Music with a batch already in
    # flight used to unconditionally call start_fetch again, which wipes
    # the table/rows (table.setRowCount(0); self._rows = []) while a
    # still-running search/download thread keeps delivering results by
    # row index into a list that no longer has those rows — IndexError,
    # which PyQt turns into a fatal abort rather than a catchable
    # exception. Re-entering must just show the in-progress batch.
    calls = []
    monkeypatch.setattr(window.download_view, "start_fetch", lambda link: calls.append(link))

    class _RunningThread:
        # wait() is required alongside isRunning(): this fake outlives
        # the test body (still attached to download_view at teardown),
        # and DownloadView.shutdown() -> stop_thread() calls wait() right
        # after isRunning() returns True — the same class of gap as
        # elsewhere in this file's _FakeFetchThread, just one method
        # further into stop_thread's body.
        def isRunning(self) -> bool: return True
        def wait(self, timeout_ms: int) -> bool: return True

    window.download_view._search_thread = _RunningThread()

    window._open_batch_import("https://open.spotify.com/playlist/abc")

    assert calls == []
    assert window.workspace.currentIndex() == WORKSPACE_ACQUIRE
    window.download_view._search_thread = None


def test_open_batch_import_fetches_normally_when_nothing_is_active(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.download_view, "start_fetch", lambda link: calls.append(link))

    window._open_batch_import("https://open.spotify.com/playlist/abc")

    assert calls == ["https://open.spotify.com/playlist/abc"]


def test_download_view_instance_survives_workspace_swaps(window, tmp_path):
    # A batch import must not be rebuilt (and its threads/rows lost) just
    # because the workspace swaps away and back. Checking object identity
    # alone doesn't prove this — nothing in the app ever reassigns
    # window.download_view, so that assertion would pass even if a swap
    # silently discarded in-progress state. Populate real in-progress
    # state (rows + a running thread) and confirm it's still there.
    view_before = window.download_view
    window._open_batch_import("https://open.spotify.com/playlist/abc")

    view_before._rows = ["fake row standing in for an in-progress track"]

    class _RunningThread:
        def isRunning(self) -> bool: return True
        def wait(self, timeout_ms: int) -> bool: return True

    view_before._search_thread = _RunningThread()

    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    window.library_panel.add_files([str(f)])
    window.library_panel.song_selected.emit(str(f))   # swap to editor
    assert window.workspace.currentIndex() == WORKSPACE_EDITOR
    # The download thread is still "running" (is_batch_active() is True),
    # so per the I1 fix this must show the batch again, not restart it.
    window._open_batch_import("https://open.spotify.com/playlist/abc")  # swap back

    assert window.download_view is view_before
    assert window.workspace.currentIndex() == WORKSPACE_ACQUIRE
    assert view_before._rows == ["fake row standing in for an in-progress track"]
    assert view_before._search_thread.isRunning() is True

    # Let teardown's stop_thread() proceed cleanly without this fake.
    view_before._search_thread = None


def test_empty_state_explains_what_to_do(window):
    from PyQt6.QtWidgets import QLabel, QPushButton

    page = window.workspace.widget(WORKSPACE_EMPTY)
    texts = [label.text() for label in page.findChildren(QLabel)]
    assert "Open a song to edit its lyrics" in texts
    assert page.findChildren(QPushButton) == []


def test_spotify_review_has_a_way_back_to_search(window):
    window._show_page(WORKSPACE_ACQUIRE)
    window.download_view.back_button.click()
    assert window.workspace.currentIndex() == app_module.WORKSPACE_SEARCH


def test_library_menu_offers_the_selection_actions(window, tmp_path):
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    a.write_bytes(b"")
    b.write_bytes(b"")
    window.library_panel.add_files([str(a), str(b)])
    window.library_panel.select_all_visible()

    window._fill_library_menu()

    labels = [act.text() for act in window.library_menu.actions() if not act.isSeparator()]
    assert "Move 2 to Trash…" in labels
    assert labels[-1] == "Select All Shown Songs"


def test_library_menu_without_selection_says_so(window):
    window._fill_library_menu()
    first = window.library_menu.actions()[0]
    assert first.text() == "Select songs in the library first"
    assert not first.isEnabled()


def test_trash_unloads_the_open_song_and_removes_rows(window, tmp_path, monkeypatch):
    from PyQt6.QtCore import QFile
    from PyQt6.QtWidgets import QMessageBox

    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    (tmp_path / "song.lrc").write_text("[00:01.00]x\n", encoding="utf-8")
    window.library_panel.add_files([str(song)])
    window.library_panel.song_selected.emit(str(song))
    trashed = []
    monkeypatch.setattr(QFile, "moveToTrash", lambda p: (trashed.append(p) or True, p))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    window._trash_files([str(song)])

    assert trashed == [str(song), str(tmp_path / "song.lrc")]
    assert window.library_panel.real_file_paths() == []
    assert not window.song_is_loaded
    assert window.workspace.currentIndex() == WORKSPACE_EMPTY


def test_trash_cancelled_keeps_everything(window, tmp_path, monkeypatch):
    from PyQt6.QtCore import QFile
    from PyQt6.QtWidgets import QMessageBox

    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    window.library_panel.add_files([str(song)])
    monkeypatch.setattr(QFile, "moveToTrash", lambda p: pytest.fail("must not trash"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel)

    window._trash_files([str(song)])

    assert window.library_panel.real_file_paths() == [str(song)]


def test_no_lyrics_card_shows_for_a_song_without_lyrics_and_hides_once_typed(window, tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    window.library_panel.add_files([str(song)])
    window.library_panel.song_selected.emit(str(song))
    assert not window.no_lyrics_card.isHidden()

    window._verses[0].verse_text.setText("first line")

    assert window.no_lyrics_card.isHidden()


def test_no_lyrics_card_hidden_when_the_song_has_lyrics(window, tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    (tmp_path / "song.lrc").write_text("[00:01.00]hello\n", encoding="utf-8")
    window.library_panel.add_files([str(song)])
    window.library_panel.song_selected.emit(str(song))

    assert window.no_lyrics_card.isHidden()


def test_ctrl_l_with_several_library_songs_fetches_the_unsynced_ones(window, tmp_path, monkeypatch):
    synced, missing = tmp_path / "a.mp3", tmp_path / "b.mp3"
    synced.write_bytes(b"")
    missing.write_bytes(b"")
    (tmp_path / "a.lrc").write_text("[00:01.00]x\n", encoding="utf-8")
    window.library_panel.add_files([str(synced), str(missing)])
    window.library_panel.select_all_visible()
    monkeypatch.setattr(window.library_panel.file_list, "hasFocus", lambda: True)
    fetched = []
    monkeypatch.setattr(window, "_fetch_lyrics_for", fetched.append)

    window._find_lyrics_command()

    assert fetched == [[str(missing)]]


def test_save_embeds_only_when_the_setting_is_on(window, tmp_path, monkeypatch):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    window.library_panel.add_files([str(song)])
    window.library_panel.song_selected.emit(str(song))
    window._verses[0].verse_text.setText("hello")
    embedded = []
    monkeypatch.setattr(app_module, "write_embedded_lyrics", lambda *a: embedded.append(a))

    window.settings.setValue("embed_lyrics", False)
    window._save_lrc_file()
    window.settings.setValue("embed_lyrics", True)
    window._save_lrc_file()

    assert len(embedded) == 1


def test_file_downloaded_adds_to_library_panel_live(window, tmp_path):
    f = tmp_path / "new_song.mp3"
    f.write_bytes(b"")

    window.download_view.file_downloaded.emit(str(f))

    assert str(f) in window.library_panel.real_file_paths()


# ---------------------------------------------------------------------------
# Review finding C1: a finishing batch must not discard unsaved work
# ---------------------------------------------------------------------------

class _FakeLyricsDownloaderThread:
    """Stands in for workers.LyricsDownloaderThread so
    _on_download_files_ready's call to _fetch_lyrics_for doesn't start a
    real network-hitting thread. isRunning() is required for the same
    reason documented on _FakeFetchThread above."""
    progress = type("Signal", (), {"connect": lambda self, fn: None})()
    finished_batch = type("Signal", (), {"connect": lambda self, fn: None})()

    def __init__(self, *a, **k) -> None: ...
    def start(self) -> None: ...
    def isRunning(self) -> bool: return False


def test_on_download_files_ready_does_not_switch_songs_while_dirty(window, monkeypatch, tmp_path):
    # _on_download_files_ready used to call _load_song(paths[0])
    # unconditionally once a batch completed — if the user had unsaved
    # sync edits on whatever song was currently loaded, a batch finishing
    # in the background would silently discard them with no prompt.
    monkeypatch.setattr(app_module, "LyricsDownloaderThread", _FakeLyricsDownloaderThread)

    current = tmp_path / "current.mp3"
    current.write_bytes(b"")
    window._load_song(str(current))
    window._add_verse("[00:01.00]", "unsaved edit")
    window._dirty = True

    new_song = tmp_path / "new_from_batch.mp3"
    new_song.write_bytes(b"")

    window._on_download_files_ready([str(new_song)])

    assert window.current_file_path == str(current)
    assert window._dirty is True
    # _load_song(current) itself added one default empty verse (no .lrc
    # file yet) before the test's own _add_verse call appended a second —
    # the fix under test is that neither gets wiped by the batch finishing.
    assert "unsaved edit" in [v.get_verse_text() for v in window._verses]
    # The new file is still added to the library even though we didn't
    # jump to it — nothing about the download itself is lost.
    assert str(new_song) in window.library_panel.real_file_paths()


def test_on_download_files_ready_switches_songs_when_not_dirty(window, monkeypatch, tmp_path):
    # The auto-jump-to-the-new-song behavior (the redesign's actual goal)
    # must still work in the common case: nothing unsaved to lose.
    monkeypatch.setattr(app_module, "LyricsDownloaderThread", _FakeLyricsDownloaderThread)

    new_song = tmp_path / "new_from_batch.mp3"
    new_song.write_bytes(b"")

    window._on_download_files_ready([str(new_song)])

    assert window.current_file_path == str(new_song)


def test_background_song_reload_does_not_leave_the_current_page(window, tmp_path):
    # Lyrics arriving for the open song (or auto-advance at the end of a
    # track) must not yank the user off the Search page.
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    window._load_song(str(f))
    window._show_page(app_module.WORKSPACE_SEARCH)

    window._load_song(str(f), keep_playing=True, show_editor=False)

    assert window.workspace.currentIndex() == app_module.WORKSPACE_SEARCH
    assert window.tabs.currentIndex() == app_module.TAB_SEARCH
