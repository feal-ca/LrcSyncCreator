"""Tests for lrc_studio/soulseek.py's query-cleanup and result-relevance
helpers (find_best_candidates and friends) — no network involved; the
SlskdClient is a stub whose .search() returns canned results per query.

The SlskdClient._request/.search tests below do exercise the real HTTP
client, against a fake requests.request rather than a live slskd.
"""

import requests

from soulseek import SlskdClient, SlskdError, SoulseekFile, clean_query_text, find_best_candidates


def _file(filename: str, bitrate: int = 320, free_slot: bool = True) -> SoulseekFile:
    return SoulseekFile(
        username="peer", filename=filename, size=5_000_000, bitrate=bitrate,
        length_s=200, queue_length=0, has_free_slot=free_slot,
    )


class _FakeClient:
    """Returns results[query] (or [] if unlisted) instead of hitting slskd."""

    def __init__(self, results_by_query: dict[str, list[SoulseekFile]]) -> None:
        self._results_by_query = results_by_query
        self.queries: list[str] = []

    def search(self, query: str) -> list[SoulseekFile]:
        self.queries.append(query)
        return self._results_by_query.get(query, [])


# ---------------------------------------------------------------------------
# clean_query_text
# ---------------------------------------------------------------------------

def test_clean_query_text_strips_parenthetical_asides():
    assert clean_query_text("Just Dance (feat. Colby O'Donis)") == "Just Dance"
    assert clean_query_text("Roxanne [Explicit]") == "Roxanne"


def test_clean_query_text_strips_known_noise_suffixes():
    assert clean_query_text("Kids - Remastered 2011") == "Kids"
    assert clean_query_text("Around the World - Radio Edit") == "Around the World"


def test_clean_query_text_leaves_plain_titles_untouched():
    assert clean_query_text("Bohemian Rhapsody") == "Bohemian Rhapsody"


def test_clean_query_text_collapses_whitespace():
    assert clean_query_text("  Song   Title  ") == "Song Title"


# ---------------------------------------------------------------------------
# find_best_candidates
# ---------------------------------------------------------------------------

def test_find_best_candidates_uses_title_only_first():
    good = _file("Radiohead/OK Computer/03 - Karma Police.mp3")
    client = _FakeClient({"Karma Police": [good]})

    result = find_best_candidates(client, "Radiohead", "Karma Police")

    assert result == [good]
    assert client.queries == ["Karma Police"]  # never fell back


def test_find_best_candidates_falls_back_to_artist_only():
    # Title-only search comes back empty; artist-only turns up a real match.
    good = _file("Daft Punk - Discovery/04 - Digital Love.mp3")
    client = _FakeClient({"Digital Love": [], "Daft Punk": [good]})

    result = find_best_candidates(client, "Daft Punk", "Digital Love")

    assert result == [good]
    assert client.queries == ["Digital Love", "Daft Punk"]


def test_find_best_candidates_falls_back_to_combined_query():
    good = _file("Some Rare Bootleg (Artist X - Title Y).flac")
    client = _FakeClient({
        "Title Y": [],
        "Artist X": [],
        "Artist X Title Y": [good],
    })

    result = find_best_candidates(client, "Artist X", "Title Y")

    assert result == [good]
    assert client.queries == ["Title Y", "Artist X", "Artist X Title Y"]


def test_find_best_candidates_filters_out_unrelated_results():
    # A free-slot, high-bitrate result for a completely different song
    # must not be returned just because it's the best-available file.
    wrong_song = _file("Someone Else - Totally Different Track.mp3", bitrate=320)
    client = _FakeClient({"Karma Police": [wrong_song]})

    result = find_best_candidates(client, "Radiohead", "Karma Police")

    assert result == []


def test_find_best_candidates_ranks_relevance_over_bitrate():
    close_match_low_bitrate = _file("Radiohead - Karma Police.mp3", bitrate=128)
    unrelated_high_bitrate = _file("Totally Unrelated Song.flac", bitrate=320)
    client = _FakeClient({
        "Karma Police": [unrelated_high_bitrate, close_match_low_bitrate],
    })

    result = find_best_candidates(client, "Radiohead", "Karma Police")

    assert result == [close_match_low_bitrate]


def test_find_best_candidates_returns_empty_when_nothing_matches():
    client = _FakeClient({})
    assert find_best_candidates(client, "Nobody", "Nothing") == []


# ---------------------------------------------------------------------------
# SlskdClient._request / .search — against a fake requests.request, not a
# live slskd. Covers the 409 ("not connected and logged in to Soulseek")
# that slskd's SearchesController returns when its own connection to the
# Soulseek server drops right as a search is posted.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self._json_data


def test_request_error_message_includes_slskd_response_body(monkeypatch):
    body = "The server connection must be connected and logged in to perform a search (currently: Disconnected)"
    monkeypatch.setattr("soulseek.requests.request", lambda *a, **k: _FakeResponse(409, text=body))

    client = SlskdClient("http://localhost:5030", "key")
    try:
        client._request("POST", "/api/v0/searches", json={"searchText": "x"})
        raised = None
    except SlskdError as exc:
        raised = exc

    assert raised is not None
    assert "connected and logged in" in str(raised)


def test_search_retries_once_after_transient_409_and_succeeds(monkeypatch):
    calls = {"post": 0}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        if method == "POST" and url.endswith("/api/v0/searches"):
            calls["post"] += 1
            if calls["post"] == 1:
                return _FakeResponse(409, text="not connected and logged in")
            return _FakeResponse(200, json_data={"id": "search-1"})
        if method == "GET" and url.endswith("/api/v0/application"):
            return _FakeResponse(200, json_data={"server": {"isLoggedIn": True}})
        if method == "GET" and url.endswith("/api/v0/searches/search-1"):
            return _FakeResponse(200, json_data={"isComplete": True})
        if method == "GET" and url.endswith("/api/v0/searches/search-1/responses"):
            return _FakeResponse(200, json_data=[])
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr("soulseek.requests.request", fake_request)

    client = SlskdClient("http://localhost:5030", "key")
    results = client.search("some query")

    assert results == []
    assert calls["post"] == 2  # first attempt 409'd, retry succeeded


def test_search_does_not_retry_on_non_409_errors(monkeypatch):
    calls = {"post": 0}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        if method == "POST" and url.endswith("/api/v0/searches"):
            calls["post"] += 1
            return _FakeResponse(500, text="boom")
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr("soulseek.requests.request", fake_request)

    client = SlskdClient("http://localhost:5030", "key")
    try:
        client.search("some query")
        raised = True
    except SlskdError:
        raised = False

    assert raised is False
    assert calls["post"] == 1  # no retry for a non-409 failure
