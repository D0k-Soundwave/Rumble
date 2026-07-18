"""Speaker correction maths: turn a measured anechoic curve into an EQ correction.

Pure Python (stdlib + :mod:`rumble.core.autoeq_merge`), so it unit-tests in
isolation and reuses the existing curve helpers. A *curve* is a list of
``(frequency_hz, gain_db)`` points sorted by ascending frequency — the same
shape :mod:`rumble.core.autoeq_merge` consumes.

Unlike a headphone AutoEQ result (which already bakes in a target), a speaker's
Spinorama "Listening Window" is a raw normalised response. We turn it into an
APO-ready correction by subtracting it from a *target* curve, where the target
is a straight tilt in log-frequency::

    target(f) = slope_db_per_oct * log2(f / pivot_hz)

``slope == 0`` is flat — the honest "anechoic starting point" for users who will
then measure their room with REW. A mild downward slope (~ -0.9 dB/oct)
approximates a typical, generally-preferred in-room response for users who can't
or won't measure their room, so they still get a usable result.

The correction is octave-smoothed (so narrow measurement wiggles aren't chased),
boost-capped and band-limited with a soft taper (so the speaker's natural
low-frequency roll-off and any ultrasonic rise aren't boosted, wasting amplifier
headroom / driver excursion).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from rumble.core.autoeq_merge import Point, compute_preamp, interp_log

__all__ = [
    "octave_smooth", "octave_smooth_adaptive", "log_grid", "tilt_target",
    "make_correction", "clamp_gain", "compute_preamp", "suggested_highpass_hz",
    "ClampSummary", "directivity_boost_cap", "directivity_gap_suppression",
]

# AutoEQ's own frequency-dependent treble handling (autoeq/constants.py,
# https://github.com/jaakkopasanen/AutoEq, read 2026-07-09): fractional-
# octave smoothing widens from a narrow window below 6 kHz to a much wider
# one above 8 kHz (DEFAULT_SMOOTHING_WINDOW_SIZE = 1/12 oct,
# DEFAULT_TREBLE_SMOOTHING_WINDOW_SIZE = 2.0 oct,
# DEFAULT_TREBLE_SMOOTHING_F_LOWER/_UPPER = 6000/8000 Hz), and boost is
# tapered by a sigmoid gain multiplier over the same band
# (DEFAULT_TREBLE_F_LOWER/_UPPER, applied via `log_f_sigmoid` in
# frequency_response.py's `equalize()`). AutoEQ's rationale — measurement-rig
# and ear-canal/room variance dominates real device differences above ~6-8
# kHz, so tight smoothing and full boost up there chases rig noise, not the
# device — applies equally to RUMBLE's speaker corrections (Rumble 2.2/2.3),
# which face the same kind of noisy anechoic measurement data. RUMBLE reuses
# AutoEQ's exact breakpoint frequencies and window sizes rather than
# inventing its own; the transition below is linear-in-log-frequency rather
# than AutoEQ's sigmoid (same two endpoints, one fewer shape constant to
# justify).
ADAPTIVE_SMOOTH_WINDOW_OCT = 1 / 12
ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT = 2.0
ADAPTIVE_SMOOTH_F_LOWER = 6000.0
ADAPTIVE_SMOOTH_F_UPPER = 8000.0


@dataclass(frozen=True)
class ClampSummary:
    """How many points :func:`make_correction` had to cap (boost) or floor
    (cut), and by how much — so a silently-truncated correction can be
    surfaced to the user instead of just applied (Rumble 1.4).

    The all-zero default means "nothing was clamped", so any caller that
    doesn't check it stays correct without a code change.
    """

    points_clamped_boost: int = 0
    points_clamped_cut: int = 0
    max_clamped_delta_db: float = 0.0

    def notable(self, threshold_db: float = 0.5) -> bool:
        """Worth surfacing to the user — RUMBLE_ACTION_GUIDE.md #1.4's own
        "meaningful amount" bar: at least one point clamped, by more than
        *threshold_db* at its worst."""
        total = self.points_clamped_boost + self.points_clamped_cut
        return total > 0 and self.max_clamped_delta_db > threshold_db


def octave_smooth(curve: list[Point], frac_oct: float = 1 / 3) -> list[Point]:
    """Fractional-octave smoothing: average each point over ±frac_oct/2 octaves.

    Operates on the curve's own frequency points. ``frac_oct <= 0`` (or a curve
    too short to smooth) returns a copy unchanged.
    """
    if frac_oct <= 0 or len(curve) < 3:
        return list(curve)
    half = frac_oct / 2.0
    freqs = [f for f, _ in curve]
    gains = [g for _, g in curve]
    out: list[Point] = []
    for f, g0 in curve:
        lo, hi = f / (2 ** half), f * (2 ** half)
        i0 = bisect.bisect_left(freqs, lo)
        i1 = bisect.bisect_right(freqs, hi)
        seg = gains[i0:i1]
        out.append((f, sum(seg) / len(seg) if seg else g0))
    return out


def _log_lerp(f: float, f_lower: float, f_upper: float,
              lo_value: float, hi_value: float) -> float:
    """Linear interpolation between *lo_value* and *hi_value* over
    ``[f_lower, f_upper]`` on a log2(frequency) axis; clamped to *lo_value*/
    *hi_value* outside that range."""
    if f <= f_lower:
        return lo_value
    if f >= f_upper:
        return hi_value
    lo, hi = math.log2(f_lower), math.log2(f_upper)
    t = (math.log2(f) - lo) / (hi - lo)
    return lo_value + t * (hi_value - lo_value)


def octave_smooth_adaptive(curve: list[Point], *,
                           window_oct: float = ADAPTIVE_SMOOTH_WINDOW_OCT,
                           treble_window_oct: float = ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT,
                           f_lower: float = ADAPTIVE_SMOOTH_F_LOWER,
                           f_upper: float = ADAPTIVE_SMOOTH_F_UPPER
                           ) -> list[Point]:
    """Frequency-dependent version of :func:`octave_smooth`: a narrow window
    below *f_lower*, widening to *treble_window_oct* above *f_upper* (defaults
    are AutoEQ's own published smoothing constants — see the module-level
    comment above). For auto-generated corrections only — manual GEQ/PEQ
    editing keeps using the flat, unconstrained :func:`octave_smooth`.
    """
    if len(curve) < 3:
        return list(curve)
    freqs = [f for f, _ in curve]
    gains = [g for _, g in curve]
    out: list[Point] = []
    for f, g0 in curve:
        frac_oct = _log_lerp(f, f_lower, f_upper, window_oct, treble_window_oct)
        half = frac_oct / 2.0
        lo, hi = f / (2 ** half), f * (2 ** half)
        i0 = bisect.bisect_left(freqs, lo)
        i1 = bisect.bisect_right(freqs, hi)
        seg = gains[i0:i1]
        out.append((f, sum(seg) / len(seg) if seg else g0))
    return out


def _hf_guardrail_gain(f: float) -> float:
    """Multiplicative taper for auto-generated boost above AutoEQ's treble
    transition band (see the module-level comment) — 1.0 at/below
    :data:`ADAPTIVE_SMOOTH_F_LOWER`, linear-in-log-frequency down to 0.0
    at/above :data:`ADAPTIVE_SMOOTH_F_UPPER`. Meant to be applied to positive
    gain (boost) only — cut is left untouched, per RUMBLE_ACTION_GUIDE.md
    #2.3's "soft no-boost" framing rather than a general treble cut."""
    return _log_lerp(f, ADAPTIVE_SMOOTH_F_LOWER, ADAPTIVE_SMOOTH_F_UPPER, 1.0, 0.0)


