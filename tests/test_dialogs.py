"""Tests for the small library dialogs in dialogs.py: the rename preview
and Edit Artist & Title."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QDialogButtonBox

import dialogs
from postprocess import RenamePlan


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_rename_preview_lists_changes_and_counts_them(qapp):
    plans = [
        RenamePlan("/m/01 track.mp3", "/m/Manu Chao - Clandestino.mp3", ""),
        RenamePlan("/m/track07.mp3", None, "No artist/title tags"),
    ]

    dialog = dialogs.RenamePreviewDialog(plans)

    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.text() == "Rename 1"
    assert ok.isEnabled()
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(0, 0).text() == "01 track.mp3"
    assert dialog.table.item(0, 1).text() == "Manu Chao - Clandestino.mp3"
    assert dialog.table.item(1, 1).text() == "No artist/title tags"


def test_rename_preview_with_nothing_to_do_disables_ok(qapp):
    dialog = dialogs.RenamePreviewDialog([RenamePlan("/m/a.mp3", None, "Already named that way")])

    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_tag_edit_dialog_prefills_and_saves(qapp, monkeypatch):
    written = []
    monkeypatch.setattr(dialogs, "read_artist_title", lambda path: ("Rosalia", "Malamente"))
    monkeypatch.setattr(dialogs, "write_artist_title", lambda *a: written.append(a))

    dialog = dialogs.TagEditDialog("/m/song.mp3")
    assert dialog.artist_input.text() == "Rosalia"
    assert dialog.title_input.text() == "Malamente"
    dialog.artist_input.setText("Rosalía")
    dialog._save()

    assert written == [("/m/song.mp3", "Rosalía", "Malamente")]
    assert dialog.result() == dialog.DialogCode.Accepted


def test_tag_edit_dialog_reports_write_errors(qapp, monkeypatch):
    def boom(*_a):
        raise ValueError("read-only file")

    monkeypatch.setattr(dialogs, "read_artist_title", lambda path: ("", ""))
    monkeypatch.setattr(dialogs, "write_artist_title", boom)
    dialog = dialogs.TagEditDialog("/m/song.mp3")
    dialog.artist_input.setText("A")
    dialog.title_input.setText("B")

    dialog._save()

    assert "read-only file" in dialog.error_label.text()
    assert dialog.result() != dialog.DialogCode.Accepted
