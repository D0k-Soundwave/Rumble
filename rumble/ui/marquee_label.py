"""A QLabel that smoothly scrolls its text when it is too wide to fit.

Drop-in replacement for QLabel in width-constrained spots. When the text fits,
it behaves exactly like a normal label. When it overflows it shows an ellipsis
at rest and, while hovered (or while externally activated), scrolls the full
text in a seamless, frame-rate-independent loop with a gap between repetitions.

Only the currently animating label runs a timer, so scattering these across a
list costs nothing until the pointer is actually over one.
"""
from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class MarqueeLabel(QLabel):
    """QLabel that marquee-scrolls overflowing text smoothly on hover."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *,
                 speed: float = 15.0, gap: int = 44, hover_scroll: bool = True,
                 always_scroll: bool = False):
        super().__init__(text, parent)
        self._speed = float(speed)   # pixels per second
        self._gap = int(gap)         # px between the looped repetitions
        self._hover_scroll = hover_scroll
        self._always = always_scroll  # scroll whenever overflowing, no trigger
        self._offset = 0.0
        self._hovered = False
        self._forced = False
        self._color_override: QColor | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60 fps
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()

        # Accept whatever width the layout gives us — never demand the full text
        # width (that would defeat the point and stretch the row).
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    # ── public API ────────────────────────────────────────────────────
    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setText(text)
        self._reset()

    def set_active_color(self, color: QColor | None) -> None:
        """Override the text colour (e.g. accent for the active profile)."""
        self._color_override = color
        self.update()

    def set_scroll_active(self, active: bool) -> None:
        """Externally drive scrolling (e.g. while the owning row is highlighted
        or selected), independent of pointer hover."""
        active = bool(active)
        if active == self._forced:
            return
        self._forced = active
        self._update_running()

    # ── geometry helpers ──────────────────────────────────────────────
    def _text_width(self) -> int:
        return QFontMetrics(self.font()).horizontalAdvance(self.text())

    def _overflows(self) -> bool:
        return self._text_width() > self.contentsRect().width()

    def _should_run(self) -> bool:
        return self._overflows() and (
            self._always or self._forced or (self._hover_scroll and self._hovered))

    def _update_running(self) -> None:
        if self._should_run():
            if not self._timer.isActive():
                self._clock.restart()
                self._timer.start()
        elif self._timer.isActive() or self._offset:
            self._timer.stop()
            self._offset = 0.0
            self.update()

    def _reset(self) -> None:
        self._offset = 0.0
        self._update_running()
        self.update()

    def _tick(self) -> None:
        self._offset += self._speed * (self._clock.restart() / 1000.0)
        period = self._text_width() + self._gap
        if period > 0 and self._offset >= period:
            self._offset -= period
        self.update()

    # ── events ────────────────────────────────────────────────────────
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._update_running()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._update_running()
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_running()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._update_running()  # kick off always-on scrolling once visible

    def _pen_color(self) -> QColor:
        if self._color_override is not None:
            return self._color_override
        return self.palette().color(QPalette.ColorRole.WindowText)

    def paintEvent(self, event) -> None:  # noqa: N802
        # Fits, not scrolling, no colour override → behave exactly like a normal
        # QLabel. (With an override we must custom-paint so the colour applies in
        # the static case too, not only while scrolling.)
        if (not self._timer.isActive() and not self._overflows()
                and self._color_override is None):
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setPen(self._pen_color())
        rect = self.contentsRect()
        fm = QFontMetrics(self.font())
        baseline = rect.y() + (rect.height() + fm.ascent() - fm.descent()) // 2

        if not self._timer.isActive():
            # Overflowing but idle → static, elided.
            elided = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
            painter.drawText(rect.x(), baseline, elided)
            return

        # Scrolling → draw the text twice, a gap apart, for a seamless loop.
        text = self.text()
        period = self._text_width() + self._gap
        x = rect.x() - int(self._offset)
        painter.setClipRect(rect)
        painter.drawText(x, baseline, text)
        painter.drawText(x + period, baseline, text)
