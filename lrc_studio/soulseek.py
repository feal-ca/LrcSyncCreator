"""soulseek.py — thin REST client for slskd (https://github.com/slskd/slskd).

LRC Studio never speaks the Soulseek protocol itself; slskd owns the P2P
connection and exposes a small REST API that this module wraps. Install it
once via ``scripts/setup_slskd.sh`` — after that, ``app.py`` starts it
itself (via ``scripts/run_slskd.sh``) the first time it's needed.

Endpoint shapes here were confirmed against a live slskd 0.26.0 instance
(search + response schema); the download-completion path is intentionally
schema-tolerant (see ``find_downloaded_file``) since triggering a real
transfer wasn't something to do casually against a stranger's upload slot
during development.
"""

from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from functools import cached_property
from typing import Callable
from urllib.parse import quote

import requests
from rapidfuzz import fuzz

AUDIO_EXTENSIONS = (".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus")

# Below this fuzzy relevance score (0-100) a search result is dropped as
# "probably not the right song" rather than just ranked lower. Looser than
# spotify_playlist.py's 80 (used for tag-derived library entries) because
# this compares against raw, uploader-chosen remote filenames/paths —
# track numbers, quality tags, and folder structure add noise that a
# clean "artist title" string doesn't have.
_DEFAULT_RELEVANCE_THRESHOLD = 65

# At or above this relevance score a candidate counts as clearly the right
# song, and the remaining few points are just filename noise (a track
# number, a folder name) — so it shouldn't outrank a candidate that will
# actually arrive sooner.
_STRONG_MATCH_SCORE = 90

# "Mid-high quality" floor, in kbps. Anything at or above it is good
# enough, and past that point download speed matters more than squeezing
# out more bitrate (a FLAC is ~3x a 320k MP3 and often arrives far later).
# Opus/Vorbis hold up at lower bitrates than MP3/AAC.
_MIN_LOSSY_KBPS = 192
_MIN_EFFICIENT_LOSSY_KBPS = 128
_EFFICIENT_LOSSY_EXTENSIONS = (".opus", ".ogg")
_LOSSLESS_EXTENSIONS = (".flac", ".wav")

# Parenthetical/bracketed asides ("(feat. X)", "[Explicit]") and trailing
# version/remaster suffixes ("- Remastered 2011", "- Radio Edit") are
# common in Spotify metadata but rarely appear verbatim in how peers name
# their files — stripping them before searching casts a wider net, the
# same way manually simplifying a query to just the title does.
_BRACKETED_RE = re.compile(r"[\(\[][^)\]]*[\)\]]")
_NOISE_SUFFIX_RE = re.compile(
    r"\s*-\s*(remaster(ed)?(\s*\d{4})?|radio\s*edit|single\s*version|"
    r"album\s*version|deluxe(\s*edition)?|mono|stereo|live|acoustic|"
    r"bonus\s*track|extended(\s*mix)?|explicit|clean)\s*$",
    re.IGNORECASE,
)


