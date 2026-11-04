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


def test_settings_dialog_saves_the_soulseek_account(qapp, tmp_path):
    from PyQt6.QtCore import QSettings

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    dialog = dialogs.SettingsDialog(settings, soulseek_status="Connected to Soulseek.")
    assert dialog.soulseek_status.text() == "Connected to Soulseek."
    assert not hasattr(dialog, "slskd_url")

    dialog.soulseek_username.setText("Me")
    dialog.soulseek_password.setText("pw")
    dialog.share_check.setChecked(False)
    dialog._save()

    assert settings.value("soulseek_username") == "Me"
    assert settings.value("soulseek_password") == "pw"
    assert settings.value("share_library") in (False, "false")


def test_lyrics_picker_lists_every_source_in_one_table(qapp):
    from lyrics_fetch import LyricsCandidate

    dialog = dialogs.LyricsPickerDialog("Manu Chao", "Clandestino", 150.0)
    dialog._show_candidates([
        LyricsCandidate("lrclib", "Manu Chao", "Clandestino", "", 150.0, "[00:01.00] a\n", None, "1"),
        LyricsCandidate("qq", "Manu Chao", "Clandestino (Live)", "", 190.0, "[00:01.00] b\n", None, "2"),
    ])

    assert not hasattr(dialog, "other_button")
    headers = [dialog.table.horizontalHeaderItem(i).text() for i in range(dialog.table.columnCount())]
    assert headers == ["Title", "Artist", "Source", "Length", "Timing"]
    assert [dialog.table.item(r, 2).text() for r in range(2)] == ["LRCLIB", "QQ Music"]
    assert dialog.table.item(1, 0).text() == "Clandestino (Live)"


def test_settings_fields_are_not_squashed(qapp, tmp_path):
    from PyQt6.QtCore import QSettings

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    dialog = dialogs.SettingsDialog(settings, soulseek_status="Connected to Soulseek.")
    dialog.show()
    qapp.processEvents()

    for field in (dialog.soulseek_username, dialog.soulseek_password, dialog.library_dir):
        assert field.height() >= field.minimumSizeHint().height()
    dialog.close()


def test_an_error_in_the_tag_dialog_does_not_squash_the_fields(qapp, monkeypatch):
    def boom(*_a):
        raise ValueError("a fairly long error message that will need to wrap onto a second line")

    monkeypatch.setattr(dialogs, "read_artist_title", lambda path: ("A", "B"))
    monkeypatch.setattr(dialogs, "write_artist_title", boom)
    dialog = dialogs.TagEditDialog("/m/song.mp3")
    dialog.show()
    qapp.processEvents()

    dialog._save()
    qapp.processEvents()

    assert dialog.artist_input.height() >= dialog.artist_input.minimumSizeHint().height()
    dialog.close()
