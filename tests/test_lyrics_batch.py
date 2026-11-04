"""Tests for workers.LyricsDownloaderThread — the batch "Find lyrics" for
several songs. It must never replace synced lyrics, even ones saved while
the batch was running, and only upgrades plain lyrics to synced ones."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtCore")

import lyrics_fetch
import workers
from lyrics_fetch import LyricsResult

SYNCED = "[00:01.00] one\n[00:02.00] two\n[00:03.00] three\n[00:04.00] four\n"
OTHER_SYNCED = "[00:05.00] uno\n[00:06.00] dos\n[00:07.00] tres\n[00:08.00] cuatro\n"


def _song(tmp_path, name="Artist - Song.mp3", lrc=None):
    path = tmp_path / name
    path.write_bytes(b"")
    if lrc is not None:
        (tmp_path / (os.path.splitext(name)[0] + ".lrc")).write_text(lrc, encoding="utf-8")
    return str(path)


def _lrc_of(path):
    with open(os.path.splitext(path)[0] + ".lrc", encoding="utf-8") as fh:
        return fh.read()


def _run(paths, monkeypatch, result_for, **kwargs):
    seen = []

    def fake_fetch(info, allow_plain=True, use_ytmusic=False):
        seen.append(use_ytmusic)
        return result_for(info)

    monkeypatch.setattr(lyrics_fetch, "fetch_best_lyrics", fake_fetch)
    thread = workers.LyricsDownloaderThread(paths, **kwargs)
    done = []
    thread.file_done.connect(lambda p, kind: done.append((os.path.basename(p), kind)))
    thread.run()
    return done, seen


def test_writes_lyrics_for_a_song_without_any(tmp_path, monkeypatch):
    song = _song(tmp_path)

    done, _ = _run([song], monkeypatch, lambda info: LyricsResult(SYNCED, True, "lrclib"))

    assert _lrc_of(song) == SYNCED
    assert done == [("Artist - Song.mp3", "synced")]


def test_never_overwrites_synced_lyrics_saved_while_it_ran(tmp_path, monkeypatch):
    song = _song(tmp_path)

    def user_saves_meanwhile(info):
        with open(os.path.splitext(song)[0] + ".lrc", "w", encoding="utf-8") as fh:
            fh.write(SYNCED)
        return LyricsResult(OTHER_SYNCED, True, "qq")

    _run([song], monkeypatch, user_saves_meanwhile)

    assert _lrc_of(song) == SYNCED


def test_plain_lyrics_are_only_replaced_by_synced_ones(tmp_path, monkeypatch):
    song = _song(tmp_path, lrc="just words\nno timing\n")

    _run([song], monkeypatch, lambda info: LyricsResult("other plain words\n", False, "lrclib"))
    assert _lrc_of(song) == "just words\nno timing\n"

    _run([song], monkeypatch, lambda info: LyricsResult(SYNCED, True, "lrclib"))
    assert _lrc_of(song) == SYNCED


def test_songs_that_already_have_synced_lyrics_are_skipped_without_a_lookup(tmp_path, monkeypatch):
    song = _song(tmp_path, lrc=SYNCED)

    _done, seen = _run([song], monkeypatch, lambda info: LyricsResult(OTHER_SYNCED, True, "qq"))

    assert seen == []
    assert _lrc_of(song) == SYNCED


def test_youtube_music_setting_is_passed_through(tmp_path, monkeypatch):
    song = _song(tmp_path)

    _done, seen = _run([song], monkeypatch, lambda info: None, use_ytmusic=True)

    assert seen == [True]
