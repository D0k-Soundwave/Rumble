"""Self-test: Rumble 3.7 — measured-vs-calculated drift detection. Extends
the live spectrum analyzer's existing "measured vs. calculated" comparison
(SpectrumController's clipping detector, which only compares a measured
scalar peak level against a fixed reference) to the general case: does the
measured spectrum's SHAPE track the calculated EQ response's shape at all?
A competing driver-level DSP, a muted/bypassed EQ, or the wrong output
device selected would all break that relationship without necessarily
pushing the signal near 0 dBFS, so nothing currently catches them.

rumble.core.spectrum.drift_correlation()/drift_expected() do the actual
correlation/significance math (Pearson r + Fisher's r-to-z significance
test — see spectrum.py's module comment for the full derivation);
SpectrumController._update_drift() pools that per-tick judgment into the
same sustain/clear hysteresis shape as the existing clipping detector, and
emits drift_changed(bool)."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

from rumble.core.spectrum import drift_correlation, drift_expected, DRIFT_MIN_CALCULATED_RANGE_DB
from rumble.ui.spectrum_overlay import SpectrumController

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── drift_correlation() / drift_expected(): pure correlation math ────────
N = 200
rng = np.random.default_rng(20260710)
# A correction with real shape (well above DRIFT_MIN_CALCULATED_RANGE_DB's
# 3 dB gate): cut low, flat mid, boost high.
correction = np.concatenate([np.full(60, -6.0), np.full(80, 0.0), np.full(60, 6.0)])
# A deterministic "unknown program content" shape, unrelated in structure to
# `correction`'s block pattern (two incommensurate sine periods) -- fixed
# rather than freshly randomized per run, since a Pearson-correlation
# significance test is inherently probabilistic (a ~0.5% one-sided false-
# positive rate at the chosen 99% confidence bar is expected BEHAVIOR of the
# real algorithm, not a bug, so re-rolling random noise on every test run
# would occasionally and correctly flip a "no relationship" trial to
# "significant" and fail this suite for the right reason at the wrong
# layer -- a unit test should exercise the mechanism deterministically, not
# gamble on it).
idx = np.arange(N)
unrelated_source = 5.0 * np.sin(2 * np.pi * idx / 23.0) + 2.0 * np.cos(2 * np.pi * idx / 11.0)

check(drift_expected(drift_correlation(unrelated_source + correction, correction), N) is True,
      "measured = source + correction correlates as expected "
      "(the EQ genuinely being applied)")
check(drift_expected(drift_correlation(unrelated_source, correction), N) is False,
      "measured = source only (EQ not applied/bypassed) is never mistaken "
      "for a tracking correlation")
check(drift_expected(drift_correlation(unrelated_source - correction, correction), N) is False,
      "measured = source - correction (a competing DSP fighting the EQ) is "
      "flagged as not tracking, not as a positive match")

# ── flat calculated curve: nothing meaningful to compare, skip judging ───
flat_calculated = np.zeros(N)
check(drift_correlation(rng.normal(0, 8, N), flat_calculated) is None,
      "a calculated curve too close to flat (below DRIFT_MIN_CALCULATED_RANGE_DB) "
      "returns None rather than a meaningless near-zero correlation")
check(DRIFT_MIN_CALCULATED_RANGE_DB > 0, "sanity: the flatness gate is a real positive threshold")

# ── degenerate inputs ──────────────────────────────────────────────────
check(drift_correlation(np.zeros(2), np.zeros(2)) is None,
      "too few points (< 3) returns None rather than a garbage correlation")
check(drift_correlation(np.zeros(N), np.ones(N + 1)) is None,
      "mismatched shapes return None rather than raising")
check(drift_expected(None, N) is None,
      "drift_expected() passes through a None correlation as 'nothing to judge'")
check(drift_expected(0.9, 2) is None,
      "drift_expected() refuses to judge on too few frequency points (n < 4) "
      "even with a strong correlation")


# ── SpectrumController._update_drift(): sustain/clear hysteresis ─────────
class _FakeCapture:
    """Minimal LoopbackCapture-shaped fake — matches selftest_spectrum.py's
    own fake. Not driven through _on_tick()/blocks here since these tests
    exercise _update_drift() directly with hand-built synthetic spectra."""

    def __init__(self, on_block):
        self.on_block = on_block
        self.status = "stopped"

    def start(self, device_name_fragment=None):
        self.status = "running"

    def stop(self):
        self.status = "stopped"


def _factory(on_block):
    return _FakeCapture(on_block)


tracking_measured = correction + unrelated_source  # tracks the correction (plus content on top)
not_tracking_measured = unrelated_source  # unrelated to the correction (EQ not applied)


def test_no_response_set_never_fires():
    ctrl = SpectrumController(np.ones(N), sample_rate=48000, capture_factory=_factory)
    seen = []
    ctrl.drift_changed.connect(seen.append)
    ctrl.start()
    for _ in range(200):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [], "drift_changed never fires when set_calculated_response() was never called")
    ctrl.stop()


def test_flat_curve_never_fires():
    ctrl = SpectrumController(np.ones(N), sample_rate=48000, capture_factory=_factory)
    ctrl.set_calculated_response(flat_calculated)
    seen = []
    ctrl.drift_changed.connect(seen.append)
    ctrl.start()
    for _ in range(200):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [], "drift_changed never fires when the calculated curve is too flat to judge")
    ctrl.stop()


def test_sets_only_after_sustained_ticks_not_before():
    ctrl = SpectrumController(
        np.ones(N), sample_rate=48000, capture_factory=_factory, drift_sustain_s=0.1)
    ctrl.set_calculated_response(correction)
    seen = []
    ctrl.drift_changed.connect(seen.append)
    ctrl.start()

    # Fewer than the sustain window's worth of ticks: must not fire yet.
    for _ in range(2):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [], f"must not flip True before the sustain window elapses, got {seen}")

    # Enough ticks (33ms each) to clear a 0.1s sustain window.
    for _ in range(6):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [True], f"expected a single True flip once the sustain window elapses, got {seen}")

    ctrl.stop()


def test_clears_after_sustained_tracking_again():
    ctrl = SpectrumController(
        np.ones(N), sample_rate=48000, capture_factory=_factory,
        drift_sustain_s=0.1, drift_clear_s=0.1)
    ctrl.set_calculated_response(correction)
    seen = []
    ctrl.drift_changed.connect(seen.append)
    ctrl.start()

    for _ in range(6):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [True], f"expected the flag to set first, got {seen}")

    for _ in range(6):
        ctrl._update_drift(tracking_measured)
    check(seen == [True, False], f"expected the flag to clear after sustained tracking resumes, got {seen}")

    ctrl.stop()


def test_stop_clears_an_active_drift_flag():
    """Mirrors the existing clipping regression test: stop() must reset AND
    emit the sustained flag, or it goes stale the same way clipping's did
    before that fix — a caller that stops while drifting, restarts, and only
    ever sees tracking ticks afterward would otherwise never see a
    True->False edge to emit."""
    ctrl = SpectrumController(
        np.ones(N), sample_rate=48000, capture_factory=_factory, drift_sustain_s=0.1)
    ctrl.set_calculated_response(correction)
    seen = []
    ctrl.drift_changed.connect(seen.append)
    ctrl.start()

    for _ in range(6):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [True], f"expected the flag to set first, got {seen}")

    ctrl.stop()
    check(seen == [True, False], f"stop() must emit the False transition, got {seen}")


def test_flat_ticks_do_not_reset_sustain_progress():
    """A tick where the calculated curve happens to be flat (nothing to
    judge) must not erase progress already made toward flagging drift --
    it's not evidence the EQ is working, so it shouldn't reset the count."""
    ctrl = SpectrumController(
        np.ones(N), sample_rate=48000, capture_factory=_factory, drift_sustain_s=0.1)
    seen = []
    ctrl.drift_changed.connect(seen.append)
    ctrl.start()

    ctrl.set_calculated_response(correction)
    for _ in range(3):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [], "not yet enough sustained ticks to flip")

    # A run of flat-curve ticks (nothing to judge) interleaved in the middle.
    ctrl.set_calculated_response(flat_calculated)
    for _ in range(3):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [], "still nothing flipped -- flat ticks are skipped, not counted either way")

    ctrl.set_calculated_response(correction)
    for _ in range(3):
        ctrl._update_drift(not_tracking_measured)
    check(seen == [True],
          "resuming judgeable ticks continues accumulating toward the sustain "
          "window rather than restarting from zero")

    ctrl.stop()


for name, fn in sorted(globals().items()):
    if name.startswith("test_"):
        fn()

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
