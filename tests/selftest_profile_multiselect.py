"""Regression test for multi-selection in the profile panel.

The tree uses ExtendedSelection (Windows-style Ctrl/Shift multi-select). This
covers: the selection mode, selected_profile_ids() across several rows, the
Delete-key and F2 shortcuts routing to batch/single signals, and that extending
a multi-selection does NOT re-emit profile_selected (which would reload the
editor for every touched row).

Run:  python tests/selftest_profile_multiselect.py
Exits non-zero on failure."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QTreeWidget

from rumble.core.profile_manager import ProfileMeta
from rumble.ui.profile_panel import ProfilePanel
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def main() -> int:
    app = QApplication([])
    theme = get_theme(DEFAULT_THEME_NAME)

    profiles = [
        ProfileMeta(id="a", name="Alpha", tags=[]),
        ProfileMeta(id="b", name="Bravo", tags=[]),
        ProfileMeta(id="c", name="Charlie", tags=[]),
        ProfileMeta(id="d", name="Delta", tags=[], readonly=True),
    ]
    panel = ProfilePanel(theme)
    panel.set_profiles(profiles)

    captured: dict = {}
    sel_emits: list[str] = []
    panel.profiles_delete_requested.connect(lambda ids: captured.update(deleted=list(ids)))
    panel.profile_rename_requested.connect(lambda pid, _n: captured.update(renamed=pid))
    panel.profile_selected.connect(lambda pid: sel_emits.append(pid))

    print("[1] selection mode")
    check(panel.tree.selectionMode() == QTreeWidget.SelectionMode.ExtendedSelection,
          "tree uses ExtendedSelection (Ctrl/Shift multi-select)")

    print("\n[2] selected_profile_ids across several rows")
    for pid in ("a", "b", "c"):
        panel._item_by_id[pid].setSelected(True)
    check(set(panel.selected_profile_ids()) == {"a", "b", "c"},
          "selected_profile_ids returns all selected profile rows")

    print("\n[3] Delete key routes the selection to the batch delete signal")
    panel.tree.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
    check(set(captured.get("deleted", [])) == {"a", "b", "c"},
          "Key_Delete emits profiles_delete_requested with every selected id")

    print("\n[4] F2 renames only when exactly one is selected")
    captured.pop("renamed", None)
    panel.tree.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F2, Qt.KeyboardModifier.NoModifier))
    check("renamed" not in captured, "F2 with several selected does nothing")
    panel.tree.clearSelection()
    panel._item_by_id["b"].setSelected(True)
    panel.tree.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F2, Qt.KeyboardModifier.NoModifier))
    check(captured.get("renamed") == "b", "F2 with one selected requests its rename")

    print("\n[5] extending a multi-selection doesn't thrash profile_selected")
    sel_emits.clear()
    panel.tree.clearSelection()
    panel._item_by_id["a"].setSelected(True)          # single -> should emit once
    single_emits = len(sel_emits)
    panel._item_by_id["b"].setSelected(True)          # now multi -> must NOT emit
    panel._item_by_id["c"].setSelected(True)          # still multi -> must NOT emit
    check(single_emits == 1, "selecting one profile emits profile_selected once")
    check(len(sel_emits) == 1, "extending to a multi-selection emits no further reloads")

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
