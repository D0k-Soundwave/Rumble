"""Self-test for always-on live tuning (real-time preview of unsaved edits).

Live tuning is the only editing mode — there is no toggle. Checks: editing a
band previews automatically after the debounce and points the active include
at the scratch preview file; saving restores the canonical pointer; bypass
blocks previewing and unbypass resumes it; quitting reverts an unsaved
preview; the preview file is never discovered as a profile.

Run:  python tools/selftest_live_tuning.py
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

from rumble.core.profile_manager import (
    ACTIVE_POINTER_FILENAME, PREVIEW_FILENAME, RUMBLE_SUBDIR,
    ProfileManager, make_flat_config,
)
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
    tmp = Path(tempfile.mkdtemp(prefix="rumble_livetest_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.activate_profile("alpha")

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)

    w = MainWindow(manager, theme_engine, QIcon())
    ep = w.editor_panel
    pointer = tmp / "config" / ACTIVE_POINTER_FILENAME
    preview_file = tmp / "config" / RUMBLE_SUBDIR / PREVIEW_FILENAME

    def pointer_target() -> str:
        # The Include path is emitted with POSIX '/' separators on every OS;
        # normalise defensively so the comparison can't break on Windows.
        return pointer.read_text(encoding="utf-8").splitlines()[-1].replace("\\", "/")

    def flush_preview() -> None:
        app.processEvents()
        if w._preview_timer.isActive():
            w._preview_timer.stop()
            w._push_live_preview()

    step("no Live toggle exists (always-on mode)",
         lambda: expect(not hasattr(ep, "btn_live"), "btn_live should be gone"))

    def edit_previews_automatically() -> None:
        ep.preamp_spin.setValue(-4.0)
        app.processEvents()
        expect(w._dirty, "editing the preamp should set the dirty flag")
        expect(w._preview_timer.isActive(), "an edit should arm the debounce timer")
        flush_preview()
        expect(manager.is_previewing, "an edit should engage the preview")
        expect(f"{RUMBLE_SUBDIR}/{PREVIEW_FILENAME}" in pointer_target(),
               f"pointer should include the preview file, got: {pointer_target()}")
        expect("Preamp: -4.0 dB" in preview_file.read_text(encoding="utf-8"),
               "preview file should hold the unsaved preamp value")

    step("edit previews automatically after the debounce", edit_previews_automatically)

    def further_edit_repreviews() -> None:
        ep.preamp_spin.setValue(-7.5)
        flush_preview()
        expect("Preamp: -7.5 dB" in preview_file.read_text(encoding="utf-8"),
               "preview file should follow the latest edit")

    step("further edits re-preview", further_edit_repreviews)

    def save_restores_pointer() -> None:
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Save)
        w._on_save_current()
        expect(not manager.is_previewing, "saving should end the preview")
        expect("Include: rumble/user/alpha.txt" in pointer_target(),
               f"pointer should be back on the saved profile, got: {pointer_target()}")
        expect(manager.load_profile_config("alpha").preamp == -7.5,
               "the previewed value should be what got saved")

    step("save restores the canonical pointer", save_restores_pointer)

    def bypass_blocks_preview() -> None:
        w._on_bypass_toggled(True)
        ep.preamp_spin.setValue(-9.0)
        app.processEvents()
        expect(not w._preview_timer.isActive(), "no preview should be scheduled while bypassed")
        expect("no active commands" in pointer.read_text(encoding="utf-8"),
               "bypass content should stay in place")
        w._on_bypass_toggled(False)
        flush_preview()
        expect(manager.is_previewing, "leaving bypass with dirty edits should resume the preview")

    step("bypass blocks previewing, unbypass resumes", bypass_blocks_preview)

    def opening_another_profile_clears_preview() -> None:
        # Single-click opens (views) a profile without activating it. Doing so
        # while a preview is live tears the preview down and reverts APO to the
        # still-active profile (alpha) — it does NOT switch the active profile.
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard)
        manager.create_profile("Beta", config=make_flat_config(5))
        w._load_profile_list()
        w._on_profile_selected("beta")
        expect(not manager.is_previewing, "opening another profile should clear the preview")
        expect("Include: rumble/user/alpha.txt" in pointer_target(),
               f"APO should revert to the active profile, got: {pointer_target()}")

    step("opening another profile clears the preview", opening_another_profile_clears_preview)

    def activating_switches_pointer() -> None:
        # Double-click (activate) is what actually changes the active profile.
        w._on_activate_profile("beta")
        expect("Include: rumble/user/beta.txt" in pointer_target(),
               f"activating should point APO at beta, got: {pointer_target()}")

    step("activating switches the active pointer", activating_switches_pointer)

    def preview_file_not_discovered() -> None:
        manager2 = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
        expect(manager2.get_profile("alpha") is not None, "profiles should survive reload")
        expect(all(PREVIEW_FILENAME not in p.file for p in manager2.profiles),
               "the preview scratch file must never be indexed as a profile")

    step("preview file is not discovered as a profile", preview_file_not_discovered)

    def quit_reverts_preview() -> None:
        ep.preamp_spin.setValue(-1.5)
        flush_preview()
        expect(manager.is_previewing, "precondition: a preview is engaged")
        w._quit_application()
        expect(not manager.is_previewing, "quit should end the preview")
        expect("Include: rumble/user/beta.txt" in pointer_target(),
               "quit should leave APO on the saved active profile")

    step("quit reverts an unsaved preview", quit_reverts_preview)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
