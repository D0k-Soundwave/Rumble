"""Enumerate audio endpoints Equalizer APO is actually installed on.

The APO Configurator records every endpoint it hooks under
`HKLM\\SOFTWARE\\EqualizerAPO\\Child APOs\\{endpoint-guid}`. The endpoint's
human-readable names live in the MMDevices registry tree:

  HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\Audio\\
      Render|Capture\\{guid}\\Properties
        {a45c254e-df1c-4efd-8020-67d146a850e0},2   → device name  ("Speakers")
        {b3f8fa53-0004-438e-9003-51a46e139bfc},6   → connection   ("FIIO K13 R2R")

Equalizer APO matches a `Device:` pattern word-wise against the string
"deviceName connectionName guid", so "deviceName connectionName" is a
readable pattern that reliably selects exactly that endpoint.
"""
from __future__ import annotations

import re
import winreg
from dataclasses import dataclass

CHILD_APOS_KEY = r"SOFTWARE\EqualizerAPO\Child APOs"
MMDEVICES_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"

_PKEY_DEVICE_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PKEY_CONNECTION_NAME = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"

# PKEY_AudioEndpoint_Disable_SysFx. Windows' "Audio enhancements" switch (per
# endpoint, in Sound settings) writes 1 here when the user turns enhancements
# OFF, and clears/zeroes it when they turn them back on.
#
# This is load-bearing for RUMBLE, not a curiosity: Equalizer APO runs as a
# system-effect (APO) in the endpoint's FX chain, and Windows skips that ENTIRE
# chain when enhancements are disabled. Every profile, every built-in, and even
# Bypass then silently do nothing — the config files are written correctly and
# simply never executed. It looks exactly like RUMBLE is broken, so we detect it
# and say so rather than let the user hunt for a bug that isn't in the app.
#
# It lives under the endpoint's **FxProperties** subkey (verified on Windows 11
# against the real toggle: flipping "Audio enhancements" off writes
# FxProperties\{1da5d803-...},5 = 1). `Properties` is checked as a fallback
# because the placement has historically differed across Windows versions, and
# reading one extra key is cheaper than a warning that never fires.
_PKEY_DISABLE_SYSFX = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5"
_SYSFX_SUBKEYS = ("FxProperties", "Properties")

DEVICE_STATE_ACTIVE = 1


@dataclass
class ApoDevice:
    """An audio endpoint that Equalizer APO is installed on."""
    guid: str
    device_name: str = ""
    connection_name: str = ""
    flow: str = ""          # "Render" (playback) or "Capture" (recording)
    state: int = 0          # MMDevices DeviceState (1 = active)

    @property
    def is_active(self) -> bool:
        return self.state == DEVICE_STATE_ACTIVE

    @property
    def pattern(self) -> str:
        """The text to put on a `Device:` line to select this endpoint."""
        words = " ".join(p for p in (self.device_name, self.connection_name) if p)
        return words or self.guid

    @property
    def label(self) -> str:
        """Human-readable description for tooltips/listings."""
        name = self.device_name or self.guid
        conn = f" ({self.connection_name})" if self.connection_name else ""
        flow = "Playback" if self.flow == "Render" else "Recording"
        state = "" if self.is_active else " — inactive"
        return f"{name}{conn} · {flow}{state}"


def _read_endpoint(guid: str) -> ApoDevice | None:
    """Resolve an endpoint GUID to its names via the MMDevices tree."""
    for flow in ("Render", "Capture"):
        key_path = rf"{MMDEVICES_KEY}\{flow}\{guid}"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as endpoint_key:
                try:
                    state = int(winreg.QueryValueEx(endpoint_key, "DeviceState")[0])
                except OSError:
                    state = 0
            device = ApoDevice(guid=guid, flow=flow, state=state)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path + r"\Properties", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as props:
                for value_name, attr in ((_PKEY_DEVICE_DESC, "device_name"),
                                         (_PKEY_CONNECTION_NAME, "connection_name")):
                    try:
                        value = winreg.QueryValueEx(props, value_name)[0]
                        if isinstance(value, str):
                            setattr(device, attr, value.strip())
                    except OSError:
                        pass
            return device
        except OSError:
            continue
    return None


def enhancements_disabled(guid: str) -> bool | None:
    """Is Windows' "Audio enhancements" switch OFF for this endpoint?

    True  -- enhancements are off, so Windows will NOT run the endpoint's system
             effects. Equalizer APO lives in that chain, so RUMBLE's EQ is
             silently inert no matter what it writes: profiles, built-ins and
             even Bypass all appear to do nothing.
    False -- enhancements are on; the APO chain runs normally.
    None  -- the endpoint (or the registry) couldn't be read, so we genuinely
             don't know. Callers must not warn on None — an unreadable key is
             not evidence of a problem.

    The flag is `_PKEY_DISABLE_SYSFX`, found under the endpoint's FxProperties
    (with Properties as a fallback — see the constant): 1 means disabled; 0, or
    the flag being absent entirely (the common case), means enabled. Never
    raises — a missing key, a hive we can't open, or a value of an unexpected
    type all resolve to None rather than a false alarm.
    """
    seen_endpoint = False
    for flow in ("Render", "Capture"):
        for subkey in _SYSFX_SUBKEYS:
            key_path = rf"{MMDEVICES_KEY}\{flow}\{guid}\{subkey}"
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as props:
                    seen_endpoint = True
                    try:
                        value = winreg.QueryValueEx(props, _PKEY_DISABLE_SYSFX)[0]
                    except OSError:
                        continue   # not in this subkey — try the other one
                    try:
                        return int(value) == 1
                    except (TypeError, ValueError):
                        return None  # unexpected shape: don't guess
            except OSError:
                continue           # no such subkey/flow — keep looking
    # The endpoint exists but carries no flag at all: enhancements are on.
    return False if seen_endpoint else None


