"""
main.py — Entry point for LRC Studio.

Run with:
    python main.py
"""

import sys

from PyQt6.QtWidgets import QApplication

import theme
from app import LrcSyncApp


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("LRC Studio")
    theme.apply(app)

    window = LrcSyncApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
