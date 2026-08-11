"""spotify_playlist.py — read a public Spotify playlist's track list from a
pasted link, with no developer account or API credentials.

Works by parsing the ``__NEXT_DATA__`` JSON Spotify embeds in its own
no-auth embed page (``open.spotify.com/embed/playlist/<id>``) — confirmed
live against a real public playlist during development.

Known limitation: that embed page caps out at the playlist's first 50
tracks and its ``?offset=`` query param has no effect (verified — it just
returns the same first 50 again). There's no unauthenticated way around
that; longer playlists come back truncated, with ``truncated=True`` so
callers can warn rather than silently drop tracks. A future upgrade path
is the official Spotify Web API (needs a free developer app), which isn't
wired up here since the point of this module is not needing one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import mutagen
import requests
from rapidfuzz import fuzz

_ID_RE = re.compile(r"playlist[:/]([a-zA-Z0-9]{22})")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
_EMBED_TRACK_LIMIT = 50
_DEFAULT_MATCH_THRESHOLD = 80  # rapidfuzz token_sort_ratio, 0-100


class SpotifyPlaylistError(Exception):
    """Raised when a playlist link can't be parsed or fetched."""


@dataclass(frozen=True)
class SpotifyTrack:
    artist: str
    title: str

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()

    def __str__(self) -> str:
        return self.query


def extract_playlist_id(link_or_uri: str) -> str:
    """Pull a 22-char playlist ID out of a pasted link, URI, or bare ID."""
    text = link_or_uri.strip()
    match = _ID_RE.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]{22}", text):
        return text
    raise SpotifyPlaylistError(f"Couldn't find a playlist ID in: {link_or_uri!r}")


def fetch_playlist_tracks(
    link_or_uri: str, timeout_s: float = 15.0,
) -> tuple[list[SpotifyTrack], bool]:
    """Return ``(tracks, truncated)`` for the playlist at *link_or_uri*."""
    playlist_id = extract_playlist_id(link_or_uri)
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    try:
        resp = requests.get(
            url, timeout=timeout_s,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LrcStudio/1.0)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SpotifyPlaylistError(f"Couldn't reach Spotify: {exc}") from exc

    match = _NEXT_DATA_RE.search(resp.text)
    if not match:
        raise SpotifyPlaylistError(
            "Couldn't parse Spotify's page — it may have changed, or the "
            "playlist link is private/invalid."
        )
    try:
        data = json.loads(match.group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        raw_tracks = entity["trackList"]
    except (KeyError, json.JSONDecodeError) as exc:
        raise SpotifyPlaylistError(f"Unexpected page structure: {exc}") from exc

    tracks = [
        SpotifyTrack(artist=t.get("subtitle", "").strip(), title=t.get("title", "").strip())
        for t in raw_tracks
        if t.get("title")
    ]
    truncated = len(raw_tracks) >= _EMBED_TRACK_LIMIT
    return tracks, truncated


# ---------------------------------------------------------------------------
# Dedup against the local library
# ---------------------------------------------------------------------------

def _library_entry(path: str) -> str:
    """Return a normalized "artist title" string for an existing library file."""
    try:
        audio = mutagen.File(path, easy=True)
        if audio:
            title = audio.get("title", [""])[0].strip()
            artist = audio.get("artist", [""])[0].strip()
            if title and artist:
                return f"{artist} {title}".lower()
    except Exception:
        pass
    stem = os.path.splitext(os.path.basename(path))[0]
    if " - " in stem:
        artist, _, title = stem.partition(" - ")
        return f"{artist} {title}".strip().lower()
    return stem.lower()


def find_missing_tracks(
    tracks: list[SpotifyTrack],
    library_paths: list[str],
    threshold: int = _DEFAULT_MATCH_THRESHOLD,
) -> list[SpotifyTrack]:
    """Return the subset of *tracks* with no good fuzzy match in the library.

    Compares "artist title" strings with rapidfuzz's token_sort_ratio,
    which tolerates word-order/punctuation differences between what
    Spotify calls a track and how a file happens to be named/tagged.
    """
    library_entries = [_library_entry(p) for p in library_paths]
    missing = []
    for track in tracks:
        query = track.query.lower()
        best = max(
            (fuzz.token_sort_ratio(query, entry) for entry in library_entries),
            default=0,
        )
        if best < threshold:
            missing.append(track)
    return missing
