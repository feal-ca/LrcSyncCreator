"""Tests for workers.wait_for_download_and_move's progress-aware waiting —
a fake slskd client feeds scripted transfer records and a fake clock
stands in for time.monotonic/time.sleep, so nothing here actually waits.

Regression context: a 24 MB FLAC transferring steadily at ~131 KB/s took
184s and was reported as failed by the old fixed 180s wall-clock deadline
even though slskd finished it moments later.
"""

import os

import workers
from soulseek import SoulseekFile

_SIZE = 24_000_000


def _file() -> SoulseekFile:
    return SoulseekFile(
        username="peer", filename="music\\GIMS\\Ma beauté.flac", size=_SIZE,
        bitrate=None, length_s=200, queue_length=0, has_free_slot=True,
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedClient:
    """get_download_transfer returns script(now) — the transfer record at that time."""

    def __init__(self, clock: _FakeClock, script) -> None:
        self._clock = clock
        self._script = script

    def get_download_transfer(self, username, filename):
        return self._script(self._clock.now)


def _patch(monkeypatch, clock, landed_at=None, downloads_dir=None):
    """Patch the clock, and make the file 'land' in downloads_dir at *landed_at*."""
    monkeypatch.setattr(workers.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(workers.time, "sleep", clock.sleep)

    def fake_find(directory, remote_filename, expected_size):
        if directory == downloads_dir and landed_at is not None and clock.now >= landed_at:
            path = os.path.join(downloads_dir, "Ma beauté.flac")
            if not os.path.exists(path):
                with open(path, "wb") as fh:
                    fh.write(b"x")
            return path
        return None

    monkeypatch.setattr(workers, "find_downloaded_file", fake_find)


def test_slow_but_steady_transfer_outlives_the_stall_timeout(monkeypatch, tmp_path):
    clock = _FakeClock()
    downloads, library = str(tmp_path / "dl"), str(tmp_path / "lib")
    os.makedirs(downloads)
    _patch(monkeypatch, clock, landed_at=184.0, downloads_dir=downloads)

    def script(now):
        done = min(_SIZE, int(now * 131_000))
        return {"state": "InProgress", "bytesTransferred": done}

    ok, detail, path = workers.wait_for_download_and_move(
        _ScriptedClient(clock, script), downloads, library, _file(), timeout_s=60.0,
    )

    assert ok, detail
    assert path.startswith(library)


def test_stalled_transfer_fails_after_timeout_without_progress(monkeypatch, tmp_path):
    clock = _FakeClock()
    _patch(monkeypatch, clock)

    def script(now):
        # Moves for 20s, then freezes at a fixed byte count forever.
        return {"state": "InProgress", "bytesTransferred": int(min(now, 20.0) * 100_000)}

    ok, detail, _ = workers.wait_for_download_and_move(
        _ScriptedClient(clock, script), str(tmp_path), str(tmp_path / "lib"), _file(),
        timeout_s=60.0,
    )

    assert not ok
    assert "stalled" in detail.lower()
    assert clock.now < 20.0 + 60.0 + 5.0  # gave up ~60s after the last progress


def test_rejected_transfer_fails_immediately_with_slskd_reason(monkeypatch, tmp_path):
    clock = _FakeClock()
    _patch(monkeypatch, clock)

    def script(now):
        return {
            "state": "Completed, Rejected", "bytesTransferred": 0,
            "exception": "Transfer rejected: File not shared.",
        }

    ok, detail, _ = workers.wait_for_download_and_move(
        _ScriptedClient(clock, script), str(tmp_path), str(tmp_path / "lib"), _file(),
        timeout_s=180.0,
    )

    assert not ok
    assert "File not shared" in detail
    assert clock.now < 10.0


def test_queued_transfer_counts_state_changes_as_progress(monkeypatch, tmp_path):
    clock = _FakeClock()
    downloads = str(tmp_path / "dl")
    os.makedirs(downloads)
    _patch(monkeypatch, clock, landed_at=150.0, downloads_dir=downloads)

    def script(now):
        # Remotely queued for 100s (place in queue shrinking), then transfers.
        if now < 100.0:
            return {"state": "Queued, Remotely", "bytesTransferred": 0,
                    "placeInQueue": 5 - int(now // 25)}
        return {"state": "InProgress", "bytesTransferred": int((now - 100.0) * 500_000)}

    ok, detail, _ = workers.wait_for_download_and_move(
        _ScriptedClient(clock, script), downloads, str(tmp_path / "lib"), _file(),
        timeout_s=60.0,
    )

    assert ok, detail


# ---------------------------------------------------------------------------
# PlaylistDownloadThread — automatic fallback to the row's next candidate
# ---------------------------------------------------------------------------

def _cand(username: str, name: str = "Ma beauté.mp3") -> SoulseekFile:
    return SoulseekFile(
        username=username, filename=f"music\\{name}", size=_SIZE,
        bitrate=320, length_s=200, queue_length=0, has_free_slot=True,
    )


class _FallbackClient:
    """Queues succeed; get_download_transfer reports whatever outcome the
    test scripted for that peer (rejected immediately, or in progress)."""

    def __init__(self, outcomes: dict[str, dict]) -> None:
        self._outcomes = outcomes
        self.requested: list[str] = []
        self.cancelled: list[str] = []

    def download(self, file):
        self.requested.append(file.username)

    def get_download_transfer(self, username, filename):
        return self._outcomes.get(username, {"state": "InProgress", "bytesTransferred": 1})

    def cancel_download(self, username, filename):
        self.cancelled.append(username)


_REJECTED = {"state": "Completed, Rejected", "exception": "Transfer rejected: File not shared."}


def _run_thread(monkeypatch, tmp_path, client, rows, good_peer=None):
    clock = _FakeClock()
    monkeypatch.setattr(workers.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(workers.time, "sleep", clock.sleep)
    downloads = str(tmp_path / "dl")
    os.makedirs(downloads, exist_ok=True)

    def fake_find(directory, remote_filename, expected_size):
        # Only a transfer actually requested from good_peer ever lands.
        if directory == downloads and good_peer and client.requested[-1:] == [good_peer]:
            path = os.path.join(downloads, f"{good_peer}.mp3")
            open(path, "wb").close()
            return path
        return None

    monkeypatch.setattr(workers, "find_downloaded_file", fake_find)

    thread = workers.PlaylistDownloadThread(
        client, downloads, str(tmp_path / "lib"), rows, timeout_s=60.0,
    )
    attempts, done = [], []
    thread.track_attempt.connect(lambda row, f: attempts.append((row, f.username)))
    thread.track_done.connect(lambda row, ok, detail, path: done.append((row, ok, detail)))
    thread.run()
    return attempts, done


def test_falls_back_to_next_candidate_after_rejection(monkeypatch, tmp_path):
    client = _FallbackClient({"bad": _REJECTED})
    rows = [(0, [_cand("bad"), _cand("good")])]

    attempts, done = _run_thread(monkeypatch, tmp_path, client, rows, good_peer="good")

    assert client.requested == ["bad", "good"]
    assert attempts == [(0, "bad"), (0, "good")]
    assert done == [(0, True, "good.mp3")]


def test_stalled_candidate_is_cancelled_before_falling_back(monkeypatch, tmp_path):
    client = _FallbackClient({"stuck": {"state": "InProgress", "bytesTransferred": 100}})
    rows = [(0, [_cand("stuck"), _cand("good")])]

    _attempts, done = _run_thread(monkeypatch, tmp_path, client, rows, good_peer="good")

    assert client.cancelled == ["stuck"]
    assert done[-1][1] is True


def test_skips_other_files_from_a_peer_that_already_failed(monkeypatch, tmp_path):
    client = _FallbackClient({"bad": _REJECTED})
    rows = [(0, [_cand("bad", "a.mp3"), _cand("bad", "b.mp3"), _cand("good")])]

    _run_thread(monkeypatch, tmp_path, client, rows, good_peer="good")

    assert client.requested == ["bad", "good"]


def test_reports_every_failure_when_all_candidates_fail(monkeypatch, tmp_path):
    client = _FallbackClient({"a": _REJECTED, "b": _REJECTED})
    rows = [(0, [_cand("a"), _cand("b")])]

    _attempts, done = _run_thread(monkeypatch, tmp_path, client, rows)

    (row, ok, detail), = done
    assert not ok
    assert "2 sources" in detail and "File not shared" in detail


def test_gives_up_after_max_attempts(monkeypatch, tmp_path):
    client = _FallbackClient({u: _REJECTED for u in "abcdef"})
    rows = [(0, [_cand(u) for u in "abcdef"])]

    _run_thread(monkeypatch, tmp_path, client, rows)

    assert client.requested == list("abc")
