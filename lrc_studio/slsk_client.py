"""slsk_client.py — LRC Studio's own Soulseek client, the way Nicotine+
works: the app speaks the Soulseek protocol itself (through the aioslsk
library) instead of driving a separate slskd daemon. Search results stream
in as each peer answers.

``SlskClient`` is a blocking, thread-safe facade with the surface the
download engine already used (check_connection, search, browse_folder,
download, cancel_download, get_download_transfer, with slskd-style state
strings) plus streaming search (start_search / stop_search). aioslsk runs
on its own asyncio loop in a daemon thread; every public method marshals
onto that loop. Callbacks passed to start_search() run ON THE LOOP THREAD:
keep them short (emit a Qt signal) and never block in them.

Qt-free. aioslsk 1.7.1 is pinned: this module works around two of its bugs
(the browse ticket, env-var parsing of its settings).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from aioslsk.client import SoulSeekClient
from aioslsk.commands import PeerGetDirectoryContentCommand
from aioslsk.events import ConnectionStateChangedEvent, KickedEvent, SearchResultEvent
from aioslsk.exceptions import (
    AioSlskException, AuthenticationError, ConnectionFailedError, ListeningConnectionFailedError,
    NetworkError,
)
from aioslsk.network.connection import CloseReason, ConnectionState, PeerConnection, ServerConnection
from aioslsk.network.network import ExpectedResponse
from aioslsk.protocol.messages import PeerDirectoryContentsReply, PeerDirectoryContentsRequest
from aioslsk.protocol.primitives import AttributeKey
from aioslsk.settings import (
    CredentialsSettings, ListeningSettings, NetworkSettings, ReconnectSettings, SearchSendSettings,
    SearchSettings, ServerSettings, Settings, SharedDirectorySettingEntry, SharesSettings,
    TransferLimitSettings, TransfersSettings, UpnpSettings,
)
from aioslsk.transfer.model import FailReason, Transfer, TransferDirection, TransferState

from soulseek import AUDIO_EXTENSIONS, SlskdConnectionError, SlskdError, SoulseekFile, availability_key

# aioslsk logs routine peer failures at INFO/WARNING; with no handler configured
# Python's last-resort handler would print every WARNING to stderr.
logging.getLogger("aioslsk").setLevel(logging.ERROR)

_LOGIN_REASONS = {
    "INVALIDPASS": "Wrong Soulseek password for this username.",
    "INVALIDUSERNAME": "That Soulseek username isn't allowed.",
    "INVALIDVERSION": "The Soulseek server rejected this client version.",
}
_DL = TransferDirection.DOWNLOAD


class _Settings(Settings):
    """aioslsk.Settings is a pydantic BaseSettings: without this, env vars such
    as DEBUG=1 or NETWORK=... are parsed into it (DEBUG=1 crashes it)."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **_other_sources):
        return (init_settings,)


class _DirectoryCommand(PeerGetDirectoryContentCommand):
    """aioslsk 1.7.1 bug: execute() calls build_expected_response() before
    send() picks the ticket, so the stock command waits for ticket=None and
    always times out. Pick the ticket first and match on it alone."""

    def build_expected_response(self, client):
        self._ticket = next(client.ticket_generator)
        return ExpectedResponse(PeerConnection, PeerDirectoryContentsReply.Request,
                                peer=self.username, fields={"ticket": self._ticket})

    async def send(self, client):
        await client.network.send_peer_messages(
            self.username, PeerDirectoryContentsRequest.Request(self._ticket, self.directory))


@dataclass
class _LiveSearch:
    query: str
    on_results: Callable[[list[SoulseekFile]], None] | None
    max_files: int
    batch_s: float
    peers: set[str] = field(default_factory=set)
    files: list[SoulseekFile] = field(default_factory=list)
    pending: list[SoulseekFile] = field(default_factory=list)
    flush: asyncio.TimerHandle | None = None


