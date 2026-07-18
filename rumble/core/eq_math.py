"""Biquad transfer-function math for the live frequency response graph.

All formulas follow the Audio EQ Cookbook (Robert Bristow-Johnson). Shelf
filters with a fixed slope (6dB/oct, 12dB/oct) are approximated with fixed Q
values chosen to resemble first- and second-order shelving responses — APO's
exact internal implementation may differ slightly, but the visual result is a
close approximation, which is all the graph promises.
"""
from __future__ import annotations

import math

import numpy as np

from rumble.core.apo_config import DEFAULT_SAMPLE_RATE, Filter
GRAPH_POINTS = 512
GRAPH_FMIN = 10.0
GRAPH_FMAX = 24000.0

_FIXED_SLOPE_Q = {
    "LS 6dB": 0.5,
    "LS 12dB": 0.707,
    "HS 6dB": 0.5,
    "HS 12dB": 0.707,
}


def q_to_bw(q: float) -> float:
    """Convert Q factor to bandwidth in octaves (Audio EQ Cookbook relation)."""
    q = max(q, 0.01)
    return (2.0 / math.log(2.0)) * math.asinh(1.0 / (2.0 * q))


def bw_to_q(bw_oct: float) -> float:
    """Convert bandwidth in octaves to Q factor (inverse of q_to_bw)."""
    bw_oct = max(bw_oct, 0.001)
    return math.sqrt(2.0 ** bw_oct) / (2.0 ** bw_oct - 1.0)


def _warp_factor(fc: float, sample_rate: int) -> float:
    """w0/sin(w0) bilinear-transform prewarping term from the Audio EQ
    Cookbook's bandwidth-specified alpha: `alpha = sin(w0) * sinh(ln(2)/2 *
    BW * w0/sin(w0))`. Equals 1.0 at low Fc and grows without bound as Fc
    approaches Nyquist (w0 -> pi), which is the exact reason q_to_bw/bw_to_q
    above (which omit this term) increasingly diverge from reality there."""
    w0 = 2.0 * math.pi * max(fc, 1.0) / sample_rate
    return w0 / math.sin(w0)


def q_to_bw_exact(q: float, fc: float, sample_rate: int) -> float:
    """Q-to-bandwidth using the full RBJ Cookbook warped relation (see
    `bw_to_q_exact`), inverted. Agrees with the unwarped `q_to_bw()` far
    from Nyquist and diverges from it near Nyquist."""
    return q_to_bw(q) / _warp_factor(fc, sample_rate)


def bw_to_q_exact(bw_oct: float, fc: float, sample_rate: int) -> float:
    """Bandwidth-to-Q via the full Audio EQ Cookbook relation, including the
    w0/sin(w0) prewarp term the plain `bw_to_q()` omits:
    Q = 1 / (2 * sinh(ln(2)/2 * BW * w0/sin(w0))).
    Physically, the same nominal octave bandwidth needs a lower Q near
    Nyquist than the unwarped formula gives — bounded below (never zero or
    negative) since the sinh argument, though large near Nyquist, is always
    finite for any Fc strictly below Nyquist; `math.sinh` overflows double
    precision only if Fc is clamped to (or numerically indistinguishable
    from) exact Nyquist, so that case floors to a near-zero Q rather than
    raising."""
    bw_oct = max(bw_oct, 0.001)
    arg = math.log(2.0) / 2.0 * bw_oct * _warp_factor(fc, sample_rate)
    if arg > 700.0:
        return 0.001
    return 1.0 / (2.0 * math.sinh(arg))


def frequency_array(num_points: int = GRAPH_POINTS, fmin: float = GRAPH_FMIN,
                     fmax: float = GRAPH_FMAX) -> np.ndarray:
    """Log-spaced frequency array for plotting / response calculation."""
    return np.logspace(np.log10(fmin), np.log10(fmax), num_points)


def _biquad_db(freqs: np.ndarray, b0: float, b1: float, b2: float,
               a0: float, a1: float, a2: float, sample_rate: int) -> np.ndarray:
    w = 2 * np.pi * freqs / sample_rate
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    H = (b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2)
    return 20 * np.log10(np.maximum(np.abs(H), 1e-12))


def _coeffs_peaking(fc, gain_db, Q, sr):
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    b0 = 1 + alpha * A
    b1 = -2 * cosw0
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cosw0
    a2 = 1 - alpha / A
    return b0, b1, b2, a0, a1, a2


def _coeffs_lowpass(fc, Q, sr):
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    b0 = (1 - cosw0) / 2
    b1 = 1 - cosw0
    b2 = (1 - cosw0) / 2
    a0 = 1 + alpha
    a1 = -2 * cosw0
    a2 = 1 - alpha
    return b0, b1, b2, a0, a1, a2


def _coeffs_highpass(fc, Q, sr):
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    b0 = (1 + cosw0) / 2
    b1 = -(1 + cosw0)
    b2 = (1 + cosw0) / 2
    a0 = 1 + alpha
    a1 = -2 * cosw0
    a2 = 1 - alpha
    return b0, b1, b2, a0, a1, a2


def _coeffs_bandpass(fc, Q, sr):
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    b0 = alpha
    b1 = 0.0
    b2 = -alpha
    a0 = 1 + alpha
    a1 = -2 * cosw0
    a2 = 1 - alpha
    return b0, b1, b2, a0, a1, a2


def _coeffs_notch(fc, Q, sr):
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    b0 = 1.0
    b1 = -2 * cosw0
    b2 = 1.0
    a0 = 1 + alpha
    a1 = -2 * cosw0
    a2 = 1 - alpha
    return b0, b1, b2, a0, a1, a2


