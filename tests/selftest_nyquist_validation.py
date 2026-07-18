"""Self-test: Fc spinbox enforces a dynamic Nyquist ceiling (Rumble 1.1) —
hard-clamped at 0.5x the active sample rate, soft-warned from 0.45x."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rumble.core.apo_config import Filter
from rumble.ui.editor_panel import EditorPanel
from rumble.ui.filter_row import FilterRow
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


app = QApplication.instance() or QApplication([])
theme = get_theme(DEFAULT_THEME_NAME)

for sample_rate in (44100, 48000):
    nyquist = 0.5 * sample_rate
    warn_at = 0.45 * sample_rate

    filt = Filter(type="PK", fc=1000.0, gain=0.0, q=1.0)
    row = FilterRow(filt, 1, theme)
    row.set_sample_rate(sample_rate)

    check(row.spn_freq.maximum() <= nyquist + 1e-6,
          f"[{sample_rate} Hz] spinbox max is capped at Nyquist")

    # Hard clamp: typing at/above Nyquist is rejected/clamped to the max.
    row.spn_freq.setValue(nyquist + 500.0)
    check(row.spn_freq.value() <= nyquist + 1e-6,
          f"[{sample_rate} Hz] value at/above Nyquist is clamped")
    check(filt.fc <= nyquist + 1e-6,
          f"[{sample_rate} Hz] underlying filter.fc is clamped too")

    # Soft warning zone: between 0.45x and 0.5x, no hard clamp but a
    # visible warning state.
    row.spn_freq.setValue(warn_at + (nyquist - warn_at) / 2.0)
    check(row.property("class") == "band-column",
          f"[{sample_rate} Hz] row class untouched by Fc warning (scoped to the field)")
    check(row.spn_freq.property("class") == "warning",
          f"[{sample_rate} Hz] Fc in the 0.45x-0.5x band shows a soft warning")

    # Well below the warning threshold: no warning.
    row.spn_freq.setValue(1000.0)
    check(row.spn_freq.property("class") != "warning",
          f"[{sample_rate} Hz] Fc well below threshold has no warning")

# ── revalidation on sample-rate change ───────────────────────────────
# A band that's safe at 48000 Hz (below its 21600 Hz warn threshold) can
# fall into the warning zone once the rate drops to 44100 Hz (whose warn
# threshold, 19845 Hz, and Nyquist, 22050 Hz, are both lower).
filt2 = Filter(type="PK", fc=20500.0, gain=0.0, q=1.0)
row2 = FilterRow(filt2, 1, theme)
row2.set_sample_rate(48000)
check(row2.spn_freq.property("class") != "warning",
      "20500 Hz is not warned at 48000 Hz sample rate")

row2.set_sample_rate(44100)
check(row2.spn_freq.property("class") == "warning",
      "revalidation on sample-rate change warns a now-borderline Fc")

# A band whose Fc is only valid because of the higher rate must be
# hard-clamped once the rate drop puts it at/above the new Nyquist.
filt3 = Filter(type="PK", fc=23000.0, gain=0.0, q=1.0)  # < 24000 (48k) but > 22050 (44.1k)
row3 = FilterRow(filt3, 1, theme)
row3.set_sample_rate(48000)
check(row3.spn_freq.value() <= 0.5 * 48000 + 1e-6,
      "23000 Hz is untouched (valid) at 48000 Hz sample rate")

row3.set_sample_rate(44100)
check(row3.spn_freq.value() <= 0.5 * 44100 + 1e-6,
      "revalidation on sample-rate change clamps a now-invalid Fc")
check(filt3.fc <= 0.5 * 44100 + 1e-6,
      "revalidation on sample-rate change clamps the underlying filter.fc")

# ── EditorPanel.set_sample_rate() revalidates every existing band ────
# Not just the row that's currently being typed into — every band already
# on the strip must be re-checked against the new rate's Nyquist ceiling.
panel = EditorPanel(get_theme(DEFAULT_THEME_NAME))
panel._add_filter_row(Filter(type="PK", fc=20500.0, gain=0.0, q=1.0))  # safe at 48k
panel._add_filter_row(Filter(type="PK", fc=23000.0, gain=0.0, q=1.0))  # safe at 48k, not 44.1k

panel.set_sample_rate(44100)
row_a, row_b = panel._filter_rows
check(row_a.spn_freq.property("class") == "warning",
      "EditorPanel.set_sample_rate revalidates band 1 into the warning zone")
check(row_b.filter.fc <= 0.5 * 44100 + 1e-6,
      "EditorPanel.set_sample_rate revalidates band 2, clamping it at the new Nyquist")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
