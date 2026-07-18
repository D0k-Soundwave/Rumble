"""Selftest: the Settings dialog applies and saves every change as it is
made -- there is no Save button (offscreen Qt, real widgets: a real
SettingsDialog wired to a real MainWindow over a real ProfileManager).

Covers: the dialog exposes no Save/Cancel; touching a control schedules an
apply that lands only after the debounce (so a slider drag coalesces);
building the dialog applies nothing by itself (the _loading guard); an
audible render-layer setting toggled in the dialog reaches the live APO
render without anything being "saved"; the change is persisted to the
settings file, not just to the in-memory dict; a pending change is flushed
rather than dropped when the dialog closes; and auto_switch_enabled, which
applies through its own live signal, is not clobbered back to its at-open
value by a later unrelated change.

Run:  python tests/selftest_settings_live_apply.py
Uses Qt's offscreen platform plugin -- no window is shown.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from rumble.core import hearing_test, listener_slots
from rumble.core.profile_manager import (
    ACTIVE_POINTER_FILENAME, LISTENER_ACTIVE_FILENAME,
    ProfileManager, make_flat_config,
)
from rumble.ui.main_window import MainWindow
from rumble.ui.settings_dialog import APPLY_DEBOUNCE_MS, SettingsDialog
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
    tmp = Path(tempfile.mkdtemp(prefix="rumble_livesettings_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.activate_profile("alpha")
    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    window = MainWindow(manager, theme_engine, QIcon())
    app.processEvents()
    return manager, window


def make_dialog(manager, window) -> SettingsDialog:
    """A dialog wired to the real main window exactly as _open_settings does
    for the one connection under test here."""
    dlg = SettingsDialog(dict(manager.settings), window.theme, manager.data_dir)
    dlg.settings_saved.connect(window._apply_settings_live)
    return dlg


def wait_for_debounce() -> None:
    """Spin a real event loop past the apply debounce -- processEvents() alone
    never fires a QTimer that has not expired yet."""
    loop = QEventLoop()
    QTimer.singleShot(APPLY_DEBOUNCE_MS * 3, loop.quit)
    loop.exec()
    QCoreApplication.processEvents()


def saved_settings(manager: ProfileManager) -> dict:
    """The settings as they exist ON DISK (ProfileManager.save writes them
    under a "settings" key in metadata_path) -- the point of auto-save is that
    the file, not just the in-memory dict, is up to date."""
    path = manager.metadata_path
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("settings", {})


def active_render_text(manager: ProfileManager) -> str:
    pointer_path = manager.config_path / ACTIVE_POINTER_FILENAME
    pointer_text = pointer_path.read_text(encoding="utf-8") if pointer_path.exists() else ""
    if LISTENER_ACTIVE_FILENAME not in pointer_text:
        return ""
    path = manager.rumble_dir / LISTENER_ACTIVE_FILENAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── the Save requirement is gone ────────────────────────────────────────

def there_is_no_save_cancel_or_close_button() -> None:
    manager, window = make_window()
    dlg = make_dialog(manager, window)
    expect(not hasattr(dlg, "btn_save"), "the Save button is gone")
    expect(not hasattr(dlg, "btn_cancel"), "the Cancel button is gone")
    expect(not hasattr(dlg, "btn_close"), "the Close button is gone too -- the titlebar X closes it")


def building_the_dialog_applies_nothing() -> None:
    # Seeding controls fires the same signals a user click does, so the build
    # must not emit an apply -- otherwise merely opening Settings would write
    # settings and re-render APO.
    manager, window = make_window()
    emitted: list[dict] = []
    dlg = SettingsDialog(dict(manager.settings), window.theme, manager.data_dir)
    dlg.settings_saved.connect(emitted.append)
    wait_for_debounce()
    expect(emitted == [], f"no apply from construction alone, got {len(emitted)}")


# ── a change applies with no Save ───────────────────────────────────────

def a_toggle_applies_and_persists_by_itself() -> None:
    manager, window = make_window()
    dlg = make_dialog(manager, window)
    expect(bool(manager.settings.get("warn_unsaved_changes", True)) is True,
           "precondition: warn_unsaved_changes starts on")

    dlg.chk_warn_unsaved.setChecked(False)
    wait_for_debounce()

    expect(manager.settings["warn_unsaved_changes"] is False,
           "the toggle reached the live settings with no Save")
    expect(saved_settings(manager).get("warn_unsaved_changes") is False,
           "the toggle was auto-saved to the settings file")


def an_audible_setting_reaches_the_live_render() -> None:
    # The global crossfeed VALUE is a render-layer setting: with the source
    # mode on Global, editing that value in the dialog must change what APO is
    # playing right now, which is the whole point of "hear the difference".
    manager, window = make_window()
    manager.settings["crossfeed_mode"] = "global"
    manager.settings["crossfeed_strength"] = 0  # inert to start, so it is dark
    manager.save()
    manager.refresh_render_layers()  # clear the block make_window rendered
    dlg = make_dialog(manager, window)
    expect("Channel: XL XR" not in active_render_text(manager),
           "precondition: no crossfeed in the live render")

    dlg.slider_crossfeed_strength.setValue(100)
    wait_for_debounce()

    expect(manager.settings["crossfeed_strength"] == 100, "setting applied")
    expect("Channel: XL XR" in active_render_text(manager),
           "the live APO render carries crossfeed immediately, with no Save")

    # ...and taking the value back to zero removes it again, live.
    dlg.slider_crossfeed_strength.setValue(0)
    wait_for_debounce()
    expect("Channel: XL XR" not in active_render_text(manager),
           "zeroing the value removes it from the live render immediately")


# ── debounce ────────────────────────────────────────────────────────────

def rapid_changes_coalesce_into_one_apply() -> None:
    manager, window = make_window()
    dlg = make_dialog(manager, window)
    emitted: list[dict] = []
    dlg.settings_saved.connect(emitted.append)

    # Stand in for a slider drag: many changes inside one debounce window.
    for value in range(0, 60, 10):
        dlg.slider_crossfeed_strength.setValue(value)
    expect(emitted == [], "nothing applied while the changes are still arriving")

    wait_for_debounce()
    expect(len(emitted) == 1, f"the drag coalesced into one apply, got {len(emitted)}")
    expect(emitted[0]["crossfeed_strength"] == 50, "the applied value is the final one")


def closing_flushes_a_pending_change() -> None:
    # A change made inside the debounce window immediately before the
    # titlebar X closes the dialog must not be lost -- there is no Save to
    # fall back on.
    manager, window = make_window()
    dlg = make_dialog(manager, window)
    dlg.spin_default_bands.setValue(7)
    dlg.reject()  # what the titlebar X does; no wait: the timer is still pending
    expect(manager.settings["default_bands"] == 7,
           "the pending change was flushed on close, not dropped")
    expect(saved_settings(manager).get("default_bands") == 7,
           "the flushed change was auto-saved too")


# ── whole-dict applies must not clobber live-applied keys ───────────────

def auto_switch_survives_a_later_unrelated_change() -> None:
    # auto_switch_enabled applies through its own signal (it has to start/stop
    # the watcher), but each apply hands the main window the WHOLE working
    # dict. If _collect() skipped the key, the value snapshotted when the
    # dialog opened would be written back over it by any later change.
    manager, window = make_window()
    dlg = make_dialog(manager, window)
    dlg.auto_switch_toggled.connect(window._on_auto_switch_toggled)
    expect(bool(manager.settings.get("auto_switch_enabled", False)) is False,
           "precondition: auto-switch starts off")

    dlg.chk_auto_switch.setChecked(True)
    wait_for_debounce()
    expect(manager.settings["auto_switch_enabled"] is True, "auto-switch applied live")

    # An unrelated control, whose apply carries the whole dict along with it.
    dlg.spin_default_bands.setValue(12)
    wait_for_debounce()
    expect(manager.settings["auto_switch_enabled"] is True,
           "auto-switch is still on after an unrelated setting changed")
    expect(saved_settings(manager).get("auto_switch_enabled") is True,
           "and the settings file agrees")


def ear_calibration_page_sample_buttons_play_bypassed_and_release() -> None:
    """Settings -> Ear Calibration's "check your volume" buttons: clicking
    one should emit play_reference_sound_requested, which main_window's
    handler turns into a bypassed, non-blocking playback that releases
    bypass once the (faked) engine reports playback finished."""
    manager, window = make_window()
    dlg = make_dialog(manager, window)
    dlg.play_reference_sound_requested.connect(window._on_play_reference_sound)

    class _FakeEngine:
        def __init__(self, *a, **k):
            self.play_calls = []
            self._playing = False

        @property
        def is_playing(self):
            return self._playing

        def play(self, buffer, sample_rate, device=None, on_done=None):
            self.play_calls.append((len(buffer), sample_rate))
            self._playing = True

        def stop(self):
            self._playing = False

    import rumble.ui.main_window as mw_module
    real_tone_engine = mw_module.ToneEngine
    mw_module.ToneEngine = _FakeEngine
    try:
        expect(hasattr(dlg, "btn_play_reference_clip"),
               "Ear Calibration page needs a full-clip preview button")
        expect(set(dlg._btn_reference_sounds) == {k for k, _ in hearing_test.REFERENCE_SOUNDS},
               "every hearing_test.REFERENCE_SOUNDS entry should have a button")

        expect(not manager.is_bypassed(), "precondition: not bypassed")
        dlg._btn_reference_sounds["hair_dryer"].click()
        expect(manager.is_bypassed(), "playback should bypass EQ so the sample isn't coloured")
        expect(len(window._reference_sound_engine.play_calls) == 1,
               "clicking a sample button should play exactly one buffer")

        # Simulate the engine finishing, then let the poll timer notice.
        window._reference_sound_engine._playing = False
        window._on_reference_sound_poll()
        expect(not manager.is_bypassed(),
               "bypass should release once the poll detects playback finished")
    finally:
        mw_module.ToneEngine = real_tone_engine


def a_render_layer_toggle_reaches_a_live_editor_preview() -> None:
    """Regression test: refresh_render_layers() refuses to act while a live
    editor preview is showing (correctly -- it must never clobber the
    profile CONTENT a preview holds). But a render-layer toggle (ear
    correction, ISO 226, crossfeed) is a session overlay, not profile
    content -- turning it on/off must still reach what's actually playing
    even mid-preview, not silently no-op until the next edit."""
    manager, window = make_window()
    meta = manager.get_active_profile()
    config = manager.load_profile_config(meta.id)
    window.editor_panel.load_profile(meta, config, is_active=True, is_bypassed=False)

    # Establish a live preview -- same call the edit-debounce timer makes.
    window._push_live_preview()
    expect(manager.is_previewing, "precondition: a live preview should now be active")

    preview_path = manager.rumble_dir / "rumble_preview.txt"
    before = preview_path.read_text(encoding="utf-8")
    expect("GraphicEQ" not in before, "precondition: no correction baked into the preview yet")

    # A real imbalance so correction actually produces a non-flat curve.
    # listener_correction_enabled defaults to True (DEFAULT_SETTINGS), so
    # simply having real calibration data is enough to make the NEXT
    # preview push carry a correction -- no toggle needed yet.
    state = {"points": {"left": {}, "right": {}}, "volume_reference": None}
    for freq in (1000, 2000, 4000):
        listener_slots.record_calibration_point(state, "left", freq, -50.0)
        listener_slots.record_calibration_point(state, "right", freq, -35.0)
    listener_slots.save_calibration_state(manager.data_dir, manager.active_listener_slot, state)
    manager.invalidate_listener_correction_cache()

    dlg = make_dialog(manager, window)
    expect(dlg.chk_listener_correction.isChecked(),
           "precondition: ear correction is on by default")
    # A real False->True->False round trip: setChecked to the SAME value
    # the checkbox already holds is a Qt no-op (toggled() only fires on an
    # actual state change), so first toggle it off (a real transition) to
    # get the correction OUT of the preview, confirming the "disable"
    # direction of the fix, then back on to confirm "enable".
    dlg.chk_listener_correction.setChecked(False)
    wait_for_debounce()
    expect(manager.is_previewing,
           "the preview should still be active -- the fix must not end it")
    after_disabled_first = preview_path.read_text(encoding="utf-8")
    expect("GraphicEQ" not in after_disabled_first,
           "disabling ear correction should reach the LIVE preview immediately, "
           f"got:\n{after_disabled_first}")

    dlg.chk_listener_correction.setChecked(True)
    wait_for_debounce()

    expect(manager.is_previewing,
           "the preview should still be active -- the fix must not end it")
    after_enabled = preview_path.read_text(encoding="utf-8")
    expect("GraphicEQ" in after_enabled,
           "enabling ear correction should reach the LIVE preview immediately, "
           f"got:\n{after_enabled}")

    # And turning it back off must reach the preview too, not leave it stuck.
    dlg.chk_listener_correction.setChecked(False)
    wait_for_debounce()
    after_disabled = preview_path.read_text(encoding="utf-8")
    expect("GraphicEQ" not in after_disabled,
           f"disabling ear correction should also reach the live preview, got:\n{after_disabled}")


def iso226_unit_combo_relabels_without_changing_the_stored_value() -> None:
    """The phon/dB SPL combo next to Reference listening level is a display
    choice only -- switching it must never change spin_iso226_phon's actual
    value (what gets saved to iso226_reference_phon), only its suffix."""
    manager, window = make_window()
    dlg = make_dialog(manager, window)

    expect([dlg.combo_iso226_unit.itemText(i) for i in range(dlg.combo_iso226_unit.count())]
           == ["phon", "dB SPL"],
           "the combo should offer exactly phon and dB SPL, in that order")
    expect(dlg.spin_iso226_phon.suffix().strip() == "phon",
           f"default suffix should be phon, got {dlg.spin_iso226_phon.suffix()!r}")

    dlg.spin_iso226_phon.setValue(72.0)
    dlg.combo_iso226_unit.setCurrentIndex(1)
    expect(dlg.spin_iso226_phon.suffix().strip() == "dB SPL",
           f"suffix should switch to dB SPL, got {dlg.spin_iso226_phon.suffix()!r}")
    expect(dlg.spin_iso226_phon.value() == 72.0,
           "switching the unit label must not change the underlying value")

    dlg.combo_iso226_unit.setCurrentIndex(0)
    expect(dlg.spin_iso226_phon.suffix().strip() == "phon",
           "switching back should restore the phon suffix")
    expect(dlg.spin_iso226_phon.value() == 72.0,
           "value should still be unchanged after switching back")


def main() -> int:
    print("=== Settings apply live, with no Save ===")
    step("no Save/Cancel/Close button; the titlebar X closes it",
         there_is_no_save_cancel_or_close_button)
    step("building the dialog applies nothing", building_the_dialog_applies_nothing)
    step("a toggle applies and persists by itself", a_toggle_applies_and_persists_by_itself)
    step("an audible setting reaches the live render", an_audible_setting_reaches_the_live_render)
    step("rapid changes coalesce into one apply", rapid_changes_coalesce_into_one_apply)
    step("closing flushes a pending change", closing_flushes_a_pending_change)
    step("auto-switch survives a later unrelated change",
         auto_switch_survives_a_later_unrelated_change)
    step("Ear Calibration sample buttons play bypassed and release on finish",
         ear_calibration_page_sample_buttons_play_bypassed_and_release)
    step("ISO 226 unit combo relabels the reference-level spin box without changing its value",
         iso226_unit_combo_relabels_without_changing_the_stored_value)
    step("a render-layer toggle reaches a live editor preview instead of being silently refused",
         a_render_layer_toggle_reaches_a_live_editor_preview)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
