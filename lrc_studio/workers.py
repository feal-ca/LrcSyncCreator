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
from collections.abc import Callable
from typing import TYPE_CHECKING

import syncedlyrics
from PyQt6.QtCore import QThread, pyqtSignal

from postprocess import unique_path

from soulseek import (
    SlskdConnectionError, SlskdError, SoulseekClient, SoulseekFile,
    find_best_candidates, find_downloaded_file,
)

if TYPE_CHECKING:
    from spotify_playlist import SpotifyTrack


def stop_thread(thread: QThread | None, timeout_ms: int = 2000) -> None:
    """Best-effort shutdown of a possibly-running *thread*, for app close.

    Threads with a ``request_cancel`` (the Playlist* threads) get asked
    to stop cooperatively first. Everything else may be blocked in a
    network call with no way to interrupt it short-of ``terminate()`` —
    normally unsafe (it can leave native state mid-update), but harmless
    here since the whole process is about to exit anyway, and the
    alternative is Qt's "QThread: Destroyed while thread is still
    running" crash.
    """
    if thread is None or not thread.isRunning():
        return
    cancel = getattr(thread, "request_cancel", None)
    if callable(cancel):
        cancel()
    if not thread.wait(timeout_ms):
        thread.terminate()
        thread.wait()


class LyricsDownloaderThread(QThread):
    """Fetch lyrics for a batch of audio files and write ``.lrc`` sidecars.

    Uses ``lyrics_fetch.fetch_best_lyrics`` (duration-aware LRCLIB first,
    other providers after, plain text only as a last resort). Optionally
    embeds the same text into the audio file's tags.

    Signals
    -------
    progress(message, files_done, files_total)
    file_done(path, kind)
        kind is "synced", "plain" or "none" — lets the library recolor one
        row as soon as its lyrics land instead of at the end of the batch.
    finished_batch(success_names, failed_names)
    """

    progress       = pyqtSignal(str, int, int)   # (msg, done, total)
    file_done      = pyqtSignal(str, str)         # (path, "synced" | "plain" | "none")
    finished_batch = pyqtSignal(list, list)       # (success, failed)

    def __init__(self, files: list[str], embed: bool = False) -> None:
        super().__init__()
        self.files = files
        self.embed = embed
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        import lyrics_fetch
        from utils import write_embedded_lyrics

        success: list[str] = []
        failed:  list[str] = []
        total = len(self.files)

        for idx, file_path in enumerate(self.files, 1):
            if self._cancel_event.is_set():
                break
            filename = os.path.basename(file_path)
            self.progress.emit(f"Lyrics: {filename}", idx, total)
            kind = "none"
            try:
                info = lyrics_fetch.read_track_info(file_path)
                result = lyrics_fetch.fetch_best_lyrics(info)
                if result and result.text.strip():
                    lrc_path = os.path.splitext(file_path)[0] + ".lrc"
                    with open(lrc_path, "w", encoding="utf-8") as fh:
                        fh.write(result.text)
                    kind = "synced" if result.synced else "plain"
                    if self.embed:
                        try:
                            write_embedded_lyrics(file_path, result.text)
                        except Exception:
                            pass  # the sidecar is what matters; tags are a bonus
            except Exception:
                # Individual file failures should never abort the whole batch.
                kind = "none"
            (success if kind != "none" else failed).append(filename)
            self.file_done.emit(file_path, kind)

        self.finished_batch.emit(success, failed)


