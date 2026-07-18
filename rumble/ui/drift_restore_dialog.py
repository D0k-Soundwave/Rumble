"""Modal listing preserved config.txt drift snapshots (content displaced when
RUMBLE re-asserted ownership after config.txt was externally clobbered or
co-written by a foreign tool) so the user can browse and restore one. Shown
on demand from Settings -> Backup & Restore."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from rumble.ui.a11y import describe
from rumble.ui.frameless import FramelessDialog


class DriftRestoreDialog(FramelessDialog):
    """Pick a single drift snapshot to restore. Read the result via
    `selected_snapshot()` after `exec()` returns `Accepted`."""

    def __init__(self, snapshots: list, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self._snapshots = list(snapshots)

        self.setWindowTitle("Restore Drift Snapshot")
        self.setModal(True)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self.body)
        root.setSpacing(12)

        intro = QLabel(
            "These are earlier versions of config.txt that RUMBLE preserved "
            "after another tool or an APO reinstall overwrote it. Restoring one "
            "replaces the current config.txt with that snapshot and stops "
            "RUMBLE managing it (use Settings to resume management afterward).")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        root.addWidget(intro)

        self.list_widget = QListWidget()
        for snap in self._snapshots:
            item = QListWidgetItem(snap.label)
            item.setData(1, snap)
            self.list_widget.addItem(item)
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        describe(self.list_widget, "Drift snapshots",
                 "Preserved earlier versions of config.txt, newest first. "
                 "Select one to restore it.")
        root.addWidget(self.list_widget, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Restore Selected…")
        ok.setProperty("class", "primary")
        ok.setEnabled(bool(self._snapshots))
        describe(ok, "Restore selected snapshot",
                 "Replace config.txt with the selected snapshot's content.")
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        describe(cancel, "Cancel", "Close without restoring anything.")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def selected_snapshot(self):
        """The snapshot the user picked, or None."""
        item = self.list_widget.currentItem()
        return item.data(1) if item is not None else None
