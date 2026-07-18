"""Volume-knob tracking for the ISO 226 loudness layer (Rumble 3.5 follow-up).

``ProfileManager.get_active_loudness_correction()`` reads the LIVE WASAPI
master volume on every render, but a render only happens at the choke points
(profile activation, live-edit preview, bypass toggle, listener-slot switch).
A user who turns the Windows volume down while a profile just sits active
would keep hearing the compensation derived at the OLD volume until the next
such event -- which defeats the point of a volume-linked layer. This adapter
closes that gap.

Mechanism: a plain GUI-thread ``QTimer`` (``TICK_MS = 1000``, the same
established polling cadence ``auto_switch_adapter.py`` already uses; one
master-volume read is a single cheap COM call) polls the volume and asks
``ProfileManager.refresh_render_layers()`` to re-render when it has moved by
at least ``REFRESH_THRESHOLD_DB`` since the last applied render. No
cross-thread marshalling is needed (unlike device_format_adapter.py) because
nothing here runs off the GUI thread.

Threshold: 1.0 dB -- the loudness model's own granularity. Its documented
assumption is 1 dB of WASAPI volume ~= 1 phon of perceived loudness change,
so a sub-1-dB wiggle alters the derived curve by less than one model unit
and isn't worth a config rewrite (each rewrite makes Equalizer APO
hot-reload the chain).

The tick early-outs while the layer can't produce a correction (disabled, or
reference never calibrated), and ``refresh_render_layers()`` itself refuses
while previewing (never clobber a live-tuning preview or an ABX trial),
while bypassed, or with no active profile -- a refused refresh is retried on
a later tick rather than recorded.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer

from rumble.core import master_volume

TICK_MS = 1000
REFRESH_THRESHOLD_DB = 1.0


class LoudnessVolumeAdapter(QObject):
    """Polls the WASAPI master volume and re-renders the active profile when
    it moves, so the ISO 226 loudness layer tracks the volume knob.

    ``volume_reader`` is a test seam (production callers never pass it) --
    tests inject a controllable reader and drive ``_tick()`` directly
    instead of waiting on the timer.
    """

    def __init__(self, manager, parent: QObject | None = None,
                 volume_reader=master_volume.get_master_volume_db):
        super().__init__(parent)
        self._manager = manager
        self._read_volume = volume_reader
        self._last_rendered_db: float | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        settings = self._manager.settings
        if not settings.get("iso226_loudness_enabled"):
            return
        if settings.get("iso226_reference_volume_db") is None:
            return
        current = self._read_volume()
        if current is None:
            return
        if (self._last_rendered_db is not None
                and abs(current - self._last_rendered_db) < REFRESH_THRESHOLD_DB):
            return
        if self._manager.refresh_render_layers():
            self._last_rendered_db = current
