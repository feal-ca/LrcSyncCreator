"""Tests for the lyrics editor in app.py: loading and saving without
losing anything, undo, the timeline, and staying fast."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QFile, QSettings
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox

import app as app_module
from app import LrcSyncApp
from widgets import TimelineWidget, Verse


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    ini = str(tmp_path / "settings.ini")
    monkeypatch.setattr(app_module, "QSettings", lambda *a, **k: QSettings(ini, QSettings.Format.IniFormat))
    monkeypatch.setattr(app_module, "audio_duration_ms", lambda path: 200_000)
    win = LrcSyncApp()
    yield win
    win._dirty = False
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    win.close()


def _song(tmp_path, lrc: bytes | None = None, name="song"):
    audio = tmp_path / f"{name}.mp3"
    audio.write_bytes(b"")
    if lrc is not None:
        (tmp_path / f"{name}.lrc").write_bytes(lrc)
    return str(audio)


def _texts(window):
    return [v.get_verse_text() for v in window._verses]


def _stamps(window):
    return [v.get_timestamp() for v in window._verses]


# -- loading -------------------------------------------------------------------

def test_a_latin1_lrc_loads(window, tmp_path):
    window._load_song(_song(tmp_path, "[00:01.00] Café\n".encode("latin-1")))
    assert _texts(window) == ["Café"]


def test_offset_tag_moves_lines_earlier(window, tmp_path):
    window._load_song(_song(tmp_path, b"[offset:500]\n[00:10.00] a\n"))
    assert _stamps(window) == ["[00:09.50]"]


def test_duration_is_known_before_pressing_play(window, tmp_path):
    window._load_song(_song(tmp_path, b"[00:10.00] a\n"))
    assert window.duration_ms == 200_000
    assert window.timeline.duration_ms == 200_000


# -- saving ----------------------------------------------------------------------

def test_save_keeps_metadata_and_leaves_no_temp_file(window, tmp_path):
    song = _song(tmp_path, b"[ar:Someone]\n[ti:Something]\n[00:01.00] a\n[00:02.00] b\n")
    window._load_song(song)
    window._verses[0].verse_text.setText("a changed")

    window._save_lrc_file()

    saved = (tmp_path / "song.lrc").read_text(encoding="utf-8")
    assert saved.splitlines()[:2] == ["[ar:Someone]", "[ti:Something]"]
    assert "[00:01.00] a changed" in saved
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".part")] == []


def test_offset_is_not_written_back_after_being_applied(window, tmp_path):
    window._load_song(_song(tmp_path, b"[offset:500]\n[00:10.00] a\n"))
    window._save_lrc_file()
    assert "[offset" not in (tmp_path / "song.lrc").read_text(encoding="utf-8")


def test_saving_the_empty_starter_row_creates_no_file(window, tmp_path):
    window._load_song(_song(tmp_path))
    window._save_lrc_file()
    assert not (tmp_path / "song.lrc").exists()


def test_saving_after_clearing_moves_the_old_lyrics_to_the_trash(window, tmp_path, monkeypatch):
    window._load_song(_song(tmp_path, b"[00:01.00] a\n"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._clear_lyrics()
    trashed = []
    monkeypatch.setattr(QFile, "moveToTrash", lambda p: (trashed.append(p) or True, p))

    window._save_lrc_file()

    assert trashed == [str(tmp_path / "song.lrc")]


def test_a_file_that_failed_to_load_is_not_overwritten_without_asking(window, tmp_path, monkeypatch):
    song = _song(tmp_path, b"[00:01.00] precious\n")

    def unreadable(path):
        raise OSError("permission denied")

    monkeypatch.setattr(app_module, "read_lrc_text", unreadable)
    window._load_song(song)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)

    window._save_lrc_file()

    assert (tmp_path / "song.lrc").read_bytes() == b"[00:01.00] precious\n"


# -- always somewhere to type -------------------------------------------------------

def test_clear_all_leaves_one_blank_row(window, tmp_path, monkeypatch):
    window._load_song(_song(tmp_path, b"[00:01.00] a\n[00:02.00] b\n"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    window._clear_lyrics()

    assert _texts(window) == [""]


def test_deleting_the_last_line_leaves_a_blank_row(window, tmp_path):
    window._load_song(_song(tmp_path, b"[00:01.00] only\n"))

    window._delete_verse(window._verses[0])

    assert _texts(window) == [""]


# -- undo ------------------------------------------------------------------------------

def test_one_undo_reverts_a_whole_shift(window, tmp_path, monkeypatch):
    lrc = "".join(f"[00:{10 + i:02d}.00] line {i}\n" for i in range(8)).encode()
    window._load_song(_song(tmp_path, lrc))
    before = _stamps(window)
    monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (1000, True))

    window._shift_all_timestamps()
    assert _stamps(window) != before
    window._undo_last_sync()

    assert _stamps(window) == before


def test_undo_reverts_a_timeline_drag(window, tmp_path):
    window._load_song(_song(tmp_path, b"[00:10.00] a\n[00:20.00] b\n"))
    before = _stamps(window)

    window._update_verses_from_timeline([(0, 12_000), (1, 22_000)])
    window._undo_last_sync()

    assert _stamps(window) == before


# -- timeline ------------------------------------------------------------------------

def test_timeline_forgets_a_selection_when_lines_change(qapp):
    timeline = TimelineWidget()
    timeline.selected_indices = {3, 4}

    timeline.load_data([], 100_000)

    assert not timeline.selected_indices


# -- speed -----------------------------------------------------------------------------

def test_moving_the_selection_restyles_only_two_rows(window, tmp_path, monkeypatch):
    lrc = "".join(f"[00:{10 + i:02d}.00] line {i}\n" for i in range(30)).encode()
    window._load_song(_song(tmp_path, lrc))
    window._select_verse(2)
    calls = []
    original = Verse.set_active
    monkeypatch.setattr(Verse, "set_active", lambda self, active: (calls.append(active), original(self, active)))

    window._select_verse(3)

    assert len(calls) == 2


def test_loading_another_song_reuses_line_widgets(window, tmp_path):
    window._load_song(_song(tmp_path, b"[00:01.00] a\n[00:02.00] b\n[00:03.00] c\n", name="one"))
    widgets = list(window._verses)
    window._sync_verse(window._verses[0])

    window._load_song(_song(tmp_path, b"[00:05.00] x\n[00:06.00] y\n", name="two"))

    assert window._verses == widgets[:2]
    assert _texts(window) == ["x", "y"]
    assert window._undo_stack == []
    assert not window._dirty


# -- closing, dialogs, robustness ---------------------------------------------------

def test_closing_during_a_conversion_asks_first(window, monkeypatch):
    from PyQt6.QtGui import QCloseEvent

    class _Running:
        def isRunning(self):
            return True

        def request_cancel(self):
            pass

        def wait(self, *_a):
            return True

    window._convert_thread = _Running()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _p, title, *a, **k: asked.append(title) or QMessageBox.StandardButton.Cancel)
    event = QCloseEvent()

    window.closeEvent(event)

    assert not event.isAccepted()
    assert any("Conversion" in t for t in asked)
    window._convert_thread = None


def test_the_lyrics_picker_is_freed_after_closing(window, tmp_path, monkeypatch):
    window._load_song(_song(tmp_path, b"[00:01.00] a\n"))
    freed = []

    class _Picker:
        DialogCode = type("DC", (), {"Accepted": 1})
        chosen_text = ""

        def __init__(self, *a, **k):
            pass

        def run_search(self):
            pass

        def exec(self):
            return 0

        def deleteLater(self):
            freed.append(True)

    monkeypatch.setattr(app_module, "LyricsPickerDialog", _Picker)

    window._get_lyrics_online()

    assert freed == [True]


def test_an_unexpected_error_is_reported_instead_of_crashing(capsys):
    import main

    main.install_excepthook()
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        import sys
        sys.excepthook(type(exc), exc, exc.__traceback__)

    assert "boom" in capsys.readouterr().err


def test_the_player_poll_does_nothing_while_paused(window, tmp_path, monkeypatch):
    import vlc

    window._load_song(_song(tmp_path, b"[00:01.00] a\n"))
    monkeypatch.setattr(window.player, "get_state", lambda: vlc.State.Paused)
    monkeypatch.setattr(window.player, "get_time", lambda: 5_000)
    monkeypatch.setattr(window.player, "get_length", lambda: 200_000)
    moved = []
    monkeypatch.setattr(window, "_on_position_changed", lambda ms: moved.append(ms))

    window._vlc_poll()
    window._vlc_poll()
    window._vlc_poll()

    assert moved == [5_000]
