"""Selftest: header/tray Listener Slot selector wiring (offscreen Qt)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from rumble.core import listener_slots
from rumble.ui.profile_panel import ProfilePanel
from rumble.ui.theme.tokens import THEMES, DEFAULT_THEME_NAME

_app = QApplication.instance() or QApplication([])


def test_profile_panel_listener_combo_reflects_active_slot():
    panel = ProfilePanel(THEMES[DEFAULT_THEME_NAME])
    panel.set_listener_slots({1: "Rob", 2: "Sam"}, active_slot=2)
    assert panel.combo_listener.currentData() == 2
    assert panel.combo_listener.currentText() == "Sam"


def test_profile_panel_listener_combo_falls_back_to_default_label():
    panel = ProfilePanel(THEMES[DEFAULT_THEME_NAME])
    panel.set_listener_slots({}, active_slot=1)
    assert panel.combo_listener.currentText() == "Listener 1"


def test_profile_panel_emits_listener_slot_changed():
    panel = ProfilePanel(THEMES[DEFAULT_THEME_NAME])
    panel.set_listener_slots({1: "Rob", 2: "Sam", 3: "", 4: ""}, active_slot=1)
    seen = []
    panel.listener_slot_changed.connect(seen.append)
    idx = panel.combo_listener.findData(2)
    panel.combo_listener.setCurrentIndex(idx)
    assert seen == [2]


from rumble.ui.toast_menu import ToastMenu
from rumble.ui.tray_icon import TrayIcon
from PySide6.QtGui import QIcon


def test_toast_menu_listener_buttons_reflect_active_slot():
    toast = ToastMenu(THEMES[DEFAULT_THEME_NAME])
    toast.set_listeners({1: "Rob", 2: "Sam"}, active_slot=2)
    assert toast._listener_buttons[2].isChecked()
    assert not toast._listener_buttons[1].isChecked()
    assert toast._listener_buttons[2].text() == "Sam"
    assert toast._listener_buttons[3].text() == "Listener 3"


def test_toast_menu_emits_listener_slot_selected():
    toast = ToastMenu(THEMES[DEFAULT_THEME_NAME])
    toast.set_listeners({}, active_slot=1)
    seen = []
    toast.listener_slot_selected.connect(seen.append)
    toast._listener_buttons[3].click()
    assert seen == [3]
    assert toast._listener_buttons[3].isChecked()
    assert not toast._listener_buttons[1].isChecked()


def test_tray_icon_set_listeners_passes_through():
    tray = TrayIcon(QIcon(), THEMES[DEFAULT_THEME_NAME])
    seen = []
    tray.listener_slot_selected.connect(seen.append)
    tray.set_listeners({1: "Rob"}, active_slot=1)
    assert tray._toast._listener_buttons[1].text() == "Rob"
    tray._toast._listener_buttons[2].click()
    assert seen == [2]


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
