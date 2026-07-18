"""Auto-preamp calculation — derives a safe negative Preamp value from a filter set.

The combined frequency response of all active filters may exceed 0 dB at some
frequencies (boosted bands). Without compensation this causes clipping.
RUMBLE computes the peak of the combined response and proposes a preamp equal
to (at minimum) the negative of that peak, so the overall response never
exceeds 0 dB.
"""
from __future__ import annotations

import math

import numpy as np

from rumble.core import eq_math
from rumble.core.apo_config import Filter

# Anti-clipping safety margins, per RUMBLE_Headroom_Maths.md ("Step 4").
_MARGIN_DEFAULT_DB = 0.5    # sample-peak guard for an ordinary boost
_MARGIN_HIGH_DB = 1.0       # extra guard against inter-sample ("true") peaks
_HIGH_BOOST_DB = 6.0        # combined boost above this escalates the margin

# The most negative Preamp value the app will ever produce or accept. Matches
# the editor's manual-entry floor (editor_panel.py's preamp_spin range) —
# defined here, not there, so calculate_auto_preamp() can enforce it itself
# instead of relying on the UI widget's setValue() to clamp it silently after
# the fact (which used to lose the difference between "computed -34.2 dB" and
# "stored -30.0 dB" with no warning). No profile has ever needed more than
# this in practice; a filter set that does still shows correctly as a
# clipping-risk headroom warning via calculate_headroom(), since the clamped
# preamp no longer covers the true peak.
PREAMP_MIN_DB = -30.0

# "Several simultaneous sizeable boosts" — the worst-case peak gain of a
# resonant filter (its impulse response's L1 norm) can exceed the steady-state
# combined-response peak used above, and empirically (see
# docs/RUMBLE_Headroom_Maths.md Step 4 addendum) that gap is driven by the
# COUNT of simultaneous sizeable boosts, not by Q — a single boost's gap is
# ~flat across Q=1-8 at a given gain, and a *wide* Q=0.3 boost measured a
# *bigger* gap than any narrow one tested. So this counts gain only.
_SIZEABLE_BOOST_GAIN_DB = 4.0  # a band counts as sizeable at/above this gain
_SIZEABLE_BOOST_COUNT = 3      # this many sizeable boosts is "several"


def _is_sizeable_boost(filt: Filter) -> bool:
    return (filt.enabled and filt.supports_gain()
            and filt.gain >= _SIZEABLE_BOOST_GAIN_DB)


def _margin_db(peak_db: float, filters: list[Filter]) -> float:
    """The safety margin to subtract below the combined peak (spec Step 4):
    0.5 dB normally, 1.0 dB when the combined boost is high (>~6 dB) or there
    are several simultaneous sizeable boosts — both raise the risk of
    inter-sample peaks."""
    if peak_db > _HIGH_BOOST_DB:
        return _MARGIN_HIGH_DB
    if sum(1 for f in filters if _is_sizeable_boost(f)) >= _SIZEABLE_BOOST_COUNT:
        return _MARGIN_HIGH_DB
    return _MARGIN_DEFAULT_DB


def _peak_response_db(filters: list[Filter], geq_string: str, sample_rate: int,
                      section_offset_db: float = 0.0,
                      ir_response: np.ndarray | None = None) -> float:
    """Peak (dB) of the completed response: parametric filters, GraphicEQ,
    an optional pre-sampled impulse-response curve (dB on the
    ``eq_math.frequency_array()`` grid — keeps this module free of file IO)
    and an optional flat per-channel trim offset."""
    freqs = eq_math.frequency_array()
    response = eq_math.combined_response(filters, freqs, sample_rate)
    if geq_string:
        response = response + eq_math.graphic_eq_response(geq_string, freqs)
    if ir_response is not None:
        response = response + ir_response
    return float(np.max(response)) + section_offset_db


def calculate_auto_preamp(filters: list[Filter], sample_rate: int = eq_math.DEFAULT_SAMPLE_RATE,
                          extra_headroom_db: float | None = None, geq_string: str = "",
                          section_offset_db: float = 0.0,
                          ir_response: np.ndarray | None = None) -> float:
    """Return the safe anti-clipping preamp value (dB), per RUMBLE_Headroom_Maths.md.

    Computes the peak of the combined response (all enabled filters + GraphicEQ
    + optional pre-sampled convolution IR curve + optional per-channel trim
    ``section_offset_db``) and returns ``-(peak + margin)`` so the loudest
    point sits back at/below 0 dBFS. Returns 0.0 when nothing boosts above
    0 dB (a positive preamp is never used).

    The margin follows the spec automatically (0.5 dB, escalating to 1.0 dB on
    high or many-steep boosts); pass ``extra_headroom_db`` to override it.
    """
    if (not filters and not geq_string and ir_response is None
            and section_offset_db <= 0.0):
        return 0.0

    peak_db = _peak_response_db(filters, geq_string, sample_rate,
                                section_offset_db, ir_response)

    # Epsilon guard: pure cuts leave float noise (~1e-15 dB) at the response
    # extremes, which must not trigger a -0.5 dB preamp.
    if peak_db <= 1e-6:
        return 0.0

    margin = _margin_db(peak_db, filters) if extra_headroom_db is None else extra_headroom_db
    required = -(peak_db + margin)
    # Quantise to APO's 0.1 dB precision, flooring toward more attenuation so the
    # stored value is never less negative than required (never under-compensates).
    quantised = math.floor(required * 10) / 10
    # Clamp to PREAMP_MIN_DB rather than let a caller silently truncate it
    # further downstream — see that constant's comment for why.
    return max(quantised, PREAMP_MIN_DB)


def calculate_headroom(filters: list[Filter], preamp_db: float,
                        sample_rate: int = eq_math.DEFAULT_SAMPLE_RATE,
                        geq_string: str = "", section_offset_db: float = 0.0,
                        ir_response: np.ndarray | None = None) -> float:
    """Return the headroom (in dB) for the given preamp value.

    A positive value means the signal stays below 0 dB (safe, with that much
    margin). A negative value means clipping risk by that many dB. The peak
    includes the GraphicEQ, an optional pre-sampled convolution IR curve and
    the per-channel trim ``section_offset_db``.
    """
    if not filters and not geq_string and ir_response is None:
        return -(preamp_db + section_offset_db)

    peak_db = _peak_response_db(filters, geq_string, sample_rate,
                                section_offset_db, ir_response)
    return -(peak_db + preamp_db)
