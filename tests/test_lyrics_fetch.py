"""Tests for lyrics_fetch.py. No real network: _http_get and syncedlyrics.search
are replaced with in-memory fakes."""

import pytest

import lyrics_fetch as lf
from lyrics_fetch import LyricsCandidate, TrackInfo


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_lrc(n=12, step=10.0):
    lines = []
    for i in range(n):
        t = 5 + i * step
        lines.append(f"[{int(t // 60):02d}:{t % 60:05.2f}] line {i}")
    return "\n".join(lines)


GOOD_LRC = make_lrc()          # last timestamp 115 s
LONG_LRC = make_lrc(n=30)      # last timestamp 295 s


def item(id_, title="Clandestino", artist="Manu Chao", duration=150.0,
         synced=GOOD_LRC, plain="plain words", album="Clandestino",
         instrumental=False):
    return {
        "id": id_, "trackName": title, "artistName": artist,
        "albumName": album, "duration": duration, "instrumental": instrumental,
        "plainLyrics": plain, "syncedLyrics": synced,
    }


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class FakeLrclib:
    """Routes by URL suffix; records every call."""

    def __init__(self, get=None, search=None, fail=False):
        self.get_payload = get
        self.search_payload = search or []
        self.fail = fail
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if self.fail:
            raise requests_error()
        if url.endswith("/get"):
            if self.get_payload is None:
                return FakeResponse(status=404)
            return FakeResponse(self.get_payload)
        if url.endswith("/search"):
            return FakeResponse(self.search_payload)
        raise AssertionError(f"unexpected url {url}")

    def n(self, suffix):
        return sum(1 for u, _ in self.calls if u.endswith(suffix))


def requests_error():
    import requests
    return requests.ConnectionError("offline")


@pytest.fixture
def no_syncedlyrics(monkeypatch):
    calls = []

    def fake(term, **kw):
        calls.append((term, kw))
        return None

    monkeypatch.setattr(lf.syncedlyrics, "search", fake)
    return calls


INFO = TrackInfo("Manu Chao", "Clandestino", "Clandestino", 150.2)


def shifted(lrc, seconds):
    out = []
    for line in lrc.splitlines():
        t = int(line[1:3]) * 60 + float(line[4:9]) + seconds
        out.append(f"[{int(t // 60):02d}:{t % 60:05.2f}]{line[10:]}")
    return "\n".join(out)


def provider_cand(source, synced=GOOD_LRC, title="Clandestino", artist="Manu Chao", duration=150.0, ref="p1"):
    return {"source": source, "artist": artist, "title": title, "album": "", "duration_s": duration,
            "synced": synced, "plain": None, "ref": ref}


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    """Other lyrics sources answer nothing unless a test says otherwise;
    returns a dict to fill: {"megalobiz": [...], "qq": [...], ...} and the call log."""
    answers: dict[str, list] = {}
    calls: list[str] = []
    for name in ("megalobiz", "qq", "netease", "ytmusic"):
        def fake(artist, title, duration_s=None, max_fetch=2, _name=name, **_kw):
            calls.append(_name)
            return [dict(c) for c in answers.get(_name, [])]
        monkeypatch.setattr(lf.lyrics_providers, f"{name}_candidates", fake)
    return answers, calls


