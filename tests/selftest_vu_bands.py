"""Self-test: the VU-bars band aggregation (rumble/core/spectrum.py).

third_octave_bands() lays out ISO 1/3-octave bands; band_levels() folds a
fine-resolution spectrum onto them by taking each band's PEAK bin, which is how
a spectrum-analyzer bar behaves (a narrow tone lights its whole band).
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from rumble.core import eq_math, spectrum

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


centres, lo, hi = spectrum.third_octave_bands(20.0, 20000.0)

# ── band grid is the ISO 1/3-octave ladder ────────────────────────────────
check(len(centres) == 31,
      f"20 Hz-20 kHz spans 31 one-third-octave bands ({len(centres)})")
check(abs(centres[np.argmin(np.abs(centres - 1000))] - 1000.0) < 1.0,
      "1 kHz is a band centre (the ISO reference frequency)")
ratios = centres[1:] / centres[:-1]
check(np.allclose(ratios, 2 ** (1 / 3), rtol=1e-6),
      "adjacent centres are one-third-octave apart (ratio 2^(1/3))")
check(np.allclose(hi[:-1], lo[1:], rtol=1e-9),
      "each band's upper edge meets the next band's lower edge exactly")
check(np.all(lo < centres) and np.all(centres < hi),
      "every centre sits inside its own edges")

# ── band_levels takes the PEAK bin in each band ───────────────────────────
freqs = eq_math.frequency_array()  # 512-pt log grid, 10-24000 Hz
floor = -120.0

# A single loud bin at 1 kHz must light the 1 kHz band and no distant band.
spec = np.full_like(freqs, floor)
k = int(np.argmin(np.abs(freqs - 1000.0)))
spec[k] = -6.0
levels = spectrum.band_levels(freqs, spec, lo, hi, floor_db=floor)
b1k = int(np.argmin(np.abs(centres - 1000.0)))
check(abs(levels[b1k] - (-6.0)) < 1e-9,
      "a -6 dB bin at 1 kHz drives the 1 kHz band to -6 dB (peak, not averaged)")
far = [i for i in range(len(centres)) if abs(math.log2(centres[i] / 1000.0)) > 1]
check(all(levels[i] == floor for i in far),
      "bands an octave away from the tone stay at the floor")

# Peak, not mean: a band with one hot bin and many quiet ones reads the hot one.
spec2 = np.full_like(freqs, -40.0)
in_1k = (freqs >= lo[b1k]) & (freqs < hi[b1k])
idxs = np.where(in_1k)[0]
check(len(idxs) >= 2, "the 1 kHz band contains several fine bins at this resolution")
spec2[idxs[0]] = -3.0
levels2 = spectrum.band_levels(freqs, spec2, lo, hi, floor_db=floor)
check(abs(levels2[b1k] - (-3.0)) < 1e-9,
      "a band reports its single hottest bin (-3), not the -40 average of its bins")

# ── always finite, one level per band ─────────────────────────────────────
check(levels.shape == centres.shape and np.all(np.isfinite(levels)),
      "band_levels returns one finite level per band")

# ── a real captured tone lands in the right band ──────────────────────────
# 1 kHz sine, magnitude spectrum peaking at 1 kHz -> 1 kHz VU band is highest.
mags = -60.0 - 12.0 * np.abs(np.log2(freqs / 1000.0))  # peak at 1 kHz, falls off
real = spectrum.band_levels(freqs, mags, lo, hi, floor_db=floor)
check(int(np.argmax(real)) == b1k,
      "the loudest VU band for a 1 kHz-centred spectrum is the 1 kHz band")

# ── VU grid derived from the EQ's OWN band centres ────────────────────────
# The meter must show one bar per EQ band, not a fixed ladder "because 31 are
# available". bands_from_centres lays bars out on the filter centre frequencies.
fcs = [60.0, 250.0, 1000.0, 4000.0, 12000.0]
c, blo, bhi = spectrum.bands_from_centres(fcs, 20.0, 20000.0)
check(len(c) == len(fcs),
      f"one VU band per EQ filter ({len(c)} bars for {len(fcs)} bands)")
check(np.allclose(c, sorted(fcs)),
      "each bar is centred on its filter's Fc")
check(np.allclose(bhi[:-1], blo[1:]),
      "adjacent EQ bars meet at their shared edge (no gaps or overlaps in ownership)")
check(np.all(blo < c) and np.all(c < bhi),
      "each centre sits inside its own edges")
mid = math.sqrt(250.0 * 1000.0)
check(abs(bhi[1] - mid) < 1e-6,
      "the edge between two bands is their log-frequency midpoint")

# One band -> one full-width bar; empty -> empty (caller falls back).
c1, l1, h1 = spectrum.bands_from_centres([700.0], 20.0, 20000.0)
check(len(c1) == 1 and l1[0] == 20.0 and h1[0] == 20000.0,
      "a single EQ band yields one bar spanning the whole range")
c0, l0, h0 = spectrum.bands_from_centres([], 20.0, 20000.0)
check(len(c0) == 0, "no EQ bands -> empty grid (the graph falls back to ISO bars)")

# Unsorted / duplicate centres are handled.
cu, _, _ = spectrum.bands_from_centres([1000.0, 100.0, 1000.0, 500.0])
check(list(cu) == [100.0, 500.0, 1000.0],
      "centres are sorted and de-duplicated")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("All VU-band checks passed.")
