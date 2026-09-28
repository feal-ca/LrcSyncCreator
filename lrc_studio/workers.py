"""
workers.py — QThread subclasses for network-bound and CPU-bound tasks.

Keeping all background work off the main thread prevents UI freezes even
when the lyrics API is slow or the file system stalls.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from typing import TYPE_CHECKING

import mutagen
import syncedlyrics
from PyQt6.QtCore import QThread, pyqtSignal

from soulseek import (
    SlskdClient, SlskdConnectionError, SlskdError, SoulseekFile, find_downloaded_file,
)

if TYPE_CHECKING:
    from spotify_playlist import SpotifyTrack


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


def wait_for_download_and_move(
    client: SlskdClient,
    downloads_dir: str,
    library_dir: str,
    file: SoulseekFile,
    timeout_s: float,
    cancel_event: threading.Event | None = None,
    progress: pyqtSignal | None = None,
) -> tuple[bool, str, str]:
    """Poll for *file* to land in ``downloads_dir``, then move it into the library.

    slskd hands off the actual peer-to-peer transfer; this just polls for
    the file's arrival (see ``find_downloaded_file``) rather than trusting
    any specific transfer-state schema to signal completion. Shared by
    ``SoulseekDownloadThread`` (single-track) and ``PlaylistDownloadThread``
    (batch) so a fix to this logic only has to land once.

    *cancel_event*, if given, is checked each poll so a batch download can
    be interrupted mid-wait. *progress*, if given, is emitted each poll
    with slskd's own transfer-state string (skipped for batch downloads to
    avoid one extra API call per row per poll tick).

    Returns ``(success, detail, path)``: on success, *detail* is a short
    status string (``"Already in library."`` or the moved file's basename)
    and *path* is its final location; on failure, *detail* is the error
    message and *path* is ``""``.
    """
    deadline = time.monotonic() + timeout_s
    local_path = None
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            break
        local_path = find_downloaded_file(downloads_dir, file.filename, file.size)
        if local_path:
            break
        if progress is not None:
            state = client.get_download_state(file.username, file.filename)
            progress.emit(f"{file.display_name}: {state or 'waiting…'}")
        time.sleep(2)

    if cancel_event is not None and cancel_event.is_set() and not local_path:
        return False, "Cancelled.", ""

    if not local_path:
        # It may have already landed and been moved into the library by
        # another operation (e.g. an overlapping run) before we could see
        # it in downloads_dir — check there before giving up.
        already_in_library = find_downloaded_file(library_dir, file.filename, file.size)
        if already_in_library:
            return True, "Already in library.", already_in_library
        return False, f"Timed out waiting for {file.display_name}.", ""

    try:
        os.makedirs(library_dir, exist_ok=True)
        dest = unique_path(os.path.join(library_dir, os.path.basename(local_path)))
        shutil.move(local_path, dest)
    except OSError as exc:
        return False, f"Downloaded but couldn't move into library: {exc}", ""

    return True, os.path.basename(dest), dest


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

        ok, detail, path = wait_for_download_and_move(
            self._client, self._downloads_dir, self._library_dir, self._file,
            self._timeout_s, progress=self.progress,
        )
        if ok:
            self.finished_ok.emit(path)
        else:
            self.finished_fail.emit(detail)


class PlaylistFetchThread(QThread):
    """Fetch a Spotify track/album/playlist's tracks and dedup against the
    local library.

    Split out from the old ``SpotifyImportThread`` so fetching is a quick,
    separate step the Download tab can react to before any Soulseek
    traffic happens — the table populates with "already have" vs
    "missing" immediately, before a single search is fired off.
    """

    fetched      = pyqtSignal(list, list, bool)   # (all_tracks, missing_tracks, truncated)
    fetch_failed = pyqtSignal(str)

    def __init__(self, spotify_link: str, library_paths: list[str]) -> None:
        super().__init__()
        self._spotify_link = spotify_link
        self._library_paths = library_paths

    def run(self) -> None:
        from spotify_playlist import SpotifyLinkError, fetch_spotify_tracks, find_missing_tracks

        try:
            tracks, truncated = fetch_spotify_tracks(self._spotify_link)
        except SpotifyLinkError as exc:
            self.fetch_failed.emit(str(exc))
            return

        missing = find_missing_tracks(tracks, self._library_paths)
        self.fetched.emit(tracks, missing, truncated)


class PlaylistSearchThread(QThread):
    """Search Soulseek for a set of playlist rows, off the UI thread.

    Deliberately decoupled from downloading: this only populates each
    row's candidate list (best match first) so the Download tab can show
    it for review before anything is actually downloaded.
    """

    progress        = pyqtSignal(str)
    track_searching = pyqtSignal(int)        # row_index about to be searched — this thread is sequential
    track_matched   = pyqtSignal(int, list)  # (row_index, list[SoulseekFile])
    track_failed    = pyqtSignal(int, str)   # (row_index, message)
    finished_search = pyqtSignal()

    def __init__(self, client: SlskdClient, rows: list[tuple[int, "SpotifyTrack"]]) -> None:
        super().__init__()
        self._client = client
        self._rows = rows
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        total = len(self._rows)
        consecutive_connection_failures = 0

        for i, (row_index, track) in enumerate(self._rows, 1):
            if self._cancel_event.is_set():
                self.progress.emit(f"Search cancelled — {total - i + 1} track(s) skipped.")
                self._mark_unprocessed(self._rows[i - 1:], "Not searched — cancelled.")
                break

            self.progress.emit(f"[{i}/{total}] Searching Soulseek: {track.query}")
            self.track_searching.emit(row_index)
            try:
                results = self._client.search(track.query)
                consecutive_connection_failures = 0
            except SlskdConnectionError as exc:
                consecutive_connection_failures += 1
                self.track_failed.emit(row_index, str(exc))
                if consecutive_connection_failures >= 2:
                    self.progress.emit(
                        f"slskd isn't responding — stopping search "
                        f"({total - i} remaining track(s) skipped)."
                    )
                    self._mark_unprocessed(
                        self._rows[i:], "Not searched — slskd stopped responding."
                    )
                    break
                continue
            except SlskdError as exc:
                self.track_failed.emit(row_index, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 — never let one bad row kill the batch
                self.track_failed.emit(row_index, f"Unexpected error: {exc}")
                continue

            if not results:
                self.track_failed.emit(row_index, "No Soulseek results.")
                continue

            self.track_matched.emit(row_index, results)

        self.finished_search.emit()

    def _mark_unprocessed(self, remaining: list[tuple[int, "SpotifyTrack"]], reason: str) -> None:
        """Resolve every row the loop never reached so none stay stuck on
        "Searching…" forever — every row was set to that status up front,
        before this thread runs one at a time, so an early exit (circuit
        breaker or cancel) has to explicitly walk them back."""
        for row_index, _track in remaining:
            self.track_failed.emit(row_index, reason)


class PlaylistDownloadThread(QThread):
    """Download a set of already-chosen Soulseek candidates for playlist rows.

    Rows and their candidates are picked ahead of time (auto-matched, or
    overridden by the user via "Change Match") — this thread only
    executes the actual transfer, reusing the same move/already-in-
    library-fallback logic as ``SoulseekDownloadThread``.
    """

    progress          = pyqtSignal(str)
    track_progress    = pyqtSignal(int, int)                # (rows done, rows total)
    track_downloading = pyqtSignal(int)                      # row_index about to be downloaded — sequential
    track_done        = pyqtSignal(int, bool, str, str)      # (row_index, success, detail, local_path)
    finished_downloads = pyqtSignal()

    def __init__(
        self,
        client: SlskdClient,
        downloads_dir: str,
        library_dir: str,
        rows: list[tuple[int, SoulseekFile]],
        timeout_s: float = 180.0,
    ) -> None:
        super().__init__()
        self._client = client
        self._downloads_dir = downloads_dir
        self._library_dir = library_dir
        self._rows = rows
        self._timeout_s = timeout_s
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        total = len(self._rows)
        consecutive_connection_failures = 0

        for i, (row_index, file) in enumerate(self._rows, 1):
            if self._cancel_event.is_set():
                self.progress.emit(f"Cancelled — {total - i + 1} track(s) skipped.")
                self._mark_unprocessed(self._rows[i - 1:], "Not downloaded — cancelled.")
                break

            self.track_progress.emit(i - 1, total)
            self.progress.emit(f"[{i}/{total}] Downloading: {file.display_name}")
            self.track_downloading.emit(row_index)
            try:
                self._client.download(file)
                consecutive_connection_failures = 0
            except SlskdConnectionError as exc:
                consecutive_connection_failures += 1
                self.track_done.emit(row_index, False, str(exc), "")
                if consecutive_connection_failures >= 2:
                    self.progress.emit(
                        f"slskd isn't responding — stopping "
                        f"({total - i} remaining track(s) skipped)."
                    )
                    self._mark_unprocessed(
                        self._rows[i:], "Not downloaded — slskd stopped responding."
                    )
                    break
                continue
            except SlskdError as exc:
                self.track_done.emit(row_index, False, str(exc), "")
                continue
            except Exception as exc:  # noqa: BLE001 — never let one bad row kill the batch
                self.track_done.emit(row_index, False, f"Unexpected error: {exc}", "")
                continue

            try:
                ok, detail, path = wait_for_download_and_move(
                    self._client, self._downloads_dir, self._library_dir, file,
                    self._timeout_s, cancel_event=self._cancel_event,
                )
            except Exception as exc:  # noqa: BLE001
                self.track_done.emit(row_index, False, f"Unexpected error: {exc}", "")
                continue

            if not ok and detail == "Cancelled.":
                self.track_done.emit(row_index, False, detail, "")
                self._mark_unprocessed(self._rows[i:], "Not downloaded — cancelled.")
                break

            self.track_done.emit(row_index, ok, detail, path)

        self.track_progress.emit(total, total)
        self.finished_downloads.emit()

    def _mark_unprocessed(self, remaining: list[tuple[int, SoulseekFile]], reason: str) -> None:
        """Resolve every row the loop never reached so none stay stuck on
        "Downloading…" forever — see PlaylistSearchThread._mark_unprocessed
        for why this is needed."""
        for row_index, _file in remaining:
            self.track_done.emit(row_index, False, reason, "")


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