def list_apo_devices() -> list[ApoDevice]:
    """All endpoints Equalizer APO is installed on, playback first, active
    first. Returns an empty list when APO is absent or nothing is hooked."""
    guids: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CHILD_APOS_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            i = 0
            while True:
                try:
                    guids.append(winreg.EnumKey(key, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        return []

    devices = []
    for guid in guids:
        device = _read_endpoint(guid)
        if device is None:
            # APO knows the endpoint but MMDevices doesn't — list it by GUID
            # so the user can still see/select it.
            device = ApoDevice(guid=guid)
        devices.append(device)

    devices.sort(key=lambda d: (d.flow != "Render", not d.is_active, d.pattern.lower()))
    return devices


def list_all_capture_endpoints() -> list[ApoDevice]:
    """Every capture (microphone/recording) endpoint Windows currently
    knows about, hooked by Equalizer APO or not -- unlike list_apo_devices()
    (which only walks the Child APOs registry key, i.e. already-hooked
    endpoints), this walks the MMDevices\\Audio\\Capture tree directly.

    D2's A1 extension: the Devices page's Capture tab uses this to note
    capture endpoints that exist but aren't APO-hooked yet -- the same
    "silent gap" default_device_hooked() flags for playback, surfaced as
    a note inside the Devices page only (no new banner, per the D2 spec)."""
    guids: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{MMDEVICES_KEY}\Capture", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            i = 0
            while True:
                try:
                    guids.append(winreg.EnumKey(key, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        return []

    devices = []
    for guid in guids:
        device = _read_endpoint(guid)
        if device is not None:
            devices.append(device)
    devices.sort(key=lambda d: (not d.is_active, d.pattern.lower()))
    return devices


def first_active_device_pattern() -> str:
    """Pattern for the first active APO device, or "all" if none are active."""
    devices = list_apo_devices()
    for device in devices:
        if device.is_active:
            return device.pattern
    return "all"


def pattern_matches_text(pattern: str, haystack: str) -> bool:
    """True when an APO `Device:` pattern matches a single *haystack* string.

    Mirrors APO's matching: alternatives are separated by ';', and every
    space-separated word of an alternative must occur (case-insensitive
    substring) in *haystack*. The keyword `all` matches unconditionally.
    """
    haystack = haystack.lower()
    for alternative in pattern.split(";"):
        words = alternative.strip().lower().split()
        if not words:
            continue
        if words == ["all"]:
            return True
        if all(word in haystack for word in words):
            return True
    return False


def pattern_matches_any(pattern: str, devices: list[ApoDevice]) -> bool:
    """True when an APO `Device:` pattern selects at least one of *devices*.

    Mirrors APO's matching: alternatives are separated by ';', and every
    space-separated word of an alternative must occur (case-insensitive
    substring) in "deviceName connectionName guid". The keyword `all`
    matches every endpoint.
    """
    for device in devices:
        haystack = f"{device.device_name} {device.connection_name} {device.guid}"
        if pattern_matches_text(pattern, haystack):
            return True
    return False


# ---------------------------------------------------------------------------
# A1: is the default playback device actually APO-hooked?
# ---------------------------------------------------------------------------
#
# See docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md "A1.
# APO-coverage health check" -- warn when the default render endpoint is not
# one Equalizer APO is installed on, since that's the single most common
# silent-failure mode of any APO front end.

# pycaw's AudioDevice.id (== the raw IMMDevice.GetId() string) is shaped like
# "{0.0.0.00000000}.{<endpoint-guid>}" -- NOT byte-identical to the plain
# "{<endpoint-guid>}" this module's ApoDevice.guid holds (same finding
# device_format.py's module docstring already documents about pycaw ids vs.
# ApoDevice.guid). The trailing brace-wrapped token is the same GUID the
# MMDevices/Child-APOs registry trees key on, so extracting it is enough to
# compare against ApoDevice.guid.
_GUID_SUFFIX_RE = re.compile(r"(\{[0-9A-Fa-f-]{8,36}\})$")


def extract_endpoint_guid(pycaw_id: str | None) -> str | None:
    """Pull the raw MMDevices endpoint GUID out of a pycaw endpoint id
    (see module note above). None when *pycaw_id* is falsy or doesn't end in
    a GUID-shaped `{...}` token -- never guessed."""
    if not pycaw_id:
        return None
    match = _GUID_SUFFIX_RE.search(pycaw_id)
    return match.group(1) if match else None


def default_render_guid() -> str | None:
    """Raw endpoint GUID of the current default playback (render) device, or
    None on any COM failure / no default device / unrecognized id shape.
    Never raises. Deferred pycaw/comtypes import + broad except, matching the
    convention `master_volume.py` and `device_format.py` already use. Split
    out from `default_device_hooked()` below as the seam tests monkeypatch."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # already initialized on this thread — fine
        speaker = AudioUtilities.GetSpeakers()
        pycaw_id = speaker.id if speaker is not None else None
    except Exception:
        return None
    return extract_endpoint_guid(pycaw_id)


def default_device_hooked() -> bool | None:
    """True when the current default playback device has Equalizer APO
    installed on it, False when it doesn't, None when this can't be
    determined (no COM, no default device, or an unrecognized id shape) --
    never guessed. Compares against `list_apo_devices()`'s Render-flow
    entries only (the default device query is playback-only, so a Capture
    endpoint sharing the same GUID -- impossible in practice, since GUIDs are
    per-endpoint -- must not count)."""
    guid = default_render_guid()
    if guid is None:
        return None
    hooked = {d.guid.upper() for d in list_apo_devices() if d.flow == "Render"}
    return guid.upper() in hooked
