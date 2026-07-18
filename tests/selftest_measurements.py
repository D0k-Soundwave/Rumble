"""Self-test: the measurement-data layer — Spinorama parsing, speaker correction
maths, and the provider registry. Fully offline: network fetches are replaced
with embedded fixtures (including a real Plotly base64 typed-array), so this runs
in the headless gate."""
import base64
import inspect
import os
import struct
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rumble.core import autoeq, eq_target, spinorama
from rumble.core.autoeq_merge import compute_preamp, distinct_entry_names
from rumble.core.measurements import FederatedCatalog, registry
from rumble.core.measurements.base import (
    CorrectionOptions, MeasurementEntry, MeasurementProvider,
)
from rumble.ui import autoeq_dialog as _autoeq_dialog_module

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── eq_target maths ──────────────────────────────────────────────────────
grid = eq_target.log_grid(20.0, 20000.0, 12)
tgt = eq_target.tilt_target(grid, slope_db_per_oct=-1.0, pivot_hz=1000.0)
# at the pivot == 0; one octave above pivot == slope
near_pivot = min(tgt, key=lambda p: abs(p[0] - 1000))
check(abs(near_pivot[1]) < 0.1, "tilt target is ~0 dB at the pivot")
two_k = min(tgt, key=lambda p: abs(p[0] - 2000))
check(abs(two_k[1] - (-1.0)) < 0.05, "tilt target is -slope one octave above pivot")

spike = [(100.0, 0.0), (200.0, 0.0), (300.0, 12.0), (400.0, 0.0), (500.0, 0.0)]
sm = eq_target.octave_smooth(spike, frac_oct=2.0)
peak_sm = [g for f, g in sm if f == 300.0][0]
check(peak_sm < 12.0, "octave smoothing reduces a narrow spike")

# A measured curve with a deep dip -> correction wants a big boost -> capped.
measured = [(20.0, -3.0), (1000.0, 0.0), (3000.0, -10.0), (16000.0, -2.0)]
corr, corr_clamp = eq_target.make_correction(
    measured, slope_db_per_oct=0.0, smooth_oct=0.0, max_boost_db=6.0,
    band=(60.0, 16000.0))
gains = [g for _, g in corr]
check(max(gains) <= 6.0 + 1e-6, "make_correction caps boost at max_boost_db")
# Below the band floor (60 Hz), correction tapers to zero.
low = [g for f, g in corr if f <= 30.0]
check(all(abs(g) < 1e-6 for g in low), "make_correction band-limits below the floor")
check(compute_preamp(corr) <= 0.0, "compute_preamp never adds gain")
check(corr_clamp.points_clamped_boost > 0,
      "make_correction's ClampSummary reports the boost-capped points (Rumble 1.4)")


# ── Spinorama parsing ────────────────────────────────────────────────────
META = {
    "Acme Monitor One": {
        "brand": "Acme", "model": "Monitor One", "type": "active",
        "shape": "bookshelves", "default_measurement": "asr",
        "measurements": {
            "asr": {"origin": "ASR", "quality": "high",
                    "estimates": {"ref_6dB": 48.0}},
            "eac": {"origin": "ErinsAudioCorner", "quality": "high",
                    "estimates": {"ref_3dB": 60.0}},
        },
    },
}
entries = spinorama.parse_index(META)
check(len(entries) == 2, "parse_index yields one entry per measurement")
asr = [e for e in entries if e.source == "ASR"][0]
check(asr.ref_6db == 48.0, "parse_index reads the -6 dB extension")
eac = [e for e in entries if e.source == "ErinsAudioCorner"][0]
check(eac.ref_3db == 60.0, "parse_index reads the -3 dB extension")
check(eac.ref_6db == 0.0,
      "ref_6db stays 0.0 (unknown) rather than being silently mislabeled "
      "with the -3 dB value when only -3 dB was published")
check(asr.measurement_url().endswith("/ASR/asr/CEA2034.json"),
      "measurement_url uses origin/key path")
check(spinorama.sources(entries) == ["ASR", "ErinsAudioCorner"],
      "sources lists distinct origins")
check(len(spinorama.search(entries, "acme")) == 2, "search matches by name")
check(len(spinorama.search(entries, "acme", source="ASR")) == 1,
      "search honours the source facet")


def _typed(values):
    raw = struct.pack(f"<{len(values)}d", *values)
    return {"dtype": "f8", "bdata": base64.b64encode(raw).decode("ascii")}


