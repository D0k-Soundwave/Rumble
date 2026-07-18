"""Test-tone and test-noise generation (Phase 4: Audio Engine Foundation).

Pure numpy signal generators used to drive the playback engine (engine.py,
Task 5) for auditioning an EQ profile's effect on real output. No Qt, no
sounddevice — this module only produces float32 stereo buffers in memory.

Public API:
    generate_sine        -- calibrated sine test tone with raised-cosine fades
    generate_pink_noise   -- calibrated pink (1/f) noise via filtered white noise
    generate_log_sweep    -- calibrated exponential (logarithmic) sine sweep,
                             the excitation signal for mic-sweep time-of-
                             arrival measurement (rumble/core/time_alignment.py)
    dbfs_to_amplitude     -- dBFS -> linear amplitude conversion
"""
from __future__ import annotations

import numpy as np

_VALID_CHANNELS = ("left", "right", "both")


def dbfs_to_amplitude(dbfs: float) -> float:
    """Convert a dBFS level to a linear peak amplitude (1.0 = 0 dBFS)."""
    return 10 ** (dbfs / 20)


def _raised_cosine_fade(n: int, n_fade: int) -> np.ndarray:
    """Build a length-n envelope of ones with raised-cosine (Hann-shaped) fade
    in/out ramps of n_fade samples at each end. n_fade is clamped so the two
    ramps never overlap on very short buffers.
    """
    envelope = np.ones(n, dtype=np.float64)
    if n_fade <= 0:
        return envelope
    n_fade = min(n_fade, n // 2)
    if n_fade <= 0:
        return envelope
    # Raised-cosine (half of a Hann window): 0 -> 1 over n_fade samples.
    ramp = 0.5 * (1 - np.cos(np.pi * np.arange(n_fade) / n_fade))
    envelope[:n_fade] = ramp
    envelope[n - n_fade:] = ramp[::-1]
    return envelope


def generate_sine(
    freq_hz: float,
    duration_s: float,
    level_dbfs: float,
    sample_rate: int,
    channel: str = "both",
    fade_ms: float = 10.0,
) -> np.ndarray:
    """Generate a calibrated sine test tone.

    Returns a float32 array of shape (n, 2). `channel` selects which output
    channel(s) carry the tone ("left", "right", or "both"); the other channel
    (if any) is exactly zero. A raised-cosine fade in/out of `fade_ms`
    milliseconds is applied at the start/end to avoid clicks; fade_ms=0
    disables fading entirely.

    `fade_ms=10.0`: the raised-cosine shape itself is click-proof at any
    positive duration (its derivative is zero at both endpoints, so there's
    no discontinuity for a fade of any length to click on) -- what the 10 ms
    duration actually buys is margin above the ear's onset-detection window
    (roughly 2-5 ms, below which even a mathematically smooth ramp's *rate*
    of rise can still read as an abrupt "thump" rather than a fade). 10 ms is
    a comfortable 2-5x that floor; no single value is uniquely "correct"
    beyond clearing it.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"channel must be one of {_VALID_CHANNELS!r}, got {channel!r}")

    n = int(round(duration_s * sample_rate))
    t = np.arange(n, dtype=np.float64) / sample_rate
    amplitude = dbfs_to_amplitude(level_dbfs)
    tone = amplitude * np.sin(2 * np.pi * freq_hz * t)

    n_fade = int(round((fade_ms / 1000.0) * sample_rate)) if fade_ms else 0
    if n_fade > 0:
        tone *= _raised_cosine_fade(n, n_fade)

    buf = np.zeros((n, 2), dtype=np.float64)
    if channel in ("left", "both"):
        buf[:, 0] = tone
    if channel in ("right", "both"):
        buf[:, 1] = tone

    return buf.astype(np.float32)


def generate_pink_noise(
    duration_s: float,
    level_dbfs: float,
    sample_rate: int,
    seed: int | None = None,
) -> np.ndarray:
    """Generate calibrated pink (1/f) noise, identical on both channels.

    White noise is filtered with the Paul Kellet 3-pole IIR approximation of
    a -3 dB/octave (1/f power, 1/sqrt(f) amplitude) response, then scaled so
    the output RMS exactly matches `level_dbfs`.
    """
    n = int(round(duration_s * sample_rate))
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)

    # Paul Kellet's refined 3-pole pink noise filter.
    b0 = b1 = b2 = 0.0
    pink = np.empty(n, dtype=np.float64)
    for i in range(n):
        w = white[i]
        b0 = 0.99765 * b0 + w * 0.0990460
        b1 = 0.96300 * b1 + w * 0.2965164
        b2 = 0.57000 * b2 + w * 1.0526913
        pink[i] = b0 + b1 + b2 + w * 0.1848

    # Calibrate RMS to the target level exactly.
    target_amplitude = dbfs_to_amplitude(level_dbfs)
    current_rms = np.sqrt(np.mean(pink ** 2))
    if current_rms > 0:
        pink *= target_amplitude / current_rms

    buf = np.empty((n, 2), dtype=np.float64)
    buf[:, 0] = pink
    buf[:, 1] = pink
    return buf.astype(np.float32)


def generate_log_sweep(
    f_start_hz: float,
    f_end_hz: float,
    duration_s: float,
    level_dbfs: float,
    sample_rate: int,
    fade_ms: float = 10.0,
) -> np.ndarray:
    """Generate a calibrated exponential (logarithmic) sine sweep, mono.

    Uses the exponential-sweep formula from A. Farina, "Simultaneous
    Measurement of Impulse Response and Distortion with a Swept-Sine
    Technique" (AES 108th Convention, 2000): every octave gets equal sweep
    time, which spreads the excitation's energy evenly across log-frequency.
    This is the standard excitation for acoustic time-of-arrival / impulse-
    response measurement -- a linear sweep spends most of its duration in
    the top octave and leaves the low end comparatively noisy, which hurts
    exactly the low-frequency correlation precision mic-sweep time-alignment
    (rumble/core/time_alignment.py) depends on.

    Returns a float32 array of shape (n,) -- mono; callers route it into one
    channel of a multichannel buffer rather than this module owning
    channel-count concerns (see time_alignment.build_channel_sweep).
    """
    if f_start_hz <= 0 or f_end_hz <= f_start_hz:
        raise ValueError("f_end_hz must be greater than f_start_hz > 0")

    n = int(round(duration_s * sample_rate))
    t = np.arange(n, dtype=np.float64) / sample_rate
    k = np.log(f_end_hz / f_start_hz)
    phase = (2 * np.pi * f_start_hz * duration_s / k) * (np.exp(t / duration_s * k) - 1)
    amplitude = dbfs_to_amplitude(level_dbfs)
    sweep = amplitude * np.sin(phase)

    n_fade = int(round((fade_ms / 1000.0) * sample_rate)) if fade_ms else 0
    if n_fade > 0:
        sweep *= _raised_cosine_fade(n, n_fade)

    return sweep.astype(np.float32)