def clean_query_text(text: str) -> str:
    """Strip noise that throws off Soulseek's filename search without
    changing what track *text* actually names."""
    text = _BRACKETED_RE.sub("", text)
    text = _NOISE_SUFFIX_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class SlskdError(Exception):
    """Raised for any slskd connectivity/API failure."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SlskdConnectionError(SlskdError):
    """slskd is unreachable (not running, wrong URL, network down).

    Distinguished from other SlskdErrors so callers looping over many
    tracks can fail fast instead of repeating the same doomed request
    once per track — see the circuit-breakers in PlaylistSearchThread
    and PlaylistDownloadThread.
    """


@dataclass
class SoulseekFile:
    """One candidate file from a search response."""

    username: str
    filename: str      # remote path, peer-native separators (often '\')
    size: int
    bitrate: int | None
    length_s: int | None
    queue_length: int
    has_free_slot: bool
    upload_speed: int | None = None   # peer's advertised upload speed, bytes/s

    @property
    def display_name(self) -> str:
        return self.filename.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        """Remote directory, in the peer's own separators (what slskd's
        browse endpoint expects back)."""
        sep_at = max(self.filename.rfind("\\"), self.filename.rfind("/"))
        return self.filename[:sep_at] if sep_at >= 0 else ""

    @property
    def folder_name(self) -> str:
        """Last component of the remote directory — usually the album."""
        return self.folder.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def extension(self) -> str:
        return os.path.splitext(self.filename)[1].lower().lstrip(".")

    @property
    def format_label(self) -> str:
        """Short quality tag for a results table: "FLAC", "MP3 320", "OPUS 160"."""
        ext = self.extension.upper() or "?"
        if self.is_lossless:
            return ext
        kbps = self.effective_kbps
        return f"{ext} {kbps}" if kbps else ext

    @cached_property
    def haystack(self) -> str:
        """Normalized full path + username, what the results filter matches."""
        return normalize_text(f"{self.filename} {self.username}")

    @property
    def is_lossless(self) -> bool:
        return self.filename.lower().endswith(_LOSSLESS_EXTENSIONS)

    @property
    def effective_kbps(self) -> int | None:
        """Reported bitrate, or one estimated from size and duration."""
        if self.bitrate:
            return self.bitrate
        if self.length_s and self.size:
            return int(self.size * 8 / self.length_s / 1000)
        return None

    @property
    def quality_ok(self) -> bool | None:
        """Mid-high quality or better? None when there's no way to tell."""
        if self.is_lossless:
            return True
        kbps = self.effective_kbps
        if kbps is None:
            return None
        floor = (
            _MIN_EFFICIENT_LOSSY_KBPS
            if self.filename.lower().endswith(_EFFICIENT_LOSSY_EXTENSIONS)
            else _MIN_LOSSY_KBPS
        )
        return kbps >= floor

    @property
    def expected_seconds(self) -> float | None:
        """Rough transfer time from the peer's advertised speed."""
        if self.upload_speed:
            return self.size / self.upload_speed
        return None

    @property
    def quality_label(self) -> str:
        mb = self.size / (1024 * 1024)
        bits = "lossless" if self.is_lossless else (
            f"{self.bitrate}kbps" if self.bitrate else "?kbps"
        )
        slot = "free slot" if self.has_free_slot else f"queued ({self.queue_length})"
        return f"{bits}, {mb:.1f}MB, {slot}"


def availability_key(f: SoulseekFile) -> tuple:
    """Sort key among equally relevant candidates: good-enough quality
    first, then whoever can send it soonest — a free slot over a queue,
    a short queue over a long one, then the shortest expected transfer."""
    quality_rank = {True: 0, None: 1, False: 2}[f.quality_ok]
    expected = f.expected_seconds
    return (
        quality_rank,
        not f.has_free_slot,
        f.queue_length,
        expected if expected is not None else float("inf"),
    )


