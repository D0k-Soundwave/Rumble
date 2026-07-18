import os
import sys
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # Task 2 pulls in PySide6

import numpy as np
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from rumble.core import eq_math
from rumble.core.audio import tones, metering
from rumble.core.spectrum import SpectrumShaper, broadband_offset_db, subtract_response
from rumble.ui.spectrum_overlay import SpectrumController

FS = 48000
POINTS = np.logspace(np.log10(20), np.log10(20000), 128)

def shaped(sig):
    f, m = metering.spectrum_mags(sig[:, 0], FS)
    s = SpectrumShaper(POINTS, FS, attack=1.0, release=1.0)   # no smoothing for the numeric checks
    return s.update(f, m)

def test_sine_peak_lands_at_1k():
    out = shaped(tones.generate_sine(1000, 1.0, -20, FS, fade_ms=0))
    assert abs(np.log10(POINTS[np.argmax(out)]) - 3.0) < 0.05  # within ~12% of 1 kHz

def test_pink_reads_roughly_flat():
    # A single FFT frame of noise is not a fair test of "reads flat": each
    # periodogram bin's power estimate is Exponential-distributed (a single
    # complex Gaussian bin), whose log has Var(ln X) = pi^2/6 exactly (a
    # standard result). In dB (10*log10(X) = (10/ln10)*ln(X)), that's a per-bin
    # std of (10/ln10)*sqrt(pi^2/6) ~= 4.343*1.2825 ~= 5.57 dB -- *regardless
    # of FFT length* (the periodogram is not a consistent PSD estimator --
    # longer FFTs improve resolution, not variance). Only averaging
    # independent frames over time reduces that variance, which is exactly
    # what the shaper's own exponential smoothing does in real use (Task 2/3
    # call update() every ~33 ms tick). So this test feeds many independent,
    # non-overlapping real frames of the same signal through the shaper using
    # its production (IEC-ballistics-derived) smoothing defaults, and checks
    # the *converged* reading -- not one instantaneous snapshot.
    #
    # At the derived release constant, effective steady-state averaging is
    # ~1/release ~= 31 frames, giving an expected per-point std of
    # ~5.57/sqrt(31) ~= 1.0 dB. Empirically re-measured across 20 seeds with
    # these exact production defaults: max observed band spread was 5.65 dB,
    # mean 4.42 -- comfortably and consistently under the spec's ±3 dB
    # (6 dB peak-to-peak) target.
    sig = tones.generate_pink_noise(4.0, -20, FS, seed=7)
    mono = sig[:, 0]
    n_fft = 2048
    n_frames = len(mono) // n_fft
    s = SpectrumShaper(POINTS, FS)  # production defaults: real attack/release
    out = None
    for i in range(n_frames):
        block = mono[i * n_fft:(i + 1) * n_fft]
        f, m = metering.spectrum_mags(block, FS, n_fft=n_fft)
        out = s.update(f, m)
    band = out[(POINTS > 100) & (POINTS < 10000)]
    assert band.max() - band.min() < 6.0                        # flat within ±3 dB after tilt

def test_attack_fast_release_slow():
    # Assertions below are exact closed-form predictions of the algorithm's
    # own definition, not loosely-picked bounds: the first update() has no
    # prior reading, so it reports the tilted value directly (see
    # SpectrumShaper's "uninitialized running state" behavior); the tilt term
    # cancels exactly in the hot/cold difference (same frequencies, same
    # tilt), so the second update's exact expected value is
    # `up - release * (hot - cold)`.
    s = SpectrumShaper(POINTS, FS, attack=0.9, release=0.1)
    hot = np.full(POINTS.size, -10.0); cold = np.full(POINTS.size, -60.0)
    f = POINTS
    up = s.update(f, hot)                                       # from uninitialized: direct
    down1 = s.update(f, cold)

    tilt = 3.0 * np.log2(POINTS / 1000.0)
    expected_up = hot + tilt
    assert np.allclose(up, expected_up)                         # first call: unsmoothed
    expected_down1 = up - 0.1 * (hot - cold)                    # release=0.1 applied to the drop
    assert np.allclose(down1, expected_down1)
    assert up.mean() > down1.mean()                             # a real drop still occurred
    assert (up.mean() - down1.mean()) < 0.5 * abs(hot.mean() - cold.mean())  # released slowly, not fully

    # Third call, back up to hot: this is the only point in the suite that
    # exercises the `coef = attack` (rising) branch -- calls 1 and 2 above
    # cover "unsmoothed first reading" and "falling" respectively, but
    # neither touches the rising branch at all. tilted-hot > down1 here (down1
    # sits below hot_tilted by construction), so this update rises, applying
    # attack=0.9, not release.
    up2 = s.update(f, hot)
    expected_up2 = down1 + 0.9 * (expected_up - down1)
    assert np.allclose(up2, expected_up2)                       # attack=0.9 applied to the rise
    assert up2.mean() > down1.mean()                            # rose back up
    assert (up.mean() - up2.mean()) < 0.5 * (up.mean() - down1.mean())  # attack recovered most of the drop fast

