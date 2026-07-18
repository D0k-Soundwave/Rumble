"""Convolution impulse-response analysis for the graph and preamp maths.

Pure numpy + struct — no Qt, no scipy. Reads the IR WAV with a minimal RIFF
chunk walker rather than the stdlib ``wave`` module because ``wave`` rejects
IEEE-float WAVs (format 3), the most common impulse-response export format.
Supported: PCM 16/24/32-bit and float 32/64, plain or WAVE_FORMAT_EXTENSIBLE,
any channel count (channels are averaged).

The response is the FFT magnitude in dB, fractional-octave smoothed
(power-domain) and sampled onto the caller's log-frequency grid — absolute,
including the IR's overall gain, which is exactly what the anti-clipping
preamp calculation needs. No normalisation, no time-domain resampling: a bin
at f Hz is the response at f Hz; frequencies above the IR's Nyquist clamp to
the Nyquist value. ``check_rate_mismatch()`` below is the dynamic check the
ConvolutionEditor uses to warn when the IR's own rate actually differs from
the device's — Equalizer APO does not resample convolution IRs at load time.

Results are cached per (path, mtime_ns) — including failures, so a broken
file is not re-read on every keystroke.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from rumble.core import eq_math

# Read at most this many seconds of IR — room correction IRs are typically a
# fraction of a second; anything longer adds nothing to the response preview.
_MAX_IR_SECONDS = 10.0

_FLOOR_POWER = 1e-30  # -300 dB — keeps log10 finite on silent bins

_cache: dict[tuple[str, int], "IRResult | None"] = {}


@dataclass
class IRResult:
    """An analysed impulse response, sampled on the graph frequency grid."""
    freqs: np.ndarray         # the grid the response was sampled on (Hz)
    response_db: np.ndarray   # absolute magnitude in dB, incl. overall gain
    peak_db: float            # max of response_db
    ir_sample_rate: int       # the WAV's own sample rate


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV file into (mono float64 samples, sample_rate).

    Channels are averaged. Raises ValueError/OSError on anything unreadable.
    """
    blob = Path(path).read_bytes()
    if len(blob) < 12 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")

    fmt_body: bytes | None = None
    data_body: bytes | None = None
    pos = 12
    while pos + 8 <= len(blob):
        chunk_id = blob[pos:pos + 4]
        size = struct.unpack_from("<I", blob, pos + 4)[0]
        body = blob[pos + 8:pos + 8 + size]
        if chunk_id == b"fmt ":
            fmt_body = body
        elif chunk_id == b"data":
            data_body = body
        pos += 8 + size + (size & 1)  # chunks are word-aligned

    if fmt_body is None or len(fmt_body) < 16 or data_body is None:
        raise ValueError("missing fmt/data chunk")

    tag, channels, fs, _byte_rate, _block_align, bits = struct.unpack_from(
        "<HHIIHH", fmt_body, 0)
    if tag == 0xFFFE and len(fmt_body) >= 26:
        # WAVE_FORMAT_EXTENSIBLE: the real format tag leads the SubFormat GUID.
        tag = struct.unpack_from("<H", fmt_body, 24)[0]
    if channels < 1 or fs <= 0:
        raise ValueError("invalid fmt chunk")

    bytes_per = bits // 8
    if bytes_per == 0:
        raise ValueError("invalid bit depth")
    frames = len(data_body) // (bytes_per * channels)
    frames = min(frames, int(_MAX_IR_SECONDS * fs))
    if frames == 0:
        raise ValueError("no audio frames")
    payload = data_body[:frames * bytes_per * channels]

    if tag == 1:  # integer PCM
        if bits == 16:
            samples = np.frombuffer(payload, dtype="<i2").astype(np.float64) / 32768.0
        elif bits == 24:
            raw = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3).astype(np.int64)
            vals = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
            vals -= (vals & 0x800000) << 1  # sign-extend
            samples = vals.astype(np.float64) / 8388608.0
        elif bits == 32:
            samples = np.frombuffer(payload, dtype="<i4").astype(np.float64) / 2147483648.0
        else:
            raise ValueError(f"unsupported PCM bit depth {bits}")
    elif tag == 3:  # IEEE float
        if bits == 32:
            samples = np.frombuffer(payload, dtype="<f4").astype(np.float64)
        elif bits == 64:
            samples = np.frombuffer(payload, dtype="<f8").astype(np.float64)
        else:
            raise ValueError(f"unsupported float bit depth {bits}")
    else:
        raise ValueError(f"unsupported WAV format tag {tag}")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, int(fs)


