"""Visual verification: launch RUMBLE and verify device selection in UI."""
import sys
sys.path.insert(0, r"H:\RUMBLE")

from rumble.core.audio_devices import first_active_device_pattern
from rumble import __main__ as rumble_main
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

expected_device = first_active_device_pattern()
print(f"Expected first active device: '{expected_device}'")

orig_exec = QApplication.exec

def patched_exec(*args, **kwargs):
    app = QApplication.instance()

    def verify_ui():
        from rumble.ui.main_window import MainWindow
        from rumble.ui.new_profile_dialog import NewProfileDialog

        # Get main window
        main_window = None
        for w in app.topLevelWidgets():
            if isinstance(w, MainWindow):
                main_window = w
                break

        if main_window:
            # Open new profile dialog
            dialog = NewProfileDialog(theme=main_window.theme)

            # Check device combo
            print(f"\n=== New Profile Dialog Device Combo ===")
            print(f"Number of devices in combo: {dialog.device_combo.count()}")

            for i in range(dialog.device_combo.count()):
                data = dialog.device_combo.itemData(i)
                text = dialog.device_combo.itemText(i)
                is_current = i == dialog.device_combo.currentIndex()
                marker = " [SELECTED]" if is_current else ""
                print(f"  {i}: {text:30s} (data: '{data}'){marker}")

            # Verify default selection
            current_data = str(dialog.device_combo.currentData())
            current_text = dialog.device_combo.currentText()
            print(f"\nDefault selected: '{current_text}' (data: '{current_data}')")

            if current_data != "all":
                print(f"[OK] Default device is NOT 'all', it is: '{current_data}'")
            else:
                print(f"[ERROR] Default device is still 'all'!")

            if current_data == expected_device:
                print(f"[OK] Default device is first active device: '{expected_device}'")
            else:
                print(f"[ERROR] Default device is not first active device!")
                print(f"       Expected: '{expected_device}'")
                print(f"       Got:      '{current_data}'")

            # Test values() method
            values = dialog.values()
            returned_device = values["device"]
            print(f"\ndialog.values()['device'] returns: '{returned_device}'")

            if returned_device == expected_device:
                print(f"[OK] dialog.values() returns first active device")
            else:
                print(f"[ERROR] dialog.values() doesn't return first active device!")

        app.quit()

    QTimer.singleShot(2000, verify_ui)
    return orig_exec()

QApplication.exec = patched_exec
sys.exit(rumble_main.main())