class SlskdClient:
    """Minimal wrapper around slskd's ``/api/v0`` REST endpoints."""

    def __init__(self, base_url: str, api_key: str, timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key}
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, **kwargs):
        try:
            resp = requests.request(
                method, f"{self.base_url}{path}",
                headers=self._headers, timeout=self.timeout_s, **kwargs,
            )
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            # Short, classified message — the underlying urllib3 exception
            # text can run to hundreds of characters and is useless noise
            # repeated once per track in a batch import.
            raise SlskdConnectionError(
                f"Can't reach slskd at {self.base_url} — is it running? "
                "(Tools -> Settings -> Test Connection)"
            ) from exc
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            detail = ""
            if exc.response is not None:
                try:
                    detail = exc.response.text.strip()
                except Exception:
                    detail = ""
            suffix = f": {detail}" if detail else ""
            raise SlskdError(
                f"slskd returned HTTP {status if status is not None else '?'} "
                f"for {method} {path}{suffix}",
                status_code=status,
            ) from exc
        except requests.RequestException as exc:
            raise SlskdError(f"slskd request failed ({method} {path}): {exc}") from exc

    def check_connection(self) -> tuple[bool, str]:
        """Return (ok, message) — reachable *and* logged in to Soulseek."""
        try:
            data = self._request("GET", "/api/v0/application").json()
        except SlskdError as exc:
            return False, str(exc)
        server = data.get("server", {})
        if server.get("isLoggedIn"):
            return True, "Connected to Soulseek."
        if server.get("isConnecting") or server.get("isLoggingIn"):
            return False, "slskd is still connecting to Soulseek — try again shortly."
        return False, "slskd is running but not logged in to Soulseek (check its credentials)."

    def _post_search(self, query: str, search_timeout_ms: int | None = None):
        """POST a new search, retrying once on a transient 409.

        slskd returns 409 for ``POST /api/v0/searches`` when its own
        connection to the Soulseek server isn't in a connected+logged-in
        state at that instant (see ``SearchesController.Post`` /
        ``SoulseekClient.SearchAsync`` upstream) — including a connection
        that was fine moments ago and is mid-reconnect right now. slskd
        reconnects on its own, so poll ``check_connection`` briefly and
        retry once rather than failing the whole search outright.
        """
        body: dict = {"searchText": query}
        if search_timeout_ms:
            body["searchTimeout"] = int(search_timeout_ms)
        try:
            return self._request("POST", "/api/v0/searches", json=body)
        except SlskdError as exc:
            if exc.status_code != 409:
                raise
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                ok, _ = self.check_connection()
                if ok:
                    break
                time.sleep(1.0)
            return self._request("POST", "/api/v0/searches", json=body)

    def search(
        self, query: str, wait_s: float = 20.0, poll_interval_s: float = 1.0,
        on_progress: Callable[[int, int], None] | None = None,
        stop_event: threading.Event | None = None,
        search_timeout_ms: int | None = None,
    ) -> list[SoulseekFile]:
        """Run a search and block until it completes (or *wait_s* elapses).

        Returns audio-file candidates across all responding peers, best
        candidates first (free upload slot, short queue, higher bitrate).

        Confirmed against a live instance: real searches routinely stay
        ``isComplete: false`` for 40-90s+ (peers keep trickling in), well
        past slskd's own advertised ~15s internal timeout — and its
        ``/responses`` endpoint stays completely empty until the search is
        actually marked complete, even though the search summary reports a
        nonzero response count well before then. Waiting out the full
        natural completion would make every track in a playlist import take
        up to a minute, so instead: if *wait_s* elapses first, explicitly
        stop the search (``PUT .../searches/{id}``) — slskd marks it
        ``Completed, Cancelled`` immediately and ``/responses`` then returns
        whatever was collected so far, which in practice is the vast
        majority of the peers that were ever going to answer.

        *on_progress(peers, files)* is called every poll with slskd's live
        counters, so a UI can show the search filling up even though the
        responses themselves only arrive at the end. Setting *stop_event*
        ends the wait early ("show me what you have").
        """
        resp = self._post_search(query, search_timeout_ms)
        search_id = resp.json()["id"]

        deadline = time.monotonic() + wait_s
        is_complete = False
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                break
            state = self._request("GET", f"/api/v0/searches/{search_id}").json()
            if on_progress is not None:
                on_progress(int(state.get("responseCount") or 0), int(state.get("fileCount") or 0))
            if state.get("isComplete"):
                is_complete = True
                break
            time.sleep(poll_interval_s)

        if not is_complete:
            self._request("PUT", f"/api/v0/searches/{search_id}")

        # /responses lags slightly behind isComplete flipping true (whether
        # that happened naturally or via the stop above) — confirmed against
        # a live instance: reading it in the same instant can still race and
        # come back empty. A few short retries clear that up reliably.
        responses = []
        for _ in range(5):
            responses = self._request("GET", f"/api/v0/searches/{search_id}/responses").json()
            if responses:
                break
            time.sleep(0.5)

        results: list[SoulseekFile] = []
        for r in responses:
            username = r.get("username", "")
            for f in r.get("files", []):
                name = f.get("filename", "")
                if not name.lower().endswith(AUDIO_EXTENSIONS):
                    continue
                results.append(SoulseekFile(
                    username=username,
                    filename=name,
                    size=f.get("size", 0),
                    bitrate=f.get("bitRate"),
                    length_s=f.get("length"),
                    queue_length=r.get("queueLength", 0),
                    has_free_slot=r.get("hasFreeUploadSlot", False),
                    upload_speed=r.get("uploadSpeed"),
                ))

        results.sort(key=availability_key)
        return results

    def browse_folder(self, username: str, directory: str, like: SoulseekFile | None = None) -> list[SoulseekFile]:
        """Every audio file in one of *username*'s shared folders.

        Used for "download the whole folder" (an album) — a search only
        returns the files that matched the query, not their siblings.
        slskd answers ``POST /users/{username}/directory`` with either one
        directory object or a list of them, and file names inside may be
        bare basenames relative to that directory; both are handled. Peer
        stats (queue, free slot, speed) are copied from *like*, the search
        result the folder was reached from, since the browse response
        doesn't carry them.
        """
        data = self._request(
            "POST", f"/api/v0/users/{quote(username, safe='')}/directory",
            json={"directory": directory},
        ).json()
        directories = data if isinstance(data, list) else [data]
        results: list[SoulseekFile] = []
        for d in directories:
            if not isinstance(d, dict):
                continue
            dir_name = d.get("name") or directory
            sep = "\\" if "\\" in dir_name or "/" not in dir_name else "/"
            for f in d.get("files", []) or []:
                name = f.get("filename", "")
                if "\\" not in name and "/" not in name:
                    name = f"{dir_name}{sep}{name}"
                if not name.lower().endswith(AUDIO_EXTENSIONS):
                    continue
                results.append(SoulseekFile(
                    username=username,
                    filename=name,
                    size=f.get("size", 0),
                    bitrate=f.get("bitRate"),
                    length_s=f.get("length"),
                    queue_length=like.queue_length if like else 0,
                    has_free_slot=like.has_free_slot if like else False,
                    upload_speed=like.upload_speed if like else None,
                ))
        results.sort(key=lambda r: r.filename.lower())
        return results

    def download(self, file: SoulseekFile) -> None:
        """Queue *file* for download from its source peer."""
        self._request(
            "POST", f"/api/v0/transfers/downloads/{file.username}",
            json=[{"filename": file.filename, "size": file.size}],
        )

    def cancel_download(self, username: str, filename: str) -> None:
        """Best-effort cancel of an abandoned download, so a stalled
        transfer that's been given up on doesn't finish later and leave a
        stray copy behind. Silently does nothing if it can't."""
        transfer = self.get_download_transfer(username, filename)
        if not transfer or not transfer.get("id"):
            return
        try:
            self._request(
                "DELETE", f"/api/v0/transfers/downloads/{username}/{transfer['id']}",
                params={"remove": "false"},
            )
        except SlskdError:
            pass

    def get_download_transfer(self, username: str, filename: str) -> dict | None:
        """Best-effort lookup of slskd's transfer record for one download.

        Returns the raw record (``state`` e.g. ``"InProgress"`` /
        ``"Queued, Remotely"`` / ``"Completed, Rejected"``,
        ``bytesTransferred``, ``exception``, ``placeInQueue``…), or None if
        it can't be found. Used to tell a slow-but-moving transfer from a
        stalled or failed one; completion itself is still confirmed by the
        file actually landing in the downloads directory (see
        ``find_downloaded_file``).
        """
        try:
            data = self._request("GET", f"/api/v0/transfers/downloads/{username}").json()
        except SlskdError:
            return None
        for directory in data.get("directories", []) or []:
            for f in directory.get("files", []) or []:
                if f.get("filename") == filename:
                    return f
        return None