def test_source_estimate():
    spec = np.full(4, -20.0); resp = np.array([0.0, 6.0, -6.0, 0.0])
    assert np.allclose(subtract_response(spec, resp), [-20, -26, -14, -20])


# ── Task 2: SpectrumController (capture-to-GUI pipeline) ────────────────
# `SpectrumController` never spawns its own real `LoopbackCapture` in these
# tests -- the constructor's `capture_factory` injection seam is handed a
# fake instead, matching the real object's shape: `.start(fragment)`,
# `.stop()`, a polled `.status` property, and it is handed an `on_block`
# callable at construction time (the fake's constructor mirrors
# `LoopbackCapture.__init__(on_block, ...)` exactly).

class _FakeCapture:
    """Minimal `LoopbackCapture`-shaped fake. `status` starts "stopped";
    `.start()` flips it to `_status_on_start` (default "running")."""

    def __init__(self, on_block, status_on_start: str = "running"):
        self.on_block = on_block
        self.status = "stopped"
        self._status_on_start = status_on_start
        self.started_with = None

    def start(self, device_name_fragment=None):
        self.started_with = device_name_fragment
        self.status = self._status_on_start

    def stop(self):
        self.status = "stopped"


def _sine_blocks(freq_hz, level_dbfs, sample_rate, n_blocks, chunk=1024):
    """Split a calibrated sine tone into (chunk, 2)-shaped blocks, the same
    shape `LoopbackCapture.on_block` delivers."""
    duration_s = (n_blocks * chunk) / sample_rate
    sig = tones.generate_sine(freq_hz, duration_s, level_dbfs, sample_rate, fade_ms=0)
    blocks = []
    for i in range(n_blocks):
        blocks.append(sig[i * chunk:(i + 1) * chunk])
    return blocks


def test_controller_spectrum_ready_peak_near_1k_from_gui_thread():
    FS = 48000
    fake = {}
    captured = {"spectrum": None, "thread": None, "level": None}

    def factory(on_block):
        cap = _FakeCapture(on_block)
        fake["cap"] = cap
        return cap

    ctrl = SpectrumController(POINTS, sample_rate=FS, capture_factory=factory)

    def on_spectrum(spec):
        captured["spectrum"] = spec
        captured["thread"] = QThread.currentThread()

    ctrl.spectrum_ready.connect(on_spectrum)
    ctrl.level_ready.connect(lambda lv: captured.__setitem__("level", lv))

    ctrl.start()
    assert fake["cap"].status == "running"

    main_thread = QThread.currentThread()

    # Push 8 chunks (= the ring buffer's full ~8192-sample window) of a
    # calibrated 1 kHz tone from a background thread, exactly as the real
    # capture thread would deliver blocks via on_block -- proving on_block
    # itself needs no GUI-thread marshaling (it only appends to the deque).
    blocks = _sine_blocks(1000.0, -20.0, FS, n_blocks=8)

    def push():
        for b in blocks:
            fake["cap"].on_block(b, FS)

    t = threading.Thread(target=push)
    t.start()
    t.join()

    app.processEvents()
    # Drive the drain tick directly rather than waiting out a real 33 ms
    # QTimer interval -- the same "call the slot directly" pattern
    # selftest_solo_ab.py uses for its debounced preview timer.
    ctrl._on_tick()

    assert captured["spectrum"] is not None, "spectrum_ready never fired"
    assert captured["thread"] == main_thread, (
        "spectrum_ready must be emitted from the GUI thread even though "
        "blocks arrived via a background thread")
    peak_freq = POINTS[np.argmax(captured["spectrum"])]
    assert abs(np.log10(peak_freq) - 3.0) < 0.05, f"expected peak near 1kHz, got {peak_freq}"
    assert captured["level"] is not None and captured["level"] > -30.0, (
        "level_ready should report a loud (-20 dBFS tone) peak")

    ctrl.stop()


def test_controller_status_unavailable():
    def factory(on_block):
        return _FakeCapture(on_block, status_on_start="unavailable")

    ctrl = SpectrumController(POINTS, sample_rate=FS, capture_factory=factory)
    seen = []
    ctrl.status_changed.connect(seen.append)

    ctrl.start()
    assert seen == ["unavailable"], f"expected a single unavailable transition, got {seen}"


