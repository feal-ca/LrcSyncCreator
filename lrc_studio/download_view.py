"""download_view.py — ``DownloadView``: the Download tab.

Downloading is a first-class part of the app rather than a modal dialog:
paste a Spotify track, album, or playlist link, everything missing from
the local library gets auto-searched on Soulseek (best match shown,
nothing downloaded yet), the user reviews/overrides matches, then
explicitly commits with "Download All Missing" (or a per-row "Download").
"""

from __future__ import annotations

import os
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
    COLOR_ACCENT, COLOR_FG, COLOR_MISSING, COLOR_MUTED, COLOR_SYNCED,
    COLOR_TIMELINE_BG, COLOR_UNSYNCED,
)
from dialogs import SoulseekSearchDialog
from soulseek import SlskdClient, SoulseekFile
from workers import (
    PlaylistDownloadThread, PlaylistFetchThread, PlaylistSearchThread,
    SoulseekSearchThread, stop_thread,
)

_STATUS_LABELS = {
    "in_library":      "In Library",
    "queued":          "Queued",
    "searching":       "Searching…",
    "matched":         "Matched",
    "no_results":      "No Results",
    "search_failed":   "Search Failed",
    "download_queued": "Queued",
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
    "search_failed":   COLOR_MISSING,
    "download_queued": COLOR_MUTED,
    "downloading":     COLOR_ACCENT,
    "downloaded":      COLOR_SYNCED,
    "download_failed": COLOR_MISSING,
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
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._get_client = get_client
        self._collect_library_paths = collect_library_paths

        self._rows: list[_RowState] = []
        self._newly_downloaded: list[str] = []

        self._fetch_thread: PlaylistFetchThread | None = None
        self._search_thread: PlaylistSearchThread | None = None
        self._download_thread: PlaylistDownloadThread | None = None
        self._adhoc_search_thread: SoulseekSearchThread | None = None
        self._current_cancel_target = None

        self._build_ui()

    def shutdown(self) -> None:
        """Stop any in-flight threads. Call from the main window's
        closeEvent so closing mid-fetch/search/download doesn't crash."""
        stop_thread(self._fetch_thread)
        stop_thread(self._search_thread)
        stop_thread(self._download_thread)
        stop_thread(self._adhoc_search_thread)

    def is_batch_active(self) -> bool:
        """True while a fetch, search, or download is in flight — callers
        (e.g. re-entering Find Music) should not restart a fetch or clear
        the table/rows while this is true; the running thread would still
        be delivering results into rows that no longer exist."""
        return any(
            thread is not None and thread.isRunning()
            for thread in (self._fetch_thread, self._search_thread, self._download_thread)
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        link_row = QHBoxLayout()
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("Paste a Spotify track, album, or playlist link…")
        self.link_input.returnPressed.connect(self._on_fetch_clicked)
        self.fetch_button = QPushButton("Fetch")
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
        self.status_label.setStyleSheet(f"color: {COLOR_MUTED.name()}; font-size: 12px;")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Track", "Status", "Match", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: {COLOR_TIMELINE_BG.name()};"
            " border: 1px solid #45475a; font-size: 13px; border-radius: 4px; }"
            f"QHeaderView::section {{ background-color: #313244; color: {COLOR_FG.name()};"
            " border: none; padding: 4px; }"
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table, stretch=1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        bottom_row = QHBoxLayout()
        self.download_button = QPushButton("Download All Missing")
        self.download_button.setToolTip(
            "Download every track's currently selected match — auto-picked "
            "best match unless you changed it"
        )
        self.download_button.clicked.connect(self._download_all)
        self.download_button.setEnabled(False)
        self.download_button.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_SYNCED.name()};"
            " color: #1e1e2e; font-weight: bold; padding: 8px; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #45475a; color: #7f849c; }"
        )
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
        self._search_thread.track_matched.connect(self._on_track_matched)
        self._search_thread.track_failed.connect(self._on_track_failed)
        self._search_thread.finished_search.connect(self._on_search_finished)
        self._current_cancel_target = self._search_thread
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(True)
        self._search_thread.start()

    def _on_track_searching(self, row_index: int) -> None:
        self._rows[row_index].status = "searching"
        self._update_row_display(row_index)

    def _on_track_matched(self, row_index: int, candidates: list) -> None:
        state = self._rows[row_index]
        state.candidates = candidates
        state.selected = candidates[0]
        state.tried_peers = set()
        state.status = "matched"
        self._update_row_display(row_index)

    def _on_track_failed(self, row_index: int, message: str) -> None:
        state = self._rows[row_index]
        state.status = "no_results" if message == "No Soulseek results." else "search_failed"
        state.detail = message
        self._update_row_display(row_index)

    def _on_search_finished(self) -> None:
        self.cancel_button.setVisible(False)
        self._search_thread = None
        matched = sum(1 for r in self._rows if r.selected)
        self.status_label.setText(
            f"Search complete — {matched} match(es) ready. Review the Match "
            "column, then click Download All Missing."
        )
        self.download_button.setEnabled(matched > 0)

    def _retry_search(self, row_index: int) -> None:
        client = self._get_client()
        if client is None:
            return
        self._start_search(client, [(row_index, self._rows[row_index].track)])

    def _choose_match(self, row_index: int) -> None:
        state = self._rows[row_index]
        # Prefill with the title alone, not "artist title" — Soulseek's
        # search tends to do better on a narrower query that a person can
        # then eyeball-filter by artist, same as searching it by hand.
        dialog = SoulseekSearchDialog(state.track.title, self)
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
            self.download_button.setEnabled(any(r.selected for r in self._rows))

    # ------------------------------------------------------------------
    # Download phase (explicit commit)
    # ------------------------------------------------------------------

    def _download_all(self) -> None:
        client = self._get_client()
        if client is None:
            return
        rows = [
            (i, self._download_order(r)) for i, r in enumerate(self._rows)
            if r.selected and r.status not in ("download_queued", "downloading", "downloaded")
        ]
        if not rows:
            QMessageBox.information(self, "Nothing To Download", "No matched tracks are ready to download.")
            return
        self._start_download(client, rows)

    def _download_row(self, row_index: int) -> None:
        client = self._get_client()
        if client is None:
            return
        state = self._rows[row_index]
        if not state.selected:
            return
        self._start_download(client, [(row_index, self._download_order(state))])

    @staticmethod
    def _download_order(state: _RowState) -> list[SoulseekFile]:
        """The chosen match first, then the row's other candidates as
        automatic fallbacks if it fails (see PlaylistDownloadThread) —
        peers that already failed this row go last."""
        others = [c for c in state.candidates if c is not state.selected]
        others.sort(key=lambda c: c.username in state.tried_peers)
        return [state.selected] + others

    def _start_download(
        self, client: SlskdClient, rows: list[tuple[int, list[SoulseekFile]]],
    ) -> None:
        if self._download_thread is not None and self._download_thread.isRunning():
            QMessageBox.information(
                self, "Download In Progress",
                "A download batch is already running — wait for it to finish.",
            )
            return

        downloads_dir = str(self._settings.value("slskd_downloads_dir", "")).strip()
        library_dir = str(self._settings.value("library_dir", "")).strip() or os.getcwd()

        # "download_queued", not "downloading" — this thread processes one
        # row at a time and emits track_downloading for whichever is
        # actually active, so the table shows real progress instead of
        # every row appearing to freeze at once (also blocks their action
        # buttons so a queued row can't be double-clicked mid-batch).
        for row_index, _ in rows:
            self._rows[row_index].status = "download_queued"
            self._update_row_display(row_index)

        self._newly_downloaded = []
        self._download_thread = PlaylistDownloadThread(client, downloads_dir, library_dir, rows)
        self._download_thread.progress.connect(self.status_label.setText)
        self._download_thread.track_progress.connect(self._on_download_progress)
        self._download_thread.track_downloading.connect(self._on_track_downloading)
        self._download_thread.track_attempt.connect(self._on_track_attempt)
        self._download_thread.track_done.connect(self._on_track_downloaded)
        self._download_thread.finished_downloads.connect(self._on_downloads_finished)
        self._current_cancel_target = self._download_thread
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(rows))
        self.progress_bar.setValue(0)
        self._download_thread.start()

    def _on_download_progress(self, done: int, total: int) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)

    def _on_track_downloading(self, row_index: int) -> None:
        self._rows[row_index].status = "downloading"
        self._update_row_display(row_index)

    def _on_track_attempt(self, row_index: int, file: SoulseekFile) -> None:
        # Keep the Match column on the source actually being tried, so a
        # fallback is visible and a later "Retry" starts from it.
        state = self._rows[row_index]
        state.selected = file
        state.tried_peers.add(file.username)
        self._update_row_display(row_index)

    def _on_track_downloaded(self, row_index: int, ok: bool, detail: str, local_path: str) -> None:
        state = self._rows[row_index]
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

    def _on_downloads_finished(self) -> None:
        self.cancel_button.setVisible(False)
        self.progress_bar.setVisible(False)
        self._download_thread = None
        if self._newly_downloaded:
            self.status_label.setText(f"Downloaded {len(self._newly_downloaded)} track(s).")
            self.files_ready.emit(self._newly_downloaded)
            self._newly_downloaded = []
        else:
            self.status_label.setText("Download batch finished.")

    def _cancel_current(self) -> None:
        if self._current_cancel_target is not None:
            self._current_cancel_target.request_cancel()
        self.cancel_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Row rendering
    # ------------------------------------------------------------------

    def _update_row_display(self, row_index: int) -> None:
        state = self._rows[row_index]

        status_item = self.table.item(row_index, 1)
        status_item.setText(_STATUS_LABELS.get(state.status, state.status))
        status_item.setForeground(_STATUS_COLORS.get(state.status, COLOR_FG))
        if state.detail:
            status_item.setToolTip(state.detail)

        match_item = self.table.item(row_index, 2)
        match_item.setText(state.selected.quality_label if state.selected else "—")

        self._update_row_actions(row_index)

    def _update_row_actions(self, row_index: int) -> None:
        state = self._rows[row_index]
        container = QWidget()
        row_layout = QHBoxLayout(container)
        row_layout.setContentsMargins(2, 2, 2, 2)
        row_layout.setSpacing(4)

        if state.status not in _NO_ACTION_STATUSES:
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
