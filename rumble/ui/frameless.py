"""Frameless-window chrome: a drag region plus close/minimize buttons that
stand in for the native OS titlebar. QWindow.startSystemMove() (PySide6
6.11.1, the version pinned in .venv) does the drag -- no raw Win32/DWM code
needed, matching the project's "minimal chrome fidelity" decision (no drop
shadow, no Aero-snap, no system menu).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from rumble.ui import icons
from rumble.ui.a11y import describe
from rumble.ui.density import dp
from rumble.ui.theme.tokens import FALLBACK_THEME

#: Height of a dialog's drag-strip. Matches the app's existing "slim strip"
#: precedent -- QStatusBar.setFixedHeight(dp(24, 20)) in main_window.py's
#: _build_status_bar -- rather than an invented number.
CHROME_STRIP_HEIGHT = dp(24, 20)

#: Standard content size (width, height, excluding CHROME_STRIP_HEIGHT) every
#: pop-out dialog in the app is fixed to -- matches SettingsDialog, the app's
#: largest/most complex dialog, so nothing else needs to be bigger. A caller
#: adds CHROME_STRIP_HEIGHT itself, the same way SettingsDialog does:
#: setFixedSize(STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT +
#: CHROME_STRIP_HEIGHT). A dialog whose content doesn't fit wraps it in a
#: QScrollArea rather than growing past this.
STANDARD_DIALOG_WIDTH = 760
STANDARD_DIALOG_HEIGHT = 580


def scrollable_page(page: QWidget) -> QScrollArea:
    """Wrap one QStackedWidget page so it scrolls inside the dialog instead
    of stretching it. QStackedWidget's minimum size is the maximum over ALL
    its pages (hidden ones included), so one long page would otherwise force
    every other page -- and the dialog itself -- taller than
    STANDARD_DIALOG_HEIGHT. A scroll area's own minimum is small, so the
    dialog keeps its fixed size and long pages scroll within the frame
    without dragging short pages' content down with them (originated in
    settings_dialog.py's SettingsDialog, the first dialog fixed at the
    standard size)."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    # QAbstractScrollArea fills its viewport with the Base colour by default,
    # which would paint a flat rectangle over the dialog's glass backdrop.
    scroll.viewport().setAutoFillBackground(False)
    # setWidgetResizable stretches the page to fill the viewport on any page
    # shorter than the dialog (matching its width is the point -- no
    # horizontal scrollbar -- but it does the same on height, which reads as
    # a big dead footer below the last control). Capping the vertical policy
    # at Maximum keeps the page at its own natural height; a page taller
    # than the viewport still scrolls; Maximum only forbids growing past it.
    page.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    scroll.setWidget(page)
    return scroll

#: Chrome button glyph/box size, matching the icon size + control height
#: convention used elsewhere for compact icon buttons (icons.icon(..., 14)
#: inside a control sized to ctl_min_h = dp(22, 18) in theme_engine.py).
_ICON_SIZE = 14
_BUTTON_BOX = dp(22, 18)


class _DragFiller(QWidget):
    """Empty, stretchy widget: a left-press anywhere on it starts an OS
    window move, standing in for dragging the native titlebar."""

    def __init__(self, window: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self._window = window
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)


def make_drag_filler(window: QWidget, parent: QWidget | None = None) -> QWidget:
    """A stretchy widget that drags *window* when left-pressed."""
    return _DragFiller(window, parent)


def make_chrome_button(window: QWidget, *, icon_name: str, on_click,
                        class_name: str, theme: dict[str, str]) -> QPushButton:
    """Shared factory for the close/minimize buttons."""
    btn = QPushButton()
    btn.setProperty("class", class_name)
    btn.setIcon(icons.icon(icon_name, theme["text_secondary"], _ICON_SIZE))
    btn.setFixedSize(_BUTTON_BOX, _BUTTON_BOX)
    btn.setCursor(Qt.CursorShape.ArrowCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.clicked.connect(on_click)
    return btn


def make_close_button(window: QWidget, theme: dict[str, str]) -> QPushButton:
    btn = make_chrome_button(
        window, icon_name="close", on_click=window.close,
        class_name="chrome-close", theme=theme)
    describe(btn, "Close", "Close this window.")
    return btn


def make_minimize_button(window: QWidget, theme: dict[str, str]) -> QPushButton:
    btn = make_chrome_button(
        window, icon_name="minimize", on_click=window.showMinimized,
        class_name="chrome-minimize", theme=theme)
    describe(btn, "Minimize", "Minimize this window.")
    return btn


class FramelessDialog(QDialog):
    """Base class for RUMBLE's dialogs: a frameless window with its own
    drag strip + close button in place of the native titlebar. Subclasses
    build their content onto `self.body` instead of `self` -- the only
    structural change required to migrate an existing QDialog subclass."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # OR the flag in, never replace -- QDialog's implicit Qt.Dialog type
        # (taskbar omission, modal stacking) must survive.
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        strip = QWidget(self)
        strip.setProperty("class", "chrome-strip")
        strip.setFixedHeight(CHROME_STRIP_HEIGHT)
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(8, 0, 4, 0)
        strip_layout.setSpacing(4)

        self._chrome_title = QLabel("", strip)
        self._chrome_title.setProperty("class", "chrome-title")
        strip_layout.addWidget(self._chrome_title)
        strip_layout.addWidget(make_drag_filler(self, strip), 1)

        theme = getattr(parent, "theme", None) or _fallback_theme(self)
        strip_layout.addWidget(make_close_button(self, theme))

        outer.addWidget(strip)

        self.body = QWidget(self)
        outer.addWidget(self.body, 1)

    def setWindowTitle(self, title: str) -> None:  # noqa: N802 - Qt override
        super().setWindowTitle(title)
        if hasattr(self, "_chrome_title"):
            self._chrome_title.setText(title)


def _fallback_theme(widget: QWidget) -> dict[str, str]:
    """Walk up the parent chain for a `.theme` dict (every dialog's parent
    is ultimately MainWindow, which always has one) so the chrome close
    button can be built before the rest of the dialog's own theme wiring.
    Falls back to the app's default theme when there is no parent (e.g. a
    dialog instantiated directly, as several offscreen selftests do) so the
    chrome button always has every token it needs, not just in the normal
    MainWindow-parented path."""
    node = widget.parent()
    while node is not None:
        theme = getattr(node, "theme", None)
        if theme:
            return theme
        node = node.parent()
    return FALLBACK_THEME