figure = {"data": [
    {"name": "On Axis", "x": _typed([100.0, 1000.0]), "y": _typed([1.0, 1.0])},
    {"name": "Listening Window",
     "x": _typed([100.0, 1000.0, 10000.0]),
     "y": _typed([-1.0, 0.0, -2.0])},
]}
lw = spinorama.extract_curve(figure, "Listening Window")
check(lw == [(100.0, -1.0), (1000.0, 0.0), (10000.0, -2.0)],
      "extract_curve decodes a base64 typed-array trace")
check(spinorama.extract_curve(figure, "Nope") == [],
      "extract_curve returns [] for a missing trace")
check(spinorama._decode_array([1.0, 2.0]) == [1.0, 2.0],
      "_decode_array passes through a plain list")


# ── B: speaker curve-basis selector ──────────────────────────────────────
check(CorrectionOptions().curve_basis == "Listening Window",
      "CorrectionOptions defaults curve_basis to Listening Window")

check(set(spinorama._CURVE_TO_FILE) == {
    "On Axis", "Listening Window", "Early Reflections", "Sound Power",
    "Estimated In-Room Response",
}, "all 5 curve bases are still available at the data layer")

_basis_src = inspect.getsource(_autoeq_dialog_module.MeasurementWorkspace._build_basis_row)
check("Early Reflections" in _basis_src and "Sound Power" in _basis_src,
      "basis_combo source offers Early Reflections and Sound Power")

_tilt_row_src = inspect.getsource(_autoeq_dialog_module.MeasurementWorkspace._build_tilt_row)
check("pivot_spin" in _tilt_row_src, "tilt row builds a pivot_spin control")
_current_options_src = inspect.getsource(_autoeq_dialog_module.MeasurementWorkspace._current_options)
check("pivot_hz=self.pivot_spin.value()" in _current_options_src,
      "_current_options feeds pivot_spin into CorrectionOptions.pivot_hz")

_basis_row_src = inspect.getsource(_autoeq_dialog_module.MeasurementWorkspace._build_basis_row)
check("boost_cap_spin" in _basis_row_src, "basis row builds a boost_cap_spin control")
_current_options_src2 = inspect.getsource(_autoeq_dialog_module.MeasurementWorkspace._current_options)
check("max_boost_db=" in _current_options_src2 and "boost_cap_spin" in _current_options_src2,
      "_current_options feeds boost_cap_spin into CorrectionOptions.max_boost_db")

_basis_calls = []


def _fake_fetch_measurement(entry, *, curve_file="CEA2034", data_dir=None, max_age_days=7):
    _basis_calls.append(curve_file)
    if curve_file == "Estimated In-Room Response":
        return {"data": [{"name": "Estimated In-Room Response",
                          "x": [100.0, 1000.0], "y": [-2.0, 0.0]}]}
    return figure


spinorama.fetch_measurement = _fake_fetch_measurement
check(spinorama.fetch_curve(asr, curve="On Axis") == [(100.0, 1.0), (1000.0, 1.0)],
      "On-Axis basis reads the On Axis trace")
check(_basis_calls[-1] == "CEA2034", "On-Axis basis requests CEA2034, not a separate file")
check(spinorama.fetch_curve(asr, curve="Estimated In-Room Response")
      == [(100.0, -2.0), (1000.0, 0.0)],
      "Estimated In-Room basis reads the dedicated file")
check(_basis_calls[-1] == "Estimated In-Room Response",
      "In-Room basis requests the dedicated file")


# ── registry gating ──────────────────────────────────────────────────────
ids = lambda provs: [p.id for p in provs]
check(ids(registry.enabled_providers(".")) == ["autoeq", "spinorama"],
      "all shipped sources are enabled by default")
check(ids(registry.all_providers(".")) == ["autoeq", "spinorama"],
      "registry exposes both providers")
check(ids(registry.enabled_providers(".", {"spinorama_enabled": False})) == ["autoeq"],
      "a source can still be force-disabled via a settings override")
for p in registry.all_providers("."):
    check(isinstance(p, MeasurementProvider), f"{p.id} is a MeasurementProvider")


# ── SpinoramaProvider end-to-end (measurement fetch stubbed) ─────────────
sp_provider = [p for p in registry.all_providers(".") if p.id == "spinorama"][0]
spinorama.fetch_curve = lambda entry, **kw: [  # type: ignore[assignment]
    (20.0, -3.0), (1000.0, 0.0), (5000.0, -8.0), (16000.0, -1.0)]
flat, flat_clamp = sp_provider.fetch_curve(asr, options=CorrectionOptions(slope_db_per_oct=0.0))
tilt, tilt_clamp = sp_provider.fetch_curve(asr, options=CorrectionOptions(slope_db_per_oct=-0.9,
                                                                          label="room tilt"))
