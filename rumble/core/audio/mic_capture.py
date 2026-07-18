"""Microphone input capture (Campaign C, Session S9: C1a capture foundation).

Input-side twin of `engine.py`'s playback primitives: enumerates input
devices and records a fixed-duration mono buffer from one, via
`sounddevice.InputStream`. Still Qt-free: the stream callback runs on
PortAudio's own realtime thread and must never touch Qt, block, or raise
anything but the sounddevice control-flow exception it understands
(`sd.CallbackStop`) -- the same Sec4 point 2 contract `engine.py` documents.

Reuses `engine.AudioError`/`engine._classify_portaudio_error` rather than
re-deriving the PortAudio error-code mapping (same -9996 "exclusive" code,
same "no-device" text match), and `metering.rms_dbfs` rather than
reimplementing dB math -- both cited per-call below.

Public API:
    InputDevice        -- one enumerated input-capable device (index, name,
                           is_default)
    list_input_devices  -- enumerate sounddevice input-capable devices
    MicRecorder         -- records a fixed duration from a chosen input into
                           a float32 mono numpy buffer, with block-level
                           clip detection and RMS via metering.py
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from rumble.core.audio import metering
from rumble.core.audio.engine import AudioError, _classify_portaudio_error

# Full-scale amplitude (metering.py's own convention: "0 dBFS = full-scale
# amplitude 1.0"). A sample at or above this magnitude is clipped.
_CLIP_THRESHOLD = 1.0


@dataclass
class InputDevice:
    """One sounddevice input-capable device, as listed by list_input_devices()."""
    index: int
    name: str
    is_default: bool = False


def list_input_devices() -> list[InputDevice]:
    """Enumerate every input-capable device sounddevice can see.

    Mirrors `engine.find_output_device`'s enumeration conventions
    (`sd.query_devices()`, filtered by the input-side twin of
    `max_output_channels` -- `max_input_channels`), but lists every match
    instead of resolving one name fragment, since this feeds a UI picker
    (S13) rather than a single-device lookup.

    The default-input index comes from `sd.default.device[0]`
    (sounddevice's own `(input, output)` default-device pair); if that
    lookup fails for any reason, no device is marked default rather than
    guessing (`default_device_hooked()`'s "never guess" convention in
    `audio_devices.py`).
    """
    try:
        default_index = sd.default.device[0]
    except Exception:
        default_index = None

    devices = []
    for index, info in enumerate(sd.query_devices()):
        if info.get("max_input_channels", 0) <= 0:
            continue
        devices.append(
            InputDevice(
                index=index,
                name=info.get("name", ""),
                is_default=(index == default_index),
            )
        )
    return devices


def _default_input_stream_factory(samplerate, channels, device, callback, finished_callback):
    """Default `stream_factory`: a thin wrapper around sounddevice.InputStream.

    Same parameter shape as `engine._default_stream_factory`'s
    `finished_callback` naming (verified against the same installed
    sounddevice 0.5.5 source).
    """
    return sd.InputStream(
        samplerate=samplerate,
        channels=channels,
        device=device,
        callback=callback,
        finished_callback=finished_callback,
    )


class MicRecorder:
    """Records a fixed duration from a chosen input into a float32 mono buffer.

    Shape mirrors `engine.ToneEngine` exactly (asynchronous `record()`/
    `stop()`, an `on_done` callback, all cross-thread state behind one
    lock) but wraps `sounddevice.InputStream` instead of `OutputStream`.
    `stream_factory(samplerate, channels, device, callback,
    finished_callback)` defaults to a thin `InputStream` wrapper; tests
    inject a fake in its place (the `ToneEngine` `FakeStream` pattern) so
    the whole class is exercisable without real hardware.

    The stream always opens with `channels=1`: PortAudio maps that to the
    device's first input channel, which gives a mono buffer directly
    without a manual downmix step.

    Clip detection and level are block-level: each callback block is
    checked against `_CLIP_THRESHOLD` (latched, never cleared mid-recording
    -- one clipped block marks the whole recording clipped) and the
    recorded-so-far buffer's RMS is available via `rms_dbfs()`
    (`metering.rms_dbfs`, not reimplemented here).
    """

    def __init__(self, stream_factory: Optional[Callable] = None):
        self._stream_factory = stream_factory or _default_input_stream_factory
        self._lock = threading.Lock()
        self._stream = None
        self._buffer: Optional[np.ndarray] = None
        self._pos = 0
        self._on_done: Optional[Callable[[], None]] = None
        self._recording = False
        self._clipped = False

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def clipped(self) -> bool:
        with self._lock:
            return self._clipped

    @property
    def buffer(self) -> Optional[np.ndarray]:
        """The samples recorded so far (a copy), or None before the first record()."""
        with self._lock:
            if self._buffer is None:
                return None
            return self._buffer[: self._pos].copy()

    def rms_dbfs(self) -> float:
        """RMS level of the buffer recorded so far, via metering.rms_dbfs."""
        buf = self.buffer
        if buf is None or buf.size == 0:
            return metering.FLOOR_DBFS
        return metering.rms_dbfs(buf)

    def record(
        self,
        duration_s: float,
        sample_rate: int,
        device: Optional[int] = None,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Start recording `duration_s` seconds at `sample_rate` from `device`.

        Stops any recording already in progress first (no overlapping
        streams -- same convention as `ToneEngine.play`). Raises
        `AudioError` if the stream fails to open (`.kind` in
        `{"exclusive", "no-device", "other"}`, via
        `engine._classify_portaudio_error`). `on_done` fires (off the
        caller's thread) once `duration_s` seconds have been captured.
        """
        self.stop()

        n_samples = int(round(duration_s * sample_rate))

        with self._lock:
            self._buffer = np.zeros(n_samples, dtype=np.float32)
            self._pos = 0
            self._on_done = on_done
            self._recording = True
            self._clipped = False

        def _callback(indata, frames, time_info, status):
            # Never blocks: a single short critical section doing plain
            # numpy slicing, nothing else (Sec4 point 2).
            with self._lock:
                buf = self._buffer
                pos = self._pos
                if buf is None:
                    raise sd.CallbackStop
                mono = np.asarray(indata)[:, 0]
                remaining = len(buf) - pos
                n = max(0, min(frames, remaining))
                if n > 0:
                    block = mono[:n]
                    buf[pos:pos + n] = block
                    if np.any(np.abs(block) >= _CLIP_THRESHOLD):
                        self._clipped = True
                self._pos = pos + n
                drained = self._pos >= len(buf)
            if drained:
                raise sd.CallbackStop

        def _finished():
            with self._lock:
                self._recording = False
                stream = self._stream
                self._stream = None
                on_done_cb = self._on_done
                self._on_done = None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            if on_done_cb is not None:
                on_done_cb()

        stream = None
        try:
            stream = self._stream_factory(sample_rate, 1, device, _callback, _finished)
            stream.start()
        except Exception as exc:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            with self._lock:
                self._recording = False
                self._buffer = None
                self._pos = 0
                self._on_done = None
            raise AudioError(str(exc), kind=_classify_portaudio_error(exc)) from exc

        with self._lock:
            self._stream = stream

    def stop(self) -> None:
        """Stop recording immediately and cleanly. Safe to call when not
        currently recording (no-op, no exception).
        """
        with self._lock:
            stream = self._stream
            self._stream = None
            self._recording = False
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
