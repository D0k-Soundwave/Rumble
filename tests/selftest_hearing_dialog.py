"""Self-test for the hearing test suite dialog shell + Test A (frequency
test) pages (Phase 7 Task 6), in rumble/ui/hearing_dialog.py.

Test B (noise screening) was a UI-reachable feature here through 2026-07-09,
then hidden, then its UI was removed entirely 2026-07-16 (permanently
abandoned -- see rumble/ui/hearing_dialog.py's module docstring). Its
Qt-free core (rumble/core/digit_screening.py) still has its own dedicated
test (tests/selftest_digit_screening.py); nothing in this file exercises it
anymore.

A FakeToneEngine (is_playing always False; play()/stop() just record calls)
is injected via HearingSuiteDialog's tone_engine_factory constructor seam so
present_tone() never actually blocks waiting on real playback -- the same
seam pattern as ABXRunner's injectable rng in tests/selftest_abx.py, applied
here to audio instead of randomness.

Follows tests/selftest_solo_ab.py's exact offscreen pattern: QT_QPA_PLATFORM
set before importing PySide6, module-scope QApplication, QMessageBox patched
so nothing blocks on a modal. Several Test A confirmations (Clear All,
CalibrationHistoryDialog's delete) build a genuinely separate QMessageBox
instance (not a QMessageBox.question/.information/.warning static
convenience call), so they're patched the way
tests/selftest_safety_warning.py patches `QMessageBox.exec` directly (a
plain function assigned to the class, capturing `self` -- the actual
constructed QMessageBox instance -- so tests can inspect its real
defaultButton()/escapeButton(), not just stub a return value).

Run:  python tests/selftest_hearing_dialog.py"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication([])

QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)

# Several Test A confirmations (Clear All, CalibrationHistoryDialog's
# delete) build a directly-constructed QMessageBox rather than using the
# QMessageBox.question(...) convenience call, so they're exercised via
# `.exec()` on the instance rather than a static method. Capturing `self`
# lets tests independently inspect the real constructed instance's
# defaultButton()/escapeButton().
_captured_msgboxes: list[QMessageBox] = []
_next_confirm_result = [QMessageBox.StandardButton.Yes]


def _fake_confirm_exec(self):
    _captured_msgboxes.append(self)
    return _next_confirm_result[0]


QMessageBox.exec = _fake_confirm_exec

import time

from rumble.core import listener_slots
from rumble.core.hearing_test import STANDARD_FREQS, export_text, normalise
from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.calibration_history_dialog import CalibrationHistoryDialog
from rumble.ui.hearing_dialog import HearingSuiteDialog
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


class FakeToneEngine:
    """Records play()/stop() calls. By default is_playing is always False
    so nothing ever waits on real hardware; `manual_playing=True` makes
    is_playing a settable flag (via .playing_now) so the play-light /
    repeat-sequencer poll can be exercised deterministically."""

    def __init__(self, manual_playing: bool = False) -> None:
        self.play_calls: list[tuple[int, int]] = []
        self.stopped = False
        self._manual = manual_playing
        self.playing_now = False

    @property
    def is_playing(self) -> bool:
        return self.playing_now if self._manual else False

    def play(self, buffer, sample_rate, device=None, on_done=None) -> None:
        self.play_calls.append((len(buffer), sample_rate))
        if self._manual:
            self.playing_now = True
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        self.stopped = True
        self.playing_now = False


def _pump(seconds: float) -> None:
    """Spin the Qt event loop for *seconds* -- the non-blocking playback
    sequencer and play light are driven by a QTimer poll, which only fires
    while events are being processed."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


_COUNTER = [0]


