"""Tests for slsk_client — the in-process Soulseek client (aioslsk) that
replaced slskd. Nothing here touches the network: aioslsk's own model
objects are built by hand and fed to the facade."""

import threading
import time

import pytest

pytest.importorskip("aioslsk")

from aioslsk.events import SearchResultEvent
from aioslsk.protocol.primitives import Attribute, AttributeKey, DirectoryData, FileData
from aioslsk.search.model import SearchRequest, SearchResult
from aioslsk.transfer.model import FailReason, Transfer, TransferDirection, TransferState

import slsk_client
from slsk_client import SlskClient, listing_to_files, read_slskd_credentials, slskd_state, to_soulseek_files
from soulseek import SoulseekFile
from soulseek import SlskdConnectionError


def _transfer(state, *, remotely_queued=False, fail_reason=None):
    t = Transfer("peer", "Music\\Artist\\song.mp3", TransferDirection.DOWNLOAD)
    t.state = TransferState.init_from_state(state, t)
    t.remotely_queued = remotely_queued
    t.fail_reason = fail_reason
    return t


S = TransferState.State


@pytest.mark.parametrize("state, kwargs, expected", [
    (S.VIRGIN, {}, "Queued, Locally"),
    (S.QUEUED, {"remotely_queued": True}, "Queued, Remotely"),
    (S.INCOMPLETE, {}, "Queued, Locally"),
    (S.INITIALIZING, {}, "Initializing"),
    (S.DOWNLOADING, {}, "InProgress"),
    (S.COMPLETE, {}, "Completed, Succeeded"),
    (S.ABORTED, {}, "Completed, Cancelled"),
    (S.FAILED, {"fail_reason": FailReason.FILE_READ_ERROR}, "Completed, Errored"),
    (S.FAILED, {"fail_reason": "Banned"}, "Completed, Rejected"),
    (S.FAILED, {}, "Queued, Locally"),          # no reason: aioslsk retries it
])
def test_transfer_states_map_to_the_strings_the_download_engine_uses(state, kwargs, expected):
    assert slskd_state(_transfer(state, **kwargs)) == expected


def _result(username="peer", files=(("Music\\A\\01 Song.flac", 30_000_000, []),), **kw):
    items = [FileData(1, name, size, "", [Attribute(k, v) for k, v in attrs]) for name, size, attrs in files]
    return SearchResult(ticket=7, username=username, shared_items=items, **kw)


def test_search_results_become_soulseek_files_with_quality_and_availability():
    result = _result(
        files=[("Music\\A\\song.mp3", 8_000_000, [(AttributeKey.BITRATE, 320), (AttributeKey.DURATION, 200)]),
               ("Music\\A\\cover.jpg", 100_000, [])],
        has_free_slots=True, avg_speed=500_000, queue_size=3,
    )

    [f] = to_soulseek_files(result)

    assert (f.username, f.filename, f.size) == ("peer", "Music\\A\\song.mp3", 8_000_000)
    assert (f.bitrate, f.length_s) == (320, 200)
    assert (f.has_free_slot, f.upload_speed, f.queue_length) == (True, 500_000, 3)


def _register(client, ticket, query, on_results):
    """What start_search does once the server accepted the search."""
    live = slsk_client._LiveSearch(query, on_results, max_files=5000, batch_s=0.25)
    client._call(lambda: client._searches.__setitem__(ticket, live))


def _inject(client, event):
    """Deliver an aioslsk event the way its EventBus does: on the loop thread."""
    client._loop.call_soon_threadsafe(client._on_search_result, event)


@pytest.fixture
def client(tmp_path):
    c = SlskClient("me", "secret", str(tmp_path / "dl"))
    c.start_loop()
    yield c
    c.close()


def test_not_connected_reports_why_and_refuses_searches(client):
    ok, message = client.check_connection()

    assert not ok
    assert message
    with pytest.raises(SlskdConnectionError):
        client.start_search("anything")


