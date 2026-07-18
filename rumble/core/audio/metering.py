"""Level and spectrum metering (Phase 4: Audio Engine Foundation).

Pure numpy metering primitives used by the live level meter / spectrum
analyzer UI. No Qt, no sounddevice — these functions take plain numpy
buffers in and return plain numbers/arrays out.

Public API:
    rms_dbfs               -- RMS level of a block, in dBFS (floored at -120)
    peak_dbfs               -- peak (max abs sample) level of a block, in dBFS (floored at -120)
    peak_dbfs_per_channel   -- per-channel peak level of a (n, channels) block, in dBFS
    rms_dbfs_per_channel    -- per-channel RMS level of a (n, channels) block, in dBFS
    StereoLevels            -- named (peak_l, peak_r, rms_l, rms_r) tuple
    stereo_levels           -- StereoLevels for an (n, 2) stereo block
    spectrum_mags           -- Hann-windowed, gain-compensated rFFT magnitude spectrum
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np

# Floor applied to both level meters and the spectrum magnitude estimate so
# that near-silence (or literal all-zero) input never produces -inf or a
# math-domain error out of log10(0).
#
# -120 dBFS is NOT "below the theoretical N-bit quantization floor" for
# 24-bit (theoretical SNR = 6.02*N + 1.76 dB puts that at ~-146 dBFS, well
# below -120). It IS below the real-world noise floor of the actual hardware
# this reads from: this module measures live WASAPI loopback capture off
# real consumer/prosumer DACs/ADCs, not synthetic bit-exact data, and no
# real converter reaches the theoretical 24-bit number -- AES17-measured
# dynamic range for good prosumer/pro interfaces clusters around 110-120 dB,
# i.e. a real noise floor around -110 to -120 dBFS. -120 sits at the
# generous end of that real-world range (16-bit's real floor, ~-98 dBFS
# theoretical, is comfortably above it too).
FLOOR_DBFS = -120.0
_FLOOR_AMPLITUDE = 10 ** (FLOOR_DBFS / 20.0)


def _amplitude_to_dbfs(amplitude: float) -> float:
    """Convert a linear amplitude to dBFS, floored at FLOOR_DBFS."""
    return 20.0 * np.log10(max(float(amplitude), _FLOOR_AMPLITUDE))


def rms_dbfs(block: np.ndarray) -> float:
    """RMS level of `block` in dBFS (0 dBFS = full-scale amplitude 1.0).

    `block` may be mono (n,) or stereo (n, 2); all samples (both channels,
    if present) contribute equally to the RMS. Floored at -120 dBFS so
    silence (or any near-zero block) returns a finite number, never -inf
    or NaN.
    """
    x = np.asarray(block, dtype=np.float64)
    if x.size == 0:
        return FLOOR_DBFS
    rms = np.sqrt(np.mean(np.square(x)))
    return _amplitude_to_dbfs(rms)


def peak_dbfs(block: np.ndarray) -> float:
    """Peak (max absolute sample) level of `block` in dBFS.

    Same shape support and -120 dBFS floor behavior as `rms_dbfs`.
    """
    x = np.asarray(block, dtype=np.float64)
    if x.size == 0:
        return FLOOR_DBFS
    peak = np.max(np.abs(x))
    return _amplitude_to_dbfs(peak)


def peak_dbfs_per_channel(block: np.ndarray) -> np.ndarray:
    """Per-channel peak level of `block` in dBFS.

    Unlike `peak_dbfs`, this does NOT collapse the channel axis -- it exists
    specifically for a stereo (or multichannel) level meter that needs each
    channel's own reading. `block` must be 2-D `(n_frames, channels)`; callers
    that may have mono or differently-shaped data (e.g. `LoopbackCapture`
    across a device change) must reduce to a fixed channel count themselves
    first, the same separation `spectrum_mags` already enforces for FFT input
    (it raises rather than guessing a shape) -- this function raises too.
    """
    x = np.asarray(block, dtype=np.float64)
    if x.ndim != 2:
        raise ValueError(f"peak_dbfs_per_channel expects a 2-D (n, channels) "
                          f"block, got shape {x.shape!r}")
    if x.shape[0] == 0:
        return np.full(x.shape[1], FLOOR_DBFS)
    peaks = np.max(np.abs(x), axis=0)
    return np.array([_amplitude_to_dbfs(p) for p in peaks])


def rms_dbfs_per_channel(block: np.ndarray) -> np.ndarray:
    """Per-channel RMS level of `block` in dBFS.

    Same 2-D shape contract and -120 dBFS floor behavior as
    `peak_dbfs_per_channel`.
    """
    x = np.asarray(block, dtype=np.float64)
    if x.ndim != 2:
        raise ValueError(f"rms_dbfs_per_channel expects a 2-D (n, channels) "
                          f"block, got shape {x.shape!r}")
    if x.shape[0] == 0:
        return np.full(x.shape[1], FLOOR_DBFS)
    rms = np.sqrt(np.mean(np.square(x), axis=0))
    return np.array([_amplitude_to_dbfs(r) for r in rms])


class StereoLevels(NamedTuple):
    """One tick's worth of per-channel level readings for a 2-channel block."""
    peak_l: float
    peak_r: float
    rms_l: float
    rms_r: float


