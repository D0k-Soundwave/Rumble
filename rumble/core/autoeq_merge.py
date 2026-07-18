"""Curve maths for AutoEQ multi-measurement merging and gear simulation.

Pure Python — no Qt and no network — so it can be unit-tested in isolation and
reused from the UI layer. A *curve* is a list of ``(frequency_hz, gain_db)``
points sorted by ascending frequency, exactly as AutoEQ publishes them in its
``GraphicEQ.txt`` exports.

No frequency points are ever invented. When two curves share a grid (the usual
case — AutoEQ uses one standard 127-point grid) the operation is point-for-
point. When grids differ they are reconciled onto the sorted *union* of the
real frequency points, each curve interpolated there with log-linear
interpolation: linear in dB, logarithmic (log10) in frequency. Points outside a
curve's range clamp to its nearest endpoint.
"""
from __future__ import annotations

import bisect
import math

import numpy as np

from rumble.core import eq_math, preamp_calc
from rumble.core.apo_config import (DEFAULT_SAMPLE_RATE, MAX_BANDS, ChannelSection,
                                   Filter, ProfileConfig)
from rumble.core.geq_peq_convert import RESIDUAL_DECIMALS, decompose_geq, solve_residual

Point = tuple[float, float]



def parse_graphiceq(text: str) -> list[Point]:
    """Parse a GraphicEQ export into sorted ``(freq, gain)`` points.

    Accepts the full ``GraphicEQ: f g; f g; …`` line or just the ``f g; …``
    payload. Malformed or non-positive-frequency pairs are skipped.
    """
    s = (text or "").strip()
    if s.lower().startswith("graphiceq") and ":" in s:
        s = s.split(":", 1)[1]
    points: list[Point] = []
    for chunk in s.split(";"):
        parts = chunk.split()
        if len(parts) < 2:
            continue
        try:
            freq, gain = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if freq > 0:
            points.append((freq, gain))
    points.sort(key=lambda p: p[0])
    return points


def _common_grid(curves: list[list[Point]]) -> list[float]:
    """Sorted union of every actual frequency point across *curves*."""
    return sorted({f for curve in curves for f, _ in curve})


def interp_log(curve: list[Point], grid: list[float]) -> list[float]:
    """Interpolate *curve* onto *grid*: linear in dB, log10 in frequency.

    Frequencies in *grid* below/above the curve's range clamp to its first/last
    gain (AutoEQ curves already span the audible band, so this only matters when
    reconciling slightly mismatched grids).
    """
    if not curve:
        return [0.0 for _ in grid]
    xs = [math.log10(f) for f, _ in curve]
    ys = [g for _, g in curve]
    out: list[float] = []
    for f in grid:
        x = math.log10(f)
        if x <= xs[0]:
            out.append(ys[0])
        elif x >= xs[-1]:
            out.append(ys[-1])
        else:
            j = bisect.bisect_right(xs, x)
            x0, x1 = xs[j - 1], xs[j]
            y0, y1 = ys[j - 1], ys[j]
            t = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
            out.append(y0 + t * (y1 - y0))
    return out


def average_curves(curves: list[list[Point]],
                   weights: list[float] | None = None) -> list[Point]:
    """Average the dB values of several curves on a common frequency grid.

    *weights* (optional, parallel to *curves*) scales each curve's
    contribution — e.g. Spinorama's per-measurement quality tier (Rumble 3.2).
    ``None`` (default) is a flat, unweighted average, unchanged from before
    Rumble 3.2 — every existing caller that doesn't pass *weights* keeps its
    exact prior behavior.
    """
    ws = weights if weights is not None else [1.0] * len(curves)
    pairs = [(c, w) for c, w in zip(curves, ws) if c]
    if not pairs:
        return []
    if len(pairs) == 1:
        return list(pairs[0][0])
    curves = [c for c, _ in pairs]
    ws = [w for _, w in pairs]
    total = sum(ws)
    if total <= 0:
        ws = [1.0] * len(ws)
        total = float(len(ws))
    grid = _common_grid(curves)
    columns = [interp_log(c, grid) for c in curves]
    return [(f, sum(col[i] * w for col, w in zip(columns, ws)) / total)
            for i, f in enumerate(grid)]


def spread_curves(curves: list[list[Point]]) -> list[tuple[float, float, float]]:
    """Per-frequency ``(freq, min_gain, max_gain)`` across several curves on
    their reconciled common grid — how much several measurements of "the
    same" device disagree, point by point (Rumble 3.1)."""
    curves = [c for c in curves if c]
    if not curves:
        return []
    if len(curves) == 1:
        return [(f, g, g) for f, g in curves[0]]
    grid = _common_grid(curves)
    columns = [interp_log(c, grid) for c in curves]
    out: list[tuple[float, float, float]] = []
    for i, f in enumerate(grid):
        vals = [col[i] for col in columns]
        out.append((f, min(vals), max(vals)))
    return out


