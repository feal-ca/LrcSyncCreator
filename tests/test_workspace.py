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
    # calls get_client(), the bound _get_slskd_client method DownloadView
    # captures at construction time. With no slskd_url/slskd_api_key in
    # this test's fresh settings, the real method pops a blocking
    # QMessageBox.information ("Soulseek Not Configured") and hangs the
    # test. Must be patched on the class before LrcSyncApp() constructs
    # DownloadView — patching the instance afterward wouldn't affect the
    # bound-method object DownloadView already captured.
    monkeypatch.setattr(app_module.LrcSyncApp, "_get_slskd_client", lambda self: object())

    win = LrcSyncApp()
    yield win
    win.close()


def test_workspace_is_a_stacked_widget_with_three_pages(window):
    assert isinstance(window.workspace, QStackedWidget)
    assert window.workspace.count() == 3


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


def test_download_view_instance_survives_workspace_swaps(window, tmp_path):
    # A batch import must not be rebuilt (and its threads lost) just
    # because the workspace swaps away and back.
    view_before = window.download_view
    window._open_batch_import("https://open.spotify.com/playlist/abc")

    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    window.library_panel.add_files([str(f)])
    window.library_panel.song_selected.emit(str(f))   # swap to editor
    window._open_batch_import("https://open.spotify.com/playlist/abc")  # swap back

    assert window.download_view is view_before


def test_empty_state_add_music_button_reaches_the_same_signal_as_the_sidebar(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    # find_music_requested is also connected to the real _find_music
    # (wired in this same task), which opens a real modal
    # SoulseekSearchDialog.exec() — blocking unless neutralized, same as
    # test_app_commands.py's Find Music test.
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)

    received = []
    window.library_panel.find_music_requested.connect(lambda: received.append(True))

    window._empty_state_add_music_button.click()

    assert received == [True]


def test_file_downloaded_adds_to_library_panel_live(window, tmp_path):
    f = tmp_path / "new_song.mp3"
    f.write_bytes(b"")

    window.download_view.file_downloaded.emit(str(f))

    assert str(f) in window.library_panel.real_file_paths()
