"""spotify_playlist.py — read a public Spotify track/album/playlist's track
list from a pasted link, with no developer account or API credentials.

Works by parsing the ``__NEXT_DATA__`` JSON Spotify embeds in its own
no-auth embed pages (``open.spotify.com/embed/<kind>/<id>`` for
``kind`` in track/album/playlist) — confirmed live against real public
links during development.

Known limitation: the album/playlist embed page caps out at the first 50
tracks and its ``?offset=`` query param has no effect (verified — it just
returns the same first 50 again). There's no unauthenticated way around
that; longer albums/playlists come back truncated, with ``truncated=True``
so callers can warn rather than silently drop tracks. A future upgrade
path is the official Spotify Web API (needs a free developer app), which
isn't wired up here since the point of this module is not needing one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import mutagen
import requests
from rapidfuzz import fuzz

_LINK_RE = re.compile(r"(track|album|playlist)[:/]([a-zA-Z0-9]{22})")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
_EMBED_TRACK_LIMIT = 50
_DEFAULT_MATCH_THRESHOLD = 80  # rapidfuzz token_sort_ratio, 0-100


class SpotifyLinkError(Exception):
    """Raised when a Spotify link can't be parsed or fetched."""


@dataclass(frozen=True)
class SpotifyTrack:
    artist: str
    title: str

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()

    def __str__(self) -> str:
        return self.query


def extract_spotify_ref(link_or_uri: str) -> tuple[str, str]:
    """Pull ``(kind, id)`` out of a pasted track/album/playlist link, URI,
    or bare ID (bare IDs are assumed to be a playlist, for backwards
    compatibility with links that omit the type)."""
    text = link_or_uri.strip()
    match = _LINK_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    if re.fullmatch(r"[a-zA-Z0-9]{22}", text):
        return "playlist", text
    raise SpotifyLinkError(
        f"Couldn't find a Spotify track/album/playlist link in: {link_or_uri!r}"
    )


def is_spotify_link(text: str) -> bool:
    """True if *text* is a Spotify track/album/playlist link, URI, or bare
    ID — the same shapes extract_spotify_ref() accepts. Used to route the
    unified Find Music input: a recognized link means "look up this
    track list," anything else means "search Soulseek directly."
    """
    try:
        extract_spotify_ref(text)
        return True
    except SpotifyLinkError:
        return False


def fetch_spotify_tracks(
    link_or_uri: str, timeout_s: float = 15.0,
) -> tuple[list[SpotifyTrack], bool]:
    """Return ``(tracks, truncated)`` for the track/album/playlist at
    *link_or_uri*. ``tracks`` has one entry for a track link, or the full
    list of tracks for an album/playlist link."""
    kind, spotify_id = extract_spotify_ref(link_or_uri)
    url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    try:
        resp = requests.get(
            url, timeout=timeout_s,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LrcStudio/1.0)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SpotifyLinkError(f"Couldn't reach Spotify: {exc}") from exc

    match = _NEXT_DATA_RE.search(resp.text)
    if not match:
        raise SpotifyLinkError(
            "Couldn't parse Spotify's page — it may have changed, or the "
            "link is private/invalid."
        )
    try:
        data = json.loads(match.group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, json.JSONDecodeError) as exc:
        raise SpotifyLinkError(f"Unexpected page structure: {exc}") from exc

    raw_tracks = entity.get("trackList")
    if raw_tracks is not None:
        # Album/playlist embed: a list of tracks.
        tracks = [
            SpotifyTrack(artist=t.get("subtitle", "").strip(), title=t.get("title", "").strip())
            for t in raw_tracks
            if t.get("title")
        ]
        truncated = len(raw_tracks) >= _EMBED_TRACK_LIMIT
    else:
        # Track embed: the entity itself is the one track.
        title = entity.get("title", "").strip()
        if not title:
            raise SpotifyLinkError(f"Unexpected page structure for {kind} embed")
        tracks = [SpotifyTrack(artist=entity.get("subtitle", "").strip(), title=title)]
        truncated = False
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
