"""Self-test: frequency-dependent adaptive smoothing + soft no-boost-above-
~8kHz guardrail for auto-generated speaker corrections (Rumble 2.2 / 2.3).

octave_smooth_adaptive() widens its averaging window above AutoEQ's own
published treble-transition band (6-8 kHz, autoeq/constants.py) and stays
narrow below it -- manual GEQ/PEQ editing keeps using the flat
octave_smooth() unchanged. make_correction(adaptive_smoothing=True) also
tapers any boost above that band toward zero, folding the taper into the
existing ClampSummary (Rumble 1.4) instead of a second notice."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core import eq_target
from rumble.core.eq_target import ClampSummary

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── octave_smooth_adaptive: narrow below 6 kHz, wide above 8 kHz ─────────
# A dense log-spaced curve with a single 10 dB spike at 1 kHz and another at
# 7 kHz, everywhere else flat -- lets us compare how much each spike gets
# smeared into its neighbours under the narrow vs. the wide window.
import math

grid = eq_target.log_grid(20.0, 20000.0, 48)
spiky = [(f, 0.0) for f in grid]


def with_spike(curve, at_hz, gain_db):
    out = []
    for f, g in curve:
        out.append((f, gain_db if abs(math.log2(f / at_hz)) < 1e-6 else g))
    return out


def nearest_freq(freqs, hz):
    return freqs[min(range(len(freqs)), key=lambda i: abs(freqs[i] - hz))]


mid_curve = with_spike(spiky, nearest_freq(grid, 1000.0), 10.0)
treble_curve = with_spike(spiky, nearest_freq(grid, 7000.0), 10.0)

smoothed_mid = eq_target.octave_smooth_adaptive(mid_curve)
smoothed_treble = eq_target.octave_smooth_adaptive(treble_curve)

mid_idx = min(range(len(grid)), key=lambda i: abs(grid[i] - 1000.0))
treble_idx = min(range(len(grid)), key=lambda i: abs(grid[i] - 7000.0))

mid_val = smoothed_mid[mid_idx][1]
treble_val = smoothed_treble[treble_idx][1]
check(treble_val < 1.0,
      "a spike above the 6-8 kHz breakpoint is smeared out by the wide treble window")
check(mid_val > 3 * treble_val,
      "the narrow window below 6 kHz preserves far more of an equivalent spike "
      "than the wide treble window does (mid={:.3f}, treble={:.3f})".format(mid_val, treble_val))

# ── the adaptive window itself widens monotonically across 6-8 kHz ───────
below = eq_target._log_lerp(5000.0, eq_target.ADAPTIVE_SMOOTH_F_LOWER,
                            eq_target.ADAPTIVE_SMOOTH_F_UPPER,
                            eq_target.ADAPTIVE_SMOOTH_WINDOW_OCT,
                            eq_target.ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT)
mid_transition = eq_target._log_lerp(7000.0, eq_target.ADAPTIVE_SMOOTH_F_LOWER,
                                     eq_target.ADAPTIVE_SMOOTH_F_UPPER,
                                     eq_target.ADAPTIVE_SMOOTH_WINDOW_OCT,
                                     eq_target.ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT)
above = eq_target._log_lerp(9000.0, eq_target.ADAPTIVE_SMOOTH_F_LOWER,
                            eq_target.ADAPTIVE_SMOOTH_F_UPPER,
                            eq_target.ADAPTIVE_SMOOTH_WINDOW_OCT,
                            eq_target.ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT)
check(below == eq_target.ADAPTIVE_SMOOTH_WINDOW_OCT,
      "below the 6 kHz breakpoint the window equals AutoEQ's narrow default (1/12 oct)")
check(above == eq_target.ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT,
      "above the 8 kHz breakpoint the window equals AutoEQ's treble default (2.0 oct)")
check(below < mid_transition < above,
      "the window widens monotonically through the 6-8 kHz transition band")

# manual/flat octave_smooth() must stay exactly as-is (no frequency dependence)
flat = eq_target.octave_smooth(mid_curve, 1 / 3)
check(flat is not smoothed_mid, "octave_smooth_adaptive is a distinct function from octave_smooth")

# ── make_correction(adaptive_smoothing=True): boost above 8 kHz tapers
#    toward zero, folded into the existing ClampSummary ──────────────────
# A flat measured curve 10 dB below a flat 0 dB target everywhere -- without
# any guardrail every point would want a full boost, capped by max_boost_db.
flat_low_measured = [(f, -10.0) for f in eq_target.log_grid(20.0, 20000.0, 12)]

corr_plain, clamp_plain = eq_target.make_correction(
    flat_low_measured, slope_db_per_oct=0.0, smooth_oct=0.0,
    max_boost_db=10.0, max_cut_db=12.0, band=(20.0, 20000.0),
    adaptive_smoothing=False)
corr_guarded, clamp_guarded = eq_target.make_correction(
    flat_low_measured, slope_db_per_oct=0.0, smooth_oct=0.0,
    max_boost_db=10.0, max_cut_db=12.0, band=(20.0, 20000.0),
    adaptive_smoothing=True)


def gain_at(curve, hz):
    idx = min(range(len(curve)), key=lambda i: abs(curve[i][0] - hz))
    return curve[idx][1]


check(gain_at(corr_plain, 10000.0) > 9.0,
      "without the guardrail, treble boost is capped only by max_boost_db")
check(gain_at(corr_guarded, 10000.0) < 1.0,
      "with adaptive_smoothing on, boost above 8 kHz is tapered toward zero")
check(gain_at(corr_guarded, 100.0) > 9.0,
      "the guardrail leaves low-frequency boost untouched")
check(clamp_guarded.points_clamped_boost > clamp_plain.points_clamped_boost,
      "the hf guardrail's taper is folded into the existing ClampSummary, not a second mechanism")
check(isinstance(clamp_guarded, ClampSummary), "make_correction still returns a ClampSummary")

# cuts are never tapered by the guardrail (only boost)
cut_measured = [(f, 10.0) for f in eq_target.log_grid(20.0, 20000.0, 12)]
corr_cut, _clamp_cut = eq_target.make_correction(
    cut_measured, slope_db_per_oct=0.0, smooth_oct=0.0,
    max_boost_db=10.0, max_cut_db=12.0, band=(20.0, 20000.0),
    adaptive_smoothing=True)
check(gain_at(corr_cut, 10000.0) < -9.0,
      "the guardrail only tapers boost, never cut, even above 8 kHz")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
