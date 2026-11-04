"""lyrics_providers.py — synced-lyrics sources besides LRCLIB, and the
tools to compare them.

- Megalobiz: official public API, matches by duration (30 requests/min
  without a key; a 429 pauses it for a while).
- QQ Music and NetEase: large catalogues with professional timing; their
  credit lines and "Title - Artist" banners are stripped.
- YouTube Music (optional, off by default): the only extra source with
  much Catalan/Spanish long-tail coverage, but it goes through an
  unofficial API, so it's used only when the user turns it on and the
  ``ytmusicapi`` package is installed.

Every provider returns candidate dicts shaped like
``lyrics_fetch.LyricsCandidate`` and never raises; ``crosscheck`` says
whether two LRCs carry the same lyrics on the same clock.

Qt-free. All HTTP goes through ``_http_get`` (throttled to one request per
second per host) so tests can replace it.
"""

from __future__ import annotations

import functools
import html
import re
import statistics
import threading
import time
import unicodedata
from urllib.parse import urlparse

import requests
from rapidfuzz import fuzz

USER_AGENT = "LRC Studio (https://github.com/feal-ca/LrcSync)"
TIMEOUT_S = 10


_MIN_INTERVAL_S = 1.0
_last_request: dict[str, float] = {}
_throttle_lock = threading.Lock()


def _http_get(url: str, params: dict | None = None, headers: dict | None = None) -> requests.Response:
    host = urlparse(url).netloc
    with _throttle_lock:
        wait = _last_request.get(host, 0.0) + _MIN_INTERVAL_S - time.monotonic()
        _last_request[host] = time.monotonic() + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt in (1, 2):      # one retry: a DNS or timeout blip shouldn't cost a song its lyrics
        try:
            return requests.get(url, params=params, headers=h, timeout=TIMEOUT_S)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
            time.sleep(1.5)
    raise AssertionError("unreachable")


