"""Selftest: A2 profile-switch loudness matching (rumble/core/abx.py's
promoted estimated_gain_db() + ProfileManager._apply_loudness_matching()).

Run:  python tests/selftest_loudness_matching.py

Covers: a hot profile and a flat profile land within 0.1 dB of each other's
estimated gain once matching is on; the reduction-only invariant (a config
already at or below the flat anchor is never boosted); matching off leaves
composed output untouched; and that matching adjusts level only, not shape,
so it doesn't cancel the ISO 226 volume-linked layer's curve.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import abx, apo_config, master_volume
from rumble.core.apo_config import ChannelSection, Filter, ProfileConfig
from rumble.core.profile_manager import PREVIEW_FILENAME, ProfileManager


def _manager(tmp) -> ProfileManager:
    data_dir = Path(tmp) / "data"
    config_dir = Path(tmp) / "apo"
    return ProfileManager(data_dir, config_path=config_dir)


def _hot_config() -> ProfileConfig:
    return ProfileConfig(preamp=0.0, channel_sections=[
        ChannelSection(channel="all",
                       filters=[Filter(enabled=True, type="PK", fc=1000.0, gain=6.0, q=1.0)]),
    ])


def _flat_config() -> ProfileConfig:
    return ProfileConfig(preamp=0.0, channel_sections=[ChannelSection(channel="all")])


def _quiet_config() -> ProfileConfig:
    return ProfileConfig(preamp=-10.0, channel_sections=[ChannelSection(channel="all")])


def _composed_from_preview(mgr: ProfileManager, config: ProfileConfig) -> ProfileConfig:
    assert mgr.preview_config(config) is True
    text = (mgr.rumble_dir / PREVIEW_FILENAME).read_text(encoding="utf-8")
    return apo_config.parse_config(text)


def test_hot_and_flat_profiles_land_within_tolerance_when_on():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["profile_loudness_matching"] = True
        sr = mgr.effective_sample_rate()

        composed_hot = _composed_from_preview(mgr, _hot_config())
        composed_flat = _composed_from_preview(mgr, _flat_config())

        gain_hot = abx.estimated_gain_db(composed_hot, sr)
        gain_flat = abx.estimated_gain_db(composed_flat, sr)
        assert abs(gain_hot - gain_flat) < 0.1, (gain_hot, gain_flat)
        assert abs(gain_hot) < 0.1   # both land at the flat anchor (0 dB)
        assert abs(gain_flat) < 0.1


def test_reduction_only_hot_profile_is_pulled_down_not_up():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["profile_loudness_matching"] = True
        hot = _hot_config()
        composed_hot = _composed_from_preview(mgr, hot)
        assert composed_hot.preamp < (hot.preamp or 0.0)


def test_reduction_only_quiet_profile_is_left_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["profile_loudness_matching"] = True
        quiet = _quiet_config()
        composed_quiet = _composed_from_preview(mgr, quiet)
        # Already quieter than the flat anchor -- never boosted back up.
        assert composed_quiet.preamp == quiet.preamp


def test_off_leaves_composed_output_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert mgr.settings["profile_loudness_matching"] is False
        hot = _hot_config()
        composed_hot = _composed_from_preview(mgr, hot)
        assert composed_hot.preamp == hot.preamp


def test_matching_cache_hits_on_repeated_identical_composed_content():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["profile_loudness_matching"] = True
        hot = _hot_config()
        _composed_from_preview(mgr, hot)
        first_key = mgr._loudness_matching_cache_key
        first_gain = mgr._loudness_matching_cache_gain
        assert first_key is not None

        # Same content again -- the fingerprint should match and the cached
        # gain should be reused rather than recomputed from scratch.
        _composed_from_preview(mgr, hot)
        assert mgr._loudness_matching_cache_key == first_key
        assert mgr._loudness_matching_cache_gain == first_gain


def test_matching_shifts_level_only_never_touches_curve_shape():
    # The ISO 226 / listener-correction layers' own auto-preamp is always
    # peak-based headroom (preamp_calc.calculate_auto_preamp), which is
    # mathematically at least as conservative as the broadband-power anchor
    # loudness matching uses -- so composed output from those layers alone
    # never needs a further reduction. To exercise matching's actual
    # level-shift against a *shaped* (non-flat) curve, apply it directly to
    # a config carrying both a real GraphicEQ curve (standing in for
    # whatever shape the upstream layers already produced) and a hot preamp
    # that pushes its broadband gain above the flat anchor.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["profile_loudness_matching"] = True
        shaped = ProfileConfig(preamp=6.0, channel_sections=[
            ChannelSection(channel="all", graphic_eq="100 3.0;1000 0.0;10000 -2.0"),
        ])
        matched = mgr._apply_loudness_matching(shaped)
        assert matched.channel_sections[0].graphic_eq == shaped.channel_sections[0].graphic_eq
        assert matched.preamp < shaped.preamp


def test_iso226_curve_survives_when_matching_is_a_noop():
    # In the realistic composed-render path, the ISO 226 layer's own
    # auto-preamp already keeps broadband gain at/under the flat anchor (see
    # the note above), so loudness matching correctly does nothing here --
    # and "doing nothing" must still mean the volume-linked curve is left
    # completely intact, not zeroed out or overridden.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["iso226_loudness_enabled"] = True
        mgr.settings["iso226_reference_volume_db"] = 0.0
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: -30.0
        try:
            base_config = ProfileConfig(
                channel_sections=[ChannelSection(channel="all")])

            mgr.settings["profile_loudness_matching"] = False
            without = _composed_from_preview(mgr, base_config)

            mgr.settings["profile_loudness_matching"] = True
            with_matching = _composed_from_preview(mgr, base_config)

            assert without.channel_sections[0].graphic_eq != ""
            assert (without.channel_sections[0].graphic_eq
                    == with_matching.channel_sections[0].graphic_eq)
            assert with_matching.preamp == without.preamp
        finally:
            master_volume.get_master_volume_db = real_get


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
