"""Self-test: Rumble 3.2 — quality-weighted merging of multi-measurement
speaker curves. average_curves() gains an optional weights= parameter
(default None = unchanged flat average); SpinoramaProvider derives a weight
from each measurement's quality tier ("high"/"medium"/"low"/"unknown" — the
real categorical values found in data/spinorama_index.json, confirmed before
writing this, not a numeric range guessed from nothing); FederatedCatalog's
Rumble-3.1 fetch_consensus_curve() now weights by provider.entry_weight()."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core import autoeq_merge, spinorama
from rumble.core.spinorama import SpinoramaEntry
from rumble.core.measurements.spinorama_provider import SpinoramaProvider
from rumble.core.measurements.federated import FederatedCatalog
from rumble.core.measurements.base import CorrectionOptions

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── average_curves(weights=) ──────────────────────────────────────────────
low = [(100.0, 0.0), (1000.0, 0.0)]
high = [(100.0, 4.0), (1000.0, -4.0)]

flat = autoeq_merge.average_curves([low, high])
check(dict(flat)[100.0] == 2.0, "average_curves with no weights is a plain 50/50 average")

weighted = autoeq_merge.average_curves([low, high], weights=[1.0, 3.0])
# 3:1 weighting toward `high`: (0*1 + 4*3) / 4 = 3.0
check(abs(dict(weighted)[100.0] - 3.0) < 1e-9,
      "a 3:1 weight ratio pulls the average toward the heavier curve")
check(abs(dict(weighted)[1000.0] - (-3.0)) < 1e-9,
      "weighting applies consistently across every frequency point")

unweighted_default = autoeq_merge.average_curves([low, high], weights=None)
check(unweighted_default == flat, "weights=None is identical to the pre-Rumble-3.2 flat average")

# ── SpinoramaProvider.entry_weight() derives from quality tier ───────────
provider = SpinoramaProvider(Path("."))


def make_entry(quality):
    return SpinoramaEntry(name="Test Speaker", brand="Test", model="Speaker",
                          source="X", measurement_key="x", quality=quality)


w_high = provider.entry_weight(make_entry("high"))
w_medium = provider.entry_weight(make_entry("medium"))
w_low = provider.entry_weight(make_entry("low"))
w_unknown = provider.entry_weight(make_entry("unknown"))
w_missing = provider.entry_weight(make_entry(""))

check(w_high > w_medium > w_low, "quality weight is strictly ordered high > medium > low")
check(w_unknown == w_low, "an 'unknown' quality tier carries no more confidence than 'low'")
check(w_missing == w_low, "a blank/missing quality string falls back to the lowest weight")

# ── FederatedCatalog.fetch_consensus_curve weights by quality (Rumble 3.1+3.2) ─
catalog = FederatedCatalog([provider])
entry_hi = make_entry("high")
entry_lo = SpinoramaEntry(name="Test Speaker", brand="Test", model="Speaker",
                          source="Y", measurement_key="y", quality="low")
wrapped = [catalog._wrap(provider, e) for e in (entry_hi, entry_lo)]

curves_by_source = {"X": [(100.0, 0.0), (1000.0, 0.0)], "Y": [(100.0, 6.0), (1000.0, -6.0)]}


def fake_fetch_curve(entry, *, options=None):
    from rumble.core.eq_target import ClampSummary
    return curves_by_source[entry.source], ClampSummary()


orig = provider.fetch_curve
provider.fetch_curve = fake_fetch_curve
try:
    curve, spread, sources, clamp = catalog.fetch_consensus_curve(
        wrapped, options=CorrectionOptions())
finally:
    provider.fetch_curve = orig

by_freq = dict(curve)
# high-quality "X" (0.0) should pull the merge closer to itself than a flat
# 50/50 average would (which would be 3.0).
check(by_freq[100.0] < 3.0,
      "quality-weighted consensus pulls toward the higher-quality measurement, "
      "not a flat 50/50 average")
check(by_freq[100.0] > 0.0, "the lower-quality measurement still contributes something")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
