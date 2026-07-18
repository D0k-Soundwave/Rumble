"""Self-test: silent correction-curve clamping is now surfaced instead of
just applied (Rumble 1.4 / 5.3). eq_target.make_correction() returns a
ClampSummary alongside the curve reporting how many points hit the boost
cap/cut floor and by how much; AutoEqProvider/SpinoramaProvider/
FederatedCatalog thread that summary through fetch_curve() (AutoEQ's data is
already a finished correction, so it always reports zero); and
ClampSummary.notable() applies RUMBLE_ACTION_GUIDE.md #1.4's own "meaningful
amount" bar (> 0.5 dB) for whether the UI should say anything."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core import autoeq, eq_target, spinorama
from rumble.core.eq_target import ClampSummary
from rumble.core.measurements import FederatedCatalog, registry
from rumble.core.measurements.base import CorrectionOptions, MeasurementEntry

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── make_correction() returns (curve, ClampSummary) ───────────────────────
flat_measured = [(20.0, 0.0), (1000.0, 0.0), (20000.0, 0.0)]
corr, clamp = eq_target.make_correction(
    flat_measured, slope_db_per_oct=0.0, smooth_oct=0.0,
    max_boost_db=6.0, max_cut_db=12.0)
check(isinstance(corr, list) and isinstance(clamp, ClampSummary),
      "make_correction returns (curve, ClampSummary)")
check(clamp.points_clamped_boost == 0 and clamp.points_clamped_cut == 0,
      "a curve that never needs more than the cap reports zero clamped points")
check(not clamp.notable(), "a zero-clamp summary is never notable")

# A deep dip wants far more boost than the 6 dB cap allows.
deep_dip = [(20.0, 0.0), (1000.0, -20.0), (20000.0, 0.0)]
corr2, clamp2 = eq_target.make_correction(
    deep_dip, slope_db_per_oct=0.0, smooth_oct=0.0,
    max_boost_db=6.0, max_cut_db=12.0)
check(clamp2.points_clamped_boost > 0,
      "a curve deliberately requiring more boost than the cap reports clamped points")
check(clamp2.max_clamped_delta_db > 0.5,
      "the reported clamp delta reflects the real overshoot")
check(clamp2.notable(), "a meaningfully clamped summary is notable")

# A deep peak wants far more cut than the 12 dB floor allows.
deep_peak = [(20.0, 0.0), (1000.0, 20.0), (20000.0, 0.0)]
corr3, clamp3 = eq_target.make_correction(
    deep_peak, slope_db_per_oct=0.0, smooth_oct=0.0,
    max_boost_db=6.0, max_cut_db=12.0)
check(clamp3.points_clamped_cut > 0,
      "a curve deliberately requiring more cut than the floor reports clamped points")

# ── the 0.5 dB significance threshold from RUMBLE_ACTION_GUIDE.md #1.4 ────
tiny = ClampSummary(points_clamped_boost=1, points_clamped_cut=0, max_clamped_delta_db=0.2)
check(not tiny.notable(), "clamping below the 0.5 dB threshold is not notable")
borderline = ClampSummary(points_clamped_boost=1, points_clamped_cut=0, max_clamped_delta_db=0.6)
check(borderline.notable(), "clamping above the 0.5 dB threshold is notable")
check(not ClampSummary().notable(), "the default (all-zero) summary is never notable")

# ── AutoEqProvider's flat (no tilt/target) fetch never clamps -- it's ──────
# already a finished correction. A tilt/named-target re-target instead goes
# through eq_target.clamp_gain() and CAN report real clamping -- see
# tests/selftest_measurements.py's "steep tilt" check for that path.
hp_provider = [p for p in registry.all_providers(".") if p.id == "autoeq"][0]
autoeq.fetch_graphic = lambda entry: (  # type: ignore[assignment]
    "GraphicEQ: 100 0.0; 1000 0.0; 10000 0.0")
hp_raw = autoeq.AutoEqEntry(name="Test HP", source="test", rig="over-ear",
                            path="test/over-ear/Test", device_type="Over-ear")
hp_curve, hp_clamp = hp_provider.fetch_curve(hp_raw, options=CorrectionOptions(slope_db_per_oct=0.0))
check(isinstance(hp_clamp, ClampSummary) and not hp_clamp.notable(),
      "AutoEqProvider.fetch_curve()'s flat path always returns a no-clamp ClampSummary")

# ── SpinoramaProvider threads a real clamp summary through fetch_curve() ──
sp_provider = [p for p in registry.all_providers(".") if p.id == "spinorama"][0]
spinorama.fetch_curve = lambda entry, **kw: [  # type: ignore[assignment]
    (20.0, 0.0), (1000.0, -20.0), (20000.0, 0.0)]
sp_entry = spinorama.SpinoramaEntry(name="Test Speaker", brand="Acme", model="Test",
                                    source="ASR", measurement_key="asr")
sp_curve, sp_clamp = sp_provider.fetch_curve(
    sp_entry, options=CorrectionOptions(slope_db_per_oct=0.0, max_boost_db=6.0))
check(sp_clamp.points_clamped_boost > 0,
      "SpinoramaProvider.fetch_curve() surfaces a real clamp summary from make_correction()")
check(sp_clamp.notable(), "the speaker's clamp summary is notable")

flat_curve = [(20.0, 0.0), (1000.0, 0.0), (20000.0, 0.0)]
spinorama.fetch_curve = lambda entry, **kw: flat_curve  # type: ignore[assignment]
sp_curve_flat, sp_clamp_flat = sp_provider.fetch_curve(
    sp_entry, options=CorrectionOptions(slope_db_per_oct=0.0, max_boost_db=6.0))
check(not sp_clamp_flat.notable(),
      "SpinoramaProvider reports no clamping for a curve that never needs the cap")

# ── FederatedCatalog passes the (curve, clamp) tuple through unchanged ────
spinorama.fetch_curve = lambda entry, **kw: [  # type: ignore[assignment]
    (20.0, 0.0), (1000.0, -20.0), (20000.0, 0.0)]
cat = FederatedCatalog(registry.all_providers("."))
we_s = MeasurementEntry(provider_id="spinorama", name=sp_entry.name, source=sp_entry.source,
                        transducer="speaker", device_type="Speaker", license="",
                        attribution="", raw=sp_entry)
fed_curve, fed_clamp = cat.fetch_curve(
    we_s, options=CorrectionOptions(slope_db_per_oct=0.0, max_boost_db=6.0))
check(fed_clamp.points_clamped_boost > 0,
      "FederatedCatalog.fetch_curve() threads the clamp summary through to the caller")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
