"""Linkwitz-Riley crossover-design presets (Rumble 4.10).

A Linkwitz-Riley crossover of a given order is built by cascading Butterworth
2nd-order sections: LR4 (24 dB/oct) is two cascaded copies of a single
Butterworth-2 section (Q = 1/sqrt(2)), and LR8 (48 dB/oct) is two cascaded
copies of a Butterworth-4 filter, which itself needs two sections at
different Q values — the standard analog-prototype Butterworth pole-angle
formula (e.g. Williams & Taylor, "Electronic Filter Design Handbook"):
theta_k = (2k-1)*pi/(2*N), Q_k = 1/(2*cos(theta_k)) for k=1..N/2. Squaring an
order-N Butterworth (cascading it with itself) is exactly how a
Linkwitz-Riley filter is constructed (Linkwitz, "Active Crossover Networks
for Noncoincident Drivers", JAES 1976) — each Butterworth section therefore
appears twice in the final stage list.

Equalizer APO's native `LP`/`HP` filter type is itself a single 2nd-order
(12 dB/oct) biquad (see eq_math.py's `_coeffs_lowpass`/`_coeffs_highpass`),
so each cascaded section here is one `Filter` object and the whole crossover
is simply a list of them appended to a channel's filter list — no new DSP
core is needed, only the preset assembly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from rumble.core.apo_config import DEFAULT_SAMPLE_RATE, Filter

# 2nd-order Butterworth Q (order N=2: single pole pair at theta=pi/4,
# Q = 1/(2*cos(pi/4)) = 1/sqrt(2)) -- the standard "maximally flat" alignment.
BUTTERWORTH_Q = 1.0 / math.sqrt(2.0)

# Crossover slope (dB/octave) -> underlying Butterworth order that gets
# squared to build the Linkwitz-Riley filter (LR slope = 2 * 6 * order).
SLOPE_TO_BUTTERWORTH_ORDER: dict[int, int] = {24: 2, 48: 4}


def _butterworth_section_qs(order: int) -> list[float]:
    """Q values for the order/2 cascaded 2nd-order sections of an even-order
    Butterworth filter, from its pole angles."""
    if order < 2 or order % 2 != 0:
        raise ValueError(f"only even Butterworth orders >= 2 are supported, got {order}")
    return [1.0 / (2.0 * math.cos((2 * k - 1) * math.pi / (2 * order)))
            for k in range(1, order // 2 + 1)]


def stage_qs_for_slope(slope_db_per_oct: int) -> list[float]:
    """Q values for every cascaded section needed to realize a Linkwitz-Riley
    crossover at `slope_db_per_oct` -- each underlying Butterworth section
    appears twice, since an LR filter is a squared Butterworth filter."""
    order = SLOPE_TO_BUTTERWORTH_ORDER.get(slope_db_per_oct)
    if order is None:
        raise ValueError(
            f"unsupported crossover slope {slope_db_per_oct} dB/oct -- "
            f"supported: {sorted(SLOPE_TO_BUTTERWORTH_ORDER)}")
    qs = _butterworth_section_qs(order)
    return qs + qs


@dataclass
class CrossoverDesign:
    """A matched Linkwitz-Riley high-pass/low-pass filter pair at one
    crossover frequency, directly usable as `ChannelSection.filters`
    additions -- `highpass_filters` for the driver covering the top of the
    band (e.g. main speakers), `lowpass_filters` for the driver covering the
    bottom (e.g. a subwoofer)."""
    fc: float
    slope_db_per_oct: int
    highpass_filters: list[Filter] = field(default_factory=list)
    lowpass_filters: list[Filter] = field(default_factory=list)
    allpass_filters: list[Filter] = field(default_factory=list)


def design_crossover(fc: float, slope_db_per_oct: int, *,
                      sample_rate: int = DEFAULT_SAMPLE_RATE,
                      allpass_alignment: bool = False) -> CrossoverDesign:
    """Build a Linkwitz-Riley crossover at `fc` for the given slope.

    `sample_rate` is accepted for API symmetry with the rest of this
    codebase's filter-construction call sites but does not affect the
    generated `Filter` objects themselves (Fc/Q are sample-rate-independent;
    the sample rate only matters when a response is actually evaluated, e.g.
    via `eq_math.combined_response`).

    When `allpass_alignment` is set, `allpass_filters` gets a cascade of `AP`
    filters at the same Fc and the same per-stage Q list as the crossover
    itself -- for correcting the group-delay differential introduced when
    only one branch (e.g. a powered subwoofer's own built-in low-pass) can't
    be directly edited here, by matching its phase response on the other
    branch. Empty by default (no phase issue to correct in the common case
    where both HP and LP come from this same tool).
    """
    qs = stage_qs_for_slope(slope_db_per_oct)
    highpass = [Filter(type="HP", fc=fc, q=q) for q in qs]
    lowpass = [Filter(type="LP", fc=fc, q=q) for q in qs]
    allpass = [Filter(type="AP", fc=fc, q=q) for q in qs] if allpass_alignment else []
    return CrossoverDesign(fc=fc, slope_db_per_oct=slope_db_per_oct,
                            highpass_filters=highpass, lowpass_filters=lowpass,
                            allpass_filters=allpass)
