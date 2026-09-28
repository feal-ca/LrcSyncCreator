"""
constants.py — Application-wide constants and Catppuccin Mocha colour tokens.
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
    ".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus"
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
# Catppuccin Mocha colour palette
# ---------------------------------------------------------------------------

COLOR_BG             = QColor("#1e1e2e")   # main window background
COLOR_FG             = QColor("#cdd6f4")   # primary text
COLOR_MUTED          = QColor("#6c7086")   # placeholders, secondary/group labels
COLOR_ACCENT         = QColor("#89b4fa")   # interactive elements / playhead
COLOR_SYNCED         = QColor("#a6e3a1")   # file has good synced LRC
COLOR_UNSYNCED       = QColor("#f9e2af")   # file has LRC but timestamps are all 0
COLOR_MISSING        = QColor("#f38ba8")   # no LRC file at all
COLOR_TIMELINE_BG    = QColor("#181825")   # timeline widget background
COLOR_TIMELINE_BLOCK = QColor("#4a34aa")   # normal verse diamond on timeline
COLOR_TIMELINE_ACTIVE = QColor("#f5c2e7")  # currently-selected verse diamond
COLOR_TIMELINE_SEL   = QColor("#f9e2af")   # drag-selected verse diamond
COLOR_SEL_BOX        = QColor(137, 180, 250, 40)  # rubber-band selection fill
