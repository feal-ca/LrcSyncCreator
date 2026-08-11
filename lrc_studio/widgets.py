"""
widgets.py — Reusable Qt widgets: ``Verse`` (a single lyric row) and
             ``TimelineWidget`` (the interactive scrub/drag timeline).
"""

from __future__ import annotations

from PyQt6.QtCore import (
    QEvent, QPoint, QRect, Qt, pyqtSignal,
)
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QWidget,
)

from constants import (
    BUTTON_SIZE,
    COLOR_ACCENT,
    COLOR_MISSING,
    COLOR_TIMELINE_ACTIVE,
    COLOR_TIMELINE_BG,
    COLOR_TIMELINE_BLOCK,
    COLOR_TIMELINE_SEL,
    COLOR_SEL_BOX,
    COLOR_SYNCED,
)
from utils import parse_lrc_timestamp


# ===========================================================================
# TimelineWidget
# ===========================================================================

class TimelineWidget(QWidget):
    """Visual timeline showing verse positions and allowing drag-to-adjust.

    Signals
    -------
    time_changed_bulk(updates)
        Emitted during a drag with a list of ``(verse_index, new_ms)`` pairs.
    seek_requested(ms)
        Emitted when the user clicks/drags the top scrub zone.
    """

    time_changed_bulk = pyqtSignal(list)   # list[tuple[int, int]]
    seek_requested    = pyqtSignal(int)

    # Height of the clickable scrub zone at the very top of the widget.
    _SCRUB_ZONE_PX: int = 15
    # Half-width of the hit-test area around each verse diamond (px).
    _HIT_THRESHOLD: int = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(90)
        self.setStyleSheet(
            f"background-color: {COLOR_TIMELINE_BG.name()};"
            " border: 1px solid #45475a; border-radius: 5px;"
        )

        self.duration_ms:        int       = 1
        self.timestamps:         list[int] = []
        self.current_play_pos:   int       = 0
        self.active_verse_index: int       = -1
        self.selected_indices:   set[int]  = set()

        # Drag state
        self._drag_start:     QPoint | None = None
        self._is_dragging:    bool          = False
        self._is_box_select:  bool          = False
        self._is_scrubbing:   bool          = False
        self._selection_rect: QRect         = QRect()
        self._initial_timestamps: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_data(self, verses: list, duration_ms: int) -> None:
        """Reload from a verse list (any object with ``get_timestamp()``)."""
        self.duration_ms = max(1, duration_ms)
        self.timestamps  = [parse_lrc_timestamp(v.get_timestamp()) for v in verses]
        self.update()

    def set_position(self, pos_ms: int) -> None:
        self.current_play_pos = pos_ms
        self.update()

    def set_active_index(self, index: int) -> None:
        self.active_verse_index = index
        self.update()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _ms_to_x(self, ms: int) -> float:
        return (ms / self.duration_ms) * self.width()

    def _x_to_ms(self, x: float) -> int:
        return int((x / self.width()) * self.duration_ms)

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, _event: QEvent) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), COLOR_TIMELINE_BG)

        # ── 30-second grid lines ─────────────────────────────────────
        painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
        step = 30_000
        for t in range(0, self.duration_ms, step):
            x = int(self._ms_to_x(t))
            painter.drawLine(x, 0, x, self.height())

        # ── Verse diamonds ───────────────────────────────────────────
        mid_y   = self.height() / 2
        base_w  = 10

        for i, ms in enumerate(self.timestamps):
            x = self._ms_to_x(ms)

            if i in self.selected_indices:
                color = COLOR_TIMELINE_SEL
                # Dashed vertical guide for selected blocks
                painter.setPen(
                    QPen(QColor(255, 255, 255, 30), 1, Qt.PenStyle.DashLine)
                )
                painter.drawLine(int(x), 0, int(x), self.height())
            elif i == self.active_verse_index:
                color = COLOR_TIMELINE_ACTIVE
            else:
                color = COLOR_TIMELINE_BLOCK

            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.save()
            painter.translate(x, mid_y)
            painter.rotate(45)
            s = base_w + 4 if i == self.active_verse_index else base_w
            painter.drawRect(int(-s / 2), int(-s / 2), s, s)
            painter.restore()

        # ── Playhead ─────────────────────────────────────────────────
        px = int(self._ms_to_x(self.current_play_pos))
        painter.setPen(QPen(COLOR_ACCENT, 2))
        painter.drawLine(px, 0, px, self.height())
        painter.setBrush(QBrush(COLOR_ACCENT))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon([
            QPoint(px, 0),
            QPoint(px - 6, 10),
            QPoint(px + 6, 10),
        ])

        # ── Rubber-band selection box ─────────────────────────────────
        if self._is_box_select:
            painter.setBrush(QBrush(COLOR_SEL_BOX))
            painter.setPen(QPen(COLOR_ACCENT, 1))
            painter.drawRect(self._selection_rect)

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return

        self._drag_start = event.pos()

        # ── Scrub zone (top strip) ────────────────────────────────────
        if event.pos().y() < self._SCRUB_ZONE_PX:
            self._is_scrubbing = True
            self.seek_requested.emit(self._x_to_ms(event.pos().x()))
            return

        # ── Hit-test for a verse diamond ─────────────────────────────
        clicked = self._get_index_at(event.pos().x())
        if clicked != -1:
            mods = QApplication.keyboardModifiers()
            if clicked not in self.selected_indices:
                if not (mods & Qt.KeyboardModifier.ControlModifier):
                    self.selected_indices.clear()
                self.selected_indices.add(clicked)
            self._is_dragging = True
            self._initial_timestamps = {
                i: self.timestamps[i] for i in self.selected_indices
            }
        else:
            # Missed all diamonds — start rubber-band selection.
            self._is_dragging   = False
            self._is_box_select = True
            self.selected_indices.clear()
            self._selection_rect = QRect(event.pos(), event.pos())

        self.update()

    def mouseMoveEvent(self, event: QEvent) -> None:  # type: ignore[override]
        if not self._drag_start:
            return

        if self._is_scrubbing:
            ms = self._x_to_ms(event.pos().x())
            self.seek_requested.emit(max(0, min(ms, self.duration_ms)))

        elif self._is_dragging:
            delta_px = event.pos().x() - self._drag_start.x()
            # Convert pixel delta to time delta (proportional, not absolute).
            delta_ms = int((delta_px / self.width()) * self.duration_ms)
            updates: list[tuple[int, int]] = []
            for i in self.selected_indices:
                original = self._initial_timestamps.get(i, 0)
                new_ms   = max(0, min(original + delta_ms, self.duration_ms))
                self.timestamps[i] = new_ms
                updates.append((i, new_ms))
            self.time_changed_bulk.emit(updates)
            self.update()

        elif self._is_box_select:
            self._selection_rect = QRect(
                self._drag_start, event.pos()
            ).normalized()
            new_sel: set[int] = set()
            for i, ms in enumerate(self.timestamps):
                x = self._ms_to_x(ms)
                if self._selection_rect.left() <= x <= self._selection_rect.right():
                    new_sel.add(i)
            self.selected_indices = new_sel
            self.update()

    def mouseReleaseEvent(self, _event: QEvent) -> None:  # type: ignore[override]
        self._drag_start      = None
        self._is_dragging     = False
        self._is_box_select   = False
        self._is_scrubbing    = False
        self._selection_rect  = QRect()
        self.update()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_index_at(self, x_pos: float) -> int:
        """Return the index of the verse diamond nearest *x_pos*, or -1."""
        for i, ms in enumerate(self.timestamps):
            if abs(x_pos - self._ms_to_x(ms)) < self._HIT_THRESHOLD:
                return i
        return -1