class LyricsCandidatesThread(QThread):
    """Look up lyric candidates for the manual picker (Get Lyrics)."""

    results_ready = pyqtSignal(list)   # list[lyrics_fetch.LyricsCandidate]
    failed        = pyqtSignal(str)

    def __init__(self, artist: str, title: str, duration_s: float | None) -> None:
        super().__init__()
        self._artist = artist
        self._title = title
        self._duration_s = duration_s

    def run(self) -> None:
        import lyrics_fetch
        try:
            self.results_ready.emit(
                lyrics_fetch.search_candidates(self._artist, self._title, self._duration_s)
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class LyricsSearchThread(QThread):
    """One-shot free-text lyrics search across syncedlyrics' providers —
    the fallback inside the manual picker when LRCLIB has nothing."""

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


def wait_for_download(
    client: SoulseekClient,
    downloads_dir: str,
    file: SoulseekFile,
    timeout_s: float,
    cancel_event: threading.Event | None = None,
    progress: pyqtSignal | None = None,
    max_total_s: float = 1800.0,
    poll_s: float = 2.0,
    queued_timeout_s: float | None = -1.0,
    on_status: Callable[[str, int, int | None, float, float], None] | None = None,
    skip_event: threading.Event | None = None,
) -> tuple[bool, str, str]:
    """Poll until *file* has finished downloading into ``downloads_dir``.

    Completion is the client reporting "Completed, Succeeded" with the
    file's local path (which may carry a " (1)" suffix), or, as a fallback,
    the file turning up in ``downloads_dir`` by name and size.

    *timeout_s* is how long to wait **without progress**, not in total:
    every poll also reads the client's transfer record, and any change in
    bytes transferred, state, or queue position resets the clock — a
    24 MB FLAC from a peer uploading at ~130 KB/s legitimately takes over
    three minutes, and used to be reported as failed seconds before it
    finished. A transfer the client marks as failed (rejected, errored,
    timed out, cancelled) ends the wait immediately with its reason.
    *max_total_s* is a backstop so nothing waits forever.

    *queued_timeout_s* replaces *timeout_s* while the transfer sits in a
    remote queue: the playlist flow keeps the default (same as
    *timeout_s*, so a dead queue falls through to the next candidate),
    while a hand-picked download passes None — waiting in a peer's queue
    is normal there, the same as in Nicotine+.

    *cancel_event*, if given, is checked each poll (result "Cancelled.");
    *skip_event* likewise ends the wait with "Skipped." so the caller can
    move on to another source. *progress* (a signal) gets a short status
    string each poll; *on_status* gets ``(state, bytes_done,
    place_in_queue, bytes_per_second, seconds_in_this_state)``.

    Returns ``(success, detail, local_path)``; on failure *detail* is the
    error message and *local_path* is ``""``.
    """
    if queued_timeout_s is not None and queued_timeout_s < 0:
        queued_timeout_s = timeout_s
    start = time.monotonic()
    last_progress_at = start
    last_signature = None
    last_state = None
    state_since = start
    failure = ""
    local_path = None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            break
        if skip_event is not None and skip_event.is_set():
            return False, "Skipped.", ""
        local_path = find_downloaded_file(downloads_dir, file.filename, file.size)
        if local_path:
            break

        now = time.monotonic()
        transfer = client.get_download_transfer(file.username, file.filename) or {}
        state = transfer.get("state") or ""
        reported = transfer.get("localPath")
        if state == "Completed, Succeeded" and reported and os.path.exists(reported):
            local_path = reported
            break
        done = transfer.get("bytesTransferred") or 0
        place = transfer.get("placeInQueue")
        signature = (state, done, place)
        if signature != last_signature:
            last_signature = signature
            last_progress_at = now
        if state != last_state:
            last_state = state
            state_since = now

        # "Completed, Succeeded" just means the file is about to appear —
        # keep polling for it; any other "Completed, …" is a dead transfer.
        if state.startswith("Completed") and "Succeeded" not in state:
            reason = transfer.get("exception") or state.split(",", 1)[-1].strip()
            failure = f"{file.username} couldn't send {file.display_name}: {reason}"
            break
        limit = queued_timeout_s if state.startswith("Queued") else timeout_s
        if limit is not None and now - last_progress_at >= limit:
            failure = (
                f"Download of {file.display_name} stalled "
                f"({state or 'no transfer state'}, no progress for {int(limit)}s)."
            )
            break
        if now - start >= max_total_s:
            failure = f"Gave up on {file.display_name} after {int(max_total_s // 60)} min."
            break

        if on_status is not None:
            on_status(state, done, place, float(transfer.get("averageSpeed") or 0), now - state_since)
        if progress is not None:
            if state == "InProgress" and file.size:
                status = f"downloading {100 * done // file.size}%"
            elif place:
                status = f"{state} (#{place} in queue)"
            else:
                status = state or "waiting…"
            progress.emit(f"{file.display_name}: {status}")
        time.sleep(poll_s)

    if cancel_event is not None and cancel_event.is_set() and not local_path:
        return False, "Cancelled.", ""
    if not local_path:
        return False, failure, ""
    return True, os.path.basename(local_path), local_path


def move_into_library(local_path: str, library_dir: str) -> str:
    """Default finalizer: move a finished download into the library as-is."""
    os.makedirs(library_dir, exist_ok=True)
    dest = unique_path(os.path.join(library_dir, os.path.basename(local_path)))
    shutil.move(local_path, dest)
    return dest


def wait_for_download_and_move(
    client: SoulseekClient,
    downloads_dir: str,
    library_dir: str,
    file: SoulseekFile,
    timeout_s: float,
    cancel_event: threading.Event | None = None,
    progress: pyqtSignal | None = None,
    max_total_s: float = 1800.0,
    poll_s: float = 2.0,
    finalize: Callable[[str], str] | None = None,
) -> tuple[bool, str, str]:
    """``wait_for_download``, then hand the file to *finalize* (convert,
    rename, move — see ``postprocess.finalize_download``), or just move
    it into *library_dir* when no finalizer is given.

    Returns ``(success, detail, path)``: on success, *detail* is a short
    status string (``"Already in library."`` or the final file's
    basename) and *path* is its final location; on failure, *detail* is
    the error message and *path* is ``""``.
    """
    ok, detail, local_path = wait_for_download(
        client, downloads_dir, file, timeout_s, cancel_event=cancel_event,
        progress=progress, max_total_s=max_total_s, poll_s=poll_s,
    )
    if not ok:
        if detail == "Cancelled.":
            return ok, detail, ""
        # It may have already landed and been moved into the library by
        # another operation (e.g. an overlapping run) before we could see
        # it in downloads_dir — check there before giving up.
        already_in_library = find_downloaded_file(library_dir, file.filename, file.size)
        if already_in_library:
            return True, "Already in library.", already_in_library
        return False, detail, ""

    try:
        dest = finalize(local_path) if finalize else move_into_library(local_path, library_dir)
    except OSError as exc:
        return False, f"Downloaded but couldn't move into library: {exc}", ""
    return True, os.path.basename(dest), dest


class SoulseekSearchThread(QThread):
    """Run a Soulseek search without blocking the UI."""

    results_ready = pyqtSignal(list)   # list[SoulseekFile]
    search_failed = pyqtSignal(str)

    def __init__(self, client: SoulseekClient, query: str) -> None:
        super().__init__()
        self._client = client
        self._query = query

    def run(self) -> None:
        try:
            self.results_ready.emit(self._client.search(self._query))
        except SlskdError as exc:
            self.search_failed.emit(str(exc))


class FolderBrowseThread(QThread):
    """List every audio file in a peer's folder (for "Download folder")."""

    results_ready = pyqtSignal(list)   # list[SoulseekFile]
    failed        = pyqtSignal(str)

    def __init__(self, client: SoulseekClient, like: SoulseekFile) -> None:
        super().__init__()
        self._client = client
        self._like = like

    def run(self) -> None:
        try:
            self.results_ready.emit(
                self._client.browse_folder(self._like.username, self._like.folder, like=self._like)
            )
        except Exception as exc:  # noqa: BLE001 — caller falls back to search results
            self.failed.emit(str(exc))


def _fmt_clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt_rate(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    return f"{int(bps // 1024)} KB/s"


def describe_transfer(file: SoulseekFile, state: str, done: int, place: int | None,
                      speed: float, in_state_s: float) -> tuple[str, int, str]:
    """(phase, percent, text) for one poll of a transfer, in plain words."""
    size = file.size or 0
    if state == "InProgress":
        pct = int(100 * done / size) if size else 0
        text = f"{pct}%"
        if speed > 0:
            text += f" · {_fmt_rate(speed)}"
            if size and done < size:
                text += f" · {_fmt_clock((size - done) / speed)} left"
        return "downloading", pct, text
    if state.startswith("Completed"):
        return "downloading", 100, "100%"
    if state == "Queued, Remotely" or (state.startswith("Queued") and place):
        where = f" (#{place})" if place else ""
        return "queued", 0, f"Queued by uploader{where} · {_fmt_clock(in_state_s)}"
    if state == "Initializing":
        return "requesting", 0, "Starting…"
    return "requesting", 0, f"Waiting for the uploader to answer · {_fmt_clock(in_state_s)}"


class TransferThread(QThread):
    """One download: request it, follow it, finalize it (convert / rename /
    move into the library), falling back to other sources when needed.

    *candidates* is the file to get first plus alternatives (other peers'
    copies). A source that rejects the request, errors, never answers, or
    stops sending for ``STALL_TIMEOUT_S`` is cancelled and the
    next one is tried, skipping peers that already failed, up to
    *max_attempts*. *queue_patience_s* is how long to sit in a peer's
    upload queue before moving on; None waits as long as it takes (a file
    the user picked by hand, with no alternatives, the way Nicotine+
    does). ``request_skip()`` moves on to the next source right away.

    ``phase`` values: "requesting", "queued", "downloading",
    "processing" (converting/moving), then finished_transfer.
    """

    status            = pyqtSignal(int, str, int, str)    # (item_id, phase, percent, text)
    source_changed    = pyqtSignal(int, object)            # (item_id, SoulseekFile now being tried)
    finished_transfer = pyqtSignal(int, bool, str, str)   # (item_id, ok, detail, final_path)

    STALL_TIMEOUT_S = 180.0
    MAX_TOTAL_S = 6 * 3600.0

    def __init__(
        self,
        item_id: int,
        client: SoulseekClient,
        downloads_dir: str,
        candidates: list[SoulseekFile] | SoulseekFile,
        finalize: Callable[[str], str],
        convert_lossless: bool = False,
        queue_patience_s: float | None = None,
        max_attempts: int = 3,
    ) -> None:
        super().__init__()
        self.item_id = item_id
        self._client = client
        self._downloads_dir = downloads_dir
        self._candidates = candidates if isinstance(candidates, list) else [candidates]
        self._finalize = finalize
        self._convert_lossless = convert_lossless
        self._queue_patience_s = queue_patience_s
        self._max_attempts = max_attempts
        self._cancel_event = threading.Event()
        self._skip_event = threading.Event()
        self._current: SoulseekFile | None = None

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def request_skip(self) -> None:
        self._skip_event.set()

    def run(self) -> None:
        failed_peers: set[str] = set()
        errors: list[str] = []
        tried = 0
        for file in self._candidates:
            if self._cancel_event.is_set():
                break
            if tried >= self._max_attempts:
                break
            if file.username in failed_peers:
                continue
            tried += 1
            self._current = file
            self._skip_event.clear()
            self.source_changed.emit(self.item_id, file)
            note = f"Trying another copy ({tried} of {self._max_attempts}). " if tried > 1 else ""
            self.status.emit(self.item_id, "requesting", 0, f"{note}Starting…")
            try:
                self._client.download(file)
            except SlskdConnectionError as exc:
                self.finished_transfer.emit(self.item_id, False, str(exc), "")
                return
            except SlskdError as exc:
                failed_peers.add(file.username)
                errors.append(f"{file.username}: {exc}")
                continue

            ok, detail, local_path = wait_for_download(
                self._client, self._downloads_dir, file, self.STALL_TIMEOUT_S,
                cancel_event=self._cancel_event, max_total_s=self.MAX_TOTAL_S,
                queued_timeout_s=self._queue_patience_s, skip_event=self._skip_event,
                on_status=lambda *a, f=file: self.status.emit(self.item_id, *describe_transfer(f, *a)),
            )
            if ok:
                self._finish(file, local_path)
                return
            # Stop an abandoned transfer, or it may still finish later and
            # leave a stray second copy behind.
            self._client.cancel_download(file.username, file.filename)
            if detail == "Cancelled.":
                break
            failed_peers.add(file.username)
            errors.append("skipped by you" if detail == "Skipped." else detail)

        if self._cancel_event.is_set():
            self.finished_transfer.emit(self.item_id, False, "Cancelled.", "")
        elif not errors:
            self.finished_transfer.emit(self.item_id, False, "No source to download from.", "")
        elif len(errors) == 1:
            self.finished_transfer.emit(self.item_id, False, errors[0], "")
        else:
            self.finished_transfer.emit(
                self.item_id, False,
                f"Tried {len(errors)} sources, all failed:\n" + "\n".join(errors), "",
            )

    def _finish(self, file: SoulseekFile, local_path: str) -> None:
        converting = self._convert_lossless and file.is_lossless
        self.status.emit(
            self.item_id, "processing", 100,
            "Converting to Opus…" if converting else "Moving into library…",
        )
        try:
            final_path = self._finalize(local_path)
        except Exception as exc:  # noqa: BLE001
            self.finished_transfer.emit(
                self.item_id, False, f"Downloaded, but couldn't add it to the library: {exc}", "",
            )
            return
        self.finished_transfer.emit(self.item_id, True, os.path.basename(final_path), final_path)


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
    """Search Soulseek for a set of playlist rows, a few at a time.

    Decoupled from downloading: this only fills each row's candidate list
    (best match first) for review. ``CONCURRENCY`` searches run at once;
    the client handles parallel searches fine and it cuts a 30-track playlist
    from several minutes to about one.
    """

    CONCURRENCY = 3

    progress        = pyqtSignal(str)
    track_searching = pyqtSignal(int)            # row_index whose search just started
    track_peers     = pyqtSignal(int, int, int)  # (row_index, peers, files) while it runs
    track_matched   = pyqtSignal(int, list)      # (row_index, list[SoulseekFile])
    track_failed    = pyqtSignal(int, str)       # (row_index, message)
    finished_search = pyqtSignal()

    def __init__(self, client: SoulseekClient, rows: list[tuple[int, "SpotifyTrack"]]) -> None:
        super().__init__()
        self._client = client
        self._rows = rows
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._connection_failures = 0
        self._done = 0

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def _search_one(self, row_index: int, track) -> None:
        if self._cancel_event.is_set():
            self.track_failed.emit(row_index, "Not searched — cancelled.")
            return
        self.track_searching.emit(row_index)
        try:
            results = find_best_candidates(
                self._client, track.artist, track.title,
                on_progress=lambda peers, files: self.track_peers.emit(row_index, peers, files),
                stop_event=self._cancel_event,
            )
            with self._lock:
                self._connection_failures = 0
        except SlskdConnectionError as exc:
            self.track_failed.emit(row_index, str(exc))
            with self._lock:
                self._connection_failures += 1
                if self._connection_failures >= 2 and not self._cancel_event.is_set():
                    self.progress.emit("Soulseek isn't responding, so the search stopped.")
                    self._cancel_event.set()
            return
        except SlskdError as exc:
            self.track_failed.emit(row_index, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — never let one bad row kill the batch
            self.track_failed.emit(row_index, f"Unexpected error: {exc}")
            return
        finally:
            with self._lock:
                self._done += 1
                done = self._done
            self.progress.emit(f"Matched {done} of {len(self._rows)} tracks on Soulseek…")

        if self._cancel_event.is_set() and not results:
            self.track_failed.emit(row_index, "Not searched — cancelled.")
        elif not results:
            self.track_failed.emit(row_index, "No Soulseek results.")
        else:
            self.track_matched.emit(row_index, results)

    def run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.progress.emit(f"Searching Soulseek for {len(self._rows)} tracks…")
        with ThreadPoolExecutor(max_workers=self.CONCURRENCY) as pool:
            for row_index, track in self._rows:
                pool.submit(self._search_one, row_index, track)
        self.finished_search.emit()


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


class LibraryConvertThread(QThread):
    """Convert lossless library files to Opus in place, one at a time.

    ``converted(old, new)`` fires per file so the library row (and the
    loaded song, if it's this one) can follow the new path; failures are
    collected and reported at the end rather than stopping the run.
    """

    progress     = pyqtSignal(str, int, int)   # (message, done, total) — same shape as lyrics progress
    converted    = pyqtSignal(str, str)        # (old_path, new_path)
    finished_all = pyqtSignal(int, list)       # (converted_count, ["name: reason", ...])

    def __init__(self, files: list[str], bitrate_kbps: int = 192) -> None:
        super().__init__()
        self._files = files
        self._bitrate = bitrate_kbps
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        import convert

        done = 0
        failed: list[str] = []
        total = len(self._files)
        for i, path in enumerate(self._files, 1):
            if self._cancel_event.is_set():
                break
            self.progress.emit(f"Converting {os.path.basename(path)}", i, total)
            try:
                new_path = convert.convert_to_opus(path, self._bitrate)
            except Exception as exc:  # noqa: BLE001 — report and keep going
                failed.append(f"{os.path.basename(path)}: {exc}")
                continue
            done += 1
            self.converted.emit(path, new_path)
        self.finished_all.emit(done, failed)
