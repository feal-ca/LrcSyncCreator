"""
constants.py — Application-wide constants and colour handles.
"""

from PyQt6.QtGui import QColor

# ---------------------------------------------------------------------------
# Sync behaviour
# ---------------------------------------------------------------------------

# How many ms to subtract from the playback position when stamping a verse.
# Compensates for the user's reaction time: press S and the timestamp is
# set to (now - SYNC_OFFSET_MS), so the line appears on-screen at the right beat.
SYNC_OFFSET_MS: int = 200

# How often (ms) the VLC poll timer fires.  Lowering this improves karaoke
# responsiveness but increases CPU usage.
POLL_INTERVAL_MS: int = 100

# Fallback timestamp inserted for un-timed verses.
DEFAULT_TIMESTAMP: str = "[00:00.00]"

# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS: tuple[str, ...] = (
    ".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus", ".aiff", ".aif",
)

# Characters that are illegal on at least one major OS file system.
BLACKLISTED_FILENAME_CHARS: list[str] = [
    ":", "?", '"', "/", "\\", "*", "<", ">", "|"
]

# Maximum number of sync operations kept in the undo stack.
MAX_UNDO: int = 100

# ---------------------------------------------------------------------------
# UI geometry
# ---------------------------------------------------------------------------

BUTTON_SIZE: int = 28   # px — square action buttons on each Verse row

# ---------------------------------------------------------------------------
# Colours — QColor views of the tokens in theme.py, for code
# that paints or compares colours directly. The status colours double as
# status keys (see utils.analyze_lrc_status).
# ---------------------------------------------------------------------------

import theme as _t

COLOR_BG             = QColor(_t.BASE)
COLOR_FG             = QColor(_t.TEXT)
COLOR_MUTED          = QColor(_t.TEXT_2)
COLOR_FAINT          = QColor(_t.TEXT_3)
COLOR_ACCENT         = QColor(_t.ACCENT)
COLOR_DANGER         = QColor(_t.DANGER)       # errors and destructive actions only
COLOR_SYNCED         = QColor(_t.OK)           # file has good synced LRC
COLOR_UNSYNCED       = QColor(_t.PLAIN)        # lyrics present, timestamps all 0 / missing
COLOR_MISSING        = QColor("#7A766F")       # no LRC file at all (not an error)
COLOR_TIMELINE_BG    = QColor(_t.SURFACE)
COLOR_TIMELINE_BLOCK = QColor("#6E6A63")       # verse marker on the timeline
COLOR_TIMELINE_ACTIVE = QColor(_t.ACCENT)      # currently-selected verse marker
COLOR_TIMELINE_SEL   = QColor(_t.PLAIN)        # drag-selected verse markers
COLOR_SEL_BOX        = QColor(205, 177, 121, 40)  # rubber-band selection fill
