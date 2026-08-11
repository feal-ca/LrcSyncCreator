"""
workers.py — QThread subclasses for network-bound and CPU-bound tasks.

Keeping all background work off the main thread prevents UI freezes even
when the lyrics API is slow or the file system stalls.
"""

from __future__ import annotations

import os

import mutagen
import syncedlyrics
from PyQt6.QtCore import QThread, pyqtSignal


class LyricsDownloaderThread(QThread):
    """Download synced LRC lyrics for a batch of audio files.

    Signals
    -------
    progress(message, files_done, files_total)
        Fired before each file is processed so the UI can update a status
        label *and* a progress bar value simultaneously.
    finished_batch(success_names, failed_names)
        Fired once when all files have been attempted.
    """

    progress      = pyqtSignal(str, int, int)   # (msg, done, total)
    finished_batch = pyqtSignal(list, list)      # (success, failed)

    def __init__(self, files: list[str]) -> None:
        super().__init__()
        self.files = files

    # ------------------------------------------------------------------
    # QThread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        success: list[str] = []
        failed:  list[str] = []
        total = len(self.files)

        for idx, file_path in enumerate(self.files, 1):
            filename = os.path.basename(file_path)
            self.progress.emit(f"Searching: {filename}", idx, total)

            try:
                query = self._build_query(file_path, filename)
                lrc_content = syncedlyrics.search(query)

                if lrc_content:
                    lrc_path = os.path.splitext(file_path)[0] + ".lrc"
                    with open(lrc_path, "w", encoding="utf-8") as fh:
                        fh.write(lrc_content)
                    success.append(filename)
                else:
                    failed.append(filename)

            except Exception:
                # Individual file failures should never abort the whole batch.
                failed.append(filename)

        self.finished_batch.emit(success, failed)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_query(file_path: str, filename: str) -> str:
        """Return the best search query for *file_path*.

        Prefers embedded ``artist`` + ``title`` tags; falls back to the
        filename stem when tags are absent or unreadable.
        """
        try:
            audio = mutagen.File(file_path, easy=True)
            if audio:
                title  = audio.get("title",  [""])[0].strip()
                artist = audio.get("artist", [""])[0].strip()
                if title and artist:
                    return f"{artist} {title}"
        except Exception:
            pass
        return os.path.splitext(filename)[0]


class LyricsSearchThread(QThread):
    """One-shot lyrics search for a free-text query (the manual "Web Search" action).

    Kept off the main thread so a slow/unreachable lyrics provider never
    freezes the UI the way a direct ``syncedlyrics.search()`` call would.
    """

    result_ready = pyqtSignal(str)

    def __init__(self, query: str) -> None:
        super().__init__()
        self._query = query

    def run(self) -> None:
        try:
            result = syncedlyrics.search(self._query)
            self.result_ready.emit(result or "")
        except Exception:
            self.result_ready.emit("")