# --------------------------------------------------------------------------
# Filename / title / artist parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stem, expected", [
    ("01 - Manu Chao - Clandestino", ("Manu Chao", "Clandestino")),
    ("Madonna - Like a Virgin", ("Madonna", "Like a Virgin")),
    ("05. Song", ("", "Song")),
    ("3_Song", ("", "Song")),
    ("07) Artist - Title", ("Artist", "Title")),
    ("01 - Elyanna - Elyanna 2 - Ghareeb Alay", ("Elyanna", "Ghareeb Alay")),
    ("99 Problems", ("", "99 Problems")),
    ("Just A Title", ("", "Just A Title")),
])
def test_parse_filename(stem, expected):
    assert lf.parse_filename(stem) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Song (feat. Someone)", "Song"),
    ("Song (ft. Someone)", "Song"),
    ("Song [feat. Someone]", "Song"),
    ("Song feat. Someone", "Song"),
    ("Song (Remastered 2011)", "Song"),
    ("Song [Explicit]", "Song"),
    ("Song (Radio Edit)", "Song"),
    ("Song (Live)", "Song"),
    ("Song (Official Video)", "Song"),
    ("Song - Remastered 2011", "Song"),
    ("Song - 2011 Remaster", "Song"),
    ("Song - Radio Edit", "Song"),
    ("Song - Single Version", "Song"),
    ("Song - Live", "Song"),
    ("Song - Mono", "Song"),
    ("Song (feat. X) (Remastered 2011) [Explicit]", "Song"),
    ("Song (Part 2)", "Song (Part 2)"),
    ("Song  (Part 2)  (Live)", "Song (Part 2)"),
    ("Live Forever", "Live Forever"),
    ("(Live)", "(Live)"),
])
def test_clean_title(raw, expected):
    assert lf.clean_title(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Rosalía, J Balvin", "Rosalía"),
    ("Calle 13 feat. Silvio Rodríguez", "Calle 13"),
    ("Pimpinela", "Pimpinela"),
    ("Ella y Él", "Ella y Él"),
    ("Bad Bunny x Jhay Cortez", "Bad Bunny"),
    ("Artist A & Artist B", "Artist A"),
    ("Artist A; Artist B", "Artist A"),
    ("Artist A ft. Artist B", "Artist A"),
    ("Artist A FEATURING Artist B", "Artist A"),
    ("Artist A / Artist B", "Artist A"),
    ("AC/DC", "AC/DC"),
])
def test_primary_artist(raw, expected):
    assert lf.primary_artist(raw) == expected


# --------------------------------------------------------------------------
# read_track_info
# --------------------------------------------------------------------------

def test_read_track_info_falls_back_to_filename(tmp_path):
    f = tmp_path / "01 - Manu Chao - Clandestino.mp3"
    f.write_bytes(b"this is not audio")
    info = lf.read_track_info(str(f))
    assert (info.artist, info.title) == ("Manu Chao", "Clandestino")
    assert info.duration_s is None


def test_read_track_info_missing_file_does_not_raise(tmp_path):
    info = lf.read_track_info(str(tmp_path / "Madonna - Like a Virgin.flac"))
    assert (info.artist, info.title) == ("Madonna", "Like a Virgin")


# --------------------------------------------------------------------------
# is_plausible_synced / ranking
# --------------------------------------------------------------------------

def test_plausible_accepts_normal_lrc():
    assert lf.is_plausible_synced(GOOD_LRC, 150.0)
    assert lf.is_plausible_synced(GOOD_LRC, None)
    assert lf.is_plausible_synced("[00:05.123] a\n[00:10.500] b\n[00:20.000] c", 30)


def test_plausible_rejects_bad_lrc():
    assert not lf.is_plausible_synced("", 100)
    assert not lf.is_plausible_synced("[00:05.00] a\n[00:10.00] b", 100)       # too few
    assert not lf.is_plausible_synced("[00:00.00] a\n[00:00.00] b\n[00:00.00] c", 100)
    assert not lf.is_plausible_synced("just plain text\nno stamps", 100)
    assert not lf.is_plausible_synced(LONG_LRC, 150.0)                         # past the end


def test_candidate_matches_checks_title_artist_duration():
    good = LyricsCandidate("lrclib", "Manu Chao", "Clandestino", "", 152.0, GOOD_LRC, None)
    far = LyricsCandidate("lrclib", "Manu Chao", "Clandestino", "", 190.0, GOOD_LRC, None)
    other = LyricsCandidate("lrclib", "Manu Chao", "Bongo Bong", "", 150.0, GOOD_LRC, None)
    wrong_artist = LyricsCandidate("lrclib", "Metallica", "Clandestino", "", 150.0, GOOD_LRC, None)
    assert lf.candidate_matches(good, INFO)
    assert not lf.candidate_matches(far, INFO)
    assert lf.candidate_matches(far, INFO, max_duration_diff_s=60)
    assert not lf.candidate_matches(other, INFO)
    assert not lf.candidate_matches(wrong_artist, INFO)


