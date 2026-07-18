"""Self-test: numeric-range philosophy for auto-generated content (Rumble
2.6). RUMBLE_ACTION_GUIDE.md #2.6's decision: keep the wide manual/expert UI
clamps unchanged (Fc/Gain/Q/Bandwidth in filter_row.py), but every
auto-generated curve must land within the tighter research-recommended gain
range (soft -12/+6 dB) — eq_target.make_correction() already enforced this
for speaker corrections (Rumble 1.4); this session adds the same enforcement
to autoeq_merge's gear-simulation delta curve, which previously had no gain
bound at all, via the new eq_target.clamp_gain() helper."""
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


# ── clamp_gain(): a curve within the default cap is untouched ────────────
mild = [(20.0, 0.0), (1000.0, 3.0), (20000.0, -5.0)]
clamped, summary = eq_target.clamp_gain(mild)
check(isinstance(summary, ClampSummary), "clamp_gain returns a ClampSummary")
check(clamped == mild, "a curve within the default +6/-12 dB cap is returned unchanged")
check(not summary.notable(), "an unclamped curve reports a non-notable summary")

# ── a gear-sim-style delta curve that wildly exceeds the default cap ─────
extreme = [(20.0, 15.0), (1000.0, 0.0), (20000.0, -25.0)]
clamped2, summary2 = eq_target.clamp_gain(extreme)
check(clamped2[0][1] == 6.0, "boost is capped at the default +6 dB")
check(clamped2[2][1] == -12.0, "cut is floored at the default -12 dB")
check(summary2.points_clamped_boost == 1 and summary2.points_clamped_cut == 1,
      "both a clamped boost point and a clamped cut point are counted")
check(summary2.max_clamped_delta_db > 0.5, "the reported overshoot is meaningful")
check(summary2.notable(), "a meaningfully clamped delta curve is notable")

# ── custom bounds are honored (same call signature as make_correction) ───
within_20 = [(20.0, 15.0), (1000.0, 0.0), (20000.0, -18.0)]
clamped3, summary3 = eq_target.clamp_gain(within_20, max_boost_db=20.0, max_cut_db=20.0)
check(clamped3 == within_20, "widening the bounds to the research hard limit (+-20 dB) stops clamping")
check(not summary3.notable(), "no clamping is reported once the curve fits inside the wider bound")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