# ===========================================================================
# Verse
# ===========================================================================

class Verse(QWidget):
    """A single lyric row: timestamp label + text field + action buttons.

    The row emits ``jump_signal`` when the user left-clicks anywhere on the
    verse body (excluding the text field focus area), allowing the main
    window to seek the player to that timestamp.
    """

    jump_signal = pyqtSignal(object)       # emits self, on click
    advance_requested = pyqtSignal(object)  # emits self, on Enter in the text field

    # Combined styles for the four (active x playing) states. `set_active`
    # marks the keyboard-nav / sync target; `set_playing` marks whichever
    # line is currently audible. They're independent so live playback
    # highlighting never fights with the sync workflow's selection cursor.
    _STYLE = (
        "#verse_inner {{"
        "  background-color: {bg};"
        "  border: {border};"
        "  border-left: {left_accent};"
        "  border-radius: 6px;"
        "}}"
    )

    def __init__(
        self,
        timestamp: str = "",
        verse: str     = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("verse")
        self._is_active = False
        self._is_playing = False
        self._build_ui(timestamp, verse)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, timestamp: str, verse: str) -> None:
        outer = QHBoxLayout()
        outer.setContentsMargins(0, 0, 0, 1)
        outer.setSpacing(0)

        # ── Inner container (styled background + border) ──────────────
        self.inner = QWidget()
        self.inner.setObjectName("verse_inner")
        self.inner.setStyleSheet(self._compute_style())
        self.inner.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inner.installEventFilter(self)

        inner_layout = QHBoxLayout(self.inner)
        inner_layout.setContentsMargins(8, 4, 8, 4)
        inner_layout.setSpacing(8)

        # Timestamp label
        self.timestamp_label = QLabel(timestamp)
        self.timestamp_label.setFixedWidth(70)
        self.timestamp_label.setStyleSheet(
            f"color: {COLOR_ACCENT.name()};"
            " font-family: monospace; font-weight: bold; font-size: 13px;"
        )
        inner_layout.addWidget(self.timestamp_label)

        # Lyrics text field
        self.verse_text = QLineEdit(verse)
        self.verse_text.setStyleSheet(
            "border: none; background: transparent; color: white; font-size: 14px;"
        )
        self.verse_text.setPlaceholderText("Lyrics here…")
        self.verse_text.installEventFilter(self)
        inner_layout.addWidget(self.verse_text, stretch=1)

        # ── Action buttons ────────────────────────────────────────────
        _btn = (
            "QPushButton { border-radius: 4px; font-weight: bold; padding: 2px; }"
            "QPushButton:hover { background-color: #45475a; }"
        )

        self.sync_button = self._make_button("⏱", "#4a34aa", _btn, "Sync to Now")
        self.add_button  = self._make_button("+",  "#313244", _btn, "Insert Line After")
        self.delete_button = self._make_button(
            "✕", COLOR_MISSING.name(), _btn, "Delete Line"
        )

        inner_layout.addWidget(self.sync_button)
        inner_layout.addWidget(self.add_button)
        inner_layout.addWidget(self.delete_button)

        outer.addWidget(self.inner)
        self.setLayout(outer)

    @staticmethod
    def _make_button(
        text: str,
        bg: str,
        extra_style: str,
        tooltip: str,
    ) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        btn.setStyleSheet(f"background-color: {bg}; {extra_style}")
        btn.setToolTip(tooltip)
        return btn

    # ------------------------------------------------------------------
    # Event filter — click on verse body → emit jump_signal
    # ------------------------------------------------------------------

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton  # type: ignore[attr-defined]
            and obj is self.inner
        ):
            self.jump_signal.emit(self)
            return False   # let the click propagate so child widgets still get it

        if (
            event.type() == QEvent.Type.KeyPress
            and obj is self.verse_text
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)  # type: ignore[attr-defined]
        ):
            self.advance_requested.emit(self)
            return True   # consume — QLineEdit has nothing useful to do with Enter

        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_timestamp(self) -> str:
        return self.timestamp_label.text()

    def get_verse_text(self) -> str:
        return self.verse_text.text()

    def set_timestamp(self, timestamp: str) -> None:
        self.timestamp_label.setText(timestamp)

    def set_active(self, active: bool) -> None:
        """Mark this line as the keyboard-nav / sync target (colored border)."""
        self._is_active = active
        self.inner.setStyleSheet(self._compute_style())

    def set_playing(self, playing: bool) -> None:
        """Mark this line as the one currently audible (left accent stripe).

        Independent of ``set_active`` so live playback following never
        overrides the line the user has manually selected for sync/nav.
        """
        self._is_playing = playing
        self.inner.setStyleSheet(self._compute_style())

    def _compute_style(self) -> str:
        if self._is_active:
            bg, border = "#3e3e5e", f"2px solid {COLOR_SYNCED.name()}"
        else:
            bg, border = "#2a2a3a", "1px solid #45475a"
        left_accent = (
            f"4px solid {COLOR_TIMELINE_ACTIVE.name()}" if self._is_playing
            else "4px solid transparent"
        )
        return self._STYLE.format(bg=bg, border=border, left_accent=left_accent)
