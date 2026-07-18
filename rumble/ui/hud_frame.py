"""A rounded glass container that paints sci-fi-HUD corner bracket ticks.

The corner ticks are the defining element of the "rounded glass HUD" look: short
L-shaped accent strokes hugging each rounded corner, with a faint wider accent
stroke underneath for a neon glow. They are painted by the frame itself in
``paintEvent`` — no ``QGraphicsEffect`` — so a HudFrame may safely wrap the
pyqtgraph EQ graph or a ``QScrollArea`` (which a drop-shadow effect would break).

The body (fill / gradient / radius / border) still comes from the theme QSS via
the frame's ``class`` property, so HudFrames inherit the engine's glass treatment
and only add the ticks on top.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QWidget

from rumble.ui.density import dp
from rumble.ui.theme.tokens import theme_float


class HudFrame(QFrame):
    """QFrame with theme-driven HUD corner brackets."""

    def __init__(self, parent: QWidget | None = None,
                 class_name: str = "panel"):
        super().__init__(parent)
        if class_name:
            self.setProperty("class", class_name)
        self._accent = "#737B8B"
        self._glow = 1.0
        self._radius_scale = 1.0

    def set_theme(self, tokens: dict) -> None:
        self._accent = tokens.get("accent", self._accent)
        self._glow = theme_float(tokens, "glow", 1.0)
        self._radius_scale = theme_float(tokens, "radius_scale", 1.0)
        self.update()

    # ── painting ─────────────────────────────────────────────────────
    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)  # let the stylesheet draw the glass body
        w, h = self.width(), self.height()
        if w < 24 or h < 24:
            return

        inset = 2
        # Corner radius mirrors the engine's r_md (= 8 * radius_scale), so the
        # brackets begin exactly where each rounded corner's arc ends.
        radius = max(1, round(8 * self._radius_scale))
        arm = dp(15, 11)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        glow_alpha = max(0, min(255, round(70 * self._glow)))
        glow = QColor(self._accent)
        glow.setAlpha(glow_alpha)
        core = QColor(self._accent)

        # Each corner: (origin x, origin y, x-direction, y-direction).
        corners = (
            (inset,         inset,         1,  1),   # top-left
            (w - inset,     inset,        -1,  1),   # top-right
            (inset,         h - inset,     1, -1),   # bottom-left
            (w - inset,     h - inset,    -1, -1),   # bottom-right
        )

        def draw(pen: QPen) -> None:
            p.setPen(pen)
            for ox, oy, sx, sy in corners:
                # Horizontal arm starts past the corner arc, runs inward.
                hx0 = ox + sx * radius
                p.drawLine(hx0, oy, hx0 + sx * arm, oy)
                # Vertical arm, likewise.
                vy0 = oy + sy * radius
                p.drawLine(ox, vy0, ox, vy0 + sy * arm)

        if glow_alpha > 0:
            glow_pen = QPen(glow, 5)
            glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            draw(glow_pen)

        core_pen = QPen(core, 2)
        core_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        draw(core_pen)
        p.end()
