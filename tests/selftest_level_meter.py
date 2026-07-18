"""Self-test: per-channel metering primitives (rumble/core/audio/metering.py)
and the stereo VU/Peak meter ballistics (rumble/core/level_meter.py).
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from rumble.core import level_meter
from rumble.core.audio import metering

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── metering.py: per-channel primitives ───────────────────────────────────

stereo_block = np.zeros((1000, 2), dtype=np.float64)
stereo_block[:, 0] = 0.5   # L: -6.02 dBFS
stereo_block[:, 1] = 0.25  # R: -12.04 dBFS

peaks = metering.peak_dbfs_per_channel(stereo_block)
check(peaks.shape == (2,), "peak_dbfs_per_channel preserves the channel axis")
check(abs(peaks[0] - 20 * math.log10(0.5)) < 1e-6,
      "L channel peak matches its own amplitude, not combined with R")
check(abs(peaks[1] - 20 * math.log10(0.25)) < 1e-6,
      "R channel peak matches its own amplitude, not combined with L")

rms = metering.rms_dbfs_per_channel(stereo_block)
check(abs(rms[0] - 20 * math.log10(0.5)) < 1e-6,
      "constant-amplitude block: per-channel RMS equals peak")

empty = np.zeros((0, 2))
check(np.all(metering.peak_dbfs_per_channel(empty) == metering.FLOOR_DBFS),
      "empty block floors both channels")
check(np.all(metering.rms_dbfs_per_channel(empty) == metering.FLOOR_DBFS),
      "empty block floors both channels (RMS)")

try:
    metering.peak_dbfs_per_channel(np.zeros(100))
    check(False, "1-D block raises ValueError (shape-strict, no guessing)")
except ValueError:
    check(True, "1-D block raises ValueError (shape-strict, no guessing)")

levels = metering.stereo_levels(stereo_block)
check(isinstance(levels, metering.StereoLevels), "stereo_levels returns a StereoLevels")
check(levels.peak_l > levels.peak_r, "louder L channel reads a higher peak_l than peak_r")
check(levels.rms_l > levels.rms_r, "louder L channel reads a higher rms_l than rms_r")

# ── level_meter.py: derived ballistics constants ──────────────────────────

expected_tau = 0.300 / math.log(100)
expected_vu_coef = 1.0 - math.exp(-level_meter._TICK_INTERVAL_S / expected_tau)
check(abs(level_meter.VU_ATTACK - expected_vu_coef) < 1e-12,
      "VU_ATTACK matches the ANSI C16.5-1942 / IEC 60268-17 derivation")
check(level_meter.VU_ATTACK == level_meter.VU_RELEASE,
      "VU ballistics are symmetric (attack == release), unlike RTA fast/slow")
check(abs(level_meter.PEAK_DECAY_DB_PER_TICK - 20.0 * level_meter._TICK_INTERVAL_S) < 1e-12,
      "Peak decay rate matches the derived 20 dB/s * tick_interval")

# ── level_meter.py: VU mode reaches ~99% of a step after 300ms of ticks ───

meter = level_meter.LevelMeter(mode="vu")
n_ticks = round(0.300 / level_meter._TICK_INTERVAL_S)
target = metering.StereoLevels(peak_l=-6.0, peak_r=-6.0, rms_l=-6.0, rms_r=-6.0)
for _ in range(n_ticks):
    meter.update(target)
step_size = target.rms_l - level_meter.FLOOR_DBFS
reached_frac = (meter.bar_l - level_meter.FLOOR_DBFS) / step_size
check(abs(reached_frac - 0.99) < 0.01,
      f"VU mode reaches ~99% of a step after 300ms of ticks (reached {reached_frac:.4f})")
check(meter.hold_l is None and meter.hold_r is None,
      "VU mode has no peak-hold segment")

# ── level_meter.py: Reference mode shares VU's ballistics exactly ────────

ref_meter = level_meter.LevelMeter(mode="ref")
for _ in range(n_ticks):
    ref_meter.update(target)
vu_meter_for_compare = level_meter.LevelMeter(mode="vu")
for _ in range(n_ticks):
    vu_meter_for_compare.update(target)
check(abs(ref_meter.bar_l - vu_meter_for_compare.bar_l) < 1e-12,
      "Reference mode's bar reaches the identical position as VU mode given the same input")
check(ref_meter.hold_l is None and ref_meter.hold_r is None,
      "Reference mode has no peak-hold segment (same as VU)")
check(set(level_meter.K_REFERENCE_DBFS) == {"k12", "k14", "k20"},
      "K_REFERENCE_DBFS defines exactly the three K-System levels")
check(level_meter.K_REFERENCE_DBFS["k12"] == -12.0
      and level_meter.K_REFERENCE_DBFS["k14"] == -14.0
      and level_meter.K_REFERENCE_DBFS["k20"] == -20.0,
      "K-System reference levels match their named dBFS points")

# ── level_meter.py: Peak mode is instant-attack, linear decay ────────────

meter.set_mode("peak")
check(meter.bar_l == level_meter.FLOOR_DBFS, "set_mode resets state to the floor")
check(meter.hold_l == level_meter.FLOOR_DBFS, "Peak mode starts its hold cap at the floor")

spike = metering.StereoLevels(peak_l=-3.0, peak_r=-20.0, rms_l=-10.0, rms_r=-25.0)
meter.update(spike)
check(meter.bar_l == -3.0, "Peak mode's bar reads the tick's measured peak directly (instant attack)")
check(meter.hold_l == -3.0, "Peak mode's hold cap jumps to a new higher peak")

silence = metering.StereoLevels(peak_l=level_meter.FLOOR_DBFS, peak_r=level_meter.FLOOR_DBFS,
                                 rms_l=level_meter.FLOOR_DBFS, rms_r=level_meter.FLOOR_DBFS)
prev_bar, prev_hold = meter.bar_l, meter.hold_l
meter.update(silence)
check(abs((prev_bar - meter.bar_l) - level_meter.PEAK_DECAY_DB_PER_TICK) < 1e-9,
      "Peak mode's bar decays linearly at the derived rate, not exponentially")
check(abs((prev_hold - meter.hold_l) - level_meter.HOLD_DECAY_DB_PER_TICK) < 1e-9,
      "Peak mode's hold cap decays at the same rate as the bar")

meter.reset()
check(meter.bar_l == level_meter.FLOOR_DBFS and meter.bar_r == level_meter.FLOOR_DBFS,
      "reset() returns both bars to the floor")

# ── clip flag ──────────────────────────────────────────────────────────────

meter.set_mode("peak")
clip_levels = metering.StereoLevels(peak_l=0.0, peak_r=level_meter.FLOOR_DBFS,
                                     rms_l=0.0, rms_r=level_meter.FLOOR_DBFS)
meter.update(clip_levels)
check(meter.clipping == (True, False), "clip flag fires per-channel at/above the threshold")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("All level-meter checks passed.")