def log_grid(f_lo: float = 20.0, f_hi: float = 20000.0,
             points_per_oct: int = 12) -> list[float]:
    """A log-spaced frequency grid (inclusive of both ends)."""
    n = max(1, int(round(math.log2(f_hi / f_lo) * points_per_oct)))
    return [f_lo * 2 ** (i / points_per_oct) for i in range(n + 1)]


def tilt_target(grid: list[float], slope_db_per_oct: float = 0.0,
                pivot_hz: float = 1000.0) -> list[Point]:
    """A straight tilt target: ``slope * log2(f/pivot)`` dB, zero at *pivot*."""
    return [(f, slope_db_per_oct * math.log2(f / pivot_hz)) for f in grid]


# Not derived from a named standard or another already-fixed project value
# (unlike most constants in this module) -- half an octave is a deliberately
# arbitrary soft-roll-off width: wide enough that the edge fade reads as
# gradual rather than a hard step, narrow enough that it doesn't eat into the
# passband next to the taper (band_low/band_high are already themselves a
# conservative estimate -- see suggested_highpass_hz() -- so a wider taper
# would suppress legitimate correction closer to the middle of the curve).
_EDGE_TAPER_OCT = 0.5


def _edge_gain(f: float, low: float, high: float, taper_oct: float = _EDGE_TAPER_OCT) -> float:
    """Soft band-limit window: 1.0 in [low, high], fading to 0 over *taper_oct*."""
    low = max(low, 1e-6)
    if low <= f <= high:
        return 1.0
    if f < low:
        lo2 = low / (2 ** taper_oct)
        if f <= lo2:
            return 0.0
        return (math.log2(f) - math.log2(lo2)) / taper_oct
    hi2 = high * (2 ** taper_oct)
    if f >= hi2:
        return 0.0
    return (math.log2(hi2) - math.log2(f)) / taper_oct


