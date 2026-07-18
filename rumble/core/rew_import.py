"""REW (Room EQ Wizard) import (Rumble 3.8).

Right now RUMBLE's only measurement inputs are AutoEQ's and Spinorama's
community databases — there is no way to bring in a user's own real
measurement of their own room or headphone unit, which the project's own
research consistently ranks as the single most trustworthy data source,
above any community database (see RUMBLE_ACTION_GUIDE.md #3.8). This module
parses REW's own plain-text export formats, confirmed against REW's
official help documentation (not re-derived from memory), read 2026-07-10:

* ``File > Export > Measurement as text`` — a frequency-response curve.
  https://www.roomeqwizard.com/help/help_en-GB/html/dataimport.html
  ("Comment lines start with *, data lines begin with the frequency, then
  the SPL in dB and finally the phase in degrees"; "Only lines which begin
  with a number are imported, others are ignored" — so ANY non-numeric-
  leading line is a header/comment, not just ``*``-prefixed ones;
  comma/tab/space/semicolon delimited, comma requires a following space;
  frequencies must already be strictly increasing; phase is optional and
  ignored here — only frequency and SPL feed the correction pipeline).

* ``EQ window > Export filter settings as text`` — a list of parametric
  filters for the measurement's current EQ. https://www.roomeqwizard.com/
  help/help_en-GB/html/eqwindow.html and community-confirmed examples, e.g.
  ``Filter 1: ON PK Fc 7515 Hz Gain -9.7 dB Q 1.00``. This line syntax is
  close enough to Equalizer APO's own native ``Filter N: ON <type> Fc <f>
  Hz Gain <g> dB Q <q>`` syntax — both trace back to the same RBJ biquad
  vocabulary (PK/LS/HS/LP/HP/...) — that this reuses
  :func:`rumble.core.apo_config.parse_config` directly rather than writing a
  second filter-line parser, mirroring the Peace importer's (Rumble 4.5)
  precedent of maximizing reuse of the existing APO parser. One REW filter
  convention this can NOT translate: bandwidth given as ``BW/60 <n>`` (a
  -60 dB-referenced bandwidth definition some REW equaliser-target profiles
  export instead of an explicit ``Q <n>``) — unlike APO's own ``BW Oct``
  syntax (which :func:`~rumble.core.apo_config.parse_config` already
  understands), ``BW/60`` is a different definition RUMBLE has no formula to
  convert, so those lines are skipped with a reported (not silent) warning,
  the same honesty convention the Peace importer uses for filter designs it
  can't faithfully translate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rumble.core import apo_config, autoeq_merge, spinorama
from rumble.core.apo_config import Filter

Point = tuple[float, float]

# REW's own import rule (dataimport.html): "Only lines which begin with a
# number are imported, others are ignored" — this is how REW itself
# recognises a data line vs. a header/comment/blank line, so this parser
# applies the identical rule rather than only skipping lines starting with
# REW's own comment marker (`*`), which would miss other header lines
# (column-name rows, notes) REW's own real exports also contain.
_NUMERIC_LEAD_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)")
# REW accepts comma (with a mandatory following space, to stay unambiguous
# with locales that use comma as a decimal separator), tab, space or
# semicolon as the field delimiter (dataimport.html).
_DELIM_RE = re.compile(r"[,\t;]\s*|\s+")

_BW60_RE = re.compile(r"BW\s*/\s*60", re.IGNORECASE)
_NO_FILTER_TYPE_RE = re.compile(r"^\s*NONE\b", re.IGNORECASE)

# Rumble 3.8: a user's own real measurement is, per this project's research,
# strictly MORE trustworthy than even the best-tier community measurement —
# not just tied with it. Reuses spinorama.py's own already-derived
# QUALITY_WEIGHTS scale ({"high": 3.0, "medium": 2.0, "low": 1.0}, Rumble
# 3.2) rather than inventing an unrelated number: double the "high" tier
# expresses "meaningfully more trusted than even the best community source"
# on the same scale, while still leaving it a WEIGHT (so it can be blended,
# not an automatic override) rather than an unbounded/infinite priority.
REW_MEASUREMENT_WEIGHT = 2.0 * spinorama.QUALITY_WEIGHTS["high"]


@dataclass
class RewMeasurementResult:
    """A parsed REW frequency-response measurement export."""

    curve: list[Point] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RewFilterSettingsResult:
    """A parsed REW "Export filter settings as text" export."""

    filters: list[Filter] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def looks_like_filter_settings(text: str) -> bool:
    """True when *text* is a REW "Filter Settings" export rather than a
    frequency-response measurement — REW's filter-settings template always
    emits one or more ``Filter N: ON/OFF ...`` lines; a measurement export
    never does (its data lines start with a bare frequency number). Checked
    line-by-line (not a whole-text search) since ``_FILTER_RE`` is anchored
    to the start of a line, and a filter-settings export always has several
    header lines before the first ``Filter N:`` line."""
    return any(apo_config._FILTER_RE.match(line.strip()) for line in text.splitlines())


def parse_measurement_text(text: str) -> RewMeasurementResult:
    """Parse a REW frequency-response measurement export into a
    ``(freq_hz, gain_db)`` curve, the same point-list shape
    :mod:`rumble.core.eq_target`/:mod:`rumble.core.autoeq_merge` already
    consume. Phase (an optional third column) is ignored — nothing in
    RUMBLE's correction pipeline uses phase today.

    Malformed or out-of-order lines are skipped and reported in
    ``.warnings`` rather than raising or silently corrupting the curve; a
    result with an empty ``.curve`` means nothing usable was found at all.
    """
    points: list[Point] = []
    warnings: list[str] = []
    last_freq: float | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not _NUMERIC_LEAD_RE.match(line):
            continue  # REW's own rule: header/comment/column-title line
        parts = [p for p in _DELIM_RE.split(line) if p]
        if len(parts) < 2:
            continue
        try:
            freq = float(parts[0])
            gain = float(parts[1])
        except ValueError:
            continue
        if freq <= 0:
            continue
        if last_freq is not None and freq <= last_freq:
            warnings.append(f"Skipped out-of-order point at {freq:g} Hz")
            continue
        points.append((freq, gain))
        last_freq = freq
    if 0 < len(points) < 5:
        warnings.append(
            f"Only {len(points)} usable point(s) found — REW itself requires "
            "at least 5, this measurement may be truncated or malformed.")
    return RewMeasurementResult(curve=points, warnings=warnings)


def parse_filter_settings_text(text: str) -> RewFilterSettingsResult:
    """Parse a REW "Export filter settings as text" export into a
    :class:`~rumble.core.apo_config.Filter` list, reusing
    :func:`~rumble.core.apo_config.parse_config` for every line whose syntax
    it understands (see the module docstring). Empty/unassigned filter
    slots (REW's own "None" type) are silently omitted — they're not a
    parse failure, just an unused slot. Any ``BW/60``-defined filter (a
    bandwidth convention APO's own syntax doesn't share) IS a real filter
    RUMBLE can't faithfully translate, so those are reported in ``.skipped``
    instead of guessed at.
    """
    keep_lines: list[str] = []
    skipped: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = apo_config._FILTER_RE.match(line)
        if not m:
            continue
        body = m.group(2)
        if _NO_FILTER_TYPE_RE.match(body):
            continue  # an empty/unassigned filter slot -- not a real filter
        if _BW60_RE.search(body):
            skipped.append(line)
            continue
        keep_lines.append(line)
    if not keep_lines:
        return RewFilterSettingsResult(filters=[], skipped=skipped)
    parsed = apo_config.parse_config("\n".join(keep_lines))
    filters = parsed.channel_sections[0].filters if parsed.channel_sections else []
    return RewFilterSettingsResult(filters=filters, skipped=skipped)


def blend_with_community(rew_curve: list[Point], community_curve: list[Point], *,
                         rew_weight: float = REW_MEASUREMENT_WEIGHT,
                         community_weight: float = 1.0) -> list[Point]:
    """Blend a user's own REW measurement with an existing community curve
    (e.g. one already imported from AutoEQ/Spinorama), weighted toward the
    REW measurement per :data:`REW_MEASUREMENT_WEIGHT`. Reuses
    :func:`~rumble.core.autoeq_merge.average_curves` — the same weighted-
    merge mechanism Rumble 3.1/3.2 already built for multi-measurement
    community averaging — rather than a new blending formula. Callers that
    want a straight replacement (no community blending) simply don't call
    this and pass the REW curve through on its own.
    """
    return autoeq_merge.average_curves(
        [rew_curve, community_curve], weights=[rew_weight, community_weight])