def test_rank_prefers_duration_and_synced():
    near = LyricsCandidate("lrclib", "Manu Chao", "Clandestino (Live)", "", 151.0, GOOD_LRC, None)
    off = LyricsCandidate("lrclib", "Manu Chao", "Clandestino", "", 180.0, GOOD_LRC, None)
    plain_only = LyricsCandidate("lrclib", "Manu Chao", "Clandestino", "", 151.0, None, "words")
    ranked = lf.rank_candidates([off, plain_only, near], INFO)
    assert ranked[0] is near
    assert ranked[-1] is off


# --------------------------------------------------------------------------
# lrclib_get / lrclib_search
# --------------------------------------------------------------------------

def test_lrclib_get_params_and_parsing(monkeypatch):
    fake = FakeLrclib(get=item(7))
    monkeypatch.setattr(lf, "_http_get", fake)
    cand = lf.lrclib_get(INFO)
    assert cand and cand.ref == "7" and cand.has_synced and cand.duration_s == 150.0
    url, params = fake.calls[0]
    assert url == "https://lrclib.net/api/get"
    assert params == {"track_name": "Clandestino", "artist_name": "Manu Chao",
                      "album_name": "Clandestino", "duration": 150}


def test_lrclib_get_needs_duration_and_skips_instrumental(monkeypatch):
    fake = FakeLrclib(get=item(1, instrumental=True))
    monkeypatch.setattr(lf, "_http_get", fake)
    assert lf.lrclib_get(TrackInfo("A", "B", "", None)) is None
    assert fake.calls == []
    assert lf.lrclib_get(INFO) is None


def test_lrclib_get_and_search_swallow_errors(monkeypatch):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(fail=True))
    assert lf.lrclib_get(INFO) is None
    assert lf.lrclib_search(q="x") == []
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None))
    assert lf.lrclib_get(INFO) is None  # 404


# --------------------------------------------------------------------------
# fetch_best_lyrics
# --------------------------------------------------------------------------

def test_exact_get_hit_short_circuits(monkeypatch, no_syncedlyrics):
    fake = FakeLrclib(get=item(1))
    monkeypatch.setattr(lf, "_http_get", fake)
    res = lf.fetch_best_lyrics(INFO)
    assert res and res.synced and res.source == "lrclib" and res.text.rstrip("\n") == GOOD_LRC
    assert fake.n("/search") == 0
    assert no_syncedlyrics == []


def test_cleaned_get_retry_for_messy_tags(monkeypatch, no_syncedlyrics):
    messy = TrackInfo("Calle 13 feat. Silvio", "Song (Remastered 2011)", "Album", 150.0)

    class Fake(FakeLrclib):
        def __call__(self, url, params):
            if url.endswith("/get") and params["track_name"] == "Song":
                self.calls.append((url, dict(params)))
                return FakeResponse(item(3, title="Song", artist="Calle 13"))
            return super().__call__(url, params)

    fake = Fake(get=None)
    monkeypatch.setattr(lf, "_http_get", fake)
    res = lf.fetch_best_lyrics(messy)
    assert res and res.candidate.ref == "3"
    clean_call = [p for u, p in fake.calls if u.endswith("/get")][-1]
    assert clean_call == {"track_name": "Song", "artist_name": "Calle 13", "duration": 150}


def test_duration_filter_prefers_close_candidate(monkeypatch, no_syncedlyrics):
    close = item(10, title="Clandestino (Version)", duration=152.0)
    far = item(11, title="Clandestino", duration=180.0)
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None, search=[far, close]))
    res = lf.fetch_best_lyrics(INFO)
    assert res and res.synced and res.candidate.ref == "10"


def test_candidates_outside_duration_window_are_ignored(monkeypatch, no_syncedlyrics):
    far = item(11, duration=190.0)
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None, search=[far]))
    assert lf.fetch_best_lyrics(INFO, allow_plain=False) is None


