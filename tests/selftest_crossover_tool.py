import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import math

import numpy as np

from rumble.core import crossover, eq_math
from rumble.core.apo_config import DEFAULT_SAMPLE_RATE

SR = DEFAULT_SAMPLE_RATE
BUTTERWORTH_Q = 1.0 / math.sqrt(2.0)


def _complex_response(filters, freqs, sample_rate):
    """Cascade filters' true complex biquad response (not just dB magnitude)
    so branch-sum flatness can be checked in the complex domain, the same way
    combined_response() checks it in the (single-branch) dB domain."""
    total = np.ones_like(freqs, dtype=complex)
    for filt in filters:
        w0 = 2 * np.pi * filt.fc / sample_rate
        alpha = np.sin(w0) / (2 * filt.q)
        cosw0 = np.cos(w0)
        if filt.type == "LP":
            b0, b1, b2 = (1 - cosw0) / 2, 1 - cosw0, (1 - cosw0) / 2
        elif filt.type == "HP":
            b0, b1, b2 = (1 + cosw0) / 2, -(1 + cosw0), (1 + cosw0) / 2
        else:
            raise ValueError(f"unsupported type for this helper: {filt.type}")
        a0, a1, a2 = 1 + alpha, -2 * cosw0, 1 - alpha
        w = 2 * np.pi * freqs / sample_rate
        z1 = np.exp(-1j * w)
        z2 = z1 * z1
        total = total * (b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2)
    return total


def test_design_crossover_24db_uses_two_butterworth_q_stages():
    design = crossover.design_crossover(1000.0, 24, sample_rate=SR)
    assert len(design.highpass_filters) == 2
    assert len(design.lowpass_filters) == 2
    for filt in design.highpass_filters + design.lowpass_filters:
        assert abs(filt.q - BUTTERWORTH_Q) < 1e-9
        assert filt.fc == 1000.0


def test_design_crossover_48db_uses_four_stage_dual_q_cascade():
    design = crossover.design_crossover(1000.0, 48, sample_rate=SR)
    assert len(design.highpass_filters) == 4
    assert len(design.lowpass_filters) == 4
    qs = sorted(f.q for f in design.lowpass_filters)
    # Two conjugate-pole-pair Q values of a 4th-order Butterworth, each used
    # twice (LR8 = two cascaded 4th-order-Butterworth stages).
    assert abs(qs[0] - 0.5411961) < 1e-6
    assert abs(qs[1] - 0.5411961) < 1e-6
    assert abs(qs[2] - 1.3065630) < 1e-6
    assert abs(qs[3] - 1.3065630) < 1e-6


def test_design_crossover_rejects_unsupported_slope():
    raised = None
    try:
        crossover.design_crossover(1000.0, 12, sample_rate=SR)
    except ValueError as exc:
        raised = exc
    assert raised is not None


def test_crossover_branches_each_down_6db_at_fc():
    for slope in (24, 48):
        design = crossover.design_crossover(1000.0, slope, sample_rate=SR)
        freqs = np.array([1000.0])
        lp_db = eq_math.combined_response(design.lowpass_filters, freqs, SR)[0]
        hp_db = eq_math.combined_response(design.highpass_filters, freqs, SR)[0]
        assert abs(lp_db - (-6.0206)) < 0.05
        assert abs(hp_db - (-6.0206)) < 0.05


def test_crossover_branches_sum_flat_across_transition_band():
    """The defining Linkwitz-Riley property: complementary HP+LP outputs
    summed in phase reconstruct a flat (0 dB) response, unlike a plain
    Butterworth crossover which bumps +3dB at the crossover point."""
    for slope in (24, 48):
        design = crossover.design_crossover(1000.0, slope, sample_rate=SR)
        freqs = np.logspace(np.log10(50.0), np.log10(SR / 2 * 0.9), 300)
        hp_total = _complex_response(design.highpass_filters, freqs, SR)
        lp_total = _complex_response(design.lowpass_filters, freqs, SR)
        summed_db = 20 * np.log10(np.abs(hp_total + lp_total))
        assert np.max(np.abs(summed_db)) < 0.1


def test_allpass_alignment_off_by_default():
    design = crossover.design_crossover(1000.0, 24, sample_rate=SR)
    assert design.allpass_filters == []


def test_allpass_alignment_matches_cascade_stage_count():
    design = crossover.design_crossover(1000.0, 48, sample_rate=SR, allpass_alignment=True)
    assert len(design.allpass_filters) == 4
    for filt in design.allpass_filters:
        assert filt.type == "AP"
        assert filt.fc == 1000.0


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
