"""Shared QFont builders for painted / non-QSS contexts.

Most of the app gets its type system from the theme QSS (see
``theme_engine._generate_qss`` and ``__main__._load_fonts``):

    Inter         — UI / body text
    Space Grotesk — display / headers / wordmark
    IBM Plex Mono — numeric readouts (dB / Hz / Q)

A few surfaces are painted by QPainter or by libraries (e.g. pyqtgraph axis
ticks) that take a ``QFont`` rather than a stylesheet. Those can't reference the
QSS families, so they build the font here. Keeping the fallback chains in one
place means the painted text stays in lock-step with the QSS roles.
"""
from __future__ import annotations

from PySide6.QtGui import QFont

# Family fallback chains, mirroring the QSS in theme_engine so painted text and
# styled text resolve to the same faces.
_MONO_CHAIN = ("IBM Plex Mono", "Cascadia Code", "Consolas")
_DISPLAY_CHAIN = ("Space Grotesk", "Inter", "Segoe UI")


def _first_available(families: tuple[str, ...]) -> QFont:
    """Return a QFont on the first family that resolves exactly, else the last
    (a guaranteed-present system fallback)."""
    for family in families:
        font = QFont(family)
        if font.exactMatch():
            return font
    return QFont(families[-1])


def mono_font(point_size: int | None = None) -> QFont:
    """IBM Plex Mono (→ Cascadia Code → Consolas) for numeric readouts."""
    font = _first_available(_MONO_CHAIN)
    if point_size is not None:
        font.setPointSize(point_size)
    return font


def display_font(point_size: int | None = None) -> QFont:
    """Space Grotesk (→ Inter → Segoe UI) for headers / wordmark."""
    font = _first_available(_DISPLAY_CHAIN)
    if point_size is not None:
        font.setPointSize(point_size)
    return font
