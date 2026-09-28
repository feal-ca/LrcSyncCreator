"""Tests for lrc_studio/library_panel.py's LibraryPanel — the persistent
file-list sidebar extracted from app.py's old _build_file_panel.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from library_panel import LibraryPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def panel(qapp, settings):
    return LibraryPanel(settings)


def test_starts_with_no_real_files(panel):
    assert panel.real_file_paths() == []


def test_add_files_appends_real_paths(panel, tmp_path):
    f1 = tmp_path / "a.mp3"
    f2 = tmp_path / "b.mp3"
    f1.write_bytes(b"")
    f2.write_bytes(b"")

    panel.add_files([str(f1), str(f2)])

    assert set(panel.real_file_paths()) == {str(f1), str(f2)}


def test_add_files_dedupes_already_present_paths(panel, tmp_path):
    f1 = tmp_path / "a.mp3"
    f1.write_bytes(b"")

    panel.add_files([str(f1)])
    panel.add_files([str(f1)])

    assert panel.real_file_paths() == [str(f1)]
