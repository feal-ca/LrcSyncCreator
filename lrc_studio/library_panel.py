"""library_panel.py — LibraryPanel: the persistent library sidebar.

Shows every audio file in the configured library folder (scanned on
startup) plus anything added by hand, each with a status dot: synced
lyrics, plain lyrics only, or none. A search box and a status filter
narrow the list; the count line says what's left to do.

Signals out only what it can't do itself: loading a song (needs the
player), renames that may hit the loaded song, and the bulk actions that
run in background threads owned by the main window.
"""

from __future__ import annotations

import os

import mutagen
from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QToolButton, QVBoxLayout, QWidget,
)

import theme
from constants import AUDIO_EXTENSIONS, COLOR_MISSING, COLOR_SYNCED, COLOR_UNSYNCED
from soulseek import normalize_text
from utils import clean_filename, lrc_status_key

PATH_ROLE = Qt.ItemDataRole.UserRole
STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
SEARCH_ROLE = Qt.ItemDataRole.UserRole + 2

_STATUS_TIPS = {
    "synced": "Synced lyrics",
    "plain": "Lyrics without timing. Open it and press S to sync",
    "missing": "No lyrics yet",
}
_COLOR_TO_STATUS = {
    COLOR_SYNCED.name(): "synced",
    COLOR_UNSYNCED.name(): "plain",
    COLOR_MISSING.name(): "missing",
}
_FILTERS = [
    ("All songs", None),
    ("No lyrics", "missing"),
    ("Not synced", "plain"),
    ("Synced", "synced"),
]