def normalize_text(text: str) -> str:
    """Lowercase, accents stripped, punctuation/underscores to single
    spaces — so "rosalia" finds "ROSALÍA_-_Malamente.flac"."""
    decomposed = unicodedata.normalize("NFKD", text)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[\W_]+", " ", no_marks.lower()).strip()


@dataclass
class ResultFilter:
    """Client-side narrowing of search results, Nicotine-style.

    *text* is matched against the whole remote path plus the username, so
    searching a title and then typing the artist (or the album, or the
    uploader) here narrows to the right copies. Words are ANDed; a word
    starting with "-" excludes; "double quotes" keep a phrase together.
    """

    text: str = ""
    formats: str = "any"          # "any" | "lossless" | "lossy" | an extension like "mp3"
    min_kbps: int = 0             # lossy only; lossless always passes
    free_slot_only: bool = False
    _include: list[str] = field(default_factory=list, init=False, repr=False)
    _exclude: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.set_text(self.text)

    def set_text(self, text: str) -> None:
        self.text = text
        self._include, self._exclude = [], []
        for quoted, bare in re.findall(r'(-?"[^"]*")|(\S+)', text):
            raw = quoted or bare
            negate = raw.startswith("-") and len(raw) > 1
            word = normalize_text(raw[1:] if negate else raw)
            if word:
                (self._exclude if negate else self._include).append(word)

    @property
    def is_active(self) -> bool:
        return bool(self._include or self._exclude or self.formats != "any"
                    or self.min_kbps or self.free_slot_only)

    def matches(self, f: SoulseekFile) -> bool:
        if self.free_slot_only and not f.has_free_slot:
            return False
        if self.formats == "lossless" and not f.is_lossless:
            return False
        if self.formats == "lossy" and f.is_lossless:
            return False
        if self.formats not in ("any", "lossless", "lossy") and f.extension != self.formats:
            return False
        if self.min_kbps and not f.is_lossless:
            kbps = f.effective_kbps
            if kbps is None or kbps < self.min_kbps:
                return False
        hay = f.haystack
        padded = f" {hay} "
        for word in self._include:
            if word not in hay:
                return False
        for word in self._exclude:
            # Whole-word for exclusions, so "-live" doesn't drop "Oliver".
            if f" {word} " in padded:
                return False
        return True


