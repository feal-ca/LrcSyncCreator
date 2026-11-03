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


def test_is_batch_active_true_while_its_downloads_are_unfinished(view):
    view._item_rows = {7: 0}
    assert view.is_batch_active() is True


# ---------------------------------------------------------------------------
# Automatic fallback: failed sources move to the back, and "Retry" starts
# from a source that hasn't failed yet.
# ---------------------------------------------------------------------------

def _cand(username):
    from soulseek import SoulseekFile
    return SoulseekFile(
        username=username, filename=f"music\\{username}.mp3", size=1, bitrate=320,
        length_s=200, queue_length=0, has_free_slot=True,
    )


def _one_row(view, candidates):
    from PyQt6.QtWidgets import QTableWidgetItem
    from download_view import _RowState

    class _FakeTrack:
        query = "Fake Artist Fake Title"

    view._rows = [_RowState(track=_FakeTrack(), status="matched",
                            candidates=candidates, selected=candidates[0])]
    view.table.setRowCount(1)
    for col in (0, 1, 2):
        view.table.setItem(0, col, QTableWidgetItem(""))
    return view._rows[0]


def test_download_order_puts_selected_first_then_other_candidates(view):
    a, b, c = _cand("a"), _cand("b"), _cand("c")
    state = _one_row(view, [a, b, c])
    state.selected = b

    assert view._download_order(state) == [b, a, c]


def test_retry_after_failure_starts_from_an_untried_source(view):
    a, b, c, d = _cand("a"), _cand("b"), _cand("c"), _cand("d")
    state = _one_row(view, [a, b, c, d])

    for f in (a, b, c):
        view._on_track_attempt(0, f)
    view._on_track_downloaded(0, False, "Tried 3 sources, all failed", "")

    assert state.selected is d
    assert view._download_order(state) == [d, a, b, c]


# ---------------------------------------------------------------------------
# Downloads go through the shared engine (TransfersView)
# ---------------------------------------------------------------------------

class _FakeTransfers:
    def __init__(self):
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Signals(QObject):
            item_changed = pyqtSignal(int, str, int, str)
            item_source_changed = pyqtSignal(int, object)
            item_finished = pyqtSignal(int, bool, str, str)

        self.signals = _Signals()
        self.item_changed = self.signals.item_changed
        self.item_source_changed = self.signals.item_source_changed
        self.item_finished = self.signals.item_finished
        self.enqueued = []
        self.cancelled = []
        self._next = 100

    def enqueue(self, files, alternatives=None, queue_patience_s=None):
        self._next += 1
        self.enqueued.append((files[0], alternatives(files[0]) if alternatives else [], queue_patience_s))
        return [self._next]

    def cancel_items(self, ids):
        self.cancelled.extend(ids)

    def skip_source(self, item_id):
        pass


@pytest.fixture
def engine_view(qapp, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)
    import download_view as download_view_module
    monkeypatch.setattr(download_view_module, "PlaylistFetchThread", _FakeFetchThread)
    engine = _FakeTransfers()
    v = DownloadView(settings, get_client=lambda: object(), collect_library_paths=lambda: [],
                     transfers=engine)
    return v, engine


def test_download_all_enqueues_matched_rows_with_their_fallbacks(engine_view):
    view, engine = engine_view
    a, b = _cand("a"), _cand("b")
    _one_row(view, [a, b])

    view._download_all()

    assert engine.enqueued == [(a, [b], 180.0)]
    assert view._rows[0].status == "download_queued"
    assert view.is_batch_active()


def test_rows_matched_after_download_all_start_downloading_by_themselves(engine_view):
    from PyQt6.QtWidgets import QTableWidgetItem
    from download_view import _RowState

    view, engine = engine_view

    class _T:
        query = "x"

    view._rows = [_RowState(track=_T(), status="searching")]
    view.table.setRowCount(1)
    for col in (0, 1, 2):
        view.table.setItem(0, col, QTableWidgetItem(""))
    view._auto_download = True

    view._on_track_matched(0, [_cand("late")])

    assert [e[0].username for e in engine.enqueued] == ["late"]


def test_row_mirrors_live_transfer_status_and_result(engine_view):
    view, engine = engine_view
    _one_row(view, [_cand("a"), _cand("b")])
    view._download_row(0)
    item_id = view._rows[0].item_id
    done = []
    view.file_downloaded.connect(done.append)

    engine.item_changed.emit(item_id, "queued", 0, "In a's queue (#3) · 1:10")
    assert view.table.item(0, 1).text() == "In a's queue (#3) · 1:10"

    engine.item_source_changed.emit(item_id, view._rows[0].candidates[1])
    assert view._rows[0].selected.username == "b"

    engine.item_finished.emit(item_id, True, "x.mp3", "/lib/x.mp3")
    assert view._rows[0].status == "downloaded"
    assert done == ["/lib/x.mp3"]
    assert not view.is_batch_active()


def test_cancel_cancels_this_pages_downloads(engine_view):
    view, engine = engine_view
    _one_row(view, [_cand("a")])
    view._download_row(0)

    view._cancel_current()

    assert engine.cancelled == [view._rows[0].item_id]