def make_correction(measured: list[Point], *, slope_db_per_oct: float = 0.0,
                    pivot_hz: float = 1000.0, smooth_oct: float = 1 / 3,
                    max_boost_db: float = 6.0, max_cut_db: float = 12.0,
                    band: tuple[float, float] = (20.0, 16000.0),
                    grid: list[float] | None = None,
                    adaptive_smoothing: bool = False,
                    target_curve: list[Point] | None = None,
                    directivity_secondary: list[Point] | None = None,
                    ) -> tuple[list[Point], ClampSummary]:
    """Build an EQ correction from a measured speaker curve toward a target.

    ``correction(f) = clamp(target(f) - smoothed_measured(f))`` evaluated on a
    log grid, boost-capped to ``max_boost_db``, cut-floored to ``max_cut_db`` and
    multiplied by a soft band-limit window (``band`` = usable frequency range;
    pass the speaker's -6 dB point as ``band[0]`` to avoid boosting its roll-off).
    Returns ``(curve, clamp_summary)`` — feed the curve through
    :func:`compute_preamp` and :func:`rumble.core.autoeq_merge.format_graphiceq`
    to store it in a profile; the summary reports how much the cap/floor
    actually altered the requested correction, so callers can surface it.

    ``target_curve`` (Rumble 3.4, e.g. from :mod:`rumble.core.target_curves`)
    replaces the straight ``slope_db_per_oct``/``pivot_hz`` tilt with a real,
    named target response, resampled onto ``grid`` via the same log-linear
    :func:`~rumble.core.autoeq_merge.interp_log` convention used everywhere
    else — pass one or the other, not conceptually both (``slope_db_per_oct``
    is ignored when ``target_curve`` is given).

    ``adaptive_smoothing`` (Rumble 2.2/2.3, off by default here — callers
    default it on for auto-generated corrections via
    :class:`~rumble.core.measurements.base.CorrectionOptions`) switches the
    smoothing step from the flat :func:`octave_smooth` to the frequency-
    dependent :func:`octave_smooth_adaptive`, and additionally tapers any
    boost above AutoEQ's treble band toward zero via :func:`_hf_guardrail_gain`
    before the existing boost cap — that taper is folded into the same
    ``clamp_summary`` bookkeeping as the boost cap/cut floor rather than a
    second notice, since both describe "the requested correction got reduced".

    ``directivity_secondary`` (Rumble 3.6, e.g. a speaker's Sound Power or
    Early Reflections curve alongside *measured* as its Listening Window)
    runs :func:`directivity_gap_suppression` and multiplies the final,
    already-capped gain at each frequency by the result — pulling correction
    toward zero where the two curves' gap deviates sharply from its own
    local trend, a signature of a directivity artifact a global EQ
    correction can't fix rather than a genuine on-axis response error. This
    is a distinct, complementary mechanism from the ``max_boost_db`` a
    caller may have already damped via :func:`directivity_boost_cap` (that
    one is a single per-speaker scalar from published angle estimates; this
    one is per-frequency, from the measured curves themselves) — both can be
    used together. Any reduction it causes is folded into the same
    ``clamp_summary`` bookkeeping as the boost cap/cut floor above.
    """
    if not measured:
        return [], ClampSummary()
    smoothed = (octave_smooth_adaptive(measured) if adaptive_smoothing
                else octave_smooth(measured, smooth_oct))
    if grid is None:
        grid = log_grid(20.0, 20000.0, 12)
    m = interp_log(smoothed, grid)
    target_values = interp_log(target_curve, grid) if target_curve else None
    suppression = (directivity_gap_suppression(measured, directivity_secondary, grid)
                  if directivity_secondary else None)
    sup_values = [v for _, v in suppression] if suppression else None
    low, high = band
    corr: list[Point] = []
    clamped_boost = 0
    clamped_cut = 0
    max_delta = 0.0
    for i, (f, mv) in enumerate(zip(grid, m)):
        target = target_values[i] if target_values is not None \
            else slope_db_per_oct * math.log2(f / pivot_hz)
        requested = target - mv
        g = requested
        if adaptive_smoothing and g > 0:
            g = g * _hf_guardrail_gain(f)
        boost_capped = min(g, max_boost_db)
        if boost_capped < requested:
            clamped_boost += 1
            max_delta = max(max_delta, requested - boost_capped)
        cut_floored = max(boost_capped, -max_cut_db)
        if cut_floored > boost_capped:
            clamped_cut += 1
            max_delta = max(max_delta, cut_floored - boost_capped)
        g = cut_floored
        if sup_values is not None:
            mult = sup_values[i]
            if mult < 1.0:
                suppressed_delta = abs(g) * (1.0 - mult)
                if suppressed_delta > 0:
                    if g >= 0:
                        clamped_boost += 1
                    else:
                        clamped_cut += 1
                    max_delta = max(max_delta, suppressed_delta)
                g = g * mult
        g = g * _edge_gain(f, low, high)
        corr.append((f, g))
    summary = ClampSummary(clamped_boost, clamped_cut, round(max_delta, 3))
    return corr, summary


