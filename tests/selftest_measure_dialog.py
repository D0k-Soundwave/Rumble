"""Self-test for the room-measurement wizard (Campaign C3) in
rumble/ui/measure_dialog.py.

S13's page-flow/state-machine checks use fully faked seams (mirrors
tests/selftest_hearing_dialog.py's FakeToneEngine pattern), so no real audio
hardware, no real device enumeration, and no real filesystem calibration
lookup is ever touched. S14 adds an end-to-end synthetic-"room" pass that
exercises the REAL baseline -> target -> correction chain (including the
shared synchronized play+record helper) via FakeRoomRig, a paired fake
tone-engine/mic-recorder that colors a played ESS sweep with a known filter
and hands the "recording" back once played -- no real audio hardware either,
but real DSP end to end.

Run:  python tests/selftest_measure_dialog.py
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

import numpy as np
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from rumble.core import eq_target, mic_cal, spinorama
from rumble.core.audio.mic_capture import InputDevice
from rumble.core.autoeq_merge import interp_log
from rumble.core.measurement import AgcCheckResult
from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.measure_dialog import MeasureDialog, _RECORD_LEAD_IN_S
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


class FakeToneEngine:
    """Records play()/stop() calls; is_playing is always False so the
    dialog's blocking play+record helper never actually waits."""

    def __init__(self) -> None:
        self.play_calls: list[tuple[int, int]] = []
        self.stopped = False

    @property
    def is_playing(self) -> bool:
        return False

    def play(self, buffer, sample_rate, device=None, on_done=None) -> None:
        self.play_calls.append((len(buffer), sample_rate))
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        self.stopped = True


class FakeRecorder:
    """Records record()/stop() calls; is_recording is always False and
    buffer returns a fixed small array, so nothing ever blocks."""

    def __init__(self) -> None:
        self.record_calls: list[tuple[float, int, object]] = []
        self.stopped = False

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def buffer(self):
        return np.zeros(4, dtype=np.float32)

    def record(self, duration_s, sample_rate, device=None, on_done=None) -> None:
        self.record_calls.append((duration_s, sample_rate, device))
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        self.stopped = True


_FAKE_INPUT_DEVICES = [
    InputDevice(index=0, name="Fake Mic A", is_default=True),
    InputDevice(index=1, name="Fake Mic B", is_default=False),
]

_FAKE_CAL_ENTRIES = [
    mic_cal.MicCalEntry(id="umik1", name="UMIK-1", source="file"),
    mic_cal.MicCalEntry(id="est1", name="Estimated Cal", source="estimated",
                         band=(500.0, 8000.0)),
]


def _fake_list_input_devices():
    return list(_FAKE_INPUT_DEVICES)


def _fake_list_cals(data_dir):
    return list(_FAKE_CAL_ENTRIES)


def _fake_correction_band(cal):
    if cal is None:
        return (20.0, 300.0)
    if cal.source == "estimated":
        return cal.band or (20.0, 300.0)
    return (20.0, 20000.0)


class _AgcSeam:
    """Controllable fake agc_check_fn: returns a canned AgcCheckResult and
    counts how many times the wizard actually invoked it, without
    exercising any real audio."""

    def __init__(self) -> None:
        self.processing_detected = False
        self.delta_db = 6.0
        self.calls = 0
        self.record_fns_seen: list = []

    def __call__(self, record_fn, **kwargs) -> AgcCheckResult:
        self.calls += 1
        self.record_fns_seen.append(record_fn)
        return AgcCheckResult(delta_db=self.delta_db,
                               processing_detected=self.processing_detected)


def _fake_measure_position(index: int):
    return [(1000.0, 0.0)]


_COUNTER = [0]

# Fixed sample rate for the S14 synthetic-room end-to-end tests below --
# manual mode so effective_sample_rate() never depends on this machine's
# real default audio device.
_TEST_SR = 48000


