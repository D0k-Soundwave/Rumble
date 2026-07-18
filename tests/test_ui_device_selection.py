"""Test UI device selection - verify new profiles use first active device."""
import sys
sys.path.insert(0, r"H:\RUMBLE")

from pathlib import Path
from rumble.core.audio_devices import first_active_device_pattern
from rumble.core.profile_manager import ProfileManager
from rumble.ui.new_profile_dialog import NewProfileDialog
from PySide6.QtWidgets import QApplication

print("\n=== Testing NewProfileDialog Device Selection ===")

# Get the expected first active device
expected_device = first_active_device_pattern()
print(f"Expected first active device: '{expected_device}'")

# Create app
app = QApplication.instance() or QApplication([])

# Create and show the dialog
dialog = NewProfileDialog(theme={})

# Get the values as if user clicked OK without changing anything
values = dialog.values()
selected_device = values["device"]

print(f"Dialog device value: '{selected_device}'")

# Verify it's not "all"
assert selected_device != "all", f"Device should not be 'all', got '{selected_device}'"
print(f"[OK] Device is not 'all'")

# Verify it's the first active device
assert selected_device == expected_device, f"Expected '{expected_device}', got '{selected_device}'"
print(f"[OK] Device is first active device: '{selected_device}'")

# Now test that we can manually select "All"
dialog.device_combo.setCurrentIndex(dialog.device_combo.count() - 1)  # Select "All" at end
all_values = dialog.values()
all_device = all_values["device"]
print(f"When 'All' is selected in combo, dialog.values() returns device: '{all_device}'")

# The dialog should convert "all" back to first active device
assert all_device != "all", "Even when 'All' is selected in combo, should return first active device"
assert all_device == expected_device, f"Should be '{expected_device}', got '{all_device}'"
print(f"[OK] Selecting 'All' in combo is converted to first active device: '{all_device}'")

print("\n=== NewProfileDialog tests passed! ===")