def clamp_gain(curve: list[Point], *, max_boost_db: float = 6.0,
               max_cut_db: float = 12.0) -> tuple[list[Point], ClampSummary]:
    """Soft-clamp a bare gain curve to the same boost cap / cut floor
    :func:`make_correction` applies to speaker corrections, reporting what
    changed via :class:`ClampSummary`.

    Rumble 2.6: this is the "research-recommended soft range" side of the
    numeric-range decision (see ``docs/RUMBLE_APP_SPECIFIC_KB.md`` §7.1) for
    any auto-generated curve that *isn't* built by ``make_correction`` itself
    — currently that's :mod:`rumble.core.autoeq_merge`'s gear-simulation
    delta curve (``target - source``), which previously had no gain bound at
    all: two arbitrary measured curves can differ by far more than either
    curve's own range, e.g. an in-ear vs. an over-ear headphone target.
    """
    clamped_boost = clamped_cut = 0
    max_delta = 0.0
    out: list[Point] = []
    for f, g in curve:
        boost_capped = min(g, max_boost_db)
        if boost_capped < g:
            clamped_boost += 1
            max_delta = max(max_delta, g - boost_capped)
        cut_floored = max(boost_capped, -max_cut_db)
        if cut_floored > boost_capped:
            clamped_cut += 1
            max_delta = max(max_delta, cut_floored - boost_capped)
        out.append((f, cut_floored))
    return out, ClampSummary(clamped_boost, clamped_cut, round(max_delta, 3))


def suggested_highpass_hz(ref_3db: float = 0.0, ref_6db: float = 0.0) -> float | None:
    """A conservative subsonic high-pass corner from a speaker's published
    extension estimate. Prefers -3 dB (closer to a filter's own -3 dB
    corner); when only -6 dB is known, nudges up ~1/3 octave as a proxy.
    None if neither estimate is available."""
    if ref_3db and ref_3db > 0:
        return round(ref_3db, 1)
    if ref_6db and ref_6db > 0:
        return round(ref_6db * (2 ** (1 / 3)), 1)
    return None


