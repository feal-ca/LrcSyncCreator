"""library_panel.py — LibraryPanel: the persistent file-list sidebar.

Owns everything that's purely about the file list (adding, removing,
renaming, status-coloring). Signals out only the two things it can't do
itself: loading a song into the editor (needs the VLC player) and a file
rename that might be the currently-loaded song (needs app-level state).
"""

from __future__ import annotations

import os

import mutagen
from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFileDialog, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)

from constants import (
    AUDIO_EXTENSIONS, COLOR_FG, COLOR_MISSING, COLOR_MUTED, COLOR_SYNCED,
    COLOR_TIMELINE_BG, COLOR_UNSYNCED,
)
from utils import analyze_lrc_status, clean_filename


class LibraryPanel(QWidget):
    song_selected = pyqtSignal(str)
    file_renamed = pyqtSignal(str, str)

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel("Audio Files")
        header.setStyleSheet(f"font-weight: bold; color: {COLOR_FG.name()}; margin-bottom: 5px;")
        layout.addWidget(header)

        self.file_list = QListWidget()
        self.file_list.setStyleSheet(
            f"background-color: {COLOR_TIMELINE_BG.name()};"
            " border: 1px solid #45475a; font-size: 13px; border-radius: 4px;"
        )
        self.file_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.file_list)

        btn_style = (
            f"QPushButton {{ background-color: #313244; color: {COLOR_FG.name()};"
            " border: 1px solid #45475a; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #45475a; }"
        )

        layout.addWidget(self._make_group_label("Add music"))
        for label, handler in [
            ("Add Files", self.open_add_files_dialog),
            ("Find on Soulseek", lambda: None),      # Task 2 rewires this
            ("Import from Spotify", lambda: None),   # Task 2 rewires this
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            btn.setStyleSheet(btn_style)
            layout.addWidget(btn)

        layout.addWidget(self._make_group_label("Manage"))
        for label, handler in [
            ("Standardize Names", self._batch_rename_files),
            ("Download Lyrics", lambda: None),        # Task 2 rewires this
            ("Clear Completed", self._remove_completed_files),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            btn.setStyleSheet(btn_style)
            layout.addWidget(btn)

        self._refresh_placeholder()

    @staticmethod
    def _make_group_label(text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setStyleSheet(
            f"color: {COLOR_MUTED.name()}; font-size: 10px; font-weight: bold;"
            " letter-spacing: 1px; margin-top: 6px;"
        )
        return label

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.song_selected.emit(path)

    def _refresh_placeholder(self) -> None:
        for i in range(self.file_list.count() - 1, -1, -1):
            if not self.file_list.item(i).data(Qt.ItemDataRole.UserRole):
                self.file_list.takeItem(i)
        if self.file_list.count() == 0:
            placeholder = QListWidgetItem('Drag audio files here, or click "Add Files" below')
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(COLOR_MUTED)
            self.file_list.addItem(placeholder)

    def real_file_paths(self) -> list[str]:
        return [
            self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.file_list.count())
            if self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
        ]

    def paths_with_status(self, color: QColor) -> list[str]:
        return [
            self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.file_list.count())
            if self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            and self.file_list.item(i).foreground().color() == color
        ]

    def refresh_status(self, path: str) -> None:
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self._update_status_color(item, path)
                return

    @staticmethod
    def _update_status_color(item: QListWidgetItem, file_path: str) -> None:
        lrc_path = os.path.splitext(file_path)[0] + ".lrc"
        color = analyze_lrc_status(lrc_path)
        item.setForeground(color)
        tip = {
            COLOR_SYNCED.name(): "Synced",
            COLOR_UNSYNCED.name(): "Unsynced (timestamps may be [00:00])",
            COLOR_MISSING.name(): "No .lrc file",
        }.get(color.name(), "Unknown")
        item.setToolTip(tip)

    def select_path(self, path: str) -> None:
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self.file_list.setCurrentItem(item)
                return

    def select_row(self, index: int) -> None:
        if 0 <= index < self.file_list.count():
            self.file_list.setCurrentItem(self.file_list.item(index))

    def current_row(self) -> int:
        return self.file_list.currentRow()

    def row_count(self) -> int:
        return self.file_list.count()

    def add_files(self, file_paths: list[str]) -> None:
        existing = set(self.real_file_paths())
        for path in file_paths:
            if os.path.isfile(path) and path.lower().endswith(AUDIO_EXTENSIONS) and path not in existing:
                item = QListWidgetItem(os.path.basename(path))
                item.setData(Qt.ItemDataRole.UserRole, path)
                self._update_status_color(item, path)
                self.file_list.addItem(item)
                existing.add(path)
        self._refresh_placeholder()

    def open_add_files_dialog(self) -> None:
        last_dir = self._settings.value("last_dir", "")
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add Audio", last_dir, "Audio (*.mp3 *.flac *.wav *.ogg *.m4a *.opus)",
        )
        if files:
            self._settings.setValue("last_dir", os.path.dirname(files[0]))
            self.add_files(files)

    def _remove_completed_files(self) -> None:
        for i in range(self.file_list.count() - 1, -1, -1):
            item = self.file_list.item(i)
            if item.foreground().color() == COLOR_SYNCED:
                self.file_list.takeItem(i)
        self._refresh_placeholder()

    def _batch_rename_files(self) -> None:
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            old_path = item.data(Qt.ItemDataRole.UserRole)
            if not old_path:
                continue
            ext = os.path.splitext(old_path)[1]
            try:
                audio = mutagen.File(old_path, easy=True)
                if not audio or "artist" not in audio or "title" not in audio:
                    continue
                artist = clean_filename(audio["artist"][0])
                title = clean_filename(audio["title"][0])
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
                self.file_renamed.emit(old_path, new_path)
            except Exception:
                continue
