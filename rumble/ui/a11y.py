"""Accessibility helpers.

`describe` is the single place widgets get their hover text and screen-reader
metadata: the same plain-language summary feeds the tooltip (hover), the
accessible description (Windows UI Automation / screen readers), and the
accessible name (the control's spoken label).

`announce*` push live updates (value/state changes) to assistive technology via
`QAccessible` events so external screen readers (NVDA/JAWS) treat them as live
updates — used by the keyboard-navigable EQ graph.
"""
from __future__ import annotations

import textwrap

from PySide6.QtWidgets import QWidget

#: Qt's plain-text QToolTip only wraps at the screen edge, so a long summary
#: renders as one line stretching most of the monitor. Wrapping to a fixed
#: character width keeps every tooltip a readable block instead.
_TOOLTIP_WRAP_CHARS = 50


def wrap_tooltip(text: str) -> str:
    """Wrap plain tooltip text into a readable block (~50 chars/line) rather
    than one line spanning the screen. Safe to call on short text too — it's
    a no-op below the wrap width."""
    return textwrap.fill(text, width=_TOOLTIP_WRAP_CHARS)


def describe(widget: QWidget, name: str, summary: str) -> None:
    """Attach a hover tooltip and screen-reader name/description to a widget.

    `name` is the short control label (e.g. "Pre-Amp"); `summary` is one or two
    plain sentences saying what the control is and what it is used for. The
    tooltip is word-wrapped for readability; the accessible description keeps
    `summary` verbatim (screen readers don't need — and shouldn't get —
    visual line-wrap artifacts).
    """
    widget.setToolTip(wrap_tooltip(summary))
    widget.setAccessibleName(name)
    widget.setAccessibleDescription(summary)


def announce(widget: QWidget, message: str, assertive: bool = False) -> None:
    """Push a live message to assistive technology.

    Uses `QAccessibleAnnouncementEvent` (Qt 6.8+) where available so screen
    readers treat `message` as a live-region update, falling back to a value
    change event.
    """
    try:
        from PySide6.QtGui import QAccessible

        try:
            from PySide6.QtGui import QAccessibleAnnouncementEvent

            ev = QAccessibleAnnouncementEvent(widget, message)
            try:
                pol = (QAccessible.AnnouncementPoliteness.Assertive if assertive
                       else QAccessible.AnnouncementPoliteness.Polite)
                ev.setPoliteness(pol)
            except Exception:
                pass
            QAccessible.updateAccessibility(ev)
        except Exception:
            announce_value(widget, message)
    except Exception:
        pass


def announce_value(widget: QWidget, value_str: str) -> None:
    """Notify assistive tech that a widget's value changed."""
    try:
        from PySide6.QtGui import QAccessible, QAccessibleValueChangeEvent

        QAccessible.updateAccessibility(QAccessibleValueChangeEvent(widget, value_str))
    except Exception:
        pass
