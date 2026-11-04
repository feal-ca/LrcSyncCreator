"""Tests for lyrics_providers — Megalobiz, QQ Music and NetEase lookups,
LRC clean-up (credit lines, banners) and the cross-check between sources.
All HTTP goes through a fake _http_get; nothing touches the network."""

import json

import pytest

import lyrics_providers as P


class _Resp:
    def __init__(self, status=200, js=None, text=""):
        self.status_code = status
        self._js = js
        self.text = text or (json.dumps(js) if js is not None else "")
        self.headers = {}

    def json(self):
        return self._js


@pytest.fixture
def routes(monkeypatch):
    """Install a fake _http_get answering by URL prefix; returns the call log."""
    calls = []
    table = []

    def fake_get(url, params=None, headers=None):
        calls.append((url, params or {}, headers or {}))
        for prefix, resp in table:
            if url.startswith(prefix):
                return resp(url, params or {}) if callable(resp) else resp
        return _Resp(404, {})

    monkeypatch.setattr(P, "_http_get", fake_get)
    monkeypatch.setattr(P, "_megalobiz_blocked_until", 0.0)
    return table, calls


def test_megalobiz_filters_by_duration_and_cleans_tags(routes):
    table, calls = routes
    table += [
        (f"{P.MEGALOBIZ_API}/search", _Resp(200, {"results": [
            {"id": 1, "artist": "", "title": "Clandestino", "duration": 149.28},
            {"id": 2, "artist": "shakira", "title": "clandestino", "duration": 233.46},
            {"id": 3, "artist": "Manu Chao", "title": "Clandestino (live)", "duration": 196.0}]})),
        (f"{P.MEGALOBIZ_API}/lrc/1", _Resp(200, text="[ar:]\n[ti:Clandestino]\n[re:Megalobiz]\n[00:16.30]placeholder opening line\n")),
    ]

    out = P.megalobiz_candidates("Manu Chao", "Clandestino", 148.53)

    assert [c["ref"] for c in out] == ["1"]
    assert out[0]["synced"] == "[00:16.30]placeholder opening line\n"
    assert calls[0][1]["duration"] == 149


def test_megalobiz_backs_off_after_a_429(routes, monkeypatch):
    table, calls = routes
    table.append((f"{P.MEGALOBIZ_API}/search", _Resp(429, {})))

    assert P.megalobiz_candidates("A", "B", 100.0) == []
    assert P.megalobiz_candidates("A", "B", 100.0) == []

    assert len(calls) == 1          # the second call didn't even ask


def test_netease_skips_ids_without_lyrics_and_strips_credits(routes):
    table, _calls = routes
    table += [
        ("https://music.163.com/api/cloudsearch/pc", _Resp(200, {"result": {"songs": [
            {"id": 10, "name": "Como Camarón", "ar": [{"name": "Estopa"}], "dt": 202083, "al": {"name": "Estopa"}},
            {"id": 11, "name": "Como Camarón", "ar": [{"name": "Estopa"}], "dt": 201758, "al": {"name": "X"}}]}})),
        ("https://music.163.com/api/song/lyric", lambda u, p: _Resp(200, {"lrc": {"lyric": "" if str(p["id"]) == "10" else
            "[00:00.000] 作词 : David Muñoz\n[00:00.025] 作曲 : José Muñoz\n[00:03.61]placeholder first verse\n"}})),
    ]

    out = P.netease_candidates("Estopa", "Como Camarón", 202.06)

    assert [c["ref"] for c in out] == ["11"]
    assert out[0]["synced"] == "[00:03.61]placeholder first verse\n"


def test_qq_sends_the_referer_strips_banners_and_unescapes(routes):
    table, calls = routes
    table += [
        ("https://c.y.qq.com/soso/", _Resp(200, {"data": {"song": {"list": [
            {"mid": "abc", "name": "Clandestino", "singer": [{"name": "Manu Chao"}], "interval": 149, "album": {"name": "C"}}]}}})),
        ("https://c.y.qq.com/lyric/", _Resp(200, {"retcode": 0, "lyric":
            "[ti:Clandestino]\n[00:00.00]Clandestino - Manu Chao\n[00:05.65]Lyrics by：Manu Chao\n[00:16.97]placeholder opening line&apos;\n"})),
    ]

    out = P.qq_candidates("Manu Chao", "Clandestino", 148.53)

    assert out[0]["synced"] == "[00:16.97]placeholder opening line'\n"
    assert all(h.get("Referer") == "https://y.qq.com/" for _u, _p, h in calls)


def test_a_provider_that_errors_returns_nothing(routes):
    table, _calls = routes
    table.append(("https://music.163.com/", lambda u, p: (_ for _ in ()).throw(OSError("network down"))))

    assert P.netease_candidates("A", "B", 100.0) == []


def test_clean_lrc_drops_credit_lines_anywhere_in_the_header():
    lrc = "[00:00.00] 作词 : X\n[00:00.10] 编曲 : Y\n[00:00.20]Produced by: Z\n[00:12.00]First sung line\n"
    assert P.clean_lrc(lrc) == "[00:12.00]First sung line\n"


_WORDS_A = ["amber", "bridge", "candle", "desert", "engine", "forest", "garden", "harbor",
            "island", "jacket", "kettle", "ladder"]
_WORDS_B = ["violet", "whistle", "yonder", "zephyr", "quartz", "pebble", "marble", "nickel",
            "orchid", "pillow", "riddle", "saddle"]


def _lrc(start_s, words=_WORDS_A, step=3.0):
    return "\n".join(
        f"[{int((start_s + step * i) // 60):02d}:{(start_s + step * i) % 60:05.2f}]"
        f"the {w} and the {words[(i + 3) % len(words)]} go walking"
        for i, w in enumerate(words)
    )


def test_crosscheck_agrees_on_the_same_lyrics_a_little_shifted():
    result = P.crosscheck(_lrc(10.0), _lrc(10.4))
    assert result["agree"]
    assert result["offset"] == 0.4


def test_crosscheck_rejects_lyrics_sixteen_seconds_off():
    assert not P.crosscheck(_lrc(10.0), _lrc(26.0))["agree"]


def test_crosscheck_rejects_different_lyrics():
    assert not P.crosscheck(_lrc(10.0), _lrc(10.0, words=_WORDS_B))["agree"]
