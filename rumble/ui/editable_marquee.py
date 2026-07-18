"""A header field that reads like static text and marquee-scrolls on hover when
it overflows, but turns into a real line edit while you are editing it.

The editor header's profile name and description used to be plain ``QLineEdit``s:
long text scrolled awkwardly left/right as the caret moved while a row was
highlighted. This keeps inline editing (click to edit) but presents a fixed-
width, hover-scrolling label the rest of the time — matching the profile
picker's marquee behaviour.

It exposes the small slice of the ``QLineEdit`` API the editor relies on
(``text`` / ``setText`` / ``setReadOnly`` / ``textEdited``) so it drops in
without touching the surrounding logic.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLineEdit, QStackedLayout, QWidget

from rumble.ui.marquee_label import MarqueeLabel


class EditableMarqueeField(QWidget):
    """Marquee label at rest, swapping to a line edit while being edited."""

    textEdited = Signal(str)

    def __init__(self, *, label_class: str, edit_class: str, placeholder: str,
                 text_color: str, placeholder_color: str,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._placeholder = placeholder
        self._text_color = text_color
        self._placeholder_color = placeholder_color
        self._readonly = False

        # Both children occupy the same cell; only one is visible at a time.
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self.label = MarqueeLabel("")
        self.label.setProperty("class", label_class)
        self.label.setCursor(Qt.CursorShape.IBeamCursor)
        self.label.installEventFilter(self)

        self.edit = QLineEdit()
        self.edit.setProperty("class", edit_class)
        self.edit.setPlaceholderText(placeholder)
        self.edit.textEdited.connect(self._on_edit)
        self.edit.editingFinished.connect(self._show_label)

        self._stack.addWidget(self.label)
        self._stack.addWidget(self.edit)
        self._stack.setCurrentWidget(self.label)
        self._sync_label()

    # ── QLineEdit-compatible API ──────────────────────────────────────
    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt-style API
        self.edit.setText(text or "")
        self._sync_label()
        if not self.edit.hasFocus():
            self._stack.setCurrentWidget(self.label)

    def setReadOnly(self, readonly: bool) -> None:  # noqa: N802
        self._readonly = bool(readonly)
        self.edit.setReadOnly(self._readonly)
        self.label.setCursor(Qt.CursorShape.ArrowCursor if self._readonly
                             else Qt.CursorShape.IBeamCursor)
        if self._readonly:
            self._show_label()

    def set_colors(self, text_color: str, placeholder_color: str) -> None:
        self._text_color = text_color
        self._placeholder_color = placeholder_color
        self._sync_label()

    # ── internals ─────────────────────────────────────────────────────
    def _on_edit(self, text: str) -> None:
        self.textEdited.emit(text)

    def _sync_label(self) -> None:
        text = self.edit.text()
        if text:
            self.label.setText(text)
            self.label.set_active_color(QColor(self._text_color))
        else:
            self.label.setText(self._placeholder)
            self.label.set_active_color(QColor(self._placeholder_color))

    def _show_label(self) -> None:
        self._sync_label()
        self._stack.setCurrentWidget(self.label)

    def _start_editing(self) -> None:
        if self._readonly:
            return
        self._stack.setCurrentWidget(self.edit)
        self.edit.setFocus(Qt.FocusReason.MouseFocusReason)
        self.edit.selectAll()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        if (obj is self.label and not self._readonly
                and event.type() == event.Type.MouseButtonPress):
            self._start_editing()
            return True
        return super().eventFilter(obj, event)
