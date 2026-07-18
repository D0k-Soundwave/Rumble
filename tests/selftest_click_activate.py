"""Self-test for sidebar click behaviour (Activate button removed).

Checks: no Activate button exists; single-clicking a profile in the sidebar
opens it in the editor WITHOUT activating it; double-clicking (activate) makes
it the live profile; double-clicking the already-open profile re-applies it;
unsaved changes still prompt before switching; saving asks for confirmation
before overwriting.

Run:  python tools/selftest_click_activate.py
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


def answer_question(button) -> None:
    QMessageBox.question = staticmethod(lambda *a, **k: button)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_clicktest_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.create_profile("Beta", config=make_flat_config(5))
    manager.activate_profile("alpha")

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)

    w = MainWindow(manager, theme_engine, QIcon())
    ep = w.editor_panel

    step("no Activate button on editor panel",
         lambda: expect(not hasattr(ep, "btn_activate"), "btn_activate still exists"))
    step("no activate signal on editor panel",
         lambda: expect(not hasattr(ep, "activate_requested"), "activate_requested still exists"))

    def startup_state() -> None:
        app.processEvents()
        expect(w._dirty is False, "editor is dirty right after startup")
        expect(w._loaded_profile_id == "alpha", f"expected alpha loaded, got {w._loaded_profile_id}")

    step("clean state after startup", startup_state)

    def click_opens_without_activating() -> None:
        w._on_profile_selected("beta")
        expect(w._loaded_profile_id == "beta", "beta should be open in the editor")
        active = manager.get_active_profile()
        expect(active is not None and active.id == "alpha",
               f"single-click must not activate; active should stay alpha, got {active and active.id}")

    step("single-click opens a profile without activating it", click_opens_without_activating)

    def double_click_activates() -> None:
        w._on_activate_profile("beta")
        active = manager.get_active_profile()
        expect(active is not None and active.id == "beta",
               f"beta should be active after activate, got {active and active.id}")
        expect(w._loaded_profile_id == "beta", "beta should still be open in the editor")

    step("double-click activates the profile", double_click_activates)

    def reactivate_current() -> None:
        manager.activate_profile("alpha")  # someone else (tray) switched away
        w._on_activate_profile("beta")     # double-click the row that is already open
        active = manager.get_active_profile()
        expect(active is not None and active.id == "beta", "re-activating the open profile should re-apply it")

    step("double-clicking the open profile re-applies it", reactivate_current)

    def dirty_cancel_blocks_switch() -> None:
        w._on_editor_dirty()
        answer_question(QMessageBox.StandardButton.Cancel)
        w._on_profile_selected("alpha")
        expect(w._loaded_profile_id == "beta", "cancel should keep the edited profile open")
        active = manager.get_active_profile()
        expect(active is not None and active.id == "beta", "cancel should not switch the active profile")

    step("unsaved changes: Cancel blocks the switch", dirty_cancel_blocks_switch)

    def dirty_discard_opens() -> None:
        answer_question(QMessageBox.StandardButton.Discard)
        w._on_profile_selected("alpha")
        expect(w._loaded_profile_id == "alpha", "discard should open the clicked profile")
        active = manager.get_active_profile()
        expect(active is not None and active.id == "beta",
               "discarding to open another profile must not change the active one")
        expect(w._dirty is False, "dirty flag should reset after discard")

    step("unsaved changes: Discard opens the clicked profile", dirty_discard_opens)

    def save_cancel_keeps_file() -> None:
        before = manager.get_profile("alpha").modified
        answer_question(QMessageBox.StandardButton.Cancel)
        w._on_save_current()
        expect(manager.get_profile("alpha").modified == before, "cancelled save must not write")

    step("save: Cancel leaves the profile untouched", save_cancel_keeps_file)

    def save_confirm_writes() -> None:
        ep.preamp_spin.setValue(-3.0)
        answer_question(QMessageBox.StandardButton.Save)
        w._on_save_current()
        expect(w._dirty is False, "save should clear the dirty flag")
        config = manager.load_profile_config("alpha")
        expect(config.preamp == -3.0, f"saved preamp should be -3.0, got {config.preamp}")

    step("save: Save confirms and writes", save_confirm_writes)

    def delete_still_confirms() -> None:
        answer_question(QMessageBox.StandardButton.No)
        w._on_delete_profile("beta")
        expect(manager.get_profile("beta") is not None, "declined delete must keep the profile")
        answer_question(QMessageBox.StandardButton.Yes)
        w._on_delete_profile("beta")
        expect(manager.get_profile("beta") is None, "confirmed delete should remove the profile")

    step("delete still confirms first", delete_still_confirms)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