def test_controller_silent_after_window_then_recovers():
    fake = {}

    def factory(on_block):
        cap = _FakeCapture(on_block)
        fake["cap"] = cap
        return cap

    # Shrink the silence window (constructor arg, per the brief) so the test
    # doesn't need to sleep out a real 2 seconds -- 4 ticks at the default
    # 33 ms tick interval comfortably clears a 0.1 s window.
    ctrl = SpectrumController(
        POINTS, sample_rate=FS, capture_factory=factory, silence_window_s=0.1,
    )
    seen = []
    ctrl.status_changed.connect(seen.append)
    ctrl.start()
    assert seen == ["running"]

    quiet_block = np.zeros((1024, 2), dtype=np.float32)  # well below -70 dBFS
    for _ in range(6):
        fake["cap"].on_block(quiet_block, FS)
        ctrl._on_tick()

    assert "silent" in seen, f"expected a silent transition after the window, got {seen}"
    assert seen[-1] == "silent", f"status should still be silent, got {seen}"

    loud_block = tones.generate_sine(1000.0, 1024 / FS, -10.0, FS, fade_ms=0)
    fake["cap"].on_block(loud_block, FS)
    ctrl._on_tick()

    assert seen[-1] == "running", f"a loud block should revert silent -> running, got {seen}"

    ctrl.stop()


# ── Task 3: EQGraph live overlay (graph curve + Live toggle + menu) ─────
# EQGraph owns the SpectrumController itself (see eq_graph.py) -- there is no
# ProfileManager/device-selection reference inside EQGraph, so tests inject a
# fake capture the same way Task 2's tests do, via a new `spectrum_capture_factory`
# constructor param that EQGraph forwards straight through to
# `SpectrumController(capture_factory=...)`.

from rumble.core.apo_config import Filter
from rumble.ui.eq_graph import EQGraph
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme


def _graph_with_fake_capture(status_on_start: str = "running"):
    fake = {}

    def factory(on_block):
        cap = _FakeCapture(on_block, status_on_start=status_on_start)
        fake["cap"] = cap
        return cap

    graph = EQGraph(get_theme(DEFAULT_THEME_NAME), spectrum_capture_factory=factory)
    return graph, fake


def test_live_toggle_on_creates_curve_and_starts_controller():
    graph, fake = _graph_with_fake_capture()
    assert not graph._spectrum_curve.isVisible(), "curve must start hidden"

    graph._btn_live.setChecked(True)

    assert graph._spectrum_curve.isVisible()
    assert fake["cap"].status == "running", "Live on must start the injected capture"
    assert fake["cap"].started_with is None  # follows the system default device

    graph._btn_live.setChecked(False)


def test_spectrum_ready_updates_curve_ydata():
    graph, _fake = _graph_with_fake_capture()
    graph._btn_live.setChecked(True)

    freqs = eq_math.frequency_array()
    fake_spectrum = np.linspace(-40.0, -10.0, len(freqs))
    graph._spectrum_ctrl.spectrum_ready.emit(fake_spectrum)

    xdata, ydata = graph._spectrum_curve.getData()
    # The waveform is zero-referenced: each frame's own average sits on the 0
    # line, so the drawn data is the spectrum minus its mean (bands above/below
    # the average bump up/down off centre).
    assert np.allclose(ydata, fake_spectrum - np.mean(fake_spectrum))
    assert abs(np.mean(ydata)) < 1e-9, "the drawn waveform is centred on zero"
    assert np.allclose(xdata, np.log10(freqs))

    graph._btn_live.setChecked(False)


def test_live_toggle_off_hides_curve_and_stops_controller():
    graph, fake = _graph_with_fake_capture()
    graph._btn_live.setChecked(True)
    assert fake["cap"].status == "running"

    graph._btn_live.setChecked(False)

    assert not graph._spectrum_curve.isVisible()
    assert fake["cap"].status == "stopped"


def test_hint_shown_only_for_unavailable_not_silent():
    """"silent" only means the (healthy, open) loopback stream is reading
    digital silence -- i.e. nothing is playing -- which an empty graph
    already says; no overlay text, and it must not blame exclusive/ASIO.
    "unavailable" (stream could not be opened at all) is the one status
    where that blame belongs, so it alone shows a hint."""
    graph, _fake = _graph_with_fake_capture()
    graph._btn_live.setChecked(True)

    graph._spectrum_ctrl.status_changed.emit("silent")
    assert not graph._spectrum_hint.isVisible(), "'silent' shows no hint at all"

    graph._spectrum_ctrl.status_changed.emit("unavailable")
    assert graph._spectrum_hint.isVisible()
    unavailable_text = graph._spectrum_hint.toPlainText()
    assert "exclusive" in unavailable_text.lower(), (
        f"'unavailable' is where exclusive/ASIO belongs: {unavailable_text!r}")

    graph._spectrum_ctrl.status_changed.emit("running")
    assert not graph._spectrum_hint.isVisible(), "a healthy stream shows no hint"

    graph._btn_live.setChecked(False)


