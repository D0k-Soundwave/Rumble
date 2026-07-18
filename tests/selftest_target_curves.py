"""Self-test: Rumble 3.4 — a real, named target-curve library (Harman
2018/2019, diffuse-field) plus eq_target.make_correction()'s ability to
target one directly instead of only a linear tilt. Spot-check values below
were independently verified against AutoEQ's own bundled CSVs
(targets/Harman over-ear 2018.csv etc., commit 6c9a097) before being written
here — not invented."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core import target_curves, eq_target
from rumble.core.autoeq_merge import interp_log

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── named curves exist and carry real, cited data ─────────────────────────
check(set(target_curves.names())
      == {"harman_2018_over_ear", "harman_2019_in_ear", "diffuse_field"},
      "exactly the three researched curves are registered")

for name in target_curves.names():
    curve = target_curves.get_curve(name)
    check(len(curve) > 20, f"{name}: non-empty, real curve (>20 points)")
    freqs = [f for f, _ in curve]
    check(freqs == sorted(freqs), f"{name}: points are frequency-sorted")
    check(freqs[0] <= 21 and freqs[-1] >= 19000,
          f"{name}: spans the audible band (20 Hz - 20 kHz)")

# Spot-check against AutoEQ's real published data (verified independently
# during research, see module docstring for the exact source/commit).
harman_over_ear = dict(target_curves.get_curve("harman_2018_over_ear"))
near_3k = min((f for f in harman_over_ear if 2900 <= f <= 3300), default=None)
check(near_3k is not None and 7.5 <= harman_over_ear[near_3k] <= 9.5,
      "Harman 2018 over-ear has its well-documented ~8 dB pinna-gain peak near 3 kHz")

harman_in_ear = dict(target_curves.get_curve("harman_2019_in_ear"))
near_3k_ie = min((f for f in harman_in_ear if 2900 <= f <= 3300), default=None)
check(near_3k_ie is not None and 8.5 <= harman_in_ear[near_3k_ie] <= 10.0,
      "Harman 2019 in-ear has its documented ~9 dB peak near 3 kHz")

diffuse = dict(target_curves.get_curve("diffuse_field"))
near_100 = min((f for f in diffuse if 90 <= f <= 115), default=None)
check(near_100 is not None and -5.0 <= diffuse[near_100] <= -3.0,
      "diffuse-field curve has its documented ~-4 dB bass-region level (relative to 1 kHz)")

# ── device-type defaults / applicability ──────────────────────────────────
check(target_curves.default_for_device_type("Over-ear") == "harman_2018_over_ear",
      "over-ear headphones default to Harman 2018")
check(target_curves.default_for_device_type("In-ear") == "harman_2019_in_ear",
      "in-ear headphones default to Harman 2019")
check(target_curves.default_for_device_type("Speaker") is None,
      "speakers have no named-curve default — they keep the existing linear tilt")
check(target_curves.applies_to("diffuse_field", "Speaker"),
      "diffuse-field is offered as an explicit opt-in for speakers too")
check(not target_curves.applies_to("harman_2018_over_ear", "Speaker"),
      "a headphone preference curve is never offered for speakers")

speaker_opts = target_curves.options_for_device_type("Speaker")
check(speaker_opts == [("Diffuse-field (5128 rig)", "diffuse_field")],
      "speakers are only offered diffuse-field, not the Harman headphone curves")

# ── make_correction() can target a named curve instead of a linear tilt ──
flat_measured = [(20.0, 0.0), (1000.0, 0.0), (20000.0, 0.0)]
corr_tilt, _ = eq_target.make_correction(flat_measured, slope_db_per_oct=0.0, smooth_oct=0.0)
corr_curve, _ = eq_target.make_correction(
    flat_measured, target_curve=target_curves.get_curve("harman_2018_over_ear"),
    smooth_oct=0.0, max_boost_db=20.0, max_cut_db=30.0, band=(10.0, 25000.0))

check(corr_tilt != corr_curve,
      "targeting a named curve produces a different correction than the flat/tilt default")

grid = [f for f, _ in corr_curve]
expected = interp_log(target_curves.get_curve("harman_2018_over_ear"), grid)
actual = [g for _, g in corr_curve]
check(all(abs(a - e) < 1e-6 for a, e in zip(actual, expected)),
      "against a flat (0 dB) measured curve, the correction exactly reproduces the "
      "named target curve, point for point")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