check(bool(flat) and bool(tilt), "SpinoramaProvider builds a correction curve")
check(max(g for _, g in flat) <= 6.0 + 1e-6,
      "provider correction respects the boost cap")
check(flat_clamp.points_clamped_boost > 0,
      "SpinoramaProvider.fetch_curve() surfaces the clamp summary for a curve that hits the cap")
check(sp_provider.import_name(asr, CorrectionOptions(label="room tilt"))
      == "Acme Monitor One (room tilt)", "import_name folds the tilt label in")
check("Speaker" in sp_provider.import_tags(asr), "speaker imports are tagged Speaker")
check(sp_provider.commercial_safe is False and sp_provider.enabled_by_default is True,
      "Spinorama is enabled by default but not commercial-safe")

# fetch_curve()'s low-frequency taper edge must fall back to ref_3db (60 Hz
# for eac) rather than defaulting all the way down to 20 Hz just because
# ref_6db is 0.0/unknown -- otherwise a speaker whose only published estimate
# is -3 dB would get boosted below its own real usable extension.
eac_curve, _ = sp_provider.fetch_curve(eac, options=CorrectionOptions(slope_db_per_oct=0.0))
no_ref_entry = spinorama.SpinoramaEntry(name="No Ref Speaker", brand="Acme", model="No Ref",
                                        source="ASR", measurement_key="noref")
no_ref_curve, _ = sp_provider.fetch_curve(no_ref_entry, options=CorrectionOptions(slope_db_per_oct=0.0))
eac_20hz = next(g for f, g in eac_curve if f == 20.0)
no_ref_20hz = next(g for f, g in no_ref_curve if f == 20.0)
check(abs(eac_20hz) < abs(no_ref_20hz),
      "a ref_3db-only entry's 20 Hz point is tapered toward its own 60 Hz "
      "extension, not left at full strength like an entry with no ref data at all")


# ── FederatedCatalog: one unified catalogue over both providers ──────────
cat = FederatedCatalog(registry.all_providers("."))
check(cat.device_types() == ["Over-ear", "In-ear", "Earbud", "Speaker"],
      "federated device-type facet unions headphones + speakers")
check(cat.supports_merge and cat.supports_tilt,
      "federated catalog aggregates provider capabilities")

# Wrapped entries: one AutoEQ headphone + the Spinorama speaker from above.
hp_raw = autoeq.AutoEqEntry(name="Sennheiser HD 600", source="oratory1990",
                            rig="over-ear", path="oratory1990/over-ear/HD600",
                            device_type="Over-ear")
we_h = MeasurementEntry(provider_id="autoeq", name=hp_raw.name, source=hp_raw.source,
                        transducer="headphone", device_type="Over-ear",
                        license="MIT", attribution="", raw=hp_raw)
we_s = MeasurementEntry(provider_id="spinorama", name=asr.name, source=asr.source,
                        transducer="speaker", device_type="Speaker",
                        license="", attribution="", raw=asr)
federated = [we_h, we_s]
check(cat.sources(federated) == ["ASR", "oratory1990"],
      "federated sources unions across providers")
check([e.name for e in cat.search(federated, "sennheiser")] == ["Sennheiser HD 600"],
      "federated search matches a headphone across the merged list")
check([e.device_type for e in cat.search(federated, device_type="Speaker")] == ["Speaker"],
      "federated type facet filters to speakers only")
fed_curve, fed_clamp = cat.fetch_curve(we_s, options=CorrectionOptions(slope_db_per_oct=-0.9))
check(bool(fed_curve), "federated fetch_curve dispatches to the speaker provider")
check(isinstance(fed_clamp, eq_target.ClampSummary),
      "federated fetch_curve returns a ClampSummary alongside the curve")
check(cat.import_name(we_s, CorrectionOptions(label="room tilt"))
      == "Acme Monitor One (room tilt)", "federated import_name dispatches by provider")
check(cat.fetch_parametric(we_s) is None, "speaker entry has no parametric via federation")


# ── A1: Spinorama multi-measurement merge ────────────────────────────────
class _NamedStub:
    def __init__(self, name):
        self.name = name


check(distinct_entry_names([_NamedStub("A"), _NamedStub("B"), _NamedStub("A")])
      == ["A", "B"], "distinct_entry_names preserves first-seen order, dedupes")
check(sp_provider.supports_merge is True, "Spinorama now supports merge")


# ── A2: AutoEQ Fixed-Band EQ fetch (backend only) ─────────────────────────
hp_provider = [p for p in registry.all_providers(".") if p.id == "autoeq"][0]
autoeq.fetch_fixed = lambda entry: (  # type: ignore[assignment]
    "Preamp: -4.0 dB\nFilter 1: ON PK Fc 105 Hz Gain -3.0 dB Q 1.41\n")
