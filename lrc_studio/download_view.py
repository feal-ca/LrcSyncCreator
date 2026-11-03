"""download_view.py — ``DownloadView``: the Playlist page.

Paste a Spotify track, album, or playlist link: everything missing from
the library is matched on Soulseek (a few searches at a time, best match
shown, nothing downloaded yet), the user reviews or overrides matches,
then commits with "Download All Missing" or a row's "Download".

Downloads run in the shared engine (``TransfersView``), several at once,
falling back to a row's other candidates when a peer rejects, never
answers or sits in its queue too long. Each row mirrors its transfer's
live status. Once "Download All Missing" is clicked, tracks still being
searched start downloading as soon as they're matched.
"""

from __future__ import annotations


from dataclasses import dataclass, field
from functools import partial
from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from constants import (
    COLOR_ACCENT, COLOR_DANGER, COLOR_FG, COLOR_MUTED, COLOR_SYNCED,
    COLOR_UNSYNCED,
)
from dialogs import SoulseekSearchDialog
from soulseek import SlskdClient, SoulseekFile, clean_query_text
from workers import (
    PlaylistFetchThread, PlaylistSearchThread, SoulseekSearchThread, stop_thread,
)

# How long a playlist track waits in one peer's upload queue before the
# next candidate is tried. Only applies when there is another candidate.
QUEUE_PATIENCE_S = 180.0

_STATUS_LABELS = {
    "in_library":      "In Library",
    "queued":          "Queued",
    "searching":       "Searching…",
    "matched":         "Matched",
    "no_results":      "No Results",
    "search_failed":   "Search Failed",
    "download_queued": "Waiting to start",
    "downloading":     "Downloading…",
    "downloaded":      "Downloaded",
    "download_failed": "Failed",
}

_STATUS_COLORS = {
    "in_library":      COLOR_MUTED,
    "queued":          COLOR_MUTED,
    "searching":       COLOR_ACCENT,
    "matched":         COLOR_ACCENT,
    "no_results":      COLOR_UNSYNCED,
    "search_failed":   COLOR_DANGER,
    "download_queued": COLOR_MUTED,
    "downloading":     COLOR_ACCENT,
    "downloaded":      COLOR_SYNCED,
    "download_failed": COLOR_DANGER,
}

# Statuses where nothing is actionable yet — no row buttons are shown.
_NO_ACTION_STATUSES = ("in_library", "queued", "searching", "download_queued", "downloading")


@dataclass
class _RowState:
    track: object                      # SpotifyTrack
    status: str = "queued"
    candidates: list = field(default_factory=list)   # list[SoulseekFile]
    selected: SoulseekFile | None = None
    tried_peers: set = field(default_factory=set)   # usernames already tried for this row
    local_path: str | None = None
    detail: str = ""
    live: str = ""                       # live search/transfer status text
    item_id: int | None = None           # TransfersView item while downloading


