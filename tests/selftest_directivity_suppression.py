"""Self-test: Rumble 3.6 — directivity-aware suppression. Unlike the
existing directivity_boost_cap() (a single per-speaker cap-damping scalar
from published horizontal/vertical directivity *estimates*),
eq_target.directivity_gap_suppression() is per-frequency: it compares a
speaker's Listening Window against its Sound Power and suppresses
correction wherever their gap deviates sharply from its own local trend — a
signature of a directivity artifact (crossover lobing, baffle diffraction)
that a global EQ correction can't actually fix, distinguished from the
expected, benign broadband LW-vs-SP divergence every real speaker has at
high frequencies (see eq_target.py's module comment for the real
reference-speaker derivation of the thresholds). make_correction() threads
this through via a new directivity_secondary= parameter, complementary to
(not a replacement for) directivity_boost_cap(); SpinoramaProvider fetches
the companion Sound Power curve automatically when
CorrectionOptions.directivity_gap_suppression is True (the default)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core import eq_target, spinorama
from rumble.core.measurements import registry
from rumble.core.measurements.base import CorrectionOptions

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


grid = eq_target.log_grid(200.0, 16000.0, 12)
DIP_LOW, DIP_HIGH = 2500.0, 3500.0  # ~1/2-octave notch band, well inside any 12-pt/oct grid


def in_dip(f: float) -> bool:
    return DIP_LOW <= f <= DIP_HIGH


dip_points = [f for f in grid if in_dip(f)]
# excludes the very top of the grid, where log_grid()'s rounding can place
# the last point slightly past the 16000 Hz band edge (soft-tapered there
# regardless of directivity suppression — not what this section is testing).
away_points = [f for f in grid if DIP_HIGH * 2 < f < 15000.0 or f < DIP_LOW / 2]

flat_power = [(f, 0.0) for f in grid]
notched_lw = [(f, -6.0 if in_dip(f) else 0.0) for f in grid]

# ── directivity_gap_suppression(): pure function ──────────────────────────
suppression = eq_target.directivity_gap_suppression(notched_lw, flat_power, grid)
sup = dict(suppression)
check(all(sup[f] < 0.5 for f in dip_points),
      "a sharp local LW-vs-SP excursion (a directivity-artifact notch) is heavily suppressed")
check(all(sup[f] == 1.0 for f in away_points),
      "frequencies far from the excursion are not suppressed at all")

smooth_tilt_lw = [(f, -3.0) for f in grid]  # a broad, benign LW-vs-SP gap (no local excursion)
suppression_flat = eq_target.directivity_gap_suppression(smooth_tilt_lw, flat_power, grid)
sup_flat = dict(suppression_flat)
check(all(v == 1.0 for v in sup_flat.values()),
      "a uniform broadband gap (the normal HF directivity divergence every "
      "speaker has) is NOT treated as an anomaly — only local excursions from trend are")

check(eq_target.directivity_gap_suppression([], flat_power, grid) == [],
      "an empty primary curve returns no suppression data rather than suppressing everything")
check(eq_target.directivity_gap_suppression(notched_lw, [], grid) == [],
      "an empty secondary curve returns no suppression data rather than suppressing everything")

# ── make_correction(): directivity_secondary= applies the suppression ────
corr_unsuppressed, _ = eq_target.make_correction(
    notched_lw, slope_db_per_oct=0.0, smooth_oct=0.0, grid=grid, band=(200.0, 16000.0))
corr_suppressed, clamp = eq_target.make_correction(
    notched_lw, slope_db_per_oct=0.0, smooth_oct=0.0, grid=grid, band=(200.0, 16000.0),
    directivity_secondary=flat_power)

unsup_d = dict(corr_unsuppressed)
sup_d = dict(corr_suppressed)
check(all(unsup_d[f] > 5.0 for f in dip_points),
      "without directivity_secondary, the notch gets the full corrective boost")
check(all(sup_d[f] < 1.0 for f in dip_points),
      "with directivity_secondary, the same notch's correction is suppressed toward zero")
check(clamp.points_clamped_boost > 0 and clamp.max_clamped_delta_db > 0.5,
      "the suppression is surfaced through the existing ClampSummary bookkeeping (Rumble 1.4)")

# a curve with no directivity anomaly is unaffected by passing a secondary curve
corr_flat_a, _ = eq_target.make_correction(
    smooth_tilt_lw, slope_db_per_oct=0.0, smooth_oct=0.0, grid=grid, band=(200.0, 16000.0))
corr_flat_b, clamp_flat = eq_target.make_correction(
    smooth_tilt_lw, slope_db_per_oct=0.0, smooth_oct=0.0, grid=grid, band=(200.0, 16000.0),
    directivity_secondary=flat_power)
check(corr_flat_a == corr_flat_b,
      "a curve with no local directivity excursion is untouched by directivity_secondary")
check(not clamp_flat.notable(), "no suppression means nothing is reported as clamped")

# ── complementary to (not a duplicate of) directivity_boost_cap() ────────
damped_cap = eq_target.directivity_boost_cap(6.0, horizontal_avg=25.0, vertical_avg=25.0)
check(damped_cap < 6.0, "sanity: the existing global damping mechanism still fires on its own")

# A modest baseline need (well under either cap) plus a deep local notch (well
# past both caps) isolates the two mechanisms: away from the notch, only
# cap-damping can act (nothing to suppress, no local excursion); at the
# notch, boost is already saturated at the *damped* cap before suppression
# even applies, so any further reduction there is attributable to the gap
# suppression alone, not a second dose of the same cap-damping.
broad_dip = [(f, -20.0 if in_dip(f) else -2.0) for f in grid]
corr_both, _ = eq_target.make_correction(
    broad_dip, slope_db_per_oct=0.0, smooth_oct=0.0, grid=grid, band=(200.0, 16000.0),
    max_boost_db=damped_cap, directivity_secondary=flat_power)
both_d = dict(corr_both)
check(all(abs(both_d[f] - 2.0) < 1e-6 for f in away_points),
      "away from the anomaly, the modest 2 dB request passes through untouched "
      "by either mechanism (well under both the damped cap and the suppression floor)")
check(all(abs(both_d[f] - round(damped_cap, 2)) > 1e-6 for f in dip_points),
      "at the notch, the correction is NOT just the damped cap value — gap "
      "suppression has additionally reduced it")
check(all(both_d[f] < both_d[away_points[0]] for f in dip_points),
      "at the anomaly, the per-frequency gap suppression pulls the "
      "cap-saturated correction below even the modest away-from-anomaly level "
      "— the two mechanisms compose rather than duplicate")

# ── SpinoramaProvider fetches the Sound Power companion curve automatically ─
sp_provider = [p for p in registry.all_providers(".") if p.id == "spinorama"][0]
sp_entry = spinorama.SpinoramaEntry(name="Test Speaker", brand="Acme", model="Test",
                                    source="ASR", measurement_key="asr")

curves_by_basis = {"Listening Window": notched_lw, "Sound Power": flat_power}


def fake_fetch_curve(entry, *, curve="Listening Window", **kw):
    return curves_by_basis.get(curve, [])


orig = spinorama.fetch_curve
spinorama.fetch_curve = fake_fetch_curve  # type: ignore[assignment]
try:
    curve_on, clamp_on = sp_provider.fetch_curve(
        sp_entry, options=CorrectionOptions(
            slope_db_per_oct=0.0, smooth_oct=0.0, directivity_gap_suppression=True))
    curve_off, clamp_off = sp_provider.fetch_curve(
        sp_entry, options=CorrectionOptions(
            slope_db_per_oct=0.0, smooth_oct=0.0, directivity_gap_suppression=False))
finally:
    spinorama.fetch_curve = orig

on_d = dict(curve_on)
off_d = dict(curve_off)
on_dip_avg = sum(on_d[f] for f in on_d if in_dip(f)) / max(1, sum(1 for f in on_d if in_dip(f)))
off_dip_avg = sum(off_d[f] for f in off_d if in_dip(f)) / max(1, sum(1 for f in off_d if in_dip(f)))
check(on_dip_avg < off_dip_avg,
      "SpinoramaProvider suppresses the notch when directivity_gap_suppression is enabled, "
      "vs. the full correction when it's disabled")
check(clamp_on.points_clamped_boost >= clamp_off.points_clamped_boost,
      "enabling gap suppression never reports fewer clamped points than disabling it")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
