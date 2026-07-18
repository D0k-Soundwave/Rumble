"""Self-test: curve-derived profiles (AutoEQ/Spinorama imports, merges, gear
sims, REW and room-measurement corrections) are DECOMPOSED into editable PK
bands plus a residual GraphicEQ layer, instead of being stored as a bare
GraphicEQ curve the editor could only show as an empty band list.

The contract: the bands and the residual SUM back to the original curve (APO
applies both in series, so their dB responses add), to within the 0.1 dB the
config file rounds gains to. So the profile gains real bands with NO loss of
accuracy versus storing the curve alone.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from rumble.core import autoeq_merge, eq_math
from rumble.core.apo_config import MAX_BANDS, ChannelSection, Filter
from rumble.core.geq_peq_convert import convert_geq_to_peq, decompose_geq

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# The precision the GraphicEQ writer and Filter.to_line() round gains to; a
# reconstruction closer than this is exact as far as the written file can tell.
WRITE_PRECISION_DB = 0.1


# AutoEQ's own standard 127-point log grid.
FREQS = np.geomspace(20.0, 20000.0, 127)

# A REAL AutoEQ correction, fetched from the live index (AAW A2H V2) and pinned
# here. Synthetic sums-of-bells are too kind: this one is jagged, its grid is
# non-uniform in log-frequency (0.049-0.116 octaves between points), and it dives
# 15 dB in its top octave — which is exactly where a fitted band's response
# steepens toward Nyquist. It found a real bug (the residual grid refined
# uniformly, spending ~900 points spectrum-wide on an error that lived entirely
# above 12 kHz, and still missed the target at 0.173 dB), so it stays as the
# regression guard for anything that touches the residual grid.
REAL_AUTOEQ_CURVE = (
    "20 -10.8; 21 -10.9; 22 -11; 23 -11; 24 -10.9; 26 -10.9; 27 -10.9; 29 -10.9; 30 -10.8; 32 "
    "-10.7; 34 -10.7; 36 -10.6; 38 -10.6; 40 -10.5; 43 -10.5; 45 -10.4; 48 -10.4; 50 -10.2; "
    "53 -10.2; 56 -10.3; 59 -10.3; 63 -10.2; 66 -10.3; 70 -10.4; 74 -10.4; 78 -10.6; 83 "
    "-10.7; 87 -10.8; 92 -11; 97 -11.3; 103 -11.5; 109 -11.6; 115 -11.8; 121 -12; 128 -12.1; "
    "136 -12.3; 143 -12.4; 151 -12.5; 160 -12.5; 169 -12.5; 178 -12.5; 188 -12.4; 199 -12.3; "
    "210 -12.2; 222 -12.1; 235 -11.9; 248 -11.8; 262 -11.5; 277 -11.3; 292 -11.1; 309 -10.8; "
    "326 -10.6; 345 -10.3; 364 -9.9; 385 -9.6; 406 -9.3; 429 -8.9; 453 -8.6; 479 -8.3; 506 "
    "-7.9; 534 -7.5; 565 -7.2; 596 -6.8; 630 -6.4; 665 -6.1; 703 -5.7; 743 -5.3; 784 -5; 829 "
    "-4.7; 875 -4.5; 924 -4.3; 977 -4.2; 1032 -4.2; 1090 -4.4; 1151 -4.6; 1216 -4.9; 1284 -5; "
    "1357 -5.2; 1433 -5.2; 1514 -5.3; 1599 -5.2; 1689 -5.1; 1784 -4.9; 1885 -4.6; 1991 -4.1; "
    "2103 -3.6; 2221 -3; 2347 -2.3; 2479 -1.7; 2618 -1.2; 2766 -0.9; 2921 -1; 3086 -1.4; 3260 "
    "-2.4; 3443 -3.8; 3637 -5.5; 3842 -5.5; 4058 -4.1; 4287 -2.6; 4528 -1.6; 4783 -2.1; 5052 "
    "-2.8; 5337 -4.3; 5637 -7.2; 5955 -8.6; 6290 -7.3; 6644 -5.9; 7018 -4.5; 7414 -3; 7831 "
    "-1.6; 8272 -0.4; 8738 -0.2; 9230 -0.2; 9749 -0.2; 10298 -0.2; 10878 -0.2; 11490 -0.2; "
    "12137 -0.2; 12821 -0.2; 13543 -0.2; 14305 -0.4; 15110 -2; 15961 -4.1; 16860 -6.5; 17809 "
    "-9.1; 18812 -11.9; 19871 -15"
)


def _bell(f, fc, gain, q):
    return gain * np.exp(-((np.log2(f / fc) * q) ** 2))


def _curve_from(gains):
    """(freq, gain) points, quantised the way a published curve would be."""
    return list(zip(FREQS.tolist(), np.round(gains, 1).tolist()))


def _curve_from_grid(freqs, gains):
    """Same, on an arbitrary grid (merges reconcile onto their sources' union)."""
    return list(zip(freqs.tolist(), np.round(gains, 1).tolist()))


def _headphone_curve():
    """An AutoEQ-shaped correction: bass lift, 3k peak tamed, presence, air."""
    return _curve_from(_bell(FREQS, 45, 6.5, 1.1) + _bell(FREQS, 200, -2.0, 1.4)
                       + _bell(FREQS, 3000, -5.0, 2.2) + _bell(FREQS, 5500, 3.0, 3.0)
                       + _bell(FREQS, 9000, -3.5, 2.5) + _bell(FREQS, 14000, 2.0, 1.8))


def _reconstruction_error(config, curve):
    """max |section response - original curve| over the curve's own span."""
    section = config.channel_sections[0]
    freqs = eq_math.frequency_array()
    total = eq_math.combined_response(section.filters, freqs)
    if section.graphic_eq:
        total = total + eq_math.graphic_eq_response(section.graphic_eq, freqs)
    original = eq_math.graphic_eq_response(
        autoeq_merge.format_graphiceq(curve), freqs)
    in_range = (freqs >= curve[0][0]) & (freqs <= curve[-1][0])
    return float(np.max(np.abs(original - total)[in_range]))


curve = _headphone_curve()
geq = autoeq_merge.format_graphiceq(curve)

# ── the actual complaint: an imported profile now HAS bands ────────────────
config = autoeq_merge.build_curve_config(curve)
section = config.channel_sections[0]
check(1 < len(section.filters) <= MAX_BANDS,
      f"a curve-derived profile carries editable PK bands — {len(section.filters)} "
      "of them here (it used to carry none)")
check(all(f.type == "PK" for f in section.filters),
      "every fitted band is a PK filter the editor can render and drag")
check(section.channel == "L R", "the section is still stereo (L R)")

# ── and it is no less accurate than storing the bare curve ────────────────
err = _reconstruction_error(config, curve)
check(err <= WRITE_PRECISION_DB,
      f"bands + residual reconstruct the curve within the file's own 0.1 dB "
      f"write precision (max error {err:.4f} dB)")

# A plain lossy conversion — bands with the residual thrown away — is what we
# are beating; assert we really are beating it, not just passing a loose bound.
lossy = convert_geq_to_peq(geq, max_bands=len(section.filters))
check(lossy.max_error_db > 10 * err,
      f"decomposition is far more accurate than a lossy convert "
      f"({err:.4f} dB vs {lossy.max_error_db:.3f} dB)")

# ── the band count is EARNED, not a fixed quota ───────────────────────────
# The fitter places bands where the curve deviates and stops when another band
# could not express an improvement the file is able to write. So a gentle curve
# must spend fewer bands than a complex one, and neither may spend all 32 just
# because they are available.
gentle = _curve_from(_bell(FREQS, 45, 6.0, 0.8))
messy = _curve_from(sum(_bell(FREQS, fc, g, q) for fc, g, q in [
    (32, -7.0, 4.0), (58, 5.0, 5.0), (95, -4.0, 6.0), (140, 3.0, 5.0),
    (220, -3.0, 4.0), (400, 2.0, 3.0), (1200, -2.0, 2.0), (3500, 3.5, 3.0),
    (7000, -3.0, 2.5), (12000, 2.5, 2.0)]))

n_gentle = len(autoeq_merge.build_curve_config(gentle).channel_sections[0].filters)
n_typical = len(section.filters)
n_messy = len(autoeq_merge.build_curve_config(messy).channel_sections[0].filters)
check(n_gentle < n_typical < n_messy,
      f"bands are spent on demand: {n_gentle} for a gentle tilt, {n_typical} for a "
      f"typical correction, {n_messy} for a messy room curve")
check(n_messy <= MAX_BANDS,
      f"even the messy curve stays inside the {MAX_BANDS}-band ceiling ({n_messy})")

# Every band must earn its editor row: the old point-mapper filled half its
# slots with sub-1 dB, Q>4 grid-chasing junk at high band counts.
for label, c in (("typical", curve), ("messy", messy)):
    bands = autoeq_merge.build_curve_config(c).channel_sections[0].filters
    no_ops = [b for b in bands if abs(b.gain) < 0.1]
    check(not no_ops,
          f"{label}: no fitted band is a written no-op (<0.1 dB) — "
          f"{len(bands)} bands, smallest {min(abs(b.gain) for b in bands):.1f} dB")

# ── bands must stay a MEANINGFUL representation, not a degenerate one ─────
# Unbounded least-squares happily fits a +/-8 dB curve with a -21 dB band
# against a +17 dB one: it fits, but dragging either swings the response wildly.
for label, c in (("typical", curve), ("messy", messy)):
    bands = autoeq_merge.build_curve_config(c).channel_sections[0].filters
    span = max(g for _, g in c) - min(g for _, g in c)
    worst = max(abs(b.gain) for b in bands)
    check(worst <= span + WRITE_PRECISION_DB,
          f"{label}: no band exceeds the curve's own peak-to-peak span "
          f"({worst:.1f} dB vs {span:.1f} dB) — no cancelling band pairs")

# ── accuracy must not depend on the band ceiling ──────────────────────────
# More bands means SHARPER bands, which a sparse residual grid resolves worse —
# so the residual grid densifies on demand. Sweep the whole range the app allows.
for n in (1, 4, 8, 16, MAX_BANDS):
    cfg = autoeq_merge.build_curve_config(curve, max_bands=n)
    sec_n = cfg.channel_sections[0]
    e = _reconstruction_error(cfg, curve)
    n_pts = len(sec_n.graphic_eq.split(";")) if sec_n.graphic_eq else 0
    check(len(sec_n.filters) <= n and e <= WRITE_PRECISION_DB,
          f"ceiling {n:>2}: spent {len(sec_n.filters)}, exact to 0.1 dB (max error "
          f"{e:.4f} dB, residual {n_pts} pts) — the ceiling never costs quality")

# ── extra filters (a Spinorama crossover) survive and eat the band budget ──
crossover = [Filter(type="HP", fc=80.0, gain=0.0, q=0.707)]
cfg = autoeq_merge.build_curve_config(curve, max_bands=6, extra_filters=crossover)
kept = cfg.channel_sections[0].filters
check(kept[0].type == "HP" and kept[0].fc == 80.0,
      "a suggested crossover is kept verbatim, not folded into the derived bands")
check(len(kept) == 1 + 6, "derived bands are added alongside the crossover")

cfg = autoeq_merge.build_curve_config(curve, max_bands=MAX_BANDS,
                                      extra_filters=crossover)
check(len(cfg.channel_sections[0].filters) <= MAX_BANDS,
      "the MAX_BANDS ceiling is respected once the crossover takes its slot")

# ── degenerate inputs ─────────────────────────────────────────────────────
flat = [(20.0, 0.0), (1000.0, 0.0), (20000.0, 0.0)]
cfg = autoeq_merge.build_curve_config(flat)
check(not cfg.channel_sections[0].filters,
      "a flat curve produces no bands (there is nothing to shape)")
cfg = autoeq_merge.build_curve_config([])
check(cfg.channel_sections[0].has_content() is False,
      "an empty curve produces an empty section rather than crashing")

check(decompose_geq("", max_bands=10) is None,
      "decompose_geq() returns None for an empty curve")

# ── preamp still protects the real (summed) response ──────────────────────
cfg = autoeq_merge.build_curve_config(curve)
sec = cfg.channel_sections[0]
freqs = eq_math.frequency_array()
peak = float(np.max(eq_math.combined_response(sec.filters, freqs)
                    + eq_math.graphic_eq_response(sec.graphic_eq, freqs)))
check(cfg.preamp <= -peak,
      f"the preamp ({cfg.preamp:.2f} dB) still offsets the summed peak "
      f"({peak:.2f} dB) of bands + residual together")

# ── section_curve() reads the total shape back, not the residual ──────────
recovered = autoeq_merge.section_curve(sec)
orig = np.array([g for _, g in curve])
rec = np.array([g for _, g in recovered])
check(len(recovered) == len(curve) and float(np.max(np.abs(orig - rec))) <= WRITE_PRECISION_DB,
      "section_curve() recovers the ORIGINAL curve from a decomposed profile "
      "(what REW blending reads) — not the residual layer alone")

peq_only = ChannelSection(channel="L R", filters=[
    Filter(type="PK", fc=1000.0, gain=6.0, q=1.0)])
check(len(autoeq_merge.section_curve(peq_only)) == 127,
      "section_curve() also gives a curve for a hand-built parametric profile "
      "that has no GraphicEQ line at all")
check(not autoeq_merge.section_curve(ChannelSection(channel="L R")),
      "section_curve() returns nothing for a section with no EQ content")

# ── the same guarantees, on a REAL published AutoEQ curve ─────────────────
real = eq_math.parse_graphic_eq(REAL_AUTOEQ_CURVE)
real_cfg = autoeq_merge.build_curve_config(real)
real_sec = real_cfg.channel_sections[0]
real_err = _reconstruction_error(real_cfg, real)
check(real_err <= WRITE_PRECISION_DB,
      f"REAL AutoEQ curve: bands + residual reconstruct it within 0.1 dB "
      f"(max error {real_err:.4f} dB)")
check(1 < len(real_sec.filters) <= MAX_BANDS,
      f"REAL AutoEQ curve: fitted into {len(real_sec.filters)} bands")

# The residual grid must be refined WHERE THE ERROR IS, not everywhere. Uniform
# doubling produced 1009 points on this curve (8x the source grid, i.e. 8x the
# draggable handles in the Graphic EQ editor) and STILL missed the target.
real_pts = len(real_sec.graphic_eq.split(";")) if real_sec.graphic_eq else 0
check(real_pts <= 2 * len(real),
      f"REAL AutoEQ curve: the residual stays near the source grid "
      f"({real_pts} points vs the curve's {len(real)}) — refinement is adaptive, "
      "so the Graphic EQ editor is not flooded with handles")

# The failure lived in the top octave; assert the whole span, but call out the
# region that actually broke.
freqs = eq_math.frequency_array()
total = (eq_math.combined_response(real_sec.filters, freqs)
         + eq_math.graphic_eq_response(real_sec.graphic_eq, freqs))
want = eq_math.graphic_eq_response(REAL_AUTOEQ_CURVE, freqs)
top = (freqs >= 12000) & (freqs <= real[-1][0])
top_err = float(np.max(np.abs(want - total)[top]))
check(top_err <= WRITE_PRECISION_DB,
      f"REAL AutoEQ curve: the top octave — where biquads steepen toward Nyquist "
      f"and the source grid is coarsest — is within 0.1 dB ({top_err:.4f} dB)")

# And it survives a sample-rate change, like any other fitted profile.
autoeq_merge.retune_config_for_sample_rate(real_cfg, 48000, 44100)
sec_44 = real_cfg.channel_sections[0]
total_44 = (eq_math.combined_response(sec_44.filters, freqs, 44100)
            + eq_math.graphic_eq_response(sec_44.graphic_eq, freqs))
band = (freqs >= real[0][0]) & (freqs <= real[-1][0])
err_44 = float(np.max(np.abs(want - total_44)[band]))
check(err_44 <= WRITE_PRECISION_DB,
      f"REAL AutoEQ curve: still exact after a 48k -> 44.1k device change "
      f"({err_44:.4f} dB)")

# ── the merge and gear-sim paths, which are curves of a different shape ───
# Both reach the same build_curve_config(), but a merged curve is reconciled onto
# the UNION of its sources' grids (so it is neither AutoEQ's 127 points nor
# uniform in log-frequency) and a sim curve is a clamped DIFFERENCE of two
# curves. Neither shape is exercised by a plain import, so pin them here.
other_freqs = np.geomspace(25.0, 19000.0, 211)
other = _curve_from_grid(other_freqs,
                         3.0 * np.sin(np.log2(other_freqs) * 1.3) - 2.0)
coarse_freqs = np.geomspace(20.0, 20000.0, 31)
coarse = _curve_from_grid(coarse_freqs, np.linspace(-4.0, 4.0, 31))

merged = autoeq_merge.average_curves([real, other])
check(len(merged) > len(real),
      f"a merge across mismatched grids lands on their union "
      f"({len(real)} + {len(other)} points -> {len(merged)})")

for label, c in (("[Merged] two grids", merged),
                 ("[Merged] three grids", autoeq_merge.average_curves(
                     [real, other, coarse])),
                 ("[Sim] delta of two curves", autoeq_merge.delta_curve(other, real))):
    cfg = autoeq_merge.build_curve_config(c)
    e = _reconstruction_error(cfg, c)
    sec_c = cfg.channel_sections[0]
    pts = len(sec_c.graphic_eq.split(";")) if sec_c.graphic_eq else 0
    check(e <= WRITE_PRECISION_DB and bool(sec_c.filters),
          f"{label}: {len(sec_c.filters)} bands, exact to 0.1 dB ({e:.4f} dB)")
    check(pts <= 2 * len(c),
          f"{label}: residual tracks the curve's own grid ({pts} points for "
          f"{len(c)}) — a merged curve is denser, but the fitter does not inflate it")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("All curve-decomposition checks passed.")
