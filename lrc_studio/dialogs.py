"""dialogs.py — secondary windows: Settings, the "Change match" picker
for the Spotify review, the lyrics picker, and Validate Sync's report.
Kept apart from ``widgets.py`` (always-visible custom widgets) and
``app.py`` (orchestration) to keep each file's job obvious.
"""

from __future__ import annotations


from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

import theme
from constants import COLOR_DANGER, COLOR_SYNCED, COLOR_UNSYNCED
from postprocess import _as_bool
from utils import read_artist_title, write_artist_title
from soulseek import SoulseekFile


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "heading")
    return label


class RenamePreviewDialog(QDialog):
    """Shows what "Rename to “Artist - Title”" will do to several songs
    before anything is touched: old name, new name (or why it stays)."""

    def __init__(self, plans: list, parent=None) -> None:
        super().__init__(parent)
        import os

        todo = [p for p in plans if p.new]
        self.setWindowTitle("Rename to “Artist - Title”")
        self.setMinimumSize(720, 420)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"{len(todo)} of {len(plans)} songs get a new name from their tags. "
            "Their .lrc files are renamed with them."
            if todo else "None of these songs can be renamed."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(plans), 2)
        self.table.setHorizontalHeaderLabels(["Now", "After"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # Changes first, then the files that stay as they are.
        ordered = todo + [p for p in plans if not p.new]
        for row, plan in enumerate(ordered):
            self.table.setItem(row, 0, QTableWidgetItem(os.path.basename(plan.old)))
            after = QTableWidgetItem(os.path.basename(plan.new) if plan.new else plan.reason)
            if not plan.new:
                after.setForeground(QColor(theme.TEXT_3))
            self.table.setItem(row, 1, after)
        layout.addWidget(self.table, stretch=1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText(f"Rename {len(todo)}")
        ok.setProperty("variant", "primary")
        ok.setEnabled(bool(todo))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


class TagEditDialog(QDialog):
    """Edit Artist & Title: the two tags that drive renaming and the
    lyrics lookup."""

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        import os

        self._path = path
        self.setWindowTitle("Edit Artist & Title")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        name = QLabel(os.path.basename(path))
        name.setProperty("role", "muted")
        layout.addWidget(name)
        artist, title = read_artist_title(path)
        form = QFormLayout()
        self.artist_input = QLineEdit(artist)
        self.title_input = QLineEdit(title)
        form.addRow("Artist:", self.artist_input)
        form.addRow("Title:", self.title_input)
        layout.addLayout(form)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {theme.DANGER};")
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("variant", "primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        try:
            write_artist_title(self._path, self.artist_input.text(), self.title_input.text())
        except Exception as exc:  # noqa: BLE001 — shown to the user, dialog stays open
            self.error_label.setText(f"Couldn't save the tags: {exc}")
            return
        self.accept()


class SettingsDialog(QDialog):
    """Where music goes, what happens to downloads, and the Soulseek account."""

    def __init__(self, settings: QSettings, parent=None, soulseek_status: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._settings = settings

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(_section("Library"))
        lib_form = QFormLayout()
        lib_row = QHBoxLayout()
        self.library_dir = QLineEdit(str(settings.value("library_dir", "")))
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_library)
        lib_row.addWidget(self.library_dir, stretch=1)
        lib_row.addWidget(browse_btn)
        lib_form.addRow("Music folder:", lib_row)
        layout.addLayout(lib_form)
        lib_hint = QLabel("Shown in the library on startup. Finished downloads are moved here.")
        lib_hint.setProperty("role", "faint")
        layout.addWidget(lib_hint)

        layout.addSpacing(6)
        layout.addWidget(_section("Downloads"))
        self.convert_check = QCheckBox("Convert lossless downloads (FLAC, WAV, ALAC) to Opus")
        self.convert_check.setChecked(_as_bool(settings.value("convert_lossless", True), True))
        self.bitrate_combo = QComboBox()
        for kbps in (128, 160, 192, 256):
            self.bitrate_combo.addItem(f"{kbps} kbps", kbps)
        current = int(settings.value("opus_bitrate", 192) or 192)
        self.bitrate_combo.setCurrentIndex(max(0, self.bitrate_combo.findData(current)))
        conv_row = QHBoxLayout()
        conv_row.addWidget(self.convert_check)
        conv_row.addStretch(1)
        conv_row.addWidget(self.bitrate_combo)
        layout.addLayout(conv_row)
        conv_hint = QLabel(
            "Tags and cover art are kept and the original is deleted once the Opus file checks out. "
            "MP3 and other lossy files are never re-encoded."
        )
        conv_hint.setWordWrap(True)
        conv_hint.setProperty("role", "faint")
        layout.addWidget(conv_hint)
        self.rename_check = QCheckBox("Rename downloads to “Artist - Title” from their tags")
        self.rename_check.setChecked(_as_bool(settings.value("rename_downloads", True), True))
        layout.addWidget(self.rename_check)
        self.embed_check = QCheckBox("Also write lyrics into the audio file's tags")
        self.embed_check.setChecked(_as_bool(settings.value("embed_lyrics", False), False))
        layout.addWidget(self.embed_check)
        self.ytmusic_check = QCheckBox("Also look for lyrics on YouTube Music (slower, unofficial)")
        self.ytmusic_check.setToolTip(
            "Covers more Spanish and Catalan songs. Needs: pip install -r requirements-optional.txt"
        )
        self.ytmusic_check.setChecked(_as_bool(settings.value("lyrics_ytmusic", False), False))
        layout.addWidget(self.ytmusic_check)

        layout.addSpacing(6)
        layout.addWidget(_section("Soulseek"))
        form = QFormLayout()
        self.soulseek_username = QLineEdit(str(settings.value("soulseek_username", "") or ""))
        form.addRow("Username:", self.soulseek_username)
        self.soulseek_password = QLineEdit(str(settings.value("soulseek_password", "") or ""))
        self.soulseek_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self.soulseek_password)
        layout.addLayout(form)
        account_hint = QLabel(
            "A new username is registered the first time you log in with it. "
            "Don't use the same account in Nicotine+ at the same time: Soulseek logs the other one out."
        )
        account_hint.setWordWrap(True)
        account_hint.setProperty("role", "faint")
        layout.addWidget(account_hint)
        self.share_check = QCheckBox("Share my music folder with other Soulseek users")
        self.share_check.setToolTip(
            "Many people only send files to users who share some. Uploads are limited to 2 at a time."
        )
        self.share_check.setChecked(_as_bool(settings.value("share_library", True), True))
        layout.addWidget(self.share_check)
        self.soulseek_status = QLabel(soulseek_status)
        self.soulseek_status.setWordWrap(True)
        self.soulseek_status.setProperty("role", "muted")
        layout.addWidget(self.soulseek_status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("variant", "primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addSpacing(6)
        layout.addWidget(buttons)

    def _browse_library(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose Music Folder", self.library_dir.text())
        if chosen:
            self.library_dir.setText(chosen)

    def _save(self) -> None:
        s = self._settings
        s.setValue("soulseek_username", self.soulseek_username.text().strip())
        s.setValue("soulseek_password", self.soulseek_password.text())
        s.setValue("share_library", self.share_check.isChecked())
        s.setValue("library_dir", self.library_dir.text().strip())
        s.setValue("convert_lossless", self.convert_check.isChecked())
        s.setValue("opus_bitrate", int(self.bitrate_combo.currentData()))
        s.setValue("rename_downloads", self.rename_check.isChecked())
        s.setValue("embed_lyrics", self.embed_check.isChecked())
        s.setValue("lyrics_ytmusic", self.ytmusic_check.isChecked())
        self.accept()


class SoulseekSearchDialog(QDialog):
    """Pick one Soulseek result: the Spotify review's "Change match".

    Search by title, narrow with the filter (prefilled with the artist),
    pick a row. Purely a picker: the caller runs the search thread and
    feeds results in, so this dialog never blocks the UI thread itself.
    """

    def __init__(self, query: str, parent=None, filter_text: str = "") -> None:
        from search_view import SearchResultsWidget

        super().__init__(parent)
        self.setWindowTitle("Change match")
        self.resize(980, 560)
        self.selected_file: SoulseekFile | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        search_row = QHBoxLayout()
        self.query_input = QLineEdit(query)
        self.query_input.setProperty("variant", "search")
        self.search_button = QPushButton("Search again")
        search_row.addWidget(self.query_input, stretch=1)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)
        self.query_input.returnPressed.connect(self.search_button.click)

        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedHeight(3)
        layout.addWidget(self.progress_bar)

        self.results = SearchResultsWidget(show_folder_action=False)
        self.results.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.activated.connect(lambda files: self._accept_file(files[0]))
        if filter_text:
            self.results.set_filter_text(filter_text)
        layout.addWidget(self.results, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Use this file")
        ok.setProperty("variant", "primary")
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_searching(self, is_searching: bool) -> None:
        self.search_button.setEnabled(not is_searching)
        self.query_input.setEnabled(not is_searching)
        self.progress_bar.setVisible(is_searching)
        if is_searching:
            self.status_label.setText("Searching Soulseek… (waiting for peers to answer)")
            self.results.set_results([])

    def show_results(self, results: list[SoulseekFile]) -> None:
        self.results.set_results(results)
        if not results:
            self.status_label.setText("No results. Try just the title, or just the artist.")
            return
        shown = self.results.visible_count()
        extra = f", {shown} match the filter" if shown != len(results) else ""
        self.status_label.setText(f"{len(results)} results{extra}. Best candidates first.")
        self.results.select_first()

    def show_error(self, message: str) -> None:
        self.status_label.setText(f"Search failed: {message}")

    def _accept_file(self, f: SoulseekFile) -> None:
        self.selected_file = f
        self.accept()

    def _accept_selected(self) -> None:
        files = self.results.selected_files()
        if files:
            self._accept_file(files[0])
        else:
            self.status_label.setText("Select a result first.")


class LyricsPickerDialog(QDialog):
    """Find lyrics: look the song up everywhere, compare, preview, choose.

    Candidates from LRCLIB, Megalobiz, QQ Music and NetEase (and YouTube
    Music when turned on) come back in one list, ranked by how well
    title, artist and duration match the loaded file. The length column
    shows the difference from the real track, the quickest tell for a
    live cut or a different edit.
    """

    def __init__(self, artist: str, title: str, duration_s: float | None, parent=None,
                 use_ytmusic: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Find lyrics")
        self._use_ytmusic = use_ytmusic
        self.resize(940, 580)
        self.chosen_text: str = ""
        self._duration_s = duration_s
        self._candidates: list = []
        self._threads: list = []

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.artist_input = QLineEdit(artist)
        self.artist_input.setPlaceholderText("Artist")
        self.title_input = QLineEdit(title)
        self.title_input.setPlaceholderText("Title")
        self.search_button = QPushButton("Search")
        self.search_button.setProperty("variant", "primary")
        self.search_button.clicked.connect(self.run_search)
        for w in (self.artist_input, self.title_input):
            w.returnPressed.connect(self.run_search)
        row.addWidget(self.artist_input, stretch=2)
        row.addWidget(self.title_input, stretch=3)
        row.addWidget(self.search_button)
        layout.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        layout.addWidget(self.status_label)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Title", "Artist", "Source", "Length", "Timing"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 130)
        for col in (2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        self.table.itemDoubleClicked.connect(lambda _i: self._accept_selected())
        split.addWidget(self.table)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(theme.mono_font(9.5))
        self.preview.setPlaceholderText("Pick a result to preview its lyrics.")
        split.addWidget(self.preview)
        split.setSizes([560, 380])
        layout.addWidget(split, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.use_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.use_button.setText("Use these lyrics")
        self.use_button.setProperty("variant", "primary")
        self.use_button.setEnabled(False)
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- searching ---------------------------------------------------------

    def run_search(self) -> None:
        from workers import LyricsCandidatesThread

        artist, title = self.artist_input.text().strip(), self.title_input.text().strip()
        if not title:
            self.status_label.setText("Enter at least a title.")
            return
        self.status_label.setText("Searching LRCLIB, Megalobiz, QQ Music and NetEase…")
        self.search_button.setEnabled(False)
        thread = LyricsCandidatesThread(artist, title, self._duration_s, use_ytmusic=self._use_ytmusic)
        thread.results_ready.connect(self._show_candidates)
        thread.failed.connect(lambda msg: self._search_done(f"Search failed: {msg}"))
        self._threads.append(thread)
        thread.start()

    def _search_done(self, message: str) -> None:
        self.search_button.setEnabled(True)
        self.status_label.setText(message)

    def _show_candidates(self, candidates: list) -> None:
        self._candidates = [c for c in candidates if c.text.strip()]
        self.table.setRowCount(0)
        for c in self._candidates:
            self._append_candidate(c)
        if self._candidates:
            synced = sum(1 for c in self._candidates if c.has_synced)
            sources = len({c.source for c in self._candidates})
            self._search_done(
                f"{len(self._candidates)} results from {sources} source{'s' if sources != 1 else ''}, "
                f"{synced} with synced timing. Closest match first."
            )
            self.table.selectRow(0)
        else:
            self._search_done("Nothing found. Try a shorter title, or check the artist.")

    def _append_candidate(self, c) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._fill_row(row, c)

    def _fill_row(self, row: int, c) -> None:
        song = QTableWidgetItem(c.title)
        song.setToolTip(f"{c.artist} - {c.title}" + (f"\nAlbum: {c.album}" if c.album else ""))
        artist = QTableWidgetItem(c.artist)
        artist.setForeground(theme_color(theme.TEXT_2))
        source = QTableWidgetItem(_SOURCE_NAMES.get(c.source, c.source))
        source.setForeground(theme_color(theme.TEXT_2))
        if c.duration_s:
            m, s = divmod(int(round(c.duration_s)), 60)
            length_text = f"{m}:{s:02d}"
            if self._duration_s:
                diff = c.duration_s - self._duration_s
                if abs(diff) >= 1:
                    length_text += f"  ({diff:+.0f}s)"
            length = QTableWidgetItem(length_text)
            if self._duration_s and abs(c.duration_s - self._duration_s) > 3:
                length.setForeground(theme_color(theme.DANGER))
        else:
            length = QTableWidgetItem("")
        timing = QTableWidgetItem("Synced" if c.has_synced else "Text only")
        timing.setForeground(theme_color(theme.OK if c.has_synced else theme.PLAIN))
        for col, item in enumerate((song, artist, source, length, timing)):
            self.table.setItem(row, col, item)

    def _preview_selected(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._candidates):
            self.preview.setPlainText(self._candidates[row].text)
            self.use_button.setEnabled(True)
        else:
            self.preview.clear()
            self.use_button.setEnabled(False)

    def _accept_selected(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._candidates):
            self.chosen_text = self._candidates[row].text
            self.accept()

    def done(self, result: int) -> None:  # noqa: D401 — QDialog override
        # A lookup still in flight finishes on its own; just keep its
        # QThread object alive until then instead of killing a request.
        for thread in self._threads:
            if thread.isRunning():
                _LINGERING.add(thread)
                thread.finished.connect(lambda t=thread: _LINGERING.discard(t))
        super().done(result)


_LINGERING: set = set()
_SOURCE_NAMES = {
    "lrclib": "LRCLIB", "megalobiz": "Megalobiz", "qq": "QQ Music",
    "netease": "NetEase", "ytmusic": "YouTube Music", "syncedlyrics": "Genius",
}


def theme_color(hex_color: str):
    from PyQt6.QtGui import QColor
    return QColor(hex_color)


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
        "text_mismatch": COLOR_DANGER,
        "no_audio_match": COLOR_DANGER,
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
