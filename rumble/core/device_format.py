"""Query the actual audio-endpoint format (sample rate / bit depth / channel
count) that Windows/WASAPI is currently using for a playback device — the
shared-mode "mix format" that everything downstream of the endpoint,
including Equalizer APO's filters, ultimately runs at.

`DeviceFormat` and `parse_waveformat` are pure and Qt/COM-free: they accept
any object exposing `nSamplesPerSec` / `wBitsPerSample` / `nChannels` (the
shape of a `WAVEFORMATEX`), so they're unit-testable without a real audio
stack.

`get_default_device_format` and `get_device_format` touch COM (via pycaw /
comtypes) and must never raise to callers: every failure mode — no default
device, COM not initialized on this thread, a device unplugged mid-call, an
unexpected pycaw/comtypes shape — is swallowed and reported as `None`.

Device identity matching reuses `rumble.core.audio_devices.pattern_matches_text`,
the same word-wise, case-insensitive substring/`;`-alternatives/`all`-keyword
matching that `pattern_matches_any` uses to resolve Equalizer APO `Device:`
patterns: pycaw's `AudioDevice.FriendlyName` is a single string shaped
"device_name (connection_name)" (e.g. "Speakers (FIIO K13 R2R)", empirically
verified against a real Windows machine with pycaw 20240210+) — not the raw
MMDevices endpoint GUID `ApoDevice.guid` holds. pycaw's own `.id` is a WASAPI
endpoint id shaped like `{0.0.0.00000000}.{guid}`, not byte-identical to
`ApoDevice.guid` either. So devices here are matched by friendly-name
fragment against `AudioDevice.FriendlyName`, never by GUID/id equality.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable

from rumble.core.audio_devices import pattern_matches_text


@dataclass(frozen=True)
class DeviceFormat:
    """The WASAPI mix format for a playback endpoint."""
    sample_rate: int
    bit_depth: int
    channels: int

    @property
    def label(self) -> str:
        """Human-readable summary, e.g. "48 kHz · 24-bit" or "44.1 kHz · 16-bit"."""
        rate_khz = self.sample_rate / 1000
        return f"{rate_khz:g} kHz · {self.bit_depth}-bit"


def parse_waveformat(wfx) -> DeviceFormat:
    """Pure conversion from a WAVEFORMATEX-shaped object to DeviceFormat.

    Accepts anything with `nSamplesPerSec` / `wBitsPerSample` / `nChannels`
    attributes (a real `WAVEFORMATEX` ctypes struct, a `SimpleNamespace` in
    tests, etc.).
    """
    return DeviceFormat(
        sample_rate=int(wfx.nSamplesPerSec),
        bit_depth=int(wfx.wBitsPerSample),
        channels=int(wfx.nChannels),
    )


def _mix_format_for_immdevice(dev) -> DeviceFormat | None:
    """Activate `IAudioClient` on a raw comtypes `IMMDevice` and read its
    mix format. Never raises — callers get None on any COM failure."""
    try:
        import comtypes
        from pycaw.pycaw import IAudioClient

        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # already initialized on this thread — fine
        iface = dev.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None)
        audio_client = iface.QueryInterface(IAudioClient)
        wfx = audio_client.GetMixFormat().contents
        return parse_waveformat(wfx)
    except Exception:
        return None


def get_default_device_format() -> DeviceFormat | None:
    """Mix format of the current default playback device, or None on any
    COM failure (no default device, COM error, offscreen/CI environment
    with no audio stack, etc.)."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        speaker = AudioUtilities.GetSpeakers()
        return _mix_format_for_immdevice(speaker._dev)
    except Exception:
        return None


