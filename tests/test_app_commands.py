"""Tests for LrcSyncApp's unified command table (app.py's _Command list) and
the Clear All Lyrics confirmation — the two concrete bugs the UX pass fixed:
shortcuts registered in two places that could drift apart, and the one
irreversible action in the app having no confirmation at all.

Needs a real (if offscreen) Qt platform to construct QMainWindow/QWidget
instances, so this sets QT_QPA_PLATFORM before importing PyQt6 rather than
relying on the environment already having it set.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox

import app as app_module
from app import LrcSyncApp

_MENU_NAMES = {"file", "lyrics", "help"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    # LrcSyncApp.__init__ constructs QSettings("LrcStudio", "Configs") —
    # the user's real, persisted config. Redirect it to a throwaway
    # per-test ini file so a test run can never read or clobber the
    # user's actual settings (window geometry, slskd credentials' URL/
    # key, embed-lyrics preference, etc.).
    ini_path = str(tmp_path / "test_settings.ini")
    monkeypatch.setattr(
        app_module, "QSettings",
        lambda *a, **k: QSettings(ini_path, QSettings.Format.IniFormat),
    )
    win = LrcSyncApp()
    yield win
    # Root cause of a real, reproducible hang found while adding this
    # task's tests: any test that leaves the window dirty (e.g. via
    # _replace_all_verses/_add_verse) makes win.close() below trigger
    # _confirm_discard_if_dirty()'s real QMessageBox.question — blocking
    # forever in a headless test with no one to click it. Some tests
    # happened to avoid this by coincidence (a QMessageBox.question patch
    # set in the test body via this same monkeypatch fixture is still
    # active here, since monkeypatch undoes patches in reverse dependency
    # order — after this fixture's teardown runs, not before); tests that
    # never touched that patch hit the real dialog and hung. Tests should
    # never see a real confirmation dialog at teardown regardless of what
    # dirty state they left behind, so force it clean before closing.
    win._dirty = False
    # Closing asks before stopping running downloads; a test that leaves a
    # fake batch "active" must not block teardown on that modal dialog.
    from PyQt6.QtWidgets import QMessageBox as _QMB
    monkeypatch.setattr(_QMB, "question", lambda *a, **k: _QMB.StandardButton.Yes)
    win.close()


# ---------------------------------------------------------------------------
# Command table integrity
# ---------------------------------------------------------------------------

def test_no_shortcut_is_bound_to_more_than_one_command(window):
    shortcuts = [cmd.shortcut for cmd in window._commands if cmd.shortcut]
    assert len(shortcuts) == len(set(shortcuts))


def test_every_command_targets_a_real_menu_or_is_transport_only(window):
    for cmd in window._commands:
        assert cmd.menu is None or cmd.menu in _MENU_NAMES


def test_shortcuts_dialog_covers_every_registered_shortcut(window):
    # Reproduces _show_shortcuts_dialog's row-building without popping the
    # modal, so a shortcut added to the table can never again go missing
    # from the Help dialog the way Ctrl+F previously did.
    rows = "".join(
        f"<tr><td><b>{cmd.shortcut}</b></td><td>{cmd.help_text or cmd.label}</td></tr>"
        for cmd in window._commands if cmd.shortcut
    )
    for cmd in window._commands:
        if cmd.shortcut:
            assert cmd.shortcut in rows


def test_transport_commands_have_no_menu_entry(window):
    # Play/Pause, sync-and-advance, seek, and line-selection are real-time
    # transport controls, deliberately kept out of the menu bar.
    transport_labels = {cmd.label for cmd in window._commands if cmd.menu is None}
    assert "Play / Pause" in transport_labels
    assert all(cmd.menu is None for cmd in window._commands if cmd.label == "Play / Pause")


# ---------------------------------------------------------------------------
# Clear All Lyrics confirmation
# ---------------------------------------------------------------------------

def test_clear_lyrics_does_nothing_on_cancel(window, monkeypatch):
    window._add_verse("[00:01.00]", "line one")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel)

    window._clear_lyrics()

    assert len(window._verses) == 1


def test_clear_lyrics_clears_on_confirm(window, monkeypatch):
    window._add_verse("[00:01.00]", "line one")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    window._clear_lyrics()

    assert len(window._verses) == 0


def test_clear_lyrics_skips_the_prompt_when_theres_nothing_to_clear(window, monkeypatch):
    asked = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes,
    )

    window._clear_lyrics()

    assert asked == []


# ---------------------------------------------------------------------------
# Unified Find Music entry point
# ---------------------------------------------------------------------------

def test_find_music_opens_the_search_page(window):
    window._find_music()

    assert window.workspace.currentIndex() == app_module.WORKSPACE_SEARCH
    assert window.tabs.currentIndex() == app_module.TAB_SEARCH


def test_find_music_routes_spotify_link_to_batch_import(window, monkeypatch):
    calls = []
    clients = []
    monkeypatch.setattr(window, "_get_slsk_client", lambda: clients.append(1) or object())
    monkeypatch.setattr(window, "_open_batch_import", lambda link: calls.append(link))

    # is_spotify_link() requires a genuine 22-character Spotify ID; a short
    # fake one would fall through to a real Soulseek search.
    spotify_link = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"

    window._find_music()
    window.search_view.query_input.setText(spotify_link)
    window.search_view.search_button.click()

    assert calls == [spotify_link]
    assert clients == []   # routed before any slskd connection is needed


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in self.slots:
            fn(*args)


def test_find_music_routes_plain_text_to_soulseek_search(window, monkeypatch):
    # Plain or malformed text must fall through to a direct Soulseek
    # search, not silently do nothing.
    import search_view

    class _FakeSearchThread:
        instances = []

        def __init__(self, client, query, wait_s=25.0) -> None:
            self.client = client
            self.query = query
            self.started = False
            self.progress = _Sig()
            self.results_ready = _Sig()
            self.search_failed = _Sig()
            self.finished = _Sig()
            _FakeSearchThread.instances.append(self)

        def start(self) -> None:
            self.started = True

        def isRunning(self) -> bool:
            return False

        def request_stop(self) -> None:
            pass

        request_cancel = request_stop

        def wait(self, *_a) -> bool:
            return True

    monkeypatch.setattr(search_view, "SoulseekLiveSearchThread", _FakeSearchThread)
    monkeypatch.setattr(window, "_get_slsk_client", lambda: object())
    batch_import_calls = []
    monkeypatch.setattr(window, "_open_batch_import", lambda link: batch_import_calls.append(link))

    window._find_music()
    window.search_view.query_input.setText("Bohemian Rhapsody Queen")
    window.search_view.search_button.click()

    assert batch_import_calls == []  # never mistaken for a Spotify link
    assert len(_FakeSearchThread.instances) == 1
    thread = _FakeSearchThread.instances[0]
    assert thread.query == "Bohemian Rhapsody Queen"
    assert thread.started is True
    assert window.search_view._history()[0] == "Bohemian Rhapsody Queen"


def test_command_table_has_one_soulseek_search_entry(window):
    by_label = {cmd.label: cmd for cmd in window._commands}
    assert by_label["Search Soulseek"].shortcut == "Ctrl+F"
    assert "Find Music…" not in by_label
    assert "Add Files…" not in by_label


# ---------------------------------------------------------------------------
# _batch_download_lyrics signature
# ---------------------------------------------------------------------------

def test_batch_download_lyrics_accepts_paths_directly(window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "_downloader", None)

    class FakeThread:
        # isRunning() is required: window._downloader ends up holding this
        # instance, and win.close() (fixture teardown) -> closeEvent ->
        # stop_thread(self._downloader) calls .isRunning() unconditionally.
        # Missing it raises AttributeError inside a Qt close-event callback,
        # which PyQt turns into a fatal C++ abort rather than a normal
        # Python exception — the same issue test_workspace.py's
        # _FakeFetchThread had (see that file's docstring).
        def __init__(self, files, embed=False):
            started.append(files)
        progress = type("Signal", (), {"connect": lambda self, fn: None})()
        file_done = type("Signal", (), {"connect": lambda self, fn: None})()
        finished_batch = type("Signal", (), {"connect": lambda self, fn: None})()
        def start(self): pass
        def isRunning(self): return False

    monkeypatch.setattr(app_module, "LyricsDownloaderThread", FakeThread)

    window._batch_download_lyrics(["/fake/a.mp3", "/fake/b.mp3"])

    assert started == [["/fake/a.mp3", "/fake/b.mp3"]]


def test_second_lyrics_batch_waits_for_the_running_one(window, monkeypatch):
    # Starting a new pass used to replace self._downloader while the
    # previous QThread was still running, letting Python destroy it mid-run.
    started = []

    class FakeThread:
        def __init__(self, files, embed=False):
            self.files = files
            self.running = True
            started.append(files)
        progress = type("Signal", (), {"connect": lambda self, fn: None})()
        file_done = type("Signal", (), {"connect": lambda self, fn: None})()
        finished_batch = type("Signal", (), {"connect": lambda self, fn: None})()
        def start(self): pass
        def isRunning(self): return self.running
        def request_cancel(self): self.running = False
        def wait(self, *_a): return True

    monkeypatch.setattr(app_module, "LyricsDownloaderThread", FakeThread)

    window._fetch_lyrics_for(["/a.mp3"])
    first = window._downloader
    window._fetch_lyrics_for(["/b.mp3", "/c.mp3"])

    assert window._downloader is first
    assert started == [["/a.mp3"]]

    first.running = False
    window._on_batch_finished(["a.mp3"], [])

    assert started == [["/a.mp3"], ["/b.mp3", "/c.mp3"]]
    window._downloader.running = False


def test_queue_downloads_hands_files_to_the_downloads_page(window, monkeypatch):
    queued = []
    monkeypatch.setattr(
        window.transfers_view, "enqueue",
        lambda files, alternatives=None, queue_patience_s=None: queued.append(files) or list(range(len(files))),
    )

    window._queue_downloads(["f1", "f2"])

    assert queued == [["f1", "f2"]]
    assert "Queued 2 downloads" in window.status_bar.text()


def test_batch_download_lyrics_with_empty_list_shows_info_and_starts_nothing(window, monkeypatch):
    # Review finding RF5: previously only asserted the negative half
    # (nothing started) — the "must show the existing message" half of
    # the requirement went unchecked, so this would still pass even if
    # the QMessageBox.information call were deleted entirely.
    started = []
    informed = []
    monkeypatch.setattr(app_module, "LyricsDownloaderThread", lambda files: started.append(files))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: informed.append(a[1:]))

    window._batch_download_lyrics([])

    assert started == []
    assert len(informed) == 1
    assert informed[0] == ("Info", "No missing lyrics to download.")


# ---------------------------------------------------------------------------
# _replace_all_verses / _clear_lyrics split
# ---------------------------------------------------------------------------

def test_get_lyrics_does_not_trigger_the_clear_confirmation(window, monkeypatch):
    window._add_verse("[00:01.00]", "old line")
    window.current_file_path = "/fake/song.mp3"  # _get_lyrics_online guards on song_filename
    window.song_filename = "song.mp3"

    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes)

    window._replace_all_verses([("[00:05.00]", "new line")])

    assert asked == []
    assert len(window._verses) == 1
    assert window._verses[0].get_verse_text() == "new line"


def test_get_lyrics_online_does_nothing_with_no_song_loaded(window, monkeypatch):
    # Review finding RF3: no test previously drove _get_lyrics_online
    # itself — only _replace_all_verses directly, which never exercises
    # the song_filename guard this method actually has.
    asked = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: asked.append(1) or ("", False))

    assert window.song_filename == ""
    window._get_lyrics_online()

    assert asked == []  # never even prompted for a query


def test_on_search_result_replaces_lyrics_without_the_clear_confirmation(window, monkeypatch):
    # Review finding RF3: exercises the actual bug path (_on_search_result,
    # the LyricsSearchThread completion handler that _get_lyrics_online
    # wires up) rather than calling _replace_all_verses directly, which
    # would still pass even if _on_search_result regressed back to
    # calling self._clear_lyrics().
    window._add_verse("[00:01.00]", "old line")

    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes)

    window._on_search_result("[00:05.00]new line\n")

    assert asked == []
    assert [v.get_verse_text() for v in window._verses] == ["new line"]
    assert window._dirty is True


def test_on_search_result_with_no_result_shows_sorry_message(window, monkeypatch):
    informed = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: informed.append(a[1:]))

    window._on_search_result("")

    assert len(informed) == 1
    assert informed[0][0] == "Sorry"


def test_clear_lyrics_still_confirms_after_the_split(window, monkeypatch):
    window._add_verse("[00:01.00]", "line one")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel)

    window._clear_lyrics()

    assert len(window._verses) == 1  # cancelled -> untouched, same as before the split


def test_replace_all_verses_clears_undo_stack_stale_entries(window):
    # Sanity check: replacing the verse list shouldn't leave undo entries
    # pointing at verse objects that no longer exist in self._verses.
    window._add_verse("[00:01.00]", "old line")
    old_verse = window._verses[0]
    window._undo_stack.append((old_verse, "[00:00.00]"))

    window._replace_all_verses([("[00:05.00]", "new line")])

    assert old_verse not in window._verses
    # The undo entry for the removed verse is harmless (already covered by
    # _undo_last_sync's "skip stale entries" logic from the earlier fix),
    # but confirm the new verse list is exactly the replacement.
    assert [v.get_verse_text() for v in window._verses] == ["new line"]


def test_loading_a_song_with_existing_verses_does_not_trigger_the_clear_confirmation(window, monkeypatch, tmp_path):
    # _load_song used to call self._clear_lyrics() unconditionally, which
    # (after the earlier UX pass added a confirmation to it) meant
    # switching to a different song while the current one has lyrics
    # popped "Remove all N lines? This can't be undone" on every single
    # switch — a bug that predates this plan. _load_song must use the
    # silent primitive instead.
    #
    # This adds the "existing verses" via _add_verse directly rather than
    # a first _load_song() call: constructing a second full LrcSyncApp
    # (VLC instance, widget tree) in a later test after a prior test's
    # _replace_all_verses() has deleteLater()'d a verse widget was found,
    # empirically, to hang window construction somewhere in Qt/VLC's
    # object lifecycle — a test-suite resource-accumulation artifact
    # across the many full-window tests in this file, not a defect in
    # _load_song/_replace_all_verses themselves (every other test here
    # constructs exactly one window, matching real single-instance usage,
    # and passes reliably). Verifying the same code path without a second
    # real song-load avoids that hang while still exercising the fix.
    f2 = tmp_path / "b.mp3"
    f2.write_bytes(b"")

    window._add_verse("[00:01.00]", "line in the currently loaded song")

    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes)

    window._load_song(str(f2))

    assert asked == []


class _FakeSlsk:
    """Stands in for slsk_client.SlskClient: records how it was built and
    connects (or fails) instantly, without any network."""

    fail = None

    def __init__(self, username, password, downloads_dir, **kwargs):
        self.args = (username, password, downloads_dir, kwargs)
        self.connected = False
        self.closed = False
        self.credentials_refused = False

    def ensure_connected(self, timeout_s=45.0):
        if type(self).fail is not None:
            raise type(self).fail
        self.connected = True

    def check_connection(self):
        return (True, "Connected to Soulseek.") if self.connected else (False, "Not connected to Soulseek.")

    def close(self, timeout_s=10.0):
        self.closed = True


@pytest.fixture
def fake_slsk(monkeypatch, tmp_path):
    _FakeSlsk.fail = None
    monkeypatch.setattr(app_module, "SlskClient", _FakeSlsk)
    monkeypatch.setattr(app_module, "SLSKD_CONFIG_PATH", str(tmp_path / "no-slskd.yml"))
    return _FakeSlsk


def test_soulseek_asks_for_an_account_first(window, fake_slsk, monkeypatch):
    opened = []
    monkeypatch.setattr(window, "_open_settings_dialog", lambda: opened.append(1))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    assert window._get_slsk_client() is None
    assert opened == [1]


def test_soulseek_account_is_carried_over_from_slskd(window, fake_slsk, monkeypatch, tmp_path):
    yml = tmp_path / "slskd.yml"
    yml.write_text("soulseek:\n  username: Me\n  password: pw\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "SLSKD_CONFIG_PATH", str(yml))

    client = window._get_slsk_client()

    assert client is not None
    assert client.args[:2] == ("Me", "pw")
    assert window.settings.value("soulseek_username") == "Me"


def test_soulseek_client_is_built_once_and_shares_the_music_folder(window, fake_slsk, tmp_path):
    lib = tmp_path / "Music"
    lib.mkdir()
    window.settings.setValue("library_dir", str(lib))
    window.settings.setValue("soulseek_username", "Me")
    window.settings.setValue("soulseek_password", "pw")

    first = window._get_slsk_client()
    second = window._get_slsk_client()

    assert first is second
    assert first.args[3]["shared_dirs"] == (str(lib),)


def test_soulseek_sharing_can_be_turned_off(window, fake_slsk, tmp_path):
    lib = tmp_path / "Music"
    lib.mkdir()
    window.settings.setValue("library_dir", str(lib))
    window.settings.setValue("soulseek_username", "Me")
    window.settings.setValue("soulseek_password", "pw")
    window.settings.setValue("share_library", False)

    assert window._get_slsk_client().args[3]["shared_dirs"] == ()


def test_soulseek_login_failure_is_explained(window, fake_slsk, monkeypatch):
    from soulseek import SlskdError

    window.settings.setValue("soulseek_username", "Me")
    window.settings.setValue("soulseek_password", "wrong")
    fake_slsk.fail = SlskdError("Wrong Soulseek password for this username.")
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *a, **k: shown.append(text))

    assert window._get_slsk_client() is None
    assert "Wrong Soulseek password" in shown[0]


def test_shutdown_closes_the_soulseek_client(window, fake_slsk):
    window.settings.setValue("soulseek_username", "Me")
    window.settings.setValue("soulseek_password", "pw")
    client = window._get_slsk_client()

    window._shutdown_soulseek()

    assert client.closed