def find_downloaded_file(downloads_dir: str, remote_filename: str, expected_size: int) -> str | None:
    """Search *downloads_dir* for the local copy of a completed download.

    slskd preserves the peer's directory structure under its downloads
    directory in a way that varies by version/config, so rather than
    depend on that layout, this matches on basename + exact size — cheap
    and reliable for the file counts a single download involves.
    """
    target_name = remote_filename.replace("\\", "/").rsplit("/", 1)[-1]
    for root, _dirs, files in os.walk(downloads_dir):
        for fname in files:
            if fname == target_name:
                full = os.path.join(root, fname)
                try:
                    if os.path.getsize(full) == expected_size:
                        return full
                except OSError:
                    continue
    return None


def _relevance_score(remote_path: str, artist: str, title: str) -> int:
    """Fuzzy-match *remote_path* against (artist, title).

    Both use a partial-ratio style match (best-aligned substring, not a
    whole-string comparison) since a remote filename is never just the
    title or artist alone — it's a track number, the title, a quality
    tag, and often the artist and extension all run together with no
    separator token-based matching can rely on ("...KarmaPolice.mp3").
    The artist is checked against the *whole* path rather than just the
    filename, since peers often nest tracks under an Artist/Album folder
    instead of naming the file itself "Artist - Title.mp3". Both have to
    hold up — a great title match on a completely different artist (a
    cover, a same-named song) shouldn't outrank a real match.
    """
    path_lower = remote_path.replace("\\", "/").lower()
    path_norm = _normalize_for_match(path_lower)
    filename_norm = _normalize_for_match(os.path.splitext(path_lower.rsplit("/", 1)[-1])[0])
    artist_score = fuzz.partial_ratio(_normalize_for_match(artist.lower()), path_norm) if artist else 100
    title_score = fuzz.partial_ratio(_normalize_for_match(title.lower()), filename_norm) if title else 100
    return int(min(artist_score, title_score))


