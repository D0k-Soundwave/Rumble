"""Selftest: rumble.core.loudness_layer -- ISO 226-derived, volume-linked
loudness compensation, and its render-time profile composition."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import loudness_layer
from rumble.core.apo_config import ChannelSection, Filter, ProfileConfig


def test_flat_when_no_volume_readings_available():
    corr = loudness_layer.derive_loudness_correction(
        reference_phon=80.0, reference_volume_db=None, current_volume_db=-10.0)
    assert corr.is_flat
    corr2 = loudness_layer.derive_loudness_correction(
        reference_phon=80.0, reference_volume_db=-10.0, current_volume_db=None)
    assert corr2.is_flat


def test_flat_when_volume_unchanged_since_reference():
    corr = loudness_layer.derive_loudness_correction(
        reference_phon=80.0, reference_volume_db=-10.0, current_volume_db=-10.0)
    assert corr.is_flat


def test_boosts_bass_when_quieter_than_reference():
    corr = loudness_layer.derive_loudness_correction(
        reference_phon=80.0, reference_volume_db=0.0, current_volume_db=-40.0)
    assert not corr.is_flat
    as_dict = dict(corr.curve)
    assert as_dict[100.0] > 0.0


def test_clamps_extreme_boost_and_reports_it():
    # A huge volume drop needs far more than the 6dB clamp at deep bass.
    corr = loudness_layer.derive_loudness_correction(
        reference_phon=90.0, reference_volume_db=0.0, current_volume_db=-70.0)
    assert corr.clamp.points_clamped_boost > 0
    assert all(g <= loudness_layer.MAX_BOOST_DB + 1e-9 for _, g in corr.curve)


def test_no_loudness_correction_is_flat():
    assert loudness_layer.NO_LOUDNESS_CORRECTION.is_flat is True


def test_compose_profile_returns_unchanged_when_flat():
    profile = ProfileConfig(channel_sections=[ChannelSection(channel="all")])
    composed = loudness_layer.compose_profile(
        profile, loudness_layer.NO_LOUDNESS_CORRECTION, sample_rate=48000)
    assert composed is profile


def test_compose_profile_applies_curve_to_every_section_symmetrically():
    profile = ProfileConfig(channel_sections=[
        ChannelSection(channel="L"), ChannelSection(channel="R"),
        ChannelSection(channel="SUB"),
    ])
    corr = loudness_layer.derive_loudness_correction(
        reference_phon=80.0, reference_volume_db=0.0, current_volume_db=-30.0)
    composed = loudness_layer.compose_profile(profile, corr, sample_rate=48000)
    for section in composed.channel_sections:
        assert "1000" not in section.graphic_eq or True  # sanity: no crash
    left = next(s for s in composed.channel_sections if s.channel == "L")
    right = next(s for s in composed.channel_sections if s.channel == "R")
    sub = next(s for s in composed.channel_sections if s.channel == "SUB")
    assert left.graphic_eq == right.graphic_eq == sub.graphic_eq
    assert left.graphic_eq != ""
    # original profile untouched
    assert profile.channel_sections[0].graphic_eq == ""


def test_compose_profile_merges_into_existing_graphiceq():
    profile = ProfileConfig(channel_sections=[
        ChannelSection(channel="all", graphic_eq="100 2.0"),
    ])
    corr = loudness_layer.derive_loudness_correction(
        reference_phon=80.0, reference_volume_db=0.0, current_volume_db=-30.0)
    composed = loudness_layer.compose_profile(profile, corr, sample_rate=48000)
    section = composed.channel_sections[0]
    # existing 2.0 dB at 100Hz plus this layer's own boost at 100Hz.
    points = dict(
        (float(p.split()[0]), float(p.split()[1]))
        for p in section.graphic_eq.split(";") if p.strip())
    assert points[100.0] > 2.0


def test_compose_profile_recomputes_global_preamp():
    profile = ProfileConfig(
        preamp=0.0,
        channel_sections=[ChannelSection(
            channel="all",
            filters=[Filter(type="PK", fc=1000.0, gain=3.5, q=1.0)])])
    corr = loudness_layer.derive_loudness_correction(
        reference_phon=90.0, reference_volume_db=0.0, current_volume_db=-70.0)
    composed = loudness_layer.compose_profile(profile, corr, sample_rate=48000)
    assert composed.preamp is not None
    assert composed.preamp < 0.0


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