def _new_manager(tmp: Path) -> ProfileManager:
    _COUNTER[0] += 1
    sub = tmp / f"case_{_COUNTER[0]}"
    manager = ProfileManager(data_dir=sub / "data", config_path=sub / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.activate_profile("alpha")
    return manager


def _drive_full_run(dlg: HearingSuiteDialog, true_threshold: float = -50.0) -> None:
    """Answer every (ear, freq) pair via the dialog's own respond-slots
    until Test A's internal FrequencyTest is done, simulating a listener
    whose threshold is a fixed true_threshold dBFS -- the same
    convergent-simulated-listener idea as tests/selftest_hearing_test.py's
    run_sim(), driven through the UI layer instead of the core class
    directly."""
    iterations = 0
    while dlg.freq_stack.currentWidget() is dlg.freq_test_page:
        ft = dlg._ft
        expect(ft is not None, "a frequency test should be running")
        heard = ft.staircase.level >= true_threshold
        dlg._on_response(heard)
        iterations += 1
        if iterations > 2000:
            raise AssertionError("frequency test did not complete in time")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_hearing_dialog_"))

    def ear_compensation_entry_exists_in_extra_syntax() -> None:
        from rumble.ui.ai_profiles_dialog import _EXTRA_SYNTAX
        commands = [e["command"].lower() for e in _EXTRA_SYNTAX]
        expect(any("ear compensation" in c for c in commands),
               f"No entry with 'ear compensation' in command found. Commands: {commands}")

    step("AI profiles guide references ear compensation",
         ear_compensation_entry_exists_in_extra_syntax)

    def picker_page_has_frequency_test_button() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)
        expect(hasattr(dlg, "btn_pick_frequency"),
               "picker page needs a Frequency Test button")
        expect(not hasattr(dlg, "btn_pick_screening"),
               "Test B's picker button was removed along with its UI -- it "
               "must not exist on the dialog at all")
        expect(dlg.stack.currentWidget() is dlg.picker_page,
               "dialog should open on the picker page")

    step("picker page has the Frequency Test button and no leftover Test B button",
         picker_page_has_frequency_test_button)

    def picker_opens_dashboard_and_start_engages_bypass() -> None:
        manager = _new_manager(tmp)
        expect(not manager.is_bypassed(), "precondition: not bypassed yet")
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        expect(dlg.stack.currentWidget() is dlg.freq_stack, "should enter Test A's sub-flow")
        expect(dlg.freq_stack.currentWidget() is dlg.freq_dash_page,
               "Test A should open on the dashboard (the point list)")
        expect(not manager.is_bypassed(),
               "the dashboard is pure navigation -- bypass must NOT engage yet")
        expect(dlg.btn_dash_start.text() == "Start Calibration",
               f"empty state should offer Start, got {dlg.btn_dash_start.text()!r}")
        expect(len(dlg._dash_chips) == len(STANDARD_FREQS) * 2,
               "the dashboard should list every (ear, freq) point")

        dlg.btn_dash_start.click()
        expect(dlg.freq_stack.currentWidget() is dlg.freq_intro_page,
               "the first pass of a session should route via the volume prep page")
        expect(manager.is_bypassed(),
               "the prep page plays the reference clip -- bypass engages with it")

        expect(dlg.btn_freq_start.isEnabled(),
               "Start should never be gated on playing the reference voice -- "
               "it's an optional aid, not a requirement")
        dlg.btn_play_volume_ref.click()
        expect(dlg.btn_freq_start.isEnabled(), "Start should still be enabled")

        dlg.btn_freq_start.click()
        expect(dlg.freq_stack.currentWidget() is dlg.freq_test_page,
               "Start should move to the test page")
        expect(dlg._total_pairs == len(STANDARD_FREQS) * 2,
               "the first pass should queue every point")

        dlg.close()
        expect(not manager.is_bypassed(),
               "closing mid-test must restore the prior (non-bypassed) state")

    step("picker opens the dashboard; starting a pass engages bypass; close restores",
         picker_opens_dashboard_and_start_engages_bypass)

    def closing_restores_prior_bypassed_state_when_it_was_true() -> None:
        manager = _new_manager(tmp)
        manager.set_bypass(True)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        expect(manager.is_bypassed(), "should still read bypassed while running")

        # Genuinely "mid-test": a couple of responses, nowhere near done.
        dlg._on_response(True)
        dlg._on_response(False)
        expect(dlg.freq_stack.currentWidget() is dlg.freq_test_page,
               "precondition: still mid-test, not at results")

        dlg.close()
        expect(manager.is_bypassed(),
               "closing mid-test must restore the PRIOR bypass state (True "
               "here), not unconditionally force it to False")

    step("closing mid-test restores the prior bypass state, whatever it was",
         closing_restores_prior_bypassed_state_when_it_was_true)

    def full_run_reaches_populated_results_and_snapshots_state() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        expect(manager.is_bypassed(), "bypass engaged for the run")

        # Non-blocking replay: the click starts the first repeat instantly;
        # the poll timer sequences the remaining two while the event loop
        # runs (no time.sleep freeze).
        play_calls_before = len(fake_engine.play_calls)
        dlg.btn_play_tone.click()
        expect(len(fake_engine.play_calls) - play_calls_before == 1,
               "the click itself should start exactly the first repeat")
        _pump(1.0)
        expect(len(fake_engine.play_calls) - play_calls_before >= 3,
               f"the poll timer should sequence at least 3 repeats, got "
               f"{len(fake_engine.play_calls) - play_calls_before}")

        milestone_seen = [False]

        def drive_with_milestone_watch() -> None:
            iterations = 0
            while dlg.freq_stack.currentWidget() is dlg.freq_test_page:
                ft = dlg._ft
                expect(ft is not None, "a frequency test should be running")
                # isHidden(), not isVisible(): the offscreen dialog is never
                # show()n, so isVisible() is False for every widget in it.
                if not dlg.lbl_freq_milestone.isHidden():
                    milestone_seen[0] = True
                dlg._on_response(ft.staircase.level >= -50.0)
                iterations += 1
                if iterations > 3000:
                    raise AssertionError("frequency test did not complete in time")

        drive_with_milestone_watch()
        expect(milestone_seen[0],
               "the baseline-complete milestone should appear mid-run "
               "(baseline points are queued first)")

        expect(dlg.freq_stack.currentWidget() is dlg.freq_results_page,
               "a full run should reach the results page")
        expect(manager.is_bypassed(),
               "bypass should remain engaged while the results page is showing "
               "(only exited by closing or Back to List)")

        x_left, y_left = dlg._freq_curve_left.getData()
        x_right, y_right = dlg._freq_curve_right.getData()
        expect(len(y_left) == len(STANDARD_FREQS),
               f"left curve should have {len(STANDARD_FREQS)} points, got {len(y_left)}")
        expect(len(y_right) == len(STANDARD_FREQS),
               f"right curve should have {len(STANDARD_FREQS)} points, got {len(y_right)}")
        expect(dlg.lbl_freq_summary.text() != "", "results page should show a summary sentence")

        # The working state is on disk, and completing the pass snapshotted
        # a run into the slot history too.
        state = listener_slots.load_calibration_state(
            manager.data_dir, manager.active_listener_slot)
        raw = listener_slots.calibration_raw(state)
        expect(len(raw["left"]) == len(STANDARD_FREQS),
               "every left-ear point should be in the persistent state")
        expect(len(listener_slots.load_slot_runs(
            manager.data_dir, manager.active_listener_slot)) == 1,
               "completing the pass should snapshot exactly one history run")

        dlg.close()
        expect(not manager.is_bypassed(), "closing after a full run must still restore bypass")
        expect(fake_engine.stopped, "closing should stop the fake engine's playback")

    step("a full run reaches results, shows the milestone, persists state + snapshot",
         full_run_reaches_populated_results_and_snapshots_state)

    def back_to_list_mid_test_keeps_finished_points() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        expect(manager.is_bypassed(), "bypass engaged for the run")

        # Finish exactly the first point (left ear 1 kHz), then leave.
        while dlg._ft is not None and dlg._ft.current == ("left", 1000):
            dlg._on_response(dlg._ft.staircase.level >= -50.0)
        dlg.btn_freq_test_back.click()

        expect(dlg.freq_stack.currentWidget() is dlg.freq_dash_page,
               "Back to List should return to the dashboard, not the picker")
        expect(not manager.is_bypassed(),
               "navigating back to the list mid-test must restore bypass")
        expect(dlg._ft is None, "the teardown should clear the in-progress test state")

        # The finished point survived -- on the chips, on disk, and in a
        # brand-new dialog instance (cross-session resume).
        expect(dlg._dash_chips[("left", 1000)].text() != "not yet",
               "the finished point's chip should show a measured value")
        expect("left" in dlg.btn_dash_start.text().lower()
               or "Continue" in dlg.btn_dash_start.text(),
               f"the start button should offer to continue, got "
               f"{dlg.btn_dash_start.text()!r}")
        dlg.close()

        dlg2 = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)
        dlg2.btn_pick_frequency.click()
        expect(dlg2._dash_chips[("left", 1000)].text() != "not yet",
               "a new dialog session should show the previously measured point")
        expect("Continue" in dlg2.btn_dash_start.text(),
               "a new dialog session should offer to continue the calibration")
        dlg2.close()

    step("Back to List mid-test keeps finished points, across dialog sessions",
         back_to_list_mid_test_keeps_finished_points)

    def chip_click_retests_single_point_and_skips_prep_second_time() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        _drive_full_run(dlg)
        expect(dlg.freq_stack.currentWidget() is dlg.freq_results_page,
               "precondition: first pass completed")
        dlg.btn_freq_results_back.click()
        expect(dlg.freq_stack.currentWidget() is dlg.freq_dash_page,
               "results Back to List should land on the dashboard")

        # Re-test one point: same sitting, so the volume prep page is
        # skipped and the queue is exactly that point.
        dlg._dash_chips[("right", 4000)].click()
        expect(dlg.freq_stack.currentWidget() is dlg.freq_test_page,
               "a same-session re-test should skip the volume prep page")
        expect(dlg._ft.current == ("right", 4000), "the queue should be just the clicked point")
        expect(dlg._total_pairs == 1, "a single-point pass should count 1 pair")
        # Seeded from the persistent state, not the cold -30 default.
        expect(dlg._ft.staircase.level != -30.0,
               "the re-test staircase should seed from the existing calibration")

        while dlg.freq_stack.currentWidget() is dlg.freq_test_page:
            dlg._on_response(dlg._ft.staircase.level >= -58.0)
        expect(dlg.freq_stack.currentWidget() is dlg.freq_results_page,
               "the single-point pass should finish at the results page")

        state = listener_slots.load_calibration_state(
            manager.data_dir, manager.active_listener_slot)
        new_val = state["points"]["right"][4000]["threshold"]
        expect(abs(new_val - (-58.0)) <= 4.0,
               f"the re-tested point should carry the NEW threshold, got {new_val}")
        dlg.close()

    step("a chip click re-tests just that point, skipping prep within a session",
         chip_click_retests_single_point_and_skips_prep_second_time)

    def retested_point_takes_full_effect_in_correction() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        _drive_full_run(dlg, true_threshold=-50.0)
        dlg.btn_freq_results_back.click()

        # Re-test left 4 kHz against a listener now 10 dB worse there: the
        # state-backed imbalance must reflect the NEW value fully (not a
        # history average that dilutes it).
        dlg._dash_chips[("left", 4000)].click()
        while dlg.freq_stack.currentWidget() is dlg.freq_test_page:
            dlg._on_response(dlg._ft.staircase.level >= -40.0)
        curve = listener_slots.effective_imbalance_curve(
            manager.data_dir, manager.active_listener_slot)
        by_freq = {f: v for f, v in curve}
        expect(abs(by_freq[4000.0] - 10.0) <= 5.0,
               f"imbalance at 4 kHz should reflect the full re-test delta "
               f"(~+10 dB), got {by_freq.get(4000.0)}")
        dlg.close()

    step("a re-tested point takes full effect in the correction curve",
         retested_point_takes_full_effect_in_correction)

    def clear_all_confirms_then_resets() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        _drive_full_run(dlg)
        dlg.btn_freq_results_back.click()

        # Cancel path first: nothing changes.
        _captured_msgboxes.clear()
        _next_confirm_result[0] = QMessageBox.StandardButton.Cancel
        dlg.btn_dash_clear.click()
        expect(len(_captured_msgboxes) == 1,
               "Clear All must ask for confirmation")
        box = _captured_msgboxes[-1]
        expect(box.standardButton(box.defaultButton()) == QMessageBox.StandardButton.Cancel,
               "the confirmation must default to Cancel")
        expect(dlg._dash_chips[("left", 1000)].text() != "not yet",
               "Cancel must not clear anything")

        _next_confirm_result[0] = QMessageBox.StandardButton.Yes
        dlg.btn_dash_clear.click()
        expect(dlg._dash_chips[("left", 1000)].text() == "not yet",
               "Yes should clear every chip back to unmeasured")
        expect(dlg.btn_dash_start.text() == "Start Calibration",
               "a cleared dashboard should offer a fresh start")
        state = listener_slots.load_calibration_state(
            manager.data_dir, manager.active_listener_slot)
        # History remains, so the freshly-loaded state re-seeds from the
        # latest snapshot -- but the state FILE must be gone.
        expect(not (manager.data_dir / "listeners" /
                    str(manager.active_listener_slot) / "hearing" /
                    listener_slots.STATE_FILENAME).exists(),
               "Clear All should delete the state file")
        dlg.close()

    step("Clear All is confirm-gated (Cancel default) and resets the list",
         clear_all_confirms_then_resets)

    def manage_history_deletes_and_recompiles() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        expect(dlg.btn_dash_history.isHidden(),
               "no saved runs yet -- Manage History stays hidden")

        # First pass (weaker, at -50 dB) then a second pass re-testing the
        # same points at a different level -- two saved-run snapshots, the
        # second one matching the CURRENT working state, same as any
        # completed pass leaves it (see _finish_frequency_test).
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        _drive_full_run(dlg, true_threshold=-50.0)
        dlg.btn_freq_results_back.click()
        expect(not dlg.btn_dash_history.isHidden(),
               "a saved run exists -- Manage History becomes visible")

        dlg.btn_dash_start.click()
        _drive_full_run(dlg, true_threshold=-30.0)
        dlg.btn_freq_results_back.click()

        slot_id = manager.active_listener_slot
        runs_before = listener_slots.slot_run_files(manager.data_dir, slot_id)
        expect(len(runs_before) == 2, "two completed passes -> two saved runs")

        hist = CalibrationHistoryDialog(
            manager.data_dir, slot_id, "Listener 1", theme, parent=dlg)
        expect(hist.list_widget.count() == 2, "history dialog lists both runs")

        changed = []
        hist.history_changed.connect(lambda: changed.append(True))

        # Check only the newest (last) row and delete it.
        hist.list_widget.item(1).setCheckState(Qt.CheckState.Checked)
        _next_confirm_result[0] = QMessageBox.StandardButton.Yes
        hist.btn_delete.click()

        expect(changed == [True], "deleting must emit history_changed exactly once")
        expect(hist.list_widget.count() == 1, "the deleted run drops out of the list")
        remaining = listener_slots.slot_run_files(manager.data_dir, slot_id)
        expect(len(remaining) == 1 and remaining[0].name == runs_before[0].name,
               "only the newest run was deleted, the older one survives")

        # The working state must have rolled back to the surviving (older,
        # -50 dB) run's numbers, not kept the deleted run's -30 dB reading --
        # the staircase converges NEAR true_threshold, not exactly on it
        # (same tolerance as the other re-test assertions in this file).
        dlg._cal_state = listener_slots.load_calibration_state(manager.data_dir, slot_id)
        raw = listener_slots.calibration_raw(dlg._cal_state, measured_only=True)
        expect(abs(raw["left"][1000] - (-50.0)) <= 4.0,
               f"expected a reading near the rolled-back -50 dB run, got {raw['left'][1000]}")

        # Deleting the last remaining run must clear the state entirely.
        hist.list_widget.item(0).setCheckState(Qt.CheckState.Checked)
        hist.btn_delete.click()
        expect(hist.list_widget.count() == 0, "no runs left after deleting the last one")
        expect(not (manager.data_dir / "listeners" / str(slot_id) / "hearing" /
                    listener_slots.STATE_FILENAME).exists(),
               "no runs left -> the working state file is gone too")
        dlg.close()

    step("Manage History deletes selected runs and recompiles the working state",
         manage_history_deletes_and_recompiles)

    def play_light_tracks_engine_playing_state() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine(manual_playing=True)
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        fake_engine.playing_now = False  # the reference clip is done
        dlg.btn_freq_start.click()
        _pump(0.5)  # let the auto-played first tone start
        expect(fake_engine.playing_now, "a tone should be playing after auto-play")
        expect(dlg.play_light.is_on, "the light must be ON while a tone plays")

        # Let the whole three-repeat sequence run out (each manual "tone
        # ends" hand-off lets the sequencer start the next repeat, which
        # correctly re-lights the light), then confirm the light rests OFF.
        for _ in range(10):
            if dlg._seq_reps_left <= 0 and not fake_engine.playing_now:
                break
            fake_engine.playing_now = False  # current repeat ends
            _pump(0.25)
        fake_engine.playing_now = False
        _pump(0.2)
        expect(not dlg.play_light.is_on,
               "the light must rest OFF once the sequence is over")
        dlg.close()

    step("the tone light is on exactly while the engine is playing",
         play_light_tracks_engine_playing_state)

    def gave_up_points_are_excluded_from_the_charts() -> None:
        # A gave-up point's stored threshold is the CEILING level, not a real
        # reading -- normalised against a typical 1 kHz anchor it lands around
        # +48 dB and squashes every genuine point into the bottom of the
        # chart. It must not be plotted (it shows as a "limit" chip instead).
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        state = listener_slots.load_calibration_state(
            manager.data_dir, manager.active_listener_slot)
        for ear in ("left", "right"):
            listener_slots.record_calibration_point(state, ear, 1000, -58.0)
            listener_slots.record_calibration_point(state, ear, 4000, -61.0)
            # Could not hear 16 kHz at the loudest presentable level.
            listener_slots.record_calibration_point(state, ear, 16000, -10.0,
                                                     gave_up=True)
        listener_slots.save_calibration_state(
            manager.data_dir, manager.active_listener_slot, state)
        dlg._cal_state = state

        norm = normalise(listener_slots.calibration_raw(state))
        expect(16000 in norm["left"],
               "precondition: the gave-up point IS in the normalised data")
        expect(norm["left"][16000] > 40.0,
               "precondition: it normalises to a huge value that would wreck the scale")

        freqs_log, values = dlg._plottable_curve(norm, "left")
        plotted = [round(10 ** x) for x in freqs_log]
        expect(16000 not in plotted,
               f"the gave-up 16 kHz point must not be plotted, got {plotted}")
        expect(plotted == [1000, 4000],
               f"the real measured points must still be plotted, got {plotted}")
        expect(max(values) < 10.0,
               f"chart values should be real readings only, got {values}")

        # And the dashboard still surfaces it, honestly, as a limit.
        dlg._populate_dashboard()
        expect(dlg._dash_chips[("left", 16000)].text() == "limit",
               "a gave-up point must still show on the dashboard as 'limit'")
        dlg.close()

    step("gave-up points are excluded from the charts but shown as 'limit' chips",
         gave_up_points_are_excluded_from_the_charts)

    def double_close_is_safe() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)
        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        dlg.close()
        expect(not manager.is_bypassed(), "first close restores bypass")
        # Idempotent teardown: a second close (or reject) must not raise and
        # must not re-toggle bypass state.
        dlg.close()
        expect(not manager.is_bypassed(), "second close must be a safe no-op")

    step("the teardown is idempotent across repeated close calls", double_close_is_safe)

    # ── Test A: volume-reference anchor (cross-session) ─────────────────

    def volume_reference_captured_and_offered_next_time() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        expect(dlg.lbl_volume_ref_hint.text() == "",
               "no prior run for this slot -> no hint text yet")
        expect(dlg.edit_gear_note.text() == "",
               "no prior run for this slot -> gear note field starts blank")

        dlg.btn_play_volume_ref.click()
        dlg.edit_gear_note.setText("Desk amp dial at 2 o'clock")
        dlg.btn_freq_start.click()

        expect(dlg._volume_reference is not None,
               "Start should capture a volume_reference dict")
        expect(dlg._volume_reference["gear_note"] == "Desk amp dial at 2 o'clock",
               "captured gear_note should match what was typed")
        for key in ("windows_volume_percent", "windows_volume_db", "captured_at"):
            expect(key in dlg._volume_reference, f"volume_reference missing key {key!r}")

        _drive_full_run(dlg)
        expect(dlg.freq_stack.currentWidget() is dlg.freq_results_page,
               "run should complete and reach results")

        saved = listener_slots.latest_volume_reference(
            manager.data_dir, manager.active_listener_slot)
        expect(saved is not None, "the completed run should have saved a volume_reference")
        expect(saved["gear_note"] == "Desk amp dial at 2 o'clock",
               "saved volume_reference should round-trip the gear note")

        # Exported text should surface the anchor AND its same-machine scope
        # caveat, not present it as a portable measurement.
        exported = export_text(dlg._norm, dlg._raw, "Test Device",
                                volume_reference=dlg._volume_reference)
        expect("Desk amp dial at 2 o'clock" in exported,
               "export should include the user's gear note")
        expect("does not transfer to a different device" in exported,
               "export should carry the same-machine/same-hardware scope caveat")

        dlg.close()

        # A second dialog instance for the SAME manager/slot should now
        # offer that prior run's note back as a starting point (a re-test
        # pass routes via the prep page again in a fresh session).
        dlg2 = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)
        dlg2.btn_pick_frequency.click()
        dlg2._dash_chips[("left", 1000)].click()
        expect(dlg2.freq_stack.currentWidget() is dlg2.freq_intro_page,
               "a fresh dialog session must route via the volume prep page")
        expect(dlg2.edit_gear_note.text() == "Desk amp dial at 2 o'clock",
               "reopening Test A should prefill the last saved gear note")
        expect("Desk amp dial at 2 o'clock" in dlg2.lbl_volume_ref_hint.text(),
               "reopening Test A should show the last gear note in the hint text")
        dlg2.close()

    step("volume-reference anchor is captured, saved, exported, and offered next time",
         volume_reference_captured_and_offered_next_time)

    def measured_spl_is_optional_and_overrides_the_default_anchor() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        expect(dlg.spin_measured_spl.value() == 0.0,
               "measured SPL should default to the 'Not measured' special value")

        # Left at "Not measured" (0.0): the default REFERENCE_CLIP_TARGET_PHON
        # (75) anchor applies, matching the plain (no-measurement) path.
        dlg.btn_play_volume_ref.click()
        dlg.btn_freq_start.click()
        # 80 (default iso226_reference_phon) - 75 (default anchor) = 5 dB above.
        expect(manager.settings["iso226_reference_volume_db"] is not None,
               "starting with the loudness-baseline checkbox on should anchor a value")
        default_anchor = manager.settings["iso226_reference_volume_db"]
        dlg.close()

        # This time enter a real measured reading -- the anchor must move.
        dlg2 = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)
        dlg2.btn_pick_frequency.click()
        dlg2._dash_chips[("left", 1000)].click()
        dlg2.spin_measured_spl.setValue(70.0)
        dlg2.btn_play_volume_ref.click()
        dlg2.btn_freq_start.click()
        measured_anchor = manager.settings["iso226_reference_volume_db"]
        expect(measured_anchor != default_anchor,
               "a real measured SPL reading must change the anchor from the "
               "assumed-default calculation")
        dlg2.close()

    step("measured SPL field is optional and overrides the default anchor when given",
         measured_spl_is_optional_and_overrides_the_default_anchor)

    def measured_spl_persists_and_prefills_next_session() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        dlg.spin_measured_spl.setValue(72.5)
        dlg.btn_freq_start.click()
        _drive_full_run(dlg)
        expect(dlg.freq_stack.currentWidget() is dlg.freq_results_page,
               "run should complete and reach results")
        dlg.close()

        saved = listener_slots.latest_volume_reference(
            manager.data_dir, manager.active_listener_slot)
        expect(saved is not None, "the completed run should have saved a volume_reference")
        expect(saved.get("measured_spl_db") == 72.5,
               f"saved volume_reference should round-trip the measured SPL, got {saved}")

        dlg2 = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)
        dlg2.btn_pick_frequency.click()
        dlg2._dash_chips[("left", 1000)].click()
        expect(dlg2.spin_measured_spl.value() == 72.5,
               "reopening Test A should prefill the last measured SPL reading")
        expect("72.5" in dlg2.lbl_volume_ref_hint.text(),
               "reopening Test A should show the last measured SPL in the hint text")
        dlg2.close()

    step("measured SPL persists across a run and prefills next session",
         measured_spl_persists_and_prefills_next_session)

    def reference_sound_buttons_play_through_the_shared_engine() -> None:
        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()
        dlg = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)

        dlg.btn_pick_frequency.click()
        dlg.btn_dash_start.click()
        # The intro page's engine is created by _begin_pass() before this
        # page is ever shown, so the sample buttons should already have
        # something to play through.
        expect(dlg._engine is fake_engine, "precondition: engine already live on the intro page")
        before = len(fake_engine.play_calls)
        dlg._on_play_reference_sound("hair_dryer")
        expect(len(fake_engine.play_calls) == before + 1,
               "clicking a reference-sound sample should play exactly one buffer")
        dlg.close()

    step("reference-sound sample buttons play through the already-live engine",
         reference_sound_buttons_play_through_the_shared_engine)

    def volume_drift_note_shows_on_results_page_when_meaningfully_different() -> None:
        from rumble.ui import hearing_dialog as hearing_dialog_module

        manager = _new_manager(tmp)
        theme = get_theme(DEFAULT_THEME_NAME)
        fake_engine = FakeToneEngine()

        readings = iter([(-25.0, 40.0), (-8.0, 75.0)])
        original_db = hearing_dialog_module.master_volume.get_master_volume_db
        original_pct = hearing_dialog_module.master_volume.get_master_volume_percent
        current_reading = [None]

        def fake_db():
            return current_reading[0][0]

        def fake_pct():
            return current_reading[0][1]

        hearing_dialog_module.master_volume.get_master_volume_db = fake_db
        hearing_dialog_module.master_volume.get_master_volume_percent = fake_pct
        try:
            # First run: no prior anchor yet -> no drift note, whatever the
            # reading is.
            current_reading[0] = next(readings)
            dlg1 = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)
            dlg1.btn_pick_frequency.click()
            dlg1.btn_dash_start.click()
            dlg1.btn_play_volume_ref.click()
            dlg1.btn_freq_start.click()
            _drive_full_run(dlg1)
            expect(dlg1.lbl_freq_volume_drift.text() == "",
                   "first-ever run for this slot should show no drift note")
            dlg1.close()

            # Second run: reading has moved by 17 dB (well past the 6 dB
            # threshold) -> the results page should say so.
            current_reading[0] = next(readings)
            dlg2 = HearingSuiteDialog(manager, theme, tone_engine_factory=lambda: fake_engine)
            dlg2.btn_pick_frequency.click()
            dlg2._dash_chips[("right", 8000)].click()
            dlg2.btn_play_volume_ref.click()
            dlg2.btn_freq_start.click()
            while dlg2.freq_stack.currentWidget() is dlg2.freq_test_page:
                dlg2._on_response(dlg2._ft.staircase.level >= -50.0)
            expect(dlg2.lbl_freq_volume_drift.text() != "",
                   "a meaningfully different volume than last time should show a drift note")
            expect("louder" in dlg2.lbl_freq_volume_drift.text(),
                   "the note should say which direction it moved")
            dlg2.close()
        finally:
            hearing_dialog_module.master_volume.get_master_volume_db = original_db
            hearing_dialog_module.master_volume.get_master_volume_percent = original_pct

    step("volume drift note appears on results page when volume meaningfully "
         "differs from the last run",
         volume_drift_note_shows_on_results_page_when_meaningfully_different)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
