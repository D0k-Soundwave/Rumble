"""Selftest: Settings -> Listeners page (naming, default slot, warn toggle)."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from rumble.core import listener_slots
from rumble.core.profile_manager import DEFAULT_SETTINGS
from rumble.ui.settings_dialog import SettingsDialog
from rumble.ui.theme.tokens import THEMES, DEFAULT_THEME_NAME

_app = QApplication.instance() or QApplication([])


def test_listeners_category_present():
    from rumble.ui.settings_dialog import CATEGORIES
    assert "Listeners" in CATEGORIES


def test_listeners_page_prefills_existing_names():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        listener_slots.save_slot_names(data_dir, {1: "Rob", 3: "Sam"})
        dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], data_dir)
        assert dlg._listener_name_edits[1].text() == "Rob"
        assert dlg._listener_name_edits[2].text() == ""
        assert dlg._listener_name_edits[3].text() == "Sam"


def test_save_writes_names_and_default_slot_and_warn_toggle():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], data_dir)
        dlg._listener_name_edits[2].setText("Sam")
        idx = dlg.combo_default_listener.findData(2)
        dlg.combo_default_listener.setCurrentIndex(idx)
        dlg.chk_warn_unsaved.setChecked(False)

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()

        assert emitted[0]["default_listener_slot"] == 2
        assert emitted[0]["warn_unsaved_changes"] is False
        assert listener_slots.load_slot_names(data_dir) == {2: "Sam"}


def test_listener_correction_toggle_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], Path(tmp))
        assert dlg.chk_listener_correction.isChecked()   # default on
        dlg.chk_listener_correction.setChecked(False)

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()

        assert emitted[0]["listener_correction_enabled"] is False


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
