"""
main.py — Entry point for LRC Studio.

Run with:
    python main.py
"""

import sys

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from constants import (
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_FG,
    COLOR_TIMELINE_BG,
)
from app import LrcSyncApp


def _build_palette() -> QPalette:
    """Return a Catppuccin Mocha palette applied globally via Fusion style."""
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window,          COLOR_BG)
    p.setColor(QPalette.ColorRole.WindowText,      COLOR_FG)
    p.setColor(QPalette.ColorRole.Base,            COLOR_TIMELINE_BG)
    p.setColor(QPalette.ColorRole.AlternateBase,   COLOR_BG)
    p.setColor(QPalette.ColorRole.ToolTipBase,     COLOR_BG)
    p.setColor(QPalette.ColorRole.ToolTipText,     COLOR_FG)
    p.setColor(QPalette.ColorRole.Text,            COLOR_FG)
    p.setColor(QPalette.ColorRole.Button,          QColor("#313244"))
    p.setColor(QPalette.ColorRole.ButtonText,      COLOR_FG)
    p.setColor(QPalette.ColorRole.Highlight,       COLOR_ACCENT)
    p.setColor(QPalette.ColorRole.HighlightedText, COLOR_BG)
    return p


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(_build_palette())

    window = LrcSyncApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
