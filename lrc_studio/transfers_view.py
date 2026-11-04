"""transfers_view.py — the Downloads page and the one download engine
behind it: files picked on the Search page and tracks from a Spotify
import both end up here, with live progress, cancel, retry and "try
another source".

Each item runs in its own ``TransferThread`` (request, follow, fall back
to another source, finalize). At most ``MAX_ACTIVE`` run at once; the
rest wait locally so an album from one slow peer doesn't spawn twenty
polling threads.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial
from typing import Callable

from PyQt6.QtCore import QModelIndex, QRect, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QKeySequence, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton,
    QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import theme
from postprocess import downloads_dir_from_settings, finalize_download, options_from_settings
from soulseek import SoulseekClient, SoulseekFile
from workers import TransferThread, stop_thread

MAX_ACTIVE = 6

COL_NAME, COL_PROGRESS = range(2)
_PERCENT_ROLE = Qt.ItemDataRole.UserRole + 1

_PHASE_TEXT = {
    "waiting": "Waiting",
    "requesting": "Starting…",
    "queued": "Queued by uploader",
    "downloading": "Downloading",
    "processing": "Finishing…",
    "done": "In library",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
_FINISHED = ("done", "failed", "cancelled")


@dataclass
class TransferItem:
    id: int
    file: SoulseekFile                       # the source being tried now
    phase: str = "waiting"
    percent: int = 0
    text: str = "Waiting"
    final_path: str = ""
    candidates: list = field(default_factory=list)   # file first, then alternatives
    queue_patience_s: float | None = None


class _ProgressDelegate(QStyledItemDelegate):
    """Status text with a thin accent-colored progress line under it."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        percent = index.data(_PERCENT_ROLE)
        if percent is None or percent < 0:
            return
        r = option.rect
        track = QRect(r.left() + 8, r.bottom() - 6, r.width() - 16, 3)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.RAISED))
        painter.drawRoundedRect(track, 1.5, 1.5)
        if percent > 0:
            fill = QRect(track.left(), track.top(), int(track.width() * min(percent, 100) / 100), 3)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(fill, 1.5, 1.5)
        painter.restore()


