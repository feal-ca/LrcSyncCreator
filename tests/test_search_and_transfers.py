"""Tests for the Nicotine-style search (filter, folder browse, live search
stop) and the Downloads queue."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

import search_view
import transfers_view
import workers
from soulseek import ResultFilter, SoulseekFile, normalize_text


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)


def _f(path, user="peer", size=5_000_000, bitrate=320, free=True, queue=0):
    return SoulseekFile(
        username=user, filename=path, size=size, bitrate=bitrate, length_s=200,
        queue_length=queue, has_free_slot=free,
    )


# -- ResultFilter --------------------------------------------------------------

def test_normalize_text_strips_accents_and_punctuation():
    assert normalize_text("ROSALÍA_-_Malamente.flac") == "rosalia malamente flac"


def test_filter_matches_artist_in_folder_not_just_filename():
    f = _f("music\\Manu Chao\\Clandestino\\01 - Clandestino.mp3")
    assert ResultFilter("manu chao").matches(f)
    assert not ResultFilter("bob marley").matches(f)


def test_filter_excludes_whole_words_and_keeps_phrases():
    live = _f("music\\Artist\\Song (Live).mp3")
    oliver = _f("music\\Oliver\\Song.mp3")
    flt = ResultFilter("song -live")
    assert not flt.matches(live)
    assert flt.matches(oliver)
    assert ResultFilter('"manu chao"').matches(_f("x\\Manu Chao - Bongo Bong.mp3"))
    assert not ResultFilter('"manu chao"').matches(_f("x\\Chao Manu.mp3"))


def test_filter_format_bitrate_and_free_slot():
    flac = _f("a\\Song.flac", bitrate=None)
    mp3_128 = _f("a\\Song.mp3", bitrate=128)
    queued = _f("a\\Song.mp3", free=False, queue=4)
    assert ResultFilter(formats="lossless").matches(flac)
    assert not ResultFilter(formats="lossless").matches(mp3_128)
    assert ResultFilter(formats="mp3").matches(mp3_128)
    assert not ResultFilter(min_kbps=192).matches(mp3_128)
    assert ResultFilter(min_kbps=192).matches(flac)   # lossless always passes
    assert not ResultFilter(free_slot_only=True).matches(queued)


def test_results_widget_filters_live(qapp):
    w = search_view.SearchResultsWidget()
    w.set_results([
        _f("m\\Manu Chao\\Clandestino.mp3"),
        _f("m\\Covers\\Clandestino (cover).mp3", user="other"),
        _f("m\\Manu Chao\\Clandestino.flac", bitrate=None),
    ])
    assert w.visible_count() == 3
    w.set_filter_text("manu")
    assert w.visible_count() == 2
    w.format_combo.setCurrentIndex(w.format_combo.findData("lossless"))
    assert w.visible_count() == 1
    assert w.file_at_proxy_row(0).filename.endswith(".flac")


# -- wait_for_download: hand-picked downloads may queue indefinitely ------------

def test_wait_for_download_does_not_stall_while_remotely_queued(monkeypatch, tmp_path):
    clock = {"t": 0.0}
    monkeypatch.setattr(workers.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(workers.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    landed = str(tmp_path / "Song.mp3")
    monkeypatch.setattr(
        workers, "find_downloaded_file",
        lambda d, name, size: landed if clock["t"] >= 900 else None,
    )

    class Client:
        def get_download_transfer(self, user, name):
            return {"state": "Queued, Remotely", "placeInQueue": 5}

    ok, detail, path = workers.wait_for_download(
        Client(), str(tmp_path), _f("x\\Song.mp3"), timeout_s=60,
        queued_timeout_s=None, max_total_s=3600,
    )
    assert ok, detail
    assert path == landed


# -- TransfersView -------------------------------------------------------------

class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in self.slots:
            fn(*args)


class _FakeTransferThread:
    instances = []

    def __init__(self, item_id, client, downloads_dir, candidates, finalize,
                 convert_lossless=False, queue_patience_s=None, max_attempts=3):
        self.item_id = item_id
        self.candidates = candidates
        self.queue_patience_s = queue_patience_s
        self.skipped = False
        self.status = _Sig()
        self.source_changed = _Sig()
        self.finished_transfer = _Sig()
        self.finished = _Sig()
        self.started = False
        self.cancelled = False
        _FakeTransferThread.instances.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return self.started

    def request_cancel(self):
        self.cancelled = True

    def request_skip(self):
        self.skipped = True

    def wait(self, *_a):
        return True

    def finish(self, ok, detail, path=""):
        self.finished_transfer.emit(self.item_id, ok, detail, path)
        self.started = False
        self.finished.emit()


@pytest.fixture
def transfers(qapp, settings, monkeypatch):
    _FakeTransferThread.instances = []
    monkeypatch.setattr(transfers_view, "TransferThread", _FakeTransferThread)
    return transfers_view.TransfersView(settings, lambda: object())


def test_enqueue_dedupes_and_limits_active_threads(transfers):
    files = [_f(f"a\\{i}.mp3") for i in range(transfers_view.MAX_ACTIVE + 2)]
    assert len(transfers.enqueue(files)) == len(files)
    assert transfers.enqueue(files[:2]) == []          # already queued
    assert len(_FakeTransferThread.instances) == transfers_view.MAX_ACTIVE
    assert transfers.active_count() == len(files)


def test_finished_transfer_emits_file_ready_and_starts_the_next(transfers):
    ready = []
    transfers.file_ready.connect(ready.append)
    transfers.enqueue([_f(f"a\\{i}.mp3") for i in range(transfers_view.MAX_ACTIVE + 1)])

    _FakeTransferThread.instances[0].finish(True, "0.mp3", "/lib/0.mp3")

    assert ready == ["/lib/0.mp3"]
    assert len(_FakeTransferThread.instances) == transfers_view.MAX_ACTIVE + 1


def test_failed_transfer_can_be_retried(transfers):
    transfers.enqueue([_f("a\\x.mp3")])
    _FakeTransferThread.instances[0].finish(False, "peer went offline")
    assert transfers.active_count() == 0
    assert transfers.retry_button.isEnabled()

    transfers.retry_failed()

    assert len(_FakeTransferThread.instances) == 2
    assert transfers.active_count() == 1


def test_alternatives_ride_along_and_patience_needs_somewhere_to_go(transfers):
    a, b = _f("a\\x.mp3", user="a"), _f("a\\x.mp3", user="b")
    lonely = _f("a\\y.mp3", user="c")
    transfers.enqueue([a, lonely], alternatives=lambda f: [b] if f is a else [], queue_patience_s=240)

    first, second = _FakeTransferThread.instances
    assert first.candidates == [a, b] and first.queue_patience_s == 240
    assert second.candidates == [lonely] and second.queue_patience_s is None


def test_skip_source_asks_the_running_thread_to_move_on(transfers):
    ids = transfers.enqueue([_f("a\\x.mp3", user="a")], alternatives=lambda f: [_f("a\\x.mp3", user="b")])
    transfers.skip_source(ids[0])
    assert _FakeTransferThread.instances[0].skipped


def test_item_signals_carry_status_and_result(transfers):
    changed, finished = [], []
    transfers.item_changed.connect(lambda *a: changed.append(a))
    transfers.item_finished.connect(lambda *a: finished.append(a))
    ids = transfers.enqueue([_f("a\\x.mp3")])
    thread = _FakeTransferThread.instances[0]

    thread.status.emit(ids[0], "downloading", 40, "40% · 1.0 MB/s")
    thread.finish(True, "x.mp3", "/lib/x.mp3")

    assert (ids[0], "downloading", 40, "40% · 1.0 MB/s") in changed
    assert finished == [(ids[0], True, "x.mp3", "/lib/x.mp3")]


def test_file_of_maps_item_ids_back_to_files_even_when_some_were_skipped(transfers):
    a, b = _f("a\\1.mp3"), _f("a\\2.mp3")
    transfers.enqueue([a])

    ids = transfers.enqueue([a, b])          # a is already queued: only b gets an id

    assert [transfers.file_of(i).filename for i in ids] == [b.filename]
    assert transfers.file_of(9999) is None
