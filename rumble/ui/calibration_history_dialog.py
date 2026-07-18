"""Per-listener saved-run history management for Ear Calibration
(Frequency Calibration's Test A) -- lets the user delete one, several, or
every saved run for the active Listener Slot, then resyncs the working
calibration state to whatever remains (listener_slots.
recompile_calibration_from_runs) so a deleted run stops feeding live
correction. Distinct from the dashboard's "Clear All" (which wipes the
working state but keeps history intact): this dialog manages the history
itself, and the working state follows it.

Reachable from the Ear Calibration dashboard's "Manage History..." button
(hearing_dialog.py), only shown there when the active listener has at
least one saved run.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from rumble.core import listener_slots
from rumble.core.hearing_test import STANDARD_FREQS
from rumble.ui.a11y import describe
from rumble.ui.frameless import CHROME_STRIP_HEIGHT, FramelessDialog

_TOTAL_POINTS = len(STANDARD_FREQS) * 2


def _format_run_timestamp(filename: str) -> str:
    """"20260713T192932Z.json" / "...Z_1.json" (hearing_test.save_results'
    naming, disambiguation suffix and extension included) -> "2026-07-13
    19:29 UTC". Falls back to the raw filename if it's ever something
    save_results didn't produce (should not happen in practice)."""
    try:
        stamp = datetime.strptime(filename[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return filename
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


class CalibrationHistoryDialog(FramelessDialog):
    """List the active Listener Slot's saved Ear Calibration runs; delete
    a selection (or all of them), then recompile the working calibration
    from whatever remains."""

    #: Emitted after any successful delete, so the dashboard reloads its
    #: in-memory state from disk and re-renders the chips/status text.
    history_changed = Signal()

    def __init__(self, data_dir, slot_id: int, slot_label: str,
                 theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self._data_dir = data_dir
        self._slot_id = slot_id

        self.setWindowTitle(f"Calibration History — {slot_label}")
        self.setModal(True)
        self.setFixedSize(520, 460 + CHROME_STRIP_HEIGHT)

        root = QVBoxLayout(self.body)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        intro = QLabel(
            "Every saved run for this listener, oldest first. Deleting one "
            "removes it from Previous Results, and if it was the most "
            "recent run, the working calibration rolls back to whatever "
            "run is now the newest. Deleting all of them leaves this "
            "listener with zero data -- the test must be run again before "
            "calibration does anything.")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        root.addWidget(intro)

        self.chk_select_all = QCheckBox("Select all")
        self.chk_select_all.toggled.connect(self._on_select_all_toggled)
        root.addWidget(self.chk_select_all)

        self.list_widget = QListWidget()
        describe(self.list_widget, "Saved runs",
                 "Saved calibration runs for this listener. Check one or "
                 "more, then Delete Selected.")
        root.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        self.btn_delete = QPushButton("Delete Selected…")
        self.btn_delete.clicked.connect(self._on_delete_clicked)
        describe(self.btn_delete, "Delete selected runs",
                 "Permanently delete the checked runs from this listener's "
                 "saved history.")
        btn_row.addWidget(self.btn_delete)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        self._refresh()

    def _runs(self) -> list[tuple]:
        """(path, payload) pairs, oldest to newest -- slot_run_files() and
        load_slot_runs() share the same glob/sort, so zipping them lines
        each file up with its parsed contents without a third data shape."""
        paths = listener_slots.slot_run_files(self._data_dir, self._slot_id)
        payloads = listener_slots.load_slot_runs(self._data_dir, self._slot_id)
        return list(zip(paths, payloads))

    def _refresh(self) -> None:
        self.list_widget.clear()
        runs = self._runs()
        for path, payload in runs:
            raw = payload.get("raw", {})
            measured = sum(1 for ear in ("left", "right")
                           for v in raw.get(ear, {}).values() if v is not None)
            resolved = sum(len(raw.get(ear, {})) for ear in ("left", "right"))
            label = (f"{_format_run_timestamp(path.name)}  —  "
                     f"{measured} of {_TOTAL_POINTS} points measured")
            if resolved != measured:
                label += f" ({resolved - measured} skipped/limit)"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, path.name)
            self.list_widget.addItem(item)
        self.chk_select_all.blockSignals(True)
        self.chk_select_all.setChecked(False)
        self.chk_select_all.blockSignals(False)
        has_runs = bool(runs)
        self.chk_select_all.setEnabled(has_runs)
        self.btn_delete.setEnabled(has_runs)

    def _on_select_all_toggled(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(state)

    def _checked_filenames(self) -> list[str]:
        return [
            self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _on_delete_clicked(self) -> None:
        filenames = self._checked_filenames()
        if not filenames:
            return
        all_selected = len(filenames) == self.list_widget.count()
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Delete Calibration Runs")
        if all_selected:
            msg.setText(
                "Delete all saved runs for this listener? The working "
                "calibration will be cleared too -- the test must be run "
                "again before calibration does anything.")
        else:
            msg.setText(
                f"Delete {len(filenames)} saved run(s)? If the most recent "
                "one is among them, the working calibration rolls back to "
                "whatever run is now the newest.")
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        msg.setEscapeButton(QMessageBox.StandardButton.Cancel)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return
        listener_slots.delete_slot_runs(self._data_dir, self._slot_id, filenames)
        listener_slots.recompile_calibration_from_runs(self._data_dir, self._slot_id)
        self._refresh()
        self.history_changed.emit()
