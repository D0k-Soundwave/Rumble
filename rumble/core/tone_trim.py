"""Session-scoped tone trim: bass/treble shelf + tilt, composed at render
time only -- ChannelSection/ProfileConfig storage is never mutated, and the
trim values themselves are never persisted (see
ProfileManager.session_trim / set_session_trim()).

Roadmap B1: "it's 11pm and this track is harsh" -- an instant, global tone
control, symmetric across every channel like loudness_layer.py (no per-ear
split; this is a taste knob, not a hearing correction). Composed in
ProfileManager._apply_render_layers() between listener correction and the
ISO 226 loudness layer, ahead of the always-last A2 loudness-matching layer.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from rumble.core import autoeq_merge, preamp_calc
from rumble.core.apo_config import ChannelSection, ProfileConfig
from rumble.core.autoeq_merge import Point
from rumble.core.eq_target import clamp_gain, log_grid, tilt_target

# Shelf corners: 250 Hz and 4000 Hz are both members of
# advanced_panel.ISO_10_BAND (31.5 Hz-16 kHz, the ISO 266 preferred
# full-octave series used as RUMBLE's 10-band GEQ scaffold) -- its 4th and
# 8th of ten centres, reused here rather than picking new numbers. These are
# also the same low/high regions builtin_profiles_data's authored
# tone-character profiles (Warm & Smooth, Bright & Clear, etc.) already
# shape with their own LSC/HSC filters.
BASS_SHELF_HZ = 250.0
TREBLE_SHELF_HZ = 4000.0

# Roadmap B1 Decisions: +/-6 dB shelves match eq_target.clamp_gain()'s own
# default max_boost_db -- the same soft cap every other auto-generated curve
# in this codebase uses. +/-3 dB/oct tilt reuses the magnitude of
# settings_dialog.py's spin_default_tilt.setRange(-3.0, 1.0) (Spinorama's
# own default-tilt picker) -- this trim control is symmetric where that
# picker isn't, but 3.0 dB/oct is not a new number.
MAX_SHELF_DB = 6.0
MAX_TILT_DB_PER_OCT = 3.0

# Same soft boost/cut bounds loudness_layer.py and listener_correction.py
# both clamp to (eq_target.clamp_gain()'s own defaults) -- applied to the
# summed shelf+tilt curve here too, since a shelf and the tilt can add
# constructively at either end of the band.
MAX_BOOST_DB = 6.0
MAX_CUT_DB = 12.0

# eq_target.make_correction()'s own default grid (log_grid(20, 20000, 12)),
# widened to also include the two shelf corners, the tilt pivot, and the
# exact 20 kHz upper edge (log_grid's own 12-points/octave rounding
# actually lands its last point at 20480 Hz, not 20000 -- adding it back
# explicitly keeps the treble shelf's stated range true) -- so a shelf's
# transition sits at a literal grid point instead of being smeared between
# two nearby ones, and render output is fully reproducible at those
# frequencies.
_GRID = sorted(set(log_grid(20.0, 20000.0, 12))
               | {BASS_SHELF_HZ, TREBLE_SHELF_HZ, 1000.0, 20000.0})


def derive_trim_curve(bass_db: float = 0.0, treble_db: float = 0.0,
                       tilt_db_per_oct: float = 0.0) -> list[Point]:
    """A merged bass-shelf + treble-shelf + tilt curve, clamped to the
    standard soft boost/cut bounds. Returns ``[]`` (flat -- the common case)
    when all three inputs are exactly zero, without doing any curve math."""
    if bass_db == 0.0 and treble_db == 0.0 and tilt_db_per_oct == 0.0:
        return []
    bass_db = max(-MAX_SHELF_DB, min(MAX_SHELF_DB, bass_db))
    treble_db = max(-MAX_SHELF_DB, min(MAX_SHELF_DB, treble_db))
    tilt_db_per_oct = max(-MAX_TILT_DB_PER_OCT, min(MAX_TILT_DB_PER_OCT, tilt_db_per_oct))
    bass_curve = [(f, bass_db if f <= BASS_SHELF_HZ else 0.0) for f in _GRID]
    treble_curve = [(f, treble_db if f >= TREBLE_SHELF_HZ else 0.0) for f in _GRID]
    tilt_curve = tilt_target(_GRID, tilt_db_per_oct)
    summed = autoeq_merge.sum_curves([bass_curve, treble_curve, tilt_curve])
    clamped, _clamp = clamp_gain(summed, max_boost_db=MAX_BOOST_DB, max_cut_db=MAX_CUT_DB)
    return clamped


def _apply_curve(section: ChannelSection, curve: list[Point]) -> ChannelSection:
    out = section.clone()
    existing = autoeq_merge.parse_graphiceq(out.graphic_eq) if out.graphic_eq else []
    merged = autoeq_merge.sum_curves([existing, curve])
    out.graphic_eq = autoeq_merge.format_graphiceq(merged)
    return out


def compose_profile(profile: ProfileConfig, curve: list[Point], sample_rate: int,
                     ir_response: "np.ndarray | None" = None) -> ProfileConfig:
    """Never mutates *profile*. Returns *profile* itself (zero-copy) when
    *curve* is empty (flat trim) -- mirrors
    listener_correction.compose_profile()'s / loudness_layer.compose_profile()'s
    own zero-copy-when-flat convention. Otherwise every channel section
    (symmetric -- no per-ear split, same as loudness_layer.py) carries the
    merged curve, and the global preamp is recomputed as the minimum
    auto-preamp across those composed sections (same pattern as both other
    layers)."""
    if not curve:
        return profile
    composed_sections = [_apply_curve(s, curve) for s in profile.channel_sections]
    composed = replace(profile, channel_sections=composed_sections)
    if composed_sections:
        composed.preamp = min(
            preamp_calc.calculate_auto_preamp(
                section.filters, sample_rate=sample_rate, geq_string=section.graphic_eq,
                section_offset_db=section.preamp_db or 0.0, ir_response=ir_response)
            for section in composed_sections
        )
    return composed
