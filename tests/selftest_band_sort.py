"""Self-test: bands never re-order on their own while editing; the Re-order
button sorts them by frequency and renumbers so band 1 is the lowest."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rumble.core.apo_config import Filter
from rumble.ui.editor_panel import EditorPanel
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


def freqs(panel):
    return [row.filter.fc for row in panel._filter_rows]


def indices(panel):
    return [row._index for row in panel._filter_rows]


app = QApplication.instance() or QApplication([])
panel = EditorPanel(get_theme(DEFAULT_THEME_NAME))

# ── no automatic re-ordering ─────────────────────────────────────────
for fc in (100.0, 400.0, 1000.0):
    panel._add_filter_row(Filter(type="PK", fc=fc))
panel._add_filter_row(Filter(type="PK", fc=20.0))
panel._refresh_graph_and_raw()
check(freqs(panel) == [100.0, 400.0, 1000.0, 20.0],
      "adding a low band does not auto-sort")

row = panel._filter_rows[0]
row.spn_freq.setValue(5000.0)  # changed.emit -> refresh; must not move rows
check(freqs(panel) == [5000.0, 400.0, 1000.0, 20.0],
      "editing a frequency does not move the row")
check(indices(panel) == [1, 2, 3, 4], "band numbers untouched while editing")

# ── Re-order button sorts and renumbers ──────────────────────────────
panel.btn_reorder.click()
check(freqs(panel) == [20.0, 400.0, 1000.0, 5000.0],
      "Re-order sorts bands by frequency, lowest first")
check(indices(panel) == [1, 2, 3, 4], "band numbers renumbered 1..n")
check([f.fc for f in panel._collect_filters()] == [20.0, 400.0, 1000.0, 5000.0],
      "collected filters match sorted display order")

# ── clicking again with sorted bands is a no-op ──────────────────────
before = freqs(panel)
panel.btn_reorder.click()
check(freqs(panel) == before, "Re-order on sorted bands is a no-op")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
