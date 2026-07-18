"""Audio I/O foundation (Phase 4: Audio Engine Foundation).

This package holds RUMBLE's audio playback and capture primitives: test-tone
generation, level/spectrum metering, a playback engine, and WASAPI loopback
capture for verifying a profile's effect on real output. Phases 5-7 (live
metering UI, gear/profile audition, etc.) build on this package -- read this
docstring first before diving into a submodule.

Public API index:
    generate_sine        -- tones.py    -- calibrated sine test tone (raised-
                             cosine fade in/out, mono-to-either-channel or
                             stereo-both)
    generate_pink_noise   -- tones.py    -- calibrated pink (1/f) noise, RMS-
                             matched to an exact dBFS target
    dbfs_to_amplitude     -- tones.py    -- dBFS -> linear peak amplitude
    rms_dbfs              -- metering.py -- RMS level of a block, in dBFS
                             (mono or stereo, floored at -120 dBFS)
    peak_dbfs             -- metering.py -- peak (max abs sample) level of a
                             block, in dBFS (same shape support/floor)
    spectrum_mags         -- metering.py -- Hann-windowed, gain-compensated
                             rFFT magnitude spectrum of a mono block
    find_output_device    -- engine.py   -- resolve a RUMBLE device-name
                             fragment to a sounddevice output device index
                             (None = system default)
    AudioError            -- engine.py   -- raised by ToneEngine.play on
                             stream-open failure; `.kind` in
                             {"exclusive", "no-device", "other"}
    ToneEngine            -- engine.py   -- plays a precomputed float32
                             buffer through an (injectable) sounddevice
                             output stream
    LoopbackCapture       -- capture.py  -- background-thread WASAPI
                             loopback capture (via pyaudiowpatch) with
                             auto-reconnect; `on_block` callback delivery
    InputDevice           -- mic_capture.py -- one enumerated input-capable
                             device (index, name, is_default)
    list_input_devices    -- mic_capture.py -- enumerate sounddevice
                             input-capable devices (the find_output_device
                             input-side twin)
    MicRecorder           -- mic_capture.py -- records a fixed duration from
                             a chosen input into a float32 mono buffer, with
                             block-level clip detection and RMS

Backend notes: tones.py and metering.py are pure numpy (no Qt, no audio
library). engine.py (playback) and mic_capture.py (microphone input) talk to
hardware via `sounddevice`. capture.py (loopback) talks to hardware via
`pyaudiowpatch` -- a separate, Windows-only backend, since sounddevice has
no WASAPI loopback support (see Task 5 / progress ledger). All
hardware-facing modules run their callbacks on a non-Qt thread; callbacks
must never touch Qt, block, or raise anything unexpected.
"""
from __future__ import annotations

from rumble.core.audio.tones import generate_sine, generate_pink_noise, dbfs_to_amplitude
from rumble.core.audio.metering import rms_dbfs, peak_dbfs, spectrum_mags
from rumble.core.audio.engine import find_output_device, AudioError, ToneEngine
from rumble.core.audio.capture import LoopbackCapture
from rumble.core.audio.mic_capture import InputDevice, list_input_devices, MicRecorder

__all__ = [
    "generate_sine",
    "generate_pink_noise",
    "dbfs_to_amplitude",
    "rms_dbfs",
    "peak_dbfs",
    "spectrum_mags",
    "find_output_device",
    "AudioError",
    "ToneEngine",
    "LoopbackCapture",
    "InputDevice",
    "list_input_devices",
    "MicRecorder",
]
