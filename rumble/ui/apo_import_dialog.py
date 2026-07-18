"""Modal offering to import existing Equalizer APO configs as RUMBLE
profiles. Shown on first run (and on demand from Settings) when the boot
reconcile finds real, non-boilerplate APO configuration that RUMBLE doesn't yet
manage. Each candidate is a tick-box; unticked items are left untouched."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDialogButtonBox, QFrame, QLabel, QScrollArea,
    QVBoxLayout, QWidget,
)

from rumble.ui.a11y import describe
from rumble.ui.frameless import FramelessDialog


class ApoImportDialog(FramelessDialog):
    """Pick which discovered APO configs to import. Read the result via
    `selected()` after `exec()` returns `Accepted`."""

    def __init__(self, candidates: list, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self._candidates = list(candidates)
        self._checks: list[QCheckBox] = []

        self.setWindowTitle("Import Equalizer APO configuration")
        self.setModal(True)
        self.setMinimumWidth(500)

        root = QVBoxLayout(self.body)
        root.setSpacing(12)

        intro = QLabel(
            "RUMBLE found existing Equalizer APO configuration. Import it as "
            "editable RUMBLE profiles so you can manage everything in one "
            "place. Your original setup has been saved as a backup either way; "
            "unticked items are left untouched.")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        root.addWidget(intro)

        listw = QWidget()
        ll = QVBoxLayout(listw)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        for c in self._candidates:
            cb = QCheckBox(f"{c.name}    ·    {c.source}")
            cb.setChecked(True)
            describe(cb, f"Import {c.name}",
                     f"Import '{c.source}' as a RUMBLE profile named "
                     f"'{c.name}'. Untick to leave it alone.")
            ll.addWidget(cb)
            self._checks.append(cb)
        ll.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(listw)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Import selected")
        ok.setProperty("class", "primary")
        describe(ok, "Import selected",
                 "Import the ticked configs as RUMBLE profiles.")
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("Not now")
        describe(cancel, "Not now",
                 "Skip importing. RUMBLE won't offer these configs again.")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def selected(self) -> list:
        """The candidates whose tick-box is checked."""
        return [c for c, cb in zip(self._candidates, self._checks) if cb.isChecked()]
