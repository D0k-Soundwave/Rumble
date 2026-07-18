"""Self-test: "Custom IIR" is hidden from the filter-type dropdown for new
authoring (Rumble 1.2) — decision (a) from the guide: hide until a
coefficient editor exists (PLANNED_WORK #16). An existing IIR filter loaded
from an imported/round-tripped profile must still display correctly (its
dropdown still shows "Custom IIR" as the current, if no longer newly
selectable, type) and must still round-trip through parse/render verbatim."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rumble.core.apo_config import (
    ChannelSection, Filter, ProfileConfig, parse_config, render_config_body,
)
from rumble.ui.filter_row import FilterRow
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


app = QApplication.instance() or QApplication([])
theme = get_theme(DEFAULT_THEME_NAME)


def dropdown_types(row: FilterRow) -> list[str]:
    return [row.cmb_type.itemData(i) for i in range(row.cmb_type.count())]


# ── new authoring: IIR is never offered ───────────────────────────────
new_row = FilterRow(Filter(type="PK", fc=1000.0, gain=0.0, q=1.0), 1, theme)
check("IIR" not in dropdown_types(new_row),
      "a new (non-IIR) band's type dropdown never offers Custom IIR")

# ── round-trip display: an existing IIR filter still shows correctly ──
iir_filt = Filter(enabled=True, type="IIR", iir_order=2,
                   iir_coefficients="1.0,0.0,0.0,1.0,-0.5,0.25")
iir_row = FilterRow(iir_filt, 1, theme)
check("IIR" in dropdown_types(iir_row),
      "a row already bound to an IIR filter still lists Custom IIR (so it "
      "can display, not silently misrepresent, the loaded type)")
check(iir_row.cmb_type.currentData() == "IIR",
      "the IIR row's dropdown selection matches the loaded filter's type")
check(not iir_row.spn_gain.isEnabled() and not iir_row.spn_q.isEnabled()
      and not iir_row.spn_freq.isEnabled(),
      "Freq/Gain/Q stay disabled (read-only) for an IIR band")

# ── full profile round-trip: an IIR filter survives render + re-parse ─
section = ChannelSection(channel="all", filters=[
    Filter(enabled=True, type="IIR", iir_order=4,
           iir_coefficients="0.5,-0.25,0.125,1.0,-0.6,0.3,0.1"),
])
config = ProfileConfig(device="all", preamp=0.0, channel_sections=[section])
text = render_config_body(config)
reparsed = parse_config(text)

check(len(reparsed.channel_sections) == 1
      and len(reparsed.channel_sections[0].filters) == 1,
      "round-tripped profile still has exactly one IIR filter")
if reparsed.channel_sections and reparsed.channel_sections[0].filters:
    rf = reparsed.channel_sections[0].filters[0]
    check(rf.type == "IIR", "round-tripped filter type is still IIR")
    check(rf.iir_order == 4, "round-tripped IIR order is preserved")
    check(rf.iir_coefficients == "0.5,-0.25,0.125,1.0,-0.6,0.3,0.1",
          "round-tripped IIR coefficients are preserved verbatim")
    check(rf.enabled is True, "round-tripped IIR filter stays enabled")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