def scan_audio_files(root: str) -> list[str]:
    """Every audio file under *root*, skipping hidden folders (.stfolder…)."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.lower().endswith(AUDIO_EXTENSIONS) and not name.startswith("."):
                found.append(os.path.join(dirpath, name))
    found.sort(key=lambda p: os.path.basename(p).lower())
    return found


class LibraryPanel(QWidget):
    song_selected = pyqtSignal(str)
    file_renamed = pyqtSignal(str, str)
    find_music_requested = pyqtSignal()
    fetch_missing_lyrics_requested = pyqtSignal(list)
    convert_library_requested = pyqtSignal()

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._current_path = ""
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("Library")
        title.setProperty("role", "heading")
        head.addWidget(title)
        head.addStretch(1)

        self.add_music_button = QToolButton()
        self.add_music_button.setText("Add music")
        self.add_music_button.setProperty("variant", "primary")
        self.add_music_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        add_menu = QMenu(self.add_music_button)
        add_menu.addAction("Add Files", self.open_add_files_dialog)
        add_menu.addAction("Find Music", self.find_music_requested.emit)
        self.add_music_button.setMenu(add_menu)
        head.addWidget(self.add_music_button)

        self.overflow_button = QToolButton()
        self.overflow_button.setText("⋯")
        self.overflow_button.setToolTip("More library actions")
        self.overflow_button.setProperty("variant", "ghost")
        self.overflow_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        overflow_menu = QMenu(self.overflow_button)
        overflow_menu.addAction("Fetch Lyrics for All Missing", self._request_missing_lyrics)
        overflow_menu.addAction("Rescan Library Folder", self.load_library_folder)
        overflow_menu.addSeparator()
        overflow_menu.addAction("Standardize Names…", self._batch_rename_files)
        overflow_menu.addAction("Convert Lossless Files to Opus…", self.convert_library_requested.emit)
        self.overflow_button.setMenu(overflow_menu)
        head.addWidget(self.overflow_button)
        layout.addLayout(head)

        self.search_input = QLineEdit()
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText("Search your library")
        self.search_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_input)

        self.status_filter = QComboBox()
        for label, key in _FILTERS:
            self.status_filter.addItem(label, key)
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        layout.addWidget(self.status_filter)

        self.file_list = QListWidget()
        self.file_list.setUniformItemSizes(True)
        self.file_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.file_list.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self.file_list, stretch=1)

        self.count_label = QLabel("")
        self.count_label.setProperty("role", "faint")
        self.count_label.setWordWrap(True)
        layout.addWidget(self.count_label)

        self._refresh_placeholder()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def library_dir(self) -> str:
        return str(self._settings.value("library_dir", "") or "").strip()

    def load_library_folder(self) -> int:
        """Add every audio file in the library folder. Returns how many
        new files appeared. Files that vanished from disk are dropped."""
        for i in range(self.file_list.count() - 1, -1, -1):
            path = self.file_list.item(i).data(PATH_ROLE)
            if path and not os.path.exists(path):
                self.file_list.takeItem(i)
        root = self.library_dir()
        if not root or not os.path.isdir(root):
            self._refresh_placeholder()
            return 0
        before = len(self.real_file_paths())
        self.add_files(scan_audio_files(root))
        return len(self.real_file_paths()) - before

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _make_item(self, path: str) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(PATH_ROLE, path)
        self._label_item(item, path)
        self._update_status(item, path)
        return item

    @staticmethod
    def _label_item(item: QListWidgetItem, path: str) -> None:
        stem = os.path.splitext(os.path.basename(path))[0]
        item.setText(stem)
        item.setData(SEARCH_ROLE, normalize_text(stem))

    @staticmethod
    def _update_status(item: QListWidgetItem, path: str) -> None:
        status = lrc_status_key(os.path.splitext(path)[0] + ".lrc")
        item.setData(STATUS_ROLE, status)
        item.setIcon(theme.status_icon(status))
        item.setToolTip(f"{_STATUS_TIPS[status]}\n{path}")

    def _refresh_placeholder(self) -> None:
        for i in range(self.file_list.count() - 1, -1, -1):
            if not self.file_list.item(i).data(PATH_ROLE):
                self.file_list.takeItem(i)
        if self.file_list.count() == 0:
            text = (
                "Drop audio files here, or use Add music."
                if self.library_dir() else
                "Set your library folder in Tools → Settings, or drop audio files here."
            )
            placeholder = QListWidgetItem(text)
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(QColor(theme.TEXT_3))
            self.file_list.addItem(placeholder)
        self._update_counts()

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        path = item.data(PATH_ROLE)
        if path:
            self.song_selected.emit(path)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _apply_filter(self, *_args) -> None:
        words = normalize_text(self.search_input.text()).split()
        wanted = self.status_filter.currentData()
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if not item.data(PATH_ROLE):
                continue
            hay = item.data(SEARCH_ROLE) or ""
            visible = all(w in hay for w in words) and (wanted is None or item.data(STATUS_ROLE) == wanted)
            item.setHidden(not visible)
        self._update_counts()

    def _update_counts(self) -> None:
        counts = {"synced": 0, "plain": 0, "missing": 0}
        total = shown = 0
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if not item.data(PATH_ROLE):
                continue
            total += 1
            counts[item.data(STATUS_ROLE)] = counts.get(item.data(STATUS_ROLE), 0) + 1
            if not item.isHidden():
                shown += 1
        if not total:
            self.count_label.setText("")
            return
        text = f"{total} songs: {counts['synced']} synced, {counts['plain']} not synced, {counts['missing']} without lyrics"
        if shown != total:
            text = f"Showing {shown}. " + text
        self.count_label.setText(text)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def real_file_paths(self) -> list[str]:
        return [
            self.file_list.item(i).data(PATH_ROLE)
            for i in range(self.file_list.count())
            if self.file_list.item(i).data(PATH_ROLE)
        ]

    def paths_with_status(self, status) -> list[str]:
        """Paths whose lyrics status is *status* ("synced" | "plain" |
        "missing", or the matching COLOR_* for older callers)."""
        if isinstance(status, QColor):
            status = _COLOR_TO_STATUS.get(status.name(), "")
        return [
            self.file_list.item(i).data(PATH_ROLE)
            for i in range(self.file_list.count())
            if self.file_list.item(i).data(PATH_ROLE)
            and self.file_list.item(i).data(STATUS_ROLE) == status
        ]

    def status_of(self, path: str) -> str | None:
        item = self._item_for(path)
        return item.data(STATUS_ROLE) if item is not None else None

    def refresh_status(self, path: str) -> None:
        item = self._item_for(path)
        if item is not None:
            self._update_status(item, path)
            self._apply_filter()

    def refresh_all_statuses(self) -> None:
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            path = item.data(PATH_ROLE)
            if path:
                self._update_status(item, path)
        self._apply_filter()

    def replace_path(self, old_path: str, new_path: str) -> None:
        """A file moved on disk (rename, conversion): keep its row."""
        item = self._item_for(old_path)
        if item is None:
            return
        item.setData(PATH_ROLE, new_path)
        self._label_item(item, new_path)
        self._update_status(item, new_path)
        if self._current_path == old_path:
            self._current_path = new_path
        self.file_renamed.emit(old_path, new_path)

    def set_current(self, path: str) -> None:
        """Bold the row of the song loaded in the editor."""
        self._current_path = path
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            font = item.font()
            font.setWeight(QFont.Weight.Bold if item.data(PATH_ROLE) == path else QFont.Weight.Normal)
            item.setFont(font)

    def next_path(self, path: str) -> str | None:
        """The next visible song after *path* (for auto-advance)."""
        seen = False
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            p = item.data(PATH_ROLE)
            if not p or item.isHidden():
                continue
            if seen:
                return p
            seen = p == path
        return None

    def select_path(self, path: str) -> None:
        item = self._item_for(path)
        if item is not None:
            if item.isHidden():
                self.search_input.clear()
                self.status_filter.setCurrentIndex(0)
            self.file_list.setCurrentItem(item)
            self.file_list.scrollToItem(item)

    def select_row(self, index: int) -> None:
        if 0 <= index < self.file_list.count():
            self.file_list.setCurrentRow(index)

    def current_row(self) -> int:
        return self.file_list.currentRow()

    def row_count(self) -> int:
        return len(self.real_file_paths())

    def add_files(self, file_paths: list[str]) -> None:
        existing = set(self.real_file_paths())
        self.file_list.setUpdatesEnabled(False)
        try:
            for path in file_paths:
                if path and path.lower().endswith(AUDIO_EXTENSIONS) and path not in existing:
                    self.file_list.addItem(self._make_item(path))
                    existing.add(path)
            self.file_list.sortItems()
        finally:
            self.file_list.setUpdatesEnabled(True)
        self._refresh_placeholder()
        self._apply_filter()

    def add_files_single(self, path: str) -> None:
        self.add_files([path])

    def open_add_files_dialog(self) -> None:
        start = str(self._settings.value("last_dir", "") or self.library_dir())
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add Audio Files", start,
            "Audio files (" + " ".join(f"*{e}" for e in AUDIO_EXTENSIONS) + ")",
        )
        if files:
            self._settings.setValue("last_dir", os.path.dirname(files[0]))
            self.add_files(files)

    def _item_for(self, path: str) -> QListWidgetItem | None:
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(PATH_ROLE) == path:
                return item
        return None

    # ------------------------------------------------------------------
    # Bulk actions
    # ------------------------------------------------------------------

    def _request_missing_lyrics(self) -> None:
        self.fetch_missing_lyrics_requested.emit(self.paths_with_status("missing"))

    def _batch_rename_files(self) -> None:
        paths = self.real_file_paths()
        if not paths:
            return
        resp = QMessageBox.question(
            self, "Standardize Names",
            f"Rename up to {len(paths)} files to “Artist - Title” using their tags?\n\n"
            "Files without artist and title tags are left alone, and each "
            "song's .lrc file is renamed with it.",
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        renamed = 0
        for old_path in paths:
            ext = os.path.splitext(old_path)[1]
            try:
                audio = mutagen.File(old_path, easy=True)
                if not audio or "artist" not in audio or "title" not in audio:
                    continue
                artist = clean_filename(audio["artist"][0])
                title = clean_filename(audio["title"][0])
                new_path = os.path.join(os.path.dirname(old_path), f"{artist} - {title}{ext}")
                if old_path == new_path or os.path.exists(new_path):
                    continue
                os.rename(old_path, new_path)
                old_lrc = os.path.splitext(old_path)[0] + ".lrc"
                new_lrc = os.path.splitext(new_path)[0] + ".lrc"
                if os.path.exists(old_lrc) and not os.path.exists(new_lrc):
                    os.rename(old_lrc, new_lrc)
                self.replace_path(old_path, new_path)
                renamed += 1
            except Exception:
                continue
        self.file_list.sortItems()
        self._apply_filter()
        QMessageBox.information(self, "Standardize Names", f"Renamed {renamed} files.")

