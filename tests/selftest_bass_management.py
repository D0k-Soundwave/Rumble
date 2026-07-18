import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from rumble.core import bass_management, eq_math
from rumble.core.apo_config import ChannelSection, Filter, ProfileConfig, parse_config, render_config_body

SR = 48000


def _complex_response(filters, freqs):
    """Complex frequency response of a Filter cascade from eq_math's own RBJ
    coefficients. The COMPLEX response (magnitude AND phase) is what governs
    how the sub's and mains' outputs actually combine acoustically -- a
    magnitude-only sum is blind to LR2's 180-degree HP/LP phase offset (the
    bug this test suite originally missed)."""
    h = np.ones(len(freqs), dtype=complex)
    w = 2 * np.pi * np.asarray(freqs, dtype=float) / SR
    z1 = np.exp(-1j * w)
    z2 = np.exp(-2j * w)
    for f in filters:
        if f.type == "HP":
            b0, b1, b2, a0, a1, a2 = eq_math._coeffs_highpass(f.fc, f.q, SR)
        else:
            b0, b1, b2, a0, a1, a2 = eq_math._coeffs_lowpass(f.fc, f.q, SR)
        h *= (b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2)
    return h


def _crossover_responses(alignment: str, xover_hz: float = 80.0):
    hp_filters = bass_management._crossover_filters(alignment, xover_hz, "HP")
    lp_filters = bass_management._crossover_filters(alignment, xover_hz, "LP")
    freqs = eq_math.frequency_array(300, 20.0, 2000.0)
    hp = _complex_response(hp_filters, freqs)
    lp = _complex_response(lp_filters, freqs)
    at_fc = eq_math.combined_response(hp_filters, np.array([xover_hz]), SR)[0]
    return hp, lp, at_fc


def test_lr2_acoustic_sum_needs_and_gets_sub_inversion():
    # LR2's HP and LP are 180 degrees out of phase (squared 1st-order
    # Butterworth): summed IN phase there is a deep null at the crossover;
    # with one side inverted (what build_bass_management()'s negative Copy:
    # factors implement) the sum is allpass-flat. Both facts asserted so the
    # inversion can't be silently dropped without this test failing.
    hp, lp, at_fc = _crossover_responses("LR2")
    in_phase_db = 20 * np.log10(np.abs(hp + lp) + 1e-30)
    inverted_db = 20 * np.log10(np.abs(hp - lp))
    assert np.min(in_phase_db) < -20.0   # null at fc without inversion
    assert np.max(np.abs(inverted_db)) < 0.01  # flat with inversion
    assert abs(at_fc - (-6.0206)) < 0.01


def test_lr4_acoustic_sum_flat_in_phase():
    # LR4 (squared 2nd-order Butterworth) sums allpass-flat with both sides
    # in phase -- no inversion, matching the positive Copy: weights.
    hp, lp, at_fc = _crossover_responses("LR4")
    in_phase_db = 20 * np.log10(np.abs(hp + lp))
    assert np.max(np.abs(in_phase_db)) < 0.01
    assert abs(at_fc - (-6.0206)) < 0.01


def test_magnitudes_complementary_both_alignments():
    # |HP|+|LP| == 1 everywhere for both alignments (the squared-Butterworth
    # magnitude property) -- the -6.021 dB crossing point follows from it.
    for alignment in ("LR2", "LR4"):
        hp, lp, _ = _crossover_responses(alignment)
        mag_sum_db = 20 * np.log10(np.abs(hp) + np.abs(lp))
        assert np.max(np.abs(mag_sum_db)) < 0.01


def test_lr4_uses_two_cascaded_sections_lr2_uses_one():
    lr2 = bass_management._crossover_filters("LR2", 80.0, "HP")
    lr4 = bass_management._crossover_filters("LR4", 80.0, "HP")
    assert len(lr2) == 1
    assert lr2[0].q == 0.5
    assert len(lr4) == 2
    assert all(abs(f.q - (1.0 / 2 ** 0.5)) < 1e-9 for f in lr4)


def test_unknown_alignment_raises():
    raised = None
    try:
        bass_management._crossover_filters("LR8", 80.0, "HP")
    except ValueError as exc:
        raised = exc
    assert raised is not None


def test_build_2_1_basic_shape():
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0)
    channels = {s.channel: s for s in result.channel_sections}
    assert set(channels) == {"L", "R", "LFE"}
    assert channels["L"].filters[0].type == "HP"
    assert channels["R"].filters[0].type == "HP"
    assert channels["LFE"].filters[0].type == "LP"
    # LR2 -> whole sub feed polarity-inverted (negative Copy: factors).
    assert result.copy_lines == ["LFE=-1*LFE+-0.5*L+-0.5*R"]


def test_build_4_1_weighting_and_channels():
    result = bass_management.build_bass_management(
        layout=["L", "R", "RL", "RR", "LFE"], xover_hz=100.0, alignment="LR4", sub_trim_db=0.0)
    channels = {s.channel: s for s in result.channel_sections}
    assert set(channels) == {"L", "R", "RL", "RR", "LFE"}
    for ch in ("L", "R", "RL", "RR"):
        assert all(f.type == "HP" for f in channels[ch].filters)
        assert len(channels[ch].filters) == 2  # LR4 -> two cascaded sections
    assert channels["LFE"].filters[0].type == "LP"
    # LR4 sums in phase -> plain positive weights, no inversion.
    assert result.copy_lines == ["LFE=LFE+0.25*L+0.25*R+0.25*RL+0.25*RR"]


