"""Tests for lrc_studio/soulseek.py's query-cleanup and result-relevance
helpers (find_best_candidates and friends) — no network involved; the
SlskdClient is a stub whose .search() returns canned results per query.
"""

from soulseek import SoulseekFile, clean_query_text, find_best_candidates


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
