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

# Matches a well-formed LRC timestamp: [mm:ss.xx] or [mm:ss.xxx]
_LRC_TS_RE = re.compile(r"\[\d{2}:\d{2}\.\d{2,3}\]")

# Matches LRC metadata tags that should be silently skipped.
_LRC_META_RE = re.compile(
    r"^\[(?:ar|al|ti|au|by|length|re|ve):", re.IGNORECASE
)

# Captures the numeric components of a timestamp for efficient parsing.
_LRC_LINE_RE = re.compile(r"\[(\d{1,2}):(\d{2})\.(\d{2,3})\]")

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
    return f"[{m:02d}:{s:02d}.{cs:02d}]"


def parse_lrc_timestamp(ts: str) -> int:
    """Parse an LRC timestamp string to milliseconds.

    Handles both ``[mm:ss.xx]`` (centiseconds) and ``[mm:ss.xxx]``
    (milliseconds) variants.  Returns 0 on any parse failure instead of
    raising, so a single corrupt line never crashes the whole file load.
    """
    try:
        inner = ts.strip("[]")
        time_part, _, frac = inner.partition(".")
        m_str, _, s_str   = time_part.partition(":")
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

def analyze_lrc_status(lrc_path: str) -> QColor:
    """Return a status colour reflecting the quality of *lrc_path*.

    * ``COLOR_MISSING``  — file does not exist or is empty.
    * ``COLOR_UNSYNCED`` — file exists but timestamps look unedited
                           (>80 % are ``[00:00…]``).
    * ``COLOR_SYNCED``   — file has real, varied timestamps.
    """
    if not os.path.exists(lrc_path):
        return COLOR_MISSING
    try:
        if os.path.getsize(lrc_path) == 0:
            return COLOR_MISSING
        with open(lrc_path, encoding="utf-8") as fh:
            content = fh.read()
        timestamps = _LRC_TS_RE.findall(content)
        if not timestamps:
            return COLOR_UNSYNCED
        zero_count = sum(1 for t in timestamps if t.startswith("[00:00"))
        if len(timestamps) > 5 and (zero_count / len(timestamps)) > 0.8:
            return COLOR_UNSYNCED
        return COLOR_SYNCED
    except OSError:
        return COLOR_MISSING


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
        line = raw.strip()
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
                cs = frac[:2]
                ts_str = f"[{int(m_str):02d}:{int(s_str):02d}.{cs}]"
                lines.append((ts_str, content))

        elif not line.startswith("["):
            # Plain lyric line with no timestamp — keep with default stamp.
            lines.append((DEFAULT_TIMESTAMP, line))

    return lines, global_offset_ms


# ---------------------------------------------------------------------------
# Embedded-lyrics tag writer
# ---------------------------------------------------------------------------

# Extensions we know how to embed lyrics into, each with a different
# underlying tag format (ID3 USLT, Vorbis comment, MP4 atom).
EMBEDDABLE_EXTENSIONS: tuple[str, ...] = (".mp3", ".flac", ".ogg", ".m4a", ".mp4")


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

    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4
        audio = MP4(file_path)
        audio["\xa9lyr"] = lrc_text
        audio.save()

    else:
        raise ValueError(f"Embedding lyrics is not supported for {ext} files.")
