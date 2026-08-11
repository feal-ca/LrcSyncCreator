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
from functools import partial

import mutagen
import vlc
from PyQt6.QtCore import Qt, QSettings, QTimer
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSplitter, QVBoxLayout, QWidget,
)

from constants import (
    AUDIO_EXTENSIONS,
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_FG,
    COLOR_MISSING,
    COLOR_SYNCED,
    COLOR_TIMELINE_BG,
    COLOR_TIMELINE_BLOCK,
    COLOR_UNSYNCED,
    DEFAULT_TIMESTAMP,
    MAX_UNDO,
    POLL_INTERVAL_MS,
    SYNC_OFFSET_MS,
)
from utils import (
    analyze_lrc_status,
    clean_filename,
    format_ms,
    ms_to_lrc_timestamp,
    parse_lrc_file,
    parse_lrc_timestamp,
    write_embedded_lyrics,
)
from widgets import TimelineWidget, Verse
from workers import LyricsDownloaderThread, LyricsSearchThread


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
        self._undo_stack: list[tuple[int, str]] = []

        # Seek-bar drag state (preserves play/pause across slider drags)
        self._was_playing_before_drag: bool = False

        # Guard against double-handling VLC "Ended" state
        self._ended_handled: bool = False

        # Batch downloader thread reference (kept to avoid GC)
        self._downloader: LyricsDownloaderThread | None = None
        # Manual "Web Search" thread reference (kept to avoid GC)
        self._search_thread: LyricsSearchThread | None = None

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
                row = self.file_list.currentRow()
                if row < self.file_list.count() - 1:
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

        self._build_menu_bar()

        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(15, 15, 15, 15)
        main.setSpacing(10)

        main.addLayout(self._build_top_bar())

        # ── Karaoke display ───────────────────────────────────────────
        self.karaoke_label = QLabel("Waiting for song…")
        self.karaoke_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.karaoke_label.setStyleSheet(
            f"font-size: 26px; font-weight: bold; color: {COLOR_UNSYNCED.name()};"
            " padding: 15px; background-color: #11111b;"
            " border-radius: 10px; border: 1px solid #313244;"
        )
        self.karaoke_label.setFixedHeight(80)
        main.addWidget(self.karaoke_label)

        # ── Timeline ──────────────────────────────────────────────────
        self.timeline = TimelineWidget()
        self.timeline.setMouseTracking(True)
        self.timeline.time_changed_bulk.connect(self._update_verses_from_timeline)
        self.timeline.seek_requested.connect(self.set_position)
        main.addWidget(self.timeline)

        # ── Splitter: file list | lyrics editor ───────────────────────
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self._build_file_panel())
        self.splitter.addWidget(self._build_lyrics_panel())
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        main.addWidget(self.splitter, stretch=1)

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
        main.addLayout(stat_row)

    def _build_menu_bar(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        self._add_action(file_menu, "Add Files…", "Ctrl+O", self._add_files_dialog)
        self._add_action(file_menu, "Save LRC", "Ctrl+S", self._save_lrc_file)
        file_menu.addSeparator()
        self._add_action(file_menu, "Exit", "Ctrl+Q", self.close)

        edit_menu = menu.addMenu("&Edit")
        self._add_action(edit_menu, "Undo Sync", "Ctrl+Z", self._undo_last_sync)
        self._add_action(edit_menu, "Sort Lines by Time", "Ctrl+T", self._sort_verses_by_time)
        self._add_action(edit_menu, "Shift All Timestamps…", "Ctrl+Shift+O", self._shift_all_timestamps)
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Clear All Lyrics", None, self._clear_lyrics)

        help_menu = menu.addMenu("&Help")
        self._add_action(help_menu, "Keyboard Shortcuts…", None, self._show_shortcuts_dialog)
        self._add_action(help_menu, "About LRC Studio", None, self._show_about_dialog)

    @staticmethod
    def _add_action(menu, label: str, shortcut: str | None, handler) -> QAction:
        action = QAction(label, menu)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(handler)
        menu.addAction(action)
        return action

    def _show_shortcuts_dialog(self) -> None:
        QMessageBox.information(
            self,
            "Keyboard Shortcuts",
            "<table cellspacing=6>"
            "<tr><td><b>Space</b></td><td>Play / Pause</td></tr>"
            "<tr><td><b>S</b></td><td>Sync current line to playback, advance</td></tr>"
            "<tr><td><b>Enter</b> (in a line)</td><td>Move to the next line</td></tr>"
            "<tr><td><b>&uarr; / &darr;</b></td><td>Select previous / next line</td></tr>"
            "<tr><td><b>&larr; / &rarr;</b></td><td>Seek -5s / +5s</td></tr>"
            "<tr><td><b>Ctrl+S</b></td><td>Save LRC</td></tr>"
            "<tr><td><b>Ctrl+Z</b></td><td>Undo last sync</td></tr>"
            "<tr><td><b>Ctrl+T</b></td><td>Sort lines by time</td></tr>"
            "<tr><td><b>Ctrl+Shift+O</b></td><td>Shift all timestamps</td></tr>"
            "<tr><td><b>Ctrl+O</b></td><td>Add files</td></tr>"
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

    def _build_file_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel("Audio Files")
        header.setStyleSheet(
            f"font-weight: bold; color: {COLOR_FG.name()}; margin-bottom: 5px;"
        )
        layout.addWidget(header)

        self.file_list = QListWidget()
        self.file_list.setStyleSheet(
            f"background-color: {COLOR_TIMELINE_BG.name()};"
            " border: 1px solid #45475a; font-size: 13px; border-radius: 4px;"
        )
        self.file_list.itemDoubleClicked.connect(self._load_selected_file)
        layout.addWidget(self.file_list)

        btn_style = (
            "QPushButton { background-color: #313244; color: white;"
            " border: 1px solid #45475a; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #45475a; }"
        )
        for label, handler in [
            ("Add Files",          self._add_files_dialog),
            ("Standardize Names",  self._batch_rename_files),
            ("Download Lyrics",    self._batch_download_lyrics),
            ("Clear Completed",    self._remove_completed_files),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            btn.setStyleSheet(btn_style)
            layout.addWidget(btn)

        return panel

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

        # Bottom control row
        ctrl = QHBoxLayout()
        for label, handler, color in [
            ("Save LRC",   self._save_lrc_file,    COLOR_SYNCED),
            ("Web Search", self._get_lyrics_online, COLOR_ACCENT),
            ("Sort Lines", self._sort_verses_by_time, COLOR_UNSYNCED),
            ("Shift All",  self._shift_all_timestamps, COLOR_TIMELINE_BLOCK),
            ("Clear All",  self._clear_lyrics,      COLOR_MISSING),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {color.name()};"
                " color: #1e1e2e; font-weight: bold; padding: 8px; border-radius: 4px; }"
            )
            ctrl.addWidget(btn)
        layout.addLayout(ctrl)

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
        shortcuts: list[tuple[str, object]] = [
            ("Space",    self._toggle_play_pause_smart),
            ("s",        self._sync_next_verse),
            ("Right",    lambda: self._seek_delta(5_000)),
            ("Left",     lambda: self._seek_delta(-5_000)),
            ("Up",       lambda: self._select_verse(self.current_verse_index - 1)),
            ("Down",     lambda: self._select_verse(self.current_verse_index + 1)),
            ("Ctrl+S",   self._save_lrc_file),
            ("Ctrl+Z",   self._undo_last_sync),
            ("Ctrl+T",   self._sort_verses_by_time),
        ]
        for key, fn in shortcuts:
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(fn)

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

    def _clear_lyrics(self) -> None:
        for v in self._verses:
            self.lyrics_layout.removeWidget(v)
            v.deleteLater()
        self._verses.clear()
        self._playback_highlight_verse = None
        self.current_verse_index = 0
        self._update_timeline()
        self._mark_dirty()

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
            idx = self._verses.index(verse)
            self._undo_stack.append((idx, verse.get_timestamp()))
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
        """Revert the most recent sync operation (Ctrl-Z)."""
        if not self._undo_stack:
            self.status_bar.setText("Nothing to undo.")
            return
        idx, old_ts = self._undo_stack.pop()
        if 0 <= idx < len(self._verses):
            self._verses[idx].set_timestamp(old_ts)
            self._update_timeline()
            self._mark_dirty()
            self.status_bar.setText(f"Undid sync on line {idx + 1}.")

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
        for i, verse in enumerate(self._verses):
            old_ts = verse.get_timestamp()
            self._undo_stack.append((i, old_ts))
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
        cur_item = self.file_list.currentItem()
        if cur_item:
            self._update_file_status_color(cur_item, self.current_file_path)
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
            self._clear_lyrics()
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

    def _update_file_status_color(
        self, item: QListWidgetItem, file_path: str
    ) -> None:
        lrc_path = os.path.splitext(file_path)[0] + ".lrc"
        color    = analyze_lrc_status(lrc_path)
        item.setForeground(color)
        # QColor isn't hashable in PyQt6, so key the lookup by name() instead.
        tip = {
            COLOR_SYNCED.name():   "Synced",
            COLOR_UNSYNCED.name(): "Unsynced (timestamps may be [00:00])",
            COLOR_MISSING.name():  "No .lrc file",
        }.get(color.name(), "Unknown")
        item.setToolTip(tip)

    def _process_added_files(self, file_paths: list[str]) -> None:
        # Use a set for O(1) duplicate detection.
        existing: set[str] = {
            self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.file_list.count())
        }
        for path in file_paths:
            if (
                os.path.isfile(path)
                and path.lower().endswith(AUDIO_EXTENSIONS)
                and path not in existing
            ):
                item = QListWidgetItem(os.path.basename(path))
                item.setData(Qt.ItemDataRole.UserRole, path)
                self._update_file_status_color(item, path)
                self.file_list.addItem(item)
                existing.add(path)

        if not self.song_is_loaded and self.file_list.count() > 0:
            self._load_file_from_index(0)

    def _add_files_dialog(self) -> None:
        last_dir = self.settings.value("last_dir", "")
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Add Audio",
            last_dir,
            "Audio (*.mp3 *.flac *.wav *.ogg *.m4a *.opus)",
        )
        if files:
            self.settings.setValue("last_dir", os.path.dirname(files[0]))
            self._process_added_files(files)

    def _batch_rename_files(self) -> None:
        if self.file_list.count() == 0:
            return
        renamed = 0
        for i in range(self.file_list.count()):
            item     = self.file_list.item(i)
            old_path = item.data(Qt.ItemDataRole.UserRole)
            ext      = os.path.splitext(old_path)[1]
            try:
                audio = mutagen.File(old_path, easy=True)
                if not audio or "artist" not in audio or "title" not in audio:
                    continue
                artist   = clean_filename(audio["artist"][0])
                title    = clean_filename(audio["title"][0])
                new_name = f"{artist} - {title}{ext}"
                new_path = os.path.join(os.path.dirname(old_path), new_name)
                if old_path == new_path:
                    continue
                os.rename(old_path, new_path)
                old_lrc = os.path.splitext(old_path)[0] + ".lrc"
                new_lrc = os.path.splitext(new_path)[0] + ".lrc"
                if os.path.exists(old_lrc):
                    os.rename(old_lrc, new_lrc)
                item.setText(new_name)
                item.setData(Qt.ItemDataRole.UserRole, new_path)
                # Keep current_file_path in sync if the loaded file was renamed.
                if old_path == self.current_file_path:
                    self.current_file_path = new_path
                    self.song_filename     = new_name
                    self.file_label.setText(new_name)
                renamed += 1
            except OSError as exc:
                print(f"[warn] rename failed for {old_path}: {exc}")
        self.status_bar.setText(f"Renamed {renamed} file(s).")

    def _remove_completed_files(self) -> None:
        for i in range(self.file_list.count() - 1, -1, -1):
            item = self.file_list.item(i)
            if item.foreground().color() == COLOR_SYNCED:
                self.file_list.takeItem(i)

    def _batch_download_lyrics(self) -> None:
        files: list[str] = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.foreground().color() == COLOR_MISSING:
                files.append(item.data(Qt.ItemDataRole.UserRole))

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

        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            self._update_file_status_color(
                item, item.data(Qt.ItemDataRole.UserRole)
            )
        # Reload lyrics pane if the current song was among the downloads.
        if (
            self.current_file_path
            and os.path.basename(self.current_file_path) in success
        ):
            self._load_song(self.current_file_path)

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
        event.accept()

    # ==================================================================
    # Song loading
    # ==================================================================

    def _load_selected_file(self, item: QListWidgetItem) -> None:
        if not self._confirm_discard_if_dirty():
            return
        self._load_song(item.data(Qt.ItemDataRole.UserRole))

    def _load_file_from_index(self, index: int) -> None:
        if 0 <= index < self.file_list.count():
            item = self.file_list.item(index)
            self.file_list.setCurrentItem(item)
            self._load_song(item.data(Qt.ItemDataRole.UserRole))

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

        lrc_path = os.path.splitext(path)[0] + ".lrc"
        color    = analyze_lrc_status(lrc_path)
        self.file_label.setStyleSheet(
            f"font-weight: bold; font-size: 14px; color: {color.name()};"
        )

        self._clear_lyrics()
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
