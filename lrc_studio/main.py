"""
main.py — Entry point for LRC Studio.

Run with:
    python main.py
"""

import sys
import traceback

from PyQt6.QtWidgets import QApplication

import theme
from app import LrcSyncApp


def install_excepthook() -> None:
    """Report unexpected errors instead of letting PyQt abort the app (it
    calls abort() on an exception escaping a slot when no hook is set)."""
    def report(exc_type, exc, tb) -> None:
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
    sys.excepthook = report


def main() -> None:
    install_excepthook()
    app = QApplication(sys.argv)
    app.setApplicationName("LRC Studio")
    theme.apply(app)

    window = LrcSyncApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
