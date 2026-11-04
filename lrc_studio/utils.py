"""
utils.py — Pure-Python helpers: timestamp parsing/formatting, filename
           cleaning, and LRC status analysis.

All timestamp functions avoid QTime so they work correctly with 2- **and**
3-digit fractional-second fields (the original QTime approach silently
truncated 3-digit ms values to centiseconds).
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from PyQt6.QtGui import QColor

from constants import (
    BLACKLISTED_FILENAME_CHARS,
    COLOR_MISSING,
    COLOR_SYNCED,
    COLOR_UNSYNCED,
    DEFAULT_TIMESTAMP,
)

# ---------------------------------------------------------------------------
# Pre-compiled regexes
# ---------------------------------------------------------------------------

# A timestamp: [mm:ss.xx], [mm:ss.xxx], [mm:ss] or [mmm:ss.xx] (past 100 min).
_LRC_TS_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")

# Metadata tags: kept aside on load and written back on save.
_LRC_META_RE = re.compile(
    r"^\[(?:ar|al|ti|au|by|length|re|ve|tool|#):", re.IGNORECASE
)

# Captures the numeric components of a timestamp for efficient parsing.
_LRC_LINE_RE = _LRC_TS_RE

# Captures the [offset:N] global timing correction tag.
_LRC_OFFSET_RE = re.compile(r"^\[offset:(-?\d+)\]$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def clean_filename(name: str) -> str:
    """Remove characters that are illegal on common file systems."""
    for ch in BLACKLISTED_FILENAME_CHARS:
        name = name.replace(ch, "")
    return name.strip()


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def format_ms(ms: int) -> str:
    """Format *ms* milliseconds as ``mm:ss`` for display labels."""
    ms = max(0, ms)
    total_s = ms // 1_000
    m, s = divmod(total_s, 60)
    return f"{m:02d}:{s:02d}"


def ms_to_lrc_timestamp(ms: int) -> str:
    """Format *ms* milliseconds as an LRC timestamp string ``[mm:ss.xx]``.

    The fraction is stored as centiseconds (2 digits) to match the most
    common LRC convention.
    """
    ms = max(0, ms)
    m  = ms // 60_000
    s  = (ms % 60_000) // 1_000
    cs = (ms % 1_000) // 10          # centiseconds
    return f"[{m:02d}:{s:02d}.{cs:02d}]"     # three-digit minutes past 99


def parse_lrc_timestamp(ts: str) -> int:
    """Parse an LRC timestamp string to milliseconds.

    Handles both ``[mm:ss.xx]`` (centiseconds) and ``[mm:ss.xxx]``
    (milliseconds) variants.  Returns 0 on any parse failure instead of
    raising, so a single corrupt line never crashes the whole file load.
    """
    try:
        inner = ts.strip("[]")
        m_str, _, rest = inner.partition(":")
        s_str, _, frac = rest.replace(":", ".").partition(".")
        m   = int(m_str)
        s   = int(s_str)
        # Normalise fractional part to milliseconds regardless of digit count.
        if len(frac) == 1:
            extra_ms = int(frac) * 100         # tenths of a second
        elif len(frac) == 2:
            extra_ms = int(frac) * 10          # centiseconds → ms
        else:
            extra_ms = int(frac[:3])           # already ms (truncate beyond 3)
        return (m * 60 + s) * 1_000 + extra_ms
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# LRC file analysis
# ---------------------------------------------------------------------------

def read_lrc_text(path: str) -> str:
    """The text of an .lrc file in whatever encoding it was saved: UTF-8
    (with or without BOM), UTF-16 with a BOM, else Windows-1252/Latin-1.
    Raises only OSError."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def lrc_status_key(lrc_path: str) -> str:
    """"synced" | "plain" | "missing" for the .lrc at *lrc_path*.

    * missing — no file, or no lyric text in it (an empty starter row
      saved as "[00:00.00] " doesn't count as lyrics).
    * plain   — lyrics without real timing (no stamps, or almost all at 0).
    * synced  — lyrics with real timestamps.
    Never raises.
    """
    try:
        if not os.path.exists(lrc_path) or os.path.getsize(lrc_path) == 0:
            return "missing"
        content = read_lrc_text(lrc_path)
    except (OSError, ValueError):
        return "missing"
    stamps = 0
    real = 0
    has_text = False
    for raw in content.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or _LRC_META_RE.match(line) or _LRC_OFFSET_RE.match(line):
            continue
        found = _LRC_TS_RE.findall(line)
        if _LRC_TS_RE.sub("", line).strip():
            has_text = True
        for m_str, s_str, frac in found:
            stamps += 1
            if int(m_str) or int(s_str) or int(frac or 0):
                real += 1
    if not has_text:
        return "missing"
    if real == 0 or real / max(stamps, 1) < 0.2:
        return "plain"
    return "synced"


