"""Selftest: ProfileManager's ISO 226 loudness-layer session state and its
composition alongside Listener Slot correction at the two render choke
points (preview_config, _write_active_pointer / activate_profile)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, listener_slots, master_volume
from rumble.core.profile_manager import ProfileManager


def _manager(tmp) -> ProfileManager:
    data_dir = Path(tmp) / "data"
    config_dir = Path(tmp) / "apo"
    return ProfileManager(data_dir, config_path=config_dir)


def test_loudness_correction_flat_when_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert mgr.get_active_loudness_correction().is_flat


def test_loudness_correction_flat_when_enabled_but_uncalibrated():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["iso226_loudness_enabled"] = True
        assert mgr.get_active_loudness_correction().is_flat  # no reference volume yet


def test_calibrate_loudness_reference_stores_current_volume():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: -12.0
        try:
            assert mgr.calibrate_loudness_reference() is True
            assert mgr.settings["iso226_reference_volume_db"] == -12.0
        finally:
            master_volume.get_master_volume_db = real_get


def test_calibrate_loudness_reference_returns_false_when_volume_unreadable():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: None
        try:
            assert mgr.calibrate_loudness_reference() is False
            assert mgr.settings["iso226_reference_volume_db"] is None
        finally:
            master_volume.get_master_volume_db = real_get


def test_calibrate_from_conversation_extrapolates_to_reference_phon():
    # Reference clip target segment level ~= 75 phon; with the default
    # 80-phon mix reference the anchor sits (80-75)=5 dB above the
    # captured volume.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert mgr.calibrate_loudness_reference_from_conversation(-40.0) is True
        assert mgr.settings["iso226_reference_volume_db"] == -35.0


def test_calibrate_from_conversation_respects_custom_reference_phon():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["iso226_reference_phon"] = 70.0
        assert mgr.calibrate_loudness_reference_from_conversation(-40.0) is True
        assert mgr.settings["iso226_reference_volume_db"] == -45.0


def test_calibrate_from_conversation_reads_live_volume_when_not_given():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: -35.0
        try:
            assert mgr.calibrate_loudness_reference_from_conversation() is True
            assert mgr.settings["iso226_reference_volume_db"] == -30.0
        finally:
            master_volume.get_master_volume_db = real_get


def test_calibrate_from_conversation_uses_measured_level_when_given():
    # A real SPL meter reading of the reference clip's target segment (say
    # 78 dB, not the assumed 75) should anchor from THAT instead -- with
    # the default 80-phon mix reference the anchor sits (80-78)=2 dB above
    # the captured volume, not (80-75)=5 dB.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert mgr.calibrate_loudness_reference_from_conversation(
            -40.0, measured_level_db=78.0) is True
        assert mgr.settings["iso226_reference_volume_db"] == -38.0


def test_calibrate_from_conversation_false_when_volume_unreadable():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: None
        try:
            assert mgr.calibrate_loudness_reference_from_conversation() is False
            assert mgr.settings["iso226_reference_volume_db"] is None
        finally:
            master_volume.get_master_volume_db = real_get


def test_refresh_render_layers_rerenders_at_current_volume():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        meta = mgr.create_from_apo_text("Test", "Channel: all\nGraphicEQ: 1000 0.0\n")
        mgr.settings["iso226_loudness_enabled"] = True
        mgr.settings["iso226_reference_volume_db"] = 0.0
        composed_path = mgr.rumble_dir / "rumble_listener_active.txt"
        real_get = master_volume.get_master_volume_db
        try:
            master_volume.get_master_volume_db = lambda: -30.0
            assert mgr.activate_profile(meta.id) is True
            before = composed_path.read_text(encoding="utf-8")
            master_volume.get_master_volume_db = lambda: -10.0
            assert mgr.refresh_render_layers() is True
            after = composed_path.read_text(encoding="utf-8")
            assert before != after   # curve re-derived at the new volume
        finally:
            master_volume.get_master_volume_db = real_get


def test_refresh_render_layers_refuses_while_previewing():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        meta = mgr.create_from_apo_text("Test", "Channel: all\nGraphicEQ: 1000 0.0\n")
        assert mgr.activate_profile(meta.id) is True
        config = apo_config.ProfileConfig(
            channel_sections=[apo_config.ChannelSection(channel="all")])
        assert mgr.preview_config(config) is True
        # A refresh here would clobber the live preview (or an ABX trial).
        assert mgr.refresh_render_layers() is False
        assert mgr.is_previewing


def test_refresh_render_layers_refuses_when_bypassed_or_inactive():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert mgr.refresh_render_layers() is False   # no active profile
        meta = mgr.create_from_apo_text("Test", "Channel: all\nGraphicEQ: 1000 0.0\n")
        assert mgr.activate_profile(meta.id) is True
        mgr.set_bypass(True)
        assert mgr.refresh_render_layers() is False   # pointer holds the bypass render


def test_loudness_correction_reflects_volume_change_from_reference():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["iso226_loudness_enabled"] = True
        mgr.settings["iso226_reference_volume_db"] = 0.0
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: -30.0
        try:
            corr = mgr.get_active_loudness_correction()
            assert not corr.is_flat
        finally:
            master_volume.get_master_volume_db = real_get


def test_preview_config_applies_loudness_correction():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["iso226_loudness_enabled"] = True
        mgr.settings["iso226_reference_volume_db"] = 0.0
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: -30.0
        try:
            config = apo_config.ProfileConfig(
                channel_sections=[apo_config.ChannelSection(channel="all")])
            assert mgr.preview_config(config) is True
            preview_text = (mgr.rumble_dir / "rumble_preview.txt").read_text(encoding="utf-8")
            assert "GraphicEQ" in preview_text
        finally:
            master_volume.get_master_volume_db = real_get


def test_preview_config_composes_both_layers_together():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        listener_slots.save_slot_result(
            mgr.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}},
            {"left": {}, "right": {}})
        mgr.invalidate_listener_correction_cache()
        mgr.settings["iso226_loudness_enabled"] = True
        mgr.settings["iso226_reference_volume_db"] = 0.0
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: -30.0
        try:
            config = apo_config.ProfileConfig(
                channel_sections=[apo_config.ChannelSection(channel="all")])
            assert mgr.preview_config(config) is True
            preview_text = (mgr.rumble_dir / "rumble_preview.txt").read_text(encoding="utf-8")
            # listener correction splits into L/R; loudness layer applies to both
            assert "Channel: L" in preview_text and "Channel: R" in preview_text
        finally:
            master_volume.get_master_volume_db = real_get


def test_activate_profile_composes_loudness_layer_when_listener_correction_flat():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        meta = mgr.create_from_apo_text("Test", "Channel: all\nGraphicEQ: 1000 0.0\n")
        assert meta is not None
        mgr.settings["iso226_loudness_enabled"] = True
        mgr.settings["iso226_reference_volume_db"] = 0.0
        real_get = master_volume.get_master_volume_db
        master_volume.get_master_volume_db = lambda: -30.0
        try:
            assert mgr.activate_profile(meta.id) is True
            pointer_text = (mgr.config_path / "rumble_active.txt").read_text(encoding="utf-8")
            assert "rumble_listener_active.txt" in pointer_text
            composed_text = (mgr.rumble_dir / "rumble_listener_active.txt").read_text(encoding="utf-8")
            assert "GraphicEQ" in composed_text
        finally:
            master_volume.get_master_volume_db = real_get


def test_activate_profile_unchanged_when_both_layers_flat():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        meta = mgr.create_from_apo_text("Test", "Channel: all\nGraphicEQ: 1000 0.0\n")
        assert mgr.activate_profile(meta.id) is True
        pointer_text = (mgr.config_path / "rumble_active.txt").read_text(encoding="utf-8")
        assert meta.file in pointer_text


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
