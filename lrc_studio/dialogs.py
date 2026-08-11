"""dialogs.py — secondary dialog windows: Settings and the Soulseek search
picker. Kept separate from ``widgets.py`` (custom-painted, always-visible
widgets) and ``app.py`` (orchestration) to keep each file's job obvious.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from constants import COLOR_MISSING, COLOR_SYNCED, COLOR_UNSYNCED
from soulseek import SlskdClient, SoulseekFile


class SettingsDialog(QDialog):
    """slskd connection details + the local library folder new downloads land in."""

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(440)
        self._settings = settings

        form = QFormLayout()

        self.slskd_url = QLineEdit(str(settings.value("slskd_url", "http://127.0.0.1:5030")))
        form.addRow("slskd URL:", self.slskd_url)

        self.slskd_api_key = QLineEdit(str(settings.value("slskd_api_key", "")))
        self.slskd_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("slskd API key:", self.slskd_api_key)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self._test_connection)
        self.test_status = QLabel("Run ./scripts/setup_slskd.sh once, then ./scripts/run_slskd.sh")
        self.test_status.setWordWrap(True)
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_status, stretch=1)
        form.addRow("", test_row)

        default_slskd_downloads = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "slskd-bin", "downloads")
        )
        dl_row = QHBoxLayout()
        self.slskd_downloads_dir = QLineEdit(
            str(settings.value("slskd_downloads_dir", default_slskd_downloads))
        )
        dl_browse_btn = QPushButton("Browse…")
        dl_browse_btn.clicked.connect(self._browse_slskd_downloads)
        dl_row.addWidget(self.slskd_downloads_dir, stretch=1)
        dl_row.addWidget(dl_browse_btn)
        form.addRow("slskd's downloads folder:", dl_row)

        lib_row = QHBoxLayout()
        self.library_dir = QLineEdit(str(settings.value("library_dir", "")))
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_library)
        lib_row.addWidget(self.library_dir, stretch=1)
        lib_row.addWidget(browse_btn)
        form.addRow("Move finished downloads to:", lib_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _browse_library(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose Library Folder", self.library_dir.text()
        )
        if chosen:
            self.library_dir.setText(chosen)

    def _browse_slskd_downloads(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose slskd Downloads Folder", self.slskd_downloads_dir.text()
        )
        if chosen:
            self.slskd_downloads_dir.setText(chosen)

    def _test_connection(self) -> None:
        client = SlskdClient(self.slskd_url.text().strip(), self.slskd_api_key.text().strip())
        ok, msg = client.check_connection()
        self.test_status.setText(msg)
        self.test_status.setStyleSheet(f"color: {(COLOR_SYNCED if ok else COLOR_MISSING).name()};")

    def _save(self) -> None:
        self._settings.setValue("slskd_url", self.slskd_url.text().strip())
        self._settings.setValue("slskd_api_key", self.slskd_api_key.text().strip())
        self._settings.setValue("slskd_downloads_dir", self.slskd_downloads_dir.text().strip())
        self._settings.setValue("library_dir", self.library_dir.text().strip())
        self.accept()


class SoulseekSearchDialog(QDialog):
    """Search Soulseek (via slskd) and let the user pick one result to download.

    Purely a picker — searching and the actual download are driven by the
    caller via ``SoulseekSearchThread``/``SoulseekDownloadThread`` so this
    dialog never blocks the UI thread itself.
    """

    def __init__(self, query: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Find on Soulseek")
        self.setMinimumSize(600, 420)
        self.selected_file: SoulseekFile | None = None
        self._results: list[SoulseekFile] = []

        layout = QVBoxLayout(self)

        search_row = QHBoxLayout()
        self.query_input = QLineEdit(query)
        self.search_button = QPushButton("Search")
        search_row.addWidget(self.query_input, stretch=1)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        self.status_label = QLabel("Enter a search and press Search.")
        layout.addWidget(self.status_label)

        self.result_list = QListWidget()
        layout.addWidget(self.result_list, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Download Selected")
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_searching(self, is_searching: bool) -> None:
        self.search_button.setEnabled(not is_searching)
        self.query_input.setEnabled(not is_searching)
        if is_searching:
            self.status_label.setText("Searching Soulseek… (waiting for peer responses)")
            self.result_list.clear()

    def show_results(self, results: list[SoulseekFile]) -> None:
        self._results = results
        self.result_list.clear()
        if not results:
            self.status_label.setText("No results.")
            return
        self.status_label.setText(f"{len(results)} result(s) — best matches first.")
        for f in results:
            item = QListWidgetItem(f"{f.display_name}   [{f.quality_label}]   from {f.username}")
            item.setToolTip(f.filename)
            self.result_list.addItem(item)

    def show_error(self, message: str) -> None:
        self.status_label.setText(f"Search failed: {message}")

    def _accept_selected(self) -> None:
        row = self.result_list.currentRow()
        if 0 <= row < len(self._results):
            self.selected_file = self._results[row]
            self.accept()
        else:
            self.status_label.setText("Select a result first.")


class SpotifyImportDialog(QDialog):
    """Paste a playlist link, watch the fetch → dedup → Soulseek pipeline run.

    A log view rather than a track-by-track picker — for a playlist-sized
    batch, a running log is more useful than a UI you'd have to babysit.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Spotify Playlist")
        self.setMinimumSize(560, 400)

        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("Paste a Spotify playlist link…")
        self.import_button = QPushButton("Import Missing Tracks")
        row.addWidget(self.link_input, stretch=1)
        row.addWidget(self.import_button)
        layout.addLayout(row)

        self.log_list = QListWidget()
        layout.addWidget(self.log_list, stretch=1)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.close_button)

    def log(self, message: str) -> None:
        self.log_list.addItem(message)
        self.log_list.scrollToBottom()

    def set_busy(self, busy: bool) -> None:
        self.import_button.setEnabled(not busy)
        self.link_input.setEnabled(not busy)
        self.close_button.setEnabled(not busy)


