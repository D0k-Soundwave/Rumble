"""Foreign-tool coexistence dashboard (Rumble 4.8, docs/PLANNED_WORK.md #2) —
read-only, on-demand snapshot (matching Config Health's convention) of
signals that another Equalizer-APO-consuming tool is touching, or has
touched, the same config: foreign `Include:`/directive content currently in
config.txt, preserved drift snapshots, and known competing APO-client GUIs
(Peace, Equalizer APO's own Editor) currently running. Shown from
Tools -> Foreign-Tool Coexistence…"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from rumble.core.foreign_tool_detection import CoexistenceReport
from rumble.ui.a11y import describe
from rumble.ui.frameless import CHROME_STRIP_HEIGHT, FramelessDialog


def _info_banner(text: str, theme: dict) -> QFrame:
    frame = QFrame()
    frame.setProperty("class", "info-banner")
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(16, 12, 16, 12)
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme['text_primary']};")
    layout.addWidget(label)
    return frame


def _section(title: str, body: str, theme: dict) -> QWidget:
    wrap = QVBoxLayout()
    wrap.setSpacing(2)
    heading = QLabel(title)
    heading.setStyleSheet(f"color: {theme['text_primary']}; font-weight: 600;")
    wrap.addWidget(heading)
    text = QLabel(body)
    text.setWordWrap(True)
    text.setProperty("class", "secondary")
    wrap.addWidget(text)
    container = QWidget()
    container.setLayout(wrap)
    return container


class CoexistenceDialog(FramelessDialog):
    """Snapshot view of a `CoexistenceReport`. Emits `restore_drift_requested`
    when the user asks to browse/restore preserved drift snapshots — the
    caller (MainWindow) already owns that whole flow via
    `_on_restore_drift_snapshot()`, reused here rather than duplicated."""

    restore_drift_requested = Signal()

    def __init__(self, report: CoexistenceReport, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme

        self.setWindowTitle("Foreign-Tool Coexistence")
        self.setModal(True)
        # Original minimum plus FramelessDialog's chrome strip on top, so
        # existing content keeps its original vertical budget. Fixed (not
        # just a floor) for consistency with every other migrated dialog --
        # FramelessWindowHint already removes the user-visible resize grip.
        self.setFixedSize(560, 420 + CHROME_STRIP_HEIGHT)

        root = QVBoxLayout(self.body)
        root.setSpacing(14)
        root.setContentsMargins(20, 20, 20, 20)

        intro = QLabel(
            "What else might be touching the same Equalizer APO config, based on "
            "what RUMBLE can actually detect right now.")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        root.addWidget(intro)

        if not report.has_any_signal:
            root.addWidget(_info_banner(
                "No foreign tools or config drift detected. RUMBLE appears "
                "to be the sole owner of config.txt.", theme))

        if report.foreign_includes:
            targets = ", ".join(report.foreign_includes)
            root.addWidget(_section(
                "Foreign Include: targets active right now",
                f"config.txt currently includes content RUMBLE doesn't own: {targets}. "
                "This is usually another tool's save file (e.g. Peace's peace.txt).",
                theme))

        if report.foreign_raw_directives:
            count = len(report.foreign_raw_directives)
            plural = "s" if count != 1 else ""
            root.addWidget(_section(
                f"{count} foreign directive line{plural} in config.txt",
                "Lines were written directly into config.txt (not via an Include:) "
                "alongside RUMBLE's managed block — either a hand edit or another "
                "tool writing straight into the file.",
                theme))

        if report.drift_snapshot_count:
            plural = "s" if report.drift_snapshot_count != 1 else ""
            row = QHBoxLayout()
            label = QLabel(
                f"{report.drift_snapshot_count} preserved drift snapshot{plural} on disk"
                + (f" — most recent {report.latest_drift_label}." if report.latest_drift_label else "."))
            label.setWordWrap(True)
            label.setProperty("class", "secondary")
            row.addWidget(label, 1)
            view_btn = QPushButton("View & Restore…")
            describe(view_btn, "View & restore drift snapshots",
                     "Open the drift snapshot picker to inspect or restore an earlier config.txt.")
            view_btn.clicked.connect(self.restore_drift_requested.emit)
            row.addWidget(view_btn)
            root.addLayout(row)

        if report.running_tools:
            names = ", ".join(report.running_tools)
            root.addWidget(_section(
                "Competing APO-client tools currently running",
                f"{names}. Having more than one EQ GUI open at once risks them "
                "overwriting each other's changes to config.txt. Note: this is a "
                "process-name match only (e.g. \"Editor.exe\" is a common filename), "
                "so treat it as a hint, not certainty.",
                theme))

        root.addStretch(1)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
