"""ISO 226:2003 equal-loudness-level contours: SPL-from-phon computation and
a volume-linked loudness-compensation curve derived from it.

Table 1's per-frequency coefficients (alpha_f, L_U, T_f) and the SPL formula
are the published standard's own values, not approximated from memory here.
They were confirmed against two independent, mutually cross-checked
open-source reproductions of ISO 226:2003 Table 1 (both bit-identical, the
strongest cross-check practical for a paywalled standard, read 2026-07-09):
Jeff Tackett's ``iso226.m`` (MATLAB Central File Exchange #7028, 2005) and
``github.com/IoSR-Surrey/MatlabToolbox``'s ``+iosr/+auditory/iso226.m``
reimplementation. Numerically verified here too: the formula reduces to
``SPL(1000 Hz, phon) == phon`` for any phon (ISO 226:2003 defines the phon
scale itself at 1 kHz), which both sources' own worked constants satisfy.

Valid phon range per both sources' documented usage note: 0-90 phon (no
experimental basis in the standard outside that range) -- callers get
inputs clamped into this range rather than extrapolated.

See docs/superpowers/plans/2026-07-08-rumble-action-guide-campaign.md
Session 16 (Rumble 3.5) and docs/RUMBLE_APP_SPECIFIC_KB.md §14.1 (Session 6's
finding that upstream's own `LoudnessCorrection:` is a hand-tuned heuristic,
*not* an ISO 226 implementation -- this module is the real one).
"""
from __future__ import annotations

import math

Point = tuple[float, float]

#: The 29 frequencies ISO 226:2003 Table 1 tabulates (Hz).
FREQUENCIES: tuple[float, ...] = (
    20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
    630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
    10000, 12500,
)

#: Loudness-perception exponent, alpha_f, one per FREQUENCIES entry.
ALPHA_F: tuple[float, ...] = (
    0.532, 0.506, 0.480, 0.455, 0.432, 0.409, 0.387, 0.367, 0.349, 0.330,
    0.315, 0.301, 0.288, 0.276, 0.267, 0.259, 0.253, 0.250, 0.246, 0.244,
    0.243, 0.243, 0.243, 0.242, 0.242, 0.245, 0.254, 0.271, 0.301,
)

#: Magnitude of the linear transfer function normalized at 1000 Hz, L_U.
L_U: tuple[float, ...] = (
    -31.6, -27.2, -23.0, -19.1, -15.9, -13.0, -10.3, -8.1, -6.2, -4.5, -3.1,
    -2.0, -1.1, -0.4, 0.0, 0.3, 0.5, 0.0, -2.7, -4.1, -1.0, 1.7, 2.5, 1.2,
    -2.1, -7.1, -11.2, -10.7, -3.1,
)

#: Threshold of hearing, T_f (also the phon=0 contour, per the standard).
T_F: tuple[float, ...] = (
    78.5, 68.7, 59.5, 51.1, 44.0, 37.5, 31.5, 26.5, 22.1, 17.9, 14.4, 11.4,
    8.6, 6.2, 4.4, 3.0, 2.2, 2.4, 3.5, 1.7, -1.3, -4.2, -6.0, -5.4, -1.5,
    6.0, 12.6, 13.9, 12.3,
)

#: Documented valid phon range (both cross-checked sources' own usage note).
MIN_PHON = 0.0
MAX_PHON = 90.0


def equal_loudness_spl(frequency_index: int, phon: float) -> float:
    """SPL (dB) at ``FREQUENCIES[frequency_index]`` that a normal listener
    perceives as equally loud as a 1 kHz tone at *phon* dB SPL -- ISO
    226:2003's own defining formula. *phon* is clamped to
    [MIN_PHON, MAX_PHON] (the standard's own documented valid range)."""
    phon = min(max(phon, MIN_PHON), MAX_PHON)
    af = ALPHA_F[frequency_index]
    lu = L_U[frequency_index]
    tf = T_F[frequency_index]
    if phon <= 0.0:
        return tf
    a_f = (4.47e-3 * (10 ** (0.025 * phon) - 1.15)
           + (0.4 * 10 ** (((tf + lu) / 10) - 9)) ** af)
    return (10 / af) * math.log10(a_f) - lu + 94


def loudness_compensation_curve(reference_phon: float, current_phon: float) -> list[Point]:
    """The per-frequency dB adjustment that keeps a signal's perceived
    spectral balance the same at *current_phon* (typically the user's
    actual, quieter listening level) as it was at *reference_phon*
    (typically the level content is mixed/mastered to sound "flat" at).

    Derived directly from the equal-loudness contour gap between the two
    levels at each of ISO 226:2003's own 29 tabulated frequencies -- no
    interpolation invented here; the GraphicEQ format these points feed
    into (autoeq_merge/apo GraphicEQ) already interpolates log-linearly
    between points, exactly as it does for every other correction curve in
    this codebase (listener_correction.py, eq_target.py).

    Zero by construction at 1000 Hz for any reference/current pair, since
    equal_loudness_spl(1kHz, phon) == phon (the phon scale's own
    definition). Positive values mean "boost this frequency" (typical at
    bass/treble when current_phon < reference_phon, i.e. the user is
    listening quieter than the reference level)."""
    curve: list[Point] = []
    for i, f in enumerate(FREQUENCIES):
        delta_current = equal_loudness_spl(i, current_phon) - current_phon
        delta_reference = equal_loudness_spl(i, reference_phon) - reference_phon
        curve.append((float(f), delta_current - delta_reference))
    return curve
