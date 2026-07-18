"""Full headless smoke test: builds the entire MainWindow against a temporary
profile library, then exercises tab switching, profile CRUD, presets, channel
linking, lock toggling, the advanced editors, every theme, and both dialogs.

Run:  python tools/selftest_smoke.py
Uses Qt's offscreen platform plugin — no window is shown. Exits non-zero if
any step raises."""
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

# Never block on dialogs
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)

from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, THEMES, get_theme

FAILURES: list[str] = []


def step(label: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception:
        FAILURES.append(label)
        print(f"  FAIL  {label}\n{traceback.format_exc()}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_smoke_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Smoke A", config=make_flat_config(10, channels=["L", "R"]))
    manager.create_profile("Smoke B", config=make_flat_config(5))

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)

    win: list[MainWindow] = []
    step("MainWindow builds", lambda: win.append(MainWindow(manager, theme_engine, QIcon())))
    if not win:
        return 1
    w = win[0]
    ep = w.editor_panel

    step("select profile", lambda: w._open_profile("smoke_a"))
    step("switch to Advanced tab", lambda: ep._switch_main_tab(1))
    step("switch to Raw Config tab", lambda: ep._switch_main_tab(2))
    step("back to EQ Editor tab", lambda: ep._switch_main_tab(0))

    step("add band", lambda: ep._on_add_band_clicked())
    step("apply Stereo preset", lambda: ep._on_preset_changed("Stereo / Headphones"))
    step("apply 5.1 preset", lambda: ep._on_preset_changed("5.1 Surround"))
    step("switch channel section", lambda: ep._switch_section(1))
    def _toggle_link() -> None:
        # Linking always asks for confirmation now — auto-answer Yes.
        from PySide6.QtWidgets import QMessageBox
        original_question = QMessageBox.question
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            ep._btn_link_channels.setChecked(False)
            ep._btn_link_channels.setChecked(True)
        finally:
            QMessageBox.question = original_question

    step("toggle link off/on", _toggle_link)
    step("auto preamp on/off", lambda: (ep.btn_auto_preamp.setChecked(True),
                                        ep.btn_auto_preamp.setChecked(False)))
    step("save profile", lambda: w._on_save_current())
    step("activate profile", lambda: w._on_activate_profile("smoke_a"))
    step("bypass on/off", lambda: (w._on_bypass_toggled(True), w._on_bypass_toggled(False)))
    step("lock profile", lambda: w._on_toggle_lock_profile("smoke_b"))
    step("unlock profile", lambda: w._on_toggle_lock_profile("smoke_b"))
    step("duplicate profile", lambda: manager.duplicate_profile("smoke_b", "Smoke C"))
    step("delete profile", lambda: manager.delete_profile(
        next(p.id for p in manager.profiles if p.name == "Smoke C")))
    step("refresh list", lambda: w._load_profile_list())

    ap = ep.advanced_panel
    step("GEQ edit", lambda: ap.graphic_eq.set_points_from_string("100 3; 1000 -2"))
    step("advanced tabs cycle", lambda: [ap.tabs.setCurrentIndex(i)
                                         for i in range(ap.tabs.count())])

    for name in THEMES:
        step(f"theme: {name}", lambda n=name: theme_engine.switch(app, get_theme(n)))
    step("restore default theme",
         lambda: theme_engine.switch(app, get_theme(DEFAULT_THEME_NAME)))

    def new_profile_dialog() -> None:
        from rumble.ui.new_profile_dialog import NewProfileDialog
        NewProfileDialog(get_theme(DEFAULT_THEME_NAME), default_bands=10, parent=w).deleteLater()
    step("NewProfileDialog builds", new_profile_dialog)

    def settings_dialog() -> None:
        from rumble.ui.settings_dialog import SettingsDialog
        SettingsDialog(manager.settings, get_theme(DEFAULT_THEME_NAME),
                       tmp / "data", parent=w).deleteLater()
    step("SettingsDialog builds", settings_dialog)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
