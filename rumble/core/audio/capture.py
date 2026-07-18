"""WASAPI loopback capture with auto-reconnect (Phase 4: Audio Engine
Foundation, Task 5).

Owns real WASAPI loopback input streams via `pyaudiowpatch` -- a Windows-only
PyAudio fork purpose-built for loopback capture (plain `sounddevice`, any
version including the one this project also depends on for playback, has NO
loopback support at all: verified empirically during Task 1). Playback
(engine.py) and capture (this module) are deliberately separate backends;
they do not share a stream_factory shape or a PortAudio/PyAudio manager.

Still Qt-free: `on_block` is invoked from the capture thread, never the
caller's thread and never Qt's -- see design spec Sec4 point 2 ("audio
callbacks never touch Qt, never block, plain-Python callbacks").

Threading/COM invariant (load-bearing -- do NOT move the open back onto the
caller): the ENTIRE PortAudio/WASAPI lifecycle (`PyAudio()` init, device
enumeration, `open`, every `read`, and `close`) runs on the one background
capture thread, which puts itself in the COM multi-threaded apartment first
(see `_ComApartment`). Opening on the caller (Qt main/STA) thread instead --
as this module originally did -- apartment-binds the WASAPI COM objects to
that STA, so every worker `read` marshals back into it and is refused with
RPC_E_CANTCALLOUT_ININPUTSYNCCALL (0x8001010d) whenever the STA is mid
input-synchronous `SendMessage` dispatch under a live Qt event loop.

`stream_factory` seam
----------------------
`stream_factory(device_name_fragment) -> stream_like_object` where the
returned object exposes:
    .sample_rate : int          -- threaded through to on_block
    .channels    : int          -- informational; blocks already carry this
                                    in their shape
    .read(n_frames) -> np.ndarray, shape (n_frames, channels), float32
                                 -- blocking; may raise on device death
    .close()                     -- tear down the stream (and, for the real
                                    backend, the owning PyAudio() manager)

Choosing "already-decoded numpy blocks" (rather than raw bytes) for
`.read()`'s return value keeps the interleaved-float32 decode logic
(`np.frombuffer(...).reshape(-1, channels)`) inside the default factory's
thin wrapper, next to where the raw PyAudioWPatch bytes actually come from,
instead of duplicating it in every stream_factory a test writes.

The default factory wraps `pyaudiowpatch` exactly per the empirically
verified API (`PyAudio.get_default_wasapi_loopback()` for the current
default output's loopback device, `get_loopback_device_info_generator()` to
search by name fragment via `pattern_matches_text`, blocking-mode
`PyAudio.open(...)` + `Stream.read(n, exception_on_overflow=False)`).

Public API:
    LoopbackCapture -- background-thread loopback capture with reconnect
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np
import pyaudiowpatch as pyaudio

from rumble.core.audio_devices import pattern_matches_text

# 1024 samples (~21.3 ms @ 48kHz): a conventional real-time-audio block size
# balancing responsiveness (well under the ~100ms threshold for perceived-
# instant UI feedback) against per-read overhead. LOAD-BEARING beyond that:
# rumble/ui/spectrum_overlay.py (Phase 6) fixes its FFT size and ring buffer
# at exactly 8x this value (8192 = 8*1024) and says so in its own comment --
# changing this number without checking that file will silently break its
# derivation.
_DEFAULT_CHUNK_SIZE = 1024
# Reconnect backoff after a dead stream: a UX/robustness tuning value (fast
# enough recovery to feel responsive, slow enough not to hammer a possibly
# still-broken device/COM layer with repeated open attempts). No formula
# pins this exact number -- unlike _DEFAULT_CHUNK_SIZE above, nothing else
# in the project depends on it being precisely 2.0.
_DEFAULT_RETRY_INTERVAL = 2.0

# How long `.start()` blocks waiting for the worker thread to attempt the first
# open, so `.status` is settled ("running"/"unavailable") synchronously by the
# time `.start()` returns (preserving the pre-existing synchronous-status
# contract even though the open now happens on the worker, not the caller).
# `pyaudiowpatch`'s open is a fast synchronous C call; this only bounds a
# pathological wedge so the GUI thread can never hang on it.
_OPEN_TIMEOUT = 10.0


class _ComApartment:
    """RAII-style guard that puts the CURRENT thread into the COM
    multi-threaded apartment (MTA) for its lifetime.

    WHY THIS EXISTS (root-cause fix for RPC_E_CANTCALLOUT_ININPUTSYNCCALL /
    0x8001010d): the WASAPI loopback COM objects (`IAudioClient` /
    `IAudioCaptureClient`) are apartment-bound to whatever thread *creates*
    them. When they were created on the Qt main thread's single-threaded
    apartment (the old code opened the stream in `.start()`, on the caller's
    thread), every `.read()` from the capture worker had to marshal back into
    that STA -- and when the STA was mid input-synchronous `SendMessage`
    dispatch (routine under a live Qt event loop), COM refused the marshalled
    call with RPC_E_CANTCALLOUT_ININPUTSYNCCALL, which surfaced as an
    uncatchable Windows structured exception inside `read()`. Creating the
    objects on an MTA thread instead makes them free-threaded: any thread can
    call them directly with no marshalling and no STA involvement, so the
    input-sync condition can never arise.

    `CoInitializeEx(COINIT_MULTITHREADED)` must run BEFORE any PortAudio call
    on this thread: `PyAudio()` (Pa_Initialize) itself calls
    `CoInitializeEx(..., COINIT_APARTMENTTHREADED)` on the calling thread, and
    whichever apartment gets established first wins. Establishing MTA first
    makes PortAudio's later STA request return RPC_E_CHANGED_MODE (which
    PortAudio tolerates), leaving the thread -- and the objects it creates --
    in the MTA. Best-effort and never raises: if COM can't be initialised the
    capture still runs (degrading to the old marshalling behaviour), so a
    COM-init failure never takes the feature down.
    """

    _COINIT_MULTITHREADED = 0x0
    _S_OK = 0
    _S_FALSE = 1
    _RPC_E_CHANGED_MODE = 0x80010106

    def __init__(self) -> None:
        self._owns_com = False

    def __enter__(self) -> "_ComApartment":
        try:
            import ctypes

            hr = ctypes.windll.ole32.CoInitializeEx(None, self._COINIT_MULTITHREADED)
            hr &= 0xFFFFFFFF
            # Own (and must later balance with CoUninitialize) only when THIS
            # call actually initialised COM. RPC_E_CHANGED_MODE means the
            # thread was already committed to a different apartment by someone
            # else -- we neither initialised it nor may we uninitialise it.
            self._owns_com = hr in (self._S_OK, self._S_FALSE)
        except Exception:  # pragma: no cover - platform/COM edge
            self._owns_com = False
        return self

    def __exit__(self, *exc_info) -> bool:
        if self._owns_com:
            try:
                import ctypes

                ctypes.windll.ole32.CoUninitialize()
            except Exception:  # pragma: no cover - platform/COM edge
                pass
            self._owns_com = False
        return False


class _PyAudioLoopbackStream:
    """Thin wrapper around a real PyAudioWPatch blocking-mode stream.

    Decodes the interleaved float32 PCM bytes `Stream.read()` returns into
    an (n_frames, channels) numpy array, and owns tearing down both the
    stream and its PyAudio() manager instance on `.close()`.
    """

    def __init__(self, pa, stream, sample_rate: int, channels: int):
        self._pa = pa
        self._stream = stream
        self.sample_rate = sample_rate
        self.channels = channels

    def read(self, n_frames: int) -> np.ndarray:
        raw = self._stream.read(n_frames, exception_on_overflow=False)
        arr = np.frombuffer(raw, dtype=np.float32)
        return arr.reshape(-1, self.channels)

    def close(self) -> None:
        try:
            self._stream.stop_stream()
        except Exception:
            pass
        try:
            self._stream.close()
        except Exception:
            pass
        try:
            self._pa.terminate()
        except Exception:
            pass


def _resolve_loopback_device(pa, device_name_fragment: Optional[str]):
    """Find the WASAPI loopback device info dict for `device_name_fragment`.

    `None` means "loopback for the current default output" --
    `get_default_wasapi_loopback()`. Otherwise every loopback device's
    `name` (which already carries the "<output name> [Loopback]"
    convention) is matched against `device_name_fragment` using the same
    APO-style fuzzy matching as the rest of the project
    (`pattern_matches_text`). Returns `None` if nothing matches.
    """
    if device_name_fragment is None:
        try:
            return pa.get_default_wasapi_loopback()
        except Exception:
            return None
    for info in pa.get_loopback_device_info_generator():
        if pattern_matches_text(device_name_fragment, info.get("name", "")):
            return info
    return None


def _make_default_stream_factory(chunk_size: int) -> Callable[[Optional[str]], "_PyAudioLoopbackStream"]:
    """Build the default `stream_factory`: a real PyAudioWPatch loopback
    stream, opened blocking-mode, wrapped in `_PyAudioLoopbackStream`.

    `chunk_size` is baked in via closure since `stream_factory`'s public
    shape (per the injection seam) only takes `device_name_fragment`.
    """

    def factory(device_name_fragment: Optional[str]) -> "_PyAudioLoopbackStream":
        pa = pyaudio.PyAudio()
        try:
            info = _resolve_loopback_device(pa, device_name_fragment)
            if info is None:
                raise RuntimeError(
                    f"No WASAPI loopback device found for {device_name_fragment!r}"
                )
            channels = int(info.get("maxInputChannels") or 2)
            sample_rate = int(info["defaultSampleRate"])
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=sample_rate,
                input=True,
                input_device_index=info["index"],
                frames_per_buffer=chunk_size,
            )
        except Exception:
            try:
                pa.terminate()
            except Exception:
                pass
            raise
        return _PyAudioLoopbackStream(pa, stream, sample_rate, channels)

    return factory


class LoopbackCapture:
    """Captures WASAPI loopback audio on a background daemon thread,
    delivering each chunk to `on_block`, with automatic reconnect if the
    stream dies mid-capture.

    All cross-thread state (`_status`) is protected by a lock. `_stop_event`
    is a `threading.Event` used both as the "please stop" signal and as the
    sleep primitive during reconnect backoff -- `Event.wait(interval)`
    returns immediately once `.stop()` sets the event, so `.stop()` never
    has to wait out a full retry interval even when it isn't overridden to
    something test-short.

    Status semantics:
        "stopped"     -- before any `.start()`, or after an explicit
                          `.stop()`.
        "running"     -- actively capturing (including while transiently
                          retrying a reconnect after a stream death --
                          status intentionally does not flap during
                          reconnect, since the device was already proven
                          available once and blocks resume as soon as the
                          retry succeeds).
        "unavailable" -- the very first `.start()` attempt could not open
                          the stream at all (exclusive-locked device, no
                          loopback device found, etc). This is a terminal
                          outcome for that `.start()` call -- it is NOT
                          retried forever; call `.start()` again explicitly
                          to retry.
    """

    def __init__(
        self,
        on_block: Callable[[np.ndarray, int], None],
        stream_factory: Optional[Callable[[Optional[str]], object]] = None,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        retry_interval: float = _DEFAULT_RETRY_INTERVAL,
    ):
        self._on_block = on_block
        self._chunk_size = chunk_size
        self._stream_factory = stream_factory or _make_default_stream_factory(chunk_size)
        self._retry_interval = retry_interval

        self._lock = threading.Lock()
        self._status = "stopped"
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def _set_status(self, value: str) -> None:
        with self._lock:
            self._status = value

    def start(self, device_name_fragment: Optional[str] = None) -> None:
        """Open the loopback stream and begin capturing on a background
        daemon thread.

        Stops any capture already in progress first (no overlapping
        streams -- same convention as `ToneEngine.play`).

        The stream is opened on the background worker thread (NOT the
        caller's thread) so the WASAPI COM objects are created inside that
        thread's multi-threaded apartment -- see `_ComApartment` for the
        full root-cause explanation. To preserve the pre-existing
        synchronous-status contract regardless, `.start()` blocks until the
        worker has attempted that first open: `.status` is settled by the
        time this returns. If the open fails (or wedges past
        `_OPEN_TIMEOUT`), `.status` becomes `"unavailable"`, no retry thread
        is left running (`_thread` is reaped back to `None`), and this
        returns without raising. If it succeeds, `.status` becomes
        `"running"` and the worker keeps reading (and, if the stream later
        dies, reconnecting) on that same MTA thread.
        """
        self.stop()
        self._stop_event = threading.Event()
        stop_event = self._stop_event
        open_done = threading.Event()

        thread = threading.Thread(
            target=self._run,
            args=(device_name_fragment, stop_event, open_done),
            daemon=True,
        )
        self._thread = thread
        thread.start()

        if not open_done.wait(_OPEN_TIMEOUT):
            # Pathological: the open is wedged (a blocking C call that never
            # returned). Don't hang the caller (in production, the Qt GUI
            # thread) on it -- report unavailable and signal the worker to
            # stop trying. The wedged thread is abandoned (daemon), which is
            # strictly better than the old behaviour of freezing the caller.
            self._set_status("unavailable")
            stop_event.set()

        if self.status != "running":
            # Failed / timed-out open: reap the worker so `_thread` is None,
            # matching the old synchronous contract (a failed start leaves no
            # lingering thread). On a clean open failure the worker has
            # already returned, so this join is immediate.
            self._thread = None
            if thread.is_alive():
                thread.join(timeout=5.0)

    def stop(self) -> None:
        """Stop capture cleanly and join the background thread. Safe to
        call when not currently running (no-op, no exception), and safe
        to call twice.
        """
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._set_status("stopped")

    def _run(
        self,
        device_name_fragment: Optional[str],
        stop_event: threading.Event,
        open_done: threading.Event,
    ) -> None:
        """Background thread body: own the ENTIRE PortAudio/WASAPI lifecycle
        for this capture generation -- open, read, reconnect, and close --
        inside this thread's multi-threaded apartment, then deliver blocks
        via `on_block` until `.stop()` is called.

        Everything COM-touching lives here, on this one thread, by design:
        `_ComApartment` puts the thread in the MTA before `_stream_factory`
        creates the WASAPI objects, so those objects are free-threaded and
        never marshal back to the Qt main STA (the RPC_E_CANTCALLOUT_
        ININPUTSYNCCALL root cause -- see `_ComApartment`). The old code
        opened the stream on the caller's (main) thread and only read here;
        that is exactly what bound the objects to the STA.

        `open_done` is set once the first open attempt has resolved (success
        or failure), unblocking `.start()` so it can report a settled
        `.status` synchronously. `stop_event` is captured by `.start()` from
        `self._stop_event` at spawn time and threaded through explicitly
        rather than read off `self`: if a later `.start()` replaces
        `self._stop_event` with a fresh `Event` (e.g. after a `.stop()` join
        timed out on a wedged thread), a stale thread reading `self._stop_
        event` would observe the NEW, unset event and run forever alongside
        the new generation, double-delivering blocks. Binding to the local
        captured `stop_event` means this thread only ever observes the event
        that was live when IT started, which `.stop()` always `.set()`s
        before any later `.start()` reassigns `self._stop_event`.

        Never raises out of the thread -- every exception from the open, a
        device read, a reconnect attempt, or `on_block` itself is caught
        here (and `open_done` is set no matter what, so `.start()` can never
        be left blocked).
        """
        stream = None
        try:
            with _ComApartment():
                try:
                    stream = self._stream_factory(device_name_fragment)
                    self._set_status("running")
                except Exception:
                    self._set_status("unavailable")
                    return
                finally:
                    # Resolve `.start()`'s wait on either outcome, and always
                    # before the read loop below could block.
                    open_done.set()

                while not stop_event.is_set():
                    try:
                        block = stream.read(self._chunk_size)
                    except Exception:
                        try:
                            stream.close()
                        except Exception:
                            pass
                        stream = self._reconnect(device_name_fragment, stop_event)
                        if stream is None:
                            return  # .stop() was called during reconnect
                        continue

                    try:
                        self._on_block(block, stream.sample_rate)
                    except Exception:
                        pass  # a broken on_block must never kill the capture thread
        except Exception:
            pass  # belt-and-braces: this thread must never crash out
        finally:
            # Guarantee `.start()` is never left blocked even if something
            # above the inner try bypassed the normal path.
            open_done.set()
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _reconnect(self, device_name_fragment: Optional[str], stop_event: threading.Event):
        """Retry opening the stream every `retry_interval` seconds until it
        succeeds or `.stop()` is called. Returns the new stream, or `None`
        if `.stop()` won the race.

        `stop_event` is the same instance `._run()` captured at
        thread-start and passes down here explicitly -- see `._run()`'s
        docstring for why this must not be read dynamically off `self`.

        Status is deliberately left as `"running"` throughout -- the
        device was already open once, so this is a transient hiccup, not
        an "unavailable" terminal state.
        """
        while not stop_event.is_set():
            try:
                return self._stream_factory(device_name_fragment)
            except Exception:
                pass
            if stop_event.wait(self._retry_interval):
                return None
        return None
