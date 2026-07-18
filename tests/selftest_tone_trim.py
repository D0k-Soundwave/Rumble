"""Selftest: B1 session tone-trim layer (rumble/core/tone_trim.py +
ProfileManager.session_trim / set_session_trim() / _apply_tone_trim()).

Run:  python tests/selftest_tone_trim.py

Covers: curve derivation vs hand-computed points at the shelf corners and
tilt pivot; the clamp bounds; flat trim is a zero-copy no-op through the
manager; a non-flat trim merges into every section's GraphicEQ and
recomputes preamp; set_session_trim() never touches the settings file on
disk; and preview_config() reflects the trim (the same choke point ABX
playback and canonical activation go through).
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, tone_trim
from rumble.core.apo_config import ChannelSection, ProfileConfig
from rumble.core.profile_manager import PREVIEW_FILENAME, ProfileManager

_TOL = 1e-9


def _manager(tmp) -> ProfileManager:
    data_dir = Path(tmp) / "data"
    config_dir = Path(tmp) / "apo"
    return ProfileManager(data_dir, config_path=config_dir)


# ── derive_trim_curve() ────────────────────────────────────────────────

def test_all_zero_is_flat_empty_curve():
    assert tone_trim.derive_trim_curve(0.0, 0.0, 0.0) == []


def test_pure_bass_shelf_hand_computed():
    curve = dict(tone_trim.derive_trim_curve(bass_db=4.0, treble_db=0.0, tilt_db_per_oct=0.0))
    assert abs(curve[20.0] - 4.0) < _TOL              # below the corner
    assert abs(curve[tone_trim.BASS_SHELF_HZ] - 4.0) < _TOL   # at the corner (inclusive)
    assert abs(curve[1000.0] - 0.0) < _TOL             # pivot, above the corner
    assert abs(curve[tone_trim.TREBLE_SHELF_HZ] - 0.0) < _TOL
    assert abs(curve[20000.0] - 0.0) < _TOL


def test_pure_treble_shelf_hand_computed():
    curve = dict(tone_trim.derive_trim_curve(bass_db=0.0, treble_db=3.0, tilt_db_per_oct=0.0))
    assert abs(curve[tone_trim.BASS_SHELF_HZ] - 0.0) < _TOL
    assert abs(curve[1000.0] - 0.0) < _TOL
    assert abs(curve[tone_trim.TREBLE_SHELF_HZ] - 3.0) < _TOL  # at the corner (inclusive)
    assert abs(curve[20000.0] - 3.0) < _TOL


def test_pure_tilt_hand_computed_at_pivot_and_corners():
    # BASS_SHELF_HZ and TREBLE_SHELF_HZ are exactly 1000/4 and 1000*4, so
    # log2(f/1000) is exactly +/-2 at each -- clean hand-computed values.
    assert math.isclose(tone_trim.BASS_SHELF_HZ * 4.0, 1000.0)
    assert math.isclose(tone_trim.TREBLE_SHELF_HZ / 4.0, 1000.0)
    curve = dict(tone_trim.derive_trim_curve(bass_db=0.0, treble_db=0.0, tilt_db_per_oct=2.0))
    assert abs(curve[1000.0] - 0.0) < _TOL
    assert abs(curve[tone_trim.BASS_SHELF_HZ] - (2.0 * -2.0)) < _TOL   # -4.0
    assert abs(curve[tone_trim.TREBLE_SHELF_HZ] - (2.0 * 2.0)) < _TOL  # +4.0


def test_combined_shelves_and_tilt_sum_at_corners_and_pivot():
    curve = dict(tone_trim.derive_trim_curve(bass_db=2.0, treble_db=-1.0, tilt_db_per_oct=1.0))
    # pivot: both shelves inactive, tilt is zero there too.
    assert abs(curve[1000.0] - 0.0) < _TOL
    # bass corner: bass shelf (2.0) + treble shelf (0, below its corner) + tilt (1.0*-2.0).
    assert abs(curve[tone_trim.BASS_SHELF_HZ] - (2.0 + 0.0 - 2.0)) < _TOL
    # treble corner: bass shelf (0, above its corner) + treble shelf (-1.0) + tilt (1.0*2.0).
    assert abs(curve[tone_trim.TREBLE_SHELF_HZ] - (0.0 - 1.0 + 2.0)) < _TOL


def test_clamp_caps_extreme_boost():
    # bass shelf at max boost plus a tilt that also boosts the low end
    # stacks well past MAX_BOOST_DB at the grid's low end (20 Hz).
    points = tone_trim.derive_trim_curve(
        bass_db=tone_trim.MAX_SHELF_DB, treble_db=0.0,
        tilt_db_per_oct=-tone_trim.MAX_TILT_DB_PER_OCT)
    curve = dict(points)
    assert curve[20.0] == tone_trim.MAX_BOOST_DB
    assert all(g <= tone_trim.MAX_BOOST_DB + _TOL for _, g in points)


def test_clamp_floors_extreme_cut():
    curve = dict(tone_trim.derive_trim_curve(
        bass_db=-tone_trim.MAX_SHELF_DB, treble_db=0.0,
        tilt_db_per_oct=tone_trim.MAX_TILT_DB_PER_OCT))
    assert curve[20.0] == -tone_trim.MAX_CUT_DB


def test_inputs_beyond_knob_range_are_clamped_before_curve_math():
    unclamped = tone_trim.derive_trim_curve(bass_db=999.0, treble_db=0.0, tilt_db_per_oct=0.0)
    clamped = tone_trim.derive_trim_curve(bass_db=tone_trim.MAX_SHELF_DB, treble_db=0.0,
                                           tilt_db_per_oct=0.0)
    assert unclamped == clamped


# ── ProfileManager wiring ──────────────────────────────────────────────

def test_apply_tone_trim_zero_copy_when_flat():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        config = ProfileConfig(channel_sections=[ChannelSection(channel="all")])
        result = mgr._apply_tone_trim(config)
        assert result is config


def test_apply_tone_trim_merges_every_section_and_recomputes_preamp():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.session_trim["bass_db"] = 4.0
        config = ProfileConfig(preamp=0.0, channel_sections=[
            ChannelSection(channel="L"), ChannelSection(channel="R"),
        ])
        result = mgr._apply_tone_trim(config)
        assert result is not config
        for section in result.channel_sections:
            assert section.graphic_eq != ""
        assert result.preamp is not None
        assert result.preamp < 0.0  # a positive bass boost needs headroom


def test_set_session_trim_never_touches_settings_file():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert not mgr.metadata_path.exists()
        mgr.set_session_trim(bass_db=3.0, treble_db=-2.0, tilt_db_per_oct=1.0)
        assert mgr.session_trim == {"bass_db": 3.0, "treble_db": -2.0, "tilt_db_per_oct": 1.0}
        assert not mgr.metadata_path.exists()


def test_set_session_trim_resets_to_flat_on_new_manager():
    with tempfile.TemporaryDirectory() as tmp:
        mgr1 = _manager(tmp)
        mgr1.set_session_trim(bass_db=5.0)
        mgr2 = _manager(tmp)
        assert mgr2.session_trim == {"bass_db": 0.0, "treble_db": 0.0, "tilt_db_per_oct": 0.0}


def test_preview_config_reflects_trim():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.session_trim["bass_db"] = 4.0
        config = ProfileConfig(channel_sections=[ChannelSection(channel="all")])
        assert mgr.preview_config(config) is True
        text = (mgr.rumble_dir / PREVIEW_FILENAME).read_text(encoding="utf-8")
        composed = apo_config.parse_config(text)
        assert composed.channel_sections[0].graphic_eq != ""


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
