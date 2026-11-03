"""lyrics_fetch.py — duration-aware lyrics lookup built around LRCLIB's API.

The old approach (``syncedlyrics.search("<artist> <title>")``) takes the first
fuzzy hit without ever looking at the track's length, so live cuts and other
versions slip through, and queries built from filenames such as
"01 - Manu Chao - Clandestino" match poorly. This module instead reads the
tags (falling back to the filename), then escalates:

1. LRCLIB ``/get`` with the exact tags and duration (LRCLIB matches within a
   couple of seconds, so a hit is almost certainly the right recording), then
   again with a cleaned title / primary artist.
2. LRCLIB ``/search``, filtered by title/artist similarity and by duration
   (closest within 3 s first, then within 8 s). Lyrics whose timestamps run
   past the track's length are rejected.
3. ``syncedlyrics`` (Musixmatch, NetEase, Megalobiz) as a last synced source.
4. Optionally, plain (unsynced) lyrics, reported as ``synced=False`` so the
   caller never mistakes them for a timed .lrc.

Every network step fails soft: an error just moves on to the next step.
This module has no Qt dependency; all HTTP goes through ``_http_get`` so tests
can replace it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import mutagen
import requests
import syncedlyrics
from rapidfuzz import fuzz

LRCLIB_API = "https://lrclib.net/api"
USER_AGENT = "LRC Studio (https://github.com/feal-ca/LrcSync)"
HTTP_TIMEOUT_S = 10
SYNCEDLYRICS_PROVIDERS = ["Musixmatch", "NetEase", "Megalobiz"]

TITLE_MIN_RATIO = 80
ARTIST_MIN_RATIO = 70
EXACT_DURATION_DIFF_S = 3.0
MAX_DURATION_DIFF_S = 8.0
# Allowance for a lyric's last timestamp vs. the audio length (outros, fades).
PLAUSIBLE_OVERRUN_S = 10.0
MAX_PICKER_RESULTS = 30


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TrackInfo:
    artist: str
    title: str
    album: str = ""
    duration_s: float | None = None


@dataclass
class LyricsCandidate:
    source: str
    artist: str
    title: str
    album: str
    duration_s: float | None
    synced: str | None
    plain: str | None
    ref: str = ""

    @property
    def has_synced(self) -> bool:
        return bool(self.synced and self.synced.strip())

    @property
    def text(self) -> str:
        return self.synced or self.plain or ""


@dataclass
class LyricsResult:
    text: str
    synced: bool
    source: str
    candidate: LyricsCandidate | None = None


# ---------------------------------------------------------------------------
# Tag / filename handling
# ---------------------------------------------------------------------------

_TRACK_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*[-._)]+\s*")


def parse_filename(stem: str) -> tuple[str, str]:
    """Guess ``(artist, title)`` from a file name without extension."""
    cleaned = _TRACK_NUMBER_RE.sub("", stem).strip() or stem.strip()
    parts = [p.strip() for p in cleaned.split(" - ") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    # "Artist - Album - Title" -> drop the middle segments.
    return parts[0], parts[-1]


def _first_tag(audio, key: str) -> str:
    try:
        values = audio.get(key)
    except Exception:
        return ""
    if not values:
        return ""
    value = values[0] if isinstance(values, (list, tuple)) else values
    return str(value).strip()


def read_track_info(path: str) -> TrackInfo:
    """Read artist/title/album/duration from tags; never raises."""
    artist = title = album = ""
    duration: float | None = None
    try:
        audio = mutagen.File(path, easy=True)
        if audio is not None:
            artist = _first_tag(audio, "artist")
            title = _first_tag(audio, "title")
            album = _first_tag(audio, "album")
            length = getattr(getattr(audio, "info", None), "length", None)
            if length and length > 0:
                duration = float(length)
    except Exception:
        pass

    if not artist or not title:
        stem = os.path.splitext(os.path.basename(path))[0]
        f_artist, f_title = parse_filename(stem)
        artist = artist or f_artist
        title = title or f_title
    return TrackInfo(artist=artist, title=title, album=album, duration_s=duration)


# ---------------------------------------------------------------------------
# Title / artist normalisation
# ---------------------------------------------------------------------------

# Whole-content patterns for a bracketed group or a " - suffix" that carries no
# identity information (matched with fullmatch, case-insensitively).
_NOISE = (
    r"(?:\d{4}\s+)?(?:digital\s+)?remaster(?:ed)?(?:\s+(?:\d{4}|version|edition))*",
    r"explicit(?:\s+version)?",
    r"clean(?:\s+version)?",
    r"(?:radio|single|album|original|mono|stereo)\s+(?:edit|version|mix)",
    r"mono|stereo",
    r"(?:official\s+)?(?:(?:music|lyrics?|audio|hd)\s+)*(?:video|audio|visuali[sz]er)",
    r"official\b.*",
    r"live(?:\s+(?:at|in|from|on|version|session)\b.*)?",
    r"bonus\s+track",
)
_NOISE_ALT = "|".join(f"(?:{p})" for p in _NOISE)
_BRACKET_NOISE_RE = re.compile(
    rf"\s*[(\[]\s*(?:{_NOISE_ALT})\s*[)\]]", re.IGNORECASE
)
_DASH_NOISE_RE = re.compile(
    rf"\s+[-–—]\s+(?:{_NOISE_ALT})\s*$", re.IGNORECASE
)
_BRACKET_FEAT_RE = re.compile(
    r"\s*[(\[]\s*(?:feat|ft|featuring)\b\.?[^)\]]*[)\]]", re.IGNORECASE
)
_TRAILING_FEAT_RE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$", re.IGNORECASE
)


def clean_title(title: str) -> str:
    """Drop featuring credits and version noise; keep meaningful parentheticals."""
    current = title
    while True:
        previous = current
        current = _BRACKET_FEAT_RE.sub("", current)
        current = _BRACKET_NOISE_RE.sub("", current)
        current = _DASH_NOISE_RE.sub("", current)
        current = _TRAILING_FEAT_RE.sub("", current)
        if current == previous:
            break
    current = re.sub(r"\s+", " ", current).strip()
    # Never clean a title into nothing ("(Live)" alone is a real title).
    return current or re.sub(r"\s+", " ", title).strip()


# " y " is deliberately absent: it is part of Spanish names ("Ella y Él").
_ARTIST_SPLIT_RE = re.compile(
    r"\s*[,;]\s*|\s+&\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|\s+x\s+|\s+/\s+",
    re.IGNORECASE,
)


def primary_artist(artist: str) -> str:
    """First credited artist of a multi-artist string."""
    first = _ARTIST_SPLIT_RE.split(artist.strip(), maxsplit=1)[0].strip()
    return first or artist.strip()


# ---------------------------------------------------------------------------
# LRCLIB
# ---------------------------------------------------------------------------

def _http_get(url: str, params: dict) -> requests.Response:
    return requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT_S,
    )


def _candidate_from_json(item: dict) -> LyricsCandidate | None:
    if not isinstance(item, dict) or item.get("instrumental"):
        return None
    duration = item.get("duration")
    try:
        duration_s = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_s = None
    ident = item.get("id")
    return LyricsCandidate(
        source="lrclib",
        artist=item.get("artistName") or "",
        title=item.get("trackName") or "",
        album=item.get("albumName") or "",
        duration_s=duration_s,
        synced=item.get("syncedLyrics") or None,
        plain=item.get("plainLyrics") or None,
        ref=str(ident) if ident is not None else "",
    )


def lrclib_get(info: TrackInfo) -> LyricsCandidate | None:
    """Exact-signature lookup; needs the duration, so skipped without one."""
    if info.duration_s is None or not info.title or not info.artist:
        return None
    params: dict = {"track_name": info.title, "artist_name": info.artist}
    if info.album:
        params["album_name"] = info.album
    params["duration"] = int(round(info.duration_s))
    try:
        resp = _http_get(f"{LRCLIB_API}/get", params)
        if resp.status_code != 200:
            return None
        return _candidate_from_json(resp.json())
    except Exception:
        return None


def lrclib_search(
    track_name: str = "", artist_name: str = "", q: str = ""
) -> list[LyricsCandidate]:
    if q:
        params = {"q": q}
    elif track_name:
        params = {"track_name": track_name}
        if artist_name:
            params["artist_name"] = artist_name
    else:
        return []
    try:
        resp = _http_get(f"{LRCLIB_API}/search", params)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        cands = (_candidate_from_json(item) for item in data)
        return [c for c in cands if c is not None]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Validation and ranking
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{2})\.(\d{2,3})\]")


def is_plausible_synced(lrc_text: str, duration_s: float | None) -> bool:
    """Cheap sanity check that an LRC body is real and fits the track length."""
    times: list[float] = []
    for line in (lrc_text or "").splitlines():
        m = _TIMESTAMP_RE.search(line)
        if m:
            frac = m.group(3)
            times.append(int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / 10 ** len(frac))
    if len(times) < 3:
        return False
    nonzero = sum(1 for t in times if t >= 1.0)
    if nonzero < 3 or nonzero / len(times) < 0.5:
        return False
    if duration_s is not None and max(times) > duration_s + PLAUSIBLE_OVERRUN_S:
        return False
    return True


def _norm(text: str) -> str:
    return text.lower().strip()


def _title_ratio(c: LyricsCandidate, info: TrackInfo) -> float:
    return fuzz.token_set_ratio(
        _norm(clean_title(c.title)), _norm(clean_title(info.title))
    )


def _artist_ratio(c: LyricsCandidate, info: TrackInfo) -> float:
    if not info.artist:
        return 100.0
    cand = _norm(c.artist)
    return max(
        fuzz.token_set_ratio(cand, _norm(primary_artist(info.artist))),
        fuzz.token_set_ratio(cand, _norm(info.artist)),
    )


def _duration_diff(c: LyricsCandidate, info: TrackInfo) -> float | None:
    if c.duration_s is None or info.duration_s is None:
        return None
    return abs(c.duration_s - info.duration_s)


def candidate_matches(
    c: LyricsCandidate, info: TrackInfo, max_duration_diff_s: float = MAX_DURATION_DIFF_S
) -> bool:
    if _title_ratio(c, info) < TITLE_MIN_RATIO:
        return False
    if info.artist and _artist_ratio(c, info) < ARTIST_MIN_RATIO:
        return False
    diff = _duration_diff(c, info)
    return diff is None or diff <= max_duration_diff_s


def _score(c: LyricsCandidate, info: TrackInfo) -> float:
    diff = _duration_diff(c, info)
    # 100 at an exact match, 0 from 20 s off; neutral when either is unknown.
    duration_score = 50.0 if diff is None else max(0.0, 100.0 - diff * 5.0)
    return (
        0.40 * _title_ratio(c, info)
        + 0.20 * _artist_ratio(c, info)
        + 0.30 * duration_score
        + (10.0 if c.has_synced else 0.0)
    )


def rank_candidates(
    cands: list[LyricsCandidate], info: TrackInfo
) -> list[LyricsCandidate]:
    return sorted(cands, key=lambda c: _score(c, info), reverse=True)


def _merge(*groups: list[LyricsCandidate]) -> list[LyricsCandidate]:
    seen: set[tuple] = set()
    merged: list[LyricsCandidate] = []
    for group in groups:
        for c in group:
            key = (c.source, c.ref) if c.ref else (c.source, c.artist, c.title, c.duration_s)
            if key not in seen:
                seen.add(key)
                merged.append(c)
    return merged


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _search_lrclib(artist: str, title: str) -> list[LyricsCandidate]:
    return _merge(
        lrclib_search(track_name=title, artist_name=artist),
        lrclib_search(q=f"{artist} {title}".strip()),
    )


def _pick_synced(
    matching: list[LyricsCandidate], info: TrackInfo
) -> LyricsCandidate | None:
    """Best plausible synced candidate, preferring the tightest duration fit."""
    synced = [c for c in rank_candidates(matching, info) if c.has_synced]
    tiers = (
        [lambda d: d is not None and d <= EXACT_DURATION_DIFF_S,
         lambda d: d is None or d <= MAX_DURATION_DIFF_S]
        if info.duration_s is not None
        else [lambda d: True]
    )
    for in_tier in tiers:
        for c in synced:
            if in_tier(_duration_diff(c, info)) and is_plausible_synced(
                c.synced or "", info.duration_s
            ):
                return c
    return None


def fetch_best_lyrics(info: TrackInfo, allow_plain: bool = True) -> LyricsResult | None:
    title = clean_title(info.title)
    artist = primary_artist(info.artist)
    if not title:
        return None

    plain_pool: list[LyricsCandidate] = []

    # 1. Exact-signature lookups.
    attempts = [info]
    if (title, artist) != (info.title, info.artist):
        attempts.append(TrackInfo(artist, title, "", info.duration_s))
    for attempt in attempts:
        try:
            cand = lrclib_get(attempt)
        except Exception:
            cand = None
        if cand is None:
            continue
        if cand.has_synced and is_plausible_synced(cand.synced or "", info.duration_s):
            return LyricsResult(cand.synced or "", True, "lrclib", cand)
        if cand.plain:
            plain_pool.append(cand)

    # 2. Fuzzy LRCLIB search, gated on text and duration.
    matching: list[LyricsCandidate] = []
    try:
        matching = [
            c for c in _search_lrclib(artist, title) if candidate_matches(c, info)
        ]
        best = _pick_synced(matching, info)
        if best is not None:
            return LyricsResult(best.synced or "", True, "lrclib", best)
    except Exception:
        pass

    # 3. Other providers via syncedlyrics.
    term = f"{title} {artist}".strip()
    try:
        text = syncedlyrics.search(
            term, synced_only=True, providers=SYNCEDLYRICS_PROVIDERS
        )
        if text and is_plausible_synced(text, info.duration_s):
            return LyricsResult(text, True, "syncedlyrics")
    except Exception:
        pass

    # 4. Plain lyrics, clearly flagged as unsynced.
    if allow_plain:
        plain_pool.extend(c for c in rank_candidates(matching, info) if c.plain)
        if plain_pool:
            best = plain_pool[0]
            return LyricsResult(best.plain or "", False, "lrclib", best)
        try:
            text = syncedlyrics.search(term, plain_only=True)
            if text and text.strip():
                return LyricsResult(text, False, "syncedlyrics")
        except Exception:
            pass
    return None


def search_candidates(
    artist: str, title: str, duration_s: float | None = None
) -> list[LyricsCandidate]:
    """Unfiltered, ranked LRCLIB results for a manual picker."""
    cands = _search_lrclib(artist, title)
    ranked = rank_candidates(cands, TrackInfo(artist, title, "", duration_s))
    return ranked[:MAX_PICKER_RESULTS]
