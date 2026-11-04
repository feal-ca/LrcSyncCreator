"""theme.py — the app's one visual theme.

Graphite surfaces, warm paper-white text, and a single periwinkle accent
spent on exactly three things: the karaoke line being sung, the primary
action on a page, and focus / selection. Status is shown by shape *and*
color (filled sage dot = synced, sand dot = plain text only, hollow ring =
no lyrics) so 900 library rows don't turn into a wall of colored text.
Red is reserved for errors and destructive actions.

Everything visual lives here as tokens plus one global stylesheet;
widgets opt into variants with dynamic properties
(``btn.setProperty("variant", "primary")``) instead of carrying their
own inline styles.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPen, QPixmap

# ── Tokens ────────────────────────────────────────────────────────────────
BASE        = "#18191C"   # window
SURFACE     = "#1F2024"   # lists, tables, panels
RAISED      = "#2A2C31"   # buttons, inputs at rest
HOVER       = "#33353B"
LINE        = "#34363C"   # borders
LINE_SOFT   = "#26282C"
TEXT        = "#ECE8E1"   # warm paper white
TEXT_2      = "#9A968F"   # secondary
TEXT_3      = "#66635E"   # faint / disabled
ACCENT      = "#8F8CF7"   # periwinkle
ACCENT_HI   = "#A9A7FA"
ACCENT_BG   = "#2C2B47"   # accent-tinted selection background
ON_ACCENT   = "#15142B"
OK          = "#82B38A"   # synced
PLAIN       = "#CDB179"   # lyrics present but not synced
DANGER      = "#D9655B"   # errors, destructive actions
DANGER_BG   = "#3A211F"

UI_FAMILIES   = ["Adwaita Sans", "Cantarell", "Noto Sans", "Sans Serif"]
MONO_FAMILIES = ["Adwaita Mono", "JetBrains Mono", "DejaVu Sans Mono", "Monospace"]

_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def mono_font(point_size: float = 10, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setPointSizeF(point_size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    return font


def _qss() -> str:
    check = os.path.join(_ASSETS, "check.svg").replace("\\", "/")
    chevron = os.path.join(_ASSETS, "chevron.svg").replace("\\", "/")
    mono = ", ".join(f'"{f}"' for f in MONO_FAMILIES)
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog, QMessageBox {{ background: {BASE}; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {LINE}; padding: 5px 7px; }}

QMenuBar {{ background: {BASE}; padding: 2px 6px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 5px; }}
QMenuBar::item:selected {{ background: {RAISED}; }}
QMenu {{ background: {SURFACE}; border: 1px solid {LINE}; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {RAISED}; }}
QMenu::item:disabled {{ color: {TEXT_3}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 5px 8px; }}

QPushButton, QToolButton {{
    background: {RAISED}; border: 1px solid {LINE}; border-radius: 7px;
    padding: 6px 13px;
}}
QPushButton:hover, QToolButton:hover {{ background: {HOVER}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {LINE}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {TEXT_3}; background: {SURFACE}; border-color: {LINE_SOFT}; }}
QPushButton:focus, QToolButton:focus {{ border-color: {ACCENT}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QPushButton[variant="primary"], QToolButton[variant="primary"] {{
    background: {ACCENT}; color: {ON_ACCENT}; border: 1px solid {ACCENT}; font-weight: 600;
}}
QPushButton[variant="primary"]:hover, QToolButton[variant="primary"]:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton[variant="primary"]:disabled {{ background: #34335A; color: #7E7CA8; border-color: #34335A; }}
QPushButton[variant="danger"] {{ background: transparent; color: {DANGER}; border: 1px solid #5A2E2A; }}
QPushButton[variant="danger"]:hover {{ background: {DANGER_BG}; }}
QPushButton[variant="ghost"], QToolButton[variant="ghost"] {{
    background: transparent; border: 1px solid transparent; color: {TEXT_2};
}}
QPushButton[variant="ghost"]:hover, QToolButton[variant="ghost"]:hover {{ background: {RAISED}; color: {TEXT}; }}
QPushButton[variant="ghost"]:checked {{ background: {RAISED}; color: {TEXT}; border-color: {LINE}; }}
QPushButton[variant="round"] {{
    background: {TEXT}; color: {BASE}; border: none; border-radius: 19px;
    font-size: 15px; font-weight: 700; padding: 0;
}}
QPushButton[variant="round"]:hover {{ background: {ACCENT}; }}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {SURFACE}; border: 1px solid {LINE}; border-radius: 7px; padding: 5px 9px;
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled {{ color: {TEXT_3}; }}
QLineEdit[variant="search"] {{ padding: 7px 11px; font-size: 11pt; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url({chevron}); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; border: 1px solid {LINE}; padding: 4px;
    selection-background-color: {RAISED}; selection-color: {TEXT};
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}

QListView, QListWidget, QTreeView, QTableView, QTableWidget {{
    background: {SURFACE}; border: 1px solid {LINE_SOFT}; border-radius: 9px;
    alternate-background-color: #222327;
}}
QListView::item, QListWidget::item {{ padding: 5px 6px; border-radius: 5px; }}
QTableView::item, QTableWidget::item {{ padding: 3px 6px; }}
QListView::item:hover, QListWidget::item:hover, QTableView::item:hover, QTableWidget::item:hover {{ background: #26282C; }}
QListView::item:selected, QListWidget::item:selected, QTableView::item:selected, QTableWidget::item:selected {{
    background: {ACCENT_BG}; color: {TEXT};
}}
QHeaderView {{ background: {SURFACE}; border: none; }}
QHeaderView::section {{
    background: {SURFACE}; color: {TEXT_2}; border: none; border-bottom: 1px solid {LINE};
    padding: 7px 8px; font-weight: 600;
}}
QHeaderView::section:hover {{ color: {TEXT}; }}
QTableCornerButton::section {{ background: {SURFACE}; border: none; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 3px 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px 3px; }}
QScrollBar::handle {{ background: {LINE}; border-radius: 4px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {TEXT_3}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSlider::groove:horizontal {{ height: 4px; background: {LINE}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {TEXT_2}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: {ACCENT}; }}
QSlider#SeekBar::sub-page:horizontal {{ background: {ACCENT}; }}

QProgressBar {{
    background: {RAISED}; border: none; border-radius: 3px; max-height: 6px;
    color: transparent; text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

QCheckBox {{ spacing: 8px; color: {TEXT_2}; }}
QCheckBox:hover {{ color: {TEXT}; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {TEXT_3}; border-radius: 4px; background: {SURFACE}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({check}); }}

QSplitter::handle {{ background: {BASE}; }}
QSplitter::handle:horizontal {{ width: 8px; }}

QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_2}; padding: 8px 4px 7px 4px; margin-right: 18px;
    border: none; border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}

QFrame#PlayerBar {{ background: {SURFACE}; border-top: 1px solid {LINE_SOFT}; }}
QFrame#Sidebar {{ background: transparent; }}
QFrame#Card {{ background: {SURFACE}; border: 1px solid {LINE_SOFT}; border-radius: 10px; }}

QLabel[role="muted"] {{ color: {TEXT_2}; }}
QLabel[role="faint"] {{ color: {TEXT_3}; }}
QLabel[role="heading"] {{ font-size: 13pt; font-weight: 700; }}
QLabel[role="title"] {{ font-size: 18pt; font-weight: 800; }}
QLabel[role="error"] {{ color: {DANGER}; }}
QLabel[role="ok"] {{ color: {OK}; }}
QLabel[role="mono"] {{ font-family: {mono}; color: {TEXT_2}; }}
QLabel#NowPlaying {{ font-weight: 600; }}
QLabel#KaraokeCurrent {{ color: {ACCENT}; font-size: 23pt; font-weight: 800; }}
QLabel#KaraokeContext {{ color: {TEXT_3}; font-size: 12pt; }}
QWidget#KaraokePanel {{ background: {SURFACE}; border: 1px solid {LINE_SOFT}; border-radius: 12px; }}
"""