def _never_raises(fn):
    """A provider that fails (network, odd JSON) just has nothing to offer."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return []
    return wrapper


def _cand(source, artist, title, duration_s, synced, ref, album=""):
    return {"source": source, "artist": artist or "", "title": title or "", "album": album or "",
            "duration_s": duration_s, "synced": synced or None, "plain": None, "ref": str(ref)}


# ---------------------------------------------------------------- matching ---
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def _plausible(artist, title, duration_s, c_artist, c_title, c_dur, max_diff=8.0) -> bool:
    """Cheap pre-filter so we only download lyrics for likely matches."""
    if duration_s is not None and c_dur is not None and abs(c_dur - duration_s) > max_diff:
        return False
    if fuzz.token_set_ratio(_norm(title), _norm(c_title)) < 80:
        # user uploads often put "Artist - Title" in the title field
        if fuzz.token_set_ratio(_norm(f"{artist} {title}"), _norm(c_title)) < 85:
            return False
    return not c_artist or fuzz.token_set_ratio(_norm(artist), _norm(c_artist)) >= 70


def _by_duration(items, duration_s):
    return sorted(items, key=lambda c: abs((c["duration_s"] or 0) - duration_s) if duration_s else 0)


# ---------------------------------------------------------- LRC clean-up ---
_ID_TAG = re.compile(r"^\s*\[(?:ti|ar|al|au|by|re|ve|id|length|hash|sign|qq|total|kana|language)\s*:[^\]]*\]\s*$", re.I)
_TS = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_CREDIT = re.compile(
    r"^\s*(?:作词|作曲|编曲|制作人|词|曲|编|混音|母带|录音|和声|吉他|贝斯|鼓|键盘|弦乐|监制|出品|发行|"
    r"op|sp|lyrics?(?: by)?|lyricist|composed by|composer|written by|writers?|arranged by|arranger|"
    r"produced by|producers?|mixed by|mastered by|recorded by|artist|song|album|title)\s*[:：]", re.I)


def _t(m) -> float:
    frac = m.group(3) or "0"
    return int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / 10 ** len(frac)


def clean_lrc(lrc: str, artist: str = "", title: str = "") -> str:
    """Drop ID tags, credit lines and 'Title - Artist' banners before the first sung line."""
    out, in_header = [], True
    for line in (lrc or "").replace("\r", "").split("\n"):
        if _ID_TAG.match(line):
            continue
        m = _TS.match(line.strip())
        text = _TS.sub("", line).replace("\u266a", "").strip()   # YT Music uses a lone note for intros
        if in_header and m:
            banner = " - " in text and _t(m) < 1.0 and fuzz.token_set_ratio(
                _norm(text), _norm(f"{title} {artist}")) >= 80
            if not text or _CREDIT.match(text) or banner:
                continue
            in_header = False
        out.append(line.replace("\u266a", "").rstrip())
    return "\n".join(out).strip() + "\n"


# --------------------------------------------------------------- Megalobiz ---
MEGALOBIZ_API = "https://api.megalobiz.com/api/public/v1"
_megalobiz_blocked_until = 0.0


@_never_raises
def megalobiz_candidates(artist, title, duration_s=None, max_fetch=2):
    global _megalobiz_blocked_until
    if time.monotonic() < _megalobiz_blocked_until:
        return []
    params = {"title": title, "artist": artist, "limit": 10}
    if duration_s:
        params["duration"] = round(duration_s)
    resp = _http_get(f"{MEGALOBIZ_API}/search", params)
    if resp.status_code == 429:
        try:
            retry = float(resp.headers.get("Retry-After", 60))
        except (TypeError, ValueError):
            retry = 60.0
        _megalobiz_blocked_until = time.monotonic() + max(retry, 1.0)
        return []
    if resp.status_code != 200:
        return []
    hits = [_cand("megalobiz", r.get("artist"), r.get("title"), r.get("duration"), None, r["id"])
            for r in resp.json().get("results", [])]
    hits = [c for c in hits if _plausible(artist, title, duration_s, c["artist"], c["title"], c["duration_s"])]
    out = []
    for c in _by_duration(hits, duration_s)[:max_fetch]:
        r = _http_get(f"{MEGALOBIZ_API}/lrc/{c['ref']}", {"format": "lrc"})
        if r.status_code == 200 and r.text.strip():
            c["synced"] = clean_lrc(r.text, artist, title)
            out.append(c)
    return out


# ----------------------------------------------------------------- NetEase ---
@_never_raises
def netease_candidates(artist, title, duration_s=None, max_fetch=2):
    resp = _http_get("https://music.163.com/api/cloudsearch/pc",
                     {"s": f"{artist} {title}", "type": 1, "limit": 10, "offset": 0})
    songs = ((resp.json().get("result") or {}).get("songs") or []) if resp.status_code == 200 else []
    hits = [_cand("netease", " / ".join(a.get("name", "") for a in s.get("ar", [])), s.get("name"),
                  (s.get("dt") or 0) / 1000 or None, None, s["id"], (s.get("al") or {}).get("name"))
            for s in songs]
    hits = [c for c in hits if _plausible(artist, title, duration_s, c["artist"], c["title"], c["duration_s"])]
    out = []
    for c in _by_duration(hits, duration_s)[:3]:      # many ids have no lyric
        if len(out) >= max_fetch:
            break
        r = _http_get("https://music.163.com/api/song/lyric", {"id": c["ref"], "lv": 1, "tv": -1})
        lrc = ((r.json().get("lrc") or {}).get("lyric") or "") if r.status_code == 200 else ""
        if _TS.search(lrc):             # many ids return "" (no lyric); try the next id
            c["synced"] = clean_lrc(lrc, artist, title)
            out.append(c)
    return out


# ---------------------------------------------------------------- QQ Music ---
_QQ_HEADERS = {"Referer": "https://y.qq.com/"}


@_never_raises
def qq_candidates(artist, title, duration_s=None, max_fetch=2):
    resp = _http_get("https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                     {"w": f"{artist} {title}", "format": "json", "p": 1, "n": 10, "new_json": 1, "cr": 1},
                     _QQ_HEADERS)
    try:
        songs = resp.json()["data"]["song"]["list"]
    except Exception:
        return []
    hits = [_cand("qq", " / ".join(a.get("name", "") for a in s.get("singer", [])), s.get("name"),
                  float(s["interval"]) if s.get("interval") else None, None, s["mid"],
                  (s.get("album") or {}).get("name")) for s in songs]
    hits = [c for c in hits if _plausible(artist, title, duration_s, c["artist"], c["title"], c["duration_s"])]
    out = []
    for c in _by_duration(hits, duration_s)[:3]:
        if len(out) >= max_fetch:
            break
        r = _http_get("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                      {"songmid": c["ref"], "format": "json", "nobase64": 1, "g_tk": 5381}, _QQ_HEADERS)
        j = r.json() if r.status_code == 200 else {}
        lrc = html.unescape(j.get("lyric") or "")   # retcode -1901 = no lyric
        if _TS.search(lrc):
            c["synced"] = clean_lrc(lrc, artist, title)
            out.append(c)
    return out


# ----------------------------------------------------------- YouTube Music ---
# Uses ytmusicapi (MIT, pure Python, depends only on requests). Pass your own
# requests.Session to make it testable/throttleable. Optional, opt-in source:
# private InnerTube API, timed lyrics only via the Android client context.
_ytm = None


def _ytmusic(session=None):
    global _ytm
    if _ytm is None:                       # first call fetches a visitor id
        from ytmusicapi import YTMusic
        _ytm = YTMusic(language="en", requests_session=session)
    return _ytm


def ytmusic_available() -> bool:
    try:
        import ytmusicapi  # noqa: F401
    except ImportError:
        return False
    return True


@_never_raises
def ytmusic_candidates(artist, title, duration_s=None, max_fetch=1, session=None):
    if not ytmusic_available():
        return []
    yt = _ytmusic(session)
    hits = [_cand("ytmusic", " / ".join(a["name"] for a in r.get("artists") or []), r.get("title"),
                  r.get("duration_seconds"), None, r["videoId"])
            for r in yt.search(f"{artist} {title}", filter="songs", limit=5) if r.get("videoId")]
    hits = [c for c in hits if _plausible(artist, title, duration_s, c["artist"], c["title"], c["duration_s"])]
    out = []
    for c in _by_duration(hits, duration_s)[:max_fetch]:
        browse_id = yt.get_watch_playlist(videoId=c["ref"], limit=1).get("lyrics")
        lyr = yt.get_lyrics(browse_id, timestamps=True) if browse_id else None
        if not lyr or not lyr.get("hasTimestamps"):
            continue                       # plain-only or no lyrics
        c["synced"] = clean_lrc("\n".join(
            f"[{ln.start_time // 60000:02d}:{ln.start_time % 60000 / 1000:05.2f}]{ln.text}"
            for ln in lyr["lyrics"]), artist, title)
        c["album"] = lyr.get("source") or ""   # e.g. "Source: Musixmatch" / "Source: LyricFind"
        out.append(c)
    return out


# ------------------------------------------------------------- cross-check ---
def _lines(lrc):
    out = []
    for line in (lrc or "").splitlines():
        m = _TS.match(line.strip())
        text = _norm(_TS.sub("", line))
        if m and text:
            out.append((_t(m), text))
    return out


def _same(xa: str, xb: str) -> bool:
    return fuzz.ratio(xa, xb) >= 85 or (min(len(xa), len(xb)) >= 10 and fuzz.partial_ratio(xa, xb) >= 90)


def crosscheck(lrc_a: str, lrc_b: str, tol: float = 1.0) -> dict:
    """Do two LRCs carry the same lyric on the same clock?

    Collects the time delta (b - a) of every pair of similar lines, takes the
    densest 2*tol-wide cluster as the offset (robust to repeated choruses and
    merged/split lines), then counts lines that match within tol of it.
    """
    a, b = _lines(lrc_a), _lines(lrc_b)
    if len(a) < 3 or len(b) < 3:
        return {"agree": False, "matched": 0.0, "offset": None}
    pairs = [(i, j, tb - ta) for i, (ta, xa) in enumerate(a) for j, (tb, xb) in enumerate(b) if _same(xa, xb)]
    if not pairs:
        return {"agree": False, "matched": 0.0, "offset": None}
    deltas = sorted(d for _, _, d in pairs)
    lo, best = 0, (0, 0, 0)
    for hi in range(len(deltas)):
        while deltas[hi] - deltas[lo] > 2 * tol:
            lo += 1
        if hi - lo + 1 > best[0]:
            best = (hi - lo + 1, lo, hi)
    offset = statistics.median(deltas[best[1]:best[2] + 1])
    near = [(i, j) for i, j, d in pairs if abs(d - offset) <= tol]
    matched = max(len({i for i, _ in near}) / len(a), len({j for _, j in near}) / len(b))
    return {"agree": matched >= 0.6 and abs(offset) <= 1.5,
            "matched": round(matched, 2), "offset": round(offset, 2)}