class TransfersView(QWidget):
    """Signals
    -------
    file_ready(path)          a download finished and is in the library
    active_count_changed(n)   unfinished items, for the tab badge
    open_requested(path)      the user double-clicked a finished item
    item_changed(id, phase, percent, text)     live status of one item
    item_finished(id, ok, detail, path)
    item_source_changed(id, file)              fell back to another source
    """

    file_ready = pyqtSignal(str)
    active_count_changed = pyqtSignal(int)
    open_requested = pyqtSignal(str)
    item_changed = pyqtSignal(int, str, int, str)
    item_finished = pyqtSignal(int, bool, str, str)
    item_source_changed = pyqtSignal(int, object)

    def __init__(
        self,
        settings: QSettings,
        get_client: Callable[[], SoulseekClient | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._get_client = get_client
        self._items: list[TransferItem] = []
        self._threads: dict[int, TransferThread] = {}
        self._next_id = 1
        self._client: SoulseekClient | None = None
        self._build_ui()

    # -- UI -----------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)

        self.summary = QLabel("Nothing downloading. Find music on the Search page (Ctrl+F).")
        self.summary.setProperty("role", "muted")
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Song", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_PROGRESS, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(COL_PROGRESS, 380)
        self.table.setItemDelegateForColumn(COL_PROGRESS, _ProgressDelegate(self.table))
        self.table.itemDoubleClicked.connect(self._on_double_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        delete = QAction(self.table)
        delete.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        delete.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        delete.triggered.connect(self.cancel_selected)
        self.table.addAction(delete)
        layout.addWidget(self.table, stretch=1)

        row = QHBoxLayout()
        self.retry_button = QPushButton("Retry failed")
        self.retry_button.clicked.connect(self.retry_failed)
        self.clear_button = QPushButton("Clear finished")
        self.clear_button.setProperty("variant", "ghost")
        self.clear_button.clicked.connect(self.clear_finished)
        row.addWidget(self.retry_button)
        hint = QLabel("Double-click a finished song to open it. Del cancels, right-click for more.")
        hint.setProperty("role", "faint")
        row.addSpacing(8)
        row.addWidget(hint)
        row.addStretch(1)
        row.addWidget(self.clear_button)
        layout.addLayout(row)
        self._update_buttons()

    # -- public ---------------------------------------------------------------

    def enqueue(
        self,
        files: list[SoulseekFile],
        alternatives: Callable[[SoulseekFile], list[SoulseekFile]] | None = None,
        queue_patience_s: float | None = None,
    ) -> list[int]:
        """Queue *files*; returns the new items' ids (a file that's already
        queued or downloading isn't added twice).

        *alternatives(file)* gives other peers' copies to fall back on.
        *queue_patience_s* is how long to wait in a peer's queue before
        trying the next copy; None waits indefinitely.
        """
        pending = {
            (i.file.username, i.file.filename) for i in self._items if i.phase not in _FINISHED
        }
        ids: list[int] = []
        for f in files:
            key = (f.username, f.filename)
            if key in pending:
                continue
            pending.add(key)
            others = [a for a in (alternatives(f) if alternatives else []) if a is not f]
            item = TransferItem(
                id=self._next_id, file=f, candidates=[f] + others,
                # With no other copy to move on to, waiting is the only option.
                queue_patience_s=queue_patience_s if others else None,
            )
            self._next_id += 1
            self._items.append(item)
            self._append_row(item)
            ids.append(item.id)
        if ids:
            self._pump()
        self._refresh_summary()
        return ids

    def cancel_items(self, item_ids: list[int]) -> None:
        for item_id in item_ids:
            item = self._find(item_id)
            if item is not None:
                self._cancel(item)

    def skip_source(self, item_id: int) -> None:
        """Abandon the current source of *item_id* and try the next one."""
        thread = self._threads.get(item_id)
        item = self._find(item_id)
        if thread is not None and item is not None and len(item.candidates) > 1:
            thread.request_skip()
            self._set_phase(item, item.phase, item.percent, "Switching to another source…")

    def file_of(self, item_id: int) -> SoulseekFile | None:
        item = self._find(item_id)
        return item.file if item is not None else None

    def active_count(self) -> int:
        return sum(1 for i in self._items if i.phase not in _FINISHED)

    def shutdown(self) -> None:
        for thread in list(self._threads.values()):
            stop_thread(thread)

    # -- scheduling -----------------------------------------------------------

    def _pump(self) -> None:
        waiting = [i for i in self._items if i.phase == "waiting" and i.id not in self._threads]
        free = MAX_ACTIVE - len(self._threads)
        if not waiting or free <= 0:
            return
        # Ask every time: the window may have replaced the client (a new
        # account in Settings) and the old one is closed. Cheap when online.
        self._client = self._get_client()
        if self._client is None:
            for item in waiting:
                self._set_phase(item, "failed", -1, "Not connected to Soulseek")
            return

        downloads_dir = downloads_dir_from_settings(self._settings)
        options = options_from_settings(self._settings)
        finalize = partial(finalize_download, options=options)
        for item in waiting[:free]:
            thread = TransferThread(
                item.id, self._client, downloads_dir, item.candidates or [item.file], finalize,
                convert_lossless=options.convert_lossless,
                queue_patience_s=item.queue_patience_s,
            )
            thread.status.connect(self._on_status)
            thread.source_changed.connect(self._on_source_changed)
            thread.finished_transfer.connect(self._on_finished)
            thread.finished.connect(lambda tid=item.id: self._on_thread_exit(tid))
            self._threads[item.id] = thread
            self._set_phase(item, "requesting", 0, "Requesting…")
            thread.start()

    def _on_thread_exit(self, item_id: int) -> None:
        self._threads.pop(item_id, None)
        self._pump()
        self._refresh_summary()

    def _on_status(self, item_id: int, phase: str, percent: int, text: str) -> None:
        item = self._find(item_id)
        if item is not None and item.phase not in _FINISHED:
            self._set_phase(item, phase, percent, text)

    def _on_source_changed(self, item_id: int, file: SoulseekFile) -> None:
        item = self._find(item_id)
        if item is None:
            return
        item.file = file
        row = self._row_for(item_id)
        if row is not None:
            self._label_name(self.table.item(row, COL_NAME), file)
        self.item_source_changed.emit(item_id, file)

    def _on_finished(self, item_id: int, ok: bool, detail: str, path: str) -> None:
        item = self._find(item_id)
        if item is None:
            return
        if ok:
            item.final_path = path
            self._set_phase(item, "done", -1, "In library")
            self.file_ready.emit(path)
        elif detail == "Cancelled.":
            self._set_phase(item, "cancelled", -1, "Cancelled")
        else:
            self._set_phase(item, "failed", -1, detail)
        self.item_finished.emit(item_id, ok, detail, path)
        self._refresh_summary()

    # -- actions --------------------------------------------------------------

    def retry_failed(self, items: list[TransferItem] | None = None) -> None:
        targets = items if items is not None else [
            i for i in self._items if i.phase in ("failed", "cancelled")
        ]
        for item in targets:
            if item.phase in ("failed", "cancelled"):
                self._set_phase(item, "waiting", 0, "Waiting")
        self._client = None   # reconnect on retry
        self._pump()
        self._refresh_summary()

    def cancel_selected(self) -> None:
        for item in self._selected_items():
            self._cancel(item)

    def _cancel(self, item: TransferItem) -> None:
        if item.phase in _FINISHED:
            return
        thread = self._threads.get(item.id)
        if thread is not None:
            thread.request_cancel()
            self._set_phase(item, item.phase, item.percent, "Cancelling…")
        else:
            self._set_phase(item, "cancelled", -1, "Cancelled")
        self._refresh_summary()

    def clear_finished(self) -> None:
        keep = [i for i in self._items if i.phase not in _FINISHED or i.id in self._threads]
        self._items = keep
        self.table.setRowCount(0)
        for item in self._items:
            self._append_row(item)
        self._refresh_summary()

    def _context_menu(self, pos) -> None:
        items = self._selected_items()
        if not items:
            return
        menu = QMenu(self)
        running = [i for i in items if i.id in self._threads and len(i.candidates) > 1]
        done = [i for i in items if i.phase == "done" and i.final_path]
        if len(done) == 1:
            menu.addAction("Open in Editor", lambda: self.open_requested.emit(done[0].final_path))
        if len(running) == 1:
            menu.addAction("Try Another Copy", lambda: self.skip_source(running[0].id))
        if any(i.phase in ("failed", "cancelled") for i in items):
            menu.addAction("Retry", lambda: self.retry_failed(items))
        if any(i.phase not in _FINISHED for i in items):
            cancel = menu.addAction("Cancel", self.cancel_selected)
            cancel.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
            cancel.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _on_double_clicked(self, cell: QTableWidgetItem) -> None:
        item = self._item_for_row(cell.row())
        if item is not None and item.phase == "done" and item.final_path:
            self.open_requested.emit(item.final_path)

    # -- rendering ------------------------------------------------------------

    def _append_row(self, item: TransferItem) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        name = QTableWidgetItem()
        name.setData(Qt.ItemDataRole.UserRole, item.id)
        self._label_name(name, item.file)
        self.table.setItem(row, COL_NAME, name)
        self.table.setItem(row, COL_PROGRESS, QTableWidgetItem())
        self._render(row, item)

    @staticmethod
    def _label_name(cell: QTableWidgetItem, file: SoulseekFile) -> None:
        cell.setText(os.path.splitext(file.display_name)[0])
        cell.setToolTip(f"From {file.username}\n{file.filename.replace(chr(92), '/')}")

    def _set_phase(self, item: TransferItem, phase: str, percent: int, text: str) -> None:
        item.phase, item.percent, item.text = phase, percent, text
        row = self._row_for(item.id)
        if row is not None:
            self._render(row, item)
        self._update_buttons()
        if phase not in _FINISHED:
            self.item_changed.emit(item.id, phase, percent, text)

    def _render(self, row: int, item: TransferItem) -> None:
        cell = self.table.item(row, COL_PROGRESS)
        if item.phase == "waiting":
            label = _PHASE_TEXT["waiting"]
        elif item.phase == "failed":
            label = (item.text.splitlines() or [""])[0]
        else:
            label = item.text or _PHASE_TEXT.get(item.phase, item.phase)
        cell.setText(label)
        cell.setToolTip(item.text)
        show_bar = item.phase in ("downloading", "processing", "queued", "requesting")
        cell.setData(_PERCENT_ROLE, item.percent if show_bar else -1)
        color = {
            "done": theme.OK, "failed": theme.DANGER, "cancelled": theme.TEXT_3,
            "waiting": theme.TEXT_2, "queued": theme.TEXT_2,
        }.get(item.phase, theme.TEXT)
        cell.setForeground(QColor(color))

    def _refresh_summary(self) -> None:
        active = self.active_count()
        done = sum(1 for i in self._items if i.phase == "done")
        failed = sum(1 for i in self._items if i.phase == "failed")
        if not self._items:
            self.summary.setText("Nothing downloading. Find music on the Search page (Ctrl+F).")
        else:
            parts = []
            if active:
                parts.append(f"{active} in progress")
            if done:
                parts.append(f"{done} finished")
            if failed:
                parts.append(f"{failed} failed")
            self.summary.setText(", ".join(parts).capitalize() + "." if parts else "All clear.")
        self.active_count_changed.emit(active)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.retry_button.setEnabled(any(i.phase in ("failed", "cancelled") for i in self._items))
        self.clear_button.setEnabled(any(i.phase in _FINISHED for i in self._items))

    # -- lookup ---------------------------------------------------------------

    def _find(self, item_id: int) -> TransferItem | None:
        return next((i for i in self._items if i.id == item_id), None)

    def _row_for(self, item_id: int) -> int | None:
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, COL_NAME)
            if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == item_id:
                return row
        return None

    def _item_for_row(self, row: int) -> TransferItem | None:
        cell = self.table.item(row, COL_NAME)
        return self._find(cell.data(Qt.ItemDataRole.UserRole)) if cell is not None else None

    def _selected_items(self) -> list[TransferItem]:
        rows = {i.row() for i in self.table.selectionModel().selectedRows()} if self.table.selectionModel() else set()
        return [it for r in sorted(rows) if (it := self._item_for_row(r)) is not None]
