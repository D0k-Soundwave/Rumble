"""Selftest: S8 (B2-ui) crossfeed controls -- Settings -> Listeners section,
the tray/toast quick toggle, and "Insert into profile..." (offscreen Qt,
real widgets -- no mocks of SettingsDialog/ProfileManager/MainWindow).

Covers: the Settings section prefills from and round-trips into the working
settings dict on apply, mirroring the ISO 226 section's own pattern; a
non-stereo active profile soft-disables the controls (values kept, not
reset) while a stereo one leaves them enabled; toggling enable/preset/
strength through the dialog and applying the saved settings to a real
ProfileManager triggers a live re-render whose active-pointer output
actually carries (or omits) the crossfeed block; the tray/toast quick
toggle persists the setting immediately and re-renders live, staying in
sync with the Settings dialog; "Insert into profile..." writes the
rendered block into the active profile's editor state as real, portable
text and turns the machine-level setting off (so it isn't applied twice),
refusing on a dirty editor. Settings-file persistence of the three plain
settings keys themselves is already covered end-to-end by
selftest_settings_persist.py's crossfeed override block (added in S7).

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
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication([])

from rumble.core import crossfeed
from rumble.core.apo_config import ChannelSection
from rumble.core.profile_manager import (
    ACTIVE_POINTER_FILENAME, LISTENER_ACTIVE_FILENAME, DEFAULT_SETTINGS,
    ProfileManager, make_flat_config,
)
from rumble.ui.main_window import MainWindow
from rumble.ui.editor_panel import EditorPanel
from rumble.ui.theme.tokens import THEMES, DEFAULT_THEME_NAME
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
    # (no render layer active, crossfeed included -- the listener scratch
    # file is left untouched/stale in that case, see
    # ProfileManager._write_active_pointer()) or routes through the
    # listener-corrected scratch file that actually carries the composed
    # body. Follow the pointer rather than assuming the scratch file is
    # current, so a transition back to "nothing active" isn't misread as
    # still carrying stale crossfeed content.
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


# ── prefill / round trip through the dialog ─────────────────────────────

def prefill_matches_defaults() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir())
    expect(dlg.chk_crossfeed_enabled.isChecked() is False, "off by default")
    expect(dlg.combo_crossfeed_preset.currentData() == crossfeed.DEFAULT_PRESET,
           "preset combo defaults to crossfeed.DEFAULT_PRESET")
    expect(dlg.slider_crossfeed_strength.value() == 100, "strength slider defaults to 100")
    expect(dlg.lbl_crossfeed_strength.text() == "100%", "value label mirrors the slider")


def save_round_trips_all_three_keys() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir())
    dlg.chk_crossfeed_enabled.setChecked(True)
    idx = dlg.combo_crossfeed_preset.findData("chumoy")
    dlg.combo_crossfeed_preset.setCurrentIndex(idx)
    dlg.slider_crossfeed_strength.setValue(65)
    expect(dlg.lbl_crossfeed_strength.text() == "65%", "value label follows a manual slider change")

    emitted = []
    dlg.settings_saved.connect(emitted.append)
    dlg._apply_now()

    expect(emitted[0]["crossfeed_enabled"] is True, "enabled flag saved")
    expect(emitted[0]["crossfeed_preset"] == "chumoy", "preset saved")
    expect(emitted[0]["crossfeed_strength"] == 65, "strength saved")


def combo_offers_all_six_modes() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir())
    keys = [dlg.combo_crossfeed_preset.itemData(i)
            for i in range(dlg.combo_crossfeed_preset.count())]
    expect(keys == ["chumoy", "meier", "linkwitz", "bauer", "direct", "custom"],
           f"unexpected preset combo contents: {keys!r}")


def mode_switch_shows_only_the_matching_control_group() -> None:
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


def save_round_trips_custom_settings() -> None:
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


# ── non-stereo active profile gates the section ──────────────────────────

def stereo_active_profile_leaves_controls_enabled() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir(),
                          active_profile_is_stereo=True)
    expect(dlg.chk_crossfeed_enabled.isEnabled(), "checkbox enabled on a stereo profile")
    expect(dlg.combo_crossfeed_preset.isEnabled(), "preset combo enabled on a stereo profile")
    expect(dlg.slider_crossfeed_strength.isEnabled(), "strength slider enabled on a stereo profile")
    expect(dlg.lbl_crossfeed_nonstereo.isHidden(), "no non-stereo note on a stereo profile")


def non_stereo_active_profile_soft_disables_controls() -> None:
    dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], tmp_data_dir(),
                          active_profile_is_stereo=False)
    expect(not dlg.chk_crossfeed_enabled.isEnabled(), "checkbox disabled on a non-stereo profile")
    expect(not dlg.combo_crossfeed_preset.isEnabled(), "preset combo disabled on a non-stereo profile")
    expect(not dlg.slider_crossfeed_strength.isEnabled(),
           "strength slider disabled on a non-stereo profile")
    expect(not dlg.lbl_crossfeed_nonstereo.isHidden(), "non-stereo note shown")
    # Soft-disable, not reset -- the underlying value is untouched by the gate.
    expect(dlg.chk_crossfeed_enabled.isChecked() is False, "checked state unaffected by the gate")


def can_crossfeed_matches_the_gate_main_window_computes() -> None:
    # Sanity on the exact helper main_window.py calls before constructing the
    # dialog (crossfeed.can_crossfeed against the active profile's parsed
    # channel_sections). Crossfeed needs an L and an R and nothing more, so a
    # layout WITH both -- even a multichannel one -- qualifies; only a layout
    # missing one of them is a genuine no-op.
    has_pair = [ChannelSection(channel="L"), ChannelSection(channel="C"),
                ChannelSection(channel="R")]
    expect(crossfeed.can_crossfeed(has_pair) is True,
           "a layout containing both L and R can take crossfeed")
    no_pair = [ChannelSection(channel="L"), ChannelSection(channel="C")]
    expect(crossfeed.can_crossfeed(no_pair) is False,
           "a layout with no R has nothing to crossfeed")


# ── toggling through the dialog triggers a live re-render ────────────────

def dialog_toggle_reaches_a_live_rerender() -> None:
    manager = make_active_stereo_manager()
    dlg = SettingsDialog(dict(manager.settings), THEMES[DEFAULT_THEME_NAME], manager.data_dir,
                          active_profile_is_stereo=True)
    dlg.chk_crossfeed_enabled.setChecked(True)
    idx = dlg.combo_crossfeed_preset.findData("meier")
    dlg.combo_crossfeed_preset.setCurrentIndex(idx)
    dlg.slider_crossfeed_strength.setValue(100)

    emitted = []
    dlg.settings_saved.connect(emitted.append)
    dlg._apply_now()

    manager.settings.update(emitted[0])
    expect(manager.refresh_render_layers() is True, "refresh succeeds with an active stereo profile")
    text = active_pointer_text(manager)
    expect("Channel: XL XR" in text, "the live-rerendered active pointer carries the crossfeed block")
    expect("Copy: XL=R" in text, "crossfeed copy lines present in the live render")


def dialog_left_disabled_produces_no_crossfeed_in_the_render() -> None:
    manager = make_active_stereo_manager()
    dlg = SettingsDialog(dict(manager.settings), THEMES[DEFAULT_THEME_NAME], manager.data_dir,
                          active_profile_is_stereo=True)
    # chk_crossfeed_enabled left at its default (off).
    emitted = []
    dlg.settings_saved.connect(emitted.append)
    dlg._apply_now()

    manager.settings.update(emitted[0])
    manager.refresh_render_layers()
    text = active_pointer_text(manager)
    expect("Channel: XL XR" not in text, "no crossfeed block when the dialog leaves it disabled")


def dialog_toggle_off_after_being_on_removes_the_block_live() -> None:
    manager = make_active_stereo_manager()
    manager.settings["crossfeed_enabled"] = True
    manager.settings["crossfeed_strength"] = 100
    manager.refresh_render_layers()
    expect("Channel: XL XR" in active_pointer_text(manager),
           "precondition: crossfeed block present before the dialog turns it off")

    dlg = SettingsDialog(dict(manager.settings), THEMES[DEFAULT_THEME_NAME], manager.data_dir,
                          active_profile_is_stereo=True)
    expect(dlg.chk_crossfeed_enabled.isChecked(), "dialog should prefill from the manager's live setting")
    dlg.chk_crossfeed_enabled.setChecked(False)

    emitted = []
    dlg.settings_saved.connect(emitted.append)
    dlg._apply_now()

    manager.settings.update(emitted[0])
    manager.refresh_render_layers()
    expect("Channel: XL XR" not in active_pointer_text(manager),
           "turning crossfeed off through the dialog removes it from the live render")


# ── tray/toast quick toggle ──────────────────────────────────────────────

def tray_toggle_persists_and_live_renders() -> None:
    manager, w = make_window()
    toast = w.tray_icon._toast
    expect(not toast.action_crossfeed.isChecked(), "tray toggle starts unchecked (off by default)")
    # Defaults are meier/100 -- flipping just the enable toggle is enough
    # for the block to actually render.
    toast.action_crossfeed.setChecked(True)
    app.processEvents()
    expect(manager.settings["crossfeed_enabled"] is True,
           "tray toggle should persist crossfeed_enabled immediately")
    raw = manager.metadata_path.read_text(encoding="utf-8")
    expect('"crossfeed_enabled": true' in raw, "the toggle should be saved to disk immediately")
    text = active_pointer_text(manager)
    expect("Channel: XL XR" in text, "the tray toggle should trigger a live re-render")


def tray_toggle_off_removes_block_live() -> None:
    manager, w = make_window()
    manager.settings["crossfeed_enabled"] = True
    manager.refresh_render_layers()
    expect("Channel: XL XR" in active_pointer_text(manager),
           "precondition: crossfeed block present before the tray turns it off")

    toast = w.tray_icon._toast
    toast.set_crossfeed_state(True)  # sync the widget the way _build_tray_icon() does at startup
    toast.action_crossfeed.setChecked(False)
    app.processEvents()
    expect(manager.settings["crossfeed_enabled"] is False, "tray toggle-off should persist immediately")
    expect("Channel: XL XR" not in active_pointer_text(manager),
           "turning the tray toggle off should remove the block from the live render")


def settings_dialog_save_syncs_the_tray_toggle() -> None:
    # main_window._open_settings()'s Accepted branch pushes the saved
    # crossfeed_enabled value into the tray via tray_icon.set_crossfeed_state()
    # -- exercised directly here (mirrors _on_crossfeed_toggled's own
    # sync-back) since driving a modal dialog.exec() isn't practical headless.
    manager, w = make_window()
    expect(not w.tray_icon._toast.action_crossfeed.isChecked(), "tray starts unchecked")
    w.tray_icon.set_crossfeed_state(True)
    app.processEvents()
    expect(w.tray_icon._toast.action_crossfeed.isChecked(),
           "tray_icon.set_crossfeed_state() should reach the toast's checkbox")


# ── "Insert into profile..." ─────────────────────────────────────────────

def insert_into_profile_writes_block_and_disables_the_setting() -> None:
    manager, w = make_window()
    manager.settings["crossfeed_enabled"] = True
    manager.settings["crossfeed_preset"] = "chumoy"
    manager.settings["crossfeed_strength"] = 100
    dlg = SettingsDialog(dict(manager.settings), THEMES[DEFAULT_THEME_NAME], manager.data_dir,
                          active_profile_is_stereo=True)
    expect(not w._dirty, "precondition: editor must be clean before inserting")

    w._on_insert_crossfeed(dlg)
    app.processEvents()

    expect(w._dirty, "inserting should mark the editor dirty (user still saves explicitly)")
    expect(w._loaded_profile_id == "alpha", "inserting should load the active profile into the editor")
    config = w.editor_panel.build_config()
    joined = "\n".join(config.extra_lines)
    expect("Channel: XL XR" in joined, "the rendered block should land in extra_lines")
    expect("Copy: XL=R" in joined, "the block's Copy lines should be present too")
    expect(manager.settings["crossfeed_enabled"] is False,
           "the machine-level setting should be turned off to avoid a double-apply")
    expect(dlg._working["crossfeed_enabled"] is False, "the dialog's working copy should reflect that")
    expect(not dlg.chk_crossfeed_enabled.isChecked(), "the dialog's checkbox should reflect that too")
    expect(not w.tray_icon._toast.action_crossfeed.isChecked(),
           "the tray toggle should also reflect the setting being turned off")


def insert_into_profile_refuses_on_dirty_editor() -> None:
    manager, w = make_window()
    manager.settings["crossfeed_enabled"] = True
    dlg = SettingsDialog(dict(manager.settings), THEMES[DEFAULT_THEME_NAME], manager.data_dir,
                          active_profile_is_stereo=True)
    w._dirty = True  # simulate an unrelated unsaved edit already in progress
    seen = []
    QMessageBox.information = staticmethod(
        lambda *a, **k: seen.append(a[2] if len(a) > 2 else k.get("text", "")))
    try:
        w._on_insert_crossfeed(dlg)
    finally:
        QMessageBox.information = staticmethod(lambda *a, **k: None)
    app.processEvents()
    expect(len(seen) == 1, f"a refusal dialog should be shown exactly once, got {seen!r}")
    expect("unsaved" in seen[0].lower(), f"the refusal should mention unsaved changes, got {seen[0]!r}")
    expect(manager.settings["crossfeed_enabled"] is True,
           "a refused insert must not touch the crossfeed setting")
    expect(w._loaded_profile_id == "alpha", "a refused insert must not reload the editor")


def insert_into_profile_noop_when_nothing_to_insert() -> None:
    manager, w = make_window()
    # crossfeed_enabled left at its default (False) -- nothing to insert.
    # MainWindow.__init__ -> _load_profile_list() already opened the active
    # profile into the editor at startup (unrelated to this feature), so the
    # precondition to check is that a no-op insert doesn't disturb that.
    loaded_before = w._loaded_profile_id
    dlg = SettingsDialog(dict(manager.settings), THEMES[DEFAULT_THEME_NAME], manager.data_dir,
                          active_profile_is_stereo=True)
    seen = []
    QMessageBox.information = staticmethod(
        lambda *a, **k: seen.append(a[2] if len(a) > 2 else k.get("text", "")))
    try:
        w._on_insert_crossfeed(dlg)
    finally:
        QMessageBox.information = staticmethod(lambda *a, **k: None)
    app.processEvents()
    expect(len(seen) == 1, f"a 'nothing to insert' dialog should be shown exactly once, got {seen!r}")
    expect(not w._dirty, "a no-op insert must not mark the editor dirty")
    expect(w._loaded_profile_id == loaded_before, "a no-op insert must not change the loaded profile")


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


def main() -> int:
    step("the editor's per-profile crossfeed control loads, edits and exposes a spec",
         editor_crossfeed_control_loads_edits_and_exposes_spec)
    step("the crossfeed section prefills from DEFAULT_SETTINGS", prefill_matches_defaults)
    step("Save round-trips enable/preset/strength into the emitted settings dict",
         save_round_trips_all_three_keys)
    step("a stereo active profile leaves the crossfeed controls enabled",
         stereo_active_profile_leaves_controls_enabled)
    step("a non-stereo active profile soft-disables the crossfeed controls",
         non_stereo_active_profile_soft_disables_controls)
    step("crossfeed.can_crossfeed() accepts any layout carrying L and R",
         can_crossfeed_matches_the_gate_main_window_computes)
    step("the preset combo offers all six modes", combo_offers_all_six_modes)
    step("switching preset mode shows only the matching control group",
         mode_switch_shows_only_the_matching_control_group)
    step("Save round-trips Custom settings", save_round_trips_custom_settings)
    step("toggling crossfeed on through the dialog and applying it triggers a live re-render",
         dialog_toggle_reaches_a_live_rerender)
    step("leaving crossfeed disabled in the dialog keeps the live render crossfeed-free",
         dialog_left_disabled_produces_no_crossfeed_in_the_render)
    step("toggling crossfeed off through the dialog removes it from an already-live render",
         dialog_toggle_off_after_being_on_removes_the_block_live)
    step("the tray/toast quick toggle persists crossfeed_enabled and re-renders live",
         tray_toggle_persists_and_live_renders)
    step("turning the tray toggle off removes the block from an already-live render",
         tray_toggle_off_removes_block_live)
    step("tray_icon.set_crossfeed_state() reaches the toast's checkbox",
         settings_dialog_save_syncs_the_tray_toggle)
    step("Insert into profile writes the block into extra_lines and disables the setting",
         insert_into_profile_writes_block_and_disables_the_setting)
    step("Insert into profile refuses on a dirty editor",
         insert_into_profile_refuses_on_dirty_editor)
    step("Insert into profile no-ops when crossfeed has nothing to insert",
         insert_into_profile_noop_when_nothing_to_insert)
    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