def ir_response(samples: np.ndarray, fs: int, freqs: np.ndarray,
                smoothing_oct: float = 1 / 6) -> np.ndarray:
    """FFT magnitude of *samples* in dB, sampled on the *freqs* grid.

    Power-domain fractional-octave smoothing keeps the raw FFT's comb ripple
    out of the curve (and out of the preamp peak) without moving broad
    features. Grid frequencies above fs/2 clamp to the Nyquist value.
    """
    n = len(samples)
    nfft = 1 << max(14, int(n - 1).bit_length())
    power = np.abs(np.fft.rfft(samples, nfft)) ** 2
    bin_freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    nyquist = fs / 2.0

    centre = np.minimum(np.asarray(freqs, dtype=np.float64), nyquist)
    half_window = 2.0 ** (smoothing_oct / 2.0)
    lo_idx = np.searchsorted(bin_freqs, centre / half_window, side="left")
    hi_idx = np.searchsorted(bin_freqs, centre * half_window, side="right")

    cumulative = np.concatenate(([0.0], np.cumsum(power)))
    counts = hi_idx - lo_idx
    sums = cumulative[hi_idx] - cumulative[lo_idx]
    mean_power = np.divide(sums, counts, out=np.zeros_like(sums),
                           where=counts > 0)

    # Windows narrower than one bin (possible at the very bottom of the grid)
    # fall back to log-frequency interpolation between bins.
    empty = counts <= 0
    if np.any(empty):
        valid = bin_freqs > 0
        mean_power[empty] = np.interp(np.log10(centre[empty]),
                                      np.log10(bin_freqs[valid]), power[valid])

    return 10.0 * np.log10(np.maximum(mean_power, _FLOOR_POWER))


def get_ir_response(path: Path, freqs: np.ndarray | None = None) -> IRResult | None:
    """Analyse the IR at *path*, cached by (path, mtime). None on any failure.

    With the default grid (``eq_math.frequency_array()``) results are cached;
    a custom *freqs* grid bypasses the cache.
    """
    path = Path(path)
    try:
        key = (str(path).lower(), path.stat().st_mtime_ns)
    except OSError:
        return None

    use_cache = freqs is None
    if use_cache and key in _cache:
        return _cache[key]

    grid = eq_math.frequency_array() if freqs is None else freqs
    try:
        samples, fs = read_wav(path)
        response = ir_response(samples, fs, grid)
        result: IRResult | None = IRResult(
            freqs=grid, response_db=response,
            peak_db=float(np.max(response)), ir_sample_rate=fs)
    except (OSError, ValueError, struct.error):
        result = None

    if use_cache:
        _cache[key] = result
    return result


def _khz_label(rate: int) -> str:
    """Format a sample rate in kHz without a trailing ``.0`` (e.g. 44100 ->
    "44.1", 48000 -> "48") — mirrors device_format.DeviceFormat.label's
    formatting without importing it (this module stays Qt/device-format
    free)."""
    return f"{rate / 1000:g}"


def check_rate_mismatch(ir_rate: int | None, device_rate: int | None) -> str | None:
    """Compare an IR's own embedded sample rate against the device's
    effective rate. Pure function, no I/O.

    Returns None when the rates match, or when either rate is unknown/zero
    (no false alarms on incomplete information) — otherwise a human-readable
    warning naming both rates. Equalizer APO does not resample convolution
    IRs at load time, so a mismatch produces a frequency-scaled response.
    """
    if not ir_rate or not device_rate:
        return None
    ir_rate, device_rate = int(ir_rate), int(device_rate)
    if ir_rate == device_rate:
        return None
    return (f"IR is {_khz_label(ir_rate)} kHz but the device runs "
            f"{_khz_label(device_rate)} kHz — Equalizer APO will not "
            "resample; the response will be wrong.")
