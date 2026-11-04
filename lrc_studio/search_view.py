"""search_view.py — the Search page: Soulseek search, Nicotine+-style but
with less to sort through.

Type an artist and title and press Enter. Results stream in as peers
answer, grouped into song versions (one row each, the individual copies
underneath) and ranked so the version you most likely want is on top and
already selected. Enter downloads its best copy, with the other copies as
automatic fallbacks; Shift+Enter downloads the whole album from that peer.
Each row then shows its download, and versions you already have say
"In library".

``SearchResultsWidget`` (filter bar + flat table) is the Spotify review's
"Change match" picker.
"""

from __future__ import annotations

import re
from typing import Callable

from PyQt6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject, QSettings, QSortFilterProxyModel, Qt,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QGuiApplication, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QProgressBar, QPushButton,
    QStackedWidget, QTableView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import theme
from search_groups import SongVersion, VersionIndex, owned_stems
from soulseek import ResultFilter, SoulseekClient, SoulseekFile
from workers import FolderBrowseThread

HISTORY_KEY = "search_history"
HISTORY_LIMIT = 30

COL_FILE, COL_FOLDER, COL_QUALITY, COL_LENGTH, COL_SIZE, COL_SPEED, COL_SLOT, COL_USER = range(8)
_HEADERS = ["File", "Folder", "Quality", "Length", "Size", "Speed", "Slot", "User"]

_FORMAT_CHOICES = [
    ("Any format", "any"),
    ("Lossless", "lossless"),
    ("Lossy", "lossy"),
    ("MP3", "mp3"),
    ("Opus", "opus"),
    ("M4A / AAC", "m4a"),
    ("OGG", "ogg"),
]
_BITRATE_CHOICES = [("Any bitrate", 0), ("128+ kbps", 128), ("192+ kbps", 192), ("256+ kbps", 256), ("320 kbps", 320)]