# Phase F0 empirical spike (RUMBLE measurement-integration plan) — before
# writing directivity_boost_cap() below, real Spinorama data was pulled for
# speakers with well-documented *excellent* directivity vs. well-documented
# *poor/inconsistent* directivity, comparing every `estimates{}` scalar
# Spinorama publishes (directivity_spdi_stddev, directivity_horizontal_avg,
# directivity_vertical_avg, smoothness_on_axis/listening_window/
# sound_power/early_reflections). The `smoothness_*` fields did not cleanly
# separate the two groups (e.g. the JBL M2 — a reference cinema monitor with
# textbook horn-controlled directivity — scores *low* on
# smoothness_early_reflections due to horn-lobing roughness in that curve,
# despite excellent actual directivity control). `directivity_horizontal_avg`
# alone didn't separate them either (budget bookshelf speakers often have
# decent horizontal control). What did separate them cleanly, with a clear
# gap and zero overlap between groups, was
# ``min(directivity_horizontal_avg, directivity_vertical_avg)`` — the
# narrower of the two angular windows. This matches the physical reason
# budget 2-way bookshelf speakers misbehave: horizontal dispersion is easy to
# control (tweeter/woofer are coaxial in that axis), but *vertical*
# directivity suffers badly from the tweeter/woofer offset and crossover
# lobing, while well-engineered monitors (coaxial or waveguide-loaded) keep
# both axes wide and symmetric.
#
# Real values recorded (quality="high" ASR/EAC measurements only; degrees):
#   Known-excellent directivity:
#     Genelec 8351B   (ASR)  h=50 v=50  -> min 50
#     Genelec 8341A   (ASR)  h=60 v=55  -> min 55
#     Neumann KH 80   (ASR)  h=50 v=40  -> min 40
#   Known-poor directivity:
#     Dayton Audio B652-AIR (ASR)  h=35 v=45  -> min 35
#     Pioneer SP-BS22-LR    (ASR)  h=50 v=25  -> min 25
#     Polk Audio T15        (ASR)  h=10 v=40  -> min 10
#
# Good-group min ranged 40-55; poor-group min ranged 10-35 — a clean gap.
# Thresholds below were picked to bracket that gap: no reduction at/above the
# best "good" sample (40 deg), maximum reduction (half the cap) at/below a
# point past the worst "poor" sample (15 deg, below Polk's 10 deg floor of
# actually-observed data), linearly interpolated between.
_DIRECTIVITY_FULL_CAP_DEG = 40.0
_DIRECTIVITY_FLOOR_DEG = 15.0
_DIRECTIVITY_MIN_CAP_FRACTION = 0.5


def directivity_boost_cap(base_cap: float, *, horizontal_avg: float = 0.0,
                          vertical_avg: float = 0.0) -> float:
    """Damp `base_cap` when a speaker's published directivity estimates
    indicate poor, narrow off-axis control — a speaker whose off-axis
    behavior is ragged shouldn't get the same aggressive boost as one with
    wide, consistent directivity, since a room will "hear" the off-axis mess
    too. Returns `base_cap` unchanged when either estimate is unavailable
    (0.0/missing) — never assume the worst for a speaker Spinorama hasn't
    scored on this axis. See the comment block above for the real-speaker
    data this formula was calibrated against (Phase F0 of the RUMBLE
    measurement-integration plan)."""
    if horizontal_avg <= 0.0 or vertical_avg <= 0.0:
        return base_cap
    angle = min(horizontal_avg, vertical_avg)
    if angle >= _DIRECTIVITY_FULL_CAP_DEG:
        return base_cap
    if angle <= _DIRECTIVITY_FLOOR_DEG:
        fraction = _DIRECTIVITY_MIN_CAP_FRACTION
    else:
        span = _DIRECTIVITY_FULL_CAP_DEG - _DIRECTIVITY_FLOOR_DEG
        t = (angle - _DIRECTIVITY_FLOOR_DEG) / span
        fraction = _DIRECTIVITY_MIN_CAP_FRACTION + t * (1.0 - _DIRECTIVITY_MIN_CAP_FRACTION)
    return round(base_cap * fraction, 2)


