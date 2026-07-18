"""Playback engine for test tones (Phase 4: Audio Engine Foundation, Task 4).

Owns real PortAudio output streams via `sounddevice` -- this is the one
module in `rumble/core/audio/` that talks to actual hardware (tones.py and
metering.py stay pure numpy). Still Qt-free: the audio callback that
PortAudio invokes on its own realtime thread must never touch Qt, block, or
raise anything but the sounddevice control-flow exceptions it understands
(`sd.CallbackStop`) -- see design spec Sec4 point 2 ("audio callbacks never
touch Qt, never block, plain-Python callbacks").

Public API:
    find_output_device -- resolve a RUMBLE device-name fragment to a
                           sounddevice output device index
    AudioError          -- raised by ToneEngine.play on stream-open failure,
                           with `.kind` in {"exclusive", "no-device", "other"}
    ToneEngine          -- plays a precomputed float32 buffer through an
                           (injectable) output stream
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from rumble.core.audio_devices import pattern_matches_text

# PortAudio's PaErrorCode for "device unavailable" -- raised when a device is
# busy / exclusively locked by another stream. sounddevice.PortAudioError
# carries this as the second element of `.args` (see PortAudioError.__str__
# in sounddevice.py, which also renders it into the message as
# "... [PaErrorCode -9996]"), so we can match on either the int or the text.
_EXCLUSIVE_ERROR_CODE = -9996


class AudioError(Exception):
    """Raised when opening or running a playback stream fails.

    `.kind` is one of:
        "exclusive" -- the device is busy / exclusively locked by another
                        stream (PortAudio paDeviceUnavailable, code -9996)
        "no-device" -- the device index/name doesn't exist or can't be
                        found
        "other"     -- anything else
    """

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


def _classify_portaudio_error(exc: BaseException) -> str:
    """Map a sounddevice.PortAudioError (or any error shaped like one, e.g.
    from a test's fake factory) to an AudioError `.kind`.
    """
    arg_codes = [a for a in getattr(exc, "args", ()) if isinstance(a, int)]
    text = str(exc).lower()
    if _EXCLUSIVE_ERROR_CODE in arg_codes or str(_EXCLUSIVE_ERROR_CODE) in text:
        return "exclusive"
    if "invalid device" in text or "no such device" in text or "device not found" in text:
        return "no-device"
    return "other"


def find_output_device(name_fragment: Optional[str]) -> Optional[int]:
    """Resolve a RUMBLE device-name fragment to a sounddevice output index.

    `name_fragment=None` means "use the system default output". This
    resolves to `None` rather than a concrete index -- matching
    sounddevice's own convention where `device=None` on a stream
    constructor means "use the default device" -- because that is exactly
    the value `ToneEngine.play`'s `device` parameter is meant to receive,
    and passing `None` straight through keeps "default" correct even if the
    default output changes between calls (no stale index).

    Otherwise, `name_fragment` is matched against every output-capable
    device's name (from `sounddevice.query_devices()`) using the same
    APO-style word-wise matching as `rumble.core.audio_devices` (via
    `pattern_matches_text`); the first match's index is returned. If
    nothing matches (e.g. a stale/renamed device name), this falls back to
    `None` (system default) rather than raising, so a bad saved device
    name never hard-fails playback -- it just plays out the default
    device instead.
    """
    if name_fragment is None:
        return None
    for index, info in enumerate(sd.query_devices()):
        if info.get("max_output_channels", 0) <= 0:
            continue
        if pattern_matches_text(name_fragment, info.get("name", "")):
            return index
    return None


def _default_stream_factory(samplerate, channels, device, callback, finished_callback):
    """Default `stream_factory`: a thin wrapper around sounddevice.OutputStream.

    Parameter names verified against the installed sounddevice 0.5.5
    source (`OutputStream.__init__`): `finished_callback`, not
    `finish_callback`.
    """
    return sd.OutputStream(
        samplerate=samplerate,
        channels=channels,
        device=device,
        callback=callback,
        finished_callback=finished_callback,
    )


class ToneEngine:
    """Plays a precomputed float32 buffer out through an audio stream, one
    PortAudio callback block at a time.

    `stream_factory(samplerate, channels, device, callback, finished_callback)`
    defaults to a thin wrapper around `sounddevice.OutputStream`; tests
    inject a fake in its place so the whole engine is exercisable without
    real hardware.

    All state (`_stream`, `_buffer`, `_pos`, `_on_done`, `_playing`) is
    protected by a single lock, because the audio callback and
    finished-callback run on PortAudio's own thread(s), not the caller's --
    the same house rule as everywhere else audio callbacks appear in this
    project: never touch Qt, never block, plain-Python only.
    """

    def __init__(self, stream_factory: Optional[Callable] = None):
        self._stream_factory = stream_factory or _default_stream_factory
        self._lock = threading.Lock()
        self._stream = None
        self._buffer: Optional[np.ndarray] = None
        self._pos = 0
        self._on_done: Optional[Callable[[], None]] = None
        self._playing = False

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    def play(
        self,
        buffer: np.ndarray,
        sample_rate: int,
        device: Optional[int] = None,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Start playing `buffer` (shape (n,) or (n, channels)).

        Stops any playback already in progress first (no overlapping
        streams). Raises `AudioError` if the stream fails to open.
        """
        self.stop()

        buf = np.asarray(buffer, dtype=np.float32)
        if buf.ndim == 1:
            buf = buf.reshape(-1, 1)
        channels = buf.shape[1]

        with self._lock:
            self._buffer = buf
            self._pos = 0
            self._on_done = on_done
            self._playing = True

        def _callback(outdata, frames, time_info, status):
            # Never blocks: a single short critical section doing plain
            # numpy slicing, nothing else.
            with self._lock:
                buf_local = self._buffer
                pos = self._pos
                if buf_local is None:
                    outdata[:] = 0
                    raise sd.CallbackStop
                remaining = len(buf_local) - pos
                n = max(0, min(frames, remaining))
                if n > 0:
                    outdata[:n] = buf_local[pos:pos + n]
                if n < frames:
                    outdata[n:] = 0
                self._pos = pos + n
                drained = self._pos >= len(buf_local)
            if drained:
                raise sd.CallbackStop

        def _finished():
            with self._lock:
                self._playing = False
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
            stream = self._stream_factory(sample_rate, channels, device, _callback, _finished)
            stream.start()
        except Exception as exc:
            # If the factory succeeded but start() failed, the stream was
            # still opened (e.g. PortAudio's Pa_OpenStream happens inside
            # the constructor) -- close it so we don't leak it.
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            with self._lock:
                self._playing = False
                self._buffer = None
                self._pos = 0
                self._on_done = None
            raise AudioError(str(exc), kind=_classify_portaudio_error(exc)) from exc

        with self._lock:
            self._stream = stream

    def stop(self) -> None:
        """Stop playback immediately and cleanly. Safe to call when not
        currently playing (no-op, no exception).
        """
        with self._lock:
            stream = self._stream
            self._stream = None
            self._playing = False
            self._buffer = None
            self._pos = 0
            self._on_done = None
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