def _coeffs_allpass(fc, Q, sr):
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    b0 = 1 - alpha
    b1 = -2 * cosw0
    b2 = 1 + alpha
    a0 = 1 + alpha
    a1 = -2 * cosw0
    a2 = 1 - alpha
    return b0, b1, b2, a0, a1, a2


def _coeffs_lowshelf(fc, gain_db, Q, sr):
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    sqrtA = np.sqrt(A)
    b0 = A * ((A + 1) - (A - 1) * cosw0 + 2 * sqrtA * alpha)
    b1 = 2 * A * ((A - 1) - (A + 1) * cosw0)
    b2 = A * ((A + 1) - (A - 1) * cosw0 - 2 * sqrtA * alpha)
    a0 = (A + 1) + (A - 1) * cosw0 + 2 * sqrtA * alpha
    a1 = -2 * ((A - 1) + (A + 1) * cosw0)
    a2 = (A + 1) + (A - 1) * cosw0 - 2 * sqrtA * alpha
    return b0, b1, b2, a0, a1, a2


def _coeffs_highshelf(fc, gain_db, Q, sr):
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    sqrtA = np.sqrt(A)
    b0 = A * ((A + 1) + (A - 1) * cosw0 + 2 * sqrtA * alpha)
    b1 = -2 * A * ((A - 1) + (A + 1) * cosw0)
    b2 = A * ((A + 1) + (A - 1) * cosw0 - 2 * sqrtA * alpha)
    a0 = (A + 1) - (A - 1) * cosw0 + 2 * sqrtA * alpha
    a1 = 2 * ((A - 1) - (A + 1) * cosw0)
    a2 = (A + 1) - (A - 1) * cosw0 - 2 * sqrtA * alpha
    return b0, b1, b2, a0, a1, a2


def _iir_response(freqs: np.ndarray, order: int, coefficients: str, sample_rate: int) -> np.ndarray:
    try:
        values = [float(x) for x in coefficients.split()]
        n = order + 1
        b = values[:n]
        a = values[n:2 * n]
        if len(b) != n or len(a) != n:
            raise ValueError("coefficient count mismatch")
    except (ValueError, IndexError):
        return np.zeros_like(freqs)

    w = 2 * np.pi * freqs / sample_rate
    z_inv = np.exp(-1j * w)
    num = sum(bk * z_inv ** k for k, bk in enumerate(b))
    den = sum(ak * z_inv ** k for k, ak in enumerate(a))
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)
    H = num / den
    return 20 * np.log10(np.maximum(np.abs(H), 1e-12))


def filter_response(filt: Filter, freqs: np.ndarray,
                    sample_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Return the dB response array for a single filter band across `freqs`."""
    if not filt.enabled:
        return np.zeros_like(freqs)

    fc = max(filt.fc, 1.0)
    if filt.use_bw and filt.supports_bw():
        q = bw_to_q(filt.bw_oct)
    else:
        q = max(filt.q, 0.05)
    ftype = filt.type

    if ftype == "IIR":
        return _iir_response(freqs, filt.iir_order, filt.iir_coefficients, sample_rate)

    if ftype in _FIXED_SLOPE_Q:
        q = _FIXED_SLOPE_Q[ftype]

    if ftype in ("PK", "PEQ", "Modal"):
        coeffs = _coeffs_peaking(fc, filt.gain, q, sample_rate)
    elif ftype in ("LP", "LPQ"):
        coeffs = _coeffs_lowpass(fc, q, sample_rate)
    elif ftype in ("HP", "HPQ"):
        coeffs = _coeffs_highpass(fc, q, sample_rate)
    elif ftype == "BP":
        coeffs = _coeffs_bandpass(fc, q, sample_rate)
    elif ftype == "NO":
        coeffs = _coeffs_notch(fc, q, sample_rate)
    elif ftype == "AP":
        coeffs = _coeffs_allpass(fc, q, sample_rate)
    elif ftype in ("LS", "LSC", "LS 6dB", "LS 12dB"):
        coeffs = _coeffs_lowshelf(fc, filt.gain, q, sample_rate)
    elif ftype in ("HS", "HSC", "HS 6dB", "HS 12dB"):
        coeffs = _coeffs_highshelf(fc, filt.gain, q, sample_rate)
    else:
        return np.zeros_like(freqs)

    return _biquad_db(freqs, *coeffs, sample_rate=sample_rate)


def combined_response(filters: list[Filter], freqs: np.ndarray,
                       sample_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Sum (in dB) the responses of all enabled filters — the total EQ curve."""
    total = np.zeros_like(freqs)
    for filt in filters:
        total = total + filter_response(filt, freqs, sample_rate)
    return total


def parse_graphic_eq(geq_string: str) -> list[tuple[float, float]]:
    """Parse a `GraphicEQ:` value string ("freq gain; freq gain; ...") into
    (freq, gain) points sorted by frequency. Malformed chunks are skipped."""
    points: list[tuple[float, float]] = []
    for chunk in geq_string.split(";"):
        parts = chunk.strip().split()
        if len(parts) >= 2:
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return sorted(points, key=lambda p: p[0])


def graphic_eq_response(geq_string: str, freqs: np.ndarray) -> np.ndarray:
    """dB response of a GraphicEQ line: gains interpolate linearly between
    points on a log-frequency axis and hold the outermost gains beyond them."""
    points = parse_graphic_eq(geq_string) if geq_string else []
    if not points:
        return np.zeros_like(freqs)
    if len(points) == 1:
        return np.full_like(freqs, points[0][1])
    log_freqs = np.log10(np.clip([p[0] for p in points], 1.0, None))
    gains = np.array([p[1] for p in points], dtype=float)
    return np.interp(np.log10(freqs), log_freqs, gains)