def to_soulseek_files(result) -> list[SoulseekFile]:
    """One peer's search reply as SoulseekFiles (audio only; locked files skipped)."""
    out = []
    for item in result.shared_items:          # locked_results can't be downloaded
        if not item.filename.lower().endswith(AUDIO_EXTENSIONS):
            continue
        attrs = item.get_attribute_map()
        out.append(SoulseekFile(
            username=result.username, filename=item.filename, size=item.filesize,
            bitrate=attrs.get(AttributeKey.BITRATE) or None,
            length_s=attrs.get(AttributeKey.DURATION) or None,
            queue_length=result.queue_size, has_free_slot=result.has_free_slots,
            upload_speed=result.avg_speed or None,
        ))
    return out


def listing_to_files(username: str, directories, like: SoulseekFile | None) -> list[SoulseekFile]:
    """A peer's folder listing (aioslsk DirectoryData) as SoulseekFiles.
    Names inside may be bare basenames relative to the folder; peer stats
    (queue, free slot, speed) are copied from *like*, the search result the
    folder was reached from, since a listing doesn't carry them."""
    results = []
    for d in directories:
        sep = "\\" if "\\" in d.name or "/" not in d.name else "/"
        for f in d.files:
            name = f.filename if ("\\" in f.filename or "/" in f.filename) else f"{d.name}{sep}{f.filename}"
            if not name.lower().endswith(AUDIO_EXTENSIONS):
                continue
            attrs = f.get_attribute_map()
            results.append(SoulseekFile(
                username=username, filename=name, size=f.filesize,
                bitrate=attrs.get(AttributeKey.BITRATE) or None,
                length_s=attrs.get(AttributeKey.DURATION) or None,
                queue_length=like.queue_length if like else 0,
                has_free_slot=like.has_free_slot if like else False,
                upload_speed=like.upload_speed if like else None))
    return sorted(results, key=lambda r: r.filename.lower())


