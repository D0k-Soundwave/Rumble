"""Convert a GraphicEQ curve into an equivalent list of parametric (PK)
Filter bands.

Per-band Q derivation: RUMBLE's GEQ grids (the 10/15/31-band ISO ladders in
`advanced_panel.py`, and imported curves such as AutoEQ's dense grid) are
locally log-spaced, so a point's neighbours sit some `log2(f_next/f_point)`
octaves away on each side. If a point "owns" the region up to the midpoint
(in log-frequency) to each neighbour, that owned region spans exactly the
sum of the two half-distances to its neighbours, in octaves. Giving the
resulting PK filter that same bandwidth (via `eq_math.bw_to_q`) puts its
-3 dB points close to those midpoints, so adjacent filters hand off to each
other without heavily overlapping. This falls directly out of the grid's
own spacing — nothing here is a tuned/guessed constant.

Overlap correction: even with midpoint-matched bandwidths, adjacent bands
still bleed into each other's centre frequency (their skirts overlap), and
summing in dB (as `eq_math.combined_response` does, matching how APO stacks
biquads) means that bleed adds on top of each band's own target, producing
visible over-boost for curves with several adjacent non-flat points. This is
corrected with a bounded Newton-Raphson pass: treating "response at each
band's own centre frequency" as a smooth function of every band's gain
(each band's own gain dominates its own centre, so the system is
diagonally dominant and well-conditioned), a handful of iterations drives
each band's centre-frequency response to match its target gain exactly,
cancelling out the neighbours' bleed. What's left over — divergence between
grid points, where a bell's rounded shoulder can't perfectly match a
GraphicEQ's straight-line interpolation — is an irreducible property of
approximating a piecewise-linear curve with a small number of parametric
peaking filters; `GeqToPeqResult.max_error_db` surfaces its size so the
caller can disclose it rather than claim a silent perfect match.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from rumble.core.apo_config import DEFAULT_SAMPLE_RATE, MAX_BANDS, Filter
from rumble.core.eq_math import bw_to_q, combined_response, filter_response, \
    frequency_array, graphic_eq_response, parse_graphic_eq, q_to_bw

# Points quieter than this round away to nothing at the 0.1 dB precision
# Filter.to_line() actually renders gain at (apo_config.py) — half that
# resolution is the natural "this point contributes nothing distinguishable
# in the written config" cutoff, not a guessed threshold.
GAIN_EPSILON_DB = 0.05

# Floor for a converted band's bandwidth, derived from the existing filter
# UI's own Q ceiling (`spn_q.setRange(0.1, 100.0)` in filter_row.py) via the
# same bw<->Q relation used everywhere else in this codebase — a converted
# band should never demand a narrower bandwidth than the UI's own Q maximum
# already implies is the practical limit.
MIN_BW_OCT = q_to_bw(100.0)

# Bounded Newton-Raphson passes for the overlap-correction described above;
# the system converges in 2-3 iterations for realistic band counts (see
# tests/selftest_geq_to_peq.py), this is a safety margin, not a tuned value.
_NEWTON_ITERS = 4
_NEWTON_STEP_EPS = 0.01  # finite-difference step, dB

# The precision gains are actually written at: Filter.to_line() renders gain as
# "%.1f" and format_graphiceq() as "{g:.1f}" (apo_config.py / autoeq_merge.py).
# A reconstruction closer than this is therefore exact as far as the written
# profile can express — it is the file format's own floor, not a tolerance we
# chose to accept.
WRITE_PRECISION_DB = 0.1

# The residual layer is written with FINER precision than a filter's gain. It
# can be: APO parses a GraphicEQ point's gain as a plain float, so decimals are
# free there, whereas Filter.to_line() is fixed at "%.1f". Rounding the residual
# at the bands' coarse 0.1 dB would inject up to +/-0.05 dB every time it is
# written — which compounds each time the residual is re-solved for a new sample
# rate. At 2 dp that injection drops to +/-0.005 dB and stops mattering.
RESIDUAL_DECIMALS = 2

# Ceiling on the residual layer's point count. Each point is one draggable
# pg.TargetItem handle in the Graphic EQ editor (advanced_panel.GraphicEQEditor),
# which costs ~0.13 ms each to build (measured: 127 pts = 18 ms, 1009 = 134 ms),
# so this bounds that editor's load time. With adaptive refinement, real AutoEQ
# curves settle well under it — the ceiling is a backstop against a pathological
# curve, not a number the normal path approaches.
MAX_RESIDUAL_POINTS = 1024

# Bisection passes for that refinement. Each pass halves every failing interval,
# so the resolvable detail doubles per pass; 12 passes is a safety stop (the loop
# exits as soon as the target is met, which real curves do in a handful).
_RESIDUAL_REFINE_PASSES = 12


@dataclass
class GeqToPeqResult:
    """Result of converting a GraphicEQ curve to PK filters."""
    filters: list[Filter]
    bands_needed: int          # significant point count before any merging
    bands_used: int             # bands actually returned (== bands_needed unless merged)
    max_error_db: float         # max |combined_response - graphic_eq_response| over the curve's own frequency span
    warning: str | None = None


@dataclass
class _Band:
    f_lo: float
    f_hi: float
    gain: float
    weight: int = 1


def _owned_span_octaves(sorted_freqs: list[float], f_lo: float, f_hi: float) -> float:
    """Octave width a band spanning [f_lo, f_hi] "owns": the core span plus
    half the distance to the grid's own neighbour on each outer side."""
    idx_lo = sorted_freqs.index(f_lo)
    idx_hi = sorted_freqs.index(f_hi)
    half_before = (math.log2(f_lo / sorted_freqs[idx_lo - 1]) / 2.0
                   if idx_lo > 0 else 0.0)
    half_after = (math.log2(sorted_freqs[idx_hi + 1] / f_hi) / 2.0
                  if idx_hi < len(sorted_freqs) - 1 else 0.0)
    core = math.log2(f_hi / f_lo) if f_hi > f_lo else 0.0
    return half_before + core + half_after