def test_build_5_1_weighting():
    result = bass_management.build_bass_management(
        layout=["L", "R", "C", "LFE", "RL", "RR"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0)
    # 5 main channels -> 1/5 = 0.2 each (negative: LR2 inverts the sub feed).
    assert result.copy_lines == ["LFE=-1*LFE+-0.2*L+-0.2*R+-0.2*C+-0.2*RL+-0.2*RR"]


def test_layout_without_lfe_raises():
    raised = None
    try:
        bass_management.build_bass_management(
            layout=["L", "R"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0)
    except ValueError as exc:
        raised = exc
    assert raised is not None


def test_layout_with_only_lfe_raises():
    raised = None
    try:
        bass_management.build_bass_management(
            layout=["LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0)
    except ValueError as exc:
        raised = exc
    assert raised is not None


def test_trims_land_in_correct_sections():
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2",
        sub_trim_db=-2.5, main_trims_db={"L": 1.0, "R": -1.0})
    channels = {s.channel: s for s in result.channel_sections}
    assert channels["L"].preamp_db == 1.0
    assert channels["R"].preamp_db == -1.0
    assert channels["LFE"].preamp_db == -2.5


def test_zero_trim_stores_none_not_zero():
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0)
    channels = {s.channel: s for s in result.channel_sections}
    assert channels["L"].preamp_db is None
    assert channels["LFE"].preamp_db is None


def test_distances_rebase_to_farthest_channel():
    # 343.0 cm @ 343 m/s = exactly 10.0 ms transit; 686.0 cm = exactly 20.0 ms.
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0,
        distances={"L": 343.0, "R": 686.0})
    channels = {s.channel: s for s in result.channel_sections}
    assert abs(channels["R"].delay_ms - 0.0) < 1e-9   # farthest -> 0 ms
    assert abs(channels["L"].delay_ms - 10.0) < 1e-9  # 20ms - 10ms top-up
    assert channels["LFE"].delay_ms is None  # not given a distance


def test_distances_can_include_sub_channel():
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0,
        distances={"L": 0.0, "R": 0.0, "LFE": 343.0})
    channels = {s.channel: s for s in result.channel_sections}
    assert abs(channels["LFE"].delay_ms - 0.0) < 1e-9  # farthest
    assert abs(channels["L"].delay_ms - 10.0) < 1e-9
    assert abs(channels["R"].delay_ms - 10.0) < 1e-9


def test_channels_without_distance_entry_get_no_delay_line():
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0)
    for section in result.channel_sections:
        assert section.delay_ms is None


def test_preamp_covers_summed_peak_no_clipping():
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=0.0,
        sample_rate=SR)
    assert result.preamp_db < 0.0  # summed feed headroom always forces some attenuation
    for section in result.channel_sections:
        offset = section.preamp_db or 0.0
        if section.channel == bass_management.SUB_CHANNEL:
            offset += bass_management._SUMMED_FEED_HEADROOM_DB
        from rumble.core import preamp_calc
        headroom = preamp_calc.calculate_headroom(
            section.filters, result.preamp_db, sample_rate=SR, section_offset_db=offset)
        assert headroom >= -1e-6, f"{section.channel} would clip: headroom={headroom}"


def test_preamp_covers_hot_sub_trim():
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=3.0)
    channels = {s.channel: s for s in result.channel_sections}
    from rumble.core import preamp_calc
    offset = channels["LFE"].preamp_db + bass_management._SUMMED_FEED_HEADROOM_DB
    headroom = preamp_calc.calculate_headroom(
        channels["LFE"].filters, result.preamp_db, sample_rate=SR, section_offset_db=offset)
    assert headroom >= -1e-6


def test_round_trips_through_parse_and_render():
    result = bass_management.build_bass_management(
        layout=["L", "R", "LFE"], xover_hz=80.0, alignment="LR2", sub_trim_db=-2.0,
        distances={"L": 343.0, "R": 686.0})
    profile = ProfileConfig(
        channel_sections=result.channel_sections,
        copy_lines=result.copy_lines,
        preamp=result.preamp_db,
    )
    rendered = render_config_body(profile)
    reparsed = parse_config(rendered, sample_rate=SR)

    assert reparsed.copy_lines == result.copy_lines
    assert abs(reparsed.preamp - result.preamp_db) < 1e-6

    reparsed_channels = {s.channel: s for s in reparsed.channel_sections}
    original_channels = {s.channel: s for s in result.channel_sections}
    assert set(reparsed_channels) == set(original_channels)
    for name, orig in original_channels.items():
        rt = reparsed_channels[name]
        assert len(rt.filters) == len(orig.filters)
        for of, rf in zip(orig.filters, rt.filters):
            assert rf.type == of.type
            assert abs(rf.fc - of.fc) < 0.05
            assert abs(rf.q - of.q) < 0.005  # Q renders at 2 decimal places
        if orig.delay_ms is not None:
            assert abs(rt.delay_ms - orig.delay_ms) < 0.05
        else:
            assert rt.delay_ms is None
        if orig.preamp_db is not None:
            assert abs(rt.preamp_db - orig.preamp_db) < 0.05
        else:
            assert rt.preamp_db is None


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