def test_spectrum_captures_the_selected_device_not_the_system_default():
    """Regression: capture was always started with no fragment, so it read the
    system DEFAULT output's loopback regardless of which endpoint the profile
    targets. EQ'ing a non-default device meant capturing a permanently silent
    stream from the wrong endpoint."""
    graph, fake = _graph_with_fake_capture()

    graph.set_spectrum_device("Speakers FIIO K13 R2R")
    graph._btn_live.setChecked(True)
    assert fake["cap"].started_with == "Speakers FIIO K13 R2R"

    # Changing the device while Live is on re-targets the running capture.
    graph.set_spectrum_device("Headphones Some Other DAC")
    assert fake["cap"].started_with == "Headphones Some Other DAC"

    # "all" (and None) mean "follow the system default output" -- the one case
    # where a bare default-loopback open is the right answer.
    graph.set_spectrum_device("all")
    assert fake["cap"].started_with is None

    graph._btn_live.setChecked(False)


def test_capture_endpoint_falls_back_to_default_output_loopback():
    """A capture (microphone) endpoint has no WASAPI loopback tap. Passing its
    pattern to the resolver would find nothing, fail the open, and report
    "unavailable" -- i.e. the exclusive/ASIO hint, for a device that simply has
    no tap. Fall back to the system default output's loopback instead."""
    graph, fake = _graph_with_fake_capture()

    graph.set_spectrum_device("Microphone Blue Yeti", has_loopback=False)
    graph._btn_live.setChecked(True)

    assert fake["cap"].started_with is None, (
        "a mic endpoint must fall back to the default output loopback, "
        f"not be handed to the loopback resolver: {fake['cap'].started_with!r}")
    assert fake["cap"].status == "running"

    graph._btn_live.setChecked(False)


def test_fallback_capture_suppresses_drift_detection():
    """While captured audio comes from a DIFFERENT device than the profile
    targets, the measured spectrum (the speakers) and the calculated response
    (the mic's curve) describe unrelated signals. Correlating them would never
    succeed and would latch the drift flag on permanently, falsely reporting
    that the EQ isn't working."""
    graph, _fake = _graph_with_fake_capture()
    graph.set_filters([Filter(type="PK", fc=1000.0, gain=6.0, q=1.0, enabled=True)])

    # Render endpoint: drift is judged against the rendered response.
    graph.set_spectrum_device("Speakers FIIO K13 R2R")
    graph.set_filters([Filter(type="PK", fc=1000.0, gain=6.0, q=1.0, enabled=True)])
    assert graph._spectrum_ctrl._calculated_response_db is not None

    # Capture endpoint -> fallback -> drift must be disabled, both immediately
    # and on the next set_filters() refresh.
    graph.set_spectrum_device("Microphone Blue Yeti", has_loopback=False)
    assert graph._spectrum_is_fallback
    assert graph._spectrum_ctrl._calculated_response_db is None

    graph.set_filters([Filter(type="PK", fc=1000.0, gain=6.0, q=1.0, enabled=True)])
    assert graph._spectrum_ctrl._calculated_response_db is None, (
        "set_filters() must not re-arm drift while the capture is on a "
        "fallback device")

    # Back to a render endpoint: drift comes back.
    graph.set_spectrum_device("Speakers FIIO K13 R2R")
    assert not graph._spectrum_is_fallback
    graph.set_filters([Filter(type="PK", fc=1000.0, gain=6.0, q=1.0, enabled=True)])
    assert graph._spectrum_ctrl._calculated_response_db is not None


def test_set_spectrum_device_does_not_restart_capture_when_unchanged():
    """It is called from the editor's central graph-sync path, which runs on
    every recompute -- reopening the loopback stream each time would thrash
    it."""
    graph, fake = _graph_with_fake_capture()
    graph.set_spectrum_device("Speakers FIIO K13 R2R")
    graph._btn_live.setChecked(True)

    fake["cap"].started_with = "sentinel"
    graph.set_spectrum_device("Speakers FIIO K13 R2R")
    assert fake["cap"].started_with == "sentinel", "unchanged device must not restart capture"

    graph._btn_live.setChecked(False)


