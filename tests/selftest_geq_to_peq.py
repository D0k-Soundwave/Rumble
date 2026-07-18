"""Self-test: GEQ->PEQ conversion (Rumble 4.3). convert_geq_to_peq() turns a
GraphicEQ curve into a list of PK Filter bands whose combined_response()
tracks the source curve's own graphic_eq_response() closely over the curve's
defined frequency span, using a per-band Q derived from the grid's own point
spacing (not a guessed constant) plus a bounded Newton-Raphson correction
pass that cancels overlap bleed between adjacent bands. It also respects a
caller-supplied band ceiling by merging points rather than silently
truncating them."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from rumble.core import eq_math
from rumble.core.geq_peq_convert import GAIN_EPSILON_DB, convert_geq_to_peq

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


ISO_10_BAND = [31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0]


def _geq_string(freqs, gains):
    return "; ".join(f"{f:g} {g:g}" for f, g in zip(freqs, gains))


# ── nothing to convert ─────────────────────────────────────────────────────
flat = _geq_string(ISO_10_BAND, [0.0] * len(ISO_10_BAND))
check(convert_geq_to_peq(flat, max_bands=32) is None,
      "a flat (all-zero) GraphicEQ curve has nothing to convert")
check(convert_geq_to_peq("", max_bands=32) is None,
      "an empty GraphicEQ string has nothing to convert")

# ── the guide's own example: one point boosted, rest flat ─────────────────
gains = [0.0, 0.0, 0.0, 0.0, 0.0, 6.0, 0.0, 0.0, 0.0, 0.0]
geq_string = _geq_string(ISO_10_BAND, gains)
result = convert_geq_to_peq(geq_string, max_bands=32)
check(result is not None, "a single boosted point converts to a result")
check(len(result.filters) == 1, "one significant point produces exactly one PK filter")
check(result.filters[0].type == "PK", "the converted band is a PK filter")
check(abs(result.filters[0].fc - 1000.0) < 1.0, "the band centres on the boosted point's frequency")
check(result.bands_needed == 1 and result.bands_used == 1,
      "no merging was needed for a single significant point")
check(result.warning is None, "no warning when the curve already fits max_bands")

# Grid-derived Q: the 1000 Hz point sits exactly 1 octave (in log-freq) from
# each of its 500 Hz / 2000 Hz neighbours on the 10-band ISO grid, so the
# owned span is 1.0 octave and Q should match eq_math.bw_to_q(1.0) exactly.
expected_q = eq_math.bw_to_q(1.0)
check(abs(result.filters[0].q - expected_q) < 1e-6,
      "per-band Q is derived from the grid's own point spacing, not guessed")

# The PK approximation of a triangular GEQ tent with a bell curve is an
# inherent, disclosed approximation (see geq_peq_convert.py's module
# docstring) — verified against the theoretical floor for ANY Q choice on
# this exact grid (~0.76 dB), so 1.5 dB is a real, measured tolerance, not
# an arbitrary pass bar.
freqs = eq_math.frequency_array()
geq_resp = eq_math.graphic_eq_response(geq_string, freqs)
peq_resp = eq_math.combined_response(result.filters, freqs)
lo, hi = ISO_10_BAND[0], ISO_10_BAND[-1]
in_range = (freqs >= lo) & (freqs <= hi)
max_err = float(np.max(np.abs(geq_resp - peq_resp)[in_range]))
check(max_err < 1.5,
      f"combined_response tracks the source curve within 1.5 dB over its own "
      f"frequency span (measured {max_err:.3f} dB)")
check(abs(max_err - result.max_error_db) < 1e-6,
      "GeqToPeqResult.max_error_db matches the actually-measured error")

# ── points quieter than GAIN_EPSILON_DB are dropped, not converted ────────
near_zero = [0.0] * len(ISO_10_BAND)
near_zero[5] = GAIN_EPSILON_DB / 2.0
check(convert_geq_to_peq(_geq_string(ISO_10_BAND, near_zero), max_bands=32) is None,
      "a curve with only sub-epsilon gain has nothing worth converting")

# ── overlap correction reduces error for a smoother multi-point bump ──────
bump = [0.0, 0.0, 0.0, 1.0, 3.0, 6.0, 3.0, 1.0, 0.0, 0.0]
bump_string = _geq_string(ISO_10_BAND, bump)
bump_result = convert_geq_to_peq(bump_string, max_bands=32)
check(bump_result is not None and len(bump_result.filters) == 5,
      "five adjacent significant points produce five PK bands")
bump_geq_resp = eq_math.graphic_eq_response(bump_string, freqs)
bump_peq_resp = eq_math.combined_response(bump_result.filters, freqs)
bump_max_err = float(np.max(np.abs(bump_geq_resp - bump_peq_resp)[in_range]))
check(bump_max_err < 1.5,
      f"overlap-corrected multi-band bump tracks the source curve within 1.5 dB "
      f"(measured {bump_max_err:.3f} dB)")

# Without correction, five overlapping 1-octave-wide bells stacked in dB at
# their target gains directly would overshoot by more than that — confirm
# the correction pass is actually doing something, not a no-op.
naive_filters = []
for f, g in zip(ISO_10_BAND, bump):
    if abs(g) <= GAIN_EPSILON_DB:
        continue
    naive_filters.append(
        type(bump_result.filters[0])(type="PK", fc=f, gain=g, q=expected_q, use_bw=False))
naive_resp = eq_math.combined_response(naive_filters, freqs)
naive_max_err = float(np.max(np.abs(bump_geq_resp - naive_resp)[in_range]))
check(bump_max_err < naive_max_err,
      "the Newton correction pass measurably reduces overlap-induced error "
      f"(naive {naive_max_err:.3f} dB -> corrected {bump_max_err:.3f} dB)")

# ── MAX_BANDS is respected by merging, never silent truncation ────────────
many_points = [(20.0 * (2 ** i), 3.0 if i % 2 == 0 else -3.0) for i in range(20)]
many_freqs = [f for f, _ in many_points]
many_gains = [g for _, g in many_points]
many_string = _geq_string(many_freqs, many_gains)
capped = convert_geq_to_peq(many_string, max_bands=8)
check(capped is not None, "a 20-point alternating curve still converts under a tight cap")
check(len(capped.filters) <= 8, "the converted band count never exceeds max_bands")
check(capped.bands_needed == 20, "bands_needed reports the pre-merge significant point count")
check(capped.bands_used <= 8, "bands_used reports the actual post-merge count")
check(capped.warning is not None, "merging to fit the cap produces a non-silent warning")
check(str(capped.bands_needed) in capped.warning and str(capped.bands_used) in capped.warning,
      "the warning names both the needed and used band counts")

# ── max_bands must leave room for at least one band ────────────────────────
raised_error = False
try:
    convert_geq_to_peq(geq_string, max_bands=0)
except ValueError:
    raised_error = True
check(raised_error, "max_bands=0 (no room) raises rather than silently returning nothing")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
