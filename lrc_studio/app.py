"""
app.py — ``LrcSyncApp``: the main window of LRC Studio.

Key architectural decisions vs. the previous version:
- ``self._verses`` is a plain Python list that acts as the single source of
  truth for the verse order.  The Qt layout always mirrors it; nothing scans
  the layout at runtime.
- ``self._verse_timestamps`` is a cached list of parsed ms values rebuilt
  whenever verses change, used by the timeline and by karaoke/playback-
  highlight lookups (a plain scan — lyric files are small enough that this
  never needs to be more clever, and a scan tolerates out-of-order edits
  that a bisect over an assumed-sorted list would get wrong).
- A lightweight undo stack records the (index, old_timestamp) before every
  sync operation and is replayed by Ctrl-Z.
- ``self._dirty`` tracks unsaved changes; the user is prompted to save
  before switching songs or closing the window while it's set.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

import vlc
from PyQt6.QtCore import Qt, QSettings, QTimer
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from constants import (
    AUDIO_EXTENSIONS,
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_FG,
    COLOR_MISSING,
    COLOR_MUTED,
    COLOR_SYNCED,
    COLOR_UNSYNCED,
    DEFAULT_TIMESTAMP,
    MAX_UNDO,
    POLL_INTERVAL_MS,
    SYNC_OFFSET_MS,
)
from utils import (
    analyze_lrc_status,
    format_ms,
    ms_to_lrc_timestamp,
    parse_lrc_file,
    parse_lrc_timestamp,
    write_embedded_lyrics,
)
import lyrics_qa
from dialogs import SettingsDialog, SoulseekSearchDialog, ValidateSyncDialog
from download_view import DownloadView
from library_panel import LibraryPanel
from soulseek import SlskdClient, SoulseekFile
from spotify_playlist import is_spotify_link
from widgets import TimelineWidget, Verse
from workers import (
    LyricsDownloaderThread,
    LyricsQAThread,
    LyricsSearchThread,
    SoulseekDownloadThread,
    SoulseekSearchThread,
    stop_thread,
)


@dataclass(frozen=True)
class _Command:
    """One entry in the app's single command table.

    Every keyboard-triggerable action is declared exactly once here and
    drives three things that used to be hand-maintained separately (and
    had drifted out of sync with each other): the menu bar, the raw
    QShortcut registrations for actions with no natural menu home, and
    the Help -> Keyboard Shortcuts dialog.
    """

    label: str                        # menu text, e.g. "Add Files…"
    shortcut: str | None              # e.g. "Ctrl+S", or None
    handler: Callable[[], None]
    menu: str | None                  # "file" | "edit" | "tools" | "help" | None
    help_text: str | None = None      # Help-dialog phrasing; defaults to *label*
    separator_before: bool = False    # start a new group within its menu


# Pages of the main window's workspace stack (see LrcSyncApp._init_ui).
WORKSPACE_EDITOR = 0
WORKSPACE_ACQUIRE = 1
WORKSPACE_EMPTY = 2


class LrcSyncApp(QMainWindow):
    """Main window: media player, lyrics editor, timeline, file list."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()

        # ── Application state ─────────────────────────────────────────
        self.settings            = QSettings("LrcStudio", "Configs")
        self.current_file_path:  str  = ""
        self.song_filename:      str  = ""
        self.song_is_loaded:     bool = False
        self.duration_ms:        int  = 1
        self.current_verse_index: int = 0

        # Verse cache — always mirrors the QVBoxLayout order
        self._verses:            list[Verse] = []
        # Timestamp cache in ms — rebuilt via _rebuild_timestamp_cache()
        self._verse_timestamps:  list[int]   = []

        # Undo stack: list of (verse_index, old_timestamp_str) snapshots
        self._undo_stack: list[tuple[Verse, str]] = []

        # Seek-bar drag state (preserves play/pause across slider drags)
        self._was_playing_before_drag: bool = False

        # Guard against double-handling VLC "Ended" state
        self._ended_handled: bool = False

        # Batch downloader thread reference (kept to avoid GC)
        self._downloader: LyricsDownloaderThread | None = None
        # Manual "Web Search" thread reference (kept to avoid GC)
        self._search_thread: LyricsSearchThread | None = None
        # Soulseek search/download thread references (kept to avoid GC)
        self._soulseek_search_thread: SoulseekSearchThread | None = None
        self._soulseek_download_thread: SoulseekDownloadThread | None = None
        self._qa_thread: LyricsQAThread | None = None
        # slskd process we spawned ourselves (None if the user is running
        # their own instance, or we haven't tried to start one yet) — only
        # ever terminated on app exit if we're the one who started it.
        self._slskd_process: subprocess.Popen | None = None

        # Unsaved-changes tracking — drives the save-before-switch/close prompt.
        self._dirty: bool = False
        # Verse currently highlighted as "playing" (independent of nav selection).
        self._playback_highlight_verse: Verse | None = None

        self._init_media_player()
        self._init_ui()
        self._init_shortcuts()
        self._restore_window_state()

    # ==================================================================
    # Media player (VLC)
    # ==================================================================

    def _init_media_player(self) -> None:
        # --aout=pulse routes through PulseAudio/PipeWire and honours
        # the system default sink chosen via pactl.
        self._vlc = vlc.Instance("--aout=pulse", "--no-video")
        self.player = self._vlc.media_player_new()

        saved_vol = float(self.settings.value("volume", 0.7))
        self._volume = int(saved_vol * 100)
        self.player.audio_set_volume(self._volume)

        # Poll timer replaces Qt media-player signals (VLC has none in Python).
        self._poll_timer = QTimer()
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._vlc_poll)
        self._poll_timer.start()

    def _vlc_poll(self) -> None:
        """Called every POLL_INTERVAL_MS ms to sync UI with VLC state."""
        if not self.song_is_loaded:
            return

        state = self.player.get_state()

        if state == vlc.State.Ended:
            if not self._ended_handled:
                self._ended_handled = True
                row = self.library_panel.current_row()
                if row < self.library_panel.row_count() - 1:
                    self._load_file_from_index(row + 1)
                    QTimer.singleShot(300, self.player.play)
                else:
                    self.play_pause_btn.setText("▶")
            return

        self._ended_handled = False

        pos_ms = self.player.get_time()    # -1 when not started
        dur_ms = self.player.get_length()  # 0 when unknown

        if pos_ms >= 0:
            self._on_position_changed(pos_ms)
        if dur_ms > 0 and dur_ms != self.duration_ms:
            self._on_duration_changed(dur_ms)

    def _rebuild_player(self) -> None:
        """Tear down and recreate the VLC instance (used after sink switch)."""
        pos        = self.player.get_time()
        was_playing = self.player.is_playing()
        path       = self.current_file_path

        self.player.stop()
        del self.player
        del self._vlc

        self._vlc   = vlc.Instance("--aout=pulse", "--no-video")
        self.player = self._vlc.media_player_new()
        self.player.audio_set_volume(self._volume)

        if path and os.path.exists(path):
            self.player.set_media(self._vlc.media_new(path))
            self.player.play()

            def _seek_and_restore() -> None:
                self.player.set_time(max(0, pos))
                if not was_playing:
                    self.player.pause()
                    self.play_pause_btn.setText("▶")
                else:
                    self.play_pause_btn.setText("⏸")

            QTimer.singleShot(400, _seek_and_restore)

    # ==================================================================
    # Audio-output sink management (PipeWire / PulseAudio)
    # ==================================================================

    def _get_sink_descriptions(self) -> list[tuple[str, str]]:
        """Return ``[(sink_name, friendly_description), …]`` from pactl.

        PipeWire/PulseAudio sink switching is Linux-specific; on other
        platforms (or when pactl isn't installed) this returns an empty
        list and the device selector is simply omitted from the UI.
        """
        if not sys.platform.startswith("linux"):
            return []
        try:
            result = subprocess.run(
                ["pactl", "list", "sinks"],
                capture_output=True, text=True, check=False,
            )
            sinks: list[tuple[str, str]] = []
            current_name: str | None = None
            for line in result.stdout.splitlines():
                if "Name:" in line:
                    current_name = line.split("Name:", 1)[1].strip()
                elif "Description:" in line and current_name:
                    desc = line.split("Description:", 1)[1].strip()
                    sinks.append((current_name, desc))
                    current_name = None
            return sinks
        except FileNotFoundError:
            # pactl not available (non-Linux system)
            return []
        except Exception as exc:
            print(f"[warn] pactl error: {exc}")
            return []

    def _set_pipewire_sink(self, sink_name: str) -> None:
        if not sink_name:
            return
        subprocess.run(
            ["pactl", "set-default-sink", sink_name],
            check=False,
        )
        self._rebuild_player()
        self.status_bar.setText(f"Audio output: {sink_name}")

    # ==================================================================
    # UI construction
    # ==================================================================

    def _init_ui(self) -> None:
        self.setWindowTitle("LRC Studio")
        self.resize(1200, 850)
        self.setAcceptDrops(True)

        self._commands = self._build_commands()
        self._build_menu_bar()

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(15, 15, 15, 15)
        root_layout.setSpacing(10)

        root_layout.addLayout(self._build_top_bar())

        # ── Karaoke display ───────────────────────────────────────────
        self.karaoke_label = QLabel("Waiting for song…")
        self.karaoke_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.karaoke_label.setStyleSheet(
            f"font-size: 26px; font-weight: bold; color: {COLOR_UNSYNCED.name()};"
            " padding: 15px; background-color: #11111b;"
            " border-radius: 10px; border: 1px solid #313244;"
        )
        self.karaoke_label.setFixedHeight(80)
        root_layout.addWidget(self.karaoke_label)

        # ── Timeline ──────────────────────────────────────────────────
        self.timeline = TimelineWidget()
        self.timeline.setMouseTracking(True)
        self.timeline.time_changed_bulk.connect(self._update_verses_from_timeline)
        self.timeline.seek_requested.connect(self.set_position)
        root_layout.addWidget(self.timeline)

        # ── Splitter: library panel | workspace ─────────────────────────
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.library_panel = LibraryPanel(self.settings)
        self.library_panel.song_selected.connect(self._on_library_song_selected)
        self.library_panel.file_renamed.connect(self._on_file_renamed)
        self.library_panel.find_music_requested.connect(self._find_music)
        self.library_panel.fetch_missing_lyrics_requested.connect(self._batch_download_lyrics)
        self.splitter.addWidget(self.library_panel)

        self.workspace = QStackedWidget()
        self.workspace.addWidget(self._build_lyrics_panel())      # WORKSPACE_EDITOR = 0

        self.download_view = DownloadView(
            self.settings, self._get_slskd_client, self._collect_library_paths, self,
        )
        self.download_view.files_ready.connect(self._on_download_files_ready)
        self.download_view.file_downloaded.connect(self.library_panel.add_files_single)
        self.download_view.open_in_library.connect(self._open_in_library)
        self.workspace.addWidget(self.download_view)               # WORKSPACE_ACQUIRE = 1

        self.workspace.addWidget(self._build_empty_state())         # WORKSPACE_EMPTY = 2

        self.splitter.addWidget(self.workspace)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        root_layout.addWidget(self.splitter, stretch=1)

        # ── Status bar ────────────────────────────────────────────────
        self.status_bar = QLabel("Ready")
        self.status_bar.setStyleSheet("font-size: 11px; color: #a6adc8;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {COLOR_SYNCED.name()}; }}"
        )

        stat_row = QHBoxLayout()
        stat_row.addWidget(self.status_bar)
        stat_row.addWidget(self.progress_bar)
        root_layout.addLayout(stat_row)

        self.workspace.setCurrentIndex(WORKSPACE_EMPTY)

    def _build_empty_state(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        label = QLabel("Select a song from your library, or add music to get started.")
        label.setStyleSheet(f"color: {COLOR_MUTED.name()}; font-size: 14px;")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        self._empty_state_add_music_button = QPushButton("+ Add Music")
        self._empty_state_add_music_button.clicked.connect(self.library_panel.find_music_requested.emit)
        self._empty_state_add_music_button.setStyleSheet(self._primary_button_style())
        layout.addWidget(self._empty_state_add_music_button, alignment=Qt.AlignmentFlag.AlignCenter)

        return page

    def _on_download_files_ready(self, paths: list[str]) -> None:
        self.library_panel.add_files(paths)
        self._fetch_lyrics_for(paths)
        if paths:
            self._load_song(paths[0])
            self.library_panel.select_path(paths[0])

    def _open_in_library(self, path: str) -> None:
        if not self._confirm_discard_if_dirty():
            return
        self._load_song(path)
        self.library_panel.select_path(path)

    def _build_commands(self) -> list[_Command]:
        """The single source of truth for every keyboard-triggerable
        action — see ``_Command`` for what building from this buys.

        Playback/navigation commands (menu=None) get no menu entry on
        purpose: they're real-time transport controls a person reaches
        for by muscle memory while a song is playing, not discrete
        one-shot commands someone would browse for in a dropdown — the
        same reason a media player doesn't put "seek forward" in a menu.
        They're listed first since they're what's used most while
        actually syncing, and they still show up in the Help dialog.
        """
        return [
            _Command("Play / Pause", "Space", self._toggle_play_pause_smart, None),
            _Command(
                "Sync current line, advance", "S", self._sync_next_verse, None,
                help_text="Sync current line to playback, advance",
            ),
            _Command("Select previous line", "Up", lambda: self._select_verse(self.current_verse_index - 1), None),
            _Command("Select next line", "Down", lambda: self._select_verse(self.current_verse_index + 1), None),
            _Command("Seek -5s", "Left", lambda: self._seek_delta(-5_000), None),
            _Command("Seek +5s", "Right", lambda: self._seek_delta(5_000), None),

            # self.library_panel doesn't exist yet at _build_commands() time
            # (it's constructed later in _init_ui) — lazy lookup via lambda.
            _Command("Add Files…", "Ctrl+O", lambda: self.library_panel.open_add_files_dialog(), "file"),
            _Command("Save LRC", "Ctrl+S", self._save_lrc_file, "file"),
            _Command("Exit", "Ctrl+Q", self.close, "file", separator_before=True),

            _Command("Undo Sync", "Ctrl+Z", self._undo_last_sync, "edit"),
            _Command("Sort Lines by Time", "Ctrl+T", self._sort_verses_by_time, "edit"),
            _Command("Shift All Timestamps…", "Ctrl+Shift+O", self._shift_all_timestamps, "edit"),
            _Command("Clear All Lyrics", None, self._clear_lyrics, "edit", separator_before=True),

            _Command("Find Music…", "Ctrl+F", self._find_music, "tools"),
            _Command("Validate Sync…", None, self._validate_sync, "tools"),
            _Command("Settings…", None, self._open_settings_dialog, "tools", separator_before=True),

            _Command("Keyboard Shortcuts…", None, self._show_shortcuts_dialog, "help"),
            _Command("About LRC Studio", None, self._show_about_dialog, "help"),
        ]

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()
        menus = {
            "file":  menu_bar.addMenu("&File"),
            "edit":  menu_bar.addMenu("&Edit"),
            "tools": menu_bar.addMenu("&Tools"),
            "help":  menu_bar.addMenu("&Help"),
        }
        for cmd in self._commands:
            if cmd.menu is None:
                continue
            target = menus[cmd.menu]
            if cmd.separator_before:
                target.addSeparator()
            action = QAction(cmd.label, target)
            if cmd.shortcut:
                action.setShortcut(QKeySequence(cmd.shortcut))
            action.triggered.connect(cmd.handler)
            target.addAction(action)

    def _show_shortcuts_dialog(self) -> None:
        rows = "".join(
            f"<tr><td><b>{cmd.shortcut}</b></td><td>{cmd.help_text or cmd.label}</td></tr>"
            for cmd in self._commands if cmd.shortcut
        )
        QMessageBox.information(
            self,
            "Keyboard Shortcuts",
            "<table cellspacing=6>"
            f"{rows}"
            # Handled inside each line's own text field, not a global
            # shortcut — not part of the command table above.
            "<tr><td><b>Enter</b> (in a line)</td><td>Move to the next line</td></tr>"
            "</table>",
        )

    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            "About LRC Studio",
            "<b>LRC Studio</b><br>"
            "A visual tool for creating and syncing .lrc lyric files.<br><br>"
            "Built with PyQt6 and VLC.",
        )

    def _build_top_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(15)

        # Play/pause button
        self.play_pause_btn = QPushButton("▶")
        self.play_pause_btn.setFixedSize(45, 45)
        self.play_pause_btn.clicked.connect(self._toggle_play_pause)
        self.play_pause_btn.setStyleSheet(
            f"background-color: {COLOR_ACCENT.name()}; color: #1e1e2e;"
            " border-radius: 22px; font-size: 20px; font-weight: bold;"
        )

        # Time labels
        self.lbl_curr = QLabel("00:00")
        self.lbl_tot  = QLabel("00:00")
        for lbl in (self.lbl_curr, self.lbl_tot):
            lbl.setStyleSheet(
                "font-family: monospace; font-size: 14px;"
                " font-weight: bold; color: #cdd6f4;"
            )

        # Seek bar — preserves play state across drags (bug fix)
        self.seek_bar = QSlider(Qt.Orientation.Horizontal)
        self.seek_bar.setFixedHeight(20)
        self.seek_bar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.seek_bar.sliderMoved.connect(self.set_position)
        self.seek_bar.sliderPressed.connect(self._on_seek_pressed)
        self.seek_bar.sliderReleased.connect(self._on_seek_released)

        # Volume
        self.vol_icon   = QLabel("🔊")
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(self._volume)
        self.vol_slider.setFixedWidth(100)
        self.vol_slider.valueChanged.connect(self._set_volume)

        # File label
        self.file_label = QLabel("No Song Loaded")
        self.file_label.setStyleSheet(
            "font-weight: bold; font-size: 14px; color: #7f849c;"
        )

        # Audio output selector — Linux/PipeWire only; omitted elsewhere.
        sinks = self._get_sink_descriptions()
        self.device_combo: QComboBox | None = None
        if sinks:
            self.device_combo = QComboBox()
            self.device_combo.setFixedWidth(220)
            self.device_combo.setToolTip("Audio Output Device")
            self.device_combo.setStyleSheet(
                "QComboBox { background-color: #313244; color: white;"
                " border: 1px solid #45475a; padding: 4px; border-radius: 4px; }"
                "QComboBox QAbstractItemView { background-color: #1e1e2e; color: white; }"
            )
            # Block signals while populating so the initial selection doesn't
            # trigger a premature sink switch.
            self.device_combo.blockSignals(True)
            for sink_name, desc in sinks:
                self.device_combo.addItem(desc, sink_name)
            self.device_combo.blockSignals(False)
            self.device_combo.currentIndexChanged.connect(
                lambda: self._set_pipewire_sink(self.device_combo.currentData())
            )

        row.addWidget(self.play_pause_btn)
        row.addWidget(self.lbl_curr)
        row.addWidget(self.seek_bar)
        row.addWidget(self.lbl_tot)
        row.addWidget(self.vol_icon)
        row.addWidget(self.vol_slider)
        if self.device_combo is not None:
            row.addWidget(self.device_combo)
        row.addStretch()
        row.addWidget(self.file_label)
        return row

    @staticmethod
    def _neutral_button_style() -> str:
        """The default look for a panel action button — used by every
        button in the file panel and most of the lyrics panel, so a
        button's importance (or lack of it) is legible at a glance
        instead of every button competing for attention with its own
        color."""
        return (
            f"QPushButton {{ background-color: #313244; color: {COLOR_FG.name()};"
            " border: 1px solid #45475a; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #45475a; }"
        )

    @staticmethod
    def _primary_button_style() -> str:
        """For the one action per panel worth calling out as primary
        (currently just Save LRC) — accented, not just bigger/bolder."""
        return (
            f"QPushButton {{ background-color: {COLOR_ACCENT.name()}; color: #1e1e2e;"
            " font-weight: bold; padding: 8px; border-radius: 4px; }"
            f"QPushButton:hover {{ background-color: {COLOR_ACCENT.lighter(110).name()}; }}"
        )

    @staticmethod
    def _danger_button_style() -> str:
        """For genuinely destructive actions only (currently just Clear
        All) — an outline rather than a solid fill, so it reads as
        "careful" without competing with a real error/missing state."""
        return (
            f"QPushButton {{ background-color: transparent; color: {COLOR_MISSING.name()};"
            f" border: 1px solid {COLOR_MISSING.name()}; padding: 8px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: {COLOR_MISSING.name()}; color: #1e1e2e; }}"
        )

    @staticmethod
    def _make_group_label(text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setStyleSheet(
            f"color: {COLOR_MUTED.name()}; font-size: 10px; font-weight: bold;"
            " letter-spacing: 1px; margin-top: 6px;"
        )
        return label

    def _build_lyrics_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Scrollable verse list
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("background-color: #1e1e2e; border: none;")
        self._scroll_content = QWidget()
        self.lyrics_layout   = QVBoxLayout(self._scroll_content)
        self.lyrics_layout.setSpacing(2)
        self.lyrics_layout.addStretch()
        self.scroll_area.setWidget(self._scroll_content)
        layout.addWidget(self.scroll_area)

        # Bottom actions. Save is the one action worth calling out as
        # primary; everything else is grouped the same way the file panel
        # groups its own buttons, and color is spent the way it's spent
        # everywhere else in the app — status colors aren't decoration,
        # so they're not reused here just to make five buttons look less
        # identical. Clear All is the one genuinely destructive action
        # and is styled (and placed) to read as separate from the rest.
        save_btn = QPushButton("Save LRC")
        save_btn.setToolTip("Save the .lrc sidecar file (Ctrl+S)")
        save_btn.clicked.connect(self._save_lrc_file)
        save_btn.setStyleSheet(self._primary_button_style())
        layout.addWidget(save_btn)

        layout.addWidget(self._make_group_label("Fetch"))
        fetch_row = QHBoxLayout()
        get_lyrics_btn = QPushButton("Get Lyrics")
        get_lyrics_btn.setToolTip("Search online for synced lyrics for this song")
        get_lyrics_btn.clicked.connect(self._get_lyrics_online)
        get_lyrics_btn.setStyleSheet(self._neutral_button_style())
        fetch_row.addWidget(get_lyrics_btn)
        layout.addLayout(fetch_row)

        layout.addWidget(self._make_group_label("Edit"))
        edit_row = QHBoxLayout()
        for label, handler, tooltip in [
            ("Sort Lines", self._sort_verses_by_time,
             "Reorder verses by their timestamp (Ctrl+T)"),
            ("Shift All", self._shift_all_timestamps,
             "Nudge every timestamp by the same +/- amount — fixes a track that's synced but drifting"),
        ]:
            btn = QPushButton(label)
            btn.setToolTip(tooltip)
            btn.clicked.connect(handler)
            btn.setStyleSheet(self._neutral_button_style())
            edit_row.addWidget(btn)
        layout.addLayout(edit_row)

        clear_btn = QPushButton("Clear All")
        clear_btn.setToolTip("Remove every verse and start this song's lyrics over")
        clear_btn.clicked.connect(self._clear_lyrics)
        clear_btn.setStyleSheet(self._danger_button_style())
        layout.addWidget(clear_btn)

        # Optional: also write lyrics into the audio file's own tags on save.
        self.embed_checkbox = QCheckBox("Also embed lyrics in audio file tags")
        self.embed_checkbox.setStyleSheet(f"color: {COLOR_FG.name()}; font-size: 12px;")
        self.embed_checkbox.setChecked(bool(self.settings.value("embed_lyrics", False, type=bool)))
        self.embed_checkbox.toggled.connect(
            lambda checked: self.settings.setValue("embed_lyrics", checked)
        )
        layout.addWidget(self.embed_checkbox)

        return panel

    def _init_shortcuts(self) -> None:
        """Register the playback/navigation commands — the only ones with
        no menu entry (see _build_commands), so QAction's own shortcut
        handling doesn't cover them and they need a raw QShortcut."""
        for cmd in self._commands:
            if cmd.menu is not None or not cmd.shortcut:
                continue
            sc = QShortcut(QKeySequence(cmd.shortcut), self)
            sc.activated.connect(cmd.handler)

    # ==================================================================
    # Player controls
    # ==================================================================

    def _toggle_play_pause(self) -> None:
        if not self.song_is_loaded:
            return
        if self.player.is_playing():
            self.player.pause()
            self.play_pause_btn.setText("▶")
        else:
            self.player.play()
            self.play_pause_btn.setText("⏸")

    def _toggle_play_pause_smart(self) -> None:
        """Ignore Space when a text field has focus."""
        if not isinstance(QApplication.focusWidget(), QLineEdit):
            self._toggle_play_pause()

    def _seek_delta(self, delta_ms: int) -> None:
        if not self.song_is_loaded:
            return
        new_pos = self.player.get_time() + delta_ms
        self.player.set_time(max(0, min(new_pos, self.duration_ms)))

    def set_position(self, position_ms: int) -> None:
        self.player.set_time(int(position_ms))

    def _set_volume(self, val: int) -> None:
        self._volume = val
        self.player.audio_set_volume(val)
        self.settings.setValue("volume", val / 100.0)

    def _on_seek_pressed(self) -> None:
        """Remember play state; pause while the user drags."""
        self._was_playing_before_drag = bool(self.player.is_playing())
        self.player.pause()

    def _on_seek_released(self) -> None:
        """Restore play state after drag ends."""
        if self._was_playing_before_drag:
            self.player.play()
            self.play_pause_btn.setText("⏸")
        else:
            self.play_pause_btn.setText("▶")

    def _on_position_changed(self, pos_ms: int) -> None:
        if not self.seek_bar.isSliderDown():
            self.seek_bar.setValue(pos_ms)
        self.lbl_curr.setText(format_ms(pos_ms))
        self._update_karaoke(pos_ms)
        self.timeline.set_position(pos_ms)

    def _on_duration_changed(self, dur_ms: int) -> None:
        self.duration_ms = dur_ms
        self.seek_bar.setRange(0, dur_ms)
        self.lbl_tot.setText(format_ms(dur_ms))
        self._update_timeline()

    # ==================================================================
    # Verse cache & timestamp cache
    # ==================================================================

    def _rebuild_timestamp_cache(self) -> None:
        """Rebuild the sorted-ms cache used by ``_update_karaoke``."""
        self._verse_timestamps = [
            parse_lrc_timestamp(v.get_timestamp()) for v in self._verses
        ]

    def _update_timeline(self) -> None:
        """Rebuild the timestamp cache and refresh the timeline widget."""
        self._rebuild_timestamp_cache()
        self.timeline.load_data(self._verses, self.duration_ms)
        self.timeline.set_active_index(self.current_verse_index)

    # ==================================================================
    # Karaoke display
    # ==================================================================

    def _update_karaoke(self, position_ms: int) -> None:
        """Update the karaoke label and playback highlight for *position_ms*.

        Scans for the latest verse whose timestamp is both real (not the
        un-stamped ``DEFAULT_TIMESTAMP``) and at/before *position_ms*. Lines
        that haven't been synced yet are never considered "active" — with
        an unsynced tail sharing the same ``[00:00.00]`` stamp, treating
        them as real timestamps would just make the last one "win".
        """
        best_idx = None
        target = position_ms + SYNC_OFFSET_MS
        for i, verse in enumerate(self._verses):
            if verse.get_timestamp() == DEFAULT_TIMESTAMP:
                continue
            ms = self._verse_timestamps[i]
            if ms <= target and (best_idx is None or ms >= self._verse_timestamps[best_idx]):
                best_idx = i

        if best_idx is None:
            self.karaoke_label.setText("♪")
            self._set_playback_highlight(None)
            return

        text = self._verses[best_idx].get_verse_text()
        self.karaoke_label.setText(text or "♪")
        self._set_playback_highlight(self._verses[best_idx])

    def _set_playback_highlight(self, verse: Verse | None) -> None:
        """Move the "currently playing" visual marker to *verse* (or clear it)."""
        if verse is self._playback_highlight_verse:
            return
        if self._playback_highlight_verse is not None:
            self._playback_highlight_verse.set_playing(False)
        self._playback_highlight_verse = verse
        if verse is not None:
            verse.set_playing(True)
            # Don't yank the scroll position away while the user is typing.
            if not isinstance(QApplication.focusWidget(), QLineEdit):
                self.scroll_area.ensureWidgetVisible(verse, 0, 300)

    # ==================================================================
    # Verse management
    # ==================================================================

    def _add_verse(
        self,
        timestamp: str,
        text: str,
        after_widget: Verse | None = None,
    ) -> Verse:
        """Create a Verse, insert it into the cache and layout, and return it."""
        verse = Verse(timestamp, text)
        verse.sync_button.clicked.connect(partial(self._sync_verse, verse))
        verse.delete_button.clicked.connect(partial(self._delete_verse, verse))
        verse.add_button.clicked.connect(partial(self._handle_add_btn, verse))
        verse.jump_signal.connect(self._handle_jump_click)
        verse.advance_requested.connect(self._focus_next_verse)
        verse.verse_text.textChanged.connect(self._mark_dirty)

        if after_widget is not None and after_widget in self._verses:
            idx = self._verses.index(after_widget)
            self._verses.insert(idx + 1, verse)
            self.lyrics_layout.insertWidget(idx + 1, verse)
        else:
            self._verses.append(verse)
            # Insert before the trailing stretch item.
            self.lyrics_layout.insertWidget(self.lyrics_layout.count() - 1, verse)

        return verse

    def _handle_add_btn(self, widget: Verse) -> None:
        self._add_verse(DEFAULT_TIMESTAMP, "", after_widget=widget)
        self._update_timeline()
        self._mark_dirty()

    def _focus_next_verse(self, verse: Verse) -> None:
        """Enter in a line's text field jumps to the next line (spreadsheet-style).

        Creates a new blank trailing line on demand so typing out a whole
        set of lyrics doesn't require reaching for the "+" button.
        """
        if verse not in self._verses:
            return
        idx = self._verses.index(verse)
        if idx + 1 < len(self._verses):
            nxt = self._verses[idx + 1]
        else:
            nxt = self._add_verse(DEFAULT_TIMESTAMP, "", after_widget=verse)
            self._update_timeline()
        nxt.verse_text.setFocus()
        nxt.verse_text.selectAll()

    def _delete_verse(self, widget: Verse) -> None:
        if widget in self._verses:
            self._verses.remove(widget)
        if widget is self._playback_highlight_verse:
            self._playback_highlight_verse = None
        self.lyrics_layout.removeWidget(widget)
        widget.deleteLater()
        self._select_verse(self.current_verse_index)
        QTimer.singleShot(50, self._update_timeline)
        self._mark_dirty()

    def _replace_all_verses(self, lines: list[tuple[str, str]]) -> None:
        """Wipe the current verse list and replace it with *lines*
        ((timestamp_str, text) pairs) — no confirmation. The primitive
        behind both Get Lyrics (an intentional replace) and Clear All
        (which adds its own confirmation before calling this with [])."""
        for v in self._verses:
            self.lyrics_layout.removeWidget(v)
            v.deleteLater()
        self._verses.clear()
        self._playback_highlight_verse = None
        self.current_verse_index = 0
        for ts_str, content in lines:
            self._add_verse(ts_str, content)
        self._update_timeline()
        self._mark_dirty()

    def _clear_lyrics(self) -> None:
        # The one action in the app with no undo path at all — unlike a
        # sync (Ctrl-Z) or a song switch (caught by the dirty-check
        # prompt), this destroys every verse immediately. It's the only
        # button in the whole UI that needed its own confirmation to
        # match how destructive it actually is.
        if self._verses and QMessageBox.question(
            self,
            "Clear All Lyrics",
            f"Remove all {len(self._verses)} line(s) for "
            f"'{self.song_filename}'? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._replace_all_verses([])

    def _select_verse(self, index: int) -> None:
        if not self._verses:
            return
        index = max(0, min(index, len(self._verses) - 1))
        for i, verse in enumerate(self._verses):
            verse.set_active(i == index)
        self._verses[index]  # ensure in-range
        self.scroll_area.ensureWidgetVisible(self._verses[index], 0, 300)
        self.current_verse_index = index
        self.timeline.set_active_index(index)

    def _handle_jump_click(self, verse: Verse) -> None:
        try:
            ms = parse_lrc_timestamp(verse.get_timestamp())
            self.player.set_time(ms)
            self.player.play()
            self.play_pause_btn.setText("⏸")
            if verse in self._verses:
                self._select_verse(self._verses.index(verse))
        except Exception:
            pass

    # ==================================================================
    # Sync operations
    # ==================================================================

    def _sync_verse(self, verse: Verse) -> None:
        """Stamp *verse* with the current playback position - SYNC_OFFSET."""
        # Save old timestamp to undo stack before overwriting.
        if verse in self._verses:
            self._undo_stack.append((verse, verse.get_timestamp()))
            if len(self._undo_stack) > MAX_UNDO:
                self._undo_stack.pop(0)

        ms = max(0, self.player.get_time() - SYNC_OFFSET_MS)
        verse.set_timestamp(ms_to_lrc_timestamp(ms))
        self._update_timeline()
        self._mark_dirty()

    def _sync_next_verse(self) -> None:
        """Sync the currently selected verse and advance to the next."""
        if not self._verses or self.current_verse_index >= len(self._verses):
            return
        self._sync_verse(self._verses[self.current_verse_index])
        self._select_verse(self.current_verse_index + 1)

    def _undo_last_sync(self) -> None:
        """Revert the most recent sync operation (Ctrl-Z).

        Undo entries are keyed by verse identity rather than list index, so
        a reorder (sort) or a delete of an unrelated line can't make this
        restore a timestamp onto the wrong verse. Entries for verses that
        no longer exist are skipped rather than applied.
        """
        while self._undo_stack:
            verse, old_ts = self._undo_stack.pop()
            if verse not in self._verses:
                continue
            idx = self._verses.index(verse)
            verse.set_timestamp(old_ts)
            self._update_timeline()
            self._mark_dirty()
            self.status_bar.setText(f"Undid sync on line {idx + 1}.")
            return
        self.status_bar.setText("Nothing to undo.")

    def _sort_verses_by_time(self) -> None:
        """Sort all verses in ascending timestamp order (Ctrl-T)."""
        if not self._verses:
            return
        self._verses.sort(key=lambda v: parse_lrc_timestamp(v.get_timestamp()))
        # Re-insert widgets into the layout in the new order.
        for v in self._verses:
            self.lyrics_layout.removeWidget(v)
        for i, v in enumerate(self._verses):
            self.lyrics_layout.insertWidget(i, v)
        self._update_timeline()
        self._mark_dirty()
        self.status_bar.setText("Verses sorted by timestamp.")

    def _shift_all_timestamps(self) -> None:
        """Shift every verse's timestamp by a user-given delta, in ms (Ctrl-Shift-O).

        Handy when a whole track's sync has drifted uniformly — e.g. lyrics
        downloaded for a slightly different edit/remaster of the song.
        """
        if not self._verses:
            return
        delta, ok = QInputDialog.getInt(
            self,
            "Shift All Timestamps",
            "Offset in milliseconds (positive delays, negative advances):",
            0, -600_000, 600_000, 100,
        )
        if not ok or delta == 0:
            return
        for verse in self._verses:
            old_ts = verse.get_timestamp()
            self._undo_stack.append((verse, old_ts))
            if len(self._undo_stack) > MAX_UNDO:
                self._undo_stack.pop(0)
            new_ms = max(0, parse_lrc_timestamp(old_ts) + delta)
            verse.set_timestamp(ms_to_lrc_timestamp(new_ms))
        self._update_timeline()
        self._mark_dirty()
        self.status_bar.setText(f"Shifted all timestamps by {delta:+d} ms.")

    # ==================================================================
    # Timeline ↔ verse sync
    # ==================================================================

    def _update_verses_from_timeline(
        self, updates: list[tuple[int, int]]
    ) -> None:
        """Apply bulk timestamp changes originating from a timeline drag."""
        for index, new_ms in updates:
            if 0 <= index < len(self._verses):
                self._verses[index].set_timestamp(ms_to_lrc_timestamp(new_ms))
        self._rebuild_timestamp_cache()
        self._mark_dirty()

    # ==================================================================
    # Save / Load / Search
    # ==================================================================

    def _save_lrc_file(self) -> None:
        if not self.current_file_path:
            return
        lrc_path = os.path.splitext(self.current_file_path)[0] + ".lrc"
        lrc_text = "".join(f"{v.get_timestamp()} {v.get_verse_text()}\n" for v in self._verses)
        try:
            with open(lrc_path, "w", encoding="utf-8") as fh:
                fh.write(lrc_text)
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Could not save file:\n{exc}")
            return

        status = f"Saved: {os.path.basename(lrc_path)}"
        if self.embed_checkbox.isChecked():
            try:
                write_embedded_lyrics(self.current_file_path, lrc_text)
                status += " (+ embedded in file tags)"
            except Exception as exc:
                status += f" — embedding failed: {exc}"

        self._dirty = False
        self._update_title()
        self.status_bar.setText(status)
        self.library_panel.refresh_status(self.current_file_path)
        self.file_label.setStyleSheet(
            f"font-weight: bold; font-size: 14px; color: {COLOR_SYNCED.name()};"
        )

    def _get_lyrics_online(self) -> None:
        """Search for synced lyrics online for a free-text query (threaded)."""
        if not self.song_filename:
            return
        query, ok = QInputDialog.getText(
            self,
            "Search Lyrics",
            "Artist - Title:",
            QLineEdit.EchoMode.Normal,
            os.path.splitext(self.song_filename)[0],
        )
        if not ok or not query:
            return

        self.status_bar.setText("Searching…")

        self._search_thread = LyricsSearchThread(query)
        self._search_thread.result_ready.connect(self._on_search_result)
        self._search_thread.start()

    def _on_search_result(self, result: str) -> None:
        if result:
            self._replace_all_verses([])
            self._parse_and_add_lyrics(result)
            self._mark_dirty()
            self.status_bar.setText("Lyrics found!")
        else:
            QMessageBox.information(self, "Sorry", "No lyrics found.")
            self.status_bar.setText("Ready")

    def _parse_and_add_lyrics(self, text: str) -> None:
        lines, offset_ms = parse_lrc_file(text)

        if offset_ms:
            self.status_bar.setText(
                f"Applied LRC global offset: {offset_ms:+d} ms"
            )

        for ts_str, content in lines:
            # Apply global offset if present.
            if offset_ms:
                base_ms  = parse_lrc_timestamp(ts_str)
                adjusted = max(0, base_ms + offset_ms)
                ts_str   = ms_to_lrc_timestamp(adjusted)
            self._add_verse(ts_str, content)

        self._update_timeline()

    # ==================================================================
    # File list management
    # ==================================================================

    def _process_added_files(self, file_paths: list[str]) -> None:
        self.library_panel.add_files(file_paths)

        # Checked via real_file_paths(), not row_count(): if nothing new
        # was actually added (e.g. all duplicates), row_count() could still
        # reflect a placeholder row and be mistaken for a loadable row 0.
        if not self.song_is_loaded and self.library_panel.real_file_paths():
            self._load_file_from_index(0)

    def _batch_download_lyrics(self, files: list[str]) -> None:
        if not files:
            QMessageBox.information(self, "Info", "No missing lyrics to download.")
            return

        self.progress_bar.setRange(0, len(files))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self._downloader = LyricsDownloaderThread(files)
        self._downloader.progress.connect(self._on_download_progress)
        self._downloader.finished_batch.connect(self._on_batch_finished)
        self._downloader.start()

    def _on_download_progress(
        self, message: str, done: int, total: int
    ) -> None:
        self.status_bar.setText(f"[{done}/{total}] {message}")
        self.progress_bar.setValue(done)

    def _on_batch_finished(
        self, success: list[str], failed: list[str]
    ) -> None:
        self.progress_bar.setVisible(False)
        parts = [f"Downloaded {len(success)} lyrics."]
        if failed:
            parts.append(f"Failed: {len(failed)}.")
        self.status_bar.setText("  ".join(parts))

        for path in self.library_panel.real_file_paths():
            self.library_panel.refresh_status(path)
        # Reload lyrics pane if the current song was among the downloads.
        if (
            self.current_file_path
            and os.path.basename(self.current_file_path) in success
        ):
            self._load_song(self.current_file_path)

    def _fetch_lyrics_for(self, file_paths: list[str]) -> None:
        """Kick off a lyrics-download pass for specific files (not the whole
        library). Reuses the exact same downloader/handlers as the "Download
        Lyrics" button — used to auto-fetch lyrics right after a Soulseek or
        Spotify-triggered download lands.
        """
        if not file_paths:
            return
        self.progress_bar.setRange(0, len(file_paths))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self._downloader = LyricsDownloaderThread(file_paths)
        self._downloader.progress.connect(self._on_download_progress)
        self._downloader.finished_batch.connect(self._on_batch_finished)
        self._downloader.start()

    # ==================================================================
    # Soulseek integration (via slskd — see lrc_studio/soulseek.py)
    # ==================================================================

    def _get_slskd_client(self) -> SlskdClient | None:
        url = str(self.settings.value("slskd_url", "")).strip()
        key = str(self.settings.value("slskd_api_key", "")).strip()
        if not url or not key:
            QMessageBox.information(
                self,
                "Soulseek Not Configured",
                "Set up slskd first:\n\n"
                "  ./scripts/setup_slskd.sh\n\n"
                "Then fill in its URL and API key under Tools → Settings — "
                "LRC Studio will start slskd itself from there on.",
            )
            return None

        client = SlskdClient(url, key)
        ok, _ = client.check_connection()
        if ok:
            return client

        if not self._ensure_slskd_running():
            return None
        if self._wait_for_slskd(client):
            return client

        # slskd's process was alive the whole wait but never reached a
        # logged-in state — most often it lost its Soulseek connection
        # earlier in the session and isn't reconnecting on its own.
        # Waiting longer doesn't help that; a fresh restart usually does.
        # Only do this for a process we launched ourselves — never kill
        # an instance the user is running (and watching) by hand.
        if self._slskd_process is not None and self._restart_slskd():
            if self._wait_for_slskd(client):
                return client

        QMessageBox.warning(
            self,
            "Soulseek Unavailable",
            f"slskd didn't come up in time at {url}, even after a restart. "
            "Run ./scripts/run_slskd.sh yourself to see what's going wrong, "
            "or check its URL/API key under Tools → Settings.",
        )
        return None

    def _restart_slskd(self) -> bool:
        """Kill and relaunch the slskd process we control."""
        if self._slskd_process is not None and self._slskd_process.poll() is None:
            self.status_bar.setText("slskd seems stuck — restarting it…")
            QApplication.processEvents()
            try:
                self._slskd_process.terminate()
                self._slskd_process.wait(timeout=5)
            except Exception:
                try:
                    self._slskd_process.kill()
                except Exception:
                    pass
        self._slskd_process = None
        return self._ensure_slskd_running()

    def _ensure_slskd_running(self) -> bool:
        """Launch slskd via ``scripts/run_slskd.sh`` if it isn't already
        running under us. Returns False (with an explanatory dialog) only
        when we can't even attempt to start it — e.g. the binary was never
        installed via ``setup_slskd.sh``.
        """
        if self._slskd_process is not None and self._slskd_process.poll() is None:
            return True  # already starting/running from a previous call

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(root_dir, "scripts", "run_slskd.sh")
        binary_path = os.path.join(root_dir, "slskd-bin", "slskd")
        if not os.path.isfile(binary_path):
            QMessageBox.information(
                self,
                "Soulseek Not Set Up",
                "slskd isn't installed yet. Run this once from a terminal:\n\n"
                "  ./scripts/setup_slskd.sh\n\n"
                "Then fill in its credentials in slskd-bin/slskd.yml and try again.",
            )
            return False

        self.status_bar.setText("Starting slskd…")
        QApplication.processEvents()
        try:
            self._slskd_process = subprocess.Popen(
                [script_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            QMessageBox.warning(
                self, "Couldn't Start slskd", f"Failed to launch {script_path}: {exc}",
            )
            return False
        return True

    def _wait_for_slskd(self, client: SlskdClient, timeout_s: float = 25.0) -> bool:
        """Block (while pumping the event loop) until *client* reports a
        live, logged-in slskd, or *timeout_s* elapses."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._slskd_process is not None and self._slskd_process.poll() is not None:
                return False  # the process we started already exited
            ok, msg = client.check_connection()
            if ok:
                self.status_bar.setText(msg)
                return True
            self.status_bar.setText(f"Starting slskd… ({msg})")
            QApplication.processEvents()
            time.sleep(0.5)
            QApplication.processEvents()
        return False

    def _open_settings_dialog(self) -> None:
        SettingsDialog(self.settings, self).exec()

    def _find_music(self) -> None:
        if self._soulseek_download_thread is not None and self._soulseek_download_thread.isRunning():
            QMessageBox.information(
                self, "Soulseek Download In Progress",
                "A Soulseek download is already running — wait for it to "
                "finish before starting another one.",
            )
            return

        client = self._get_slskd_client()
        if client is None:
            return

        dialog = SoulseekSearchDialog("", self)
        dialog.query_input.setPlaceholderText("Search Soulseek, or paste a Spotify link…")
        self._active_find_music_dialog = dialog   # exposed for testability

        def run_search() -> None:
            query = dialog.query_input.text().strip()
            if not query:
                return
            if is_spotify_link(query):
                dialog.reject()
                self._open_batch_import(query)
                return
            dialog.set_searching(True)
            self._soulseek_search_thread = SoulseekSearchThread(client, query)
            self._soulseek_search_thread.results_ready.connect(
                lambda results: (dialog.set_searching(False), dialog.show_results(results))
            )
            self._soulseek_search_thread.search_failed.connect(
                lambda msg: (dialog.set_searching(False), dialog.show_error(msg))
            )
            self._soulseek_search_thread.start()

        dialog.search_button.clicked.connect(run_search)

        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_file:
            self._start_soulseek_download(client, dialog.selected_file, auto_fetch_lyrics=True)

    def _open_batch_import(self, link: str) -> None:
        self.workspace.setCurrentIndex(WORKSPACE_ACQUIRE)
        self.download_view.start_fetch(link)

    def _start_soulseek_download(
        self, client: SlskdClient, file: SoulseekFile, auto_fetch_lyrics: bool,
    ) -> None:
        downloads_dir = str(self.settings.value("slskd_downloads_dir", "")).strip()
        library_dir = str(self.settings.value("library_dir", "")).strip() or os.getcwd()

        self.status_bar.setText(f"Downloading {file.display_name} from Soulseek…")
        self._soulseek_download_thread = SoulseekDownloadThread(
            client, downloads_dir, library_dir, file,
        )
        self._soulseek_download_thread.progress.connect(self.status_bar.setText)
        self._soulseek_download_thread.finished_ok.connect(
            lambda path: self._on_soulseek_download_ok(path, auto_fetch_lyrics)
        )
        self._soulseek_download_thread.finished_fail.connect(self._on_soulseek_download_fail)
        self._soulseek_download_thread.start()

    def _on_soulseek_download_ok(self, local_path: str, auto_fetch_lyrics: bool) -> None:
        self.status_bar.setText(f"Downloaded: {os.path.basename(local_path)}")
        self._process_added_files([local_path])
        if auto_fetch_lyrics:
            self._fetch_lyrics_for([local_path])

    def _on_soulseek_download_fail(self, message: str) -> None:
        self.status_bar.setText("Soulseek download failed.")
        QMessageBox.warning(self, "Download Failed", message)

    # ==================================================================
    # Library dedup (shared with the Download tab)
    # ==================================================================

    def _collect_library_paths(self) -> list[str]:
        """Every audio file we should treat as "already have" for dedup.

        Scans the configured library folder (the real source of truth for
        "songs I already own") plus anything already in the current
        session's file list, in case some of those live elsewhere on disk.
        """
        paths: set[str] = set()
        library_dir = str(self.settings.value("library_dir", "")).strip()
        if library_dir and os.path.isdir(library_dir):
            for root, _dirs, files in os.walk(library_dir):
                for fname in files:
                    if fname.lower().endswith(AUDIO_EXTENSIONS):
                        paths.add(os.path.join(root, fname))
        paths.update(self.library_panel.real_file_paths())
        return list(paths)

    # ==================================================================
    # Optional sync validation (faster-whisper spot-check)
    # ==================================================================

    def _validate_sync(self) -> None:
        if not lyrics_qa.is_available():
            QMessageBox.information(
                self,
                "Optional Feature Not Installed",
                "Validate Sync uses faster-whisper to locally spot-check a "
                "few lines against the audio — it's optional and not "
                "installed by default.\n\n"
                "Install it with:\n    pip install faster-whisper\n\n"
                "(First run downloads a small speech model, ~150MB; after "
                "that it works fully offline and takes a few seconds per song.)",
            )
            return
        if not self.current_file_path or not self._verses:
            return

        lines = [
            (i, v.get_verse_text(), self._verse_timestamps[i])
            for i, v in enumerate(self._verses)
            if v.get_timestamp() != DEFAULT_TIMESTAMP and v.get_verse_text().strip()
        ]
        if not lines:
            QMessageBox.information(self, "Validate Sync", "No synced lines to check yet.")
            return

        dialog = ValidateSyncDialog(self)
        self._qa_thread = LyricsQAThread(self.current_file_path, lines)
        self._qa_thread.progress.connect(dialog.set_status)
        self._qa_thread.finished_qa.connect(dialog.show_results)
        self._qa_thread.failed.connect(lambda msg: dialog.set_status(f"Failed: {msg}"))
        self._qa_thread.start()
        dialog.exec()

    # ==================================================================
    # Unsaved-changes tracking & window state
    # ==================================================================

    def _mark_dirty(self) -> None:
        if not self._dirty:
            self._dirty = True
            self._update_title()

    def _update_title(self) -> None:
        self.setWindowTitle("LRC Studio — Unsaved Changes" if self._dirty else "LRC Studio")

    def _confirm_discard_if_dirty(self) -> bool:
        """Return True if it's safe to move on from the current song.

        Prompts Save / Discard / Cancel when there are unsaved changes.
        Not called on auto-advance to the next track at end-of-song —
        that path should never interrupt playback with a modal dialog.
        """
        if not self._dirty:
            return True
        resp = QMessageBox.question(
            self,
            "Unsaved Changes",
            f"'{self.song_filename}' has unsaved lyric changes. Save before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if resp == QMessageBox.StandardButton.Save:
            self._save_lrc_file()
            return not self._dirty  # only proceed if the save actually succeeded
        return resp == QMessageBox.StandardButton.Discard

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter_state = self.settings.value("splitter_state")
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if not self._confirm_discard_if_dirty():
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter_state", self.splitter.saveState())

        # Stop any in-flight background work before the window (and the
        # QThread objects with it) goes away — otherwise Qt either warns
        # loudly ("QThread: Destroyed while thread is still running") or
        # crashes outright.
        stop_thread(self._search_thread)
        stop_thread(self._soulseek_search_thread)
        stop_thread(self._soulseek_download_thread)
        stop_thread(self._qa_thread)
        stop_thread(self._downloader)
        self.download_view.shutdown()

        if self._slskd_process is not None and self._slskd_process.poll() is None:
            self._slskd_process.terminate()
        event.accept()

    # ==================================================================
    # Song loading
    # ==================================================================

    def _on_library_song_selected(self, path: str) -> None:
        if not self._confirm_discard_if_dirty():
            return
        self._load_song(path)

    def _on_file_renamed(self, old_path: str, new_path: str) -> None:
        if self.current_file_path == old_path:
            self.current_file_path = new_path
            self.song_filename = os.path.basename(new_path)
            self.file_label.setText(self.song_filename)
        self.status_bar.setText(f"Renamed to {os.path.basename(new_path)}.")

    def _load_file_from_index(self, index: int) -> None:
        if 0 <= index < self.library_panel.row_count():
            self.library_panel.select_row(index)
            self._load_song(self.library_panel.real_file_paths()[index])

    def _load_song(self, path: str) -> None:
        if not os.path.exists(path):
            return

        self.current_file_path = path
        self.song_filename     = os.path.basename(path)
        self.file_label.setText(self.song_filename)
        self._ended_handled = False

        self.player.set_media(self._vlc.media_new(path))
        self.song_is_loaded = True
        self.play_pause_btn.setText("▶")
        self.workspace.setCurrentIndex(WORKSPACE_EDITOR)

        lrc_path = os.path.splitext(path)[0] + ".lrc"
        color    = analyze_lrc_status(lrc_path)
        self.file_label.setStyleSheet(
            f"font-weight: bold; font-size: 14px; color: {color.name()};"
        )

        self._replace_all_verses([])
        if os.path.exists(lrc_path):
            try:
                with open(lrc_path, encoding="utf-8") as fh:
                    self._parse_and_add_lyrics(fh.read())
            except OSError as exc:
                self.status_bar.setText(f"Could not read LRC: {exc}")
        else:
            self._add_verse(DEFAULT_TIMESTAMP, "")
            self._update_timeline()

        # Freshly loaded content matches disk — not dirty until the user edits it.
        self._dirty = False
        self._update_title()
        self.status_bar.setText(f"Loaded: {self.song_filename}")

    # ==================================================================
    # Drag-and-drop
    # ==================================================================

    def dragEnterEvent(self, event: object) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():  # type: ignore[attr-defined]
            event.accept()             # type: ignore[attr-defined]
        else:
            event.ignore()             # type: ignore[attr-defined]

    def dropEvent(self, event: object) -> None:  # type: ignore[override]
        urls = event.mimeData().urls()           # type: ignore[attr-defined]
        self._process_added_files([u.toLocalFile() for u in urls])
