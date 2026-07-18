"""Self-test: the "Windows Audio enhancements is off" warning.

Equalizer APO runs as a system effect (an APO in the endpoint's FX chain).
Windows skips that entire chain when "Audio enhancements" is off for the device,
so RUMBLE's EQ becomes inert even though APO is correctly installed and the
config files are written perfectly — every profile, every built-in, and even
Bypass silently do nothing. Every symptom points at RUMBLE being broken when it
is not, so the app detects the switch and says so.

Covers the detection (`audio_devices.enhancements_disabled`) and the banner's
two-reason presentation. The registry is faked, so this runs anywhere.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rumble.core import audio_devices
from rumble.ui import apo_coverage_banner
from rumble.ui.apo_coverage_banner import (REASON_ENHANCEMENTS_OFF, REASON_NOT_HOOKED,
                                          ApoCoverageBanner)
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── detection: the PKEY_AudioEndpoint_Disable_SysFx flag ──────────────────
# Fake winreg so the three outcomes (off / on / unreadable) are all reachable
# without touching the real machine's registry.
class _FakeKey:
    def __init__(self, values):
        self._values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_registry(by_subkey, *, openable=True):
    """by_subkey: {"FxProperties"|"Properties": {value-name: value}}. A subkey
    absent from the mapping does not exist on this fake endpoint."""
    real = (audio_devices.winreg.OpenKey, audio_devices.winreg.QueryValueEx)

    def fake_open(root, path, reserved, access):
        if not openable:
            raise OSError("no such key")
        sub = path.rsplit("\\", 1)[-1]
        if sub not in by_subkey:
            raise OSError("no such subkey")
        return _FakeKey(by_subkey[sub])

    def fake_query(key, name):
        if name not in key._values:
            raise OSError("no such value")
        return (key._values[name], 4)

    audio_devices.winreg.OpenKey = fake_open
    audio_devices.winreg.QueryValueEx = fake_query
    return real


def _restore(real):
    audio_devices.winreg.OpenKey, audio_devices.winreg.QueryValueEx = real


FLAG = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5"
GUID = "{db2e4e87-ec3a-4682-a8ee-3eaa86c0809f}"

# Windows 11 puts the flag in FxProperties. This is where the first cut of this
# feature was WRONG (it only looked in Properties), so the banner never fired on
# a real machine with the toggle genuinely off. Pin the real location.
real = _install_fake_registry({"FxProperties": {FLAG: 1}, "Properties": {}})
check(audio_devices.enhancements_disabled(GUID) is True,
      "flag == 1 in FxProperties (where Windows 11 actually writes it) -> OFF")
_restore(real)

real = _install_fake_registry({"FxProperties": {}, "Properties": {FLAG: 1}})
check(audio_devices.enhancements_disabled(GUID) is True,
      "flag == 1 in Properties (older placement) -> OFF, via the fallback")
_restore(real)

real = _install_fake_registry({"FxProperties": {FLAG: 0}, "Properties": {}})
check(audio_devices.enhancements_disabled(GUID) is False,
      "flag == 0 -> enhancements are on")
_restore(real)

real = _install_fake_registry({"FxProperties": {}, "Properties": {}})
check(audio_devices.enhancements_disabled(GUID) is False,
      "endpoint exists but carries no flag -> enhancements are on, not a warning")
_restore(real)

real = _install_fake_registry({}, openable=False)
check(audio_devices.enhancements_disabled(GUID) is None,
      "unreadable registry -> None, so the caller never warns on a guess")
_restore(real)

real = _install_fake_registry({"FxProperties": {FLAG: "unexpected"}})
check(audio_devices.enhancements_disabled(GUID) is None,
      "unexpected value type -> None rather than a false alarm")
_restore(real)

# The real machine must answer without raising, whatever it says.
try:
    live = audio_devices.enhancements_disabled(GUID)
    check(live in (True, False, None), f"live registry read returns a valid verdict ({live})")
except Exception as exc:  # noqa: BLE001
    check(False, f"live registry read raised {exc!r} (it must never raise)")

# ── the banner reports the right cause, with the right fix ────────────────
app = QApplication.instance() or QApplication([])
banner = ApoCoverageBanner(get_theme(DEFAULT_THEME_NAME))

banner.set_reason(REASON_NOT_HOOKED)
check("does not have Equalizer APO" in banner.label.text(),
      "not-hooked reason explains that APO isn't installed on the device")
check(banner.sound_settings_button.isHidden(),
      "not-hooked reason hides the Sound-settings button (wrong fix for that cause)")

banner.set_reason(REASON_ENHANCEMENTS_OFF)
text = banner.label.text()
check("Audio enhancements" in text and "OFF" in text,
      "enhancements reason names the Windows switch that is actually off")
check("Bypass" in text,
      "...and warns that even Bypass will appear to do nothing — the symptom "
      "that makes this look like a RUMBLE bug")
check(banner.sound_settings_button.isVisibleTo(banner),
      "enhancements reason offers 'Open Sound Settings' (the fix that fixes IT)")
check(banner.configurator_button.isHidden(),
      "...and hides the APO Configurator button, which would not help here")

# Switching back restores the other presentation (the banner is reused, not
# rebuilt, so a stale button/message would be a real bug).
banner.set_reason(REASON_NOT_HOOKED)
check(banner.configurator_button.isVisibleTo(banner)
      and banner.sound_settings_button.isHidden(),
      "switching reason back restores the configurator fix and hides the other")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("All audio-enhancements warning checks passed.")
