"""Soft drop-shadow elevation + accent glow for the glass theme look.

Qt stylesheets can't draw box-shadows, so genuine depth comes from
``QGraphicsDropShadowEffect`` applied per widget. This module decorates a small,
deliberate set of *leaf* elements only — cards, hero bands and primary buttons —
identified by their ``class`` property. It never touches the big container
panels, because wrapping the interactive EQ graph (pyqtgraph) in a graphics
effect would break its rendering.

Usage: call :func:`refresh_tree(root, tokens)` after a theme is applied. It is
idempotent and reuses an existing RUMBLE effect in place (only recolouring it), so
switching themes never flickers or rebuilds effects.
"""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractScrollArea, QGraphicsDropShadowEffect, QWidget

from rumble.ui.hud_frame import HudFrame
from rumble.ui.theme.tokens import _lum, theme_float

_FX_NAME = "rumble-fx"

# Widget class → effect role.
_GLOW = {"primary"}                       # accent halo (the signature glow)
# Soft neutral lift. "raised" is for safe leaf containers (no scroll area / no
# pyqtgraph child) that want genuine elevation, e.g. the editor header bar.
_ELEVATE = {"card", "card-selected", "hero", "raised"}

# The theme currently on screen, so callers without the token dict (dialogs)
# can still refresh from the live palette.
_current: dict[str, str] = {}


def set_current_theme(tokens: dict[str, str]) -> None:
    _current.clear()
    _current.update(tokens)


_f = theme_float  # shared structural-lever reader (rumble.ui.theme.tokens)


def _in_scroll_area(widget: QWidget) -> bool:
    """True when ``widget`` lives inside a scroll area's viewport.

    A ``QGraphicsDropShadowEffect`` on a scrolled child smears as it moves — the
    same reason the EQ graph is wrapped in a painted HudFrame rather than given a
    real shadow. So an elevatable ``card``/``hero``/``raised`` that happens to sit
    inside a dialog's QScrollArea is skipped, keeping the effect on safe leaf
    containers only.
    """
    p = widget.parentWidget()
    while p is not None:
        if isinstance(p, QAbstractScrollArea):
            return True
        p = p.parentWidget()
    return False


def _ensure_effect(widget: QWidget) -> QGraphicsDropShadowEffect:
    """Return the widget's RUMBLE shadow effect, creating it once if needed."""
    eff = widget.graphicsEffect()
    if isinstance(eff, QGraphicsDropShadowEffect) and eff.objectName() == _FX_NAME:
        return eff
    if eff is not None:
        return eff  # respect a foreign effect — never stomp it
    eff = QGraphicsDropShadowEffect(widget)
    eff.setObjectName(_FX_NAME)
    widget.setGraphicsEffect(eff)
    return eff


def _apply_glow(widget: QWidget, tokens: dict[str, str]) -> None:
    glow = _f(tokens, "glow", 1.0)
    eff = _ensure_effect(widget)
    if eff.objectName() != _FX_NAME:
        return
    c = QColor(tokens.get("accent", "#000000"))
    c.setAlpha(max(0, min(255, round(150 * glow))))
    eff.setColor(c)
    eff.setBlurRadius(max(0.0, 26.0 * glow))
    eff.setOffset(0, 0)  # even halo all around — reads as light, not weight


def _apply_elevation(widget: QWidget, tokens: dict[str, str], accent: bool) -> None:
    glow = _f(tokens, "glow", 1.0)
    light = _lum(tokens.get("bg_base", "#000000")) > 0.5
    eff = _ensure_effect(widget)
    if eff.objectName() != _FX_NAME:
        return
    if accent:
        c = QColor(tokens.get("accent", "#000000"))
        c.setAlpha(max(0, min(255, round(120 * glow))))
        eff.setBlurRadius(max(0.0, 22.0 * glow))
    else:
        c = QColor(0, 0, 0)
        # On light themes a soft cool shadow; on dark themes a deeper one so the
        # card separates from the surface behind it.
        c.setAlpha(70 if light else 150)
        eff.setBlurRadius(24.0)
    eff.setColor(c)
    eff.setOffset(0, 3 if not accent else 0)


def refresh_tree(root: QWidget, tokens: dict[str, str] | None = None) -> None:
    """Apply / recolour glass shadows on ``root`` and all its descendants."""
    t = tokens if tokens is not None else _current
    if not t or root is None:
        return
    widgets = [root, *root.findChildren(QWidget)]
    for w in widgets:
        if isinstance(w, HudFrame):
            w.set_theme(t)  # recolour painted corner ticks to the new accent
        cls = w.property("class")
        if not cls:
            continue
        if _in_scroll_area(w):
            continue  # never shadow a scrolled child — it smears as it moves
        if cls in _GLOW and w.property("no_glow"):
            continue  # opted out of the accent halo (e.g. editor Save button)
        if cls in _GLOW:
            _apply_glow(w, t)
        elif cls in _ELEVATE:
            _apply_elevation(w, t, accent=(cls == "card-selected"))