def get_default_device_name() -> str:
    """Friendly name of the current default playback device (e.g. "Speakers
    (FIIO K13 R2R)"), the same "device_name (connection_name)" shape
    `get_device_format`/`pattern_matches_text` already match against. Used by
    Phase 3's auto-switch device rules to test the live default device
    against a rule's `Device:`-style match fragment. Empty string on any COM
    failure or when there is no default device — matches nothing, the same
    fail-safe convention as the other query functions in this module."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        speaker = AudioUtilities.GetSpeakers()
        return speaker.FriendlyName or ""
    except Exception:
        return ""


def get_device_format(device_name_fragment: str) -> DeviceFormat | None:
    """Mix format of the active playback device whose friendly name matches
    *device_name_fragment* (Equalizer APO `Device:` pattern semantics — see
    `rumble.core.audio_devices.pattern_matches_text`: word-wise
    case-insensitive substring matching, `;`-separated alternatives, and the
    `all` keyword matching every device). Returns None when no matching
    active playback device is found or on any COM failure."""
    try:
        import comtypes
        from pycaw.pycaw import AudioDeviceState, AudioUtilities, EDataFlow

        if not device_name_fragment or not device_name_fragment.strip():
            return None
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        devices = AudioUtilities.GetAllDevices(
            data_flow=EDataFlow.eRender.value,
            device_state=AudioDeviceState.Active.value,
        )
        for device in devices:
            name = device.FriendlyName or ""
            if pattern_matches_text(device_name_fragment, name):
                return _mix_format_for_immdevice(device._dev)
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Push notifications: DeviceFormatWatcher
# ---------------------------------------------------------------------------
#
# Windows delivers endpoint-change notifications (property/format changes,
# default-device changes, device add/remove/state changes) via the
# IMMNotificationClient COM callback interface, registered through
# IMMDeviceEnumerator.RegisterEndpointNotificationCallback. Callbacks arrive
# on a COM/foreign thread chosen by Windows — never the thread that called
# .start(), and never the Qt main thread. This module stays strictly Qt-free
# (no PySide6 import anywhere here) so DeviceFormatWatcher can fire plain
# zero-arg callbacks from any thread; a later Qt adapter is responsible for
# marshalling on_change() onto the Qt main thread before touching any widget.
#
# Empirically verified on this machine (comtypes 1.4+, pycaw 20240210+):
# pycaw.pycaw already declares IMMNotificationClient and IMMDeviceEnumerator
# with the correct COMMETHOD shapes (including
# Register/UnregisterEndpointNotificationCallback), so no hand-written COM
# interface definitions are needed here. A comtypes.COMObject subclass whose
# handler methods omit the `this` parameter (e.g.
# `def OnDeviceAdded(self, pwstrDeviceId):`) is dispatched correctly by
# comtypes' `call_without_this` path (comtypes/_vtbl.py): comtypes itself
# already wraps every call in try/except and defaults a `None`/absent return
# to S_OK, but this module adds its own try/except around each handler body
# too, per the plan's explicit defense-in-depth requirement — an exception
# reaching a COM callback thread must never be relied upon to be someone
# else's problem.


_NotificationSinkClass = None


def _get_notification_sink_class():
    """Build (once) and cache the comtypes.COMObject subclass implementing
    IMMNotificationClient. Deferred rather than defined at module scope so
    `import rumble.core.device_format` never requires a working comtypes/pycaw
    stack, matching the deferred-import convention every COM-touching
    function in this module already follows."""
    global _NotificationSinkClass
    if _NotificationSinkClass is not None:
        return _NotificationSinkClass

    from comtypes import COMObject
    from pycaw.pycaw import IMMNotificationClient

    class _Sink(COMObject):
        _com_interfaces_ = [IMMNotificationClient]

        def __init__(self, callback: "Callable[[], None]"):
            super().__init__()
            self._callback = callback

        def _dispatch(self) -> int:
            # Never let an exception escape a COM callback thread.
            try:
                self._callback()
            except Exception:
                pass
            return 0  # S_OK

        def OnDeviceStateChanged(self, pwstrDeviceId, dwNewState):
            return self._dispatch()

        def OnDeviceAdded(self, pwstrDeviceId):
            return self._dispatch()

        def OnDeviceRemoved(self, pwstrDeviceId):
            return self._dispatch()

        def OnDefaultDeviceChanged(self, flow, role, pwstrDefaultDeviceId):
            return self._dispatch()

        def OnPropertyValueChanged(self, pwstrDeviceId, key):
            return self._dispatch()

    _NotificationSinkClass = _Sink
    return _NotificationSinkClass


def _NotificationSink(callback: "Callable[[], None]"):
    """Construct a fresh IMMNotificationClient COM sink that calls *callback*
    (no args) from each of its five change-notification handlers.

    A thin factory function rather than a plain class so the real
    comtypes.COMObject subclass can be built lazily (see
    `_get_notification_sink_class`) while still being usable as
    `_NotificationSink(callback)`. Its dispatch logic — the fan-out to
    *callback*, and the try/except inside each handler — is fully testable
    without live COM/CoInitialize: constructing a COMObject and calling its
    methods directly touches neither COM registration nor the audio stack.
    """
    return _get_notification_sink_class()(callback)


class DeviceFormatWatcher:
    """Push-notification watcher for playback-endpoint changes.

    Registers an IMMNotificationClient with the shared IMMDeviceEnumerator
    and fires `on_change()` (no args — subscribers re-query current state,
    e.g. via `get_default_device_format()` / `get_device_format()`) whenever
    Windows reports a device property change, a default-device change, or a
    device add/remove/state change.

    Notifications are debounced with a `threading.Timer` so a burst of
    related Windows notifications (which commonly fire several
    OnPropertyValueChanged calls back-to-back for a single user action, e.g.
    changing a device's sample rate) collapses into a single `on_change()`
    call. `_DEBOUNCE_SECONDS` is set from a real measurement (2026-07-07,
    this machine): registering the same notification sink directly, without
    debouncing, and changing a real device's Advanced format a few times
    showed each single user action fires ~20-30 notifications within 50 ms
    followed by ONE MORE confirmation notification roughly 1.0-1.1 s later
    (Windows finalizing the driver reconfiguration) — genuinely separate
    actions were 2-4 s apart. A 500 ms debounce (the previous, unmeasured
    value) would miss that trailing confirmation and fire `on_change()`
    twice per action; 1.5 s clears the measured ~1.1 s max gap with margin
    while staying well under the ~2 s+ gap between distinct actions.

    `.start()` / `.stop()` never raise: if COM registration fails (no COM,
    no audio stack, transient audio-service error, offscreen/CI environment)
    `.start()` logs a warning and leaves the watcher inert — but does *not*
    latch that failure permanently. `.start()` is idempotent only once
    registration has actually succeeded (a second `.start()` after a
    successful one is a true no-op, never double-registering); calling
    `.start()` again after a *failed* attempt retries registration rather
    than silently staying inert for the rest of the process lifetime.
    `.stop()` before `.start()` / called twice / called after a failed
    `.start()` are all no-ops.

    Qt-free by design: `on_change()` fires on whatever COM/foreign thread
    Windows delivers the notification on (and, after debounce, on a
    `threading.Timer` thread) — never the Qt main thread. Callers that need
    to touch UI must marshal onto the main thread themselves.
    """

    _DEBOUNCE_SECONDS = 1.5

    def __init__(self, on_change: "Callable[[], None]"):
        self._on_change = on_change
        self._enumerator = None
        self._sink = None
        self._timer = None
        self._timer_lock = threading.Lock()
        # Set only on registration SUCCESS — deliberately distinct from "has
        # .start() ever been called". This is what makes .start() retry after
        # a failed attempt instead of latching into permanent inertness: the
        # idempotency guard below checks "are we already registered?", not
        # "did someone call start() before?".
        self._registered = False

    def start(self) -> None:
        """Register for endpoint notifications.

        A no-op if already successfully registered. If a prior `.start()`
        call failed to register, this retries registration rather than
        staying silently inert. Never raises — registration failure is
        logged and swallowed."""
        if self._registered:
            return
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities

            try:
                comtypes.CoInitialize()
            except OSError:
                pass  # already initialized on this thread — fine
            enumerator = AudioUtilities.GetDeviceEnumerator()
            sink = _NotificationSink(self._on_notification)
            enumerator.RegisterEndpointNotificationCallback(sink)
            # Keep strong references: COM holds no strong Python reference
            # back to these, and stop() needs this exact sink instance to
            # unregister.
            self._enumerator = enumerator
            self._sink = sink
            self._registered = True
        except Exception as exc:
            print(
                f"WARNING: DeviceFormatWatcher failed to register for "
                f"endpoint notifications: {exc}",
                file=sys.stderr,
            )
            self._enumerator = None
            self._sink = None
            self._registered = False

    def stop(self) -> None:
        """Unregister and cancel any pending debounce timer. Safe to call
        before `.start()`, after a failed `.start()`, or more than once.
        Never raises."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._registered and self._enumerator is not None and self._sink is not None:
            try:
                self._enumerator.UnregisterEndpointNotificationCallback(self._sink)
            except Exception as exc:
                print(
                    f"WARNING: DeviceFormatWatcher failed to unregister "
                    f"endpoint notifications: {exc}",
                    file=sys.stderr,
                )
        self._registered = False
        self._enumerator = None
        self._sink = None

    def _on_notification(self) -> None:
        """Called (possibly many times in a burst) from a COM thread.
        Coalesces via `_DEBOUNCE_SECONDS`'s timer into a single on_change()."""
        try:
            with self._timer_lock:
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(self._DEBOUNCE_SECONDS, self._fire_on_change)
                self._timer.daemon = True
                self._timer.start()
        except Exception:
            pass

    def _fire_on_change(self) -> None:
        try:
            self._on_change()
        except Exception:
            pass
