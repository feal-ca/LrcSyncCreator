"""Tests for lrc_studio/soulseek.py's query-cleanup and result-relevance
helpers (find_best_candidates and friends) — no network involved; the
client is a stub whose .search() returns canned results per query.
"""

from soulseek import SoulseekFile, clean_query_text, find_best_candidates


def _file(
    filename: str, bitrate: int | None = 320, free_slot: bool = True,
    size: int = 5_000_000, upload_speed: int | None = None, queue_length: int = 0,
    length_s: int | None = 200,
) -> SoulseekFile:
    return SoulseekFile(
        username="peer", filename=filename, size=size, bitrate=bitrate,
        length_s=length_s, queue_length=queue_length, has_free_slot=free_slot,
        upload_speed=upload_speed,
    )


class _FakeClient:
    """Returns results[query] (or [] if unlisted) instead of hitting slskd."""

    def __init__(self, results_by_query: dict[str, list[SoulseekFile]]) -> None:
        self._results_by_query = results_by_query
        self.queries: list[str] = []

    def search(self, query: str, **_kwargs) -> list[SoulseekFile]:
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

def test_find_best_candidates_tries_artist_plus_title_first():
    # Title alone floods for common names ("Your Love": 9,000+ files from
    # 250 peers, past slskd's response limit), so the precise query goes first.
    good = _file("Radiohead/OK Computer/03 - Karma Police.mp3")
    client = _FakeClient({"Radiohead Karma Police": [good]})

    result = find_best_candidates(client, "Radiohead", "Karma Police")

    assert result == [good]
    assert client.queries == ["Radiohead Karma Police"]  # never fell back


def test_find_best_candidates_falls_back_to_artist_only():
    # Title-only search comes back empty; artist-only turns up a real match.
    good = _file("Daft Punk - Discovery/04 - Digital Love.mp3")
    client = _FakeClient({"Digital Love": [], "Daft Punk": [good]})

    result = find_best_candidates(client, "Daft Punk", "Digital Love")

    assert result == [good]
    assert client.queries == ["Daft Punk Digital Love", "Digital Love", "Daft Punk"]


def test_find_best_candidates_falls_back_to_title_only():
    # A peer that spells the artist differently in its folder names.
    good = _file("AC-DC/Back in Black/06 - Back In Black.mp3")
    client = _FakeClient({"AC DC Back In Black": [], "Back In Black": [good]})

    result = find_best_candidates(client, "AC/DC", "Back In Black - Remastered")

    assert result == [good]
    assert client.queries == ["AC DC Back In Black", "Back In Black"]


def test_build_queries_uses_primary_artist_and_drops_punctuation():
    from soulseek import build_queries

    assert build_queries("La Fúmiga, The Tyets", "Cap. 2: Havia de Passar") == [
        "La Fúmiga Cap 2 Havia de Passar", "Cap 2 Havia de Passar", "La Fúmiga",
    ]
    assert build_queries("Gims feat. Maluma", "Hola Señorita (Remix)")[0] == "Gims Hola Señorita"
    assert build_queries("Paris est Gothique", "Paris est Gothique") == ["Paris est Gothique"]
    assert build_queries("L'Algérino", "Les Menottes")[0] == "L'Algérino Les Menottes"


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


def test_ranking_prefers_fast_320_mp3_over_slow_flac():
    # Bitrate isn't everything: once a file is good enough, how soon it
    # will actually arrive matters more than squeezing out more quality.
    slow_flac = _file("Radiohead - Karma Police.flac", bitrate=None,
                      size=30_000_000, upload_speed=150_000)
    fast_mp3 = _file("Radiohead - Karma Police.mp3", bitrate=320,
                     size=9_000_000, upload_speed=2_000_000)
    client = _FakeClient({"Karma Police": [slow_flac, fast_mp3]})

    result = find_best_candidates(client, "Radiohead", "Karma Police")

    assert result == [fast_mp3, slow_flac]


def test_ranking_puts_low_bitrate_behind_any_acceptable_quality():
    fast_128 = _file("Radiohead - Karma Police.mp3", bitrate=128, upload_speed=10_000_000)
    slow_256 = _file("Radiohead - Karma Police.mp3", bitrate=256, upload_speed=200_000)
    client = _FakeClient({"Karma Police": [fast_128, slow_256]})

    result = find_best_candidates(client, "Radiohead", "Karma Police")

    assert result == [slow_256, fast_128]


def test_ranking_prefers_free_slot_over_speed():
    queued_fast = _file("Radiohead - Karma Police.mp3", free_slot=False,
                        queue_length=12, upload_speed=10_000_000)
    free_slow = _file("Radiohead - Karma Police.mp3", upload_speed=300_000)
    client = _FakeClient({"Karma Police": [queued_fast, free_slow]})

    result = find_best_candidates(client, "Radiohead", "Karma Police")

    assert result == [free_slow, queued_fast]


def test_ranking_ignores_small_relevance_differences_between_strong_matches():
    # Both clearly name the right song (a misspelled folder scores ~94
    # instead of 100) — that much fuzzy-score noise shouldn't beat a free slot.
    noisy_free = _file("Radiohed/OK Computer/06 - Karma Police.mp3")
    clean_queued = _file("Radiohead - Karma Police.mp3", free_slot=False, queue_length=30)
    client = _FakeClient({"Karma Police": [clean_queued, noisy_free]})

    result = find_best_candidates(client, "Radiohead", "Karma Police")

    assert result[0] is noisy_free


def test_relevance_ignores_extension_and_separator_noise():
    from soulseek import _relevance_score

    assert _relevance_score("music\\GIMS\\Ma beauté.flac", "GIMS", "Ma beauté") == 100
    assert _relevance_score("GIMS\\01-ma_beauté.mp3", "GIMS", "Ma beauté") == 100
    # A different song sharing a word stays below the strong-match band.
    assert _relevance_score("GIMS\\beauté fatale.mp3", "GIMS", "Ma beauté") < 90


def test_quality_ok_judges_by_format_and_bitrate():
    assert _file("a.flac", bitrate=None).quality_ok is True
    assert _file("a.mp3", bitrate=320).quality_ok is True
    assert _file("a.mp3", bitrate=192).quality_ok is True
    assert _file("a.mp3", bitrate=128).quality_ok is False
    assert _file("a.opus", bitrate=160).quality_ok is True
    # No reported bitrate: estimated from size and duration (5 MB / 200 s ~ 200 kbps).
    assert _file("a.mp3", bitrate=None).quality_ok is True
    assert _file("a.mp3", bitrate=None, length_s=None).quality_ok is None


def test_find_best_candidates_returns_empty_when_nothing_matches():
    client = _FakeClient({})
    assert find_best_candidates(client, "Nobody", "Nothing") == []