def analyze_lrc_status(lrc_path: str) -> QColor:
    """``lrc_status_key`` as a status colour (older callers)."""
    return {"synced": COLOR_SYNCED, "plain": COLOR_UNSYNCED}.get(lrc_status_key(lrc_path), COLOR_MISSING)


def lrc_metadata_lines(text: str) -> list[str]:
    """The [ar:]/[ti:]/[al:]… tag lines of an LRC, to write back on save.
    [offset:] is left out: it's applied to the timestamps on load."""
    out = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if _LRC_META_RE.match(line):
            out.append(line)
    return out


def parse_lrc_file(text: str) -> tuple[list[tuple[str, str]], int]:
    """Parse raw LRC text into a structured list.

    Returns
    -------
    lines
        A list of ``(timestamp_str, lyric_text)`` pairs, in the order they
        appear in the file.  Multiple timestamps on one line are expanded
        into separate entries (all sharing the same lyric text).
    global_offset_ms
        The value of the ``[offset:N]`` tag, in milliseconds (0 if absent).
        **Positive** values mean every timestamp should be shifted forward;
        **negative** values shift backward.
    """
    lines: list[tuple[str, str]] = []
    global_offset_ms: int = 0

    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue

        # ── [offset:N] — global timing correction ──────────────────────────
        offset_match = _LRC_OFFSET_RE.match(line)
        if offset_match:
            global_offset_ms = int(offset_match.group(1))
            continue

        # ── Other metadata tags — skip silently ────────────────────────────
        if _LRC_META_RE.match(line):
            continue

        # ── Lines that carry timestamp(s) ──────────────────────────────────
        ts_matches = _LRC_LINE_RE.findall(line)   # [(m, s, frac), …]
        if ts_matches:
            content = _LRC_LINE_RE.sub("", line).strip()
            for m_str, s_str, frac in ts_matches:
                # Normalise to 2-digit centiseconds for storage.
                cs = (frac or "00").ljust(2, "0")[:2]
                ts_str = f"[{int(m_str):02d}:{int(s_str):02d}.{cs}]"
                lines.append((ts_str, content))

        else:
            # A line with no timestamp (plain lyrics, or a label such as
            # "[Chorus]") — kept as an unsynced line rather than dropped.
            lines.append((DEFAULT_TIMESTAMP, line))

    return lines, global_offset_ms


# ---------------------------------------------------------------------------
# Embedded-lyrics tag writer
# ---------------------------------------------------------------------------

# Extensions we know how to embed lyrics into, each with a different
# underlying tag format (ID3 USLT, Vorbis comment, MP4 atom).
EMBEDDABLE_EXTENSIONS: tuple[str, ...] = (".mp3", ".flac", ".ogg", ".opus", ".m4a", ".mp4")


def write_embedded_lyrics(file_path: str, lrc_text: str) -> None:
    """Write *lrc_text* into *file_path*'s own metadata tags.

    Stores the same timestamped text that goes into the sidecar ``.lrc``
    file — most modern players (VLC, MusicBee, Apple Music, etc.) will
    happily parse an LRC-formatted lyrics tag as synced lyrics. Raises
    ``ValueError`` for unsupported formats and propagates any I/O error
    from mutagen so the caller can report it.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".mp3":
        from mutagen.id3 import ID3, ID3NoHeaderError, USLT
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="eng", desc="", text=lrc_text))
        tags.save(file_path)

    elif ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(file_path)
        audio["LYRICS"] = lrc_text
        audio.save()

    elif ext == ".ogg":
        from mutagen.oggvorbis import OggVorbis
        audio = OggVorbis(file_path)
        audio["LYRICS"] = lrc_text
        audio.save()

    elif ext == ".opus":
        from mutagen.oggopus import OggOpus
        audio = OggOpus(file_path)
        audio["LYRICS"] = lrc_text
        audio.save()

    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4
        audio = MP4(file_path)
        audio["\xa9lyr"] = lrc_text
        audio.save()

    else:
        raise ValueError(f"Embedding lyrics is not supported for {ext} files.")


# ---------------------------------------------------------------------------
# Artist / title tags (library "Edit Artist & Title…")
# ---------------------------------------------------------------------------

def read_artist_title(path: str) -> tuple[str, str]:
    """The file's artist and title tags, ("", "") when it has none or
    can't be read."""
    import mutagen

    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return "", ""
    if audio is None or audio.tags is None:
        return "", ""
    artist = (audio.get("artist") or [""])[0].strip()
    title = (audio.get("title") or [""])[0].strip()
    return artist, title


def write_artist_title(path: str, artist: str, title: str) -> None:
    """Set the artist and title tags, keeping every other tag. Raises on
    files mutagen can't tag."""
    import mutagen

    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError(f"Not an audio file mutagen can tag: {os.path.basename(path)}")
    if audio.tags is None:
        audio.add_tags()
    audio["artist"] = [artist.strip()]
    audio["title"] = [title.strip()]
    audio.save()
