"""Verify profile filter dropdown: All, User, Defaults, and custom tags."""
import sys
sys.path.insert(0, r"H:\RUMBLE")

from rumble import __main__ as rumble_main
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

orig_exec = QApplication.exec

def patched_exec(*args, **kwargs):
    app = QApplication.instance()

    def verify_filters():
        from rumble.ui.main_window import MainWindow

        main_window = None
        for w in app.topLevelWidgets():
            if isinstance(w, MainWindow):
                main_window = w
                break

        if main_window:
            profile_panel = main_window.profile_panel
            print(f"\n=== Profile Filter Dropdown ===")

            combo = profile_panel.combo_filter
            filters = [combo.itemText(i) for i in range(combo.count())]
            print(f"Number of filter options: {len(filters)}")

            expected_base = ["All", "User", "Defaults"]
            actual_base = filters[:3]

            for i, text in enumerate(filters):
                print(f"  {i}: {text}")

            print(f"\nExpected base filters: {expected_base}")
            print(f"Actual base filters:   {actual_base}")

            if actual_base == expected_base:
                print("[OK] Filter dropdown is correct!")
            else:
                print("[ERROR] Filter dropdown mismatch!")
                for i, (exp, act) in enumerate(zip(expected_base, actual_base)):
                    status = "OK" if exp == act else "MISMATCH"
                    print(f"  {i}: Expected '{exp}', got '{act}' [{status}]")

        app.quit()

    QTimer.singleShot(2000, verify_filters)
    return orig_exec()

QApplication.exec = patched_exec
sys.exit(rumble_main.main())