def _new_manager(tmp: Path) -> ProfileManager:
    _COUNTER[0] += 1
    sub = tmp / f"case_{_COUNTER[0]}"
    manager = ProfileManager(data_dir=sub / "data", config_path=sub / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.activate_profile("alpha")
    return manager


def _new_manager_fixed_rate(tmp: Path) -> ProfileManager:
    manager = _new_manager(tmp)
    manager.settings["sample_rate_mode"] = "manual"
    manager.settings["sample_rate"] = _TEST_SR
    return manager


def _peaking_complex_response(fc, gain_db, q, freqs, sample_rate):
    """Complex (magnitude + phase) response of an RBJ peaking filter --
    duplicated from tests/selftest_measurement.py's own helper of the same
    name, for the same reason: a target magnitude with no phase builds a
    zero-phase, non-causal, time-symmetric synthetic signal that does not
    behave like a real (causal) filtered recording under ESS deconvolution
    (see the progress file's S11 Decisions entry)."""
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * fc / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cosw0 = np.cos(w0)
    b0 = 1 + alpha * A
    b1 = -2 * cosw0
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cosw0
    a2 = 1 - alpha / A

    w = 2 * np.pi * freqs / sample_rate
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    return (b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2)


def _make_room_coloring_fn(filters):
    """Build a record_fn-shaped coloring function that applies known RBJ
    peaking filters (a list of (fc, gain_db, q) triples, cascaded by
    multiplying their complex responses) to a played signal, simulating a
    colored room/speaker capture -- no real audio hardware involved."""
    def _color(signal: np.ndarray, sample_rate: int) -> np.ndarray:
        n = len(signal)
        spectrum = np.fft.rfft(np.asarray(signal, dtype=np.float64))
        bin_freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        h_total = np.ones_like(bin_freqs, dtype=np.complex128)
        for fc, gain_db, q in filters:
            h_total = h_total * _peaking_complex_response(fc, gain_db, q, bin_freqs, sample_rate)
        colored = np.fft.irfft(spectrum * h_total, n=n)
        return colored.astype(np.float32)
    return _color


class FakeRoomRig:
    """Paired fake tone-engine + mic-recorder standing in for a real room,
    for exercising the wizard's REAL _synchronized_record -> deconvolve ->
    ir_to_magnitude chain (S14) without any real audio hardware.

    record() defers completion until play() delivers the signal (mirroring
    the real world's play-then-arrive timing) -- the wizard's own
    lead_in_s sleep is real wall-clock time, so ordering between the two is
    reliable without extra synchronization here. The produced buffer places
    the colored capture starting at the same lead_in_s offset a real
    recording would have, so _synchronized_record's own lead-in trim lines
    up with the played signal exactly as it would on real hardware.
    """

    def __init__(self, coloring_fn):
        self._coloring_fn = coloring_fn
        self._pending = None
        self._last_buffer = None
        self.play_calls = 0

    def set_coloring_fn(self, coloring_fn) -> None:
        """Swap the "physical room" mid-test -- S15 uses this to represent
        the room becoming flatter once the saved correction is previewed
        for the verify pass, without needing to re-derive make_correction's
        own math inside the fixture."""
        self._coloring_fn = coloring_fn

    # ToneEngine-shaped
    @property
    def is_playing(self) -> bool:
        return False

    def play(self, buffer, sample_rate, device=None, on_done=None) -> None:
        self.play_calls += 1
        if self._pending is not None:
            record_seconds, rec_sr, rec_on_done = self._pending
            self._pending = None
            colored = self._coloring_fn(np.asarray(buffer, dtype=np.float64), rec_sr)
            n_total = int(round(record_seconds * rec_sr))
            lead_in_samples = int(round(_RECORD_LEAD_IN_S * rec_sr))
            buf = np.zeros(n_total, dtype=np.float32)
            end = min(lead_in_samples + len(colored), n_total)
            if end > lead_in_samples:
                buf[lead_in_samples:end] = colored[:end - lead_in_samples]
            self._last_buffer = buf
            if rec_on_done is not None:
                rec_on_done()
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        pass

    # MicRecorder-shaped
    @property
    def is_recording(self) -> bool:
        return False

    @property
    def buffer(self):
        return self._last_buffer

    def record(self, duration_s, sample_rate, device=None, on_done=None) -> None:
        self._pending = (duration_s, sample_rate, on_done)


def _cal_entries_for(source: str | None) -> list:
    if source is None:
        return []
    return [mic_cal.MicCalEntry(id="flatcal", name="Flat Cal", source=source)]


def _select_cal_source(dlg: MeasureDialog, source: str | None) -> None:
    if source is None:
        dlg.cmb_cal_file.setCurrentIndex(0)
        return
    for i in range(dlg.cmb_cal_file.count()):
        data = dlg.cmb_cal_file.itemData(i)
        if isinstance(data, mic_cal.MicCalEntry) and data.source == source:
            dlg.cmb_cal_file.setCurrentIndex(i)
            return
    raise AssertionError(f"no combo entry with source={source!r}")


def _new_real_dialog(manager: ProfileManager, rig: FakeRoomRig, cal_source: str | None) -> MeasureDialog:
    """Build a MeasureDialog with every seam real EXCEPT the tone
    engine/mic recorder (FakeRoomRig) and device/cal enumeration -- the
    point of these tests is to exercise the actual S14 wiring
    (_synchronized_record, _default_measure_position, make_correction),
    not to fake it away like the S13 page-flow tests above do."""
    cal_entries = _cal_entries_for(cal_source)

    def _list_cals(data_dir):
        return list(cal_entries)

    return MeasureDialog(
        manager, get_theme(DEFAULT_THEME_NAME),
        tone_engine_factory=lambda: rig,
        mic_recorder_factory=lambda: rig,
        list_input_devices_fn=_fake_list_input_devices,
        list_cals_fn=_list_cals,
        correction_band_fn=mic_cal.correction_band,
    )


def _run_synthetic_room_to_results(manager: ProfileManager, filters,
                                    cal_source: str | None) -> tuple[MeasureDialog, "FakeRoomRig"]:
    rig = FakeRoomRig(_make_room_coloring_fn(filters))
    dlg = _new_real_dialog(manager, rig, cal_source)
    dlg.btn_intro_next.click()
    _select_cal_source(dlg, cal_source)
    dlg.btn_setup_next.click()
    dlg.btn_run_agc_check.click()
    expect(dlg._agc_result is not None and not dlg._agc_result.processing_detected,
           f"a clean linear room fake should not flag AGC processing, got {dlg._agc_result}")
    dlg.btn_level_next.click()
    for _ in range(dlg._n_positions):
        dlg.btn_measure_position.click()
    dlg.btn_baseline_next.click()
    dlg.btn_target_next.click()
    return dlg, rig


# A modest bass dip (needs +4 dB boost, within the 6 dB default cap) well
# inside the no-cal tier's <=300 Hz trust band, plus a treble bump (needs
# -5 dB cut, within the 12 dB default cap) well outside it -- lets one
# synthetic "room" demonstrate both "corrects within caps below the cap"
# and "leaves the region above the cap alone" without ever needing to
# exercise the clamp itself.
_ROOM_BASS_DIP_HZ = 150.0
_ROOM_TREBLE_BUMP_HZ = 5000.0
_ROOM_FILTERS = [(_ROOM_BASS_DIP_HZ, -4.0, 1.4), (_ROOM_TREBLE_BUMP_HZ, 5.0, 2.0)]
_FLATTEN_TOLERANCE_DB = 1.5

# S15's verify-pass fixture: a quarter of the original room's own deviation,
# standing in for "the same physical room, now measured through the saved
# correction" -- a synthetic proxy for the corrected system (not a literal
# re-simulation of make_correction's own filter math, which has no fixed
# phase model to build a causal synthetic recording from -- see the S11
# progress-file lesson on complex vs. magnitude-only synthetic filtering).
# Swapped into the FakeRoomRig via set_coloring_fn() right before the verify
# positions are measured, so the wizard's real average/smooth/RMS-deviation
# pipeline is what's actually under test, not this fixture's own numbers.
_VERIFIED_ROOM_FILTERS = [(_ROOM_BASS_DIP_HZ, -1.0, 1.4), (_ROOM_TREBLE_BUMP_HZ, 1.25, 2.0)]


def _new_dialog(manager, agc_seam=None, measure_position_fn=None) -> MeasureDialog:
    theme = get_theme(DEFAULT_THEME_NAME)
    return MeasureDialog(
        manager, theme,
        tone_engine_factory=FakeToneEngine,
        mic_recorder_factory=FakeRecorder,
        list_input_devices_fn=_fake_list_input_devices,
        list_cals_fn=_fake_list_cals,
        correction_band_fn=_fake_correction_band,
        agc_check_fn=(agc_seam if agc_seam is not None else _AgcSeam()),
        measure_position_fn=(measure_position_fn or _fake_measure_position),
    )


def _drive_to_baseline_page(dlg: MeasureDialog, agc_seam: _AgcSeam) -> None:
    dlg.btn_intro_next.click()
    dlg.btn_setup_next.click()
    agc_seam.processing_detected = False
    dlg.btn_run_agc_check.click()
    dlg.btn_level_next.click()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_measure_dialog_"))

    def opens_on_intro_page() -> None:
        manager = _new_manager(tmp)
        dlg = _new_dialog(manager)
        expect(dlg.stack.currentWidget() is dlg.intro_page,
               "dialog should open on the intro/safety page")

    step("dialog opens on the intro/safety page", opens_on_intro_page)

    def full_page_walk_with_fake_seams() -> None:
        manager = _new_manager(tmp)
        expect(not manager.is_bypassed(), "precondition: not bypassed")
        agc_seam = _AgcSeam()
        dlg = _new_dialog(manager, agc_seam=agc_seam)

        dlg.btn_intro_next.click()
        expect(manager.is_bypassed(), "Next on the intro page should engage bypass")
        expect(dlg.stack.currentWidget() is dlg.setup_page, "should reach the setup page")

        expect(dlg.cmb_input_device.count() == 2,
               "input device combo should list both fake devices")
        expect(dlg.cmb_cal_file.count() == 3,
               "cal combo should list (none) + both fake cal entries")

        dlg.btn_setup_next.click()
        expect(dlg.stack.currentWidget() is dlg.level_page, "should reach the level/AGC page")

        expect(not dlg.btn_level_next.isEnabled(),
               "Next should start disabled before the sanity check runs")
        dlg.btn_run_agc_check.click()
        expect(agc_seam.calls == 1, "Run Sanity Check should call the fake agc_check_fn once")
        expect(dlg.btn_level_next.isEnabled(),
               "Next should enable once a clean sanity check result comes back")
        dlg.btn_level_next.click()
        expect(dlg.stack.currentWidget() is dlg.baseline_page, "should reach the baseline page")

        expect(not dlg.btn_baseline_next.isEnabled(),
               "baseline Next should start disabled before any position is measured")
        for _ in range(dlg._n_positions):
            dlg.btn_measure_position.click()
        expect(all(c is not None for c in dlg._position_curves),
               "every position should have a stored curve after measuring")
        expect(dlg.btn_baseline_next.isEnabled(),
               "baseline Next should enable once every position is measured")
        dlg.btn_baseline_next.click()
        expect(dlg.stack.currentWidget() is dlg.target_page, "should reach the target page")

        dlg.btn_target_next.click()
        expect(dlg.stack.currentWidget() is dlg.results_page, "should reach the results page")
        expect(not dlg.btn_results_next.isEnabled(),
               "results Next should start disabled before the correction is saved")
        dlg.btn_save_profile.click()
        expect(dlg.btn_results_next.isEnabled(),
               "results Next should enable once the correction is saved as a profile")
        dlg.btn_results_next.click()
        expect(dlg.stack.currentWidget() is dlg.verify_page, "should reach the verify page")

        expect(not dlg.btn_verify_next.isEnabled(),
               "verify Next should start disabled before the verify measurement runs")
        for _ in range(dlg._n_positions):
            dlg.btn_measure_verify_position.click()
        expect(dlg._verify_curve, "the verify measurement should produce a curve")
        expect(dlg.btn_verify_next.isEnabled(),
               "verify Next should enable once the verify measurement completes")
        expect(manager.is_bypassed(),
               "the verify pass's own preview override must restore bypass back to the "
               "wizard's forced-True state once it finishes, not leave it False")

        dlg.btn_verify_next.click()
        expect(dlg.stack.currentWidget() is dlg.done_page, "should reach the done page")

        dlg.close()
        expect(not manager.is_bypassed(), "closing on the done page must restore bypass")

    step("full page walk with fake seams reaches the done page", full_page_walk_with_fake_seams)

    def cancel_mid_flow_restores_bypass() -> None:
        manager = _new_manager(tmp)
        expect(not manager.is_bypassed(), "precondition: not bypassed")
        agc_seam = _AgcSeam()
        dlg = _new_dialog(manager, agc_seam=agc_seam)

        _drive_to_baseline_page(dlg, agc_seam)
        expect(manager.is_bypassed(), "precondition: bypassed mid-flow")
        expect(dlg.stack.currentWidget() is dlg.baseline_page, "precondition: mid-flow")

        dlg.reject()
        expect(not manager.is_bypassed(), "cancelling mid-flow must restore bypass")

    step("cancel mid-flow restores bypass state", cancel_mid_flow_restores_bypass)

    def close_restores_prior_bypassed_state_when_it_was_true() -> None:
        manager = _new_manager(tmp)
        manager.set_bypass(True)
        dlg = _new_dialog(manager)
        dlg.btn_intro_next.click()
        expect(manager.is_bypassed(), "should still read bypassed while the wizard runs")
        dlg.close()
        expect(manager.is_bypassed(),
               "closing must restore the PRIOR bypass state (True here), not force it False")

    step("closing restores the prior bypass state, whatever it was",
         close_restores_prior_bypassed_state_when_it_was_true)

    def double_close_is_safe() -> None:
        manager = _new_manager(tmp)
        dlg = _new_dialog(manager)
        dlg.btn_intro_next.click()
        dlg.close()
        expect(not manager.is_bypassed(), "first close restores bypass")
        dlg.close()
        expect(not manager.is_bypassed(), "second close must be a safe no-op")

    step("the teardown is idempotent across repeated close calls", double_close_is_safe)

    def tier_label_reflects_cal_choice() -> None:
        manager = _new_manager(tmp)
        dlg = _new_dialog(manager)
        dlg.btn_intro_next.click()

        dlg.cmb_cal_file.setCurrentIndex(0)
        expect("Standard mic" in dlg.lbl_tier.text(),
               f"no-cal selection should show the standard-mic tier text, got: {dlg.lbl_tier.text()!r}")

        def _index_for_source(source: str) -> int:
            for i in range(dlg.cmb_cal_file.count()):
                data = dlg.cmb_cal_file.itemData(i)
                if isinstance(data, mic_cal.MicCalEntry) and data.source == source:
                    return i
            raise AssertionError(f"no combo entry with source={source!r}")

        dlg.cmb_cal_file.setCurrentIndex(_index_for_source("file"))
        expect("Calibrated mic" in dlg.lbl_tier.text(),
               f"a file-source cal should show the calibrated tier text, got: {dlg.lbl_tier.text()!r}")
        expect("20000" in dlg.lbl_tier.text(),
               f"calibrated tier text should show the full 20000 Hz range, got: {dlg.lbl_tier.text()!r}")

        dlg.cmb_cal_file.setCurrentIndex(_index_for_source("estimated"))
        expect("Estimated" in dlg.lbl_tier.text(),
               f"an estimated-source cal should show the estimated tier text, got: {dlg.lbl_tier.text()!r}")
        expect("8000" in dlg.lbl_tier.text(),
               f"estimated tier text should reflect its own stored band, got: {dlg.lbl_tier.text()!r}")

    step("tier label reflects the selected calibration's tier", tier_label_reflects_cal_choice)

    def agc_page_blocks_next_when_processing_detected() -> None:
        manager = _new_manager(tmp)
        agc_seam = _AgcSeam()
        agc_seam.processing_detected = True
        dlg = _new_dialog(manager, agc_seam=agc_seam)
        dlg.btn_intro_next.click()
        dlg.btn_setup_next.click()

        dlg.btn_run_agc_check.click()
        expect(not dlg.btn_level_next.isEnabled(),
               "Next must stay disabled when the fake check flags processing")
        expect("processing" in dlg.lbl_agc_result.text().lower(),
               "the result label should explain what was detected")

        dlg.btn_level_next.click()
        expect(dlg.stack.currentWidget() is dlg.level_page,
               "clicking a disabled/blocked Next must not advance the page")

        agc_seam.processing_detected = False
        dlg.btn_run_agc_check.click()
        expect(dlg.btn_level_next.isEnabled(), "re-running the check clean should re-enable Next")

    step("AGC page blocks Next when the faked check flags processing",
         agc_page_blocks_next_when_processing_detected)

    def tools_menu_action_constructs_the_dialog() -> None:
        from rumble.ui import main_window as mw_module

        manager = _new_manager(tmp)
        theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
        win = mw_module.MainWindow(manager, theme_engine, QIcon())

        constructed = []

        class SpyDialog:
            def __init__(self, *a, **kw):
                constructed.append((a, kw))

            def exec(self):
                return None

        original = mw_module.MeasureDialog
        mw_module.MeasureDialog = SpyDialog
        try:
            expect(hasattr(win, "action_measure_room"),
                   "MainWindow should have a Measure Room Tools-menu action")
            win.action_measure_room.trigger()
            expect(len(constructed) == 1,
                   f"triggering the action should construct exactly one MeasureDialog, "
                   f"got {len(constructed)}")
        finally:
            mw_module.MeasureDialog = original

    step("Tools menu action constructs the dialog", tools_menu_action_constructs_the_dialog)

    def agc_check_uses_the_shared_synchronized_helper() -> None:
        manager = _new_manager_fixed_rate(tmp)
        rig = FakeRoomRig(_make_room_coloring_fn([]))  # identity -- level only
        dlg = _new_real_dialog(manager, rig, cal_source=None)
        expect(not hasattr(dlg, "_default_record_fn"),
               "S13's crude unsynchronized helper must not survive S14")
        dlg.btn_intro_next.click()
        dlg.btn_setup_next.click()
        dlg.btn_run_agc_check.click()
        expect(rig.play_calls == 2,
               f"the real agc_check should play exactly 2 sweeps via the shared "
               f"synchronized helper, got {rig.play_calls}")
        expect(dlg._agc_result is not None and not dlg._agc_result.processing_detected,
               f"a clean identity room fake should not flag AGC processing, got {dlg._agc_result}")
        dlg.close()

    step("AGC check page goes through the shared synchronized helper, not the old S13 one",
         agc_check_uses_the_shared_synchronized_helper)

    def synthetic_room_correction_flattens_within_caps_for_file_cal() -> None:
        manager = _new_manager_fixed_rate(tmp)
        dlg, _rig = _run_synthetic_room_to_results(manager, _ROOM_FILTERS, cal_source="file")

        expect(dlg._correction_band == (20.0, 20000.0),
               f"a file-source cal should get the full-range correction band, "
               f"got {dlg._correction_band}")

        # Boost/cut caps are checked at the two frequencies this room's
        # known filters actually touch, not across the whole 20-20000 Hz
        # grid -- ESS deconvolution has real, documented edge-of-sweep
        # artifacts near the sweep's own f1/f2 (S11 progress-file Decisions
        # log) that can independently need clamping right at the band's
        # extremes and aren't part of what this test is checking.
        for f in (_ROOM_BASS_DIP_HZ, _ROOM_TREBLE_BUMP_HZ):
            measured = interp_log(dlg._baseline_curve, [f])[0]
            corr = interp_log(dlg._correction_curve, [f])[0]
            flattened = measured + corr
            expect(abs(flattened) < _FLATTEN_TOLERANCE_DB,
                   f"full-range correction should flatten {f} Hz toward target "
                   f"(measured={measured:.2f} corr={corr:.2f} flattened={flattened:.2f})")

        dlg.btn_save_profile.click()
        expect(dlg._saved_profile_id is not None, "saving should record the new profile's id")
        saved = manager.get_profile(dlg._saved_profile_id)
        expect(saved is not None, "the saved correction profile should land in the library")
        expect("Room Correction" in saved.tags,
               f"the saved profile should be tagged Room Correction, got {saved.tags}")
        dlg.close()

    step("synthetic-room correction flattens within caps across the full-range tier",
         synthetic_room_correction_flattens_within_caps_for_file_cal)

    def no_cal_tier_corrects_only_below_its_cap() -> None:
        manager = _new_manager_fixed_rate(tmp)
        dlg, _rig = _run_synthetic_room_to_results(manager, _ROOM_FILTERS, cal_source=None)

        expect(dlg._correction_band[1] <= 300.0,
               f"a no-cal tier should cap the correction band at <=300 Hz, "
               f"got {dlg._correction_band}")

        measured_bass = interp_log(dlg._baseline_curve, [_ROOM_BASS_DIP_HZ])[0]
        corr_bass = interp_log(dlg._correction_curve, [_ROOM_BASS_DIP_HZ])[0]
        flattened_bass = measured_bass + corr_bass
        expect(abs(flattened_bass) < _FLATTEN_TOLERANCE_DB,
               f"the no-cal tier should still correct within its cap "
               f"(measured={measured_bass:.2f} corr={corr_bass:.2f} flattened={flattened_bass:.2f})")

        corr_treble = interp_log(dlg._correction_curve, [_ROOM_TREBLE_BUMP_HZ])[0]
        expect(abs(corr_treble) < 0.5,
               f"the no-cal tier must not correct above its cap, got "
               f"{corr_treble:.2f} dB of correction at {_ROOM_TREBLE_BUMP_HZ} Hz")
        dlg.close()

    step("no-cal tier corrects nothing above its cap", no_cal_tier_corrects_only_below_its_cap)

    def verify_pass_measures_a_flatter_room_and_restores_state() -> None:
        manager = _new_manager_fixed_rate(tmp)
        dlg, rig = _run_synthetic_room_to_results(manager, _ROOM_FILTERS, cal_source="file")
        dlg.btn_save_profile.click()
        expect(dlg._saved_profile_id is not None, "save must succeed before verify can run")

        dlg.btn_results_next.click()
        expect(dlg.stack.currentWidget() is dlg.verify_page, "should reach the verify page")
        expect(not dlg.btn_verify_next.isEnabled(),
               "verify Next should start disabled before any verify measurement runs")

        # Swap the "physical room" to a flatter one, standing in for the
        # saved correction actually being applied by the previewed profile
        # (see _VERIFIED_ROOM_FILTERS's own comment for why this is a
        # synthetic proxy rather than a literal re-simulation).
        rig.set_coloring_fn(_make_room_coloring_fn(_VERIFIED_ROOM_FILTERS))
        for _ in range(dlg._n_positions):
            dlg.btn_measure_verify_position.click()

        expect(dlg._verify_curve, "the verify measurement should produce a curve")
        expect(dlg.btn_verify_next.isEnabled(),
               "verify Next should enable once the verify measurement completes")
        expect(dlg._verify_achieved_rms < dlg._verify_baseline_rms,
               f"the verified-after curve should be measurably flatter than baseline "
               f"(achieved={dlg._verify_achieved_rms:.2f} baseline={dlg._verify_baseline_rms:.2f})")
        expect(not dlg._verify_previewing,
               "completing the verify pass must clear the verify-local preview flag")
        expect(manager.is_bypassed(),
               "completing the verify pass must restore bypass to the wizard's own "
               "forced-True state, not leave it False")

        text = dlg._verify_export_text()
        expect("Calibrated mic" in text,
               f"export text should contain the tier caveat, got:\n{text}")
        expect("Achieved" in text and "predicted" in text,
               f"export text should contain the achieved-vs-predicted delta, got:\n{text}")

        dlg.btn_verify_next.click()
        dlg.close()
        expect(not manager.is_bypassed(),
               "closing on the done page must restore the ORIGINAL pre-wizard bypass state")

    step("verify pass measures a flatter room, restores state, and exports the tier caveat",
         verify_pass_measures_a_flatter_room_and_restores_state)

    def verify_pass_restores_state_when_a_position_measurement_raises() -> None:
        manager = _new_manager_fixed_rate(tmp)
        dlg, _rig = _run_synthetic_room_to_results(manager, _ROOM_FILTERS, cal_source="file")
        dlg.btn_save_profile.click()
        dlg.btn_results_next.click()

        def _boom(index):
            raise RuntimeError("simulated mic failure")
        dlg._measure_position_fn = _boom

        # Called directly (not via .click()) so the exception propagates
        # through a plain Python call rather than being swallowed by Qt's
        # own top-level slot-exception handler.
        threw = False
        try:
            dlg._on_measure_verify_position()
        except RuntimeError:
            threw = True
        expect(threw, "the raised exception should propagate rather than being swallowed")
        expect(not dlg._verify_previewing,
               "a mid-position measurement failure must still clear the verify preview flag")
        expect(manager.is_bypassed(),
               "a mid-position measurement failure must still restore bypass to the "
               "wizard's own forced-True state")

        dlg.close()
        expect(not manager.is_bypassed(),
               "closing after a failed verify attempt must still fully restore bypass")

    step("verify pass restores preview/bypass state even if a position measurement raises",
         verify_pass_restores_state_when_a_position_measurement_raises)

    def transfer_cal_flow_derives_and_selects_new_cal() -> None:
        manager = _new_manager(tmp)
        ref_id = mic_cal.save_cal(manager.data_dir, "Reference Mic",
                                  [(1000.0, 0.3)], source="file")

        passes = iter([
            [(1000.0, 0.5)],  # reference pass 1
            [(1000.0, 0.7)],  # reference pass 2
            [(1000.0, 1.5)],  # unknown pass 1
            [(1000.0, 1.7)],  # unknown pass 2
        ])

        def _fake_transfer_capture():
            return next(passes)

        agc_seam = _AgcSeam()
        dlg = MeasureDialog(
            manager, get_theme(DEFAULT_THEME_NAME),
            tone_engine_factory=FakeToneEngine,
            mic_recorder_factory=FakeRecorder,
            list_input_devices_fn=_fake_list_input_devices,
            list_cals_fn=mic_cal.list_cals,  # real -- picks up the saved reference cal
            correction_band_fn=mic_cal.correction_band,
            agc_check_fn=agc_seam,
            measure_position_fn=_fake_measure_position,
            transfer_capture_fn=_fake_transfer_capture,
        )
        dlg.btn_intro_next.click()
        expect(dlg.stack.currentWidget() is dlg.setup_page, "should reach the setup page")

        dlg.btn_calibrate_transfer.click()
        expect(dlg.stack.currentWidget() is dlg.transfer_cal_intro_page,
               "the entry button should open the transfer-cal intro page")
        expect(dlg.cmb_transfer_reference_cal.count() == 1,
               "reference-cal combo should list the one saved calibration")
        expect(dlg.btn_transfer_intro_next.isEnabled(),
               "Next should be enabled once a reference calibration exists")

        dlg.btn_transfer_intro_next.click()
        expect(dlg.stack.currentWidget() is dlg.transfer_cal_measure_page,
               "Next should open the measure page")
        expect(dlg._transfer_stage == "reference", "should start on the reference stage")
        expect(not dlg.btn_transfer_measure_next.isEnabled(),
               "measure-page Next should start disabled before any pass is captured")

        dlg.btn_transfer_measure_pass.click()
        dlg.btn_transfer_measure_pass.click()
        expect(dlg.btn_transfer_measure_next.isEnabled(),
               "Next should enable once both reference passes are measured")

        dlg.btn_transfer_measure_next.click()
        expect(dlg._transfer_stage == "unknown", "should advance to the unknown-mic stage")
        expect(dlg._transfer_reference_curve == [(1000.0, 0.6)],
               f"the two reference passes should be averaged, got {dlg._transfer_reference_curve}")

        dlg.btn_transfer_measure_pass.click()
        dlg.btn_transfer_measure_pass.click()
        dlg.btn_transfer_measure_next.click()

        expect(dlg.stack.currentWidget() is dlg.transfer_cal_done_page,
               "completing the unknown-mic stage should reach the done page")
        expect(dlg._transfer_saved_cal_id is not None, "a new calibration should have been saved")

        saved_entries = mic_cal.list_cals(manager.data_dir)
        expect(any(e.id == dlg._transfer_saved_cal_id and e.source == "transfer"
                   for e in saved_entries),
               "the derived calibration should be saved with source=transfer")

        dlg.btn_transfer_done_close.click()
        expect(dlg.stack.currentWidget() is dlg.setup_page,
               "closing the done page should return to setup")
        expect(dlg._selected_cal is not None and dlg._selected_cal.id == dlg._transfer_saved_cal_id,
               "the new calibration should be selected for the current session")

        dlg.close()

    step("transfer-cal flow derives a new calibration and selects it for the session",
         transfer_cal_flow_derives_and_selects_new_cal)

    def transfer_cal_intro_disables_next_with_no_reference_cals() -> None:
        manager = _new_manager(tmp)
        dlg = _new_dialog(manager)
        dlg.btn_intro_next.click()
        dlg.cmb_cal_file.setCurrentIndex(0)  # "(none)" -- irrelevant to the reference picker

        # This manager's data_dir has no saved calibrations, but _new_dialog's
        # fake list_cals_fn always returns two -- swap in the real (empty)
        # function to exercise the no-reference-available guard honestly.
        dlg._list_cals_fn = mic_cal.list_cals
        dlg._populate_setup_page()
        dlg.btn_calibrate_transfer.click()

        expect(dlg.cmb_transfer_reference_cal.count() == 0,
               "reference-cal combo should be empty with no saved calibrations")
        expect(not dlg.btn_transfer_intro_next.isEnabled(),
               "Next must stay disabled with no reference calibration to pick")
        expect("no saved calibrations" in dlg.lbl_transfer_intro_status.text().lower(),
               f"status label should explain why, got: {dlg.lbl_transfer_intro_status.text()!r}")

        dlg.btn_transfer_intro_back.click()
        expect(dlg.stack.currentWidget() is dlg.setup_page,
               "Back should return to setup without requiring a reference cal")
        dlg.close()

    step("transfer-cal intro page disables Next with no reference calibrations available",
         transfer_cal_intro_disables_next_with_no_reference_cals)

    def estimate_cal_subflow_derives_and_selects_new_cal() -> None:
        manager = _new_manager(tmp)
        entry = spinorama.SpinoramaEntry(
            name="Test Speaker", brand="Test", model="Speaker",
            source="ASR", measurement_key="asr", quality="high")
        on_axis_curve = [(100.0, 0.0), (1000.0, 0.0), (10000.0, 0.0), (20000.0, 0.0)]

        sample_rate = 48000
        ir = np.zeros(4800, dtype=np.float32)
        peak_idx = 100
        ir[peak_idx] = 1.0
        reflection_idx = peak_idx + int(round(0.005 * sample_rate))  # 5 ms later
        ir[reflection_idx] = 0.4  # clears the 0.15 detection ratio

        agc_seam = _AgcSeam()
        dlg = MeasureDialog(
            manager, get_theme(DEFAULT_THEME_NAME),
            tone_engine_factory=FakeToneEngine,
            mic_recorder_factory=FakeRecorder,
            list_input_devices_fn=_fake_list_input_devices,
            list_cals_fn=mic_cal.list_cals,  # real -- picks up the saved estimated cal
            correction_band_fn=mic_cal.correction_band,
            agc_check_fn=agc_seam,
            measure_position_fn=_fake_measure_position,
            estimate_index_fn=lambda: [entry],
            estimate_fetch_on_axis_fn=lambda e: list(on_axis_curve),
            estimate_capture_fn=lambda: (ir, sample_rate),
        )
        dlg.btn_intro_next.click()
        expect(dlg.stack.currentWidget() is dlg.setup_page, "should reach the setup page")

        dlg.btn_estimate_cal.click()
        expect(dlg.stack.currentWidget() is dlg.estimate_cal_pick_page,
               "the entry button should open the estimate-cal pick page")
        expect(dlg.estimate_panel.results.count() == 1,
               "the fake index's one speaker should be listed")
        expect(not dlg.btn_estimate_pick_next.isEnabled(),
               "Next should start disabled before a speaker is highlighted")

        dlg.estimate_panel.results.setCurrentRow(0)
        expect(dlg.btn_estimate_pick_next.isEnabled(),
               "Next should enable once a speaker is highlighted")

        dlg.btn_estimate_pick_next.click()
        expect(dlg.stack.currentWidget() is dlg.estimate_cal_measure_page,
               "Next should open the measure page")
        expect(not dlg.btn_estimate_save.isEnabled(),
               "Save should start disabled before a measurement runs")

        dlg.btn_estimate_measure.click()
        expect(abs(dlg._estimate_gate_ms - 5.0) < 0.1,
               f"gate should reflect the fake IR's 5 ms reflection, got {dlg._estimate_gate_ms}")
        expect(dlg.btn_estimate_save.isEnabled(),
               "Save should enable once the estimate is computed")
        expect("Estimated calibration" in dlg.lbl_estimate_result.text(),
               f"result label should show the honesty caveat, got: {dlg.lbl_estimate_result.text()!r}")

        dlg.btn_estimate_save.click()
        expect(dlg._estimate_saved_cal_id is not None, "saving should record the new cal's id")

        saved_entry, saved_curve = mic_cal.load_cal(manager.data_dir, dlg._estimate_saved_cal_id)
        expect(saved_entry.source == "estimated",
               f"the derived calibration should be saved with source=estimated, "
               f"got {saved_entry.source}")
        expected_band = mic_cal.estimated_band(dlg._estimate_gate_ms)
        expect(saved_entry.band == expected_band,
               f"saved band should match mic_cal.estimated_band(gate_ms), "
               f"got {saved_entry.band} expected {expected_band}")
        # A flat IR peak (delta function) has an ~flat spectrum, and the fake
        # on-axis curve is flat at 0 dB too -- the derived curve should land
        # close to 0 dB everywhere.
        expect(all(abs(g) < 3.0 for _, g in saved_curve),
               f"derived curve should be near-flat for a flat IR vs. a flat "
               f"on-axis curve, got {saved_curve}")

        expect(dlg.btn_estimate_measure_next.isEnabled(), "Next should enable once saved")
        dlg.btn_estimate_measure_next.click()
        expect(dlg.stack.currentWidget() is dlg.estimate_cal_done_page,
               "Next should reach the done page")

        dlg.btn_estimate_done_close.click()
        expect(dlg.stack.currentWidget() is dlg.setup_page,
               "closing the done page should return to setup")
        expect(dlg._selected_cal is not None
               and dlg._selected_cal.id == dlg._estimate_saved_cal_id,
               "the new calibration should be selected for the current session")

        dlg.close()

    step("estimate-cal sub-flow derives a new calibration and selects it for the session",
         estimate_cal_subflow_derives_and_selects_new_cal)

    def estimate_cal_measure_handles_missing_on_axis_curve() -> None:
        manager = _new_manager(tmp)
        entry = spinorama.SpinoramaEntry(
            name="No Data Speaker", brand="Test", model="NoData",
            source="ASR", measurement_key="asr")
        ir = np.zeros(4800, dtype=np.float32)
        ir[100] = 1.0

        dlg = MeasureDialog(
            manager, get_theme(DEFAULT_THEME_NAME),
            tone_engine_factory=FakeToneEngine,
            mic_recorder_factory=FakeRecorder,
            list_input_devices_fn=_fake_list_input_devices,
            list_cals_fn=mic_cal.list_cals,
            correction_band_fn=mic_cal.correction_band,
            agc_check_fn=_AgcSeam(),
            measure_position_fn=_fake_measure_position,
            estimate_index_fn=lambda: [entry],
            estimate_fetch_on_axis_fn=lambda e: [],
            estimate_capture_fn=lambda: (ir, 48000),
        )
        dlg.btn_intro_next.click()
        dlg.btn_estimate_cal.click()
        dlg.estimate_panel.results.setCurrentRow(0)
        dlg.btn_estimate_pick_next.click()
        dlg.btn_estimate_measure.click()

        expect(not dlg.btn_estimate_save.isEnabled(),
               "Save must stay disabled when the speaker has no published on-axis curve")
        expect("on-axis" in dlg.lbl_estimate_result.text().lower(),
               f"result label should explain why, got: {dlg.lbl_estimate_result.text()!r}")
        dlg.close()

    step("estimate-cal measure page reports a missing on-axis curve without saving",
         estimate_cal_measure_handles_missing_on_axis_curve)

    def estimated_tier_uses_the_tightened_boost_cap() -> None:
        manager = _new_manager_fixed_rate(tmp)
        dlg = _new_dialog(manager)

        dlg._selected_cal = None
        expect(dlg._correction_max_boost_db() == 6.0,
               "no-cal should use the standard 6 dB boost cap")

        estimated_cal = mic_cal.MicCalEntry(id="est", name="Est", source="estimated",
                                            band=(250.0, 10000.0))
        dlg._selected_cal = estimated_cal
        expect(dlg._correction_max_boost_db() == mic_cal.ESTIMATED_TIER_MAX_BOOST_DB,
               "an estimated-source cal should use the tightened boost cap")

        # Integration: a baseline needing far more boost than either cap
        # allows, across the estimated tier's own band -- the generated
        # correction must never exceed the tightened cap there.
        dlg._baseline_curve = [(f, -10.0) for f in eq_target.log_grid(20.0, 20000.0, 12)]
        dlg._generate_correction()
        band = dlg._correction_band
        in_band_gains = [g for f, g in dlg._correction_curve if band[0] <= f <= band[1]]
        expect(in_band_gains, "the estimated band should contain at least one grid point")
        expect(max(in_band_gains) <= mic_cal.ESTIMATED_TIER_MAX_BOOST_DB + 0.05,
               f"the generated correction must respect the tightened boost cap "
               f"within the estimated band, got a max of {max(in_band_gains):.2f} dB")

        dlg.close()

    step("estimated-tier corrections use the tightened boost cap",
         estimated_tier_uses_the_tightened_boost_cap)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
