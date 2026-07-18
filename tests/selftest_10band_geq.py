"""Self-test for the 10-band ISO GEQ scaffold (Rumble 4.11).

Confirms the new ISO_10_BAND octave-band centres (31.5 Hz - 16 kHz) are
selectable in the Graphic EQ editor and correctly resample the current
curve, mirroring the existing 15/31-band scaffold behavior.

Run:  python tests/selftest_10band_geq.py
Uses Qt's offscreen platform plugin - no window is shown."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

app = QApplication([])

from rumble.ui.advanced_panel import ISO_10_BAND, GraphicEQEditor
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def step(label: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception:
        FAILURES.append(label)
        print(f"  FAIL  {label}\n{traceback.format_exc()}")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    theme = get_theme(DEFAULT_THEME_NAME)
    geq = GraphicEQEditor(theme)

    def ten_band_constant_matches_iso_octave_series() -> None:
        expect(ISO_10_BAND == [31.5, 63.0, 125.0, 250.0, 500.0, 1000.0,
                                2000.0, 4000.0, 8000.0, 16000.0],
               f"ISO_10_BAND should be the standard octave series, got {ISO_10_BAND}")

    step("ISO_10_BAND matches the standard 10-band octave series",
         ten_band_constant_matches_iso_octave_series)

    def ten_band_button_exists() -> None:
        expect(hasattr(geq, "btn_iso10"), "GraphicEQEditor must expose btn_iso10")
        expect(geq.btn_iso10.text().strip() == "10-Band",
               f"button text should be '10-Band', got {geq.btn_iso10.text()!r}")

    step("10-Band scaffold button exists", ten_band_button_exists)

    def ten_band_scaffold_resamples() -> None:
        geq.set_points_from_string("100 3; 1000 3")
        geq.btn_iso10.click()
        app.processEvents()
        pts = sorted((p[0], p[1]) for p in geq._points)
        expect(len(pts) == 10, f"10-band scaffold should give 10 points, got {len(pts)}")
        expect(all(abs(g - 3.0) < 1e-6 for _, g in pts),
               "a flat +3 dB curve must stay +3 dB at every ISO centre")
        expect(abs(pts[0][0] - 31.5) < 1e-6 and abs(pts[-1][0] - 16000.0) < 1e-6,
               "10-band centres span 31.5 Hz - 16 kHz")

    step("10-band scaffold resamples the current curve onto ISO octave centres",
         ten_band_scaffold_resamples)

    def existing_15_31_band_scaffolds_still_work() -> None:
        geq.set_points_from_string("100 2; 1000 2")
        geq.btn_iso15.click()
        app.processEvents()
        expect(len(geq._points) == 15, "15-band scaffold still gives 15 points")
        geq.btn_iso31.click()
        app.processEvents()
        expect(len(geq._points) == 31, "31-band scaffold still gives 31 points")

    step("existing 15/31-band scaffolds are unaffected by the new button",
         existing_15_31_band_scaffolds_still_work)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
