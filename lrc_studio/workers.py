"""
workers.py — QThread subclasses for network-bound and CPU-bound tasks.

Keeping all background work off the main thread prevents UI freezes even
when the lyrics API is slow or the file system stalls.
"""

from __future__ import annotations

import os
import shutil
import time

import mutagen
import syncedlyrics
from PyQt6.QtCore import QThread, pyqtSignal

from soulseek import SlskdClient, SlskdError, SoulseekFile, find_downloaded_file


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


def unique_path(path: str) -> str:
    """Return *path*, or ``name (2).ext``, ``name (3).ext``, … if it exists."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"


class SoulseekSearchThread(QThread):
    """Run a Soulseek search via slskd without blocking the UI."""

    results_ready = pyqtSignal(list)   # list[SoulseekFile]
    search_failed = pyqtSignal(str)

    def __init__(self, client: SlskdClient, query: str) -> None:
        super().__init__()
        self._client = client
        self._query = query

    def run(self) -> None:
        try:
            self.results_ready.emit(self._client.search(self._query))
        except SlskdError as exc:
            self.search_failed.emit(str(exc))


class SoulseekDownloadThread(QThread):
    """Queue a Soulseek download, wait for it to land, and move it into the library.

    slskd hands off the actual peer-to-peer transfer; this thread just
    polls for the file's arrival in slskd's downloads directory (see
    ``find_downloaded_file``) rather than trusting any specific transfer-
    state schema to signal completion.
    """

    progress      = pyqtSignal(str)
    finished_ok   = pyqtSignal(str)   # final path inside the library
    finished_fail = pyqtSignal(str)   # error message

    def __init__(
        self,
        client: SlskdClient,
        downloads_dir: str,
        library_dir: str,
        file: SoulseekFile,
        timeout_s: float = 180.0,
    ) -> None:
        super().__init__()
        self._client = client
        self._downloads_dir = downloads_dir
        self._library_dir = library_dir
        self._file = file
        self._timeout_s = timeout_s

    def run(self) -> None:
        try:
            self.progress.emit(f"Requesting {self._file.display_name} from {self._file.username}…")
            self._client.download(self._file)
        except SlskdError as exc:
            self.finished_fail.emit(str(exc))
            return

        deadline = time.monotonic() + self._timeout_s
        local_path = None
        while time.monotonic() < deadline:
            local_path = find_downloaded_file(
                self._downloads_dir, self._file.filename, self._file.size
            )
            if local_path:
                break
            state = self._client.get_download_state(self._file.username, self._file.filename)
            self.progress.emit(f"{self._file.display_name}: {state or 'waiting…'}")
            time.sleep(2)

        if not local_path:
            self.finished_fail.emit(f"Timed out waiting for {self._file.display_name}.")
            return

        try:
            os.makedirs(self._library_dir, exist_ok=True)
            dest = unique_path(os.path.join(self._library_dir, os.path.basename(local_path)))
            shutil.move(local_path, dest)
        except OSError as exc:
            self.finished_fail.emit(f"Downloaded but couldn't move into library: {exc}")
            return

        self.finished_ok.emit(dest)


class SpotifyImportThread(QThread):
    """End-to-end: fetch a Spotify playlist, skip tracks already in the
    library, then search+download each missing one via Soulseek.

    Runs entirely off the UI thread — a playlist fetch chained into up to
    dozens of sequential Soulseek searches/downloads can take a while.
    """

    progress        = pyqtSignal(str)
    track_done      = pyqtSignal(str, bool, str)      # (track_label, success, detail)
    fetch_failed    = pyqtSignal(str)
    finished_import = pyqtSignal(list, int, int)      # (new_paths, skipped_existing, skipped_no_result)

    def __init__(
        self,
        client: SlskdClient,
        downloads_dir: str,
        library_dir: str,
        playlist_link: str,
        library_paths: list[str],
        download_timeout_s: float = 180.0,
    ) -> None:
        super().__init__()
        self._client = client
        self._downloads_dir = downloads_dir
        self._library_dir = library_dir
        self._playlist_link = playlist_link
        self._library_paths = library_paths
        self._download_timeout_s = download_timeout_s

    def run(self) -> None:
        from spotify_playlist import SpotifyPlaylistError, fetch_playlist_tracks, find_missing_tracks

        self.progress.emit("Fetching playlist from Spotify…")
        try:
            tracks, truncated = fetch_playlist_tracks(self._playlist_link)
        except SpotifyPlaylistError as exc:
            self.fetch_failed.emit(str(exc))
            return

        if truncated:
            self.progress.emit(
                "Note: this playlist has 50+ tracks — Spotify's public embed "
                "page only exposes the first 50 without an API login."
            )

        missing = find_missing_tracks(tracks, self._library_paths)
        skipped_existing = len(tracks) - len(missing)
        self.progress.emit(
            f"{len(tracks)} tracks in playlist, {skipped_existing} already in your "
            f"library, {len(missing)} to fetch."
        )

        new_paths: list[str] = []
        skipped_no_result = 0

        for i, track in enumerate(missing, 1):
            self.progress.emit(f"[{i}/{len(missing)}] Searching Soulseek: {track.query}")
            try:
                results = self._client.search(track.query)
            except SlskdError as exc:
                self.track_done.emit(track.query, False, str(exc))
                skipped_no_result += 1
                continue

            if not results:
                self.track_done.emit(track.query, False, "No Soulseek results.")
                skipped_no_result += 1
                continue

            best = results[0]
            self.progress.emit(f"[{i}/{len(missing)}] Downloading: {best.display_name}")
            try:
                self._client.download(best)
            except SlskdError as exc:
                self.track_done.emit(track.query, False, str(exc))
                skipped_no_result += 1
                continue

            local_path = None
            deadline = time.monotonic() + self._download_timeout_s
            while time.monotonic() < deadline:
                local_path = find_downloaded_file(self._downloads_dir, best.filename, best.size)
                if local_path:
                    break
                time.sleep(2)

            if not local_path:
                self.track_done.emit(track.query, False, "Timed out waiting for download.")
                skipped_no_result += 1
                continue

            try:
                os.makedirs(self._library_dir, exist_ok=True)
                dest = unique_path(os.path.join(self._library_dir, os.path.basename(local_path)))
                shutil.move(local_path, dest)
            except OSError as exc:
                self.track_done.emit(track.query, False, f"Couldn't move into library: {exc}")
                skipped_no_result += 1
                continue

            new_paths.append(dest)
            self.track_done.emit(track.query, True, os.path.basename(dest))

        self.finished_import.emit(new_paths, skipped_existing, skipped_no_result)


class LyricsQAThread(QThread):
    """Run the optional faster-whisper sync spot-check off the UI thread.

    Model load + a handful of clip transcriptions take a few seconds even
    on a fast CPU — background it like everything else network/CPU-bound.
    """

    progress   = pyqtSignal(str)
    finished_qa = pyqtSignal(list)   # list[lyrics_qa.LineCheck]
    failed     = pyqtSignal(str)

    def __init__(self, audio_path: str, lines: list[tuple[int, str, int]]) -> None:
        super().__init__()
        self._audio_path = audio_path
        self._lines = lines

    def run(self) -> None:
        import lyrics_qa
        try:
            results = lyrics_qa.validate_sync(
                self._audio_path, self._lines, progress_callback=self.progress.emit,
            )
            self.finished_qa.emit(results)
        except lyrics_qa.WhisperUnavailable as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Validation failed: {exc}")