def max_spread_db(spread: list[tuple[float, float, float]]) -> float:
    """The worst per-frequency disagreement in a :func:`spread_curves` result."""
    return max((hi - lo for _, lo, hi in spread), default=0.0)


def normalize_device_key(name: str) -> str:
    """Fold a device name for "same device, different source" grouping.

    Case/whitespace-insensitive exact match only — real AutoEQ INDEX.md data
    (``data/autoeq_index.md``) confirms different sources publish the
    identical bracket text for the same device (e.g. "Sennheiser HD 600"
    appears verbatim across 8 sources), so fuzzy matching would only risk
    false-positive grouping of genuinely different devices.
    """
    return " ".join((name or "").strip().lower().split())


def sum_curves(curves: list[list[Point]]) -> list[Point]:
    """Sum the dB values of several curves on a common frequency grid.

    Exact for APO's GraphicEQ model (piecewise linear in dB over log10
    frequency, flat beyond the endpoints): the sum of such curves is itself
    piecewise linear with breakpoints at the union of the inputs' breakpoints,
    so no information is lost or invented.
    """
    curves = [c for c in curves if c]
    if not curves:
        return []
    if len(curves) == 1:
        return list(curves[0])
    grid = _common_grid(curves)
    columns = [interp_log(c, grid) for c in curves]
    return [(f, sum(col[i] for col in columns)) for i, f in enumerate(grid)]


def delta_curve(target: list[Point], source: list[Point]) -> list[Point]:
    """``target - source`` at each point of their reconciled common grid."""
    if not target or not source:
        return []
    grid = _common_grid([target, source])
    t = interp_log(target, grid)
    s = interp_log(source, grid)
    return [(f, t[i] - s[i]) for i, f in enumerate(grid)]


def compute_preamp(curve: list[Point], sample_rate: int = DEFAULT_SAMPLE_RATE,
                   filters: list[Filter] | None = None) -> float:
    """Pre-amp that keeps *curve* (and any accompanying parametric *filters*,
    e.g. a suggested crossover) from clipping.

    Delegates to :func:`rumble.core.preamp_calc.calculate_auto_preamp` — the
    single source of truth for the anti-clipping margin table (flat 0.5 dB,
    escalating to 1.0 dB per docs/RUMBLE_Headroom_Maths.md Step 4) — so a
    curve-based AutoEQ/Spinorama import and a manually-built parametric
    profile with an equivalent combined response get the identical preamp
    value (Rumble 1.3; previously this used its own flat 0.5 dB margin
    regardless of the response, diverging from calculate_auto_preamp's
    escalation).
    """
    geq_string = format_graphiceq(curve) if curve else ""
    return preamp_calc.calculate_auto_preamp(
        list(filters or []), sample_rate=sample_rate, geq_string=geq_string)


def _format_residual(residual: list[Point]) -> str:
    """Render a fitted profile's residual layer, or "" when the bands captured
    the curve outright — writing an all-zero GraphicEQ line would just be noise
    in the profile."""
    if not any(round(g, RESIDUAL_DECIMALS) for _, g in residual):
        return ""
    return format_graphiceq(residual, decimals=RESIDUAL_DECIMALS)


def build_curve_config(curve: list[Point], *, sample_rate: int = DEFAULT_SAMPLE_RATE,
                       max_bands: int = MAX_BANDS,
                       extra_filters: list[Filter] | None = None,
                       device: str = "") -> ProfileConfig:
    """Build the stereo ("L R") ProfileConfig every curve-derived profile uses —
    AutoEQ/Spinorama imports, merges, gear simulations, REW and room-measurement
    corrections.

    The curve is *decomposed* rather than merely stored: it becomes fitted,
    editable PK bands plus (only if they still miss something) a residual
    GraphicEQ layer that sums with them back to the original curve. Storing the
    curve alone — as every one of these paths used to — produced a profile the
    editor could only render as an empty band list, because the band list shows
    section.filters and nothing else.

    `max_bands` is a CEILING, not a quota: geq_peq_convert.fit_peq() spends a
    band only where the curve still deviates and stops once further bands could
    not express an improvement the config file is even able to write. A gentle
    curve therefore yields a handful of bands, a complex one spends the budget.

    `extra_filters` (e.g. a suggested Spinorama crossover) are intentional
    filters in their own right, so they are kept verbatim alongside the fitted
    bands and eat into the ceiling rather than being folded in.
    """
    extras = list(extra_filters or [])
    if not curve:
        section = ChannelSection(channel="L R", filters=extras)
        return ProfileConfig(
            device=device,
            preamp=preamp_calc.calculate_auto_preamp(extras, sample_rate=sample_rate),
            channel_sections=[section])

    geq_string = format_graphiceq(curve)
    budget = max(1, min(int(max_bands), MAX_BANDS - len(extras)))
    decomposed = decompose_geq(geq_string, max_bands=budget, sample_rate=sample_rate)

    if decomposed is None:
        # Flat/degenerate curve — nothing to turn into bands; keep the old shape.
        filters, residual_geq = extras, geq_string
    else:
        filters = extras + decomposed.filters
        residual_geq = _format_residual(decomposed.residual)

    section = ChannelSection(channel="L R", filters=filters, graphic_eq=residual_geq)
    return ProfileConfig(
        device=device,
        preamp=preamp_calc.calculate_auto_preamp(
            filters, sample_rate=sample_rate, geq_string=residual_geq),
        channel_sections=[section])


