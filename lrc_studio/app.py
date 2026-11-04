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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

import vlc
from PyQt6.QtCore import QEventLoop, QFile, Qt, QSettings, QTimer
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSplitter, QStackedWidget, QTabBar,
    QVBoxLayout, QWidget,
)

from constants import (
    AUDIO_EXTENSIONS,
    DEFAULT_TIMESTAMP,
    MAX_UNDO,
    POLL_INTERVAL_MS,
    SYNC_OFFSET_MS,
)
from utils import (
    format_ms,
    ms_to_lrc_timestamp,
    parse_lrc_file,
    parse_lrc_timestamp,
    write_embedded_lyrics,
)
import lyrics_qa
import theme
from dialogs import LyricsPickerDialog, SettingsDialog, ValidateSyncDialog
from download_view import DownloadView
from library_panel import LibraryPanel, scan_audio_files
from postprocess import _as_bool, downloads_dir_from_settings
from search_view import SearchView
from slsk_client import SlskClient, read_slskd_credentials
from soulseek import SlskdError
from spotify_playlist import is_spotify_link
from transfers_view import TransfersView
from widgets import TimelineWidget, Verse
from workers import (
    LibraryConvertThread,
    LyricsDownloaderThread,
    LyricsQAThread,
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
    menu: str | None                  # "file" | "lyrics" | "help" | None (shortcut only)
    help_text: str | None = None      # Help-dialog phrasing; defaults to *label*
    separator_before: bool = False    # start a new group within its menu


# An old slskd setup's config: its Soulseek account is carried over once.
SLSKD_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "slskd-bin", "slskd.yml"
)

# Pages of the main window's workspace stack (see LrcSyncApp._init_ui).
WORKSPACE_EDITOR = 0
WORKSPACE_ACQUIRE = 1      # Spotify playlist review
WORKSPACE_EMPTY = 2
WORKSPACE_SEARCH = 3
WORKSPACE_TRANSFERS = 4