class DownloadView(QWidget):
    """Fetch a playlist, auto-match on Soulseek, review, download.

    Signals
    -------
    files_ready(paths)
        Emitted once a download batch finishes with at least one new file
        — the main window hooks this to add the files to the library and
        auto-fetch lyrics for them.
    open_in_library(path)
        Emitted when the user double-clicks a "Downloaded" row — the main
        window hooks this to switch to the Library tab with that song
        loaded, so syncing lyrics is one click away from downloading.
    """

    files_ready     = pyqtSignal(list)   # list[str]
    open_in_library = pyqtSignal(str)
    file_downloaded = pyqtSignal(str)    # emitted per completed row, for live library updates

    def __init__(
        self,
        settings: QSettings,
        get_client: Callable[[], SlskdClient | None],
        collect_library_paths: Callable[[], list[str]],
        parent: QWidget | None = None,
        transfers=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._get_client = get_client
        self._collect_library_paths = collect_library_paths

        self._rows: list[_RowState] = []
        self._newly_downloaded: list[str] = []
        self._item_rows: dict[int, int] = {}   # unfinished TransfersView item id -> row
        self._auto_download = False            # "Download All Missing" was clicked
        self._batch_total = 0
        self._batch_done = 0

        self._fetch_thread: PlaylistFetchThread | None = None
        self._search_thread: PlaylistSearchThread | None = None
        self._adhoc_search_thread: SoulseekSearchThread | None = None

        if transfers is None:   # standalone use (tests): a private engine
            from transfers_view import TransfersView
            transfers = TransfersView(settings, get_client, self)
            transfers.hide()
        self._transfers = transfers
        transfers.item_changed.connect(self._on_item_changed)
        transfers.item_source_changed.connect(self._on_item_source_changed)
        transfers.item_finished.connect(self._on_item_finished)

        self._build_ui()

    def shutdown(self) -> None:
        """Stop any in-flight threads. Call from the main window's
        closeEvent so closing mid-fetch/search/download doesn't crash."""
        stop_thread(self._fetch_thread)
        stop_thread(self._search_thread)
        stop_thread(self._adhoc_search_thread)

    def is_batch_active(self) -> bool:
        """True while a fetch, search, or download is in flight — callers
        (e.g. re-entering Find Music) should not restart a fetch or clear
        the table/rows while this is true; the running thread would still
        be delivering results into rows that no longer exist."""
        return bool(self._item_rows) or any(
            thread is not None and thread.isRunning()
            for thread in (self._fetch_thread, self._search_thread)
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)

        link_row = QHBoxLayout()
        self.link_input = QLineEdit()
        self.link_input.setProperty("variant", "search")
        self.link_input.setPlaceholderText("Paste a Spotify track, album or playlist link")
        self.link_input.setClearButtonEnabled(True)
        self.link_input.returnPressed.connect(self._on_fetch_clicked)
        self.fetch_button = QPushButton("Import")
        self.fetch_button.clicked.connect(self._on_fetch_clicked)
        link_row.addWidget(self.link_input, stretch=1)
        link_row.addWidget(self.fetch_button)
        layout.addLayout(link_row)

        self.status_label = QLabel(
            "Paste a Spotify track, album, or playlist link and click Fetch. "
            "Every track not already in your library gets auto-matched on "
            "Soulseek for you to review before anything downloads."
        )
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("role", "muted")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Track", "Status", "Match", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 300)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(2, 260)
        # Fixed: row buttons change as a row moves through its states, and
        # sizing to the first rows' buttons clipped longer ones further down.
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(3, 236)
        self.table.itemDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table, stretch=1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        bottom_row = QHBoxLayout()
        self.download_button = QPushButton("Download All Missing")
        self.download_button.setToolTip(
            "Download every matched track now, and every track still being "
            "searched as soon as it's matched"
        )
        self.download_button.clicked.connect(self._download_all)
        self.download_button.setEnabled(False)
        self.download_button.setProperty("variant", "primary")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_current)
        bottom_row.addWidget(self.download_button)
        bottom_row.addWidget(self.cancel_button)
        bottom_row.addStretch(1)
        layout.addLayout(bottom_row)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def start_fetch(self, link: str) -> None:
        """Programmatic equivalent of pasting *link* and clicking Fetch —
        used by the unified Find Music entry point in app.py."""
        self.link_input.setText(link)
        self._on_fetch_clicked()

    def _on_fetch_clicked(self) -> None:
        link = self.link_input.text().strip()
        if not link:
            return
        if self._fetch_thread is not None and self._fetch_thread.isRunning():
            return
        client = self._get_client()
        if client is None:
            return

        self.fetch_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.status_label.setText("Fetching from Spotify…")
        self.table.setRowCount(0)
        self._rows = []
        self._item_rows = {}
        self._auto_download = False
        self._batch_total = self._batch_done = 0
        self._refresh_download_button()

        library_paths = self._collect_library_paths()
        self._fetch_thread = PlaylistFetchThread(link, library_paths)
        self._fetch_thread.fetched.connect(
            lambda tracks, missing, truncated: self._on_fetched(client, tracks, missing, truncated)
        )
        self._fetch_thread.fetch_failed.connect(self._on_fetch_failed)
        self._fetch_thread.start()

    def _on_fetch_failed(self, message: str) -> None:
        self.fetch_button.setEnabled(True)
        self.status_label.setText(f"Couldn't fetch from Spotify: {message}")

    def _on_fetched(self, client: SlskdClient, tracks: list, missing: list, truncated: bool) -> None:
        self.fetch_button.setEnabled(True)
        missing_set = set(missing)

        self._rows = [
            _RowState(track=t, status=("queued" if t in missing_set else "in_library"))
            for t in tracks
        ]
        self.table.setRowCount(len(tracks))
        for i, track in enumerate(tracks):
            track_item = QTableWidgetItem(track.query)
            track_item.setFlags(track_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(i, 0, track_item)
            for col in (1, 2):
                item = QTableWidgetItem("")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, col, item)
            self._update_row_display(i)

        n_missing = len(missing)
        note = (
            " Note: this has 50+ tracks — Spotify's public embed page only "
            "exposes the first 50 without an API login."
            if truncated else ""
        )
        self.status_label.setText(
            f"{len(tracks)} track(s) found, {len(tracks) - n_missing} "
            f"already in your library, {n_missing} to search on Soulseek."
            f"{note}"
        )

        missing_rows = [(i, r.track) for i, r in enumerate(self._rows) if r.status == "queued"]
        if missing_rows:
            self._start_search(client, missing_rows)

    # ------------------------------------------------------------------
    # Search phase (auto-match, no download yet)
    # ------------------------------------------------------------------

    def _start_search(self, client: SlskdClient, rows: list[tuple[int, object]]) -> None:
        if self._search_thread is not None and self._search_thread.isRunning():
            QMessageBox.information(
                self, "Search In Progress",
                "A Soulseek search is already running — wait for it to finish.",
            )
            return

        # Left as "queued" (not "searching") here — the thread processes one
        # row at a time and emits track_searching for whichever is actually
        # active, so the table shows real progress moving down the list
        # instead of every row appearing to freeze on "Searching…" at once.
        for row_index, _ in rows:
            self._rows[row_index].status = "queued"
            self._update_row_display(row_index)

        self._search_thread = PlaylistSearchThread(client, rows)
        self._search_thread.progress.connect(self.status_label.setText)
        self._search_thread.track_searching.connect(self._on_track_searching)
        self._search_thread.track_peers.connect(self._on_track_peers)
        self._search_thread.track_matched.connect(self._on_track_matched)
        self._search_thread.track_failed.connect(self._on_track_failed)
        self._search_thread.finished_search.connect(self._on_search_finished)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(True)
        self._search_thread.start()

    def _on_track_searching(self, row_index: int) -> None:
        self._rows[row_index].status = "searching"
        self._rows[row_index].live = ""
        self._update_row_display(row_index)

    def _on_track_peers(self, row_index: int, peers: int, files: int) -> None:
        state = self._rows[row_index]
        if state.status == "searching":
            state.live = f"{peers} peers answered" if peers else "waiting for peers"
            self._update_row_display(row_index, actions=False)

    def _on_track_matched(self, row_index: int, candidates: list) -> None:
        state = self._rows[row_index]
        state.candidates = candidates
        state.selected = candidates[0]
        state.tried_peers = set()
        state.status = "matched"
        state.live = ""
        self._update_row_display(row_index)
        if self._auto_download:
            self._enqueue_rows([row_index])
        self._refresh_download_button()

    def _on_track_failed(self, row_index: int, message: str) -> None:
        state = self._rows[row_index]
        state.status = "no_results" if message == "No Soulseek results." else "search_failed"
        state.detail = message
        state.live = ""
        self._update_row_display(row_index)

    def _on_search_finished(self) -> None:
        self._search_thread = None
        self.cancel_button.setVisible(bool(self._item_rows))
        matched = sum(1 for r in self._rows if r.selected)
        missing = sum(1 for r in self._rows if r.status in ("no_results", "search_failed"))
        if self._auto_download:
            self.status_label.setText(
                f"Search done: {matched} matched, {missing} not found. Downloads keep going below."
            )
        else:
            self.status_label.setText(
                f"Search done: {matched} matched, {missing} not found. Check the Match "
                "column, then click Download All Missing."
            )
        self._refresh_download_button()
        self._maybe_finish_batch()

    def _retry_search(self, row_index: int) -> None:
        client = self._get_client()
        if client is None:
            return
        self._start_search(client, [(row_index, self._rows[row_index].track)])

    def _choose_match(self, row_index: int) -> None:
        state = self._rows[row_index]
        # Search the title alone and prefill the filter with the artist:
        # Soulseek's search does better on a narrower query, and the
        # filter then keeps the right artist's copies, same as by hand.
        dialog = SoulseekSearchDialog(
            clean_query_text(state.track.title) or state.track.title, self,
            filter_text=clean_query_text(state.track.artist.split(",")[0]),
        )
        dialog.show_results(state.candidates)

        def run_search() -> None:
            client = self._get_client()
            if client is None:
                return
            dialog.set_searching(True)
            self._adhoc_search_thread = SoulseekSearchThread(client, dialog.query_input.text().strip())
            self._adhoc_search_thread.results_ready.connect(
                lambda results: (dialog.set_searching(False), dialog.show_results(results))
            )
            self._adhoc_search_thread.search_failed.connect(
                lambda msg: (dialog.set_searching(False), dialog.show_error(msg))
            )
            self._adhoc_search_thread.start()

        dialog.search_button.clicked.connect(run_search)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_file:
            state.selected = dialog.selected_file
            if dialog.selected_file not in state.candidates:
                state.candidates.append(dialog.selected_file)
            if state.status not in ("download_queued", "downloading"):
                state.status = "matched"
            self._update_row_display(row_index)
            self._refresh_download_button()

    # ------------------------------------------------------------------
    # Download phase (explicit commit)
    # ------------------------------------------------------------------

    def _downloadable(self) -> list[int]:
        return [
            i for i, r in enumerate(self._rows)
            if r.selected and r.status in ("matched", "download_failed")
        ]

    def _refresh_download_button(self) -> None:
        n = len(self._downloadable())
        searching = self._search_thread is not None and self._search_thread.isRunning()
        if n:
            self.download_button.setText(f"Download All Missing ({n})")
        elif self._auto_download and searching:
            self.download_button.setText("New matches download automatically")
        else:
            self.download_button.setText("Download All Missing")
        self.download_button.setEnabled(n > 0 or (searching and not self._auto_download and bool(self._rows)))

    def _download_all(self) -> None:
        self._auto_download = True
        rows = self._downloadable()
        searching = self._search_thread is not None and self._search_thread.isRunning()
        if not rows and not searching:
            QMessageBox.information(self, "Nothing To Download", "No matched tracks are ready to download.")
            return
        self._enqueue_rows(rows)
        self._refresh_download_button()

    def _download_row(self, row_index: int) -> None:
        if self._rows[row_index].selected:
            self._enqueue_rows([row_index])
            self._refresh_download_button()

    @staticmethod
    def _download_order(state: _RowState) -> list[SoulseekFile]:
        """The chosen match first, then the row's other candidates as
        automatic fallbacks if it fails — peers that already failed this
        row go last."""
        others = [c for c in state.candidates if c is not state.selected]
        others.sort(key=lambda c: c.username in state.tried_peers)
        return [state.selected] + others

    def _enqueue_rows(self, row_indices: list[int]) -> None:
        if row_indices and self._get_client() is None:
            return
        for row_index in row_indices:
            state = self._rows[row_index]
            if not state.selected or state.item_id is not None:
                continue
            order = self._download_order(state)
            ids = self._transfers.enqueue(
                [order[0]], alternatives=lambda _f, rest=order[1:]: rest,
                queue_patience_s=QUEUE_PATIENCE_S,
            )
            if not ids:
                continue   # that exact file is already downloading elsewhere
            state.item_id = ids[0]
            self._item_rows[ids[0]] = row_index
            state.status = "download_queued"
            state.live = ""
            self._batch_total += 1
            self._update_row_display(row_index)
        if self._item_rows:
            self.cancel_button.setEnabled(True)
            self.cancel_button.setVisible(True)
            self.progress_bar.setVisible(True)
            self.progress_bar.setRange(0, max(self._batch_total, 1))
            self.progress_bar.setValue(self._batch_done)
            self._update_download_summary()

    def _on_item_changed(self, item_id: int, phase: str, percent: int, text: str) -> None:
        row_index = self._item_rows.get(item_id)
        if row_index is None:
            return
        state = self._rows[row_index]
        state.status = "download_queued" if phase == "waiting" else "downloading"
        state.live = text
        self._update_row_display(row_index, actions=False)

    def _on_item_source_changed(self, item_id: int, file: SoulseekFile) -> None:
        row_index = self._item_rows.get(item_id)
        if row_index is not None:
            self._on_track_attempt(row_index, file)

    def _on_item_finished(self, item_id: int, ok: bool, detail: str, path: str) -> None:
        row_index = self._item_rows.pop(item_id, None)
        if row_index is None:
            return
        self._rows[row_index].item_id = None
        self._batch_done += 1
        self.progress_bar.setValue(self._batch_done)
        self._on_track_downloaded(row_index, ok, detail, path)
        self._update_download_summary()
        self._refresh_download_button()
        self._maybe_finish_batch()

    def _update_download_summary(self) -> None:
        if not self._batch_total:
            return
        active = sum(1 for r in self._rows if r.status == "downloading")
        waiting = sum(1 for r in self._rows if r.status == "download_queued")
        done = sum(1 for r in self._rows if r.status == "downloaded")
        failed = sum(1 for r in self._rows if r.status == "download_failed")
        parts = [f"{done} downloaded"]
        if active:
            parts.append(f"{active} in progress")
        if waiting:
            parts.append(f"{waiting} waiting to start")
        if failed:
            parts.append(f"{failed} failed")
        searching = self._search_thread is not None and self._search_thread.isRunning()
        self.status_label.setText(
            ("Still matching tracks. " if searching else "") + ", ".join(parts).capitalize() + "."
        )

    def _on_track_attempt(self, row_index: int, file: SoulseekFile) -> None:
        # Keep the Match column on the source actually being tried, so a
        # fallback is visible and a later "Retry" starts from it.
        state = self._rows[row_index]
        state.selected = file
        state.tried_peers.add(file.username)
        self._update_row_display(row_index, actions=False)

    def _on_track_downloaded(self, row_index: int, ok: bool, detail: str, local_path: str) -> None:
        state = self._rows[row_index]
        state.live = ""
        if ok:
            state.status = "downloaded"
            state.local_path = local_path
            self._newly_downloaded.append(local_path)
            self.file_downloaded.emit(local_path)
        else:
            state.status = "download_failed"
            state.detail = detail
            # Point "Retry" at a source that hasn't failed yet, if any left.
            fresh = [c for c in state.candidates if c.username not in state.tried_peers]
            if fresh:
                state.selected = fresh[0]
        self._update_row_display(row_index)

    def _maybe_finish_batch(self) -> None:
        searching = self._search_thread is not None and self._search_thread.isRunning()
        if self._item_rows or searching or not self._batch_total:
            return
        self._on_downloads_finished()

    def _on_downloads_finished(self) -> None:
        self.cancel_button.setVisible(False)
        self.progress_bar.setVisible(False)
        self._batch_total = self._batch_done = 0
        if self._newly_downloaded:
            failed = sum(1 for r in self._rows if r.status == "download_failed")
            note = f" {failed} failed: hover their status to see why, or Retry." if failed else ""
            self.status_label.setText(f"Downloaded {len(self._newly_downloaded)} tracks.{note}")
            self.files_ready.emit(self._newly_downloaded)
            self._newly_downloaded = []
        else:
            self.status_label.setText("Download batch finished. Nothing new was downloaded.")

    def _cancel_current(self) -> None:
        if self._search_thread is not None and self._search_thread.isRunning():
            self._search_thread.request_cancel()
        self._auto_download = False
        self._transfers.cancel_items(list(self._item_rows))
        self.cancel_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Row rendering
    # ------------------------------------------------------------------

    def _update_row_display(self, row_index: int, actions: bool = True) -> None:
        state = self._rows[row_index]

        status_item = self.table.item(row_index, 1)
        label = _STATUS_LABELS.get(state.status, state.status)
        if state.status == "downloading" and state.live:
            label = state.live
        elif state.live:
            label = f"{label} {state.live}"
        elif state.status in ("download_failed", "search_failed") and state.detail:
            label = f"{label}: {state.detail.splitlines()[-1][:80]}"
        status_item.setText(label)
        status_item.setForeground(_STATUS_COLORS.get(state.status, COLOR_FG))
        status_item.setToolTip(state.detail or label)

        match_item = self.table.item(row_index, 2)
        if state.selected:
            f = state.selected
            slot = "free slot" if f.has_free_slot else f"queue {f.queue_length}"
            match_item.setText(f"{f.format_label} · {f.size / (1024 * 1024):.1f} MB · {slot} · {f.username}")
            match_item.setToolTip(f.filename.replace("\\", "/"))
        else:
            match_item.setText("—")
            match_item.setToolTip("")

        if actions:
            self._update_row_actions(row_index)

    def _update_row_actions(self, row_index: int) -> None:
        state = self._rows[row_index]
        container = QWidget()
        container.setStyleSheet("QPushButton { padding: 3px 10px; }")
        row_layout = QHBoxLayout(container)
        row_layout.setContentsMargins(2, 2, 2, 2)
        row_layout.setSpacing(4)

        if state.item_id is not None:
            if len(state.candidates) > 1:
                skip_btn = QPushButton("Other source")
                skip_btn.setToolTip("Give up on this peer and try the next candidate now")
                skip_btn.clicked.connect(partial(self._transfers.skip_source, state.item_id))
                row_layout.addWidget(skip_btn)
            cancel_btn = QPushButton("Cancel")
            cancel_btn.clicked.connect(partial(self._transfers.cancel_items, [state.item_id]))
            row_layout.addWidget(cancel_btn)
        elif state.status not in _NO_ACTION_STATUSES:
            if state.candidates:
                choose_btn = QPushButton("Change Match")
                choose_btn.setToolTip("Pick a different Soulseek result for this track")
                choose_btn.clicked.connect(partial(self._choose_match, row_index))
                row_layout.addWidget(choose_btn)
            else:
                retry_btn = QPushButton("Retry Search")
                retry_btn.clicked.connect(partial(self._retry_search, row_index))
                row_layout.addWidget(retry_btn)

            if state.selected:
                label = {"downloaded": "Re-download", "download_failed": "Retry"}.get(state.status, "Download")
                dl_btn = QPushButton(label)
                dl_btn.clicked.connect(partial(self._download_row, row_index))
                row_layout.addWidget(dl_btn)

        self.table.setCellWidget(row_index, 3, container)

    def _on_row_double_clicked(self, item: QTableWidgetItem) -> None:
        state = self._rows[item.row()]
        if state.status == "downloaded" and state.local_path:
            self.open_in_library.emit(state.local_path)
