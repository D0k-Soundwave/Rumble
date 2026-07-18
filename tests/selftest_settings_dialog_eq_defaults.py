"""Self-test for the Settings > EQ Defaults spinorama tilt/smoothing controls.

Run:  python tests/selftest_settings_dialog_eq_defaults.py
Uses Qt's offscreen platform plugin — no window is shown. Exits non-zero if
any check fails.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

app = QApplication([])

from rumble.core.profile_manager import DEFAULT_SETTINGS
from rumble.ui.settings_dialog import SettingsDialog
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_eq_defaults_"))
    theme = get_theme(DEFAULT_THEME_NAME)

    print("[1] controls seed from the current settings")
    settings = dict(DEFAULT_SETTINGS)
    settings["spinorama_default_tilt"] = -1.2
    settings["spinorama_smoothing_oct"] = 0.5
    settings["sample_rate"] = 96000
    dlg = SettingsDialog(settings, theme, tmp)
    check(hasattr(dlg, "spin_default_tilt"), "spin_default_tilt exists")
    check(hasattr(dlg, "spin_smoothing_oct"), "spin_smoothing_oct exists")
    check(hasattr(dlg, "cmb_sample_rate"), "cmb_sample_rate exists")
    check(abs(dlg.spin_default_tilt.value() - (-1.2)) < 1e-6,
          "tilt spin seeds from settings")
    check(abs(dlg.spin_smoothing_oct.value() - 0.5) < 1e-6,
          "smoothing spin seeds from settings")
    check(dlg.cmb_sample_rate.currentData() == 96000,
          "sample-rate combo seeds from settings")

    print("\n[2] applying emits the edited values")
    captured: list[dict] = []
    dlg.settings_saved.connect(lambda s: captured.append(s))
    dlg.spin_default_tilt.setValue(-2.0)
    dlg.spin_smoothing_oct.setValue(0.75)
    dlg.cmb_sample_rate.setCurrentIndex(dlg.cmb_sample_rate.findData(44100))
    dlg._apply_now()
    check(len(captured) == 1, "settings_saved fired once")
    if captured:
        check(abs(captured[0]["spinorama_default_tilt"] - (-2.0)) < 1e-6,
              "edited tilt value is saved")
        check(abs(captured[0]["spinorama_smoothing_oct"] - 0.75) < 1e-6,
              "edited smoothing value is saved")
        check(captured[0]["sample_rate"] == 44100,
              "edited sample rate is saved")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