def test_mode_switch_to_source_subtracts_current_response():
    graph, _fake = _graph_with_fake_capture()
    # A single +6 dB peaking band at 1kHz gives a non-trivial combined
    # response for "Source (estimated)" to subtract back out.
    graph.set_filters([Filter(type="PK", fc=1000.0, gain=6.0, q=1.0, enabled=True)])
    graph._btn_live.setChecked(True)

    freqs = eq_math.frequency_array()
    fake_spectrum = np.full(len(freqs), -20.0)

    # The waveform is zero-referenced (drawn minus its own per-frame mean).
    graph._spectrum_ctrl.spectrum_ready.emit(fake_spectrum)
    _, y_output = graph._spectrum_curve.getData()
    assert np.allclose(y_output, fake_spectrum - np.mean(fake_spectrum)), \
        "Output waveform draws the spectrum centred on zero"

    # Input (estimated) now draws to its own curve — output and input are
    # separate series so "both" mode can overlay them for comparison.
    graph._act_mode_source.trigger()
    graph._spectrum_ctrl.spectrum_ready.emit(fake_spectrum)
    _, y_source = graph._spectrum_in_curve.getData()

    src = subtract_response(fake_spectrum, graph._spectrum_response_db)
    expected = src - np.mean(src)
    assert np.allclose(y_source, expected)
    assert not np.allclose(y_source, y_output), "Input mode must actually change the drawn data"

    graph._btn_live.setChecked(False)


# ── Task 4: measured clipping (SpectrumController.clipping_changed) ─────

def test_clipping_flag_sets_after_three_sustained_ticks_not_before():
    fake = {}

    def factory(on_block):
        cap = _FakeCapture(on_block)
        fake["cap"] = cap
        return cap

    ctrl = SpectrumController(POINTS, sample_rate=FS, capture_factory=factory)
    seen = []
    ctrl.clipping_changed.connect(seen.append)
    ctrl.start()

    # 0 dBFS full-scale square block -- comfortably >= the -0.1 dBFS
    # threshold on every tick.
    full_scale_block = np.ones((1024, 2), dtype=np.float32)

    fake["cap"].on_block(full_scale_block, FS)
    ctrl._on_tick()
    assert seen == [], f"must not flip True before 3 sustained emissions, got {seen}"

    fake["cap"].on_block(full_scale_block, FS)
    ctrl._on_tick()
    assert seen == [], f"must not flip True before 3 sustained emissions, got {seen}"

    fake["cap"].on_block(full_scale_block, FS)
    ctrl._on_tick()
    assert seen == [True], f"expected a single True flip on the 3rd sustained emission, got {seen}"

    # A 4th sustained tick must not re-emit (only flips are reported).
    fake["cap"].on_block(full_scale_block, FS)
    ctrl._on_tick()
    assert seen == [True], f"must only emit on the flip, not every tick, got {seen}"

    ctrl.stop()


def test_clipping_flag_clears_after_hold_window_of_quiet_blocks():
    fake = {}

    def factory(on_block):
        cap = _FakeCapture(on_block)
        fake["cap"] = cap
        return cap

    # Shrink the clear window (constructor arg, per the brief) so the test
    # doesn't need to sleep out a real 5 seconds -- mirrors the silence
    # test's use of a shrunk silence_window_s.
    ctrl = SpectrumController(
        POINTS, sample_rate=FS, capture_factory=factory, clip_clear_window_s=0.1,
    )
    seen = []
    ctrl.clipping_changed.connect(seen.append)
    ctrl.start()

    full_scale_block = np.ones((1024, 2), dtype=np.float32)
    for _ in range(3):
        fake["cap"].on_block(full_scale_block, FS)
        ctrl._on_tick()
    assert seen == [True], f"expected the flag to set first, got {seen}"

    # _on_tick reads a peak over the WHOLE ring-buffer window (up to 8
    # chunks, per Task 2's `_RING_BUFFER_CHUNKS`), not just the newest
    # block -- so the loud blocks just pushed would still linger in that
    # window for several more ticks even with no new loud audio arriving.
    # Clear the ring buffer directly to simulate the transient having fully
    # decayed out of the capture window, isolating this test to the
    # clear-window countdown this task actually adds.
    ctrl._blocks.clear()

    quiet_block = np.zeros((1024, 2), dtype=np.float32)
    for _ in range(6):  # 6 * 33ms > 0.1s shrunk window
        fake["cap"].on_block(quiet_block, FS)
        ctrl._on_tick()

    assert seen == [True, False], f"expected the flag to clear after the hold window, got {seen}"

    ctrl.stop()