def apply(app) -> None:
    """Install the palette, font and stylesheet on a QApplication."""
    app.setStyle("Fusion")

    font = QFont()
    font.setFamilies(UI_FAMILIES)
    font.setPointSizeF(10)
    app.setFont(font)

    p = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, BASE),
        (QPalette.ColorRole.WindowText, TEXT),
        (QPalette.ColorRole.Base, SURFACE),
        (QPalette.ColorRole.AlternateBase, "#222327"),
        (QPalette.ColorRole.ToolTipBase, RAISED),
        (QPalette.ColorRole.ToolTipText, TEXT),
        (QPalette.ColorRole.Text, TEXT),
        (QPalette.ColorRole.Button, RAISED),
        (QPalette.ColorRole.ButtonText, TEXT),
        (QPalette.ColorRole.Highlight, ACCENT),
        (QPalette.ColorRole.HighlightedText, ON_ACCENT),
        (QPalette.ColorRole.PlaceholderText, TEXT_3),
        (QPalette.ColorRole.Link, ACCENT),
    ):
        p.setColor(role, QColor(color))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(TEXT_3))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(TEXT_3))
    app.setPalette(p)
    app.setStyleSheet(_qss())


def set_variant(widget, variant: str) -> None:
    """Switch a widget's stylesheet variant at runtime (re-polishes it)."""
    widget.setProperty("variant", variant)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def set_role(label, role: str) -> None:
    label.setProperty("role", role)
    style = label.style()
    style.unpolish(label)
    style.polish(label)


_ICON_CACHE: dict[str, QIcon] = {}


def status_icon(status: str) -> QIcon:
    """Small dot for a library row: "synced" (filled sage), "plain"
    (filled steel), "missing" (hollow ring)."""
    if status in _ICON_CACHE:
        return _ICON_CACHE[status]
    size = 14
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(3.5, 3.5, 7, 7)
    if status == "synced":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(OK))
        painter.drawEllipse(rect)
    elif status == "plain":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(PLAIN))
        painter.drawEllipse(rect)
    else:
        painter.setPen(QPen(QColor(TEXT_3), 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)
    painter.end()
    icon = QIcon(pix)
    _ICON_CACHE[status] = icon
    return icon
