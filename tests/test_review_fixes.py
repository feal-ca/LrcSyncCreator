"""Regression tests for the findings of the final whole-branch review."""

import os
import urllib.parse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QMessageBox

import app as app_module
import postprocess
import search_view
import transfers_view
from app import LrcSyncApp
from library_panel import LibraryPanel
from soulseek import SoulseekFile


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


# 1. Renaming the open song must not leave VLC on the old path.
def test_renaming_the_open_song_points_the_player_at_the_new_file(window, tmp_path):
    old = tmp_path / "track.mp3"
    old.write_bytes(b"")
    window.library_panel.add_files([str(old)])
    window._load_song(str(old))
    new = tmp_path / "Someone - Something.mp3"
    os.rename(old, new)

    window.library_panel.replace_path(str(old), str(new))

    mrl = urllib.parse.unquote(window.player.get_media().get_mrl())
    assert mrl.endswith("Someone - Something.mp3")


# 2. A new client after a Settings change must reach new downloads.
def test_downloads_ask_for_the_current_client_each_time(qapp, tmp_path, monkeypatch):
    built = []

    class _Thread:
        def __init__(self, item_id, client, *a, **k):
            built.append(client)
            self.status = self.source_changed = self.finished_transfer = self.finished = type(
                "S", (), {"connect": lambda *a: None})()

        def start(self):
            pass

        def isRunning(self):
            return True

    monkeypatch.setattr(transfers_view, "TransferThread", _Thread)
    clients = iter(["first client", "second client"])
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    view = transfers_view.TransfersView(settings, lambda: next(clients))
    f = lambda name: SoulseekFile("u", f"a\\{name}.mp3", 1, 320, 100, 0, True, 1)

    view.enqueue([f("one")])
    view.enqueue([f("two")])

    assert built == ["first client", "second client"]


def test_a_closed_client_says_it_is_disconnected(tmp_path):
    from slsk_client import SlskClient

    client = SlskClient("me", "pw", str(tmp_path / "dl"))
    client.start_loop()
    client.close()

    assert "Disconnected" in client.check_connection()[1]


# 3. A copy picked by hand stays picked while results keep arriving.
def test_a_hand_picked_copy_stays_selected_across_refreshes(qapp, tmp_path):
    class _Client:
        def start_search(self, query, on_results=None, **_k):
            self.cb = on_results
            return 1

        def stop_search(self, _sid):
            return []

    client = _Client()
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    view = search_view.SearchView(settings, lambda: client, lambda t: False)
    view.query_input.setText("song")
    view.run_search()
    mk = lambda user, free: SoulseekFile(user, "x\\Song.mp3", 5, 320, 200, 0 if free else 9, free, 1)
    client.cb([mk("alice", True), mk("bob", False)])
    view.refresh_now()
    top = view.tree.topLevelItem(0)
    top.setExpanded(True)
    bob = next(top.child(i) for i in range(top.childCount()) if top.child(i).text(0) == "bob")
    view.tree.setCurrentItem(bob)
    view.on_user_picked()

    client.cb([mk("carol", True)])
    view.refresh_now()

    _version, chosen = view._selected()
    assert chosen.username == "bob"
    view.shutdown()


# 4. Renames never attach another song's lyrics or orphan a song's own.
def test_same_tags_with_different_extensions_rename_only_the_first(tmp_path, monkeypatch):
    for name in ("x1.opus", "x2.ogg"):
        (tmp_path / name).write_bytes(b"a")
        (tmp_path / (name.split(".")[0] + ".lrc")).write_text("lyrics", encoding="utf-8")
    monkeypatch.setattr(postprocess, "tag_based_stem", lambda p: "A - T")

    first, second = postprocess.plan_standard_names([str(tmp_path / "x1.opus"), str(tmp_path / "x2.ogg")])

    assert first.new and second.new is None


def test_rename_is_skipped_when_lyrics_with_that_name_already_exist(tmp_path, monkeypatch):
    (tmp_path / "x1.opus").write_bytes(b"a")
    (tmp_path / "A - T.lrc").write_text("someone else's lyrics", encoding="utf-8")
    monkeypatch.setattr(postprocess, "tag_based_stem", lambda p: "A - T")

    [plan] = postprocess.plan_standard_names([str(tmp_path / "x1.opus")])

    assert plan.new is None


# 5. A missing library folder or a broken index doesn't make every song "new".
def _lib(tmp_path, settings):
    lib = tmp_path / "Music"
    lib.mkdir()
    for name in ("a.mp3", "b.mp3"):
        (lib / name).write_bytes(b"")
    settings.setValue("library_dir", str(lib))
    return lib


def test_a_library_folder_missing_for_one_start_keeps_the_history(qapp, tmp_path):
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    lib = _lib(tmp_path, settings)
    LibraryPanel(settings).load_library_folder()          # first run seeds
    os.rename(lib, tmp_path / "away")
    LibraryPanel(settings).load_library_folder()          # drive not mounted
    os.rename(tmp_path / "away", lib)

    panel = LibraryPanel(settings)
    panel.load_library_folder()

    assert not any(panel.is_new(p) for p in panel.real_file_paths())


def test_an_unreadable_index_is_treated_as_a_first_run(qapp, tmp_path):
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    _lib(tmp_path, settings)
    (tmp_path / "library_index.json").write_text("{broken", encoding="utf-8")

    panel = LibraryPanel(settings)
    panel.load_library_folder()

    assert not any(panel.is_new(p) for p in panel.real_file_paths())


# 6. Only one line looks selected after lines above it are deleted.
def test_only_one_line_is_highlighted_after_deleting_above_it(window, tmp_path):
    song = tmp_path / "s.mp3"
    song.write_bytes(b"")
    (tmp_path / "s.lrc").write_text("".join(f"[00:0{i}.00] {c}\n" for i, c in enumerate("abcd")), encoding="utf-8")
    window._load_song(str(song))
    window._select_verse(2)                     # "c"

    window._delete_verse(window._verses[0])     # delete "a"
    window._select_verse(window.current_verse_index + 1)

    assert sum(1 for v in window._verses if v._is_active) == 1


# 7. A second request while Soulseek is still connecting doesn't stack dialogs.
def test_a_nested_soulseek_request_while_connecting_shows_no_dialog(window, monkeypatch):
    window.settings.setValue("soulseek_username", "me")
    window.settings.setValue("soulseek_password", "pw")
    window._slsk_waiting = True
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))

    assert window._get_slsk_client() is None
    assert shown == []
    window._slsk_waiting = False
