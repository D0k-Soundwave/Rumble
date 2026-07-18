"""Read-only "what's actually applying" view of the live Equalizer APO
config.txt chain (docs/PLANNED_WORK.md #3) — shows the fully-resolved,
Include:-flattened effective config, which file each part came from, and how
many raw If/ElseIf/Else/EndIf conditional blocks were found. Shown on demand
from Tools -> Config Health…, computed fresh (on-demand) each time it opens."""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from rumble.core.profile_manager import EffectiveConfigReport
from rumble.ui.a11y import describe
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH, FramelessDialog,
)
from rumble.ui.raw_config_view import RawConfigView


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


class ConfigHealthDialog(FramelessDialog):
    """Snapshot view of an `EffectiveConfigReport` — no live auto-refresh;
    reopen to see current state, since config.txt only changes on
    save/activate or an external tool writing to it."""

    def __init__(self, report: EffectiveConfigReport, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme

        self.setWindowTitle("Config Health")
        self.setModal(True)
        self.setFixedSize(STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT + CHROME_STRIP_HEIGHT)

        root = QVBoxLayout(self.body)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        intro = QLabel(
            "The fully-resolved Equalizer APO config chain — config.txt with every "
            "Include: target inlined, exactly as Equalizer APO itself will read it.")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        root.addWidget(intro)

        if report.conditional_block_count:
            plural = "s" if report.conditional_block_count != 1 else ""
            root.addWidget(_info_banner(
                f"{report.conditional_block_count} conditional block{plural} "
                "(If/ElseIf/Else/EndIf) found below. RUMBLE shows this logic "
                "raw — Equalizer APO evaluates which branch actually applies at "
                "runtime, not RUMBLE", theme))

        if report.sources:
            sources_label = QLabel("Sources: " + " → ".join(report.sources))
            sources_label.setWordWrap(True)
            sources_label.setProperty("class", "muted")
            describe(sources_label, "Contributing files",
                     "Every file whose content was inlined into the config "
                     "shown below, in the order Equalizer APO would read them.")
            root.addWidget(sources_label)

        self.raw_view = RawConfigView(theme)
        self.raw_view.set_text(report.resolved_text or "# config.txt is empty or missing.")
        root.addWidget(self.raw_view, 1)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.raw_view.set_theme(theme)