def test_clipping_flag_clears_when_controller_stopped_while_clipping():
    """Regression test for review finding: previously `stop()` neither
    reset nor emitted the sustained clipping flag, so it stayed stuck
    `True` after Live was toggled off -- and a subsequent `start()` reset
    it back to `False` *silently* (no signal), meaning even feeding only
    quiet ticks afterward would never see a True->False edge to emit. The
    only way to clear it was to trigger a brand new clip event."""
    fake = {}

    def factory(on_block):
        cap = _FakeCapture(on_block)
        fake["cap"] = cap
        return cap

    ctrl = SpectrumController(POINTS, sample_rate=FS, capture_factory=factory)
    seen = []
    ctrl.clipping_changed.connect(seen.append)
    ctrl.start()

    full_scale_block = np.ones((1024, 2), dtype=np.float32)
    for _ in range(3):
        fake["cap"].on_block(full_scale_block, FS)
        ctrl._on_tick()
    assert seen == [True], f"expected the flag to set first, got {seen}"

    ctrl.stop()

    assert seen == [True, False], (
        "stop() must clear a sustained clipping flag and emit the False "
        f"transition immediately, got {seen}")

    # Restart and feed only quiet ticks: must NOT re-emit anything (no
    # stale flag, no flicker) -- proves start()'s own silent reset isn't
    # papering over a still-broken stop().
    ctrl.start()
    quiet_block = np.zeros((1024, 2), dtype=np.float32)
    fake["cap"].on_block(quiet_block, FS)
    ctrl._on_tick()
    assert seen == [True, False], (
        f"quiet ticks after a clean stop/restart must not re-emit, got {seen}")

    ctrl.stop()


def test_measured_clipping_clears_when_live_toggled_off_while_clipping():
    """Full end-to-end regression test for the same review finding, driven
    through the real user-facing path: Live toggle off (`_btn_live`) ->
    `EQGraph._apply_spectrum_enabled(False)` -> `SpectrumController.stop()`.
    Must clear `measured_clipping_changed` immediately, not leave the
    headroom indicator (editor_panel.py) stuck at its clipping-active
    state indefinitely."""
    graph, fake = _graph_with_fake_capture()
    seen = []
    graph.measured_clipping_changed.connect(seen.append)

    graph._btn_live.setChecked(True)

    full_scale_block = np.ones((1024, 2), dtype=np.float32)
    for _ in range(3):
        fake["cap"].on_block(full_scale_block, FS)
        graph._spectrum_ctrl._on_tick()
    assert seen == [True], f"expected the flag to set first, got {seen}"

    graph._btn_live.setChecked(False)  # Live off

    # The controller's own stop() emits the True->False flip, and EQGraph's
    # else-branch also forces an unconditional False (belt-and-suspenders,
    # mirroring how it unconditionally clears `_spectrum_hint` too) -- so a
    # harmless duplicate `False` is expected. What matters: it flips once
    # to True, then never sees another True, and ends up False.
    assert seen[0] is True, f"expected the flag to set first, got {seen}"
    assert all(v is False for v in seen[1:]), (
        "toggling Live off while measured clipping is active must clear it "
        f"immediately (only False after the initial True), got {seen}")
    assert seen[-1] is False, f"must end up cleared, got {seen}"


def test_eqgraph_reexposes_measured_clipping_changed():
    graph, fake = _graph_with_fake_capture()
    graph._btn_live.setChecked(True)

    seen = []
    graph.measured_clipping_changed.connect(seen.append)

    graph._spectrum_ctrl.clipping_changed.emit(True)
    assert seen == [True]

    graph._spectrum_ctrl.clipping_changed.emit(False)
    assert seen == [True, False]

    graph._btn_live.setChecked(False)


def test_spectrum_settings_round_trip_across_fresh_construction():
    # Mirrors how main_window.py applies `graph_y_range` after construction
    # (EQGraph has no settings/manager reference of its own): the caller
    # reads settings, then calls a configure_* method explicitly.
    graph1, _fake1 = _graph_with_fake_capture()
    graph1.configure_spectrum_settings(enabled=True, mode="source", peak_hold=True)

    assert graph1._btn_live.isChecked()
    assert graph1._spectrum_mode == "source"
    assert graph1._act_peak_hold.isChecked()

    settings = {
        "spectrum_enabled": graph1._spectrum_enabled,
        "spectrum_mode": graph1._spectrum_mode,
        "spectrum_peak_hold": graph1._spectrum_peak_hold,
    }
    assert settings == {
        "spectrum_enabled": True, "spectrum_mode": "source", "spectrum_peak_hold": True,
    }
    graph1._btn_live.setChecked(False)

    graph2, _fake2 = _graph_with_fake_capture()
    # Mirrors how main_window.py would read these back from
    # `manager.settings` and apply them (same shape as its existing
    # `graph_y_range` -> `set_y_range()` call).
    graph2.configure_spectrum_settings(
        enabled=settings["spectrum_enabled"],
        mode=settings["spectrum_mode"],
        peak_hold=settings["spectrum_peak_hold"],
    )

    assert graph2._btn_live.isChecked()
    assert graph2._spectrum_mode == "source"
    assert graph2._act_peak_hold.isChecked()
    graph2._btn_live.setChecked(False)


