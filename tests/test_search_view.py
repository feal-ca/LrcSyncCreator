"""Tests for the Search page (search_view.SearchView): live results grouped
by version, Enter downloads the best copy, Shift+Enter the album, and each
row follows its download."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

import search_view
from soulseek import SoulseekFile


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _StreamingClient:
    """start_search hands back the on_results callback; the test calls it
    the way the real client does when peers answer."""

    def __init__(self) -> None:
        self.searches: list[tuple[str, object]] = []
        self.stopped: list[int] = []

    def start_search(self, query, on_results=None, **_kw):
        self.searches.append((query, on_results))
        return len(self.searches)

    def stop_search(self, search_id):
        self.stopped.append(search_id)
        return []

    def deliver(self, files, search=-1):
        self.searches[search][1](files)


def _f(path, user="u", bitrate=320, length=200, free=True, queue=0, size=8_000_000):
    return SoulseekFile(user, path, size, bitrate, length, queue, free, 1_000_000)


@pytest.fixture
def client():
    return _StreamingClient()


@pytest.fixture
def view(qapp, tmp_path, client):
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    v = search_view.SearchView(settings, lambda: client, lambda text: "spotify.com" in text,
                               library_paths=lambda: ["/m/Queen - Bohemian Rhapsody.mp3"])
    yield v
    v.shutdown()


def _search(view, client, query):
    view.query_input.setText(query)
    view.run_search()
    return client


def _rows(view):
    return [view.tree.topLevelItem(i).text(0) for i in range(view.tree.topLevelItemCount())]


def test_results_stream_in_grouped_with_the_best_version_selected(view, client):
    _search(view, client, "sabrina carpenter espresso")
    client.deliver([_f(f"k{i}\\Sabrina Carpenter - Espresso (Karaoke).mp3", user=f"k{i}") for i in range(3)])
    client.deliver([_f("a\\Sabrina Carpenter - Espresso.mp3", user="a"),
                    _f("b\\Sabrina Carpenter - Espresso.mp3", user="b")])

    view.refresh_now()

    assert client.searches[0][0] == "sabrina carpenter espresso"
    assert _rows(view) == ["Sabrina Carpenter - Espresso", "Sabrina Carpenter - Espresso (Karaoke)"]
    assert view.tree.topLevelItem(0).text(4) == "2"            # copies
    assert view.tree.currentItem() is view.tree.topLevelItem(0)


def test_enter_downloads_the_best_copy_with_the_others_as_fallbacks(view, client):
    _search(view, client, "espresso")
    slow = _f("a\\Espresso.mp3", user="slow", free=False, queue=30)
    fast = _f("b\\Espresso.mp3", user="fast")
    client.deliver([slow, fast])
    view.refresh_now()
    queued = []
    view.download_requested.connect(queued.append)

    view.download_selected()

    assert [f.username for f in queued[0]] == ["fast"]
    assert [f.username for f in view.alternatives_for(fast)] == ["slow"]


def test_shift_enter_downloads_the_album_of_the_best_copy(view, client, monkeypatch):
    _search(view, client, "espresso")
    client.deliver([_f("Short n' Sweet\\03 Espresso.mp3", user="a")])
    view.refresh_now()
    albums = []
    monkeypatch.setattr(view, "download_folder", albums.append)

    view.download_selected_album()

    assert albums[0].folder_name == "Short n' Sweet"


def test_late_results_from_a_previous_search_are_ignored(view, client):
    _search(view, client, "first")
    _search(view, client, "second")

    client.deliver([_f("x\\First.mp3")], search=0)
    view.refresh_now()

    assert _rows(view) == []
    assert client.stopped == [1]


def test_filter_and_lossless_toggle(view, client):
    _search(view, client, "espresso")
    client.deliver([_f("pop\\Espresso.mp3", user="a"),
                    _f("jazz\\Espresso Macchiato.flac", user="b", bitrate=None, size=30_000_000)])
    view.refresh_now()

    view.filter_input.setText("-macchiato")
    view.refresh_now()
    assert _rows(view) == ["Espresso"]

    view.filter_input.setText("")
    view.lossless_check.setChecked(True)
    view.refresh_now()
    assert _rows(view) == ["Espresso Macchiato"]


def test_rows_follow_their_download(view, client):
    _search(view, client, "espresso")
    best = _f("a\\Espresso.mp3", user="a")
    client.deliver([best])
    view.refresh_now()

    view.note_queued([best], [41])
    view.on_transfer_status(41, "downloading", 40, "40% · 1.2 MB/s")
    assert view.tree.topLevelItem(0).text(5) == "Downloading 40%"

    view.on_transfer_finished(41, True, "Espresso.mp3", "/m/Espresso.mp3")
    assert view.tree.topLevelItem(0).text(5) == "In library"


def test_versions_already_in_the_library_say_so(view, client):
    _search(view, client, "bohemian rhapsody")
    client.deliver([_f("Queen\\A Night at the Opera\\11 - Bohemian Rhapsody.flac", bitrate=None)])
    view.refresh_now()

    assert view.tree.topLevelItem(0).text(5) == "In library"


def test_picking_a_row_stops_the_list_from_reshuffling(view, client):
    _search(view, client, "song")
    client.deliver([_f("a\\Song.mp3", user="a")])
    view.refresh_now()
    view.tree.setCurrentItem(view.tree.topLevelItem(0))
    view.on_user_picked()

    client.deliver([_f(f"b{i}\\Song.mp3", user=f"b{i}", length=320) for i in range(6)])
    view.refresh_now()

    assert view.tree.topLevelItem(0).text(4) == "1"     # the picked version stayed on top
    assert view.tree.currentItem() is view.tree.topLevelItem(0)


def test_spotify_links_go_to_the_import(view, client):
    links = []
    view.spotify_link_entered.connect(links.append)

    _search(view, client, "https://open.spotify.com/album/x")

    assert links == ["https://open.spotify.com/album/x"]
    assert client.searches == []
