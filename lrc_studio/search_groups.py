"""search_groups.py — Soulseek search results as song versions.

The same rip is usually shared by many peers under the same file name, so
a flat list shows one song ten times. Here copies are grouped into
versions: same file name (track number and extension aside) and a length
within a few seconds. Each version knows its best copy (good quality,
then whoever can send it soonest), and versions are ranked so the one you
most likely want is first:

1. every word you searched for is in the file name,
2. versions you didn't ask for (karaoke, remix, live, …) go below,
3. more copies first (a widely shared rip is usually the right one),
4. then the quality and availability of the best copy.

Qt-free; the search page shows a VersionIndex as a tree.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from postprocess import strip_track_number
from soulseek import ResultFilter, SoulseekFile, availability_key, normalize_text

SAME_VERSION_MAX_LENGTH_DIFF_S = 3

# Versions nobody wants by accident. Matched as whole words in the file name,
# and ignored when the search itself asks for them ("espresso remix").
UNWANTED_WORDS = (
    "karaoke", "instrumental", "remix", "live", "cover", "sped up", "slowed",
    "nightcore", "8d", "acapella", "a cappella", "reverb", "tribute", "made famous",
)


def _stem(f: SoulseekFile) -> str:
    return strip_track_number(os.path.splitext(f.display_name)[0]).strip()


# "03 Song" (a track number with only a space after it). Only for grouping:
# shown titles keep it, since "50 Cent - …" starts the same way.
_BARE_TRACK_NUMBER_RE = re.compile(r"^\d{1,2}\s+(?=\S)")


def _group_key(f: SoulseekFile) -> str:
    return normalize_text(_BARE_TRACK_NUMBER_RE.sub("", _stem(f)))


@dataclass
class SongVersion:
    id: int
    key: str                       # normalized stem shared by every copy
    length_s: int | None
    unwanted: bool
    relevant: bool
    in_library: bool
    copies: list[SoulseekFile] = field(default_factory=list)

    @property
    def best(self) -> SoulseekFile:
        return min(self.copies, key=availability_key)

    @property
    def title(self) -> str:
        return _stem(self.best)


class OwnedSongs:
    """What's already in the library, by file name, for "In library" marks."""

    def __init__(self, stems: set[str], artists_by_title: dict[str, set[str]]) -> None:
        self._stems = stems
        self._artists_by_title = artists_by_title

    def owns(self, key: str, normalized_path: str) -> bool:
        if key in self._stems:
            return True
        padded = f" {normalized_path} "
        return any(f" {artist} " in padded for artist in self._artists_by_title.get(key, ()))


def owned_stems(paths: list[str]) -> OwnedSongs:
    stems: set[str] = set()
    artists_by_title: dict[str, set[str]] = {}
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        stems.add(normalize_text(strip_track_number(stem)))
        if " - " in stem:
            artist, title = stem.split(" - ", 1)
            artists_by_title.setdefault(normalize_text(title), set()).add(normalize_text(artist))
    return OwnedSongs(stems, artists_by_title)


_NO_OWNED = OwnedSongs(set(), {})


class VersionIndex:
    def __init__(self, query: str, owned: OwnedSongs | None = None) -> None:
        self._query_words = normalize_text(query).split()
        padded_query = f" {' '.join(self._query_words)} "
        self._unwanted = [w for w in UNWANTED_WORDS if f" {w} " not in padded_query]
        self._owned = owned or _NO_OWNED
        self._by_key: dict[str, list[SongVersion]] = {}
        self._by_file: dict[tuple[str, str], SongVersion] = {}
        self._versions: list[SongVersion] = []
        self._filter = ResultFilter()
        self._frozen: list[int] | None = None
        self._next_id = 1

    # -- building ---------------------------------------------------------------

    def add(self, files: list[SoulseekFile]) -> None:
        for f in files:
            if (f.username, f.filename) in self._by_file:
                continue
            version = self._version_for(f)
            version.copies.append(f)
            self._by_file[(f.username, f.filename)] = version

    def _version_for(self, f: SoulseekFile) -> SongVersion:
        key = _group_key(f)
        for version in self._by_key.get(key, []):
            if (version.length_s is None or f.length_s is None
                    or abs(version.length_s - f.length_s) <= SAME_VERSION_MAX_LENGTH_DIFF_S):
                if version.length_s is None:
                    version.length_s = f.length_s
                return version
        padded = f" {key} "
        name = normalize_text(f.display_name)
        version = SongVersion(
            id=self._next_id,
            key=key,
            length_s=f.length_s,
            unwanted=any(f" {w} " in padded for w in self._unwanted),
            relevant=all(w in name for w in self._query_words),
            in_library=self._owned.owns(key, normalize_text(f.filename)),
        )
        self._next_id += 1
        self._by_key.setdefault(key, []).append(version)
        self._versions.append(version)
        return version

    # -- filtering and ranking --------------------------------------------------

    def set_filter(self, text: str, lossless_only: bool) -> None:
        self._filter = ResultFilter(text=text, formats="lossless" if lossless_only else "any")

    def visible_copies(self, version: SongVersion) -> list[SoulseekFile]:
        return sorted((f for f in version.copies if self._filter.matches(f)), key=availability_key)

    def _rank_key(self, version: SongVersion) -> tuple:
        copies = self.visible_copies(version)
        best = copies[0]
        quality = {True: 0, None: 1, False: 2}[best.quality_ok]
        return (not version.relevant, version.unwanted, -len(copies), quality, availability_key(best), version.id)

    def ranked(self) -> list[SongVersion]:
        visible = [v for v in self._versions if self.visible_copies(v)]
        if self._frozen is None:
            return sorted(visible, key=self._rank_key)
        by_id = {v.id: v for v in visible}
        kept = [by_id[i] for i in self._frozen if i in by_id]
        known = set(self._frozen)
        fresh = sorted((v for v in visible if v.id not in known), key=self._rank_key)
        self._frozen.extend(v.id for v in fresh)
        return kept + fresh

    def freeze_order(self) -> None:
        """Stop reshuffling rows the user can already see (they picked one)."""
        if self._frozen is None:
            self._frozen = [v.id for v in self.ranked()]

    # -- lookups ----------------------------------------------------------------

    def version_of(self, f: SoulseekFile) -> SongVersion | None:
        return self._by_file.get((f.username, f.filename))

    def alternatives(self, f: SoulseekFile) -> list[SoulseekFile]:
        """The other copies of *f*'s version, best first: fallbacks if its
        peer never answers or rejects the request."""
        version = self.version_of(f)
        if version is None:
            return []
        return sorted((c for c in version.copies if c is not f), key=availability_key)

    @property
    def file_count(self) -> int:
        return len(self._by_file)

    @property
    def peer_count(self) -> int:
        return len({user for user, _ in self._by_file})