# Tabs above the workspace, in order. A pasted Spotify link opens its
# review page under the Search tab.
TAB_EDITOR, TAB_SEARCH, TAB_DOWNLOADS = range(3)
_PAGE_TO_TAB = {
    WORKSPACE_EDITOR: TAB_EDITOR, WORKSPACE_EMPTY: TAB_EDITOR,
    WORKSPACE_SEARCH: TAB_SEARCH, WORKSPACE_TRANSFERS: TAB_DOWNLOADS,
    WORKSPACE_ACQUIRE: TAB_SEARCH,
}


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
        # Files waiting for a lyrics pass while one is already running —
        # a second batch queues here instead of replacing (and orphaning)
        # the running thread.
        self._lyrics_pending: list[str] = []
        self._qa_thread: LyricsQAThread | None = None
        self._convert_thread: LibraryConvertThread | None = None
        # The Soulseek connection (built on first use from Settings) and
        # the background thread logging it in, if one is running.
        self._slsk: SlskClient | None = None
        self._slsk_thread: threading.Thread | None = None
        self._slsk_error = ""

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
                nxt = self.library_panel.next_path(self.current_file_path)
                # Never auto-advance away from unsaved edits.
                if nxt and not self._dirty:
                    self._load_song(nxt, show_editor=False)
                    self.library_panel.select_path(nxt)
                    QTimer.singleShot(300, self._play)
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
        self.resize(1280, 860)
        self.setAcceptDrops(True)

        self._commands = self._build_commands()
        self._build_menu_bar()

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 10, 14, 6)
        body_layout.setSpacing(6)

        # ── Splitter: library panel | tabs + workspace ────────────────
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.library_panel = LibraryPanel(self.settings)
        self.library_panel.setMinimumWidth(240)
        self.library_panel.song_selected.connect(self._on_library_song_selected)
        self.library_panel.file_renamed.connect(self._on_file_renamed)
        self.library_panel.lyrics_requested.connect(self._fetch_lyrics_for)
        self.library_panel.lyrics_picker_requested.connect(self._open_lyrics_picker_for)
        self.library_panel.convert_requested.connect(self._convert_files)
        self.library_panel.trash_requested.connect(self._trash_files)
        self.splitter.addWidget(self.library_panel)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(8)

        self.tabs = QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        for label in ("Editor", "Search", "Downloads"):
            self.tabs.addTab(label)
        self.tabs.setTabToolTip(TAB_SEARCH, "Search Soulseek, or paste a Spotify link (Ctrl+F)")
        self.tabs.setTabToolTip(TAB_DOWNLOADS, "Downloads in progress and finished (Ctrl+J)")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        tabs_row = QHBoxLayout()
        tabs_row.addWidget(self.tabs)
        tabs_row.addStretch(1)
        self.slsk_status_label = QLabel("")
        self.slsk_status_label.setProperty("role", "faint")
        tabs_row.addWidget(self.slsk_status_label)
        right_layout.addLayout(tabs_row)
        self._slsk_status_timer = QTimer(self)
        self._slsk_status_timer.setInterval(2000)
        self._slsk_status_timer.timeout.connect(self._refresh_slsk_status)
        self._slsk_status_timer.start()

        self.workspace = QStackedWidget()
        self.workspace.addWidget(self._build_lyrics_panel())      # WORKSPACE_EDITOR = 0

        # The one download engine; the Playlist page feeds it too.
        self.transfers_view = TransfersView(self.settings, lambda: self._get_slsk_client(), self)
        self.transfers_view.file_ready.connect(self._on_transfer_ready)
        self.transfers_view.active_count_changed.connect(self._on_active_downloads_changed)
        self.transfers_view.open_requested.connect(self._open_in_library)

        self.download_view = DownloadView(
            self.settings, lambda: self._get_slsk_client(), self._collect_library_paths, self,
            transfers=self.transfers_view,
        )
        self.download_view.files_ready.connect(self._on_download_files_ready)
        self.download_view.file_downloaded.connect(self.library_panel.add_files_single)
        self.download_view.open_in_library.connect(self._open_in_library)
        self.download_view.back_requested.connect(lambda: self._show_page(WORKSPACE_SEARCH))
        self.workspace.addWidget(self.download_view)               # WORKSPACE_ACQUIRE = 1

        self.workspace.addWidget(self._build_empty_state())         # WORKSPACE_EMPTY = 2

        # Lambdas rather than bound methods so a test (or a later
        # override) replacing these on the instance is honoured.
        self.search_view = SearchView(
            self.settings, lambda: self._get_slsk_client(), is_spotify_link, self,
            library_paths=lambda: self.library_panel.real_file_paths(),
        )
        self.transfers_view.item_changed.connect(self.search_view.on_transfer_status)
        self.transfers_view.item_finished.connect(self.search_view.on_transfer_finished)
        self.search_view.spotify_link_entered.connect(lambda link: self._open_batch_import(link))
        self.search_view.download_requested.connect(self._queue_downloads)
        self.workspace.addWidget(self.search_view)                  # WORKSPACE_SEARCH = 3

        self.workspace.addWidget(self.transfers_view)               # WORKSPACE_TRANSFERS = 4

        self.workspace.currentChanged.connect(self._sync_tab_to_page)
        right_layout.addWidget(self.workspace, stretch=1)

        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([320, 960])
        body_layout.addWidget(self.splitter, stretch=1)

        # ── Status line ───────────────────────────────────────────────
        self.status_bar = QLabel("Ready")
        self.status_bar.setProperty("role", "faint")
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedSize(180, 4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        stat_row = QHBoxLayout()
        stat_row.addWidget(self.status_bar, stretch=1)
        stat_row.addWidget(self.progress_bar)
        body_layout.addLayout(stat_row)

        root_layout.addWidget(body, stretch=1)
        root_layout.addWidget(self._build_player_bar())

        self.workspace.setCurrentIndex(WORKSPACE_EMPTY)
        QTimer.singleShot(0, self._load_library)
        # Log in to Soulseek in the background if an account is set, so the
        # first search doesn't wait for it (like Nicotine+ does at start).
        QTimer.singleShot(500, self._autoconnect_soulseek)

    def _build_empty_state(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(2)
        title = QLabel("Open a song to edit its lyrics")
        title.setProperty("role", "title")
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignHCenter)
        label = QLabel(
            "Double-click a song in your library. New songs are on top, tagged \u201cnew\u201d.\n"
            "Right-click songs for everything else. Ctrl+F finds music on Soulseek."
        )
        label.setProperty("role", "muted")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(4)
        layout.addWidget(label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(3)
        return page

    def _on_download_files_ready(self, paths: list[str]) -> None:
        # Each file already joined the library and got its lyrics queued
        # as it landed (see _on_transfer_ready); this is the end-of-batch
        # step: open the first new song.
        self.library_panel.add_files(paths)
        # Only auto-jump to the new song when it's safe: switching away
        # from unsaved sync edits on whatever's currently loaded would
        # silently discard them, with no confirmation, since this runs
        # from a background download completing — never do that.
        if paths and not self._dirty:
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
            _Command("Settings…", "Ctrl+,", self._open_settings_dialog, "file"),
            _Command("Rescan Library", "F5", self._load_library, "file"),
            _Command("Quit", "Ctrl+Q", self.close, "file", separator_before=True),

            _Command("Save Lyrics", "Ctrl+S", self._save_lrc_file, "lyrics"),
            _Command("Undo", "Ctrl+Z", self._undo_last_sync, "lyrics"),
            _Command("Find Lyrics…", "Ctrl+L", self._find_lyrics_command, "lyrics", separator_before=True,
                     help_text="Find lyrics (the open song, or the songs selected in the library)"),
            _Command("Shift Timing…", "Ctrl+Shift+O", self._shift_all_timestamps, "lyrics"),
            _Command("Sort Lines by Time", "Ctrl+T", self._sort_verses_by_time, "lyrics"),
            _Command("Check Sync…", None, self._validate_sync, "lyrics"),
            _Command("Clear Lyrics…", None, self._clear_lyrics, "lyrics", separator_before=True),

            _Command("Search Soulseek", "Ctrl+F", self._find_music, None),
            _Command("Downloads", "Ctrl+J", lambda: self._show_page(WORKSPACE_TRANSFERS), None),

            _Command("Keyboard Shortcuts…", None, self._show_shortcuts_dialog, "help"),
            _Command("About LRC Studio", None, self._show_about_dialog, "help"),
        ]

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()
        menus = {
            "file":   menu_bar.addMenu("&File"),
            "lyrics": menu_bar.addMenu("&Lyrics"),
        }
        # Library: the right-click menu for whatever is selected, rebuilt
        # each time it opens so it always matches the selection.
        self.library_menu = menu_bar.addMenu("Li&brary")
        self.library_menu.aboutToShow.connect(self._fill_library_menu)
        menus["help"] = menu_bar.addMenu("&Help")
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

    def _build_player_bar(self) -> QFrame:
        """The transport, docked along the bottom of the window so it stays
        reachable from every page (preview a download while searching)."""
        bar = QFrame()
        bar.setObjectName("PlayerBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(12)

        self.play_pause_btn = QPushButton("▶")
        self.play_pause_btn.setFixedSize(38, 38)
        self.play_pause_btn.setProperty("variant", "round")
        self.play_pause_btn.setToolTip("Play / pause (Space)")
        self.play_pause_btn.clicked.connect(self._toggle_play_pause)

        self.lbl_curr = QLabel("0:00")
        self.lbl_tot  = QLabel("0:00")
        for lbl in (self.lbl_curr, self.lbl_tot):
            lbl.setFont(theme.mono_font(9.5))
            lbl.setProperty("role", "muted")
            lbl.setMinimumWidth(44)
        self.lbl_curr.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Seek bar — preserves play state across drags
        self.seek_bar = QSlider(Qt.Orientation.Horizontal)
        self.seek_bar.setObjectName("SeekBar")
        self.seek_bar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.seek_bar.sliderMoved.connect(self.set_position)
        self.seek_bar.sliderPressed.connect(self._on_seek_pressed)
        self.seek_bar.sliderReleased.connect(self._on_seek_released)

        self.file_label = QLabel("Nothing playing")
        self.file_label.setObjectName("NowPlaying")
        self.file_label.setMinimumWidth(180)
        self.file_label.setMaximumWidth(360)

        self.vol_icon   = QLabel("🔊")
        self.vol_icon.setProperty("role", "muted")
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(self._volume)
        self.vol_slider.setFixedWidth(96)
        self.vol_slider.setToolTip("Volume")
        self.vol_slider.valueChanged.connect(self._set_volume)

        # Audio output selector — Linux/PipeWire only; omitted elsewhere.
        sinks = self._get_sink_descriptions()
        self.device_combo: QComboBox | None = None
        if sinks:
            self.device_combo = QComboBox()
            self.device_combo.setFixedWidth(200)
            self.device_combo.setToolTip("Audio output device")
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
        row.addWidget(self.file_label)
        row.addWidget(self.lbl_curr)
        row.addWidget(self.seek_bar, stretch=1)
        row.addWidget(self.lbl_tot)
        row.addSpacing(8)
        row.addWidget(self.vol_icon)
        row.addWidget(self.vol_slider)
        if self.device_combo is not None:
            row.addWidget(self.device_combo)
        return bar

    # ------------------------------------------------------------------
    # Workspace navigation
    # ------------------------------------------------------------------

    def _show_page(self, page: int) -> None:
        self.workspace.setCurrentIndex(page)

    def _sync_tab_to_page(self, page: int) -> None:
        tab = _PAGE_TO_TAB.get(page, TAB_EDITOR)
        if self.tabs.currentIndex() != tab:
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(tab)
            self.tabs.blockSignals(False)

    def _on_tab_changed(self, tab: int) -> None:
        if tab == TAB_EDITOR:
            self.workspace.setCurrentIndex(WORKSPACE_EDITOR if self.song_is_loaded else WORKSPACE_EMPTY)
        elif tab == TAB_SEARCH:
            self.workspace.setCurrentIndex(WORKSPACE_SEARCH)
            self.search_view.focus_query()
        elif tab == TAB_DOWNLOADS:
            self.workspace.setCurrentIndex(WORKSPACE_TRANSFERS)

    def _on_active_downloads_changed(self, n: int) -> None:
        self.tabs.setTabText(TAB_DOWNLOADS, f"Downloads ({n})" if n else "Downloads")

    def _build_lyrics_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)

        # ── Karaoke: previous, current, next line ─────────────────────
        karaoke = QWidget()
        karaoke.setObjectName("KaraokePanel")
        karaoke.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        k_layout = QVBoxLayout(karaoke)
        k_layout.setContentsMargins(20, 12, 20, 12)
        k_layout.setSpacing(2)
        self.karaoke_prev = QLabel("")
        self.karaoke_label = QLabel("Press play")
        self.karaoke_next = QLabel("")
        for lbl, name in ((self.karaoke_prev, "KaraokeContext"),
                          (self.karaoke_label, "KaraokeCurrent"),
                          (self.karaoke_next, "KaraokeContext")):
            lbl.setObjectName(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setWordWrap(False)
            lbl.setMinimumWidth(1)
            k_layout.addWidget(lbl)
        layout.addWidget(karaoke)

        # ── Timeline ──────────────────────────────────────────────────
        self.timeline = TimelineWidget()
        self.timeline.setMouseTracking(True)
        self.timeline.time_changed_bulk.connect(self._update_verses_from_timeline)
        self.timeline.seek_requested.connect(self.set_position)
        layout.addWidget(self.timeline)

        # Shown instead of a bare empty line when the song has no lyrics yet.
        self.no_lyrics_card = QFrame()
        self.no_lyrics_card.setObjectName("NoLyricsCard")
        card = QHBoxLayout(self.no_lyrics_card)
        card.setContentsMargins(16, 12, 16, 12)
        card_text = QLabel("Find them online, or type them below line by line (Enter moves on).")
        card_text.setWordWrap(True)
        card_text.setProperty("role", "muted")
        card.addWidget(card_text, stretch=1)
        self.no_lyrics_button = QPushButton("Find lyrics")
        self.no_lyrics_button.setProperty("variant", "primary")
        self.no_lyrics_button.clicked.connect(self._get_lyrics_online)
        card.addWidget(self.no_lyrics_button)
        self.no_lyrics_card.setVisible(False)
        layout.addWidget(self.no_lyrics_card)

        # Scrollable verse list
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self._scroll_content = QWidget()
        self.lyrics_layout   = QVBoxLayout(self._scroll_content)
        self.lyrics_layout.setContentsMargins(0, 0, 6, 0)
        self.lyrics_layout.setSpacing(1)
        self.lyrics_layout.addStretch()
        self.scroll_area.setWidget(self._scroll_content)
        layout.addWidget(self.scroll_area, stretch=1)

        # ── Actions: Save is the one primary; Clear all sits apart ────
        actions = QHBoxLayout()
        actions.setSpacing(8)
        save_btn = QPushButton("Save")
        save_btn.setToolTip("Save the lyrics next to the song (Ctrl+S)")
        save_btn.setProperty("variant", "primary")
        save_btn.clicked.connect(self._save_lrc_file)
        actions.addWidget(save_btn)

        for label, handler, tooltip in [
            ("Find lyrics…", self._get_lyrics_online,
             "Look this song up online and pick the best version (Ctrl+L)"),
            ("Shift timing…", self._shift_all_timestamps,
             "Move every line earlier or later by the same amount (Ctrl+Shift+O)"),
        ]:
            btn = QPushButton(label)
            btn.setToolTip(tooltip)
            btn.clicked.connect(handler)
            actions.addWidget(btn)
        actions.addStretch(1)

        clear_btn = QPushButton("Clear all")
        clear_btn.setToolTip("Remove every line and start this song's lyrics over")
        clear_btn.setProperty("variant", "danger")
        clear_btn.clicked.connect(self._clear_lyrics)
        actions.addWidget(clear_btn)
        layout.addLayout(actions)

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
        """Update the karaoke lines and playback highlight for *position_ms*.

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
            self.karaoke_prev.setText("")
            self.karaoke_label.setText("♪")
            first = next((v.get_verse_text() for v in self._verses if v.get_verse_text().strip()), "")
            self.karaoke_next.setText(first)
            self._set_playback_highlight(None)
            return

        def text_at(i: int) -> str:
            return self._verses[i].get_verse_text() if 0 <= i < len(self._verses) else ""

        self.karaoke_prev.setText(text_at(best_idx - 1))
        self.karaoke_label.setText(text_at(best_idx) or "♪")
        self.karaoke_next.setText(text_at(best_idx + 1))
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
        self._update_no_lyrics_card()

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
        if _as_bool(self.settings.value("embed_lyrics", False), False):
            try:
                write_embedded_lyrics(self.current_file_path, lrc_text)
                status += " (+ embedded in file tags)"
            except Exception as exc:
                status += f" — embedding failed: {exc}"

        self._dirty = False
        self._update_title()
        self.status_bar.setText(status)
        self.library_panel.refresh_status(self.current_file_path)

    def _get_lyrics_online(self) -> None:
        """Look the loaded song up online and let the user pick a version."""
        if not self.song_filename:
            return
        import lyrics_fetch
        info = lyrics_fetch.read_track_info(self.current_file_path)
        duration = info.duration_s or (self.duration_ms / 1000 if self.duration_ms > 1 else None)
        dialog = LyricsPickerDialog(info.artist, lyrics_fetch.clean_title(info.title), duration, self)
        QTimer.singleShot(0, dialog.run_search)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.chosen_text:
            self._on_search_result(dialog.chosen_text)

    def _on_search_result(self, result: str) -> None:
        if result:
            self._replace_all_verses([])
            self._parse_and_add_lyrics(result)
            self._mark_dirty()
            self._update_no_lyrics_card()
            self.status_bar.setText("Lyrics found. Check them and press Ctrl+S to save.")
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
        self._fetch_lyrics_for(files)

    def _on_download_progress(
        self, message: str, done: int, total: int
    ) -> None:
        self.status_bar.setText(f"[{done}/{total}] {message}")
        self.progress_bar.setValue(done)

    def _on_batch_finished(
        self, success: list[str], failed: list[str]
    ) -> None:
        self.progress_bar.setVisible(False)
        parts = [f"Lyrics found for {len(success)} songs."]
        if failed:
            parts.append(f"Nothing found for {len(failed)}.")
        self.status_bar.setText("  ".join(parts))
        self._downloader = None
        if self._lyrics_pending:
            pending, self._lyrics_pending = self._lyrics_pending, []
            self._fetch_lyrics_for(pending)

    def _on_lyrics_file_done(self, path: str, kind: str) -> None:
        self.library_panel.refresh_status(path)
        # Show freshly fetched lyrics if that song is open and untouched.
        if kind != "none" and path == self.current_file_path and not self._dirty:
            self._load_song(path, keep_playing=True, show_editor=False)

    def _fetch_lyrics_for(self, file_paths: list[str]) -> None:
        """Queue a lyrics pass for specific files. If a pass is already
        running, the files wait for it instead of starting a second
        thread (replacing the reference to a running QThread would let
        Python destroy it mid-run)."""
        if not file_paths:
            return
        running = self._downloader is not None and self._downloader.isRunning()
        if running:
            known = set(self._lyrics_pending)
            self._lyrics_pending.extend(p for p in file_paths if p not in known)
            return
        self.progress_bar.setRange(0, len(file_paths))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        embed = _as_bool(self.settings.value("embed_lyrics", False), False)
        self._downloader = LyricsDownloaderThread(file_paths, embed)
        self._downloader.progress.connect(self._on_download_progress)
        if hasattr(self._downloader, "file_done"):
            self._downloader.file_done.connect(self._on_lyrics_file_done)
        self._downloader.finished_batch.connect(self._on_batch_finished)
        self._downloader.start()

    # ==================================================================
    # Library conversion (lossless -> Opus, in place)
    # ==================================================================

    def _convert_files(self, files: list[str]) -> None:
        """Convert the given lossless library files to Opus, in place."""
        import convert

        if self._convert_thread is not None and self._convert_thread.isRunning():
            QMessageBox.information(self, "Conversion Running", "A conversion is already running.")
            return
        if not convert.ffmpeg_available():
            QMessageBox.warning(self, "ffmpeg Not Found",
                                "Converting needs ffmpeg on your PATH (e.g. dnf install ffmpeg).")
            return
        files = [f for f in files if os.path.exists(f)]
        if not files:
            return
        size_gb = sum(os.path.getsize(f) for f in files) / 1024 ** 3
        try:
            bitrate = int(self.settings.value("opus_bitrate", 192) or 192)
        except (TypeError, ValueError):
            bitrate = 192
        resp = QMessageBox.warning(
            self, "Convert to Opus",
            f"Convert {len(files)} lossless file{'s' if len(files) != 1 else ''} "
            f"({size_gb:.1f} GB) to Opus {bitrate} kbps?\n\n"
            "Each original is deleted once its Opus copy is written and checked. Tags, "
            "cover art and lyrics are kept. This can't be undone, and any device that "
            "syncs this folder will see the originals disappear.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        if self.current_file_path in files:
            self.player.stop()
            self.play_pause_btn.setText("▶")
        self.progress_bar.setRange(0, len(files))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self._convert_thread = LibraryConvertThread(files, bitrate)
        self._convert_thread.progress.connect(self._on_download_progress)
        self._convert_thread.converted.connect(self._on_file_converted)
        self._convert_thread.finished_all.connect(self._on_convert_finished)
        self._convert_thread.start()

    # ==================================================================
    # Library actions that need the window (trash, lyrics, Library menu)
    # ==================================================================

    def _trash_files(self, paths: list[str]) -> None:
        """Move songs (and their lyrics) to the trash, after asking once."""
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        names = "\n".join(os.path.basename(p) for p in paths[:12])
        if len(paths) > 12:
            names += f"\n… and {len(paths) - 12} more"
        title = "Move to Trash" if len(paths) == 1 else f"Move {len(paths)} Songs to Trash"
        if QMessageBox.question(
            self, title,
            f"Move to the trash, with their lyrics?\n\n{names}\n\n"
            "Devices that sync this folder will lose them too. You can restore them from the trash.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        if self.current_file_path in paths:
            self._unload_song()
        trashed, failed = [], []
        for path in paths:
            ok, _where = QFile.moveToTrash(path)
            if not ok:
                failed.append(os.path.basename(path))
                continue
            trashed.append(path)
            lrc = os.path.splitext(path)[0] + ".lrc"
            if os.path.exists(lrc):
                QFile.moveToTrash(lrc)
        self.library_panel.remove_paths(trashed)
        msg = f"Moved {len(trashed)} song{'s' if len(trashed) != 1 else ''} to the trash."
        if failed:
            msg += f" Couldn't move {len(failed)}: {', '.join(failed[:5])}"
        self.status_bar.setText(msg)

    def _unload_song(self) -> None:
        """Forget the loaded song (it's being trashed): stop, clear, empty page."""
        self.player.stop()
        self.player.set_media(None)
        self.play_pause_btn.setText("▶")
        self.song_is_loaded = False
        self.current_file_path = ""
        self.song_filename = ""
        self.file_label.setText("Nothing playing")
        self._replace_all_verses([])
        self._dirty = False
        self._update_title()
        self._update_no_lyrics_card()
        if self.workspace.currentIndex() == WORKSPACE_EDITOR:
            self.workspace.setCurrentIndex(WORKSPACE_EMPTY)

    def _find_lyrics_command(self) -> None:
        """Ctrl+L: the library's selection when the list has focus (one song:
        the picker; several: fetch automatically), else the open song."""
        if self.library_panel.file_list.hasFocus():
            paths = self.library_panel.selected_paths()
            if len(paths) == 1:
                self._open_lyrics_picker_for(paths[0])
                return
            if paths:
                self._fetch_lyrics_for([p for p in paths if self.library_panel.status_of(p) != "synced"])
                return
        self._get_lyrics_online()

    def _open_lyrics_picker_for(self, path: str) -> None:
        if path != self.current_file_path:
            if not self._confirm_discard_if_dirty():
                return
            self._load_song(path)
        self._get_lyrics_online()

    def _fill_library_menu(self) -> None:
        menu = self.library_menu
        menu.clear()
        paths = self.library_panel.selected_paths()
        if paths:
            self.library_panel.fill_menu(menu, paths)
        else:
            hint = menu.addAction("Select songs in the library first")
            hint.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Select All Shown Songs", self._select_all_library)

    def _select_all_library(self) -> None:
        self.library_panel.select_all_visible()
        self.library_panel.file_list.setFocus()

    def _update_no_lyrics_card(self) -> None:
        empty = self.song_is_loaded and not any(v.get_verse_text().strip() for v in self._verses)
        self.no_lyrics_card.setVisible(empty)

    def _on_file_converted(self, old_path: str, new_path: str) -> None:
        was_current = old_path == self.current_file_path
        self.library_panel.replace_path(old_path, new_path)
        if was_current:
            self.player.set_media(self._vlc.media_new(new_path))

    def _on_convert_finished(self, done: int, failed: list) -> None:
        self.progress_bar.setVisible(False)
        msg = f"Converted {done} files to Opus."
        if failed:
            msg += f" {len(failed)} failed and were left as they were."
            QMessageBox.warning(self, "Some Files Weren't Converted",
                                msg + "\n\n" + "\n".join(failed[:15]) + ("\n…" if len(failed) > 15 else ""))
        self.status_bar.setText(msg)

    # ==================================================================
    # Soulseek (in-process client — see lrc_studio/slsk_client.py)
    # ==================================================================

    def _soulseek_credentials(self) -> tuple[str, str]:
        """The account from Settings, carried over from slskd.yml once."""
        user = str(self.settings.value("soulseek_username", "") or "").strip()
        password = str(self.settings.value("soulseek_password", "") or "")
        if not (user and password):
            old = read_slskd_credentials(SLSKD_CONFIG_PATH)
            if old:
                user, password = old
                self.settings.setValue("soulseek_username", user)
                self.settings.setValue("soulseek_password", password)
        return user, password

    def _build_slsk_client(self) -> SlskClient | None:
        user, password = self._soulseek_credentials()
        if not (user and password):
            return None
        library_dir = self.library_panel.library_dir()
        share = _as_bool(self.settings.value("share_library", True), True)
        shared = (library_dir,) if share and library_dir and os.path.isdir(library_dir) else ()
        return SlskClient(user, password, downloads_dir_from_settings(self.settings), shared_dirs=shared)

    def _start_slsk_connect(self) -> None:
        """Log in on a background thread (never blocks the window)."""
        if self._slsk is None:
            self._slsk = self._build_slsk_client()
            if self._slsk is None:
                return
        if self._slsk_thread is not None and self._slsk_thread.is_alive():
            return
        client = self._slsk

        def work() -> None:
            try:
                client.ensure_connected()
                self._slsk_error = ""
            except SlskdError as exc:
                self._slsk_error = str(exc)
            except Exception as exc:  # noqa: BLE001 — shown to the user, never fatal
                self._slsk_error = f"Soulseek failed to start: {exc}"

        self._slsk_thread = threading.Thread(target=work, name="slsk-connect", daemon=True)
        self._slsk_thread.start()

    def _get_slsk_client(self) -> SlskClient | None:
        """The logged-in Soulseek client, connecting first if needed (the
        window stays responsive meanwhile). None, with an explanation,
        when there's no account or the login failed."""
        if self._slsk is not None and self._slsk.check_connection()[0]:
            return self._slsk
        if self._slsk is None and self._build_slsk_client() is None:
            QMessageBox.information(
                self, "Soulseek Account Needed",
                "Enter your Soulseek username and password in Settings. "
                "Any new name works: Soulseek registers it the first time you log in.",
            )
            self._open_settings_dialog()
            return None
        self._start_slsk_connect()
        self.status_bar.setText("Connecting to Soulseek…")
        thread = self._slsk_thread
        while thread is not None and thread.is_alive():
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            thread.join(0.05)
        self._refresh_slsk_status()
        if self._slsk is not None and self._slsk.check_connection()[0]:
            self.status_bar.setText("Connected to Soulseek.")
            return self._slsk
        message = self._slsk_error or (self._slsk.check_connection()[1] if self._slsk else "Not connected.")
        QMessageBox.warning(
            self, "Soulseek Unavailable",
            f"{message}\n\nCheck the account in File → Settings, or try again in a moment.",
        )
        return None

    def _refresh_slsk_status(self) -> None:
        if self._slsk is None:
            self.slsk_status_label.setText("")
            return
        ok, message = self._slsk.check_connection()
        if ok:
            text = "Soulseek: online"
        elif self._slsk_thread is not None and self._slsk_thread.is_alive():
            text = "Soulseek: connecting…"
        else:
            text = "Soulseek: offline"
        self.slsk_status_label.setText(text)
        self.slsk_status_label.setToolTip(self._slsk_error or message)

    def _reset_slsk_client(self) -> None:
        """Account or sharing changed: drop the old session, log in anew."""
        self._shutdown_soulseek()
        if self._build_slsk_client() is not None:
            self._start_slsk_connect()

    def _autoconnect_soulseek(self) -> None:
        user = str(self.settings.value("soulseek_username", "") or "").strip()
        if user and self.settings.value("soulseek_password"):
            self._start_slsk_connect()
            self._refresh_slsk_status()

    def _shutdown_soulseek(self) -> None:
        client, self._slsk = self._slsk, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — closing anyway
                pass
        self._refresh_slsk_status()

    def _downloads_active(self) -> bool:
        return self.transfers_view.active_count() > 0 or self.download_view.is_batch_active()

    def _open_settings_dialog(self) -> None:
        keys = ("soulseek_username", "soulseek_password", "share_library", "library_dir")
        before = [self.settings.value(k) for k in keys]
        status = self._slsk.check_connection()[1] if self._slsk is not None else ""
        dialog = SettingsDialog(self.settings, self, soulseek_status=self._slsk_error or status)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if accepted and [self.settings.value(k) for k in keys] != before:
            if self._slsk is not None or self._build_slsk_client() is not None:
                self._reset_slsk_client()
            self._load_library()

    def _find_music(self) -> None:
        self._show_page(WORKSPACE_SEARCH)
        self.search_view.focus_query()

    def _open_batch_import(self, link: str) -> None:
        self.workspace.setCurrentIndex(WORKSPACE_ACQUIRE)
        # A running search/download thread delivers results by row index
        # into download_view's table — start_fetch() would wipe that
        # table (and the row list) out from under it, corrupting the
        # in-progress batch. Re-entering Find Music while one is active
        # just shows it instead of restarting.
        if not self.download_view.is_batch_active():
            self.download_view.start_fetch(link)

    def _queue_downloads(self, files: list) -> None:
        # A file picked by hand waits in its peer's queue as long as it
        # takes, unless other peers have the same file: then after four
        # minutes it moves on to the next copy.
        alternatives = getattr(self.search_view, "alternatives_for", None)
        ids = self.transfers_view.enqueue(files, alternatives=alternatives, queue_patience_s=240)
        # enqueue skips files already queued, so ask it which file each id is.
        self.search_view.note_queued([self.transfers_view.file_of(i) for i in ids], ids)
        added = len(ids)
        if added:
            self.status_bar.setText(
                f"Queued {added} download{'s' if added != 1 else ''}. Follow them in Downloads (Ctrl+J)."
            )
        else:
            self.status_bar.setText("Already in the download queue.")

    def _on_transfer_ready(self, path: str) -> None:
        self.library_panel.add_files([path])
        self.status_bar.setText(f"Downloaded {os.path.basename(path)}. Fetching lyrics…")
        self._fetch_lyrics_for([path])

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
        if not self.no_lyrics_card.isHidden():
            self._update_no_lyrics_card()
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
        if self._downloads_active() and QMessageBox.question(
            self, "Downloads Running",
            f"{self.transfers_view.active_count()} downloads are still running. "
            "Quitting stops them. Quit anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter_state", self.splitter.saveState())

        # Stop any in-flight background work before the window (and the
        # QThread objects with it) goes away — otherwise Qt either warns
        # loudly ("QThread: Destroyed while thread is still running") or
        # crashes outright.
        stop_thread(self._qa_thread)
        stop_thread(self._downloader)
        stop_thread(self._convert_thread)
        self.download_view.shutdown()
        self.search_view.shutdown()
        self.transfers_view.shutdown()

        self._slsk_status_timer.stop()
        self._shutdown_soulseek()

        # Release the VLC player/instance explicitly rather than relying
        # on Python GC to eventually free the underlying native
        # (PipeWire/PulseAudio) resources — found via the test suite
        # accumulating dozens of never-released VLC instances across
        # many LrcSyncApp constructions in one process and eventually
        # crashing inside VLC's PipeWire plugin. Never manifests in real
        # usage (the app only ever constructs one window), but closing
        # cleanly is correct regardless.
        self._poll_timer.stop()
        self.player.stop()
        self.player.release()
        self._vlc.release()

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
            self.file_label.setText(os.path.splitext(self.song_filename)[0])

    def _load_file_from_index(self, index: int) -> None:
        paths = self.library_panel.real_file_paths()
        if 0 <= index < len(paths):
            self.library_panel.select_path(paths[index])
            self._load_song(paths[index])

    def _load_library(self) -> None:
        added = self.library_panel.load_library_folder()
        if added:
            self.status_bar.setText(f"Library: {self.library_panel.row_count()} songs.")

    def _load_song(self, path: str, keep_playing: bool = False, show_editor: bool = True) -> None:
        """Load *path* into the player and editor. *keep_playing* reloads
        the lyrics of the song already playing without touching playback;
        *show_editor* False leaves the user on whatever page they're on
        (auto-advance, background lyrics arriving)."""
        if not os.path.exists(path):
            return

        resume_at = self.player.get_time() if keep_playing and path == self.current_file_path else None
        was_playing = bool(self.player.is_playing()) if resume_at is not None else False

        self.current_file_path = path
        self.song_filename     = os.path.basename(path)
        self.file_label.setText(os.path.splitext(self.song_filename)[0])
        self.file_label.setToolTip(path)
        self._ended_handled = False
        self.library_panel.set_current(path)

        if resume_at is None:
            self.player.set_media(self._vlc.media_new(path))
            self.play_pause_btn.setText("▶")
        self.song_is_loaded = True
        if show_editor or self.workspace.currentIndex() == WORKSPACE_EMPTY:
            self.workspace.setCurrentIndex(WORKSPACE_EDITOR)

        lrc_path = os.path.splitext(path)[0] + ".lrc"
        self._replace_all_verses([])
        if os.path.exists(lrc_path):
            try:
                with open(lrc_path, encoding="utf-8") as fh:
                    self._parse_and_add_lyrics(fh.read())
            except (OSError, UnicodeDecodeError) as exc:
                self.status_bar.setText(f"Could not read LRC: {exc}")
        else:
            self._add_verse(DEFAULT_TIMESTAMP, "")
            self._update_timeline()
        self.karaoke_prev.setText("")
        self.karaoke_label.setText("♪" if self._verses and self._verses[0].get_verse_text() else "No lyrics yet")
        self.karaoke_next.setText("")
        if resume_at is not None and was_playing:
            self.play_pause_btn.setText("⏸")

        # Freshly loaded content matches disk — not dirty until the user edits it.
        self._dirty = False
        self._update_title()
        self._update_no_lyrics_card()
        self.status_bar.setText(f"Loaded: {self.song_filename}")

    def _play(self) -> None:
        if self.song_is_loaded:
            self.player.play()
            self.play_pause_btn.setText("⏸")

    # ==================================================================
    # Drag-and-drop
    # ==================================================================

    def dragEnterEvent(self, event: object) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():  # type: ignore[attr-defined]
            event.accept()             # type: ignore[attr-defined]
        else:
            event.ignore()             # type: ignore[attr-defined]

    def dropEvent(self, event: object) -> None:  # type: ignore[override]
        paths: list[str] = []
        for url in event.mimeData().urls():      # type: ignore[attr-defined]
            local = url.toLocalFile()
            paths.extend(scan_audio_files(local) if os.path.isdir(local) else [local])
        self._process_added_files(paths)

