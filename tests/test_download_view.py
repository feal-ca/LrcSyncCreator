"""Tests for lrc_studio/download_view.py's start_fetch() (a programmatic
trigger used by app.py's unified Find Music entry point) and the
file_downloaded signal (drives live library updates during a batch
download).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from download_view import DownloadView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeFetchThread:
    """Stands in for workers.PlaylistFetchThread — start_fetch() triggers
    a real fetch via _on_fetch_clicked(), and a test must never let that
    hit the network."""
    fetched = type("Signal", (), {"connect": lambda self, fn: None})()
    fetch_failed = type("Signal", (), {"connect": lambda self, fn: None})()

    def __init__(self, *a, **k) -> None: ...
    def start(self) -> None: ...


@pytest.fixture
def view(qapp, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)
    import download_view as download_view_module
    monkeypatch.setattr(download_view_module, "PlaylistFetchThread", _FakeFetchThread)
    # get_client must return non-None: _on_fetch_clicked returns early on a
    # None client, before it ever disables the button — a None here would
    # make test_start_fetch_disables_the_button_like_a_manual_click_does
    # fail for a reason unrelated to what it's testing.
    return DownloadView(settings, get_client=lambda: object(), collect_library_paths=lambda: [])


def test_start_fetch_populates_the_link_input(view):
    view.start_fetch("https://open.spotify.com/playlist/abc")
    assert view.link_input.text() == "https://open.spotify.com/playlist/abc"


def test_start_fetch_disables_the_button_like_a_manual_click_does(view):
    view.start_fetch("https://open.spotify.com/playlist/abc")
    assert view.fetch_button.isEnabled() is False


def test_file_downloaded_signal_exists():
    assert hasattr(DownloadView, "file_downloaded")


def test_on_track_downloaded_actually_emits_file_downloaded(view):
    # Strengthens the hasattr check above: proves the signal fires from
    # the real completion handler with the real downloaded path, not
    # just that the attribute exists on the class.
    from download_view import _RowState

    class _FakeTrack:
        query = "Fake Artist Fake Title"

    view._rows = [_RowState(track=_FakeTrack(), status="downloading")]
    view.table.setRowCount(1)
    for col in (0, 1, 2):
        from PyQt6.QtWidgets import QTableWidgetItem
        view.table.setItem(0, col, QTableWidgetItem(""))

    received = []
    view.file_downloaded.connect(lambda path: received.append(path))

    view._on_track_downloaded(0, True, "downloaded.mp3", "/fake/library/downloaded.mp3")

    assert received == ["/fake/library/downloaded.mp3"]


def test_on_track_downloaded_does_not_emit_on_failure(view):
    from download_view import _RowState

    class _FakeTrack:
        query = "Fake Artist Fake Title"

    view._rows = [_RowState(track=_FakeTrack(), status="downloading")]
    view.table.setRowCount(1)
    for col in (0, 1, 2):
        from PyQt6.QtWidgets import QTableWidgetItem
        view.table.setItem(0, col, QTableWidgetItem(""))

    received = []
    view.file_downloaded.connect(lambda path: received.append(path))

    view._on_track_downloaded(0, False, "some error", "")

    assert received == []


# ---------------------------------------------------------------------------
# Review finding I1: is_batch_active() lets callers avoid re-fetching
# (and wiping the table/rows) while a search or download is in flight.
# ---------------------------------------------------------------------------

def test_is_batch_active_false_when_nothing_running(view):
    assert view.is_batch_active() is False


def test_is_batch_active_true_when_fetch_thread_running(view):
    class _RunningThread:
        def isRunning(self) -> bool: return True

    view._fetch_thread = _RunningThread()
    assert view.is_batch_active() is True


def test_is_batch_active_true_when_search_thread_running(view):
    class _RunningThread:
        def isRunning(self) -> bool: return True

    view._search_thread = _RunningThread()
    assert view.is_batch_active() is True


def test_is_batch_active_true_when_download_thread_running(view):
    class _RunningThread:
        def isRunning(self) -> bool: return True

    view._download_thread = _RunningThread()
    assert view.is_batch_active() is True