check(hp_provider.fetch_fixed_band(hp_raw) is not None,
      "AutoEqProvider exposes fixed-band text")
check(sp_provider.fetch_fixed_band(asr) is None,
      "SpinoramaProvider has no fixed-band variant (base default)")
check(hp_raw.fixed_url().endswith("%20FixedBandEQ.txt"),
      "fixed_url targets FixedBandEQ.txt")
check(cat.fetch_fixed_band(we_h) is not None,
      "federated fetch_fixed_band dispatches to AutoEQ")


# ── A3: crossover / subsonic high-pass suggestion ─────────────────────────
check(eq_target.suggested_highpass_hz(ref_3db=55.0) == 55.0,
      "prefers -3dB when present")
check(eq_target.suggested_highpass_hz(ref_6db=48.0) > 48.0,
      "nudges up from -6dB")
check(eq_target.suggested_highpass_hz() is None, "None with no estimate")
filters = sp_provider.suggested_filters(asr)
check(len(filters) == 1 and filters[0].type == "HPQ",
      "Spinorama suggests one HPQ filter")
check(hp_provider.suggested_filters(hp_raw) == [],
      "AutoEQ has no filter suggestion (base default)")


# ── C: shared "Import as" capability flags ────────────────────────────────
check(hp_provider.supports_fixed_band is True, "AutoEqProvider supports fixed-band")
check(sp_provider.supports_fixed_band is False, "SpinoramaProvider does not")
check(cat.supports_fixed_band is True, "federated catalog aggregates fixed-band support")


# ── D: Convolution/IR import ──────────────────────────────────────────────
autoeq.fetch_ir = lambda entry, sample_rate=44100: (  # type: ignore[assignment]
    b"RIFF" + bytes([sample_rate % 256]))
irs = hp_provider.fetch_convolution(hp_raw)
check(irs is not None and set(irs) == {44100, 48000}, "AutoEqProvider fetches both IR rates")
check(sp_provider.fetch_convolution(asr) is None, "SpinoramaProvider has no convolution variant")
check(cat.fetch_convolution(we_h) is not None, "federated fetch_convolution dispatches to AutoEQ")


# ── E: headphone preference tilt / re-target ──────────────────────────────
csv_text = "frequency,raw,target\n100,0.0,1.0\n1000,-2.0,0.0\n10000,-1.0,-3.0\n"
csv_data = autoeq.parse_csv(csv_text)
check(csv_data.raw == [(100.0, 0.0), (1000.0, -2.0), (10000.0, -1.0)], "parse_csv reads raw")
check(csv_data.target == [(100.0, 1.0), (1000.0, 0.0), (10000.0, -3.0)], "parse_csv reads target")

autoeq.fetch_csv = lambda entry: csv_text  # type: ignore[assignment]
autoeq.fetch_parametric = lambda entry: "Preamp: 0 dB\n"  # type: ignore[assignment]
autoeq.fetch_graphic = lambda entry: (  # type: ignore[assignment]
    "GraphicEQ: 100 0.0; 1000 0.0; 10000 0.0")
flat, flat_hp_clamp = hp_provider.fetch_curve(hp_raw, options=CorrectionOptions(slope_db_per_oct=0.0))
tilted, tilted_hp_clamp = hp_provider.fetch_curve(
    hp_raw, options=CorrectionOptions(slope_db_per_oct=-1.0, label="tilt -1.0"))
check(flat != tilted, "non-zero tilt changes the AutoEQ correction curve")
check(not flat_hp_clamp.notable(),
      "flat AutoEQ fetch (no tilt/target) never clamps -- it's already a finished correction")
check(not tilted_hp_clamp.notable(),
      "a modest tilt on this fixture stays within the clamp -- not evidence tilt can't clamp, see below")
# A tilt/re-target IS a bare `target - measured` delta with no inherent gain
# bound (same as autoeq_merge's gear-simulation delta) -- Rumble 2.6 fixed
# gear-sim with clamp_gain(); AutoEqProvider.fetch_curve()'s tilt/named-target
# path had the identical unbounded-delta shape but was missed. Confirm a
# tilt steep enough to actually exceed the 6/12 dB cap now gets clamped.
steep, steep_hp_clamp = hp_provider.fetch_curve(
    hp_raw, options=CorrectionOptions(slope_db_per_oct=-20.0, label="steep tilt"))
check(steep_hp_clamp.notable() and steep_hp_clamp.points_clamped_boost > 0,
      "a tilt steep enough to exceed the boost cap is now clamped, not left unbounded")
