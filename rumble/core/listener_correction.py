"""Weak-ear-only per-listener correction: derive a curve from a Listener
Slot's averaged imbalance data, then compose it into a ProfileConfig at
render time only -- ChannelSection/ProfileConfig storage is never mutated.

See docs/superpowers/specs/2026-07-09-listener-correction-design.md §4-5,
§9 (reuse inventory: this module invents no new curve math, only combines
autoeq_merge.sum_curves()/eq_target.clamp_gain()/preamp_calc's existing
peak-response summation the way the design doc specifies).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from rumble.core import autoeq_merge, preamp_calc
from rumble.core.apo_config import ChannelSection, ProfileConfig, stereo_section_ears
from rumble.core.autoeq_merge import Point
from rumble.core.eq_target import ClampSummary, clamp_gain

#: Reuses the already-documented, already-fixed +6dB single-boost cap from
#: hearing_test.py's own export instruction text, independently corroborated
#: by the auto-correction pipeline's own 6dB boost cap (RUMBLE_ACTION_GUIDE.md
#: line 27) -- not a new number (design spec §4.2).
CORRECTION_MAX_BOOST_DB = 6.0

#: The strong ear is never cut (design spec §4.1) -- passing 0.0 here makes
#: that an enforced invariant of clamp_gain(), not just an assumption of how
#: we build the input curves.
CORRECTION_MAX_CUT_DB = 0.0


@dataclass
class EarCorrection:
    """A derived per-ear correction curve pair. Not frozen (its list fields
    aren't hashable) -- equality still works fine via field comparison."""
    left: list[Point] = field(default_factory=list)
    right: list[Point] = field(default_factory=list)
    clamp: ClampSummary = field(default_factory=ClampSummary)

    @property
    def is_flat(self) -> bool:
        return (all(g == 0.0 for _, g in self.left)
                and all(g == 0.0 for _, g in self.right))


#: Shared "nothing to apply" instance -- cheap identity check for callers
#: on the hot preview path (ProfileManager.preview_config() runs on every
#: live-edit debounce tick).
NO_CORRECTION = EarCorrection()


def derive_correction(imbalance_curve: list[Point]) -> EarCorrection:
    """Turn a slot's averaged imbalance curve (left_raw - right_raw at each
    tested frequency, per hearing_test.normalise()) into a weak-ear-only,
    +6dB-capped correction. Positive imbalance => left measured weaker (a
    louder tone was needed) => LEFT gets boosted; negative => RIGHT gets
    boosted. The stronger ear is never cut (design spec §4.1)."""
    if not imbalance_curve:
        return EarCorrection()
    left_raw = [(f, max(delta, 0.0)) for f, delta in imbalance_curve]
    right_raw = [(f, max(-delta, 0.0)) for f, delta in imbalance_curve]
    left_curve, left_clamp = clamp_gain(
        left_raw, max_boost_db=CORRECTION_MAX_BOOST_DB, max_cut_db=CORRECTION_MAX_CUT_DB)
    right_curve, right_clamp = clamp_gain(
        right_raw, max_boost_db=CORRECTION_MAX_BOOST_DB, max_cut_db=CORRECTION_MAX_CUT_DB)
    combined_clamp = ClampSummary(
        points_clamped_boost=left_clamp.points_clamped_boost + right_clamp.points_clamped_boost,
        points_clamped_cut=left_clamp.points_clamped_cut + right_clamp.points_clamped_cut,
        max_clamped_delta_db=max(left_clamp.max_clamped_delta_db, right_clamp.max_clamped_delta_db),
    )
    return EarCorrection(left=left_curve, right=right_curve, clamp=combined_clamp)


def _apply_ear_curve(section: ChannelSection, ear_curve: list[Point]) -> ChannelSection:
    out = section.clone()
    if not ear_curve:
        return out
    existing = autoeq_merge.parse_graphiceq(out.graphic_eq) if out.graphic_eq else []
    merged = autoeq_merge.sum_curves([existing, ear_curve])
    out.graphic_eq = autoeq_merge.format_graphiceq(merged)
    return out


def compose_channel_sections(sections: list[ChannelSection],
                              correction: EarCorrection) -> list[ChannelSection]:
    """Design spec §5 steps 1-2: an already-split L/R section gets its own
    ear's curve merged in via sum_curves(); a section addressing BOTH ears
    (a combined "L R", or the linked "all") is expanded into one section per
    ear so each can carry its own curve (rendered output only -- the caller
    never persists this). Any other channel (SUB, C, SL, SR, ...) is out of
    scope for per-ear correction and is left untouched -- this feature is
    specifically a stereo/per-ear mechanism, not a general multi-channel one.

    Which sections qualify is apo_config.stereo_section_ears()'s call -- the
    ONE definition. This function used to match the channel string against a
    private, exact-match ("L"/"R"/"all") rule that did not recognise the
    combined "L R" section builtin_profiles_data._body() writes, so a
    "Channel: L R" section fell through to the untouched branch and per-ear
    correction was a SILENT NO-OP on every built-in playback profile -- a
    listener could complete a full Ear Calibration and get no correction at
    all, with no error. Fixed 2026-07-13."""
    if correction.is_flat:
        return [s.clone() for s in sections]
    if not sections:
        # No `Channel:` line anywhere means "every channel" -- APO's own
        # default, and the shape of nearly every real headphone profile
        # (AutoEQ/convolution exports are Preamp + Filters/Convolution with
        # no Channel: line at all). Treat it as exactly that, so the loop
        # below expands it per ear like any other `all` section. Without
        # this the loop had nothing to iterate and returned NO sections, so
        # per-ear correction silently did nothing on precisely the profiles
        # an Ear Calibration exists to correct. Fixed 2026-07-13.
        sections = [ChannelSection(channel="all")]
    curves = {"L": correction.left, "R": correction.right}
    composed: list[ChannelSection] = []
    for section in sections:
        ears = stereo_section_ears(section.channel)
        if not ears:
            composed.append(section.clone())
            continue
        for ear in ears:
            # One section per ear: a combined section's filters/GraphicEQ/
            # delay/trim already applied to both ears, so cloning it per ear
            # (clone() carries every field) preserves that exactly, and each
            # copy then merges only its own ear's correction curve.
            per_ear = section.clone()
            per_ear.channel = ear
            composed.append(_apply_ear_curve(per_ear, curves[ear]))
    return composed


def compose_profile(profile: ProfileConfig, correction: EarCorrection, sample_rate: int,
                     ir_response: "np.ndarray | None" = None) -> ProfileConfig:
    """Design spec §5: never mutates *profile*. Returns *profile* itself
    (zero-copy) when the correction is flat -- the common case, since most
    listeners/profiles won't have an active asymmetric correction on any
    given render. Otherwise returns a new ProfileConfig whose channel
    sections carry the merged correction and whose global preamp is
    recomputed as the minimum auto-preamp across those composed sections
    (editor_panel.py:581-586's own pattern, generalized to the render path)."""
    if correction.is_flat:
        return profile
    composed_sections = compose_channel_sections(profile.channel_sections, correction)
    composed = replace(profile, channel_sections=composed_sections)
    if composed_sections:
        composed.preamp = min(
            preamp_calc.calculate_auto_preamp(
                section.filters, sample_rate=sample_rate, geq_string=section.graphic_eq,
                section_offset_db=section.preamp_db or 0.0, ir_response=ir_response)
            for section in composed_sections
        )
    return composed