def read_slskd_credentials(path: str) -> tuple[str, str] | None:
    """The Soulseek username/password from an old slskd.yml (to carry them
    over once), or None. Only the top-level ``soulseek:`` block counts; the
    web UI's own username/password further down are someone else's."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    found: dict[str, str] = {}
    in_block = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_block = line.split(":", 1)[0].strip() == "soulseek"
            continue
        if not in_block or line[: len(line) - len(line.lstrip())] not in ("  ", "    ", "\t"):
            continue
        key, _, value = line.strip().partition(":")
        if key in ("username", "password") and key not in found:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            found[key] = value
    user, password = found.get("username", ""), found.get("password", "")
    return (user, password) if user and password else None


def slskd_state(t: Transfer) -> str:
    """aioslsk transfer state -> the slskd state strings workers.py expects."""
    s = t.state.VALUE
    if s == TransferState.INITIALIZING:
        return "Initializing"
    if s == TransferState.DOWNLOADING:
        return "InProgress"
    if s == TransferState.COMPLETE:
        return "Completed, Succeeded"
    if s == TransferState.ABORTED:
        return "Completed, Cancelled"
    if s == TransferState.FAILED and t.fail_reason is not None:
        return "Completed, Errored" if t.fail_reason == FailReason.FILE_READ_ERROR else "Completed, Rejected"
    # VIRGIN, QUEUED, INCOMPLETE, PAUSED, FAILED-without-reason: aioslsk retries these
    return "Queued, Remotely" if t.remotely_queued else "Queued, Locally"


class SlskClient:
    PLACE_POLL_S = 60.0

    def __init__(self, username: str, password: str, downloads_dir: str, *, listen_port: int = 2234,
                 upnp: bool = True, shared_dirs: tuple[str, ...] = (), upload_slots: int = 2,
                 server: tuple[str, int] = ("server.slsknet.org", 2416)) -> None:
        self._settings = _Settings(
            credentials=CredentialsSettings(username=username, password=password),
            network=NetworkSettings(
                server=ServerSettings(hostname=server[0], port=server[1],
                                      reconnect=ReconnectSettings(auto=False)),  # we reconnect ourselves
                listening=ListeningSettings(port=listen_port, obfuscated_port=0),
                upnp=UpnpSettings(enabled=upnp)),
            searches=SearchSettings(send=SearchSendSettings(store_results=False)),
            shares=SharesSettings(download=os.path.abspath(downloads_dir), scan_on_start=bool(shared_dirs),
                                  directories=[SharedDirectorySettingEntry(path=p) for p in shared_dirs]),
            transfers=TransfersSettings(limits=TransferLimitSettings(upload_slots=upload_slots)),
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="slsk-loop", daemon=True)
        self._client: SoulSeekClient | None = None
        self._searches: dict[int, _LiveSearch] = {}
        self._place_asked: dict[int, float] = {}
        self._status = "Not connected to Soulseek."
        self._fatal = False          # bad credentials / kicked: don't auto-retry
        self._closing = False
        self._reconnect_task: asyncio.Task | None = None

    @property
    def listen_port(self) -> int:
        return self._settings.network.listening.port

    # -- loop plumbing ---------------------------------------------------------
    def start_loop(self) -> None:
        """Start the background asyncio loop (start() does this itself)."""
        if not self._thread.is_alive():
            self._thread.start()

    def _run(self, coro, timeout: float | None = 30.0):
        if threading.current_thread() is self._thread:
            raise RuntimeError("blocking SlskClient call made from its own loop thread")
        if not self._thread.is_alive():
            coro.close()
            raise SlskdConnectionError(self._status)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise SlskdError("Soulseek operation timed out.") from None

    def _call(self, fn, timeout: float = 10.0):
        async def wrapper():
            return fn()
        return self._run(wrapper(), timeout)

    def _require_session(self) -> SoulSeekClient:
        if self._client is None or self._client.session is None:
            raise SlskdConnectionError(self._status)
        return self._client

    # -- lifecycle -------------------------------------------------------------
    def start(self, timeout_s: float = 45.0) -> None:
        """Start the loop thread, bind the listening port, connect and log in.
        Raises SlskdError (bad credentials, port in use) or SlskdConnectionError."""
        os.makedirs(self._settings.shares.download, exist_ok=True)
        self.start_loop()
        self._run(self._a_start(), timeout_s)

    async def _a_start(self) -> None:
        if self._client is None:
            self._client = SoulSeekClient(self._settings)
            ev = self._client.events          # listeners are weakly held: bound methods only
            ev.register(SearchResultEvent, self._on_search_result)
            ev.register(KickedEvent, self._on_kicked)
            ev.register(ConnectionStateChangedEvent, self._on_conn_state)
            await self._client.start(connect=False)
        await self._a_connect()

    async def _a_connect(self) -> None:
        c = self._client
        if c.session is not None:
            return
        net = c.network
        if not net.listening_connections[0] or net.listening_connections[0].state != ConnectionState.CONNECTED:
            try:
                await net.connect_listening_ports()
            except ListeningConnectionFailedError as exc:
                self._status = f"Port {self._settings.network.listening.port} is in use (Nicotine+ or slskd running?)."
                raise SlskdError(self._status) from exc
        self._status = "Connecting to Soulseek…"
        try:
            if net.server_connection.state != ConnectionState.CONNECTED:
                await net.connect_server()
            async with asyncio.timeout(30):   # login() itself would wait SERVER_READ_TIMEOUT (600 s)
                await c.login()
        except AuthenticationError as exc:
            self._fatal = True
            self._status = _LOGIN_REASONS.get(exc.reason, f"Soulseek login refused: {exc.reason}")
            await net.disconnect_server()
            raise SlskdError(self._status) from exc
        except (ConnectionFailedError, TimeoutError, AioSlskException) as exc:
            self._status = "Can't reach the Soulseek server (network down?)."
            await net.disconnect_server()
            raise SlskdConnectionError(self._status) from exc
        self._fatal = False
        self._status = "Connected to Soulseek."

    def _on_kicked(self, _event: KickedEvent) -> None:
        self._fatal = True
        self._status = "Logged out: this account signed in somewhere else."

    def _on_conn_state(self, event: ConnectionStateChangedEvent) -> None:
        if not isinstance(event.connection, ServerConnection) or event.state != ConnectionState.CLOSED:
            return
        if self._closing or self._fatal or event.close_reason in (CloseReason.REQUESTED, CloseReason.CONNECT_FAILED):
            return
        if not self._status.startswith("Logged out"):
            self._status = "Lost the Soulseek connection; reconnecting…"
        if self._reconnect_task is None or self._reconnect_task.done():
            delay = 60.0 if event.close_reason == CloseReason.EOF else 10.0
            self._reconnect_task = self._loop.create_task(self._a_reconnect(delay))

    async def _a_reconnect(self, delay: float) -> None:
        while not (self._closing or self._fatal):
            await asyncio.sleep(delay)        # the server bans IPs that reconnect too fast
            try:
                await self._a_connect()
                return
            except SlskdConnectionError:
                delay = min(delay * 2, 300.0)
            except SlskdError:
                return

    def check_connection(self) -> tuple[bool, str]:
        logged_in = self._client is not None and self._client.session is not None
        return (True, "Connected to Soulseek.") if logged_in else (False, self._status)

    def ensure_connected(self, timeout_s: float = 45.0) -> None:
        """(Re)connect now if not logged in. Raises like start()."""
        if not self.check_connection()[0]:
            self.start(timeout_s)

    @property
    def credentials_refused(self) -> bool:
        """Wrong password or kicked: retrying won't help until settings change."""
        return self._fatal

    def close(self, timeout_s: float = 10.0) -> None:
        if not self._thread.is_alive():
            return
        self._closing = True

        async def stop():
            if self._reconnect_task:
                self._reconnect_task.cancel()
            if self._client:
                await self._client.stop()
            await asyncio.sleep(0.25)          # let transports detach (aioslsk issue #534)
        try:
            self._run(stop(), timeout_s)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout_s)

    # -- search ----------------------------------------------------------------
    def start_search(self, query: str, on_results: Callable[[list[SoulseekFile]], None] | None = None,
                     *, max_files: int = 5000, batch_s: float = 0.25) -> int:
        """Send a network search; on_results(batch) fires on the loop thread at
        most every batch_s while peers answer. Returns the search id."""
        async def go():
            req = await self._require_session().searches.search(query)
            self._searches[req.ticket] = _LiveSearch(query, on_results, max_files, batch_s)
            return req.ticket
        return self._run(go())

    def _on_search_result(self, event: SearchResultEvent) -> None:
        live = self._searches.get(event.query.ticket)
        if live is None or len(live.files) >= live.max_files:
            return
        files = to_soulseek_files(event.result)[: live.max_files - len(live.files)]
        live.peers.add(event.result.username)
        live.files.extend(files)
        if live.on_results and files:
            live.pending.extend(files)
            if live.flush is None:
                live.flush = self._loop.call_later(live.batch_s, self._flush, live)

    def _flush(self, live: _LiveSearch) -> None:
        live.flush, batch, live.pending = None, live.pending, []
        if batch and live.on_results:
            try:
                live.on_results(batch)
            except Exception:  # noqa: BLE001 — a UI bug must not kill the loop
                logging.getLogger(__name__).exception("on_results callback failed")

    def search_stats(self, search_id: int) -> tuple[int, int]:
        live = self._searches.get(search_id)
        return (len(live.peers), len(live.files)) if live else (0, 0)

    def stop_search(self, search_id: int) -> list[SoulseekFile]:
        """Stop accepting results (late replies are dropped). Returns everything
        collected, best first."""
        def stop():
            live = self._searches.pop(search_id, None)
            if self._client:
                self._client.searches.requests.pop(search_id, None)
            if live is None:
                return []
            if live.flush:
                live.flush.cancel()
                self._flush(live)
            return sorted(live.files, key=availability_key)
        return self._call(stop)

    def search(self, query, wait_s=20.0, poll_interval_s=0.5, on_progress=None, stop_event=None,
               search_timeout_ms=None, settle_s: float | None = None) -> list[SoulseekFile]:
        """Blocking search (the Spotify matcher and Change match use it). settle_s: return early
        once results have arrived and none came in for that long."""
        if search_timeout_ms:
            wait_s = min(wait_s, search_timeout_ms / 1000)
        sid = self.start_search(query)
        deadline, last_count, last_change = time.monotonic() + wait_s, 0, time.monotonic()
        try:
            while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
                peers, files = self.search_stats(sid)
                if on_progress:
                    on_progress(peers, files)
                if files != last_count:
                    last_count, last_change = files, time.monotonic()
                elif settle_s and files and time.monotonic() - last_change >= settle_s:
                    break
                time.sleep(poll_interval_s)
        finally:
            results = self.stop_search(sid)
        return results

    # -- browse ----------------------------------------------------------------
    def browse_folder(self, username: str, directory: str, like: SoulseekFile | None = None,
                      timeout_s: float = 60.0) -> list[SoulseekFile]:
        async def go():
            client = self._require_session()
            try:   # execute()'s timeout doesn't cover connecting to the peer
                async with asyncio.timeout(timeout_s):
                    return await client.execute(_DirectoryCommand(username, directory),
                                                response=True, timeout=timeout_s)
            except (TimeoutError, NetworkError) as exc:
                raise SlskdError(f"{username} didn't answer the folder request.") from exc
        return listing_to_files(username, self._run(go(), timeout_s + 5) or [], like)

    # -- transfers -------------------------------------------------------------
    def download(self, file: SoulseekFile) -> None:
        async def go():
            await self._require_session().transfers.download(file.username, file.filename)
        self._run(go())

    def get_download_transfer(self, username: str, filename: str) -> dict | None:
        """slskd-shaped snapshot: state, bytesTransferred, placeInQueue,
        averageSpeed, exception, plus localPath (aioslsk may add " (1)")."""
        def snap():
            if self._client is None:
                return None
            t = self._client.transfers.find_transfer(username, filename, _DL)
            if t is None:
                return None
            state = slskd_state(t)
            if state == "Queued, Remotely" and time.monotonic() - self._place_asked.get(id(t), 0) > self.PLACE_POLL_S:
                self._place_asked[id(t)] = time.monotonic()
                self._loop.create_task(self._a_place(t))
            return {
                "id": id(t), "state": state, "bytesTransferred": t.bytes_transfered, "size": t.filesize,
                "placeInQueue": t.place_in_queue, "averageSpeed": t.get_speed(),
                "exception": t.fail_reason or t.abort_reason,
                "localPath": t.local_path if t.state.VALUE == TransferState.COMPLETE else None,
            }
        try:
            return self._call(snap)
        except SlskdError:
            return None

    async def _a_place(self, t: Transfer) -> None:
        try:
            await self._client.transfers.request_place_in_queue(t)
        except Exception:  # noqa: BLE001 — RequestPlaceFailedError, peer offline…
            pass

    def cancel_download(self, username: str, filename: str) -> None:
        """Abort (deletes the partial file) and forget the transfer, so a later
        download() of the same file starts clean. Never raises."""
        async def go():
            t = self._client.transfers.find_transfer(username, filename, _DL) if self._client else None
            if t is not None:
                await self._client.transfers.remove(t)
        try:
            self._run(go())
        except Exception:  # noqa: BLE001
            pass
