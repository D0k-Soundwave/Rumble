"""Self-test: editor-wide Q/BW toggle — label switch, value conversion,
storage semantics, and graph math honouring BW-stored filters."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rumble.core.apo_config import Filter
from rumble.core.eq_math import (
    bw_to_q, bw_to_q_exact, filter_response, frequency_array, q_to_bw, q_to_bw_exact,
)

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


app = QApplication.instance() or QApplication([])

# ── conversion math ──────────────────────────────────────────────────
check(abs(q_to_bw(2 ** 0.5) - 1.0) < 1e-6, "q_to_bw: Q sqrt(2) -> 1 octave")
check(abs(bw_to_q(1.0) - 2 ** 0.5) < 1e-6, "bw_to_q: 1 octave -> Q sqrt(2)")
for q in (0.3, 0.707, 1.41, 4.32, 30.0):
    check(abs(bw_to_q(q_to_bw(q)) - q) < 1e-9, f"round-trip Q={q}")

# ── graph math honours BW-stored filters ─────────────────────────────
freqs = frequency_array()
f_q = Filter(type="PK", fc=1000.0, gain=6.0, q=bw_to_q(2.0))
f_bw = Filter(type="PK", fc=1000.0, gain=6.0, q=1.0, use_bw=True, bw_oct=2.0)
resp_q = filter_response(f_q, freqs)
resp_bw = filter_response(f_bw, freqs)
check(abs(resp_q - resp_bw).max() < 1e-9,
      "filter_response: BW-stored band matches equivalent Q band")

# ── FilterRow display + edit semantics ───────────────────────────────
from rumble.ui.filter_row import BandGridHeader, FilterRow
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

theme = get_theme(DEFAULT_THEME_NAME)

filt = Filter(type="PK", fc=1000.0, gain=3.0, q=1.41)
row = FilterRow(filt, 1, theme)
check(abs(row.spn_q.value() - 1.41) < 1e-6, "Q mode shows stored Q")

row.set_bw_mode(True)
check(abs(row.spn_q.value() - q_to_bw_exact(1.41, filt.fc, row._sample_rate)) < 0.005,
      "BW mode shows converted value")
check(filt.use_bw is False, "toggle alone does not rewrite the filter")

row.spn_q.setValue(2.0)  # user edit in BW mode
check(filt.use_bw is True and abs(filt.bw_oct - 2.0) < 1e-9,
      "edit in BW mode stores BW")
check(abs(filt.q - bw_to_q_exact(2.0, filt.fc, row._sample_rate)) < 1e-9,
      "edit keeps Q mirror in sync")

row.set_bw_mode(False)
check(abs(row.spn_q.value() - bw_to_q_exact(2.0, filt.fc, row._sample_rate)) < 0.005,
      "back to Q mode shows converted Q")

# AP supports Q but not BW: stays in Q even in BW mode
ap = Filter(type="AP", fc=500.0, q=0.7)
ap_row = FilterRow(ap, 2, theme, bw_mode=True)
check(abs(ap_row.spn_q.value() - 0.7) < 1e-6, "non-BW type keeps showing Q")
ap_row.spn_q.setValue(0.9)
check(ap.use_bw is False and abs(ap.q - 0.9) < 1e-9,
      "non-BW type edit stores Q even in BW mode")

# ── header label follows mode ────────────────────────────────────────
hdr = BandGridHeader()
check(hdr._lbl_q.text() == "Q", "header label starts as Q")
hdr.set_bw_mode(True)
check(hdr._lbl_q.text() == "BW", "header label switches to BW")

# ── serialisation follows storage ────────────────────────────────────
check("BW Oct 2.00" in filt.to_line(1), "BW-stored filter writes BW Oct")
check("Q 0.90" in ap.to_line(2), "Q-stored filter writes Q")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
