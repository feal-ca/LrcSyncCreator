"""library_panel.py — LibraryPanel: the library sidebar.

Every audio file in the library folder, newest first, each with a status
dot (synced lyrics, plain lyrics only, none) and a "new" tag until it has
been opened once. One filter (with live counts) and a search box narrow
the list. Everything you can do to songs lives on the right-click menu and
works on a multi-selection; keyboard: Enter opens, Del moves to the trash,
F2 edits artist/title, Ctrl+R renames to "Artist - Title".

The panel renames files itself; anything that touches the player or runs
in a background thread is handed to the main window through signals.
"""

from __future__ import annotations

import os
import subprocess
import time

from PyQt6.QtCore import QModelIndex, QRect, QSettings, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont, QKeySequence, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QStyledItemDelegate,
    QStyleOptionViewItem, QVBoxLayout, QWidget,
)

import postprocess
import theme
from constants import AUDIO_EXTENSIONS
from library_index import LibraryIndex
from soulseek import normalize_text
from utils import lrc_status_key

PATH_ROLE = Qt.ItemDataRole.UserRole
STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
SEARCH_ROLE = Qt.ItemDataRole.UserRole + 2
NEW_ROLE = Qt.ItemDataRole.UserRole + 3

_STATUS_TIPS = {
    "synced": "Synced lyrics",
    "plain": "Lyrics without timing. Open it and press S to sync",
    "missing": "No lyrics yet",
}
# (label, data) — the label gets " (count)" appended live.
_FILTERS = [
    ("All songs", None),
    ("New", "new"),
    ("No lyrics", "missing"),
    ("Not synced", "plain"),
]
_SORTS = [("Newest first", "newest"), ("A–Z", "name")]
_LOSSLESS_EXTENSIONS = (".flac", ".wav", ".aiff", ".aif", ".ape", ".wv")


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


def show_in_folder(path: str) -> None:
    """Open the file manager with *path* selected (falls back to just
    opening its folder when no FileManager1 service answers)."""
    uri = QUrl.fromLocalFile(path).toString()
    try:
        done = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
             "--type=method_call", "/org/freedesktop/FileManager1",
             "org.freedesktop.FileManager1.ShowItems", f"array:string:{uri}", "string:"],
            capture_output=True, timeout=3, check=False,
        )
        if done.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


class _SongItem(QListWidgetItem):
    """A row that sorts by an explicit key instead of its text."""

    sort_key: tuple = ()

    def __lt__(self, other: QListWidgetItem) -> bool:  # noqa: D105
        return self.sort_key < getattr(other, "sort_key", ())


class _NewTagDelegate(QStyledItemDelegate):
    """Draws a small accent "new" tag at the right of new rows, keeping the
    title clear of it."""

    TAG_W = 38

    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        if index.data(NEW_ROLE):
            room = option.rect.width() - option.decorationSize.width() - self.TAG_W - 22
            option.text = option.fontMetrics.elidedText(option.text, Qt.TextElideMode.ElideRight, max(room, 20))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        if not index.data(NEW_ROLE):
            return
        r = option.rect
        tag = QRect(r.right() - self.TAG_W - 6, r.center().y() - 8, self.TAG_W, 16)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        accent = QColor(theme.ACCENT)
        fill = QColor(accent)
        fill.setAlpha(46)
        painter.setBrush(fill)
        painter.drawRoundedRect(tag, 8, 8)
        font = QFont(option.font)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(accent)
        painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, "new")
        painter.restore()


