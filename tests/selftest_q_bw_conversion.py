"""Self-test: RBJ Audio EQ Cookbook frequency-warped Q<->Bandwidth conversion
(Rumble 2.1). The exact functions diverge from the simplified/unwarped ones
as Fc approaches Nyquist and agree closely far from it; FilterRow's Q/BW
display uses the exact, sample-rate-aware conversion."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core.eq_math import bw_to_q, bw_to_q_exact, q_to_bw, q_to_bw_exact

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


SR = 48000

# ── low Fc: exact and simplified nearly coincide ─────────────────────
for q in (0.707, 1.41, 4.0):
    diff = abs(q_to_bw(q) - q_to_bw_exact(q, 100.0, SR))
    check(diff < 0.001, f"q_to_bw_exact ~= q_to_bw at low Fc=100Hz, Q={q} (diff={diff:.6f})")

# ── high Fc (approaching Nyquist): the two formulas visibly diverge,
#    and the divergence grows monotonically closer to Nyquist ────────
divergences = []
for frac in (0.3, 0.4, 0.45):
    fc = SR * frac
    diff = abs(q_to_bw(1.41) - q_to_bw_exact(1.41, fc, SR))
    divergences.append(diff)
    check(diff > 0.01, f"q_to_bw_exact diverges from q_to_bw near Nyquist (fc={frac}*SR, diff={diff:.4f})")
check(divergences[0] < divergences[1] < divergences[2],
      "divergence from the simplified formula grows monotonically as Fc approaches Nyquist")

# ── round-trip: bw_to_q_exact(q_to_bw_exact(q)) == q at fixed fc/sr ───
for frac in (0.05, 0.2, 0.4):
    fc = SR * frac
    for q in (0.3, 0.707, 1.41, 4.32, 30.0):
        bw = q_to_bw_exact(q, fc, SR)
        q_rt = bw_to_q_exact(bw, fc, SR)
        check(abs(q_rt - q) < 1e-6, f"round-trip exact Q={q} at fc={frac}*SR")

# ── verify the exact formula directly against the Audio EQ Cookbook
#    relation (not just against our own paraphrase of it) ────────────
fc, bw = 12000.0, 1.0
w0 = 2 * math.pi * fc / SR
warp = w0 / math.sin(w0)
expected_q = 1.0 / (2.0 * math.sinh(math.log(2.0) / 2.0 * bw * warp))
check(abs(bw_to_q_exact(bw, fc, SR) - expected_q) < 1e-9,
      "bw_to_q_exact matches the RBJ Cookbook relation computed independently")

# ── edge case: Fc at exactly Nyquist must not crash (extreme warp) ───
try:
    q_edge = bw_to_q_exact(1.0, SR * 0.5, SR)
    check(q_edge > 0.0, "bw_to_q_exact handles Fc at exact Nyquist without crashing")
except OverflowError:
    check(False, "bw_to_q_exact handles Fc at exact Nyquist without crashing")

# ── FilterRow uses the sample-rate-aware exact conversion ─────────────
from PySide6.QtWidgets import QApplication

from rumble.core.apo_config import Filter
from rumble.ui.filter_row import FilterRow
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

app = QApplication.instance() or QApplication([])
theme = get_theme(DEFAULT_THEME_NAME)

fc_high = SR * 0.45
filt = Filter(type="PK", fc=fc_high, gain=3.0, q=1.41)
row = FilterRow(filt, 1, theme)
row.set_bw_mode(True)
expected_exact = q_to_bw_exact(1.41, fc_high, SR)
expected_simple = q_to_bw(1.41)
check(abs(row.spn_q.value() - expected_exact) < 0.005,
      "FilterRow BW display uses the sample-rate-aware exact conversion")
check(abs(expected_exact - expected_simple) > 0.05,
      "the exact and simplified formulas meaningfully diverge at this Fc/SR (sanity check)")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
