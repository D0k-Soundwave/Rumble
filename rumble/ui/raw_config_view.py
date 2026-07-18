"""Read-only pane showing the exact APO config text RUMBLE will write, with
minimal syntax highlighting and a copy-to-clipboard button."""
from __future__ import annotations

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import (
    QFont, QGuiApplication, QSyntaxHighlighter, QTextCharFormat,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from rumble.ui import fonts, icons
from rumble.ui.a11y import describe

_COMMAND_KEYWORDS = (
    "Filter", "Preamp", "Include", "Device", "Channel", "GraphicEQ",
    "Convolution", "Delay", "Copy", "Stage", "If", "ElseIf", "Else", "EndIf", "Eval",
)
_FILTER_TYPES = (
    "PK", "PEQ", "Modal", "LP", "LPQ", "HP", "HPQ", "BP",
    "LS", "LSC", "HS", "HSC", "NO", "AP", "IIR",
)


class ConfigHighlighter(QSyntaxHighlighter):
    def __init__(self, document, theme: dict):
        super().__init__(document)
        self.theme = theme
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self._build_rules()

    def _fmt(self, color: str, *, italic: bool = False, bold: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(Qt.GlobalColor.white)
        from PySide6.QtGui import QColor
        fmt.setForeground(QColor(color))
        if italic:
            fmt.setFontItalic(True)
        if bold:
            fmt.setFontWeight(QFont.Weight.DemiBold)
        return fmt

    def _build_rules(self) -> None:
        t = self.theme
        self._rules = []

        keyword_fmt = self._fmt(t["accent"], bold=True)
        for kw in _COMMAND_KEYWORDS:
            self._rules.append((QRegularExpression(rf"\b{kw}\b"), keyword_fmt))

        type_fmt = self._fmt(t["success"], bold=True)
        for ftype in _FILTER_TYPES:
            self._rules.append((QRegularExpression(rf"\b{ftype}\b"), type_fmt))

        onoff_fmt = self._fmt(t["warning"], bold=True)
        self._rules.append((QRegularExpression(r"\b(ON|OFF)\b"), onoff_fmt))

        number_fmt = self._fmt(t["text_primary"])
        self._rules.append((QRegularExpression(r"[-+]?\b\d+(\.\d+)?\b"), number_fmt))

        comment_fmt = self._fmt(t["text_muted"], italic=True)
        self._rules.append((QRegularExpression(r"#.*$"), comment_fmt))

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self._build_rules()
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt override
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


class RawConfigView(QWidget):
    """Collapsible-friendly read-only raw config text pane with copy button."""

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("RAW CONFIG")
        title.setProperty("class", "heading")
        header.addWidget(title)
        header.addStretch(1)

        self.btn_copy = QPushButton(" Copy")
        self.btn_copy.setProperty("class", "ghost")
        self.btn_copy.setIcon(icons.icon("content_copy", theme["text_secondary"], 14))
        self.btn_copy.clicked.connect(self._copy_to_clipboard)
        describe(self.btn_copy, "Copy raw config",
                 "Copy the full configuration text to the clipboard, ready to "
                 "paste into a text file or another EQ tool.")
        header.addWidget(self.btn_copy)
        layout.addLayout(header)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setProperty("class", "raw-config")
        self.text_edit.setReadOnly(True)
        self.text_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text_edit.setFont(fonts.mono_font(10))
        self.text_edit.setMinimumHeight(140)
        describe(self.text_edit, "Raw configuration text",
                 "Read-only preview of the exact Equalizer APO commands this "
                 "profile writes when saved or activated.")
        layout.addWidget(self.text_edit)

        self._highlighter = ConfigHighlighter(self.text_edit.document(), theme)

    def set_text(self, text: str) -> None:
        if self.text_edit.toPlainText() != text:
            self.text_edit.setPlainText(text)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.btn_copy.setIcon(icons.icon("content_copy", theme["text_secondary"], 14))
        self._highlighter.set_theme(theme)

    def _copy_to_clipboard(self) -> None:
        QGuiApplication.clipboard().setText(self.text_edit.toPlainText())