def test_implausible_lyrics_rejected_then_next_step_used(monkeypatch, providers, no_syncedlyrics):
    # Exact hit has timestamps running to 295 s on a 150 s track.
    bad = item(1, synced=LONG_LRC)
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=bad, search=[bad]))
    answers, _calls = providers
    answers["megalobiz"] = [provider_cand("megalobiz")]

    res = lf.fetch_best_lyrics(INFO)

    assert res and res.source == "megalobiz" and res.synced


def test_search_results_deduped_across_queries(monkeypatch):
    fake = FakeLrclib(get=None, search=[item(5), item(5)])
    monkeypatch.setattr(lf, "_http_get", fake)
    assert [c.ref for c in lf.search_candidates("Manu Chao", "Clandestino", 150)] == ["5"]
    assert fake.n("/search") == 2


def test_plain_fallback_is_flagged_unsynced(monkeypatch, no_syncedlyrics):
    only_plain = item(2, synced=None, plain="la la la")
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None, search=[only_plain]))
    res = lf.fetch_best_lyrics(INFO)
    assert res and not res.synced and res.text == "la la la" and res.source == "lrclib"
    assert lf.fetch_best_lyrics(INFO, allow_plain=False) is None


def test_plain_fallback_via_syncedlyrics_genius_only(monkeypatch):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None))
    asked = []

    def fake_search(term, **kw):
        asked.append(kw)
        return "just words" if kw.get("plain_only") else None

    monkeypatch.setattr(lf.syncedlyrics, "search", fake_search)
    res = lf.fetch_best_lyrics(INFO)
    assert res and not res.synced and res.source == "syncedlyrics"
    assert all(kw.get("providers") == ["Genius"] for kw in asked)   # never Musixmatch (decoys)


def test_total_failure_returns_none(monkeypatch):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(fail=True))

    def boom(term, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(lf.syncedlyrics, "search", boom)
    assert lf.fetch_best_lyrics(INFO) is None


def test_unknown_duration_skips_get_and_still_searches(monkeypatch, no_syncedlyrics):
    fake = FakeLrclib(get=item(1), search=[item(9)])
    monkeypatch.setattr(lf, "_http_get", fake)
    res = lf.fetch_best_lyrics(TrackInfo("Manu Chao", "Clandestino"))
    assert res and res.candidate.ref == "9"
    assert fake.n("/get") == 0


# --------------------------------------------------------------------------
# search_candidates
# --------------------------------------------------------------------------

def test_search_candidates_ranked_unfiltered_and_capped(monkeypatch):
    results = [item(i, title=f"Other {i}", artist="Someone", duration=60.0 + i)
               for i in range(40)]
    results.append(item(99, duration=150.0))
    fake = FakeLrclib(search=results)
    monkeypatch.setattr(lf, "_http_get", fake)
    out = lf.search_candidates("Manu Chao", "Clandestino", 150.0)
    assert len(out) == 30
    assert out[0].ref == "99"
    assert {"track_name": "Clandestino", "artist_name": "Manu Chao"} in [p for _, p in fake.calls]
    assert {"q": "Manu Chao Clandestino"} in [p for _, p in fake.calls]



# --------------------------------------------------------------------------
# More sources: confirm, overrule, fill in
# --------------------------------------------------------------------------

def test_lrclib_hit_confirmed_by_megalobiz_is_kept(monkeypatch, providers, no_syncedlyrics):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=item(1)))
    answers, calls = providers
    answers["megalobiz"] = [provider_cand("megalobiz", synced=shifted(GOOD_LRC, 0.3))]

    res = lf.fetch_best_lyrics(INFO)

    assert res.source == "lrclib"
    assert calls == ["megalobiz"]            # agreement: no need to ask anyone else


def test_lrclib_hit_with_nothing_to_compare_is_still_used(monkeypatch, providers, no_syncedlyrics):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=item(1)))

    res = lf.fetch_best_lyrics(INFO)

    assert res.source == "lrclib" and res.synced


