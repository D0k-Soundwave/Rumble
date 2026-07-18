"""Selftest: Settings -> Listeners page's ISO 226 loudness-compensation
section (enable toggle, reference phon, calibrate button)."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from rumble.core.profile_manager import DEFAULT_SETTINGS
from rumble.ui.settings_dialog import SettingsDialog
from rumble.ui.theme.tokens import THEMES, DEFAULT_THEME_NAME

_app = QApplication.instance() or QApplication([])


def test_prefills_from_settings():
    with tempfile.TemporaryDirectory() as tmp:
        settings = dict(DEFAULT_SETTINGS)
        settings["iso226_loudness_enabled"] = True
        settings["iso226_reference_phon"] = 75.0
        settings["iso226_reference_volume_db"] = -12.0
        dlg = SettingsDialog(settings, THEMES[DEFAULT_THEME_NAME], Path(tmp))
        assert dlg.chk_iso226_enabled.isChecked() is True
        assert dlg.spin_iso226_phon.value() == 75.0
        assert "-12.0" in dlg.lbl_iso226_reference.text()


def test_defaults_show_uncalibrated():
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], Path(tmp))
        assert dlg.chk_iso226_enabled.isChecked() is False
        assert dlg.spin_iso226_phon.value() == 80.0
        assert "Not yet calibrated" in dlg.lbl_iso226_reference.text()


def test_save_writes_enabled_and_phon():
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], Path(tmp))
        dlg.chk_iso226_enabled.setChecked(True)
        dlg.spin_iso226_phon.setValue(70.0)
        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()
        assert emitted[0]["iso226_loudness_enabled"] is True
        assert emitted[0]["iso226_reference_phon"] == 70.0


def test_calibrate_button_emits_signal():
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], Path(tmp))
        fired = []
        dlg.calibrate_loudness_requested.connect(lambda: fired.append(True))
        dlg.btn_iso226_calibrate.click()
        assert fired == [True]


def test_note_loudness_reference_calibrated_updates_label_and_working_dict():
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], Path(tmp))
        dlg.note_loudness_reference_calibrated(-8.5)
        assert "-8.5" in dlg.lbl_iso226_reference.text()
        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()
        assert emitted[0]["iso226_reference_volume_db"] == -8.5


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
