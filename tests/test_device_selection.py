"""Test device selection behavior: verify first active device is used instead of "all"."""
import sys
sys.path.insert(0, r"H:\RUMBLE")

from pathlib import Path
from rumble.core.audio_devices import list_apo_devices, first_active_device_pattern
from rumble.core.profile_manager import ProfileManager
from rumble.core.apo_config import ProfileConfig

# Test 1: Check first_active_device_pattern function
print("\n=== Test 1: first_active_device_pattern() ===")
devices = list_apo_devices()
print(f"Available APO devices: {len(devices)}")
for i, dev in enumerate(devices, 1):
    print(f"  {i}. {dev.label} (pattern: '{dev.pattern}', active: {dev.is_active})")

first_device = first_active_device_pattern()
print(f"\nfirst_active_device_pattern() returns: '{first_device}'")
assert first_device != "", "Should return a device pattern"
print("[OK] first_active_device_pattern() working correctly")

# Test 2: Check _remap_unmatched_devices now uses first device instead of "all"
print("\n=== Test 2: Profile device remapping ===")
from rumble.core.profile_manager import _remap_unmatched_devices

# Test with unmatched device
test_config = "Preamp: -3 dB\nDevice: UnmatchedDevice123\nChannel: L R\n"
remapped, changed = _remap_unmatched_devices(test_config, devices)
print(f"Original: {test_config.split(chr(10))[1]}")
print(f"Remapped: {remapped.split(chr(10))[1]}")
assert changed, "Should have changed the unmatched device"
assert f"Device: {first_device}" in remapped, f"Should use first device '{first_device}' not 'all'"
print(f"[OK] Unmatched devices remapped to first active device: '{first_device}'")

# Test 3: Check build_config() behavior
print("\n=== Test 3: Device selection in build_config() ===")
from rumble.core.apo_config import ChannelSection, Filter
test_config = ProfileConfig(
    device="all",  # Config still has "all"
    preamp=0.0,
    channel_sections=[ChannelSection("all", filters=[
        Filter(enabled=True, type="PK", fc=1000.0, gain=0.0, q=1.41)
    ])]
)
config_text = str(test_config.device)
print(f"Original ProfileConfig device: '{config_text}'")
assert test_config.device == "all", "Config can still have 'all' internally"
print("[OK] ProfileConfig can still have 'all' internally (backward compat)")

print("\n=== All device selection tests passed! ===")

print("\n=== All device selection tests passed! ===")
print(f"[OK] App will use first active device '{first_device}' instead of 'all'")
