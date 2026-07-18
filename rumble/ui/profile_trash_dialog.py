"""Modal listing deleted profiles preserved in the recycle bin
(`data_dir/trash/`, see ProfileManager.delete_profile) so the user can restore
or permanently purge one. Shown on demand from Settings -> Backup & Restore.

Unlike DriftRestoreDialog (a single pick-then-close action), this dialog stays
open across an action so the user can restore/purge several entries in one
visit -- it never touches the manager itself; the main window handles the
signals and calls set_entries() afterward to refresh the list in place."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Signal

from rumble.ui.a11y import describe
from rumble.ui.frameless import FramelessDialog


class ProfileTrashDialog(FramelessDialog):
    """List deleted profiles; Restore/Purge act on the current selection and
    emit a signal for the main window to carry out, then call set_entries()
    to refresh this dialog's list without closing it."""

    restore_requested = Signal(object)  # TrashedProfile
    purge_requested = Signal(object)    # TrashedProfile

    def __init__(self, entries: list, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme

        self.setWindowTitle("Deleted Profiles")
        self.setModal(True)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self.body)
        root.setSpacing(12)

        intro = QLabel(
            "Deleted profiles are kept here until restored or purged. "
            "Restoring creates a new profile from the preserved copy; "
            "purging removes it for good.")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        root.addWidget(intro)

        self.list_widget = QListWidget()
        describe(self.list_widget, "Deleted profiles",
                 "Deleted profiles, newest first. Select one to restore or purge it.")
        root.addWidget(self.list_widget, 1)

        action_row = QHBoxLayout()
        self.btn_restore = QPushButton("Restore Selected")
        self.btn_restore.setProperty("class", "primary")
        self.btn_restore.clicked.connect(self._on_restore_clicked)
        describe(self.btn_restore, "Restore selected profile",
                 "Recreate the selected deleted profile as a new profile.")
        self.btn_purge = QPushButton("Purge Selected…")
        self.btn_purge.clicked.connect(self._on_purge_clicked)
        describe(self.btn_purge, "Purge selected profile",
                 "Permanently remove the selected profile from the recycle bin.")
        action_row.addWidget(self.btn_restore)
        action_row.addWidget(self.btn_purge)
        action_row.addStretch(1)
        root.addLayout(action_row)

        self.set_entries(entries)

    def set_entries(self, entries: list) -> None:
        """Repopulate the list (called by the main window after a restore or
        purge) without closing the dialog."""
        self._entries = list(entries)
        previous_row = self.list_widget.currentRow()
        self.list_widget.clear()
        for entry in self._entries:
            label = f"{entry.name}  —  deleted {entry.label}"
            item = QListWidgetItem(label)
            item.setData(1, entry)
            self.list_widget.addItem(item)
        has_entries = bool(self._entries)
        if has_entries:
            self.list_widget.setCurrentRow(min(previous_row, len(self._entries) - 1)
                                           if previous_row >= 0 else 0)
        self.btn_restore.setEnabled(has_entries)
        self.btn_purge.setEnabled(has_entries)

    def selected_entry(self):
        item = self.list_widget.currentItem()
        return item.data(1) if item is not None else None

    def _on_restore_clicked(self) -> None:
        entry = self.selected_entry()
        if entry is not None:
            self.restore_requested.emit(entry)

    def _on_purge_clicked(self) -> None:
        entry = self.selected_entry()
        if entry is not None:
            self.purge_requested.emit(entry)
