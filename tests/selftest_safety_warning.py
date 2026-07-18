"""Self-test for the first-use hearing/gear safety warning.

Checks: the warning appears the first time the main window is shown; it
appears only once per session; closing it without ticking the checkbox keeps
it enabled for the next launch; ticking "Don't show this warning again"
persists the opt-out; an opted-out install never shows it.

Run:  python tools/selftest_safety_warning.py
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


# Simulated user interaction with the warning box: count how often it
# appears and optionally tick the "don't show again" checkbox.
calls = {"shown": 0, "tick": False}
_original_exec = QMessageBox.exec


def fake_exec(self):
    calls["shown"] += 1
    if calls["tick"] and self.checkBox() is not None:
        self.checkBox().setChecked(True)
    return QMessageBox.StandardButton.Ok


QMessageBox.exec = fake_exec


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_safetytest_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(5))
    manager.activate_profile("alpha")

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)

    w = MainWindow(manager, theme_engine, QIcon())

    def shows_on_first_display() -> None:
        expect(manager.settings.get("show_safety_warning") is True,
               "warning should be enabled by default")
        w.show()
        app.processEvents()
        expect(calls["shown"] == 1, f"warning should appear once, appeared {calls['shown']}x")
        expect(manager.settings.get("show_safety_warning") is True,
               "closing without ticking must keep the warning enabled")

    step("warning appears on first display, stays enabled if not ticked",
         shows_on_first_display)

    def once_per_session() -> None:
        w.hide()
        w.show()
        app.processEvents()
        expect(calls["shown"] == 1, "warning must not repeat within one session")

    step("warning appears only once per session", once_per_session)

    def next_session_shows_again_and_tick_persists() -> None:
        calls["tick"] = True
        w2 = MainWindow(manager, theme_engine, QIcon())
        w2.show()
        app.processEvents()
        expect(calls["shown"] == 2, "a new session should show the warning again")
        expect(manager.settings.get("show_safety_warning") is False,
               "ticking the checkbox should disable the warning")
        w2.hide()
        w2.tray_icon.hide()

    step("next session shows again; ticking the box opts out",
         next_session_shows_again_and_tick_persists)

    def optout_persists_across_restart() -> None:
        manager2 = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
        expect(manager2.settings.get("show_safety_warning") is False,
               "the opt-out should persist across restarts")
        w3 = MainWindow(manager2, theme_engine, QIcon())
        w3.show()
        app.processEvents()
        expect(calls["shown"] == 2, "an opted-out install must never show the warning")
        w3.hide()
        w3.tray_icon.hide()

    step("opt-out persists across restarts", optout_persists_across_restart)

    w.tray_icon.hide()
    QMessageBox.exec = _original_exec
    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
