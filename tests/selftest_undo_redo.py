"""Self-test for editor undo/redo (snapshot history with commit debounce).

A burst of edits inside the 300 ms debounce coalesces into one history step;
undo restores values, structure, name and raw view and emits dirty; redo
replays; a fresh edit clears the redo branch; preview-only auditions (solo)
never create steps; loading a profile resets the history.

Run:  python tests/selftest_undo_redo.py
Uses Qt's offscreen platform plugin — no window is shown."""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication([])

QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard)

from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def step(label: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception:
        FAILURES.append(label)
        print(f"  FAIL  {label}\n{traceback.format_exc()}")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_undo_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.create_profile("Beta", config=make_flat_config(5))
    manager.activate_profile("alpha")

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    w = MainWindow(manager, theme_engine, QIcon())
    ep = w.editor_panel

    def flush_undo() -> None:
        app.processEvents()
        if ep._undo_commit_timer.isActive():
            ep._undo_commit_timer.stop()
            ep._commit_undo_snapshot()

    w._on_profile_selected("alpha")
    app.processEvents()

    def baseline_state() -> None:
        expect(not ep._history.can_undo, "a freshly loaded profile has nothing to undo")
        expect(not ep._history.can_redo, "…and nothing to redo")
        expect(not w.action_undo.isEnabled() and not w.action_redo.isEnabled(),
               "the Edit menu's Undo/Redo start disabled")

    step("history starts empty on load", baseline_state)

    def edits_coalesce_into_one_step() -> None:
        row = ep._filter_rows[0]
        row.spn_gain.setValue(2.0)
        row.spn_gain.setValue(4.0)
        row.spn_gain.setValue(6.0)
        flush_undo()
        expect(ep._history.can_undo, "a committed edit is undoable")
        ep.undo()
        app.processEvents()
        expect(abs(ep._filter_rows[0].spn_gain.value()) < 1e-9,
               f"one undo should revert the whole burst, got "
               f"{ep._filter_rows[0].spn_gain.value()}")
        expect(not ep._history.can_undo, "the burst was a single history step")

    step("a rapid edit burst coalesces into one undo step",
         edits_coalesce_into_one_step)

    def undo_marks_dirty_and_updates_raw() -> None:
        expect(w._dirty, "undo is a tracked state change (dirty)")
        expect("Gain 6.0" not in ep.raw_config_view.toPlainText()
               if hasattr(ep.raw_config_view, "toPlainText")
               else "Gain 6.0" not in ep.raw_config_view.text_edit.toPlainText(),
               "the raw view follows the undone state")

    step("undo marks dirty and refreshes the raw view",
         undo_marks_dirty_and_updates_raw)

    def redo_replays() -> None:
        expect(ep._history.can_redo, "an undone step is redoable")
        ep.redo()
        app.processEvents()
        expect(abs(ep._filter_rows[0].spn_gain.value() - 6.0) < 1e-9,
               "redo should restore the edit")

    step("redo replays the undone edit", redo_replays)

    def new_edit_clears_redo() -> None:
        ep.undo()
        app.processEvents()
        ep._filter_rows[1].spn_gain.setValue(3.0)
        flush_undo()
        expect(not ep._history.can_redo, "a fresh edit must clear the redo branch")
        expect(ep._history.can_undo, "…while remaining undoable itself")

    step("a new edit clears the redo branch", new_edit_clears_redo)

    def undo_across_structure_change() -> None:
        before = [s.channel for s in ep._channel_sections]
        ep._preset_combo.setCurrentText("5.1 Surround")
        app.processEvents()
        flush_undo()
        expect(len(ep._channel_sections) == 6, "precondition: 5.1 layout applied")
        ep.undo()
        app.processEvents()
        expect([s.channel for s in ep._channel_sections] == before,
               f"undo must restore the previous channel structure, got "
               f"{[s.channel for s in ep._channel_sections]}")

    step("undo restores a channel-structure change", undo_across_structure_change)

    def name_edit_is_undoable() -> None:
        old_name = ep.name_edit.text()
        ep.name_edit.setText("Alpha Renamed")
        ep._on_name_edited("Alpha Renamed")
        flush_undo()
        ep.undo()
        app.processEvents()
        expect(ep.name_edit.text() == old_name,
               f"undo should restore the profile name, got {ep.name_edit.text()!r}")

    step("profile name edits are undoable", name_edit_is_undoable)

    def solo_never_commits() -> None:
        depth_before = len(ep._history._undo)
        ep._filter_rows[0].btn_solo.setChecked(True)
        app.processEvents()
        flush_undo()
        ep._filter_rows[0].btn_solo.setChecked(False)
        app.processEvents()
        flush_undo()
        expect(len(ep._history._undo) == depth_before,
               "solo (preview-only) must not create history steps")

    step("preview-only auditions never create history steps", solo_never_commits)

    def menu_shortcuts_exist_and_fire() -> None:
        expect(hasattr(w, "action_undo") and hasattr(w, "action_redo"),
               "Undo/Redo actions should exist on the window")
        ep._filter_rows[0].spn_gain.setValue(9.0)
        flush_undo()
        w.action_undo.trigger()
        app.processEvents()
        # The restore rebuilds the rows — re-fetch before asserting.
        expect(abs(ep._filter_rows[0].spn_gain.value() - 9.0) > 1e-9,
               "the Undo action must drive the editor history")
        w.action_redo.trigger()
        app.processEvents()
        expect(abs(ep._filter_rows[0].spn_gain.value() - 9.0) < 1e-9,
               "the Redo action must drive the editor history")

    step("menu actions drive undo/redo", menu_shortcuts_exist_and_fire)

    def profile_switch_resets_history() -> None:
        w._on_profile_selected("beta")
        app.processEvents()
        expect(not ep._history.can_undo and not ep._history.can_redo,
               "switching profiles must reset the history")
        expect(not w.action_undo.isEnabled(), "Undo greys out on a fresh profile")

    step("switching profiles resets the history", profile_switch_resets_history)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