class LibraryPanel(QWidget):
    """Signals
    -------
    song_selected(path)            open this song in the editor
    file_renamed(old, new)         a file moved on disk (rename, conversion)
    lyrics_requested(paths)        fetch lyrics automatically for these
    lyrics_picker_requested(path)  open the lyrics picker for this song
    convert_requested(paths)       convert these lossless files to Opus
    trash_requested(paths)         move these to the trash (the window
                                   confirms, unloads and calls remove_paths)
    """

    song_selected = pyqtSignal(str)
    file_renamed = pyqtSignal(str, str)
    lyrics_requested = pyqtSignal(list)
    lyrics_picker_requested = pyqtSignal(str)
    convert_requested = pyqtSignal(list)
    trash_requested = pyqtSignal(list)

    def __init__(self, settings: QSettings, parent: QWidget | None = None,
                 index_path: str | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._current_path = ""
        self._items: dict[str, QListWidgetItem] = {}
        if index_path is None:
            index_path = os.path.join(os.path.dirname(settings.fileName()) or ".", "library_index.json")
        self._index = LibraryIndex(index_path)
        self._index.load()
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title = QLabel("Library")
        title.setProperty("role", "heading")
        layout.addWidget(title)

        self.search_input = QLineEdit()
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText("Search your library")
        self.search_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_input)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.status_filter = QComboBox()
        for label, key in _FILTERS:
            self.status_filter.addItem(label, key)
        self.status_filter.setToolTip("Show only some songs")
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self.status_filter, stretch=1)
        self.sort_combo = QComboBox()
        for label, key in _SORTS:
            self.sort_combo.addItem(label, key)
        saved = str(self._settings.value("library_sort", "newest") or "newest")
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(saved)))
        self.sort_combo.setToolTip("Order of the list")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        row.addWidget(self.sort_combo)
        layout.addLayout(row)

        self.file_list = QListWidget()
        self.file_list.setUniformItemSizes(True)
        self.file_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.setItemDelegate(_NewTagDelegate(self.file_list))
        self.file_list.itemActivated.connect(self._on_item_activated)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.file_list, stretch=1)

        for keys, handler in (
            (QKeySequence.StandardKey.Delete, self.trash_selected),
            ("F2", self.edit_tags_of_selected),
            ("Ctrl+R", self.rename_selected),
            (QKeySequence.StandardKey.SelectAll, self.select_all_visible),
        ):
            action = QAction(self.file_list)
            action.setShortcut(QKeySequence(keys))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(handler)
            self.file_list.addAction(action)

        self._refresh_placeholder()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def library_dir(self) -> str:
        return str(self._settings.value("library_dir", "") or "").strip()

    def load_library_folder(self) -> int:
        """Show every audio file in the library folder. Returns how many
        rows appeared. Files that vanished from disk are dropped."""
        root = self.library_dir()
        gone = [p for p in self._items if not os.path.exists(p)]
        if not root or not os.path.isdir(root):
            # Folder unavailable (a drive not mounted yet): hide the rows but
            # keep their history, or every song would come back as "new".
            self._remove_items(gone, forget=False)
            self._refresh_placeholder()
            return 0
        self._remove_items(gone)
        scanned = scan_audio_files(root)
        before = len(self._items)
        self._index.sync(sorted(set(self._items) | set(scanned)))
        self._add_items(scanned)
        self._save_index()
        return len(self._items) - before

    def add_files(self, file_paths: list[str]) -> None:
        """Songs that just arrived (a download, a drop): they show up as new."""
        paths = [p for p in file_paths if p and p.lower().endswith(AUDIO_EXTENSIONS)]
        self._index.add(paths)
        self._add_items(paths)
        self._save_index()

    def add_files_single(self, path: str) -> None:
        self.add_files([path])

    def _add_items(self, paths: list[str]) -> None:
        self.file_list.setUpdatesEnabled(False)
        try:
            for path in paths:
                if path in self._items:
                    continue
                item = _SongItem()
                item.setData(PATH_ROLE, path)
                self._items[path] = item
                self._label_item(item, path)
                self._update_status(item, path)
                self._update_new(item, path)
                self.file_list.addItem(item)
        finally:
            self.file_list.setUpdatesEnabled(True)
        self._resort()
        self._refresh_placeholder()
        self._apply_filter()

    def _remove_items(self, paths: list[str], forget: bool = True) -> None:
        for path in paths:
            item = self._items.pop(path, None)
            if item is not None:
                self.file_list.takeItem(self.file_list.row(item))
            if forget:
                self._index.forget(path)

    def _save_index(self) -> None:
        try:
            self._index.save()
        except OSError:
            pass   # losing "new" flags is harmless; never block the library on it

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _label_item(self, item: QListWidgetItem, path: str) -> None:
        stem = os.path.splitext(os.path.basename(path))[0]
        item.setText(stem)
        item.setData(SEARCH_ROLE, normalize_text(stem))
        self._set_sort_key(item, path)

    def _set_sort_key(self, item: QListWidgetItem, path: str) -> None:
        name = item.text().lower()
        if self.sort_combo.currentData() == "name":
            item.sort_key = (name,)
        else:
            item.sort_key = (-self._index.added_at(path), name)

    @staticmethod
    def _update_status(item: QListWidgetItem, path: str) -> None:
        status = lrc_status_key(os.path.splitext(path)[0] + ".lrc")
        item.setData(STATUS_ROLE, status)
        item.setIcon(theme.status_icon(status))

    def _update_new(self, item: QListWidgetItem, path: str) -> None:
        item.setData(NEW_ROLE, self._index.is_new(path))
        added = self._index.added_at(path)
        when = time.strftime("%d %b %Y", time.localtime(added)) if added else "unknown"
        item.setToolTip(f"{_STATUS_TIPS[item.data(STATUS_ROLE)]}\nAdded {when}\n{path}")

    def _resort(self) -> None:
        self.file_list.sortItems()

    def _on_sort_changed(self, *_args) -> None:
        self._settings.setValue("library_sort", self.sort_combo.currentData())
        for path, item in self._items.items():
            self._set_sort_key(item, path)
        self._resort()

    def _refresh_placeholder(self) -> None:
        for i in range(self.file_list.count() - 1, -1, -1):
            if not self.file_list.item(i).data(PATH_ROLE):
                self.file_list.takeItem(i)
        if not self._items:
            text = (
                "Drop audio files here, or find music with Ctrl+F."
                if self.library_dir() else
                "Choose your music folder in File → Settings."
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
        for item in self._items.values():
            hay = item.data(SEARCH_ROLE) or ""
            if wanted == "new":
                keep = bool(item.data(NEW_ROLE))
            else:
                keep = wanted is None or item.data(STATUS_ROLE) == wanted
            item.setHidden(not (keep and all(w in hay for w in words)))
        self._update_counts()

    def _update_counts(self) -> None:
        counts = {None: len(self._items), "new": 0, "missing": 0, "plain": 0}
        for item in self._items.values():
            if item.data(NEW_ROLE):
                counts["new"] += 1
            status = item.data(STATUS_ROLE)
            if status in counts:
                counts[status] += 1
        for i, (label, key) in enumerate(_FILTERS):
            self.status_filter.setItemText(i, f"{label} ({counts[key]})")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def real_file_paths(self) -> list[str]:
        return list(self._items)

    def paths_in_view_order(self) -> list[str]:
        return [
            self.file_list.item(i).data(PATH_ROLE)
            for i in range(self.file_list.count())
            if self.file_list.item(i).data(PATH_ROLE)
        ]

    def paths_with_status(self, status: str) -> list[str]:
        """Paths whose lyrics status is *status* ("synced" | "plain" | "missing")."""
        return [p for p in self.paths_in_view_order() if self._items[p].data(STATUS_ROLE) == status]

    def status_of(self, path: str) -> str | None:
        item = self._items.get(path)
        return item.data(STATUS_ROLE) if item is not None else None

    def is_new(self, path: str) -> bool:
        return self._index.is_new(path)

    def refresh_status(self, path: str) -> None:
        item = self._items.get(path)
        if item is not None:
            self._update_status(item, path)
            self._update_new(item, path)
            self._apply_filter()

    def refresh_all_statuses(self) -> None:
        for path, item in self._items.items():
            self._update_status(item, path)
            self._update_new(item, path)
        self._apply_filter()

    def replace_path(self, old_path: str, new_path: str) -> None:
        """A file moved on disk (rename, conversion): keep its row."""
        item = self._items.pop(old_path, None)
        if item is None:
            return
        self._items[new_path] = item
        self._index.rename(old_path, new_path)
        item.setData(PATH_ROLE, new_path)
        self._label_item(item, new_path)
        self._update_status(item, new_path)
        self._update_new(item, new_path)
        if self._current_path == old_path:
            self._current_path = new_path
        self._save_index()
        self.file_renamed.emit(old_path, new_path)

    def remove_paths(self, paths: list[str]) -> None:
        """Drop rows for files that are gone (moved to the trash)."""
        self._remove_items(paths)
        self._save_index()
        self._refresh_placeholder()
        self._apply_filter()

    def set_current(self, path: str) -> None:
        """Bold the row of the song loaded in the editor; it's been seen now."""
        previous = self._items.get(self._current_path)
        if previous is not None:
            font = previous.font()
            font.setWeight(QFont.Weight.Normal)
            previous.setFont(font)
        self._current_path = path
        item = self._items.get(path)
        if item is not None:
            font = item.font()
            font.setWeight(QFont.Weight.Bold)
            item.setFont(font)
        self.mark_seen([path])

    def mark_seen(self, paths: list[str]) -> None:
        if not any(self._index.is_new(p) for p in paths):
            return
        self._index.mark_seen(paths)
        for path in paths:
            item = self._items.get(path)
            if item is not None:
                self._update_new(item, path)
        self._save_index()
        self._apply_filter()

    def next_path(self, path: str) -> str | None:
        """The next visible song after *path* (for auto-advance)."""
        seen = False
        for p in self.paths_in_view_order():
            if self._items[p].isHidden():
                continue
            if seen:
                return p
            seen = p == path
        return None

    def select_path(self, path: str) -> None:
        item = self._items.get(path)
        if item is not None:
            if item.isHidden():
                self.search_input.clear()
                self.status_filter.setCurrentIndex(0)
            self.file_list.setCurrentItem(item)
            self.file_list.scrollToItem(item)

    def select_all_visible(self) -> None:
        self.file_list.clearSelection()
        for item in self._items.values():
            if not item.isHidden():
                item.setSelected(True)

    def selected_paths(self) -> list[str]:
        """Selected songs in on-screen order, never ones the filter hides."""
        return [
            p for p in self.paths_in_view_order()
            if self._items[p].isSelected() and not self._items[p].isHidden()
        ]

    def row_count(self) -> int:
        return len(self._items)

    def _item_for(self, path: str) -> QListWidgetItem | None:
        return self._items.get(path)

    # ------------------------------------------------------------------
    # Right-click menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        clicked = self.file_list.itemAt(pos)
        if clicked is None or not clicked.data(PATH_ROLE):
            return
        if not clicked.isSelected():
            self.file_list.setCurrentItem(clicked)
        paths = self.selected_paths() or [clicked.data(PATH_ROLE)]
        self.build_context_menu(paths).exec(self.file_list.viewport().mapToGlobal(pos))

    def menu_actions_for(self, paths: list[str]) -> list[str]:
        return [a.text() for a in self.build_context_menu(paths).actions() if not a.isSeparator()]

    def build_context_menu(self, paths: list[str]) -> QMenu:
        menu = QMenu(self)
        self.fill_menu(menu, paths)
        return menu

    def fill_menu(self, menu: QMenu, paths: list[str]) -> None:
        """Add the actions for *paths* to *menu* (the right-click menu and
        the window's Library menu share this)."""
        if len(paths) == 1:
            self._fill_single_menu(menu, paths[0])
        else:
            self._fill_multi_menu(menu, paths)
        menu.setToolTipsVisible(True)
        for action in menu.actions():
            # The shortcuts shown here are labels; the list's own actions
            # handle the keys, so they never fire from another widget.
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def _fill_single_menu(self, menu: QMenu, path: str) -> None:
        open_action = menu.addAction("Open in Editor", lambda: self.song_selected.emit(path))
        open_action.setShortcut(QKeySequence("Return"))
        bold = open_action.font()
        bold.setBold(True)
        open_action.setFont(bold)
        menu.addSeparator()
        find = menu.addAction("Find Lyrics…", lambda: self.lyrics_picker_requested.emit(path))
        find.setShortcut(QKeySequence("Ctrl+L"))
        [plan] = postprocess.plan_standard_names([path])
        if plan.new:
            rename = menu.addAction(
                f"Rename to “{os.path.splitext(os.path.basename(plan.new))[0]}”",
                lambda: self._apply_rename_plans([plan]),
            )
        else:
            rename = menu.addAction("Rename to “Artist - Title”")
            rename.setEnabled(False)
            rename.setToolTip(plan.reason)
        rename.setShortcut(QKeySequence("Ctrl+R"))
        tags = menu.addAction("Edit Artist && Title…", lambda: self._edit_tags(path))
        tags.setShortcut(QKeySequence("F2"))
        menu.addSeparator()
        menu.addAction("Show in Folder", lambda: show_in_folder(path))
        if self._index.is_new(path):
            menu.addAction("Mark as Seen", lambda: self.mark_seen([path]))
        menu.addSeparator()
        trash = menu.addAction("Move to Trash…", lambda: self.trash_requested.emit([path]))
        trash.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))

    def _fill_multi_menu(self, menu: QMenu, paths: list[str]) -> None:
        unsynced = [p for p in paths if self.status_of(p) != "synced"]
        find = menu.addAction(f"Find Lyrics for {_plural(len(unsynced), 'Song')}",
                              lambda: self.lyrics_requested.emit(unsynced))
        find.setEnabled(bool(unsynced))
        find.setShortcut(QKeySequence("Ctrl+L"))
        rename = menu.addAction(f"Rename {len(paths)} to “Artist - Title”…",
                                lambda: self._rename_with_preview(paths))
        rename.setShortcut(QKeySequence("Ctrl+R"))
        lossless = [p for p in paths if self._is_lossless(p)]
        if lossless:
            menu.addAction(f"Convert {len(lossless)} to Opus…", lambda: self.convert_requested.emit(lossless))
        new = [p for p in paths if self._index.is_new(p)]
        if new:
            menu.addAction(f"Mark {len(new)} as Seen", lambda: self.mark_seen(new))
        menu.addSeparator()
        trash = menu.addAction(f"Move {len(paths)} to Trash…", lambda: self.trash_requested.emit(list(paths)))
        trash.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))

    @staticmethod
    def _is_lossless(path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        if ext in _LOSSLESS_EXTENSIONS:
            return True
        if ext == ".m4a":
            try:
                import convert
                return convert.is_lossless(path)
            except Exception:
                return False
        return False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def trash_selected(self) -> None:
        paths = self.selected_paths()
        if paths:
            self.trash_requested.emit(paths)

    def rename_selected(self) -> None:
        paths = self.selected_paths()
        if len(paths) == 1:
            [plan] = postprocess.plan_standard_names(paths)
            if plan.new:
                self._apply_rename_plans([plan])
            else:
                QMessageBox.information(self, "Can't Rename", plan.reason)
        elif paths:
            self._rename_with_preview(paths)

    def edit_tags_of_selected(self) -> None:
        paths = self.selected_paths()
        if len(paths) == 1:
            self._edit_tags(paths[0])

    def _rename_with_preview(self, paths: list[str]) -> None:
        from dialogs import RenamePreviewDialog

        plans = postprocess.plan_standard_names(paths)
        dialog = RenamePreviewDialog(plans, self)
        if dialog.exec() == RenamePreviewDialog.DialogCode.Accepted:
            self._apply_rename_plans(plans)

    def _apply_rename_plans(self, plans: list[postprocess.RenamePlan]) -> None:
        for old, new in postprocess.apply_renames(plans):
            self.replace_path(old, new)
        self._resort()
        self._apply_filter()

    def _edit_tags(self, path: str) -> None:
        from dialogs import TagEditDialog

        dialog = TagEditDialog(path, self)
        if dialog.exec() == TagEditDialog.DialogCode.Accepted:
            self.refresh_status(path)