def _fmt_length(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt_speed(bps: int | None) -> str:
    if not bps:
        return ""
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    return f"{bps // 1024} KB/s"


class ResultsModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.files: list[SoulseekFile] = []

    def set_files(self, files: list[SoulseekFile]) -> None:
        self.beginResetModel()
        self.files = list(files)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.files)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        f = self.files[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return {
                COL_FILE: f.display_name,
                COL_FOLDER: f.folder_name,
                COL_QUALITY: f.format_label,
                COL_LENGTH: _fmt_length(f.length_s),
                COL_SIZE: f"{f.size / (1024 * 1024):.1f} MB",
                COL_SPEED: _fmt_speed(f.upload_speed),
                COL_SLOT: "Free" if f.has_free_slot else f"Queue {f.queue_length}",
                COL_USER: f.username,
            }[col]
        if role == Qt.ItemDataRole.UserRole:   # sort key
            if col == COL_QUALITY:
                return 100_000 if f.is_lossless else (f.effective_kbps or 0)
            if col == COL_LENGTH:
                return f.length_s or 0
            if col == COL_SIZE:
                return f.size
            if col == COL_SPEED:
                return f.upload_speed or 0
            if col == COL_SLOT:
                return -1 if f.has_free_slot else f.queue_length
            return (self.data(index) or "").lower()
        if role == Qt.ItemDataRole.ToolTipRole:
            if col in (COL_FILE, COL_FOLDER):
                return f.filename.replace("\\", "/")
            if col == COL_QUALITY and f.quality_ok is False:
                return "Below 192 kbps (128 for Opus/OGG)"
            return None
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_SLOT and f.has_free_slot:
                return QColor(theme.OK)
            if f.quality_ok is False:
                return QColor(theme.TEXT_3)
            if col in (COL_FOLDER, COL_USER, COL_SPEED, COL_SIZE, COL_LENGTH):
                return QColor(theme.TEXT_2)
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (COL_LENGTH, COL_SIZE, COL_SPEED):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None


class ResultsProxy(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.result_filter = ResultFilter()
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setDynamicSortFilter(False)

    def refilter(self) -> None:
        if hasattr(self, "beginFilterChange"):     # Qt >= 6.9
            self.beginFilterChange()
            self.endFilterChange()
        else:
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model: ResultsModel = self.sourceModel()
        return self.result_filter.matches(model.files[source_row])


class SearchResultsWidget(QWidget):
    """Filter bar + sortable results table. Emits ``activated(files)``
    on double-click / Enter with the selected (visible) rows."""

    activated = pyqtSignal(list)
    folder_requested = pyqtSignal(object)   # SoulseekFile whose folder to grab
    selection_changed = pyqtSignal(int)

    def __init__(self, parent=None, show_folder_action: bool = True) -> None:
        super().__init__(parent)
        self._show_folder_action = show_folder_action
        self.model = ResultsModel(self)
        self.proxy = ResultsProxy(self)
        self.proxy.setSourceModel(self.model)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter results by artist, album or uploader.  -word excludes")
        self.filter_input.setClearButtonEnabled(True)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.filter_input.textChanged.connect(lambda _t: self._filter_timer.start())
        bar.addWidget(self.filter_input, stretch=1)

        self.format_combo = QComboBox()
        for label, key in _FORMAT_CHOICES:
            self.format_combo.addItem(label, key)
        self.format_combo.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self.format_combo)

        self.bitrate_combo = QComboBox()
        for label, kbps in _BITRATE_CHOICES:
            self.bitrate_combo.addItem(label, kbps)
        self.bitrate_combo.setToolTip("Minimum bitrate for lossy files; lossless always passes")
        self.bitrate_combo.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self.bitrate_combo)

        self.free_slot_check = QCheckBox("Free slot")
        self.free_slot_check.setToolTip("Only peers that can start sending right away")
        self.free_slot_check.toggled.connect(self._apply_filter)
        bar.addWidget(self.free_slot_check)
        layout.addLayout(bar)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)   # keep best-first order until a header is clicked
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_FOLDER, QHeaderView.ResizeMode.Interactive)
        for col in (COL_QUALITY, COL_LENGTH, COL_SIZE, COL_SPEED, COL_SLOT, COL_USER):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(COL_FOLDER, 210)
        header.resizeSection(COL_QUALITY, 86)
        header.resizeSection(COL_LENGTH, 58)
        header.resizeSection(COL_SIZE, 76)
        header.resizeSection(COL_SPEED, 84)
        header.resizeSection(COL_SLOT, 78)
        header.resizeSection(COL_USER, 120)
        self.table.doubleClicked.connect(lambda _i: self._emit_activated())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: self.selection_changed.emit(len(self.selected_files()))
        )
        enter = QAction(self.table)
        enter.setShortcuts([QKeySequence(Qt.Key.Key_Return), QKeySequence(Qt.Key.Key_Enter)])
        enter.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        enter.triggered.connect(self._emit_activated)
        self.table.addAction(enter)
        layout.addWidget(self.table, stretch=1)

    # -- data ---------------------------------------------------------------

    def set_results(self, files: list[SoulseekFile]) -> None:
        self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.model.set_files(files)

    def total_count(self) -> int:
        return len(self.model.files)

    def visible_count(self) -> int:
        return self.proxy.rowCount()

    def selected_files(self) -> list[SoulseekFile]:
        rows = sorted({self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.files[r] for r in rows]

    def file_at_proxy_row(self, row: int) -> SoulseekFile | None:
        if 0 <= row < self.proxy.rowCount():
            return self.model.files[self.proxy.mapToSource(self.proxy.index(row, 0)).row()]
        return None

    def select_first(self) -> None:
        if self.proxy.rowCount():
            self.table.selectRow(0)

    # -- filtering ----------------------------------------------------------

    def _apply_filter(self) -> None:
        f = self.proxy.result_filter
        f.set_text(self.filter_input.text())
        f.formats = self.format_combo.currentData()
        f.min_kbps = int(self.bitrate_combo.currentData() or 0)
        f.free_slot_only = self.free_slot_check.isChecked()
        self.proxy.refilter()
        self.selection_changed.emit(len(self.selected_files()))

    def set_filter_text(self, text: str) -> None:
        self.filter_input.setText(text)
        self._apply_filter()

    # -- actions ------------------------------------------------------------

    def _emit_activated(self) -> None:
        files = self.selected_files()
        if files:
            self.activated.emit(files)

    def _show_context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        if not self.table.selectionModel().isRowSelected(index.row(), QModelIndex()):
            self.table.selectRow(index.row())
        clicked = self.file_at_proxy_row(index.row())
        files = self.selected_files()
        menu = QMenu(self)
        n = len(files)
        menu.addAction(f"Download {n} files" if n > 1 else "Download", self._emit_activated)
        if self._show_folder_action and clicked is not None:
            menu.addAction(
                f"Download whole folder “{clicked.folder_name or '/'}”",
                lambda: self.folder_requested.emit(clicked),
            )
        menu.addSeparator()
        if clicked is not None:
            menu.addAction(f"Show only files from {clicked.username}",
                           lambda: self.set_filter_text(f'"{clicked.username}"'))
            if clicked.folder_name:
                menu.addAction("Show only this folder",
                               lambda: self.set_filter_text(f'"{clicked.folder_name}" "{clicked.username}"'))
            menu.addAction("Copy remote path",
                           lambda: QGuiApplication.clipboard().setText(clicked.filename))
        menu.exec(self.table.viewport().mapToGlobal(pos))


# How long a search keeps listening for late peers. Most answer within
# seconds; stopping frees the network and memory.
SEARCH_SECONDS = 60
# Re-rank at most this often while results pour in.
REFRESH_MS = 1000

S_SONG, S_ALBUM, S_LENGTH, S_QUALITY, S_COPIES, S_STATUS = range(6)
_S_HEADERS = ["Song", "Album", "Length", "Quality", "Copies", "Status"]
_VERSION_ROLE = Qt.ItemDataRole.UserRole
_COPY_ROLE = Qt.ItemDataRole.UserRole + 1

_PHASE_STATUS = {
    "requesting": "Starting…",
    "processing": "Finishing…",
}


class _ResultsBridge(QObject):
    """Carries result batches from the Soulseek loop thread to the GUI."""

    results = pyqtSignal(int, list)   # (search sequence number, files)


class SearchView(QWidget):
    """The Search page of the workspace.

    Signals
    -------
    download_requested(files)
        Queue these files (TransfersView does the rest; alternatives_for()
        supplies the fallbacks).
    spotify_link_entered(link)
        The query was a Spotify link; the main window opens its review.
    """

    download_requested = pyqtSignal(list)
    spotify_link_entered = pyqtSignal(str)

    def __init__(
        self,
        settings: QSettings,
        get_client: Callable[[], SoulseekClient | None],
        is_spotify_link: Callable[[str], bool],
        parent: QWidget | None = None,
        library_paths: Callable[[], list[str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._get_client = get_client
        self._is_spotify_link = is_spotify_link
        self._library_paths = library_paths or (lambda: [])
        self._search_seq = 0
        self._client = None
        self._search_id: int | None = None
        self._query = ""
        self._index = VersionIndex("")
        self._status_by_version: dict[int, str] = {}
        self._version_by_item: dict[int, int] = {}      # transfer item id -> version id
        self._user_picked = False
        self._dirty = False
        self._threads: set = set()     # keep QThread refs alive until they finish
        self._bridge = _ResultsBridge(self)
        self._bridge.results.connect(self._on_results)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh_if_dirty)
        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(self.stop_search)
        self._build_ui()

    # -- UI -----------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.query_input = QLineEdit()
        self.query_input.setProperty("variant", "search")
        self.query_input.setPlaceholderText("Artist and title, or a Spotify link")
        self.query_input.setClearButtonEnabled(True)
        self.query_input.returnPressed.connect(self.run_search)
        self._completer = QCompleter(self._history(), self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.query_input.setCompleter(self._completer)
        row.addWidget(self.query_input, stretch=1)
        self.search_button = QPushButton("Search")
        self.search_button.setProperty("variant", "primary")
        self.search_button.clicked.connect(self.run_search)
        row.addWidget(self.search_button)
        layout.addLayout(row)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter the results (-word hides)")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(lambda _t: self._mark_dirty(soon=True))
        filters.addWidget(self.filter_input, stretch=1)
        self.lossless_check = QCheckBox("Lossless only")
        self.lossless_check.toggled.connect(lambda _c: self._mark_dirty(soon=True))
        filters.addWidget(self.lossless_check)
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        filters.addWidget(self.status_label)
        layout.addLayout(filters)

        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setFixedHeight(3)
        self.busy_bar.setVisible(False)
        layout.addWidget(self.busy_bar)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_intro())                 # 0
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(_S_HEADERS))
        self.tree.setHeaderLabels(_S_HEADERS)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(S_SONG, QHeaderView.ResizeMode.Stretch)
        for col, width in ((S_ALBUM, 200), (S_LENGTH, 60), (S_QUALITY, 86), (S_COPIES, 64), (S_STATUS, 170)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(col, width)
        self.tree.itemExpanded.connect(self._fill_copies)
        self.tree.itemActivated.connect(lambda _item, _col: self.download_selected())
        self.tree.itemPressed.connect(lambda *_a: self.on_user_picked())
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        for keys, handler in (
            ("Shift+Return", self.download_selected_album),
            ("Shift+Enter", self.download_selected_album),
        ):
            action = QAction(self.tree)
            action.setShortcut(QKeySequence(keys))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(handler)
            self.tree.addAction(action)
        self.tree.installEventFilter(self)
        self.stack.addWidget(self.tree)                            # 1
        layout.addWidget(self.stack, stretch=1)

        actions = QHBoxLayout()
        self.download_button = QPushButton("Download")
        self.download_button.setProperty("variant", "primary")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_selected)
        self.album_button = QPushButton("Download album")
        self.album_button.setEnabled(False)
        self.album_button.clicked.connect(self.download_selected_album)
        hint = QLabel("Enter downloads the best copy, Shift+Enter the whole album. Other copies are used if one fails.")
        hint.setProperty("role", "faint")
        actions.addWidget(self.download_button)
        actions.addWidget(self.album_button)
        actions.addSpacing(8)
        actions.addWidget(hint)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.tree.currentItemChanged.connect(lambda *_a: self._update_buttons())

    def _build_intro(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addStretch(2)
        title = QLabel("Find music on Soulseek")
        title.setProperty("role", "title")
        v.addWidget(title, alignment=Qt.AlignmentFlag.AlignHCenter)
        body = QLabel(
            "Type the artist and the title, then press Enter.\n"
            "Results appear as people answer, the best copy on top.\n"
            "Paste a Spotify link to import a whole album or playlist."
        )
        body.setProperty("role", "muted")
        body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(6)
        v.addWidget(body, alignment=Qt.AlignmentFlag.AlignHCenter)
        v.addStretch(3)
        return page

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 — Qt override
        # Any key or click in the results means the user is choosing: stop
        # reshuffling rows under them.
        if obj is self.tree and event.type() in (event.Type.KeyPress, event.Type.MouseButtonPress):
            self.on_user_picked()
        return super().eventFilter(obj, event)

    def focus_query(self) -> None:
        self.query_input.setFocus()
        self.query_input.selectAll()

    # -- history --------------------------------------------------------------

    def _history(self) -> list[str]:
        raw = self._settings.value(HISTORY_KEY, [])
        if isinstance(raw, str):
            raw = [raw] if raw else []
        return [str(q) for q in (raw or [])]

    def _remember(self, query: str) -> None:
        history = [q for q in self._history() if q.lower() != query.lower()]
        history.insert(0, query)
        history = history[:HISTORY_LIMIT]
        self._settings.setValue(HISTORY_KEY, history)
        self._completer.model().setStringList(history)

    # -- searching ------------------------------------------------------------

    def search_for(self, query: str) -> None:
        self.query_input.setText(query)
        self.run_search()

    def run_search(self) -> None:
        query = self.query_input.text().strip()
        if not query:
            return
        if self._is_spotify_link(query):
            self.spotify_link_entered.emit(query)
            return
        client = self._get_client()
        if client is None:
            return
        self._remember(query)
        self.stop_search()
        self._search_seq += 1
        seq = self._search_seq
        self._client = client
        self._query = query
        self._index = VersionIndex(query, owned=owned_stems(self._library_paths()))
        self._status_by_version = {}
        self._version_by_item = {}
        self._user_picked = False
        self.tree.clear()
        self.stack.setCurrentIndex(1)
        try:
            self._search_id = client.start_search(
                query, on_results=lambda files, s=seq: self._bridge.results.emit(s, files),
            )
        except Exception as exc:  # noqa: BLE001 — shown, never fatal
            self.status_label.setText(f"Search failed: {exc}")
            return
        self.status_label.setText("Searching…")
        self.busy_bar.setVisible(True)
        self._refresh_timer.start()
        self._stop_timer.start(SEARCH_SECONDS * 1000)

    def stop_search(self) -> None:
        """Stop listening for more results (they stay on screen)."""
        self._stop_timer.stop()
        if self._client is not None and self._search_id is not None:
            try:
                self._client.stop_search(self._search_id)
            except Exception:  # noqa: BLE001 — the connection may already be gone
                pass
        self._search_id = None
        self.busy_bar.setVisible(False)
        self._refresh_if_dirty()
        self._refresh_timer.stop()
        self._update_status_line(searching=False)

    def _on_results(self, seq: int, files: list) -> None:
        if seq != self._search_seq or self._search_id is None:
            return
        first = self._index.file_count == 0
        self._index.add(files)
        self._mark_dirty(soon=first)

    def _mark_dirty(self, soon: bool = False) -> None:
        self._dirty = True
        if soon:
            QTimer.singleShot(0, self._refresh_if_dirty)

    def _refresh_if_dirty(self) -> None:
        if self._dirty:
            self.refresh_now()

    def refresh_now(self) -> None:
        """Redraw the ranked versions, keeping the selection, the expanded
        rows and the scroll position."""
        self._dirty = False
        self._index.set_filter(self.filter_input.text(), self.lossless_check.isChecked())
        current = self.tree.currentItem()
        keep_id = keep_copy = None
        if current is not None:
            top = current if current.parent() is None else current.parent()
            keep_id = top.data(0, _VERSION_ROLE)
            keep_copy = current.data(0, _COPY_ROLE)
        expanded = {
            self.tree.topLevelItem(i).data(0, _VERSION_ROLE)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).isExpanded()
        }
        scroll = self.tree.verticalScrollBar().value()
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            select = None
            for version in self._index.ranked():
                item = self._version_item(version)
                self.tree.addTopLevelItem(item)
                if version.id in expanded:
                    item.setExpanded(True)
                if version.id == keep_id:
                    select = item
            if select is None and self.tree.topLevelItemCount() and not self._user_picked:
                select = self.tree.topLevelItem(0)
            if select is not None and keep_copy:
                # The user picked a particular copy: keep exactly that one.
                select.setExpanded(True)
                self._fill_copies(select)
                for i in range(select.childCount()):
                    if select.child(i).data(0, _COPY_ROLE) == keep_copy:
                        select = select.child(i)
                        break
            if select is not None:
                self.tree.setCurrentItem(select)
            self.tree.verticalScrollBar().setValue(scroll)
        finally:
            self.tree.setUpdatesEnabled(True)
        self._update_status_line(searching=self._search_id is not None)
        self._update_buttons()

    def _version_item(self, version: SongVersion) -> QTreeWidgetItem:
        copies = self._index.visible_copies(version)
        best = copies[0]
        item = QTreeWidgetItem([
            version.title,
            best.folder_name,
            _fmt_length(version.length_s),
            best.format_label,
            str(len(copies)),
            self._status_text(version),
        ])
        item.setData(0, _VERSION_ROLE, version.id)
        item.setToolTip(S_SONG, best.filename.replace("\\", "/"))
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        for col in (S_LENGTH, S_COPIES):
            item.setTextAlignment(col, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
        muted = QColor(theme.TEXT_3)
        if version.unwanted or best.quality_ok is False:
            for col in range(len(_S_HEADERS)):
                item.setForeground(col, muted)
        else:
            item.setForeground(S_ALBUM, QColor(theme.TEXT_2))
        status = self._status_text(version)
        if status == "In library":
            item.setForeground(S_STATUS, QColor(theme.OK))
        elif status.startswith("Failed"):
            item.setForeground(S_STATUS, QColor(theme.DANGER))
        return item

    def _fill_copies(self, item: QTreeWidgetItem) -> None:
        if item.parent() is not None or item.childCount():
            return
        version = self._version_by_id(item.data(0, _VERSION_ROLE))
        if version is None:
            return
        for f in self._index.visible_copies(version):
            child = QTreeWidgetItem([
                f.username, f.folder_name, _fmt_length(f.length_s), f.format_label,
                "free" if f.has_free_slot else f"queue {f.queue_length}",
                _fmt_speed(f.upload_speed),
            ])
            child.setData(0, _COPY_ROLE, (f.username, f.filename))
            child.setToolTip(S_SONG, f.filename.replace("\\", "/"))
            for col in range(len(_S_HEADERS)):
                child.setForeground(col, QColor(theme.TEXT_2))
            item.addChild(child)

    def _set_expanded(self, version_id: int, expanded: bool) -> None:
        """By id: the item the menu was opened on may have been rebuilt."""
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, _VERSION_ROLE) == version_id:
                item.setExpanded(expanded)
                return

    def _version_by_id(self, version_id) -> SongVersion | None:
        return next((v for v in self._index.ranked() if v.id == version_id), None)

    def _update_status_line(self, searching: bool) -> None:
        files, peers = self._index.file_count, self._index.peer_count
        if searching:
            text = f"Searching… {peers} people, {files} files" if files else "Searching…"
        elif files:
            text = f"{files} files from {peers} people"
        elif self._query:
            text = "Nothing found. Try fewer words, or just the title."
        else:
            text = ""
        self.status_label.setText(text)

    def on_user_picked(self) -> None:
        if not self._user_picked:
            self._user_picked = True
            self._index.freeze_order()

    def _update_buttons(self) -> None:
        has = self.tree.currentItem() is not None
        self.download_button.setEnabled(has)
        self.album_button.setEnabled(has)

    # -- selection → files ------------------------------------------------------

    def _selected(self) -> tuple[SongVersion | None, SoulseekFile | None]:
        """The selected version, and the copy to get: the one picked by hand
        (a child row) or the version's best."""
        item = self.tree.currentItem()
        if item is None:
            return None, None
        top = item if item.parent() is None else item.parent()
        version = self._version_by_id(top.data(0, _VERSION_ROLE))
        if version is None:
            return None, None
        key = item.data(0, _COPY_ROLE)
        if key:
            chosen = next((f for f in version.copies if (f.username, f.filename) == tuple(key)), None)
            if chosen is not None:
                return version, chosen
        copies = self._index.visible_copies(version)
        return version, (copies[0] if copies else None)

    def download_selected(self) -> None:
        _version, chosen = self._selected()
        if chosen is not None:
            self.download_requested.emit([chosen])

    def download_selected_album(self) -> None:
        _version, chosen = self._selected()
        if chosen is not None:
            self.download_folder(chosen)

    def alternatives_for(self, f: SoulseekFile) -> list[SoulseekFile]:
        """Other peers' copies of the same version, best first."""
        return self._index.alternatives(f)

    # -- download status on the rows ----------------------------------------------

    def note_queued(self, files: list[SoulseekFile], item_ids: list[int]) -> None:
        for f, item_id in zip(files, item_ids):
            version = self._index.version_of(f) if f is not None else None
            if version is not None:
                self._version_by_item[item_id] = version.id
                self._set_version_status(version.id, "Starting…")

    def on_transfer_status(self, item_id: int, phase: str, percent: int, text: str) -> None:
        version_id = self._version_by_item.get(item_id)
        if version_id is None:
            return
        if phase == "downloading":
            status = f"Downloading {percent}%"
        elif phase == "queued":
            place = re.search(r"\(#(\d+)\)", text or "")
            status = f"Queued by uploader (#{place.group(1)})" if place else "Queued by uploader"
        else:
            status = _PHASE_STATUS.get(phase, text or "Starting…")
        self._set_version_status(version_id, status)

    def on_transfer_finished(self, item_id: int, ok: bool, detail: str, path: str) -> None:
        version_id = self._version_by_item.pop(item_id, None)
        if version_id is None:
            return
        if ok:
            status = "In library"
        elif detail == "Cancelled.":
            status = "Cancelled"
        else:
            status = "Failed"
        self._set_version_status(version_id, status)

    def _set_version_status(self, version_id: int, status: str) -> None:
        self._status_by_version[version_id] = status
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, _VERSION_ROLE) == version_id:
                item.setText(S_STATUS, status)
                color = theme.OK if status == "In library" else theme.DANGER if status == "Failed" else theme.TEXT
                item.setForeground(S_STATUS, QColor(color))
                break

    def _status_text(self, version: SongVersion) -> str:
        if version.id in self._status_by_version:
            return self._status_by_version[version.id]
        return "In library" if version.in_library else ""

    # -- right-click -------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        self.on_user_picked()
        version, chosen = self._selected()
        if chosen is None:
            return
        menu = QMenu(self)
        is_copy = item.parent() is not None
        download = menu.addAction("Download This Copy" if is_copy else "Download",
                                  lambda f=chosen: self.download_requested.emit([f]))
        download.setShortcut(QKeySequence("Return"))
        album = menu.addAction(
            f"Download Album “{chosen.folder_name or '/'}” from {chosen.username}",
            lambda f=chosen: self.download_folder(f),
        )
        album.setShortcut(QKeySequence("Shift+Return"))
        menu.addSeparator()
        if not is_copy:
            expanded = item.isExpanded()
            menu.addAction("Hide Copies" if expanded else f"Show the {len(version.copies)} Copies",
                           lambda vid=version.id, e=expanded: self._set_expanded(vid, not e))
        menu.addAction(f"Show Only Files from {chosen.username}",
                       lambda: self.filter_input.setText(f'"{chosen.username}"'))
        menu.addAction("Copy Remote Path", lambda: QGuiApplication.clipboard().setText(chosen.filename))
        for action in menu.actions():
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # -- folders --------------------------------------------------------------

    def download_folder(self, like: SoulseekFile) -> None:
        client = self._get_client()
        if client is None:
            return
        siblings = [
            f for v in self._index.ranked() for f in v.copies
            if f.username == like.username and f.folder == like.folder
        ]
        self.status_label.setText(f"Listing “{like.folder_name}” on {like.username}…")
        thread = FolderBrowseThread(client, like)

        def on_ok(files: list) -> None:
            chosen = files or siblings
            self.status_label.setText(f"Queued {len(chosen)} files from “{like.folder_name}”.")
            self.download_requested.emit(chosen)

        def on_fail(message: str) -> None:
            self.status_label.setText(
                f"Couldn't list the whole album ({message}). Queued the {len(siblings)} "
                "files from it that were in the results."
            )
            if siblings:
                self.download_requested.emit(siblings)

        thread.results_ready.connect(on_ok)
        thread.failed.connect(on_fail)
        self._track(thread)
        thread.start()

    def _track(self, thread) -> None:
        self._threads.add(thread)
        thread.finished.connect(lambda t=thread: self._threads.discard(t))

    def shutdown(self) -> None:
        from workers import stop_thread
        self._refresh_timer.stop()
        self._stop_timer.stop()
        if self._client is not None and self._search_id is not None:
            try:
                self._client.stop_search(self._search_id)
            except Exception:  # noqa: BLE001
                pass
        self._search_id = None
        for thread in list(self._threads):
            stop_thread(thread)
