"""Read the current WASAPI endpoint (master) volume, in dB.

Same source Session 6 confirmed upstream Equalizer APO's own
`LoudnessCorrection:` filter uses for its volume-linked behavior
(`VolumeController.cpp`, `IAudioEndpointVolume::GetMasterVolumeLevel` --
docs/RUMBLE_APP_SPECIFIC_KB.md §14.1) -- the system output device's master
volume in dB (0 = full scale, negative = attenuated), not a per-session or
per-app volume.

Deferred pycaw/comtypes import, mirroring device_format.py's convention:
`import rumble.core.master_volume` never requires a working COM/audio stack,
and every COM failure mode (no default device, no endpoint-volume interface,
offscreen/CI environment) is swallowed and reported as `None` rather than
raised.
"""
from __future__ import annotations


def get_master_volume_db() -> float | None:
    """Current default playback device's master volume, in dB (0 = full
    scale). None on any COM failure."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # already initialized on this thread — fine
        speaker = AudioUtilities.GetSpeakers()
        iface = speaker._dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        endpoint_volume = iface.QueryInterface(IAudioEndpointVolume)
        return float(endpoint_volume.GetMasterVolumeLevel())
    except Exception:
        return None


def set_master_volume_percent(percent: float) -> bool:
    """Set the default playback device's master volume to the given 0-100
    scalar -- the same linear scalar Windows' own taskbar slider shows (via
    `IAudioEndpointVolume.SetMasterVolumeLevelScalar`, the setter mirror of
    get_master_volume_percent below). Exists for the Ear Calibration
    dashboard's cross-session volume guard: thresholds saved at one volume
    are only comparable at that same volume, so "restore the volume this
    calibration was measured at" must be one deterministic click, not a
    manual chase of a remembered number. Returns False (never raises) on
    any COM failure, mirroring this module's getter convention."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # already initialized on this thread — fine
        speaker = AudioUtilities.GetSpeakers()
        iface = speaker._dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        endpoint_volume = iface.QueryInterface(IAudioEndpointVolume)
        scalar = max(0.0, min(100.0, float(percent))) / 100.0
        endpoint_volume.SetMasterVolumeLevelScalar(scalar, None)
        return True
    except Exception:
        return False


def get_master_volume_percent() -> float | None:
    """Current default playback device's master volume as the 0-100 linear
    scalar Windows itself displays on the taskbar volume slider (via
    `IAudioEndpointVolume.GetMasterVolumeLevelScalar` -- a separate reading
    from `get_master_volume_db` above, not a conversion of it: Windows'
    displayed percentage is that linear scalar directly, not a simple
    10**(dB/20) of the dB reading). Exists because a dB figure isn't
    something a user can go read or reproduce anywhere in the Windows UI,
    whereas this percentage is exactly the number shown next to the volume
    slider they'd actually go set. None on any COM failure."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # already initialized on this thread — fine
        speaker = AudioUtilities.GetSpeakers()
        iface = speaker._dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        endpoint_volume = iface.QueryInterface(IAudioEndpointVolume)
        return float(endpoint_volume.GetMasterVolumeLevelScalar()) * 100.0
    except Exception:
        return None