check(hp_provider.fetch_parametric(hp_raw, options=CorrectionOptions(slope_db_per_oct=-1.0)) is None,
      "fetch_parametric yields to fetch_curve when a tilt is requested")
check(hp_provider.fetch_parametric(hp_raw, options=CorrectionOptions(slope_db_per_oct=0.0)) is not None,
      "fetch_parametric still short-circuits with no tilt")
check(hp_provider.import_name(hp_raw, CorrectionOptions(slope_db_per_oct=-1.0, label="tilt -1.0"))
      == f"{hp_raw.name} (tilt -1.0)", "tilted import_name reflects the tilt")
check(hp_provider.import_name(hp_raw, CorrectionOptions(slope_db_per_oct=0.0)) == hp_raw.name,
      "flat import_name is unchanged (no tilt suffix)")
check(hp_provider.supports_tilt is True, "AutoEqProvider now supports tilt")
check(cat.supports_tilt is True, "federated catalog still aggregates tilt support")


# ── F: directivity-aware EQ limiting ──────────────────────────────────────
check(eq_target.directivity_boost_cap(6.0, horizontal_avg=0.0, vertical_avg=0.0) == 6.0,
      "no limiting applied when directivity data is unavailable")
check(eq_target.directivity_boost_cap(6.0, horizontal_avg=50.0, vertical_avg=0.0) == 6.0,
      "no limiting applied when only one axis is known")
# Real speaker values from the Phase F0 spike (quality="high" ASR measurements).
check(eq_target.directivity_boost_cap(6.0, horizontal_avg=50.0, vertical_avg=50.0) == 6.0,
      "Genelec 8351B (min 50 deg, well above the full-cap threshold): no reduction")
check(eq_target.directivity_boost_cap(6.0, horizontal_avg=50.0, vertical_avg=40.0) == 6.0,
      "Neumann KH 80 (min 40 deg, at the full-cap threshold): no reduction")
check(abs(eq_target.directivity_boost_cap(6.0, horizontal_avg=35.0, vertical_avg=45.0) - 5.4) < 1e-6,
      "Dayton Audio B652-AIR (min 35 deg): mild reduction")
check(eq_target.directivity_boost_cap(6.0, horizontal_avg=10.0, vertical_avg=40.0) == 3.0,
      "Polk Audio T15 (min 10 deg, at/below the floor): maximum reduction (half the cap)")
check(eq_target.directivity_boost_cap(6.0, horizontal_avg=1.0, vertical_avg=1.0) == 3.0,
      "reduction never exceeds the configured floor fraction")

# parse_index captures the new fields.
_DIR_META = {
    "Acme Wide Dispersion": {
        "brand": "Acme", "model": "Wide Dispersion", "type": "passive",
        "shape": "bookshelves", "default_measurement": "asr",
        "measurements": {"asr": {"origin": "ASR", "quality": "high", "estimates": {
            "directivity_horizontal_avg": 50.0, "directivity_vertical_avg": 50.0}}},
    },
}
dir_entries = spinorama.parse_index(_DIR_META)
check(dir_entries[0].directivity_horizontal_avg == 50.0
      and dir_entries[0].directivity_vertical_avg == 50.0,
      "parse_index captures directivity_horizontal_avg/vertical_avg")

# SpinoramaProvider.fetch_curve applies the effective cap end-to-end (curve fixture
# from the SpinoramaProvider end-to-end block above: a deep dip wanting ~8 dB boost).
good_directivity = replace(asr, directivity_horizontal_avg=50.0, directivity_vertical_avg=50.0)
poor_directivity = replace(asr, directivity_horizontal_avg=50.0, directivity_vertical_avg=10.0)
good_corr, good_clamp = sp_provider.fetch_curve(
    good_directivity, options=CorrectionOptions(slope_db_per_oct=0.0))
poor_corr, poor_clamp = sp_provider.fetch_curve(
    poor_directivity, options=CorrectionOptions(slope_db_per_oct=0.0))
poor_corr_off, poor_clamp_off = sp_provider.fetch_curve(
    poor_directivity, options=CorrectionOptions(slope_db_per_oct=0.0, directivity_limiting=False))
check(max(g for _, g in good_corr) <= 6.0 + 1e-6,
      "good-directivity speaker keeps the full boost cap")
check(max(g for _, g in poor_corr) <= 3.0 + 1e-6,
      "poor-directivity speaker gets a damped boost cap when limiting is on")
check(max(g for _, g in poor_corr_off) > 3.0 + 1e-6,
      "poor-directivity speaker keeps the full boost cap when limiting is off")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