def stereo_levels(block_stereo: np.ndarray) -> StereoLevels:
    """`StereoLevels` for an exactly-2-channel `(n, 2)` block.

    Thin convenience wrapper combining `peak_dbfs_per_channel` and
    `rms_dbfs_per_channel` into one typed payload, so callers that cross a Qt
    signal boundary (e.g. the live VU/Peak meter) pass a single named object
    instead of a bare tuple.
    """
    peaks = peak_dbfs_per_channel(block_stereo)
    rms = rms_dbfs_per_channel(block_stereo)
    return StereoLevels(peak_l=float(peaks[0]), peak_r=float(peaks[1]),
                         rms_l=float(rms[0]), rms_r=float(rms[1]))


def spectrum_mags(
    block_mono: np.ndarray,
    sample_rate: int,
    n_fft: int = 2048,
) -> tuple[np.ndarray, np.ndarray]:
    """Hann-windowed rFFT magnitude spectrum of a mono block.

    `n_fft=2048`'s default is NOT a considered choice for the live spectrum
    feature -- no production caller uses it. `rumble/ui/spectrum_overlay.py`
    (Phase 6) always passes an explicit `n_fft=8192`, derived there from the
    EQ graph's own 10-20 Hz floor (2048 samples collapses a 20 Hz cycle into
    a single bin) and from this module's sibling `LoopbackCapture`'s
    already-fixed 1024-sample chunk size (8192 = 8x1024, see capture.py).
    This bare default only fires in tests / any future direct caller that
    doesn't have that feature's latency/resolution constraints; kept as a
    reasonable general-purpose fallback (43 ms window at 48 kHz), not as a
    tuned value for any specific RUMBLE feature.

    Returns `(freqs_hz, mags_db)`, both length `n_fft // 2 + 1`. `block_mono`
    is truncated or zero-padded to exactly `n_fft` samples.

    Window-gain compensation (derivation):
        For a sinusoid of linear amplitude A sitting exactly on FFT bin k0
        (0 < k0 < n_fft/2), write it as a sum of two complex exponentials at
        +k0 and -k0, each of amplitude A/2. Windowing multiplies the signal
        by w[n] before the DFT, which (by the convolution theorem) smears
        each exponential's contribution across nearby bins; evaluated at the
        bin it's centered on, the windowed DFT coefficient is

            X_w[k0] ~= (A/2) * sum(w[n]) = (A/2) * mean(w) * n_fft

        (the -k0 term's contribution at k0 is negligible for any reasonable
        window/length, since it aliases in from far away). So the linear
        amplitude is recovered as

            A = |X_w[k0]| / (mean(w) * n_fft / 2)

        i.e. compensate by the window's coherent gain (mean(w), the mean of
        the window function -- how much of the window's own DC gain survives
        vs. a rectangular window) times n_fft/2 (the one-sided-spectrum
        scaling: half the full-length transform, doubled back out because
        the two-sided energy folds into one bin). DC (k=0) and Nyquist
        (k=n_fft/2, only present when n_fft is even) have no +/- pair to
        fold, so they use half that gain (mean(w) * n_fft, no factor of 2).
    """
    x = np.asarray(block_mono, dtype=np.float64)
    if x.ndim != 1:
        raise ValueError(f"spectrum_mags expects a mono (1-D) block, got shape {x.shape!r}")

    n = x.shape[0]
    if n < n_fft:
        x = np.pad(x, (0, n_fft - n))
    elif n > n_fft:
        x = x[:n_fft]

    window = np.hanning(n_fft)
    coherent_gain = window.mean()

    spectrum = np.fft.rfft(x * window)
    freqs_hz = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    one_sided_scale = coherent_gain * (n_fft / 2.0)
    mags_linear = np.abs(spectrum) / one_sided_scale
    mags_linear[0] *= 0.5           # DC: no folding partner
    if n_fft % 2 == 0:
        mags_linear[-1] *= 0.5      # Nyquist: no folding partner

    mags_db = 20.0 * np.log10(np.maximum(mags_linear, _FLOOR_AMPLITUDE))
    return freqs_hz, mags_db