def _normalize_for_match(text: str) -> str:
    """Punctuation and underscores to single spaces ("01-ma_beauté" ->
    "01 ma beauté"), so separators peers use instead of spaces don't cost
    a correct match points."""
    return re.sub(r"[\W_]+", " ", text).strip()


def _filter_and_rank(
    results: list[SoulseekFile], artist: str, title: str, min_score: int,
) -> list[SoulseekFile]:
    scored = [
        (_relevance_score(f.filename, artist, title), f) for f in results
    ]
    scored = [pair for pair in scored if pair[0] >= min_score]
    # Strong matches all tie on relevance, so availability decides among
    # them; weaker matches still rank by score first, so a loosely matched
    # wrong song never outranks a likelier one just by being faster.
    scored.sort(key=lambda pair: (
        -min(pair[0], _STRONG_MATCH_SCORE),
        availability_key(pair[1]),
        -pair[0],
    ))
    return [f for _score, f in scored]


# Separators and punctuation that never help a Soulseek token match ("Cap.
# 2: Havia de Passar" with "2:" as a token matches nothing). Apostrophes
# stay: they're part of words like "L'Amour".
_QUERY_PUNCT_RE = re.compile(r"[\s,;:!?¿¡\"“”()\[\]{}/\\|+*&·.]+|\s-+\s|^-+|-+$")
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|&|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b)\s*", re.IGNORECASE,
)

# Automatic matching doesn't need slskd's full 15 s: most peers answer in
# the first few seconds, and a rare track that needs longer usually isn't
# there at all.
AUTO_SEARCH_TIMEOUT_MS = 10_000


def query_text(text: str) -> str:
    """*text* cleaned for use as Soulseek search tokens."""
    text = clean_query_text(text)
    return re.sub(r"\s+", " ", _QUERY_PUNCT_RE.sub(" ", text)).strip()


def primary_artist(artist: str) -> str:
    """First credited artist: "La Fúmiga, The Tyets" -> "La Fúmiga"."""
    parts = [p for p in _ARTIST_SPLIT_RE.split(artist or "") if p.strip()]
    return parts[0].strip() if parts else (artist or "").strip()


def build_queries(artist: str, title: str) -> list[str]:
    """Queries to try, most precise first, without duplicates.

    Artist + title comes first: Soulseek only returns files whose path
    contains every word, so it goes straight to the right copies. A title
    on its own floods for common names ("Your Love" brings back 9,000+
    files from 250 peers, past slskd's response limit, which can push the
    right artist's copies out entirely). Title alone and artist alone are
    still tried next, for peers whose folders spell the artist differently.
    """
    a = query_text(primary_artist(artist))
    t = query_text(title)
    title_words = {w.lower() for w in t.split()}
    extra = [w for w in a.split() if w.lower() not in title_words]
    combined = " ".join(extra + t.split())
    queries: list[str] = []
    for q in (combined, t, a):
        if q and q.lower() not in (x.lower() for x in queries):
            queries.append(q)
    return queries


def find_best_candidates(
    client: SlskdClient, artist: str, title: str, min_score: int = _DEFAULT_RELEVANCE_THRESHOLD,
    on_progress: Callable[[int, int], None] | None = None,
    stop_event: threading.Event | None = None,
) -> list[SoulseekFile]:
    """Search Soulseek for (*artist*, *title*), automatically, without a
    human in the loop to judge results by eye.

    Tries ``build_queries`` in order and stops at the first one that
    yields any candidate above *min_score*; within each attempt candidates
    rank by relevance first, so a loosely matched wrong song never
    outranks a real one on availability or bitrate alone.
    """
    for query in build_queries(artist, title):
        if stop_event is not None and stop_event.is_set():
            break
        results = client.search(
            query, wait_s=AUTO_SEARCH_TIMEOUT_MS / 1000 + 4,
            on_progress=on_progress, stop_event=stop_event,
            search_timeout_ms=AUTO_SEARCH_TIMEOUT_MS,
        )
        ranked = _filter_and_rank(results, artist, title, min_score)
        if ranked:
            return ranked
    return []
