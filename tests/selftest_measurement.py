"""Selftest: C2a ESS sweep + deconvolution engine
(rumble/core/measurement.py).

Run:  python tests/selftest_measurement.py

Covers: generate_ess()'s shape/dtype and its self-normalization invariant
(sweep convolved with its own inverse filter peaks at exactly 1.0); the
decisive round-trip test -- a known eq_math filter response applied to the
sweep in the frequency domain, deconvolved, and compared against eq_math's
own curve within 0.5 dB across 40 Hz-15 kHz; a flat (unfiltered) loopback
recovering ~0 dB across the same band; time_gate() removing an artificial
late echo while leaving the direct-sound peak untouched (S11); and (S12)
measure_rta()'s power-domain block averaging converging on a synthetic
spectrum, average_positions() matching autoeq_merge.average_curves()
directly, smooth_measurement() matching eq_target.octave_smooth_adaptive()
directly, and agc_check() passing a linear fake / flagging a simulated
2:1 compressor fake.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import autoeq_merge, eq_math, eq_target, measurement
from rumble.core.apo_config import Filter
from rumble.core.audio import metering

_SR = 48000
_DURATION_S = 2.0
_BAND_LO_HZ = 40.0
_BAND_HI_HZ = 15000.0
_TOL_DB = 0.5

# The two accuracy tests sweep wider than the compared band (standard ESS
# measurement practice -- always excite beyond the region of interest) so the
# comparison band stays clear of the sweep's own edge-of-passband
# deconvolution artifacts; f2 stops just short of Nyquist (24000 Hz exactly)
# to avoid the biquad bilinear-transform warp eq_math itself documents
# growing unbounded there (see eq_math._warp_factor).
_SWEEP_F1_HZ = 10.0
_SWEEP_F2_HZ = 23500.0


def _peaking_complex_response(fc, gain_db, q, freqs, sample_rate):
    """Complex (magnitude + phase) response of an RBJ peaking filter --
    same coefficients as eq_math._coeffs_peaking (private there, since
    eq_math only ever needs magnitude-in-dB for the graph), kept complex
    here because a *real, causal* filter's phase matters for this test:
    applying only a target magnitude in the frequency domain (dropping
    phase) builds a zero-phase, non-causal, time-symmetric synthetic
    "recorded" signal that does not behave like the causal system ESS
    deconvolution is designed to measure -- verified by comparison during
    this session (see the progress file's S11 Decisions entry).
    """
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * fc / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cosw0 = np.cos(w0)
    b0 = 1 + alpha * A
    b1 = -2 * cosw0
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cosw0
    a2 = 1 - alpha / A

    w = 2 * np.pi * freqs / sample_rate
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    return (b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2)


def test_generate_ess_shapes_and_self_normalization():
    sweep, inverse = measurement.generate_ess(_DURATION_S, _SR)
    assert sweep.dtype == np.float32
    assert inverse.dtype == np.float32
    assert len(sweep) == len(inverse) == int(round(_DURATION_S * _SR))

    conv = np.convolve(sweep.astype(np.float64), inverse.astype(np.float64))
    peak = np.max(np.abs(conv))
    assert abs(peak - 1.0) < 1e-6, f"self-normalization peak was {peak}"


def test_generate_ess_rejects_invalid_range():
    try:
        measurement.generate_ess(1.0, _SR, f1=100.0, f2=100.0)
        assert False, "expected ValueError for f2 <= f1"
    except ValueError:
        pass


def _band_points(points, lo=_BAND_LO_HZ, hi=_BAND_HI_HZ):
    return [(f, d) for f, d in points if lo <= f <= hi]


def test_flat_loopback_recovers_near_zero_db():
    sweep, inverse = measurement.generate_ess(_DURATION_S, _SR, f1=_SWEEP_F1_HZ, f2=_SWEEP_F2_HZ)
    ir = measurement.deconvolve(sweep, inverse, _SR)
    points = measurement.ir_to_magnitude(ir, _SR, n_points=200)
    band = _band_points(points)
    assert len(band) > 50
    max_err = max(abs(d) for _, d in band)
    assert max_err < _TOL_DB, f"flat loopback max deviation {max_err} dB"


def test_known_filter_response_matches_eq_math():
    sweep, inverse = measurement.generate_ess(_DURATION_S, _SR, f1=_SWEEP_F1_HZ, f2=_SWEEP_F2_HZ)
    n = len(sweep)

    filt = Filter(type="PK", fc=1000.0, gain=6.0, q=1.0)

    bin_freqs = np.fft.rfftfreq(n, d=1.0 / _SR)
    h_complex = _peaking_complex_response(filt.fc, filt.gain, filt.q, bin_freqs, _SR)
    sweep_spectrum = np.fft.rfft(sweep.astype(np.float64))
    recorded = np.fft.irfft(sweep_spectrum * h_complex, n=n)
    recorded = recorded.astype(np.float32)

    ir = measurement.deconvolve(recorded, inverse, _SR)
    points = measurement.ir_to_magnitude(ir, _SR, n_points=200)
    band = _band_points(points)
    assert len(band) > 50

    check_freqs = np.array([f for f, _ in band])
    recovered_db = np.array([d for _, d in band])
    expected_db = eq_math.filter_response(filt, check_freqs, _SR)

    max_err = np.max(np.abs(recovered_db - expected_db))
    assert max_err < _TOL_DB, f"recovered curve max deviation {max_err} dB"


def test_time_gate_removes_late_echo_keeps_direct_peak():
    n = int(round(0.1 * _SR))  # 100 ms buffer
    ir = np.zeros(n, dtype=np.float32)
    peak_idx = 100
    echo_idx = peak_idx + int(round(0.010 * _SR))  # 10 ms after the peak
    ir[peak_idx] = 1.0
    ir[echo_idx] = 0.5

    gated = measurement.time_gate(ir, gate_ms=5.0, sample_rate=_SR, taper_ms=1.0)

    assert len(gated) == len(ir)
    assert gated[echo_idx] == 0.0
    assert gated[peak_idx] != 0.0
    assert abs(float(gated[peak_idx]) - 1.0) < 1e-6


def test_time_gate_zero_before_peak():
    n = 1000
    ir = np.zeros(n, dtype=np.float32)
    ir[50] = 1.0
    gated = measurement.time_gate(ir, gate_ms=1.0, sample_rate=_SR)
    assert np.all(gated[:50] == 0.0)


def test_detect_first_reflection_gate_ms_finds_a_known_secondary_peak():
    n = int(round(0.1 * _SR))  # 100 ms buffer
    ir = np.zeros(n, dtype=np.float32)
    peak_idx = 100
    reflection_ms = 4.0
    reflection_idx = peak_idx + int(round(reflection_ms / 1000.0 * _SR))
    ir[peak_idx] = 1.0
    ir[reflection_idx] = 0.4  # well above the 0.15 detection ratio

    gate_ms = measurement.detect_first_reflection_gate_ms(ir, _SR)
    assert abs(gate_ms - reflection_ms) < 0.05, gate_ms


def test_detect_first_reflection_gate_ms_ignores_reflection_below_ratio():
    n = int(round(0.1 * _SR))
    ir = np.zeros(n, dtype=np.float32)
    peak_idx = 100
    ir[peak_idx] = 1.0
    ir[peak_idx + int(round(2.0 / 1000.0 * _SR))] = 0.05  # below the 0.15 ratio -- noise, not a reflection
    reflection_ms = 6.0
    reflection_idx = peak_idx + int(round(reflection_ms / 1000.0 * _SR))
    ir[reflection_idx] = 0.3

    gate_ms = measurement.detect_first_reflection_gate_ms(ir, _SR)
    assert abs(gate_ms - reflection_ms) < 0.05, gate_ms


def test_detect_first_reflection_gate_ms_falls_back_when_no_reflection_found():
    n = int(round(0.1 * _SR))
    ir = np.zeros(n, dtype=np.float32)
    ir[100] = 1.0  # a perfectly clean, reflection-free capture
    gate_ms = measurement.detect_first_reflection_gate_ms(ir, _SR)
    assert gate_ms == 15.0  # measurement._REFLECTION_SEARCH_MAX_MS


def test_ir_to_magnitude_point_count_and_grid():
    sweep, inverse = measurement.generate_ess(_DURATION_S, _SR)
    ir = measurement.deconvolve(sweep, inverse, _SR)
    points = measurement.ir_to_magnitude(ir, _SR, n_points=64)
    assert len(points) == 64
    freqs = [f for f, _ in points]
    assert freqs == sorted(freqs)
    assert freqs[0] >= eq_math.GRAPH_FMIN
    assert freqs[-1] <= _SR / 2.0 + 1e-6  # np.logspace rounds the exact endpoint by a few ULPs


def test_measure_rta_converges_on_synthetic_spectrum():
    """Power-domain averaging over many independent white-noise blocks
    should shrink the dB-domain spread across a flat band far more than a
    single block, while leaving the band's mean level roughly unchanged
    (unbiased -- averaging reduces variance, not the estimate itself)."""
    sr = 48000
    n_fft = 2048
    band_lo, band_hi = 500.0, 10000.0

    def white_block(seed):
        rng = np.random.default_rng(seed)
        return rng.standard_normal(n_fft)

    def band_values(points):
        return [d for f, d in points if band_lo <= f <= band_hi]

    single = measurement.measure_rta([white_block(0)], sr, n_fft=n_fft, n_points=200)
    many = measurement.measure_rta([white_block(s) for s in range(50)], sr, n_fft=n_fft, n_points=200)

    std_single = float(np.std(band_values(single)))
    std_many = float(np.std(band_values(many)))
    assert std_many < std_single * 0.5, f"std_single={std_single} std_many={std_many}"

    # A single-block estimate's own band MEAN is itself a noisy sample (its
    # per-point std is ~std_single, and neighboring log-grid points are
    # correlated -- interpolated from overlapping nearby raw FFT bins --
    # so the mean doesn't benefit from the full sqrt(N) independent-sample
    # reduction); this only checks averaging doesn't introduce a large bias,
    # not that the two means are as close as independent-sample statistics
    # would suggest.
    mean_single = float(np.mean(band_values(single)))
    mean_many = float(np.mean(band_values(many)))
    assert abs(mean_single - mean_many) < 4.0, f"mean_single={mean_single} mean_many={mean_many}"


def test_measure_rta_empty_blocks():
    assert measurement.measure_rta([], 48000) == []


def test_average_positions_matches_average_curves_directly():
    curves = [
        [(100.0, 1.0), (1000.0, 2.0), (10000.0, -1.0)],
        [(100.0, -1.0), (1000.0, 0.0), (10000.0, 3.0)],
        [(100.0, 0.5), (1000.0, 1.0), (10000.0, 1.0)],
    ]
    assert measurement.average_positions(curves) == autoeq_merge.average_curves(curves)


def test_smooth_measurement_matches_octave_smooth_adaptive_directly():
    curve = [(f, 5.0 if 900.0 <= f <= 1100.0 else 0.0) for f in eq_target.log_grid(20.0, 20000.0, 12)]
    assert measurement.smooth_measurement(curve) == eq_target.octave_smooth_adaptive(curve)


def test_agc_check_passes_linear_fake():
    def linear_record(signal, sample_rate):
        return signal.copy()

    result = measurement.agc_check(linear_record, duration_s=0.5, sample_rate=48000)
    assert not result.processing_detected
    assert abs(result.delta_db - measurement.AGC_STEP_DB) < 0.1, result.delta_db


def test_agc_check_flags_simulated_compressor():
    threshold_db = -40.0

    def compressed_record(signal, sample_rate):
        level_db = metering.rms_dbfs(signal)
        if level_db > threshold_db:
            compressed_db = threshold_db + (level_db - threshold_db) / 2.0  # 2:1 ratio
        else:
            compressed_db = level_db
        gain_db = compressed_db - level_db
        return signal * (10.0 ** (gain_db / 20.0))

    result = measurement.agc_check(compressed_record, duration_s=0.5, sample_rate=48000)
    assert result.processing_detected
    assert result.delta_db < measurement.AGC_FLAG_THRESHOLD_DB, result.delta_db


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