def retune_config_for_sample_rate(config: ProfileConfig, old_rate: int,
                                  new_rate: int) -> bool:
    """Re-solve a curve-fitted profile's residual so it is exact at `new_rate`.

    A PK band is a biquad, and a biquad's response warps with the sample rate it
    is realised at; a GraphicEQ line does not warp at all. So the residual that
    completes a fitted profile only cancels the bands exactly at the rate it was
    solved for — play the same file at another rate and the two no longer add up
    to the curve (up to ~1 dB between 96k and 44.1k, mostly above 10 kHz).

    The fix needs no stored copy of the original curve, because the profile still
    contains it: the curve the profile MEANS is exactly what it produces at the
    rate it was fitted at, i.e. ``bands @ old_rate + residual``. Recover that,
    then re-derive the residual against the same bands at `new_rate`. The bands
    are left untouched — they stay the editable representation the user sees, and
    only the correction layer moves.

    This also repairs profiles whose fit was good enough to need no residual at
    all (a pure-parametric result): at the new rate the bands drift, so a fresh
    residual is created to absorb that drift.

    Mutates `config` in place. Returns True when something actually changed.
    Callers must only pass profiles known to be curve-fitted (ProfileMeta
    .fit_sample_rate is set) — on a hand-authored profile the GraphicEQ layer is
    a deliberate part of the tuning, not a residual, and rewriting it would
    destroy the user's work.
    """
    if old_rate == new_rate:
        return False

    changed = False
    for section in config.channel_sections:
        if not section.filters:
            continue  # nothing warps; a pure GraphicEQ section is rate-agnostic
        # What the profile MEANS: what it produces at the rate it was fitted at.
        # Derived from the profile itself rather than from any stored copy of the
        # original curve, which also makes this edit-safe — if the user has since
        # dragged a band, the intent recovered here is their EDITED profile, and
        # the re-solve preserves that instead of silently reverting it.
        intended = section_curve(section, old_rate)
        residual, _ = solve_residual(section.filters, intended, sample_rate=new_rate)
        section.graphic_eq = _format_residual(residual)
        changed = True
    return changed


def section_curve(section: ChannelSection,
                  sample_rate: int = DEFAULT_SAMPLE_RATE) -> list[Point]:
    """A channel section's *total* EQ shape as a curve — the rough inverse of
    :func:`build_curve_config`.

    Reads the PK bands and the GraphicEQ layer and sums them, because that is
    what APO does with them. Anything treating a section's `graphic_eq` alone as
    "the profile's curve" is wrong for any profile built by build_curve_config
    (where the GraphicEQ line holds only the small residual left over from the
    bands) and equally wrong for a hand-built parametric profile (where there is
    no GraphicEQ line at all, yet plainly a curve).

    Sampled on the GraphicEQ layer's own frequency grid when it has one — so a
    decomposed import round-trips on the grid it arrived with — and otherwise on
    a standard log grid. Returns [] for a section with no EQ content.
    """
    if not section.filters and not section.graphic_eq:
        return []
    points = parse_graphiceq(section.graphic_eq) if section.graphic_eq else []
    grid = ([f for f, _ in points] if points
            else [20.0 * (1000.0 ** (i / 126.0)) for i in range(127)])
    freqs = np.array(grid, dtype=float)
    response = eq_math.combined_response(section.filters, freqs, sample_rate)
    if section.graphic_eq:
        response = response + eq_math.graphic_eq_response(section.graphic_eq, freqs)
    return list(zip(grid, response.tolist()))


def _format_freq(freq: float) -> str:
    rounded = round(freq)
    return str(int(rounded)) if abs(freq - rounded) < 1e-6 else f"{freq:g}"


def format_graphiceq(curve: list[Point], decimals: int = 1) -> str:
    """Render a curve as a GraphicEQ value string (gains to `decimals` places).

    Returns just the ``f g; f g; …`` payload (no ``GraphicEQ:`` label), which is
    what ``apo_config.ChannelSection.graphic_eq`` stores.

    A measured curve is written at 1 dp, matching every other gain in the format.
    A fitted profile's RESIDUAL layer is written at
    geq_peq_convert.RESIDUAL_DECIMALS instead: it is a correction term that gets
    re-solved whenever the device sample rate changes, and rounding it at 0.1 dB
    would inject fresh error on every rewrite.
    """
    return "; ".join(f"{_format_freq(f)} {g:.{decimals}f}" for f, g in curve)


def distinct_entry_names(entries: list) -> list[str]:
    """Distinct .name values among entries, in first-seen order."""
    seen: list[str] = []
    for e in entries:
        n = getattr(e, "name", "")
        if n and n not in seen:
            seen.append(n)
    return seen
