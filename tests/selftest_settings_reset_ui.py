"""Self-test for the Reset Settings UI (Settings -> Backup & Restore).

Covers the picker dialog's guard rails and the main window's apply path: that a
reset only touches the chosen groups, is persisted, is applied to the LIVE app
(not just the settings dict), leaves protected state alone, and reopens Settings
with a fresh working dict rather than the pre-reset snapshot -- which would
otherwise be written straight back over the reset by the next control touched.

Run:  python tests/selftest_settings_reset_ui.py
Uses Qt's offscreen platform plugin -- no window is shown."""
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
from rumble.core.settings_reset import RESET_GROUPS
from rumble.ui.main_window import MainWindow
from rumble.ui.settings_dialog import CATEGORIES, ResetSettingsDialog
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
    tmp = Path(tempfile.mkdtemp(prefix="rumble_resetui_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.activate_profile("alpha")

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    w = MainWindow(manager, theme_engine, QIcon())

    # Record reopens instead of opening a real modal: exec() would block
    # forever with nobody to click it.
    reopened: list = []
    w._open_settings = lambda restore=None, initial_category=None: reopened.append(restore)

    # ── the picker dialog ────────────────────────────────────────────
    def picker_guards() -> None:
        d = ResetSettingsDialog(w.theme, w)
        expect(not d.btn_reset.isEnabled(),
               "Reset must be disabled until something is ticked -- this dialog "
               "is the confirmation step")
        expect(d.selected_groups() == [], "nothing may be pre-ticked")
        d._checks["appearance"].setChecked(True)
        expect(d.btn_reset.isEnabled(), "ticking a group must enable Reset")
        expect(d.selected_groups() == ["appearance"], "selection should report the ticked group")
        d._on_select_all()
        expect(len(d.selected_groups()) == len(RESET_GROUPS),
               "Select All must tick every group")
        for check in d._checks.values():
            check.setChecked(False)
        expect(not d.btn_reset.isEnabled(), "un-ticking everything must disable Reset again")
        d.reject()

    step("picker starts empty, Reset gates on a selection", picker_guards)

    def picker_covers_every_group() -> None:
        d = ResetSettingsDialog(w.theme, w)
        expect(set(d._checks) == {g.key for g in RESET_GROUPS},
               "every reset group needs a checkbox -- a group with no way to "
               "tick it is unreachable")
        d.reject()

    step("picker offers every reset group", picker_covers_every_group)

    def labels_escape_mnemonics() -> None:
        # Qt reads a lone '&' in a label as a mnemonic marker and swallows it:
        # "Startup & warnings" renders as "Startup _warnings".
        d = ResetSettingsDialog(w.theme, w)
        for group in RESET_GROUPS:
            text = d._checks[group.key].text()
            expect("&&" in text or "&" not in text,
                   f"group '{group.key}' label {text!r} has an unescaped '&' -- "
                   "Qt will eat it and underline the next letter")
            expect(text == group.label.replace("&", "&&"),
                   f"group '{group.key}' checkbox text {text!r} does not match "
                   f"its label {group.label!r}")
        d.reject()

    step("group labels escape Qt mnemonics", labels_escape_mnemonics)

    # ── the apply path ───────────────────────────────────────────────
    def do_reset(groups: list[str]) -> None:
        """Reset, then let the deferred reopen run. The reopen is deliberately
        posted to the event loop (opening the next modal inline would nest one
        exec() inside the one unwinding), so every call must be pumped or the
        pending reopens pile up and land on a later check."""
        w._reset_settings(groups)
        app.processEvents()

    def seed() -> None:
        manager.settings.update({
            "theme": "Panther",
            "default_bands": 31,
            "auto_switch_enabled": True,
            "auto_switch_rules": [{"profile_id": "alpha"}],
            "crossfeed_enabled": True,
            # Protected -- must survive any reset.
            "apo_managed": False,
            "last_active_profile": "alpha",
            "legacy_hearing_migrated": True,
        })
        manager.save()

    def resets_only_chosen_groups() -> None:
        seed()
        reopened.clear()
        do_reset(["appearance", "automation"])
        s = manager.settings
        expect(s["theme"] == DEFAULT_THEME_NAME, "theme should be back to default")
        expect(s["auto_switch_enabled"] is False, "auto-switch should be off")
        expect(s["auto_switch_rules"] == [], "auto-switch rules should be cleared")
        expect(s["default_bands"] == 31,
               "eq_defaults was not selected and must be untouched")
        expect(s["crossfeed_enabled"] is True,
               "sound_layers was not selected and must be untouched")

    step("resets only the chosen groups", resets_only_chosen_groups)

    def protected_state_survives() -> None:
        seed()
        do_reset([g.key for g in RESET_GROUPS])
        s = manager.settings
        expect(s["apo_managed"] is False,
               "a reset must never re-take APO ownership from the user")
        expect(s["last_active_profile"] == "alpha",
               "a reset must never deactivate the running profile")
        expect(s["legacy_hearing_migrated"] is True,
               "a reset must never re-arm the one-shot hearing migration")

    step("protected state survives a reset of every group", protected_state_survives)

    def applies_live_not_just_to_the_dict() -> None:
        seed()
        w._auto_switch_adapter.start()
        expect(w._auto_switch_adapter._timer.isActive(),
               "precondition: the auto-switch watcher is running")
        do_reset(["appearance", "automation"])
        expect(w.theme is get_theme(DEFAULT_THEME_NAME),
               "the live app should be on the default theme, not just the settings dict")
        expect(not w._auto_switch_adapter._timer.isActive(),
               "resetting auto_switch_enabled must actually stop the watcher, "
               "not just flip the flag")

    step("reset is applied to the live app", applies_live_not_just_to_the_dict)

    def reset_is_persisted() -> None:
        seed()
        do_reset(["appearance", "automation"])
        reloaded = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
        expect(reloaded.settings["theme"] == DEFAULT_THEME_NAME,
               "the reset theme should survive a restart")
        expect(reloaded.settings["auto_switch_rules"] == [],
               "the cleared rules should survive a restart")
        expect(reloaded.settings["apo_managed"] is False,
               "protected APO ownership should survive a restart")

    step("reset is persisted to disk", reset_is_persisted)

    def manager_owned_keys_are_reset() -> None:
        # _apply_settings_live drops ignored_apo_sources (the dialog's snapshot
        # of it is stale by design), so a reset has to write it explicitly --
        # otherwise the APO group would silently no-op on that key.
        manager.settings["ignored_apo_sources"] = ["some/file.txt"]
        manager.settings["config_path_override"] = r"C:\bogus"
        manager.save()
        do_reset(["apo"])
        expect(manager.settings["ignored_apo_sources"] == [],
               "resetting APO integration must un-dismiss ignored sources")
        expect(manager.settings["config_path_override"] is None,
               "resetting APO integration must clear the config path override")
        reloaded = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
        expect(reloaded.settings["ignored_apo_sources"] == [],
               "the un-dismissed sources must be persisted, not just in memory")

    step("manager-owned APO keys are reset and persisted", manager_owned_keys_are_reset)

    def reopens_settings_fresh() -> None:
        seed()
        reopened.clear()
        do_reset(["appearance"])
        expect(len(reopened) == 1, f"Settings should reopen once, got {len(reopened)}")
        state = reopened[0]
        expect("working" not in state,
               "the reopened dialog must NOT carry the pre-reset working dict -- "
               "the next control touched would write it back over the reset")
        expect(state.get("category") == CATEGORIES.index("Backup & Restore"),
               "Settings should reopen on the page the reset was started from")

    step("reopens Settings without the stale working dict", reopens_settings_fresh)

    def noop_reset_still_reopens() -> None:
        reopened.clear()
        do_reset(["appearance"])  # already default from the check above
        do_reset(["appearance"])
        expect(len(reopened) == 2,
               "a no-op reset must still reopen Settings, not strand the user "
               "with no window")

    step("a no-op reset still reopens Settings", noop_reset_still_reopens)

    def unknown_group_is_safe() -> None:
        seed()
        do_reset(["nope"])
        expect(manager.settings["theme"] == "Panther",
               "an unknown group id must not reset anything")

    step("an unknown group id resets nothing", unknown_group_is_safe)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
