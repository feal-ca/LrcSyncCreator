"""search_view.py — Soulseek search, Nicotine+-style.

Search for a title (or an artist, or an album), then narrow the results
by typing in the filter box: it matches the whole remote path plus the
uploader, so "title, then filter by artist" (or the reverse) is two
quick steps. Results sort by any column; double-click or Enter queues a
download; "Download folder" grabs a whole album from that peer.

``SearchResultsWidget`` (filter bar + table) is shared with the Spotify
review's "Change match" dialog.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import (
    QAbstractTableModel, QModelIndex, QSettings, QSortFilterProxyModel, Qt,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QGuiApplication, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QProgressBar, QPushButton,
    QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

import theme
from soulseek import ResultFilter, SoulseekClient, SoulseekFile
from workers import FolderBrowseThread, SoulseekLiveSearchThread

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


class SearchView(QWidget):
    """The Search page of the workspace.

    Signals
    -------
    download_requested(files)
        Queue these files (TransfersView does the rest).
    spotify_link_entered(link)
        The query was a Spotify link; the main window routes it to the
        playlist review page instead of searching Soulseek for it.
    """

    download_requested = pyqtSignal(list)
    spotify_link_entered = pyqtSignal(str)

    def __init__(
        self,
        settings: QSettings,
        get_client: Callable[[], SoulseekClient | None],
        is_spotify_link: Callable[[str], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._get_client = get_client
        self._is_spotify_link = is_spotify_link
        self._search_seq = 0
        self._active_search: SoulseekLiveSearchThread | None = None
        self._threads: set = set()     # keep QThread refs alive until they finish
        self._elapsed = 0
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
        self.query_input.setPlaceholderText("Search Soulseek for a song, artist or album, or paste a Spotify link")
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

        self.stop_button = QPushButton("Show results now")
        self.stop_button.setToolTip("Stop waiting for more peers and show what has arrived")
        self.stop_button.clicked.connect(self.stop_search)
        self.stop_button.setVisible(False)
        row.addWidget(self.stop_button)
        layout.addLayout(row)

        status_row = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        status_row.addWidget(self.status_label, stretch=1)
        self.count_label = QLabel("")
        self.count_label.setProperty("role", "muted")
        status_row.addWidget(self.count_label)
        layout.addLayout(status_row)

        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setFixedHeight(3)
        self.busy_bar.setVisible(False)
        layout.addWidget(self.busy_bar)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_intro())                 # 0
        self.results = SearchResultsWidget()
        self.results.activated.connect(self.download_requested.emit)
        self.results.folder_requested.connect(self.download_folder)
        self.results.selection_changed.connect(self._on_selection_changed)
        self.results.proxy.layoutChanged.connect(self._update_counts)
        self.results.proxy.modelReset.connect(self._update_counts)
        self.results.proxy.rowsInserted.connect(self._update_counts)
        self.results.proxy.rowsRemoved.connect(self._update_counts)
        self.stack.addWidget(self.results)                         # 1
        layout.addWidget(self.stack, stretch=1)

        actions = QHBoxLayout()
        self.download_button = QPushButton("Download")
        self.download_button.setProperty("variant", "primary")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(lambda: self.results._emit_activated())
        self.folder_button = QPushButton("Download folder")
        self.folder_button.setToolTip("Every audio file in the selected result's folder on that peer (the whole album)")
        self.folder_button.setEnabled(False)
        self.folder_button.clicked.connect(self._download_selected_folder)
        hint = QLabel("Double-click or Enter to download. Right-click for more.")
        hint.setProperty("role", "faint")
        actions.addWidget(self.download_button)
        actions.addWidget(self.folder_button)
        actions.addSpacing(8)
        actions.addWidget(hint)
        actions.addStretch(1)
        layout.addLayout(actions)

    def _build_intro(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addStretch(2)
        title = QLabel("Find music on Soulseek")
        title.setProperty("role", "title")
        v.addWidget(title, alignment=Qt.AlignmentFlag.AlignHCenter)
        body = QLabel(
            "Search by title, then type the artist in the filter box to keep the\n"
            "right copies (or search the artist and filter by title).\n"
            "Paste a Spotify track, album or playlist link to import all of it."
        )
        body.setProperty("role", "muted")
        body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(6)
        v.addWidget(body, alignment=Qt.AlignmentFlag.AlignHCenter)
        v.addStretch(3)
        return page

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
        self.stop_search(silent=True)
        self._search_seq += 1
        seq = self._search_seq

        thread = SoulseekLiveSearchThread(client, query)
        thread.progress.connect(lambda peers, files, s=seq: self._on_progress(s, peers, files))
        thread.results_ready.connect(lambda results, s=seq: self._on_results(s, results))
        thread.search_failed.connect(lambda msg, s=seq: self._on_failed(s, msg))
        self._track(thread)
        self._active_search = thread

        self._elapsed = 0
        self.results.set_results([])
        self.stack.setCurrentIndex(1)
        self.status_label.setText(f"Searching for “{query}”…")
        self.count_label.setText("")
        self.busy_bar.setVisible(True)
        self.stop_button.setVisible(True)
        thread.start()

    def stop_search(self, silent: bool = False) -> None:
        if self._active_search is not None and self._active_search.isRunning():
            self._active_search.request_stop()
            if not silent:
                self.status_label.setText("Collecting results…")
        self.stop_button.setVisible(False)

    def _track(self, thread) -> None:
        self._threads.add(thread)
        thread.finished.connect(lambda t=thread: self._threads.discard(t))

    def _on_progress(self, seq: int, peers: int, files: int) -> None:
        if seq != self._search_seq or self._active_search is None:
            return
        self.status_label.setText(
            f"Searching for “{self._active_search.query}”: {peers} peers have answered with {files} files so far"
        )

    def _on_results(self, seq: int, results: list) -> None:
        if seq != self._search_seq:
            return
        self.busy_bar.setVisible(False)
        self.stop_button.setVisible(False)
        query = self._active_search.query if self._active_search else ""
        self._active_search = None
        self.results.set_results(results)
        peers = len({f.username for f in results})
        if results:
            self.status_label.setText(f"{len(results)} audio files from {peers} peers for “{query}”")
            self.results.filter_input.setFocus()
        else:
            self.status_label.setText(
                f"No audio files for “{query}”. Try fewer words, or just the title."
            )
        self._update_counts()

    def _on_failed(self, seq: int, message: str) -> None:
        if seq != self._search_seq:
            return
        self.busy_bar.setVisible(False)
        self.stop_button.setVisible(False)
        self._active_search = None
        self.status_label.setText(f"Search failed: {message}")

    def _update_counts(self, *_args) -> None:
        total = self.results.total_count()
        shown = self.results.visible_count()
        if total and shown != total:
            self.count_label.setText(f"Showing {shown} of {total}")
        else:
            self.count_label.setText("")

    def _on_selection_changed(self, n: int) -> None:
        self.download_button.setEnabled(n > 0)
        self.download_button.setText(f"Download {n} files" if n > 1 else "Download")
        self.folder_button.setEnabled(n > 0)

    def alternatives_for(self, f: SoulseekFile) -> list[SoulseekFile]:
        """Other peers' copies of *f* in the current results (same file
        name, size within 15%), best first — fallbacks if its peer never
        answers or rejects the request."""
        from soulseek import availability_key, normalize_text

        stem = normalize_text(f.display_name.rsplit(".", 1)[0])
        same = [
            o for o in self.results.model.files
            if o is not f and o.username != f.username
            and normalize_text(o.display_name.rsplit(".", 1)[0]) == stem
            and f.size and abs(o.size - f.size) <= 0.15 * f.size
        ]
        return sorted(same, key=availability_key)

    # -- folders --------------------------------------------------------------

    def _download_selected_folder(self) -> None:
        files = self.results.selected_files()
        if files:
            self.download_folder(files[0])

    def download_folder(self, like: SoulseekFile) -> None:
        client = self._get_client()
        if client is None:
            return
        siblings = [
            f for f in self.results.model.files
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
                f"Couldn't list the whole folder ({message}). Queued the {len(siblings)} "
                "files from it that were in the results."
            )
            if siblings:
                self.download_requested.emit(siblings)

        thread.results_ready.connect(on_ok)
        thread.failed.connect(on_fail)
        self._track(thread)
        thread.start()

    def shutdown(self) -> None:
        from workers import stop_thread
        for thread in list(self._threads):
            stop_thread(thread)
