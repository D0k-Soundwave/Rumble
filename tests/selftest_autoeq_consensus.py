"""Self-test: Rumble 3.1 — cross-provider consensus + AutoEQ multi-measurement
averaging. The default AutoEQ import path now averages every measurement of
the same device (across sources/rigs) into one consensus curve instead of
returning just the first result, and exposes a per-frequency min/max spread.

Real INDEX.md data confirms plain exact-string matching on AutoEqEntry.name
already groups the same device across sources with no fuzzy matching needed
(e.g. "Sennheiser HD 600" appears identically for 8 different sources/rigs in
data/autoeq_index.md)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core import autoeq_merge
from rumble.core.autoeq import AutoEqEntry
import rumble.core.autoeq as autoeq_mod
from rumble.core.measurements.autoeq_provider import AutoEqProvider
from rumble.core.measurements.federated import FederatedCatalog

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


def entry(name, source, path="x/over-ear/Foo"):
    return AutoEqEntry(name=name, source=source, rig="", path=path, device_type="Over-ear")


# ── normalize_device_key ──────────────────────────────────────────────────
check(autoeq_merge.normalize_device_key("  Sennheiser HD 600 ")
      == autoeq_merge.normalize_device_key("sennheiser hd 600"),
      "normalize_device_key folds case/leading-trailing whitespace")
check(autoeq_merge.normalize_device_key("Sennheiser  HD   600")
      == autoeq_merge.normalize_device_key("Sennheiser HD 600"),
      "normalize_device_key folds internal whitespace runs")

# ── spread_curves ──────────────────────────────────────────────────────────
a = [(100.0, 0.0), (1000.0, 2.0), (10000.0, -1.0)]
b = [(100.0, 1.0), (1000.0, -1.0), (10000.0, -1.0)]
c = [(100.0, -1.0), (1000.0, 0.0), (10000.0, 0.5)]
spread = autoeq_merge.spread_curves([a, b, c])
by_freq = {f: (lo, hi) for f, lo, hi in spread}
check(by_freq[100.0] == (-1.0, 1.0), "spread_curves: min/max at 100 Hz")
check(by_freq[1000.0] == (-1.0, 2.0), "spread_curves: min/max at 1000 Hz")
check(by_freq[10000.0] == (-1.0, 0.5), "spread_curves: min/max at 10000 Hz")

single_spread = autoeq_merge.spread_curves([[(100.0, 1.5), (1000.0, -2.0)]])
check(all(lo == hi for _, lo, hi in single_spread),
      "spread_curves: a single curve has zero spread everywhere")

# ── max_spread_db ──────────────────────────────────────────────────────────
check(autoeq_merge.max_spread_db([(100.0, -1.0, 1.0), (1000.0, 0.0, 0.5)]) == 2.0,
      "max_spread_db picks the worst per-frequency spread")
check(autoeq_merge.max_spread_db([]) == 0.0, "max_spread_db of no data is 0.0")

# ── FederatedCatalog.same_device_entries groups by exact name, per provider ─
provider = AutoEqProvider(Path("."))
catalog = FederatedCatalog([provider])
hd600_a = entry("Sennheiser HD 600", "oratory1990")
hd600_b = entry("Sennheiser HD 600", "crinacle")
hd600_c = entry("Sennheiser HD 600", "Rtings")
other = entry("Sennheiser HD 650", "oratory1990")
wrapped = [catalog._wrap(provider, e) for e in (hd600_a, hd600_b, hd600_c, other)]

siblings = catalog.same_device_entries(wrapped, wrapped[0])
check(len(siblings) == 3, "same_device_entries finds all 3 same-name entries")
check({e.source for e in siblings} == {"oratory1990", "crinacle", "Rtings"},
      "same_device_entries preserves each entry's distinct source")
check(all(s.name == "Sennheiser HD 600" for s in siblings) and other not in siblings,
      "same_device_entries excludes a differently-named device")

lone = catalog.same_device_entries(wrapped, wrapped[3])
check(len(lone) == 1, "a device with only one measurement has no siblings besides itself")

# ── default import averages, rather than returning the first result ──────
curves_by_source = {
    "SourceA": [(100.0, 0.0), (1000.0, 3.0)],
    "SourceB": [(100.0, 2.0), (1000.0, -1.0)],
    "SourceC": [(100.0, -2.0), (1000.0, 1.0)],
}


def fake_fetch_graphic(e):
    payload = curves_by_source[e.source]
    return "; ".join(f"{f:g} {g:.1f}" for f, g in payload)


multi_a = entry("Sennheiser HD 600", "SourceA")
multi_b = entry("Sennheiser HD 600", "SourceB")
multi_c = entry("Sennheiser HD 600", "SourceC")
multi_wrapped = [catalog._wrap(provider, e) for e in (multi_a, multi_b, multi_c)]

orig_fetch_graphic = autoeq_mod.fetch_graphic
autoeq_mod.fetch_graphic = fake_fetch_graphic
try:
    curve, spread2, sources, clamp = catalog.fetch_consensus_curve(multi_wrapped)
finally:
    autoeq_mod.fetch_graphic = orig_fetch_graphic

curve_by_freq = dict(curve)
check(curve_by_freq[100.0] == 0.0, "consensus curve averages 100 Hz (0+2-2)/3, not SourceA's 0.0 by coincidence alone")
check(abs(curve_by_freq[1000.0] - 1.0) < 1e-9,
      "consensus curve at 1000 Hz is the 3-way average (1.0), not SourceA's first-result value (3.0)")
check(set(sources) == {"SourceA", "SourceB", "SourceC"},
      "consensus curve reports every contributing source")
check(autoeq_merge.max_spread_db(spread2) == 4.0,
      "consensus spread reports the worst per-frequency disagreement (100 Hz: 2 - (-2) = 4 dB)")
check(clamp.points_clamped_boost == 0 and clamp.points_clamped_cut == 0,
      "AutoEQ consensus curves report no clamping (already-finished corrections)")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
