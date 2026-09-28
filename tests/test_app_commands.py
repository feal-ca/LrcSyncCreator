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
from PyQt6.QtWidgets import QApplication, QMessageBox

import app as app_module
from app import LrcSyncApp

_MENU_NAMES = {"file", "edit", "tools", "help"}


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
