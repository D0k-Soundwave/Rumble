"""ISO 226-derived, volume-linked loudness compensation: derive a curve from
the gap between the user's calibrated reference listening level and their
current WASAPI master volume, then compose it into a ProfileConfig at render
time only -- ChannelSection/ProfileConfig storage is never mutated.

Independent of rumble.core.listener_correction (per-ear, Test-A-derived) --
this layer is symmetric across every channel (there is no "weak ear" concept
for loudness perception) and volume-linked rather than static. Both layers
follow the same "layer, don't merge" architecture from
docs/superpowers/specs/2026-07-09-listener-correction-design.md §5 / §9 (see
rumble.core.listener_correction's own module docstring for the reuse
inventory this shares: autoeq_merge.sum_curves()/eq_target.clamp_gain()/
preamp_calc's peak-response summation).

See docs/superpowers/plans/2026-07-08-rumble-action-guide-campaign.md
Session 16 (Rumble 3.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from rumble.core import autoeq_merge, iso226, preamp_calc
from rumble.core.apo_config import ChannelSection, ProfileConfig
from rumble.core.autoeq_merge import Point
from rumble.core.eq_target import ClampSummary, clamp_gain

#: Same soft boost/cut bounds eq_target.clamp_gain() already uses by default
#: for every other auto-generated correction curve in this codebase
#: (docs/RUMBLE_APP_SPECIFIC_KB.md §7.1's "Correction boost cap"/"Correction
#: cut floor" rows) -- reused here rather than picked anew, per the project's
#: no-guessed-constants rule.
MAX_BOOST_DB = 6.0
MAX_CUT_DB = 12.0

#: Target loudness level for the Ear Calibration reference clip's "target"
#: segment: ~75 dB SPL, and phon == dB SPL at 1 kHz by definition
#: (ISO 226:2003) -- so a listener who has just adjusted their volume to
#: match the clip's target segment is listening at approximately 75 phon.
#:
#: Raised from an earlier 60 dB SPL ("normal conversation") default: the
#: Test A staircase's usable window is -90..-10 dBFS, and at a 60 dB SPL
#: anchor, real ears with any age- or noise-related high-frequency loss
#: (common, and exactly who benefits most from this test) may need more
#: SPL at 12-16 kHz than the -10 dBFS ceiling can deliver once mapped
#: through a quiet anchor -- showing up as a false "gave up" instead of a
#: real reading. 75 dB SPL gives ~10 dB more ceiling headroom while still
#: sitting a full 15 dB below NIOSH/OSHA's well-established 85 dB SPL / 8hr
#: occupational damage-risk threshold, with real margin for a test that
#: lasts minutes, not hours. The reference clip itself (see
#: rumble/assets/audio/volume_reference.wav, built from
#: docs/superpowers -- ask a project session for the generation script if
#: it needs to be regenerated) plays a bracketed quieter/target/louder
#: sequence around this level, described against real-world comparisons
#: (a hair dryer, a whistling kettle, a school bell, a pedestrian crossing
#: signal, a whistle blast) rather than "normal conversation."
REFERENCE_CLIP_TARGET_PHON = 75.0

#: Backward-compatible alias -- some docstrings/comments elsewhere in this
#: codebase still say "conversation"; kept so a stale reference doesn't
#: silently resolve to nothing. Prefer REFERENCE_CLIP_TARGET_PHON in new code.
CONVERSATION_PHON = REFERENCE_CLIP_TARGET_PHON


def reference_volume_from_conversation(conversation_volume_db: float,
                                        reference_phon: float,
                                        measured_phon: float | None = None) -> float:
    """Extrapolate where the *reference_phon* listening level sits on the
    Windows volume scale, given the volume at which the Ear Calibration
    reference clip's target segment was matched by ear
    (~REFERENCE_CLIP_TARGET_PHON, see above) -- or, when the listener had a
    real SPL meter reading of that segment, *measured_phon* instead of the
    assumed default, for a more accurate anchor than the ear-matched guess.

    Uses the same 1 dB ~= 1 phon modeling assumption
    derive_loudness_correction() documents: *reference_phon* is
    (reference_phon - anchor_phon) phon louder than the matched clip level,
    i.e. that many dB further up the volume scale, where anchor_phon is
    *measured_phon* if given, else REFERENCE_CLIP_TARGET_PHON. With the
    default 80-phon mix reference and no measured value this returns
    conversation volume + 5 dB -- anchoring the layer from the Ear
    Calibration volume-setting step without redefining what reference_phon
    means."""
    anchor_phon = measured_phon if measured_phon is not None else REFERENCE_CLIP_TARGET_PHON
    return conversation_volume_db + (reference_phon - anchor_phon)


@dataclass
class LoudnessCorrection:
    """A derived, volume-linked loudness-compensation curve. Not frozen (its
    list field isn't hashable) -- equality still works via field comparison.
    """
    curve: list[Point] = field(default_factory=list)
    clamp: ClampSummary = field(default_factory=ClampSummary)

    @property
    def is_flat(self) -> bool:
        return all(g == 0.0 for _, g in self.curve)


#: Shared "nothing to apply" instance -- cheap identity check on the hot
#: preview path, same convention as listener_correction.NO_CORRECTION.
NO_LOUDNESS_CORRECTION = LoudnessCorrection()


def derive_loudness_correction(reference_phon: float,
                                reference_volume_db: float | None,
                                current_volume_db: float | None) -> LoudnessCorrection:
    """Turn a calibrated reference listening level (*reference_phon*, the
    loudness level the user's content is assumed mixed/mastered to sound
    tonally flat at, captured at WASAPI master volume *reference_volume_db*)
    and the current WASAPI master volume (*current_volume_db*) into an ISO
    226:2003-derived compensation curve.

    Modeling assumption (documented, not a numeric constant): a 1 dB change
    in WASAPI master volume shifts perceived loudness level by
    approximately 1 phon -- the standard practical approximation used by
    volume-linked loudness compensation designs (e.g. AVR "loudness"
    controls tied to the volume knob), since RUMBLE cannot know the absolute
    acoustic SPL without a user-supplied calibration measurement. Flat
    (no correction) when either volume reading is unavailable (no COM/audio
    stack, e.g. offscreen/CI) or unchanged since the reference was captured.
    """
    if reference_volume_db is None or current_volume_db is None:
        return LoudnessCorrection()
    if current_volume_db == reference_volume_db:
        return LoudnessCorrection()
    current_phon = reference_phon + (current_volume_db - reference_volume_db)
    curve = iso226.loudness_compensation_curve(reference_phon, current_phon)
    clamped, clamp = clamp_gain(curve, max_boost_db=MAX_BOOST_DB, max_cut_db=MAX_CUT_DB)
    return LoudnessCorrection(curve=clamped, clamp=clamp)


def _apply_curve(section: ChannelSection, curve: list[Point]) -> ChannelSection:
    out = section.clone()
    existing = autoeq_merge.parse_graphiceq(out.graphic_eq) if out.graphic_eq else []
    merged = autoeq_merge.sum_curves([existing, curve])
    out.graphic_eq = autoeq_merge.format_graphiceq(merged)
    return out


def compose_profile(profile: ProfileConfig, correction: LoudnessCorrection, sample_rate: int,
                     ir_response: "np.ndarray | None" = None) -> ProfileConfig:
    """Never mutates *profile*. Returns *profile* itself (zero-copy) when the
    correction is flat. Otherwise returns a new ProfileConfig whose every
    channel section (symmetric -- no per-ear split, unlike
    listener_correction.compose_profile()) carries the merged curve, and
    whose global preamp is recomputed as the minimum auto-preamp across
    those composed sections (same pattern as listener_correction.py)."""
    if correction.is_flat:
        return profile
    composed_sections = [_apply_curve(s, correction.curve) for s in profile.channel_sections]
    composed = replace(profile, channel_sections=composed_sections)
    if composed_sections:
        composed.preamp = min(
            preamp_calc.calculate_auto_preamp(
                section.filters, sample_rate=sample_rate, geq_string=section.graphic_eq,
                section_offset_db=section.preamp_db or 0.0, ir_response=ir_response)
            for section in composed_sections
        )
    return composed
