"""Selftest: S5 (B1-ui) tray/header tone-trim controls (offscreen Qt, real
widgets -- no mocks of ToneTrimWidget/ToastMenu/TrayIcon/MainWindow).

Covers: a slider change updates ProfileManager.session_trim and refreshes the
active pointer; the status-bar trim chip's visibility tracks
any(session_trim.values()); Reset zeroes the trim; "Bake into profile..."
writes the trim curve into the active profile's editor state as real
GraphicEQ content and resets the trim; baking refuses while the editor is
dirty.

Run:  python tests/selftest_tone_trim_ui.py
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

from rumble.core import tone_trim
from rumble.core.profile_manager import (
    LISTENER_ACTIVE_FILENAME, ProfileManager, make_flat_config,
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


def make_window():
    tmp = Path(tempfile.mkdtemp(prefix="rumble_tonetrimui_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.activate_profile("alpha")
    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    w = MainWindow(manager, theme_engine, QIcon())
    app.processEvents()
    return manager, w


def listener_active_text(manager: ProfileManager) -> str:
    path = manager.rumble_dir / LISTENER_ACTIVE_FILENAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── slider change -> manager.session_trim + pointer refresh ─────────────

def header_slider_change_updates_session_trim_and_pointer() -> None:
    manager, w = make_window()
    trim_widget = w._tone_trim_widget
    trim_widget._slider_bass.setValue(30)  # +3.0 dB (STEPS_PER_DB == 10)
    app.processEvents()
    expect(trim_widget._timer.isActive(), "a slider drag should arm the debounce timer")
    trim_widget._timer.stop()
    trim_widget._emit_trim_changed()
    app.processEvents()
    expect(manager.session_trim == {"bass_db": 3.0, "treble_db": 0.0, "tilt_db_per_oct": 0.0},
           f"session_trim should reflect the slider, got {manager.session_trim!r}")
    text = listener_active_text(manager)
    expect(text != "", "the active pointer should now route through the listener-active scratch file")
    curve = dict(tone_trim.derive_trim_curve(bass_db=3.0, treble_db=0.0, tilt_db_per_oct=0.0))
    expect(abs(curve[20.0] - 3.0) < 1e-9, "precondition: a +3 dB bass trim should show at 20 Hz")


def tray_toast_slider_relays_to_same_manager_state() -> None:
    manager, w = make_window()
    toast_widget = w.tray_icon._toast._tone_trim
    toast_widget._slider_treble.setValue(-20)  # -2.0 dB
    toast_widget._timer.stop()
    toast_widget._emit_trim_changed()
    app.processEvents()
    expect(manager.session_trim == {"bass_db": 0.0, "treble_db": -2.0, "tilt_db_per_oct": 0.0},
           f"tray toast slider should reach the same manager, got {manager.session_trim!r}")
    # The header popover's own widget instance should be kept in sync too.
    expect(w._tone_trim_widget._treble_db == -2.0,
           "the header popover widget should reflect state changed from the tray toast")


# ── status-bar chip visibility ───────────────────────────────────────────

def chip_visible_only_while_trim_nonzero() -> None:
    manager, w = make_window()
    expect(w.status_tone_trim.isHidden(), "chip should start hidden (trim starts flat)")
    w._on_tone_trim_changed(1.5, 0.0, 0.0)
    expect(not w.status_tone_trim.isHidden(), "chip should show once any trim value is non-zero")
    w._on_tone_trim_changed(0.0, 0.0, 0.0)
    expect(w.status_tone_trim.isHidden(), "chip should hide again once trim returns to flat")


# ── Reset ─────────────────────────────────────────────────────────────

def reset_button_zeroes_trim_immediately() -> None:
    manager, w = make_window()
    w._on_tone_trim_changed(2.0, -1.0, 0.5)
    expect(any(manager.session_trim.values()), "precondition: trim must be non-flat")
    w._tone_trim_widget.btn_reset.click()
    app.processEvents()
    expect(manager.session_trim == {"bass_db": 0.0, "treble_db": 0.0, "tilt_db_per_oct": 0.0},
           f"Reset should zero every trim value immediately, got {manager.session_trim!r}")
    expect(w.status_tone_trim.isHidden(), "chip should hide after Reset")


# ── Bake into profile ────────────────────────────────────────────────

def bake_writes_curve_into_editor_and_resets_trim() -> None:
    manager, w = make_window()
    w._on_tone_trim_changed(2.0, 0.0, 0.0)
    expect(not w._dirty, "precondition: editor must be clean before baking")
    w._on_bake_tone_trim()
    app.processEvents()
    expect(w._dirty, "baking should mark the editor dirty (user still saves explicitly)")
    expect(w._loaded_profile_id == "alpha", "baking should load the active profile into the editor")
    config = w.editor_panel.build_config()
    expect(all(section.graphic_eq != "" for section in config.channel_sections),
           "every section should carry the baked GraphicEQ curve")
    expect(manager.session_trim == {"bass_db": 0.0, "treble_db": 0.0, "tilt_db_per_oct": 0.0},
           f"baking should reset the trim to flat, got {manager.session_trim!r}")
    expect(w.status_tone_trim.isHidden(), "chip should hide after a bake resets the trim")


def bake_refuses_on_dirty_editor() -> None:
    manager, w = make_window()
    w._on_tone_trim_changed(2.0, 0.0, 0.0)
    w._dirty = True  # simulate an unrelated unsaved edit already in progress
    seen = []
    QMessageBox.information = staticmethod(
        lambda *a, **k: seen.append(a[2] if len(a) > 2 else k.get("text", "")))
    try:
        w._on_bake_tone_trim()
    finally:
        QMessageBox.information = staticmethod(lambda *a, **k: None)
    app.processEvents()
    expect(len(seen) == 1, f"a refusal dialog should be shown exactly once, got {seen!r}")
    expect("unsaved" in seen[0].lower(), f"the refusal should mention unsaved changes, got {seen[0]!r}")
    expect(manager.session_trim == {"bass_db": 2.0, "treble_db": 0.0, "tilt_db_per_oct": 0.0},
           "a refused bake must not touch session_trim")
    expect(w._loaded_profile_id == "alpha", "a refused bake must not reload the editor")


def main() -> int:
    step("a header-popover slider change updates session_trim and refreshes the pointer",
         header_slider_change_updates_session_trim_and_pointer)
    step("the tray toast's slider reaches the same manager state as the header popover",
         tray_toast_slider_relays_to_same_manager_state)
    step("the status-bar trim chip is visible only while any trim is non-zero",
         chip_visible_only_while_trim_nonzero)
    step("Reset zeroes the trim immediately", reset_button_zeroes_trim_immediately)
    step("Bake writes the trim curve into the editor and resets the trim",
         bake_writes_curve_into_editor_and_resets_trim)
    step("Bake refuses while the editor is dirty", bake_refuses_on_dirty_editor)
    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