# Rumble 3.6: directivity-*gap* suppression — distinct from and complementary
# to directivity_boost_cap() above. That function damps the boost cap by a
# single per-speaker scalar derived from published horizontal/vertical
# directivity *estimates* (how narrow the speaker's dispersion is, overall).
# This one is per-frequency: it looks at how far a speaker's own Listening-
# Window-vs-Sound-Power (or -Early-Reflections) gap deviates from ITS OWN
# local smoothed trend, not the gap's raw magnitude. Real CEA2034 data shows
# raw |LW-SP| gap does NOT cleanly separate well- from poorly-behaved
# speakers (checked below) — Sound Power naturally rolls off relative to
# Listening Window at high frequencies for essentially every real speaker,
# since directivity narrows as wavelength shrinks relative to driver/baffle
# size, so a flat "gap > N dB" test would misfire on that universal, benign
# broadband divergence and suppress correction almost everywhere at treble.
# A *local* excursion from the gap's own trend instead isolates genuine
# anomalies — crossover-region lobing, baffle-diffraction notches — that a
# global (all-directions-equal) EQ correction cannot actually fix: boosting
# an on-axis dip caused by an off-axis cancellation just pushes the
# cancellation, it doesn't remove it.
#
# Same reference-speaker set as directivity_boost_cap()'s Phase F0 spike
# (data/spinorama_index.json, ASR/EAC "high"-quality measurements only, read
# 2026-07-10). For each speaker: Listening Window and Sound Power fetched
# live 200 Hz-16 kHz, the raw gap LW(f)-SP(f) computed, a 1-octave
# octave_smooth() applied to that gap curve as its local trend, then
# |gap - trend| recorded per point:
#
#   Raw |LW-SP| gap (does NOT separate the groups — confirms the above):
#     Genelec 8351B  mean 6.05  max 8.44   Dayton B652-AIR   mean 4.70  max 9.54
#     Genelec 8341A  mean 5.63  max 7.94   Pioneer SP-BS22LR mean 5.12  max 11.14
#     Neumann KH 80  mean 5.63  max 9.93   Polk Audio T15    mean 4.35  max 10.50
#
#   Local excursion |gap-trend| (cleanly separates the groups):
#     Known-excellent directivity (coaxial/waveguide monitors):
#       Genelec 8351B   max 0.74 dB
#       Genelec 8341A   max 0.50 dB
#       Neumann KH 80   max 1.08 dB
#     Known-poor directivity (2-way bookshelf, crossover lobing):
#       Dayton Audio B652-AIR  max 6.50 dB
#       Pioneer SP-BS22-LR     max 1.25 dB
#       Polk Audio T15         max 2.05 dB
#
# Good-group max excursion ranged 0.50-1.08 dB; poor-group max excursion
# ranged 1.25-6.50 dB — a clean gap, zero overlap. Thresholds below bracket
# it the same way directivity_boost_cap()'s do: no suppression at/below a
# point just past the best good sample (1.1 dB), full suppression at/above a
# point comfortably into the poor group's range (2.5 dB), linear between.
_GAP_TREND_SMOOTH_OCT = 1.0
_GAP_NO_SUPPRESS_DB = 1.1
_GAP_FULL_SUPPRESS_DB = 2.5
_GAP_MIN_FRACTION = 0.1


def directivity_gap_suppression(primary: list[Point], secondary: list[Point],
                                grid: list[float], *,
                                trend_smooth_oct: float = _GAP_TREND_SMOOTH_OCT
                                ) -> list[Point]:
    """Per-frequency suppression multiplier (Rumble 3.6): ``1.0`` = no
    suppression, down to :data:`_GAP_MIN_FRACTION` where *primary* (e.g. a
    speaker's Listening Window) diverges sharply from its own local trend
    against *secondary* (e.g. Sound Power or Early Reflections) — signalling
    a directivity artifact at that frequency rather than a genuine on-axis
    response error. Both curves are resampled onto *grid* first (log-linear,
    via :func:`~rumble.core.autoeq_merge.interp_log`). Returns ``[]`` if
    either curve is empty — callers should treat that as "no suppression
    available" and skip applying it, not as "suppress everything"."""
    if not primary or not secondary:
        return []
    p = interp_log(primary, grid)
    s = interp_log(secondary, grid)
    gap: list[Point] = list(zip(grid, (pv - sv for pv, sv in zip(p, s))))
    trend = dict(octave_smooth(gap, trend_smooth_oct))
    out: list[Point] = []
    for f, g in gap:
        excursion = abs(g - trend[f])
        if excursion <= _GAP_NO_SUPPRESS_DB:
            mult = 1.0
        elif excursion >= _GAP_FULL_SUPPRESS_DB:
            mult = _GAP_MIN_FRACTION
        else:
            t = (excursion - _GAP_NO_SUPPRESS_DB) / (_GAP_FULL_SUPPRESS_DB - _GAP_NO_SUPPRESS_DB)
            mult = 1.0 - t * (1.0 - _GAP_MIN_FRACTION)
        out.append((f, mult))
    return out
