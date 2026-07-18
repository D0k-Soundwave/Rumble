"""Self-test: autoeq_merge.compute_preamp() and preamp_calc.calculate_auto_preamp()
must agree on the same anti-clipping margin table (Rumble 1.3). Before this
fix, compute_preamp() used its own flat 0.5 dB margin regardless of the
underlying filter set, while calculate_auto_preamp() already escalated to
1.0 dB either on a high combined peak (>6 dB) or on 3+ simultaneous sizeable
(>=4 dB) boosts — so an AutoEQ/Spinorama import (compute_preamp) and a
manually-built profile with an equivalent response (calculate_auto_preamp)
could receive different preamp values for the identical response. Now
compute_preamp() delegates to calculate_auto_preamp(), so both paths use the
identical margin logic."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rumble.core import preamp_calc
from rumble.core.apo_config import Filter
from rumble.core.autoeq_merge import compute_preamp, format_graphiceq

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


SR = 48000

# ── the exact case that used to diverge: 3+ simultaneous boosts >= 4 dB ──
# Narrow, well-separated peaks so the *combined* steady-state response peak
# stays at or below 6 dB — this isolates the boost-count escalation branch
# (docs/RUMBLE_Headroom_Maths.md Step 4) from the separate >6 dB branch, so
# the test actually exercises the formula that used to be skipped.
three_boosts = [
    Filter(type="PK", fc=100.0, gain=4.5, q=4.0),
    Filter(type="PK", fc=1000.0, gain=4.5, q=4.0),
    Filter(type="PK", fc=8000.0, gain=4.5, q=4.0),
]
peak_db = -preamp_calc.calculate_headroom(three_boosts, preamp_db=0.0, sample_rate=SR)
check(peak_db <= 6.0 + 1e-6,
      f"fixture's combined peak ({peak_db:.2f} dB) stays <= 6 dB, isolating the "
      "boost-count escalation branch")

direct = preamp_calc.calculate_auto_preamp(three_boosts, sample_rate=SR)
via_curve = compute_preamp([], sample_rate=SR, filters=three_boosts)
check(direct == via_curve,
      "compute_preamp(filters=...) matches calculate_auto_preamp(...) exactly "
      "for a filter set with 3+ boosts >= 4 dB")

import math
escalated_expected = math.floor(-(peak_db + 1.0) * 10) / 10
check(direct == escalated_expected,
      "the 3+ sizeable-boost case actually escalates to the 1.0 dB margin "
      "(not the flat 0.5 dB one)")

# ── the common real-world path: a pure GraphicEQ curve, no filters ────────
# AutoEQ imports/merges are GraphicEQ curves with no discrete parametric
# filters at all — compute_preamp's primary caller. A high-peak curve (> 6
# dB) must still escalate the margin exactly like calculate_auto_preamp does
# for an equivalent geq_string.
high_curve = [(20.0, 0.0), (1000.0, 8.0), (20000.0, 0.0)]
geq = format_graphiceq(high_curve)
direct_curve = preamp_calc.calculate_auto_preamp([], sample_rate=SR, geq_string=geq)
via_compute = compute_preamp(high_curve, sample_rate=SR)
check(direct_curve == via_compute,
      "compute_preamp(curve) matches calculate_auto_preamp(geq_string=...) "
      "exactly for a high-peak (>6 dB) pure GraphicEQ curve")
check(via_compute < 0, "a high-peak curve still produces a negative preamp")

# ── backward compatibility: single positional curve arg still works ──────
flat_curve = [(20.0, 0.0), (20000.0, 0.0)]
check(compute_preamp(flat_curve) == 0.0,
      "a flat (never-boosting) curve with no filters/sample_rate args still "
      "returns 0.0 preamp (existing call sites unaffected)")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
