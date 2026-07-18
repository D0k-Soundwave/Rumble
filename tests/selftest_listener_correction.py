# tests/selftest_listener_correction.py
"""Selftest: rumble.core.listener_correction -- weak-ear-only correction
derivation and render-time profile composition."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import listener_correction
from rumble.core.apo_config import ChannelSection, ProfileConfig, Filter


def test_derive_correction_boosts_only_weak_ear():
    # imbalance = left - right (hearing_test.normalise()'s convention).
    # Positive => left is weaker (needed a louder tone) => LEFT gets boosted.
    curve = [(1000.0, 4.0), (4000.0, -3.0), (8000.0, 0.0)]
    corr = listener_correction.derive_correction(curve)
    left = dict(corr.left)
    right = dict(corr.right)
    assert left[1000.0] == 4.0 and right[1000.0] == 0.0
    assert right[4000.0] == 3.0 and left[4000.0] == 0.0
    assert left[8000.0] == 0.0 and right[8000.0] == 0.0


def test_derive_correction_caps_at_6db_and_reports_clamp():
    curve = [(1000.0, 9.0)]  # would need +9dB boost on the weak (left) ear
    corr = listener_correction.derive_correction(curve)
    assert dict(corr.left)[1000.0] == listener_correction.CORRECTION_MAX_BOOST_DB
    assert corr.clamp.points_clamped_boost == 1
    assert corr.clamp.max_clamped_delta_db > 0.5


def test_derive_correction_never_cuts():
    curve = [(1000.0, 5.0), (2000.0, -5.0)]
    corr = listener_correction.derive_correction(curve)
    assert all(g >= 0.0 for _, g in corr.left)
    assert all(g >= 0.0 for _, g in corr.right)


def test_no_correction_is_flat():
    assert listener_correction.NO_CORRECTION.is_flat is True
    assert listener_correction.derive_correction([]).is_flat is True
    assert listener_correction.derive_correction([(1000.0, 0.0)]).is_flat is True
    assert listener_correction.derive_correction([(1000.0, 2.0)]).is_flat is False


def test_compose_profile_returns_unchanged_when_flat():
    profile = ProfileConfig(channel_sections=[ChannelSection(channel="all")])
    composed = listener_correction.compose_profile(
        profile, listener_correction.NO_CORRECTION, sample_rate=48000)
    assert composed is profile  # identity, not just equality -- zero overhead when flat


def test_compose_profile_expands_linked_section_into_l_and_r():
    profile = ProfileConfig(channel_sections=[ChannelSection(channel="all")])
    corr = listener_correction.derive_correction([(1000.0, 4.0)])  # left weak
    composed = listener_correction.compose_profile(profile, corr, sample_rate=48000)
    channels = {s.channel for s in composed.channel_sections}
    assert channels == {"L", "R"}
    left = next(s for s in composed.channel_sections if s.channel == "L")
    right = next(s for s in composed.channel_sections if s.channel == "R")
    assert "1000" in left.graphic_eq and "4.0" in left.graphic_eq
    assert right.graphic_eq == "" or "0.0" in right.graphic_eq
    # original profile untouched
    assert profile.channel_sections[0].channel == "all"
    assert profile.channel_sections[0].graphic_eq == ""


def test_compose_profile_expands_combined_l_r_section_into_l_and_r():
    # The COMBINED "Channel: L R" section -- what builtin_profiles_data._body()
    # writes, so this is the layout of every built-in playback profile. It used
    # to fall through compose_channel_sections' untouched branch, making per-ear
    # correction a SILENT no-op on those profiles: a listener could complete a
    # full Ear Calibration and get nothing, with no error. Pin it.
    profile = ProfileConfig(channel_sections=[
        ChannelSection(channel="L R", graphic_eq="1000 1.0")])
    corr = listener_correction.derive_correction([(1000.0, 4.0)])  # left weak
    composed = listener_correction.compose_profile(profile, corr, sample_rate=48000)
    channels = {s.channel for s in composed.channel_sections}
    assert channels == {"L", "R"}, "a combined L R section must split per ear"
    left = next(s for s in composed.channel_sections if s.channel == "L")
    right = next(s for s in composed.channel_sections if s.channel == "R")
    assert "5.0" in left.graphic_eq   # 1.0 existing (carried to BOTH ears) + 4.0
    assert "1.0" in right.graphic_eq  # existing carried, no correction (strong ear)
    # original profile untouched -- storage is never mutated, render-only
    assert profile.channel_sections[0].channel == "L R"
    assert profile.channel_sections[0].graphic_eq == "1000 1.0"


def test_combined_section_carries_filters_and_trim_to_both_ears():
    # Splitting must clone every field, not just the GraphicEQ -- a combined
    # section's filters/delay/trim already applied to both ears, and must
    # still do so after the split.
    profile = ProfileConfig(channel_sections=[ChannelSection(
        channel="L R",
        filters=[Filter(type="PK", fc=100.0, gain=3.0, q=1.0)],
        preamp_db=-2.0, delay_ms=5.0)])
    corr = listener_correction.derive_correction([(1000.0, 4.0)])
    composed = listener_correction.compose_profile(profile, corr, sample_rate=48000)
    assert len(composed.channel_sections) == 2
    for section in composed.channel_sections:
        assert [f.fc for f in section.filters] == [100.0]
        assert section.preamp_db == -2.0
        assert section.delay_ms == 5.0


def test_every_builtin_playback_profile_receives_ear_correction():
    """End-to-end guard: correction is only real if it reaches the profiles
    users actually run. Every shipped playback built-in must come back split
    per ear with the weak ear boosted."""
    from rumble.core.apo_config import parse_config
    from rumble.core.builtin_profiles_data import BUILTIN_PROFILES
    corr = listener_correction.derive_correction([(1000.0, 4.0)])  # left weak
    checked = 0
    for pid, entry in BUILTIN_PROFILES.items():
        if pid.startswith("rumble_voice_"):
            continue
        composed = listener_correction.compose_profile(
            parse_config(entry["body"]), corr, sample_rate=48000)
        channels = [s.channel for s in composed.channel_sections]
        assert "L" in channels and "R" in channels, f"{pid}: not split per ear ({channels})"
        left = next(s for s in composed.channel_sections if s.channel == "L")
        assert "4.0" in left.graphic_eq, f"{pid}: weak ear got no correction"
        checked += 1
    assert checked == 7


def test_compose_profile_corrects_a_channelless_profile():
    # A profile with NO Channel: line at all -- APO applies it to every
    # channel. This is the shape of essentially every real headphone profile
    # (AutoEQ / convolution exports: Preamp + Filters/Convolution, no
    # Channel:). compose_channel_sections() used to have nothing to iterate
    # and returned NO sections, so per-ear correction silently did nothing on
    # exactly the profiles an Ear Calibration exists to correct.
    profile = ProfileConfig(channel_sections=[])
    corr = listener_correction.derive_correction([(1000.0, 4.0)])  # left weak
    composed = listener_correction.compose_profile(profile, corr, sample_rate=48000)
    channels = [s.channel for s in composed.channel_sections]
    assert channels == ["L", "R"], f"expected per-ear sections, got {channels}"
    left = next(s for s in composed.channel_sections if s.channel == "L")
    right = next(s for s in composed.channel_sections if s.channel == "R")
    assert "4.0" in left.graphic_eq, "weak ear must be boosted"
    assert right.graphic_eq == "" or "0.0" in right.graphic_eq
    # Headroom for the new boost must still be taken.
    assert composed.preamp is not None and composed.preamp < 0.0
    # Storage untouched -- render-time composition only.
    assert profile.channel_sections == []


def test_channelless_profile_unchanged_when_correction_is_flat():
    # The zero-overhead path must still hold: no correction, no synthesised
    # sections, byte-identical profile (identity, not a copy).
    profile = ProfileConfig(channel_sections=[])
    composed = listener_correction.compose_profile(
        profile, listener_correction.NO_CORRECTION, sample_rate=48000)
    assert composed is profile
    assert composed.channel_sections == []


def test_compose_profile_merges_into_already_split_sections():
    profile = ProfileConfig(channel_sections=[
        ChannelSection(channel="L", graphic_eq="1000 1.0"),
        ChannelSection(channel="R", graphic_eq="1000 1.0"),
    ])
    corr = listener_correction.derive_correction([(1000.0, 4.0)])  # left weak
    composed = listener_correction.compose_profile(profile, corr, sample_rate=48000)
    left = next(s for s in composed.channel_sections if s.channel == "L")
    right = next(s for s in composed.channel_sections if s.channel == "R")
    assert "5.0" in left.graphic_eq   # 1.0 existing + 4.0 correction
    assert "1.0" in right.graphic_eq  # unchanged (right is the strong ear)


def test_compose_profile_leaves_non_ear_sections_untouched():
    profile = ProfileConfig(channel_sections=[
        ChannelSection(channel="all"), ChannelSection(channel="SUB"),
    ])
    corr = listener_correction.derive_correction([(1000.0, 4.0)])
    composed = listener_correction.compose_profile(profile, corr, sample_rate=48000)
    channels = {s.channel for s in composed.channel_sections}
    assert channels == {"L", "R", "SUB"}
    sub = next(s for s in composed.channel_sections if s.channel == "SUB")
    assert sub.graphic_eq == ""


def test_compose_profile_recomputes_global_preamp_as_min_across_sections():
    profile = ProfileConfig(
        preamp=0.0,
        channel_sections=[ChannelSection(
            channel="all",
            filters=[Filter(type="PK", fc=1000.0, gain=3.5, q=1.0)])])
    corr = listener_correction.derive_correction([(1000.0, 6.0)])  # left weak, capped at 6
    composed = listener_correction.compose_profile(profile, corr, sample_rate=48000)
    # Left ear now carries the +3.5dB PK filter AND +6dB correction --
    # global preamp must be negative enough to keep that ear from clipping,
    # so it must differ from (be more negative than) the uncorrected preamp.
    assert composed.preamp is not None
    assert composed.preamp < 0.0


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