def _merge_to_fit(bands: list[_Band], max_bands: int) -> list[_Band]:
    """Merge the adjacent pair whose gains agree most closely, repeatedly,
    until the band count fits `max_bands`. Never drops a point's
    contribution outright — its gain is folded (weighted by how many
    original points it represents) into the merged band."""
    bands = list(bands)
    while len(bands) > max(max_bands, 1):
        best_i = min(range(len(bands) - 1),
                     key=lambda i: abs(bands[i].gain - bands[i + 1].gain))
        a, b = bands[best_i], bands[best_i + 1]
        merged_gain = (a.gain * a.weight + b.gain * b.weight) / (a.weight + b.weight)
        merged = _Band(a.f_lo, b.f_hi, merged_gain, a.weight + b.weight)
        bands[best_i:best_i + 2] = [merged]
    return bands


def _solve_gains(fcs: np.ndarray, qs: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Newton-Raphson: find gains so combined_response of the resulting PK
    filters, evaluated at each filter's own fc, matches `targets`."""
    n = len(fcs)
    gains = targets.copy()
    for _ in range(_NEWTON_ITERS):
        filters = [Filter(type="PK", fc=float(fcs[i]), gain=float(gains[i]),
                           q=float(qs[i]), use_bw=False) for i in range(n)]
        base = combined_response(filters, fcs)
        residual = targets - base
        if np.max(np.abs(residual)) < 1e-4:
            break
        jac = np.zeros((n, n))
        for j in range(n):
            filters[j].gain += _NEWTON_STEP_EPS
            perturbed = combined_response(filters, fcs)
            filters[j].gain -= _NEWTON_STEP_EPS
            jac[:, j] = (perturbed - base) / _NEWTON_STEP_EPS
        try:
            delta = np.linalg.solve(jac, residual)
        except np.linalg.LinAlgError:
            delta, *_ = np.linalg.lstsq(jac, residual, rcond=None)
        gains = gains + delta
    return gains


def convert_geq_to_peq(geq_string: str, max_bands: int) -> GeqToPeqResult | None:
    """Convert `geq_string` (a `GraphicEQ:` value) into PK filters.

    Returns None if the curve is empty or effectively flat (nothing to
    convert). `max_bands` must be at least 1 — the caller is responsible for
    checking there's room (e.g. `MAX_BANDS - len(existing_filters)`) first.
    """
    if max_bands < 1:
        raise ValueError("max_bands must be >= 1")

    points = parse_graphic_eq(geq_string)
    if not points:
        return None
    sorted_freqs = [f for f, _ in points]

    significant = [(f, g) for f, g in points if abs(g) > GAIN_EPSILON_DB]
    if not significant:
        return None

    bands = [_Band(f, f, g) for f, g in significant]
    bands_needed = len(bands)
    bands = _merge_to_fit(bands, max_bands)
    bands_used = len(bands)

    fcs = np.array([math.sqrt(b.f_lo * b.f_hi) for b in bands])
    qs = np.array([
        bw_to_q(max(_owned_span_octaves(sorted_freqs, b.f_lo, b.f_hi), MIN_BW_OCT))
        for b in bands
    ])
    targets = np.array([b.gain for b in bands])

    solved_gains = _solve_gains(fcs, qs, targets)
    filters = [
        Filter(type="PK", fc=float(fcs[i]), gain=float(solved_gains[i]), q=float(qs[i]),
               use_bw=False)
        for i in range(len(bands))
    ]

    freqs = frequency_array()
    geq_resp = graphic_eq_response(geq_string, freqs)
    peq_resp = combined_response(filters, freqs)
    lo, hi = sorted_freqs[0], sorted_freqs[-1]
    in_range = (freqs >= lo) & (freqs <= hi)
    max_error_db = float(np.max(np.abs(geq_resp - peq_resp)[in_range])) if in_range.any() else 0.0

    warning = None
    if bands_used < bands_needed:
        warning = (f"{bands_needed} points were merged into {bands_used} bands to fit "
                    f"the {max_bands}-band limit; approximation error up to "
                    f"{max_error_db:.2f} dB in merged regions.")

    return GeqToPeqResult(
        filters=filters,
        bands_needed=bands_needed,
        bands_used=bands_used,
        max_error_db=max_error_db,
        warning=warning,
    )


# ── least-squares band fitting ────────────────────────────────────────────
#
# convert_geq_to_peq() above MAPS one band per significant curve point: it never
# chooses where a band should sit, so asking it for more bands just hands each
# one fewer grid points to own, driving Q up and gain toward zero (at 32 bands
# on a 127-point curve, half the bands move <1 dB and most land past Q 4 —
# grid wiggle, not audible features). The fitter below instead PLACES bands
# where the curve actually deviates and solves their Fc/gain/Q jointly, so each
# band earns its slot.

# Parameter bounds mirror the editor's own filter-row spin ranges
# (rumble/ui/filter_row.py: gain +/-30 dB, Q 0.1..100, Fc up to
# NYQUIST_FRACTION * sample_rate) — a fitted band the user cannot represent or
# edit in the UI would be a bug, so the solver is constrained to the same box.
_FIT_GAIN_LIMIT_DB = 30.0
_FIT_Q_MIN = 0.1
_FIT_Q_MAX = 100.0
_NYQUIST_FRACTION = 0.5

# Levenberg-Marquardt: damping start and the factor it is scaled by on an
# accepted/rejected step (the textbook 10x).
_LM_LAMBDA0 = 1e-3
_LM_LAMBDA_FACTOR = 10.0

# Iteration ceiling and the relative cost improvement below which a refit is
# called converged. Each refit is warm-started from the previous band set, so it
# begins near its optimum. Measured against a 60-iteration / 1e-6 budget on real
# curves, these produce IDENTICAL band counts and fit error at ~2.6x the speed
# (0.73s -> 0.28s on a rough 17-band room curve) — the iterations they skip were
# chasing digits below WRITE_PRECISION_DB, which the config file cannot write
# anyway. This matters because the fit runs on the UI thread.
_LM_MAX_ITERS = 25
_LM_CONVERGED = 1e-4

# Relative finite-difference step for the Jacobian. Small enough that the
# derivative is accurate, large enough to stay well clear of float64 noise.
_FD_REL_STEP = 1e-4

# RMS of the file's own gain-rounding noise: gains are written to
# WRITE_PRECISION_DB, so each one carries a rounding error uniform in
# +/-(WRITE_PRECISION_DB / 2), whose RMS is that half-quantum over sqrt(3). A
# band that improves the fit by less than this is improving it by less than the
# noise the writer itself injects — it cannot be represented, so it has not
# earned an editor row. This is the greedy loop's "diminishing returns" floor,
# derived rather than tuned.
_ROUNDING_RMS_DB = (WRITE_PRECISION_DB / 2.0) / math.sqrt(3.0)


@dataclass
class Decomposition:
    """A curve split into editable PK bands plus a residual GraphicEQ layer."""
    filters: list[Filter]
    residual: list[tuple[float, float]]  # (freq, gain) points; caller formats
    max_error_db: float                  # of bands + residual vs the original curve
    bands_used: int
    band_error_db: float                 # of the bands ALONE, before the residual


def _pk(fc: float, gain: float, q: float) -> Filter:
    return Filter(type="PK", fc=float(fc), gain=float(gain), q=float(q), use_bw=False)


def quantize_filters(filters: list[Filter]) -> list[Filter]:
    """Round bands to exactly the precision `Filter.to_line()` writes them at
    (Fc and Gain to 0.1, Q to 0.01).

    Without this the residual would be solved against bands that do not exist:
    the fit works in full float precision, but the profile on disk — and so the
    biquad APO actually builds — carries the ROUNDED values. Cancelling the
    unrounded bands leaves the rounding error uncancelled in the shipped file.
    Quantising here makes the returned filters identical to the written ones, so
    everything downstream (the residual, the preamp, the graph) is computed
    against what the user will really hear.
    """
    return [Filter(type=f.type, fc=round(f.fc, 1), gain=round(f.gain, 1),
                   q=round(f.q, 2), use_bw=f.use_bw, enabled=f.enabled)
            for f in filters]


def _band_response(params: np.ndarray, freqs: np.ndarray, sample_rate: int) -> np.ndarray:
    """dB response of one band, params = [fc, gain, q]."""
    return filter_response(_pk(*params), freqs, sample_rate)


def _gain_limit(span_db: float) -> float:
    """How far a single band is allowed to move.

    Capped at the curve's own peak-to-peak span (within the editor's absolute
    +/-30 dB range). A peaking band bigger than the entire span of the curve it
    is fitting cannot be justified by that curve: no frequency asks for more
    than the span, so such a band can only exist to be cancelled by another one.
    Left unbounded, the solver does exactly that — it will happily fit a +/-8 dB
    room curve with a -21 dB band against a +17 dB band, which fits beautifully
    and is useless to a human, because dragging either one swings the response
    wildly. Bounding the box keeps the fit out of that degenerate basin
    altogether (a penalty alone cannot: once there, escaping costs more error
    than any tie-breaking weight will pay).
    """
    return float(min(max(span_db, WRITE_PRECISION_DB), _FIT_GAIN_LIMIT_DB))


def _clamp_params(params: np.ndarray, f_lo: float, f_hi: float,
                  sample_rate: int, gain_limit: float) -> np.ndarray:
    """Hold every band inside the representable, non-degenerate box."""
    out = params.copy()
    nyquist = sample_rate * _NYQUIST_FRACTION
    out[:, 0] = np.clip(out[:, 0], f_lo, min(f_hi, nyquist))
    out[:, 1] = np.clip(out[:, 1], -gain_limit, gain_limit)
    out[:, 2] = np.clip(out[:, 2], _FIT_Q_MIN, _FIT_Q_MAX)
    return out


def _ridge_weight(n_points: int, span_db: float) -> float:
    """Weight of the gain penalty that keeps the fit from going degenerate.

    Without it, the solver happily lands on huge opposing bands that cancel —
    e.g. -19 dB at 7 kHz against +15 dB at 10 kHz on a curve that never leaves
    +/-8 dB. That fits, but it is a terrible *representation*: the bands no
    longer correspond to anything a human would recognise, and nudging one
    swings the response wildly. Penalising the sum of squared gains makes the
    solver prefer the lowest-energy fit among equally good ones.

    The weight is fixed by declaring an exchange rate rather than tuning a
    number: we are willing to accept as much as half a write-quantum of RMS fit
    error to avoid a band whose gain is as large as the curve's entire
    peak-to-peak span (a band that big can only exist to be cancelled by another
    one). Equating those two costs gives the weight below.
    """
    span = max(span_db, WRITE_PRECISION_DB)
    return n_points * (WRITE_PRECISION_DB / 2.0) ** 2 / (span ** 2)


def _lm_refine(params: np.ndarray, freqs: np.ndarray, target: np.ndarray,
               f_lo: float, f_hi: float, sample_rate: int,
               ridge: float, gain_limit: float) -> np.ndarray:
    """Levenberg-Marquardt on every band's (Fc, gain, Q) at once, with a ridge
    penalty on the gains.

    Bands interact — in dB their responses add, so a neighbour's skirt shifts
    what this band should do — which is exactly why they are solved jointly
    rather than one at a time. Each band's response is cached and only the
    perturbed band is recomputed for its Jacobian columns, so a step costs
    ~3N band evaluations rather than N per column.

    The gain penalty enters as extra rows on the residual (sqrt(ridge) * gain
    per band), which is the standard way to fold Tikhonov regularisation into a
    least-squares problem: the Jacobian rows for it are constant, so it costs
    nothing to evaluate.
    """
    params = _clamp_params(params, f_lo, f_hi, sample_rate, gain_limit)
    n = len(params)
    m = len(freqs)
    root_ridge = math.sqrt(ridge)

    def residual_of(bands: np.ndarray, responses: list[np.ndarray]) -> np.ndarray:
        return np.concatenate([target - np.sum(responses, axis=0),
                               root_ridge * bands[:, 1]])

    responses = [_band_response(p, freqs, sample_rate) for p in params]
    err = residual_of(params, responses)
    cost = float(err @ err)
    lam = _LM_LAMBDA0

    for _ in range(_LM_MAX_ITERS):
        jac = np.zeros((m + n, n * 3))
        for i in range(n):
            for k in range(3):
                step = _FD_REL_STEP * max(abs(params[i, k]), 1.0)
                bumped = params[i].copy()
                bumped[k] += step
                jac[:m, 3 * i + k] = (
                    _band_response(bumped, freqs, sample_rate) - responses[i]) / step
            # d(root_ridge * gain_i) / d(gain_i); every other entry stays zero.
            jac[m + i, 3 * i + 1] = root_ridge

        jtj = jac.T @ jac
        jte = jac.T @ err
        try:
            delta = np.linalg.solve(jtj + lam * np.diag(np.diag(jtj) + 1e-12), jte)
        except np.linalg.LinAlgError:
            break

        trial = _clamp_params(params + delta.reshape(n, 3), f_lo, f_hi, sample_rate,
                              gain_limit)
        trial_responses = [_band_response(p, freqs, sample_rate) for p in trial]
        trial_err = residual_of(trial, trial_responses)
        trial_cost = float(trial_err @ trial_err)

        if trial_cost < cost:
            improvement = (cost - trial_cost) / max(cost, 1e-12)
            params, responses, err, cost = trial, trial_responses, trial_err, trial_cost
            lam = max(lam / _LM_LAMBDA_FACTOR, 1e-12)
            if improvement < _LM_CONVERGED:
                break
        else:
            lam *= _LM_LAMBDA_FACTOR
            if lam > 1e12:
                break

    return params


def _seed_q(freqs: np.ndarray, residual: np.ndarray, idx: int) -> float:
    """Q for a band seeded at `idx`, from the width of the residual feature it
    is being placed on: walk out to where the feature falls to half its height
    and read the bandwidth off in octaves (eq_math.bw_to_q is the same
    bw<->Q relation used everywhere else here). A feature's own width is a
    measurement, not a guess."""
    peak = residual[idx]
    half = abs(peak) / 2.0
    lo = hi = idx
    while lo > 0 and abs(residual[lo]) > half and np.sign(residual[lo]) == np.sign(peak):
        lo -= 1
    while hi < len(residual) - 1 and abs(residual[hi]) > half \
            and np.sign(residual[hi]) == np.sign(peak):
        hi += 1
    if hi <= lo:
        return bw_to_q(1.0)
    bw_oct = max(math.log2(freqs[hi] / freqs[lo]), MIN_BW_OCT)
    return float(np.clip(bw_to_q(bw_oct), _FIT_Q_MIN, _FIT_Q_MAX))


def fit_peq(geq_string: str, max_bands: int = MAX_BANDS,
            sample_rate: int = DEFAULT_SAMPLE_RATE,
            target_error_db: float = WRITE_PRECISION_DB) -> GeqToPeqResult | None:
    """Fit PK bands to a GraphicEQ curve, using AS MANY BANDS AS THE CURVE
    EARNS — up to `max_bands`, and no more.

    Bands are added greedily: each new one is seeded at the frequency where the
    curve is still worst-approximated (with its Q read off that feature's own
    width), then EVERY band's Fc/gain/Q is re-solved jointly by
    Levenberg-Marquardt, because bands overlap in dB and adding one changes what
    its neighbours should do.

    The band count is an OUTCOME, not an input. Bands keep being added until one
    of three things is true, each tied to what the config file can actually
    express rather than to a taste threshold:

      * the fit is within `target_error_db` (default WRITE_PRECISION_DB) of the
        curve — the bands already reproduce it to the precision gains are
        written at, so no further band could express an improvement;
      * the new band's fitted gain rounds away (<= GAIN_EPSILON_DB) — it would
        be written as a literal `Gain 0.0 dB` no-op band;
      * the new band improves the fit by less than half a write quantum, i.e.
        by less than the file's own rounding noise.

    Note the improvement test deliberately reads RMS, not peak, error: peak
    error is a minimax quantity that plateaus (a band can flatten one peak while
    another equally tall one remains, showing almost no peak improvement) and
    using it as the greedy signal stops the fit several bands too early.

    Returns None when the curve is empty or effectively flat.
    """
    points = parse_graphic_eq(geq_string)
    if not points or max_bands < 1:
        return None
    src_freqs = np.array([f for f, _ in points], dtype=float)
    src_gains = np.array([g for _, g in points], dtype=float)
    if np.max(np.abs(src_gains)) <= GAIN_EPSILON_DB:
        return None

    f_lo, f_hi = float(src_freqs[0]), float(src_freqs[-1])
    # Fit on a dense log grid spanning the curve, so the solver optimises the
    # response BETWEEN the curve's points too, not just at them.
    freqs = frequency_array()
    in_range = (freqs >= f_lo) & (freqs <= f_hi)
    freqs = freqs[in_range]
    target = np.interp(np.log10(freqs), np.log10(src_freqs), src_gains)

    def errors(bands: np.ndarray) -> tuple[float, float]:
        """(peak, rms) error of a band set against the target curve."""
        err = target - combined_response(
            [_pk(*p) for p in bands], freqs, sample_rate)
        return float(np.max(np.abs(err))), float(np.sqrt(np.mean(err ** 2)))

    span_db = float(np.max(target) - np.min(target))
    ridge = _ridge_weight(len(freqs), span_db)
    gain_limit = _gain_limit(span_db)
    params = np.zeros((0, 3))
    peak, rms = float(np.max(np.abs(target))), float(np.sqrt(np.mean(target ** 2)))

    for _ in range(max_bands):
        residual = target - (combined_response(
            [_pk(*p) for p in params], freqs, sample_rate)
            if len(params) else np.zeros_like(target))
        idx = int(np.argmax(np.abs(residual)))
        if abs(residual[idx]) <= GAIN_EPSILON_DB:
            break

        # Seed the new band on the worst-fitted feature, then re-solve ALL of
        # them: bands overlap in dB, so a new one changes what its neighbours
        # should be doing.
        seed = np.array([[freqs[idx], residual[idx], _seed_q(freqs, residual, idx)]])
        trial = _lm_refine(np.vstack([params, seed]) if len(params) else seed,
                           freqs, target, f_lo, f_hi, sample_rate, ridge, gain_limit)
        trial_peak, trial_rms = errors(trial)

        # Does the new band earn its slot? (The first always does — without it
        # there is no fit at all.)
        if len(params):
            no_op_band = abs(trial[-1][1]) <= GAIN_EPSILON_DB
            below_rounding_noise = (rms - trial_rms) < _ROUNDING_RMS_DB
            if no_op_band or below_rounding_noise:
                break

        params, peak, rms = trial, trial_peak, trial_rms
        if peak <= target_error_db:
            break

    if not len(params):
        return None

    # Quantise to the written precision and re-measure against THOSE bands, so
    # the reported error is the error of the profile that actually ships.
    filters = quantize_filters([_pk(*p) for p in params])
    written_err = target - combined_response(filters, freqs, sample_rate)
    return GeqToPeqResult(
        filters=filters,
        bands_needed=len(filters),
        bands_used=len(filters),
        max_error_db=float(np.max(np.abs(written_err))),
        warning=None,
    )


def _residual_error(filters: list[Filter], src_freqs: np.ndarray, src_gains: np.ndarray,
                    grid: np.ndarray,
                    sample_rate: int = DEFAULT_SAMPLE_RATE) -> tuple[np.ndarray, float]:
    """Residual gains on `grid`, and the error of the reconstruction they give.

    The residual is ``original - bands`` evaluated on `grid`; the error is
    measured against the WRITTEN form (gains rounded to the 0.1 dB the GraphicEQ
    writer emits), over the source curve's own span.
    """
    log_src = np.log10(src_freqs)
    original_on_grid = np.interp(np.log10(grid), log_src, src_gains)
    residual = original_on_grid - combined_response(filters, grid, sample_rate)

    freqs = frequency_array()
    in_range = (freqs >= src_freqs[0]) & (freqs <= src_freqs[-1])
    reconstructed = (combined_response(filters, freqs, sample_rate)
                     + np.interp(np.log10(freqs), np.log10(grid),
                                 np.round(residual, RESIDUAL_DECIMALS)))
    original = np.interp(np.log10(freqs), log_src, src_gains)
    error = (float(np.max(np.abs(original - reconstructed)[in_range]))
             if in_range.any() else 0.0)
    return residual, error


def solve_residual(filters: list[Filter], curve: list[tuple[float, float]],
                   sample_rate: int = DEFAULT_SAMPLE_RATE,
                   target_error_db: float = WRITE_PRECISION_DB,
                   max_residual_points: int = MAX_RESIDUAL_POINTS
                   ) -> tuple[list[tuple[float, float]], float]:
    """The GraphicEQ layer that completes `filters` into `curve` at `sample_rate`.

    Returns ``(points, max_error_db)``. The error is measured on the WRITTEN form
    (gains rounded to RESIDUAL_DECIMALS), which is why this refines rather than
    subtracting once: the residual interpolates LINEARLY between its points while
    the band response it must cancel is curved, so the reconstruction is only as
    good as the grid's resolution wherever that curvature is high.

    The grid starts as the curve's own and is refined ADAPTIVELY — only the
    intervals that actually miss the target get a midpoint inserted. That matters
    because the error is not spread evenly: on real AutoEQ curves it collapses
    into the top octave (biquad response steepens toward Nyquist, and the source
    grids are coarsest there), so a uniform doubling would add ~900 points across
    the whole spectrum to fix a problem living in the last few. Every point is one
    draggable handle in the Graphic EQ editor, so spending them where they are
    needed keeps that editor usable.

    Shared by the initial fit (decompose_geq) and by the sample-rate re-solve
    (autoeq_merge.retune_config_for_sample_rate) so both are held to the same
    written-file accuracy.
    """
    src_freqs = np.array([f for f, _ in curve], dtype=float)
    src_gains = np.array([g for _, g in curve], dtype=float)
    f_lo, f_hi = float(src_freqs[0]), float(src_freqs[-1])

    # Where the reconstruction is judged: the app's own analysis grid, over the
    # span the curve actually defines.
    eval_freqs = frequency_array()
    eval_freqs = eval_freqs[(eval_freqs >= f_lo) & (eval_freqs <= f_hi)]
    log_eval = np.log10(eval_freqs)
    log_src = np.log10(src_freqs)
    want = np.interp(log_eval, log_src, src_gains)
    bands_at_eval = combined_response(filters, eval_freqs, sample_rate)

    grid = src_freqs
    for _ in range(_RESIDUAL_REFINE_PASSES):
        residual = (np.interp(np.log10(grid), log_src, src_gains)
                    - combined_response(filters, grid, sample_rate))
        written = np.round(residual, RESIDUAL_DECIMALS)
        err = np.abs(want - (bands_at_eval
                             + np.interp(log_eval, np.log10(grid), written)))
        worst = float(np.max(err)) if err.size else 0.0
        if worst <= target_error_db or len(grid) >= max_residual_points:
            return list(zip(grid.tolist(), residual.tolist())), worst

        # Split only the intervals that hold a failing evaluation point.
        failing = eval_freqs[err > target_error_db]
        slots = np.unique(np.clip(np.searchsorted(grid, failing) - 1, 0, len(grid) - 2))
        room = max_residual_points - len(grid)
        if not slots.size or room <= 0:
            return list(zip(grid.tolist(), residual.tolist())), worst
        midpoints = np.sqrt(grid[slots[:room]] * grid[slots[:room] + 1])
        grid = np.unique(np.concatenate([grid, midpoints]))

    residual = (np.interp(np.log10(grid), log_src, src_gains)
                - combined_response(filters, grid, sample_rate))
    err = np.abs(want - (bands_at_eval + np.interp(
        log_eval, np.log10(grid), np.round(residual, RESIDUAL_DECIMALS))))
    return (list(zip(grid.tolist(), residual.tolist())),
            float(np.max(err)) if err.size else 0.0)


def decompose_geq(geq_string: str, max_bands: int = MAX_BANDS,
                  sample_rate: int = DEFAULT_SAMPLE_RATE,
                  target_error_db: float = WRITE_PRECISION_DB,
                  max_residual_points: int = MAX_RESIDUAL_POINTS) -> Decomposition | None:
    """Split a GraphicEQ curve into fitted PK bands plus a *residual* curve, the
    two summing back to the original.

    The bands come from :func:`fit_peq`, which uses as many as the curve earns
    (up to `max_bands`). Whatever they still miss is kept, not discarded: APO
    applies a channel section's filters and its GraphicEQ line in series, so
    their dB responses add (the same assumption preamp_calc, eq_graph and abx
    already make), and a residual GraphicEQ layer therefore restores the curve
    exactly while the bands stay editable. When the fit already lands within
    `target_error_db` the residual rounds away to nothing and the profile is
    pure parametric EQ — no GraphicEQ line at all.

    The residual's accuracy between its own points is bounded by how well its
    grid resolves the *smooth* PK response under GraphicEQ's log-linear
    interpolation — sharper bands being exactly the ones a sparse grid
    mis-resolves. So the grid starts as the curve's own and doubles only as far
    as needed to get under `target_error_db`; each point costs one draggable
    handle in the Graphic EQ editor, hence the `max_residual_points` ceiling.

    Returns None when the curve is empty or effectively flat.
    """
    result = fit_peq(geq_string, max_bands=max_bands, sample_rate=sample_rate,
                     target_error_db=target_error_db)
    if result is None:
        return None

    residual, error = solve_residual(
        result.filters, parse_graphic_eq(geq_string), sample_rate=sample_rate,
        target_error_db=target_error_db, max_residual_points=max_residual_points)

    return Decomposition(
        filters=result.filters,
        residual=residual,
        max_error_db=error,
        bands_used=result.bands_used,
        band_error_db=result.max_error_db,
    )