class ValidateSyncDialog(QDialog):
    """Reports the optional Whisper-based sync spot-check's results.

    A spot-check, not an exhaustive audit — it samples a few lines, so the
    summary is worded accordingly and results are colored as a hint, not
    a verdict to blindly trust.
    """

    _VERDICT_LABELS = {
        "ok": "OK",
        "timing_drift": "TIMING?",
        "text_mismatch": "TEXT?",
        "no_audio_match": "NO AUDIO",
    }
    _VERDICT_COLORS = {
        "ok": COLOR_SYNCED,
        "timing_drift": COLOR_UNSYNCED,
        "text_mismatch": COLOR_MISSING,
        "no_audio_match": COLOR_MISSING,
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Validate Sync")
        self.setMinimumSize(600, 420)

        layout = QVBoxLayout(self)

        self.status_label = QLabel("Starting…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.result_list = QListWidget()
        layout.addWidget(self.result_list, stretch=1)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.close_button)

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def show_results(self, results: list) -> None:
        self.result_list.clear()
        if not results:
            self.status_label.setText("No synced lines to check yet.")
            return

        counts: dict[str, int] = {}
        for r in results:
            counts[r.verdict] = counts.get(r.verdict, 0) + 1

            label = self._VERDICT_LABELS.get(r.verdict, r.verdict.upper())
            drift = f", drift {r.timing_drift_ms:+d}ms" if r.timing_drift_ms is not None else ""
            item = QListWidgetItem(
                f"[{label}] line {r.verse_index + 1}: expected \"{r.expected_text}\" — "
                f"heard \"{r.heard_text or '(nothing)'}\" (match {r.text_score}%{drift})"
            )
            item.setForeground(self._VERDICT_COLORS.get(r.verdict, COLOR_UNSYNCED))
            self.result_list.addItem(item)

        flagged = len(results) - counts.get("ok", 0)
        self.status_label.setText(
            f"Spot-checked {len(results)} sampled line(s): {counts.get('ok', 0)} OK, "
            f"{flagged} flagged for review. This samples a few lines, not the "
            "whole song — treat flags as worth a listen, not certainties."
        )