# ── Task 4: editor_panel headroom indicator wiring end-to-end ───────────

from rumble.ui.editor_panel import EditorPanel


def test_measured_clipping_overrides_headroom_label_then_restores():
    panel = EditorPanel(get_theme(DEFAULT_THEME_NAME))
    panel._update_headroom_label()
    default_class = panel.lbl_headroom.property("class")
    default_tooltip = panel.lbl_headroom.toolTip()
    assert default_class in ("value-success", "value-error")

    # Drive it through the real signal chain (EQGraph re-exposing its
    # SpectrumController's clipping_changed), not by calling the handler
    # directly -- proving _build_graph_section()'s new connection actually
    # wires end to end.
    panel.graph.measured_clipping_changed.emit(True)

    assert panel._measured_clipping is True
    assert panel.lbl_headroom.property("class") == "value-warning"
    assert panel.lbl_headroom.toolTip() == "Measured output peaks at/above 0 dBFS"

    panel.graph.measured_clipping_changed.emit(False)

    assert panel._measured_clipping is False
    assert panel.lbl_headroom.property("class") == default_class, (
        "clearing measured clipping must restore the calculated success/error state")
    assert panel.lbl_headroom.toolTip() == default_tooltip, (
        "clearing measured clipping must restore the original headroom tooltip")


def test_measured_clipping_updates_accessible_description_too():
    """Review finding: the clipping-override branch updated the tooltip and
    QSS class but never the accessible description, so a screen-reader user
    got no textual indication of the "measured clipping" state -- only a
    tooltip + color change neither of which a screen reader surfaces on its
    own. `setAccessibleDescription` must flip alongside the tooltip, and
    restore alongside it too."""
    panel = EditorPanel(get_theme(DEFAULT_THEME_NAME))
    panel._update_headroom_label()
    default_description = panel.lbl_headroom.accessibleDescription()
    assert default_description, "lbl_headroom must have a describe()'d accessible description"

    panel.graph.measured_clipping_changed.emit(True)

    clipping_description = panel.lbl_headroom.accessibleDescription()
    assert clipping_description != default_description, (
        "accessible description must change when measured clipping activates")
    assert "clip" in clipping_description.lower()

    panel.graph.measured_clipping_changed.emit(False)

    assert panel.lbl_headroom.accessibleDescription() == default_description, (
        "clearing measured clipping must restore the original accessible description")


# ── Whole-branch review findings (final Phase 6 review, commit 72ad493) ──

def test_stop_spectrum_stops_controller_regardless_of_enabled_state():
    """Regression test for review Finding 1: EQGraph had no way to stop its
    SpectrumController independent of the Live toggle state, so
    MainWindow._apply_ui_size_change()'s in-process window rebuild (which
    explicitly calls `self._auto_switch_adapter.close()` before tearing
    down the old window) had no equivalent call for the old EQGraph's
    capture thread/stream -- leaking a daemon thread + a second WASAPI
    loopback stream against the same device on every size toggle while
    Live was on. Tested at the EQGraph level (see report for why a full
    MainWindow-level test was judged impractical): `stop_spectrum()` must
    unconditionally stop the underlying capture even though the widget's
    own Live toggle is still (visually) on -- it's being torn down, not
    turned off by the user."""
    graph, fake = _graph_with_fake_capture()
    graph._btn_live.setChecked(True)
    assert fake["cap"].status == "running"

    graph.stop_spectrum()

    assert fake["cap"].status == "stopped", (
        "stop_spectrum() must stop the underlying capture so an outgoing "
        "EQGraph (e.g. during MainWindow's in-process rebuild) doesn't leak "
        "its capture thread/stream into the new window's lifetime")


def test_hiding_graph_pauses_spectrum_and_showing_resumes():
    """Regression test for review Finding 2: design spec Sec11 ("auto-pauses
    when the graph is hidden") was never wired -- `SpectrumController.
    set_visible()` existed but nothing called it, so Live capture + 30fps
    FFT work kept running even while the app sat minimized in the tray.
    Confirms hiding EQGraph (e.g. via the top-level window being hidden --
    exactly how MainWindow's tray-hide `closeEvent` works, verified
    separately to propagate Qt hide/show events down to child widgets)
    pauses capture via `set_visible(False)`, and showing it again resumes
    via `set_visible(True)` -- only while Live is actually on."""
    graph, fake = _graph_with_fake_capture()
    graph.show()  # a never-shown widget is already "hidden" -- no transition to fire hideEvent
    app.processEvents()
    graph._btn_live.setChecked(True)
    assert fake["cap"].status == "running"

    graph.hide()
    app.processEvents()
    assert fake["cap"].status == "stopped", (
        "hiding the graph while Live is on must pause capture via set_visible(False)")

    graph.show()
    app.processEvents()
    assert fake["cap"].status == "running", (
        "showing the graph again must resume capture via set_visible(True)")

    graph._btn_live.setChecked(False)


