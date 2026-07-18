"""Selftest: crossfeed UI -- the editor's per-profile crossfeed control, the
main-window Off / Per profile / Global source switch, the Settings dialog's
global-value editor, and the tray/toast quick toggle (offscreen Qt, real
widgets -- no mocks of SettingsDialog/ProfileManager/MainWindow).

Covers: the editor's per-profile crossfeed control reflects the loaded
profile, marks dirty on edit and rebuilds its spec, and soft-disables on a
non-stereo layout; the main-UI source switch drives crossfeed_mode so each of
its three states (off / profile / global) produces the expected live re-render
of the active pointer, and stays in sync with the tray toggle; the Settings
card is now purely the GLOBAL crossfeed value (preset/strength/custom, no
enable checkbox and no "Insert into profile" button) and its value reaches a
live re-render when the mode selects it; the tray quick toggle flips
crossfeed_mode between off and the last non-off mode, persists immediately, and
keeps the editor switch in sync. Settings-file persistence of the plain keys is
covered by selftest_settings_persist.py; the three render modes at the manager
level are covered by selftest_crossfeed.py.

Run:  python tests/selftest_crossfeed_ui.py
Uses Qt's offscreen platform plugin -- no window is shown.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from rumble.core import crossfeed
from rumble.core.apo_config import ChannelSection
from rumble.core.profile_manager import (
    ACTIVE_POINTER_FILENAME, LISTENER_ACTIVE_FILENAME, DEFAULT_SETTINGS,
    ProfileManager, make_flat_config,
)
from rumble.ui.main_window import MainWindow
from rumble.ui.editor_panel import EditorPanel
from rumble.core import apo_config
from rumble.ui.settings_dialog import SettingsDialog
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


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def tmp_data_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="rumble_crossfeedui_"))


def active_pointer_text(manager: ProfileManager) -> str:
    # The canonical pointer either includes the stored profile file directly
    # (no render layer active -- the listener scratch file is left
    # untouched/stale in that case) or routes through the listener-corrected
    # scratch file that actually carries the composed body. Follow the pointer
    # rather than assuming the scratch file is current, so a transition back to
    # "nothing active" isn't misread as still carrying stale crossfeed content.
    pointer_path = manager.config_path / ACTIVE_POINTER_FILENAME
    pointer_text = pointer_path.read_text(encoding="utf-8") if pointer_path.exists() else ""
    if LISTENER_ACTIVE_FILENAME not in pointer_text:
        return ""
    path = manager.rumble_dir / LISTENER_ACTIVE_FILENAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


def make_active_stereo_manager() -> ProfileManager:
    tmp = tmp_data_dir()
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.activate_profile("alpha")
    return manager


def make_window():
    manager = make_active_stereo_manager()
    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    w = MainWindow(manager, theme_engine, QIcon())
    app.processEvents()
    return manager, w


def _set_mode(w, mode: str) -> None:
    combo = w.editor_panel.combo_crossfeed_mode
    combo.setCurrentIndex(combo.findData(mode))
    app.processEvents()


# ── editor per-profile control ───────────────────────────────────────────

def editor_crossfeed_control_loads_edits_and_exposes_spec() -> None:
    """The per-profile crossfeed control in the editor: reflects the loaded
    profile's crossfeed, marks dirty and rebuilds the spec on edit, and
    soft-disables on a non-stereo layout."""
    stereo = apo_config.parse_config(
        "Preamp: -3 dB\nChannel: L R\nFilter 1: ON PK Fc 1000 Hz Gain -2 dB Q 1\n")
    ed = EditorPanel(theme=THEMES[DEFAULT_THEME_NAME])

    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(data_dir=Path(tmp) / "d", config_path=Path(tmp) / "c")
        mgr.ensure_first_run()
        meta = mgr.create_profile("P", config=stereo)
    ed.load_profile(meta, stereo, is_active=True, is_bypassed=False)
    expect(ed.chk_crossfeed.isChecked() is False, "no crossfeed -> checkbox off")
    expect(ed.crossfeed_spec() is None, "no crossfeed -> spec None")
    expect(ed.chk_crossfeed.isEnabled(), "stereo layout enables the control")

    dirty = []
    ed.dirty.connect(lambda: dirty.append(1))
    ed.chk_crossfeed.setChecked(True)
    ed.combo_crossfeed.setCurrentIndex(ed.combo_crossfeed.findData("meier"))
    ed.slider_crossfeed.setValue(80)
    expect(len(dirty) > 0, "editing crossfeed marks the profile dirty")
    spec = ed.crossfeed_spec()
    expect(spec is not None and spec.preset == "meier" and spec.strength == 80,
           f"spec reflects the edit, got {spec!r}")

    meta.crossfeed = crossfeed.CrossfeedSpec(preset="bauer", strength=60)
    ed.load_profile(meta, stereo, is_active=True, is_bypassed=False)
    expect(ed.chk_crossfeed.isChecked() and ed.combo_crossfeed.currentData() == "bauer"
           and ed.slider_crossfeed.value() == 60, "loading a crossfeed profile reflects it")

    mono = apo_config.parse_config("Channel: C\nFilter 1: ON PK Fc 100 Hz Gain 1 dB Q 1\n")
    ed.load_profile(meta, mono, is_active=True, is_bypassed=False)
    expect(ed.chk_crossfeed.isEnabled() is False, "non-stereo layout soft-disables the control")


def editor_mode_switch_does_not_mark_dirty_and_syncs_without_emitting() -> None:
    """The machine-level source switch is not a profile edit: setting it via
    set_crossfeed_mode() must not emit, and a user pick must not mark dirty."""
    ed = EditorPanel(theme=THEMES[DEFAULT_THEME_NAME])
    keys = [ed.combo_crossfeed_mode.itemData(i)
            for i in range(ed.combo_crossfeed_mode.count())]
    expect(keys == ["off", "profile", "global"], f"unexpected mode items: {keys!r}")

    emitted: list[str] = []
    ed.crossfeed_mode_changed.connect(emitted.append)
    ed.set_crossfeed_mode("global")
    expect(emitted == [], "set_crossfeed_mode must not emit")
    expect(ed.combo_crossfeed_mode.currentData() == "global", "setter reflects the mode")

    dirty = []
    ed.dirty.connect(lambda: dirty.append(1))
    ed.combo_crossfeed_mode.setCurrentIndex(ed.combo_crossfeed_mode.findData("off"))
    expect(emitted == ["off"], f"a user pick emits the mode, got {emitted!r}")
    expect(dirty == [], "the machine-level switch must not mark the profile dirty")


# ── main-UI source switch drives the live render ─────────────────────────

def source_switch_three_states_drive_the_live_render() -> None:
    manager, w = make_window()
    # Global value present so the un-set active profile has something to fall
    # back to in "profile" mode and something to render in "global" mode.
    manager.settings["crossfeed_preset"] = "linkwitz"
    manager.settings["crossfeed_strength"] = 100

    _set_mode(w, "off")
    expect("Channel: XL XR" not in active_pointer_text(manager),
           "Off renders no crossfeed block")

    _set_mode(w, "profile")
    text = active_pointer_text(manager)
    expect("Channel: XL XR" in text,
           "Per profile falls back to the global value for the un-set profile")

    _set_mode(w, "global")
    expect("Channel: XL XR" in active_pointer_text(manager),
           "Global renders the global value for every profile")


def source_switch_persists_and_syncs_the_tray() -> None:
    manager, w = make_window()
    _set_mode(w, "off")
    expect(manager.settings["crossfeed_mode"] == "off", "the switch persists crossfeed_mode")
    raw = manager.metadata_path.read_text(encoding="utf-8")
    expect('"crossfeed_mode": "off"' in raw, "the mode is saved to disk immediately")
    expect(not w.tray_icon._toast.action_crossfeed.isChecked(),
           "Off unchecks the tray toggle")

    _set_mode(w, "global")
    expect(w.tray_icon._toast.action_crossfeed.isChecked(),
           "a non-off mode checks the tray toggle")


# ── tray quick toggle ────────────────────────────────────────────────────

def tray_toggle_flips_mode_and_syncs_the_switch() -> None:
    manager, w = make_window()
    toast = w.tray_icon._toast
    # Default mode is "profile", so the tray starts checked.
    expect(toast.action_crossfeed.isChecked(), "tray starts checked (profile mode)")

    toast.action_crossfeed.setChecked(False)
    app.processEvents()
    expect(manager.settings["crossfeed_mode"] == "off", "tray off parks the mode at off")
    expect(w.editor_panel.combo_crossfeed_mode.currentData() == "off",
           "the editor switch follows the tray to off")

    toast.action_crossfeed.setChecked(True)
    app.processEvents()
    expect(manager.settings["crossfeed_mode"] == "profile",
           "tray on restores the last non-off mode (profile)")
    expect(w.editor_panel.combo_crossfeed_mode.currentData() == "profile",
           "the editor switch follows the tray back on")


def tray_toggle_restores_global_when_that_was_the_last_mode() -> None:
    manager, w = make_window()
    _set_mode(w, "global")
    toast = w.tray_icon._toast
    toast.action_crossfeed.setChecked(False)
    app.processEvents()
    expect(manager.settings["crossfeed_mode"] == "off", "tray off from global parks at off")
    toast.action_crossfeed.setChecked(True)
    app.processEvents()
    expect(manager.settings["crossfeed_mode"] == "global",
           "tray on restores global, the last non-off mode the user chose")


# ── Settings dialog: the GLOBAL crossfeed VALUE editor ───────────────────

def settings_card_has_no_enable_or_insert_controls() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir())
    expect(not hasattr(dlg, "chk_crossfeed_enabled"),
           "the enable checkbox is gone (the main-UI switch owns on/off)")
    expect(not hasattr(dlg, "btn_insert_crossfeed"),
           "the Insert into profile button is gone")


def settings_card_prefills_and_round_trips_the_value() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir())
    expect(dlg.combo_crossfeed_preset.currentData() == crossfeed.DEFAULT_PRESET,
           "preset combo defaults to crossfeed.DEFAULT_PRESET")
    expect(dlg.slider_crossfeed_strength.value() == 0,
           "strength slider defaults to 0 (the global fallback starts inert)")

    idx = dlg.combo_crossfeed_preset.findData("chumoy")
    dlg.combo_crossfeed_preset.setCurrentIndex(idx)
    dlg.slider_crossfeed_strength.setValue(65)
    expect(dlg.lbl_crossfeed_strength.text() == "65%", "value label follows the slider")

    emitted = []
    dlg.settings_saved.connect(emitted.append)
    dlg._apply_now()
    expect("crossfeed_enabled" not in emitted[0], "the card no longer emits crossfeed_enabled")
    expect(emitted[0]["crossfeed_preset"] == "chumoy", "preset saved")
    expect(emitted[0]["crossfeed_strength"] == 65, "strength saved")


def settings_card_combo_offers_all_six_modes() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir())
    keys = [dlg.combo_crossfeed_preset.itemData(i)
            for i in range(dlg.combo_crossfeed_preset.count())]
    expect(keys == ["chumoy", "meier", "linkwitz", "bauer", "direct", "custom"],
           f"unexpected preset combo contents: {keys!r}")


def settings_card_mode_switch_shows_only_the_matching_control_group() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir())

    def select(key: str) -> None:
        idx = dlg.combo_crossfeed_preset.findData(key)
        dlg.combo_crossfeed_preset.setCurrentIndex(idx)

    select("chumoy")
    expect(not dlg.slider_crossfeed_strength.isHidden(), "Strength shown for a named preset")
    expect(dlg.spin_crossfeed_custom_gain.isHidden(), "Custom controls hidden for a named preset")

    select("custom")
    expect(dlg.slider_crossfeed_strength.isHidden(), "Strength hidden for Custom")
    expect(not dlg.spin_crossfeed_custom_gain.isHidden(), "Custom controls shown for Custom")
    expect(not dlg.spin_crossfeed_custom_freq.isHidden(), "Custom cutoff shown for Custom")
    expect(not dlg.spin_crossfeed_custom_delay.isHidden(), "Custom delay shown for Custom")


def settings_card_round_trips_custom_settings() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir())
    dlg.spin_crossfeed_custom_gain.setValue(-11.5)
    dlg.spin_crossfeed_custom_freq.setValue(900.0)
    dlg.spin_crossfeed_custom_delay.setValue(150.0)

    emitted = []
    dlg.settings_saved.connect(emitted.append)
    dlg._apply_now()
    expect(emitted[0]["crossfeed_custom_gain_db"] == -11.5, "custom gain saved")
    expect(emitted[0]["crossfeed_custom_freq_hz"] == 900.0, "custom cutoff saved")
    expect(emitted[0]["crossfeed_custom_delay_us"] == 150.0, "custom delay saved")


def settings_card_stereo_gate() -> None:
    stereo = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir(),
                            active_profile_is_stereo=True)
    expect(stereo.combo_crossfeed_preset.isEnabled(), "preset combo enabled on a stereo profile")
    expect(stereo.slider_crossfeed_strength.isEnabled(), "strength slider enabled on a stereo profile")
    expect(stereo.lbl_crossfeed_nonstereo.isHidden(), "no non-stereo note on a stereo profile")

    nons = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir(),
                          active_profile_is_stereo=False)
    expect(not nons.combo_crossfeed_preset.isEnabled(), "preset combo disabled on a non-stereo profile")
    expect(not nons.slider_crossfeed_strength.isEnabled(),
           "strength slider disabled on a non-stereo profile")
    expect(not nons.lbl_crossfeed_nonstereo.isHidden(), "non-stereo note shown")


def settings_value_drives_the_live_render_when_the_mode_selects_it() -> None:
    manager = make_active_stereo_manager()
    manager.settings["crossfeed_mode"] = "global"
    dlg = SettingsDialog(dict(manager.settings), THEMES[DEFAULT_THEME_NAME], manager.data_dir,
                         active_profile_is_stereo=True)
    idx = dlg.combo_crossfeed_preset.findData("meier")
    dlg.combo_crossfeed_preset.setCurrentIndex(idx)
    dlg.slider_crossfeed_strength.setValue(100)

    emitted = []
    dlg.settings_saved.connect(emitted.append)
    dlg._apply_now()
    manager.settings.update(emitted[0])
    expect(manager.refresh_render_layers() is True, "refresh succeeds with an active stereo profile")
    text = active_pointer_text(manager)
    expect("Channel: XL XR" in text, "the global value renders live in Global mode")
    expect("Copy: XL=R" in text, "crossfeed copy lines present in the live render")


def settings_value_stays_dark_when_the_mode_is_off() -> None:
    manager = make_active_stereo_manager()
    manager.settings["crossfeed_mode"] = "off"
    manager.settings["crossfeed_preset"] = "meier"
    manager.settings["crossfeed_strength"] = 100
    manager.refresh_render_layers()
    expect("Channel: XL XR" not in active_pointer_text(manager),
           "a set global value renders nothing while the mode is Off")


# ── helper sanity ────────────────────────────────────────────────────────

def can_crossfeed_matches_the_gate_main_window_computes() -> None:
    has_pair = [ChannelSection(channel="L"), ChannelSection(channel="C"),
                ChannelSection(channel="R")]
    expect(crossfeed.can_crossfeed(has_pair) is True,
           "a layout containing both L and R can take crossfeed")
    no_pair = [ChannelSection(channel="L"), ChannelSection(channel="C")]
    expect(crossfeed.can_crossfeed(no_pair) is False,
           "a layout with no R has nothing to crossfeed")


def main() -> int:
    step("the editor's per-profile crossfeed control loads, edits and exposes a spec",
         editor_crossfeed_control_loads_edits_and_exposes_spec)
    step("the editor source switch syncs silently and never marks the profile dirty",
         editor_mode_switch_does_not_mark_dirty_and_syncs_without_emitting)
    step("the source switch's three states each drive the expected live render",
         source_switch_three_states_drive_the_live_render)
    step("the source switch persists crossfeed_mode and syncs the tray",
         source_switch_persists_and_syncs_the_tray)
    step("the tray quick toggle flips crossfeed_mode and syncs the editor switch",
         tray_toggle_flips_mode_and_syncs_the_switch)
    step("the tray quick toggle restores global when that was the last mode",
         tray_toggle_restores_global_when_that_was_the_last_mode)
    step("the Settings card has no enable checkbox or insert button",
         settings_card_has_no_enable_or_insert_controls)
    step("the Settings card prefills and round-trips the global value",
         settings_card_prefills_and_round_trips_the_value)
    step("the Settings preset combo offers all six modes",
         settings_card_combo_offers_all_six_modes)
    step("switching preset mode shows only the matching control group",
         settings_card_mode_switch_shows_only_the_matching_control_group)
    step("the Settings card round-trips Custom settings",
         settings_card_round_trips_custom_settings)
    step("the Settings card soft-disables on a non-stereo active profile",
         settings_card_stereo_gate)
    step("the global value reaches a live re-render when the mode selects it",
         settings_value_drives_the_live_render_when_the_mode_selects_it)
    step("a set global value renders nothing while the mode is Off",
         settings_value_stays_dark_when_the_mode_is_off)
    step("crossfeed.can_crossfeed() accepts any layout carrying L and R",
         can_crossfeed_matches_the_gate_main_window_computes)
    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