def test_streamed_results_arrive_in_batches_on_the_callback(client):
    batches, arrived = [], threading.Event()

    def on_results(batch):
        batches.append(batch)
        arrived.set()

    _register(client, 7, "song", on_results)
    request = SearchRequest(ticket=7, query="song")
    for user in ("a", "b"):
        _inject(client, SearchResultEvent(request, _result(username=user)))

    assert arrived.wait(2)
    time.sleep(0.4)
    assert sum(len(b) for b in batches) == 2
    assert client.search_stats(7) == (2, 2)
    assert {f.username for f in client.stop_search(7)} == {"a", "b"}


def test_results_for_a_stopped_search_are_ignored(client):
    _register(client, 7, "song", None)
    client.stop_search(7)

    _inject(client, SearchResultEvent(SearchRequest(ticket=7, query="song"), _result()))
    time.sleep(0.1)

    assert client.search_stats(7) == (0, 0)


def test_blocking_search_returns_early_once_results_settle(tmp_path, monkeypatch):
    c = SlskClient("me", "secret", str(tmp_path / "dl"))
    stats = iter([(1, 5), (1, 5), (1, 5), (1, 5), (1, 5), (1, 5)])
    monkeypatch.setattr(c, "start_search", lambda q, on_results=None, **k: 1)
    monkeypatch.setattr(c, "search_stats", lambda sid: next(stats, (1, 5)))
    monkeypatch.setattr(c, "stop_search", lambda sid: ["five files"])

    started = time.monotonic()
    results = c.search("song", wait_s=20, poll_interval_s=0.05, settle_s=0.2)

    assert results == ["five files"]
    assert time.monotonic() - started < 2


def test_read_slskd_credentials(tmp_path):
    yml = tmp_path / "slskd.yml"
    yml.write_text(
        "directories:\n  downloads: /x\nsoulseek:\n  username: Someone\n  password: 'p4ss: word'\n"
        "web:\n  authentication:\n    username: admin\n    password: other\n",
        encoding="utf-8",
    )

    assert read_slskd_credentials(str(yml)) == ("Someone", "p4ss: word")


def test_read_slskd_credentials_missing_or_placeholder(tmp_path):
    assert read_slskd_credentials(str(tmp_path / "nope.yml")) is None
    yml = tmp_path / "slskd.yml"
    yml.write_text("soulseek:\n  username: \n  password: \n", encoding="utf-8")
    assert read_slskd_credentials(str(yml)) is None


def test_env_vars_named_like_aioslsk_settings_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG", "1")             # crashes stock aioslsk Settings
    monkeypatch.setenv("NETWORK", '{"listening": {"port": 1234}}')

    c = SlskClient("me", "secret", str(tmp_path / "dl"), listen_port=2234)

    assert c.listen_port == 2234
    assert slsk_client  # module imported fine


def test_folder_listing_joins_bare_filenames_and_copies_peer_stats():
    like = SoulseekFile("peer x", "music\\Manu Chao\\Clandestino\\01 - Clandestino.mp3", 10, 320, None, 3, False)
    listing = [DirectoryData("music\\Manu Chao\\Clandestino", [
        FileData(1, "01 - Clandestino.mp3", 10, "mp3", [Attribute(AttributeKey.BITRATE, 320)]),
        FileData(1, "cover.jpg", 1, "jpg", []),
        FileData(1, "music\\Manu Chao\\Clandestino\\02 - Desaparecido.mp3", 11, "mp3", []),
    ])]

    files = listing_to_files("peer x", listing, like)

    assert [f.filename for f in files] == [
        "music\\Manu Chao\\Clandestino\\01 - Clandestino.mp3",
        "music\\Manu Chao\\Clandestino\\02 - Desaparecido.mp3",
    ]
    assert all(f.queue_length == 3 and not f.has_free_slot for f in files)
    assert files[0].bitrate == 320


def test_folder_listing_with_forward_slashes():
    files = listing_to_files("u", [DirectoryData("a/b", [FileData(1, "song.opus", 3, "opus", [])])], None)
    assert [f.filename for f in files] == ["a/b/song.opus"]