def test_hiding_graph_with_live_off_does_not_start_capture():
    """Showing a hidden graph must not spuriously start capture when the
    user never turned Live on in the first place."""
    graph, fake = _graph_with_fake_capture()
    graph.show()
    app.processEvents()

    graph.hide()
    app.processEvents()
    graph.show()
    app.processEvents()

    assert fake["cap"].status == "stopped", (
        "showing the graph must not start capture when Live has never been enabled")


def test_on_tick_handles_mixed_channel_count_blocks_without_raising():
    """Regression test for review Finding 3: `LoopbackCapture._reconnect()`
    (Phase 4, following the system default device via `device_fragment=
    None`) can reopen against a new default device with a different
    channel count mid-session, leaving blocks of differing channel counts
    sitting together in the ring buffer (e.g. a stereo block next to a
    mono one). The old `_on_tick()` concatenated raw blocks first (`np.
    concatenate(list(self._blocks), axis=0)`) then mono-ized the result --
    which raises `ValueError` on that shape mismatch, an uncaught exception
    inside a continuously-running Qt timer slot. `_on_tick()` must mono-ize
    each block individually BEFORE concatenating so 1-D arrays concatenate
    fine regardless of source channel count."""
    fake = {}

    def factory(on_block):
        cap = _FakeCapture(on_block)
        fake["cap"] = cap
        return cap

    ctrl = SpectrumController(POINTS, sample_rate=FS, capture_factory=factory)
    ctrl.start()

    stereo_block = tones.generate_sine(1000.0, 1024 / FS, -20.0, FS, fade_ms=0)  # (1024, 2)
    mono_block = stereo_block[:, 0].copy()  # (1024,) -- differing ndim/shape

    fake["cap"].on_block(stereo_block, FS)
    fake["cap"].on_block(mono_block, FS)

    captured = {}
    ctrl.spectrum_ready.connect(lambda s: captured.__setitem__("spectrum", s))
    ctrl.level_ready.connect(lambda lv: captured.__setitem__("level", lv))

    ctrl._on_tick()  # must not raise

    assert captured.get("spectrum") is not None, "spectrum_ready never fired"
    assert captured.get("level") is not None and captured["level"] > -30.0, (
        "peak should still read the loud (-20 dBFS) tone despite the mixed shapes")

    ctrl.stop()


def test_broadband_offset_uniform_boost():
    # A flat +6 dB EQ boost applied uniformly across the whole spectrum
    # means the true pre-EQ signal was uniformly 6 dB quieter than what's
    # measured now -- the broadband offset should read exactly -6 dB, not
    # an approximation, since every bin agrees on the same number.
    measured = np.full(64, -20.0)
    response = np.full(64, 6.0)
    offset = broadband_offset_db(measured, response)
    assert abs(offset - (-6.0)) < 1e-9, offset

def test_broadband_offset_zero_response():
    # No EQ effect at all (flat 0 dB response) -> input reads identical to
    # output, regardless of how uneven the measured spectrum's own shape is.
    measured = np.array([-10.0, -30.0, -50.0, -5.0])
    response = np.zeros_like(measured)
    offset = broadband_offset_db(measured, response)
    assert abs(offset) < 1e-9, offset

def test_broadband_offset_no_power_is_safe():
    # Defensive edge case (an empty measured spectrum carries zero power) --
    # must return 0.0 rather than dividing by zero / raising.
    offset = broadband_offset_db(np.array([]), np.array([]))
    assert offset == 0.0, offset

def test_broadband_offset_weights_by_actual_energy():
    # Two bins: one loud, one at the floor. A boost applied ONLY to the
    # quiet bin should barely move the broadband offset (that bin carries
    # almost none of the signal's actual power), while the same boost
    # applied to the LOUD bin should move it close to the full boost.
    measured = np.array([-6.0, -100.0])
    boost_quiet = np.array([0.0, 40.0])
    boost_loud = np.array([40.0, 0.0])
    offset_quiet_boosted = broadband_offset_db(measured, boost_quiet)
    offset_loud_boosted = broadband_offset_db(measured, boost_loud)
    assert abs(offset_quiet_boosted) < 0.5, offset_quiet_boosted
    assert abs(offset_loud_boosted - (-40.0)) < 0.5, offset_loud_boosted


if __name__ == "__main__":
    for n, fn in sorted(globals().items()):
        if n.startswith("test_"): fn(); print("PASS", n)