def test_lrclib_overruled_when_two_other_sources_agree_on_other_timing(monkeypatch, providers, no_syncedlyrics):
    late = shifted(GOOD_LRC, 16.0)                       # LRCLIB entry 16 s late
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=item(1, synced=late)))
    answers, calls = providers
    answers["megalobiz"] = [provider_cand("megalobiz")]
    answers["qq"] = [provider_cand("qq", synced=shifted(GOOD_LRC, 0.4))]

    res = lf.fetch_best_lyrics(INFO)

    assert res.source == "megalobiz"
    assert "qq" in calls


def test_other_sources_fill_in_when_lrclib_has_nothing(monkeypatch, providers, no_syncedlyrics):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None))
    answers, calls = providers
    answers["netease"] = [provider_cand("netease")]

    res = lf.fetch_best_lyrics(INFO)

    assert res.source == "netease" and res.synced
    assert calls[:3] == ["megalobiz", "qq", "netease"]


def test_youtube_music_only_when_turned_on(monkeypatch, providers, no_syncedlyrics):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None))
    answers, calls = providers
    answers["ytmusic"] = [provider_cand("ytmusic")]

    assert lf.fetch_best_lyrics(INFO, allow_plain=False) is None
    assert "ytmusic" not in calls

    res = lf.fetch_best_lyrics(INFO, use_ytmusic=True)
    assert res.source == "ytmusic"


def test_a_different_song_from_another_source_is_ignored(monkeypatch, providers, no_syncedlyrics):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None))
    answers, _calls = providers
    answers["megalobiz"] = [provider_cand("megalobiz", title="Something Else Entirely", artist="Nobody")]

    assert lf.fetch_best_lyrics(INFO, allow_plain=False) is None


def test_credit_lines_never_reach_the_saved_lyrics(monkeypatch, providers, no_syncedlyrics):
    credited = "[00:00.00] 作词 : Someone\n[00:00.10] 作曲 : Someone\n" + GOOD_LRC
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=item(1, synced=credited)))

    res = lf.fetch_best_lyrics(INFO)

    assert "作词" not in res.text and "作曲" not in res.text
    assert res.text.splitlines()[0].endswith("line 0")


def test_without_an_artist_the_duration_must_be_close(monkeypatch, providers, no_syncedlyrics):
    info = TrackInfo("", "Hello", "", 200.0)
    far = item(3, title="Hello", artist="Someone Else", duration=206.0)
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None, search=[far]))
    assert lf.fetch_best_lyrics(info, allow_plain=False) is None

    near = item(4, title="Hello", artist="Someone Else", duration=201.0)
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(get=None, search=[near]))
    assert lf.fetch_best_lyrics(info, allow_plain=False).candidate.ref == "4"


def test_picker_shows_every_source_in_one_list(monkeypatch, providers):
    monkeypatch.setattr(lf, "_http_get", FakeLrclib(search=[item(7)]))
    answers, _calls = providers
    answers["megalobiz"] = [provider_cand("megalobiz", ref="m")]
    answers["qq"] = [provider_cand("qq", ref="q")]

    out = lf.search_candidates("Manu Chao", "Clandestino", 150.0)

    assert {c.source for c in out} == {"lrclib", "megalobiz", "qq"}


def test_a_network_blip_is_retried_once(monkeypatch):
    import requests

    attempts = []

    def flaky_get(url, params=None, headers=None, timeout=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise requests.ConnectionError("Temporary failure in name resolution")
        return FakeResponse({"ok": True})

    monkeypatch.setattr(lf.requests, "get", flaky_get)
    monkeypatch.setattr(lf.time, "sleep", lambda s: None)

    resp = lf._http_get("https://lrclib.net/api/get", {})

    assert resp.json() == {"ok": True}
    assert len(attempts) == 2


def test_providers_retry_a_network_blip_too(monkeypatch):
    import requests

    import lyrics_providers as P

    attempts = []

    def flaky_get(url, params=None, headers=None, timeout=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise requests.Timeout("slow")
        return FakeResponse({"ok": True})

    monkeypatch.setattr(P.requests, "get", flaky_get)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)

    assert P._http_get("https://example.invalid/x").json() == {"ok": True}
    assert len(attempts) == 2
