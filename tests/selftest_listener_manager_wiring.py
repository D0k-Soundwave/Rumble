"""Selftest: ProfileManager's Listener Slot session state, migration-on-boot,
and correction caching."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import listener_slots
from rumble.core.profile_manager import ProfileManager


def _manager(tmp) -> ProfileManager:
    data_dir = Path(tmp) / "data"
    config_dir = Path(tmp) / "apo"
    return ProfileManager(data_dir, config_path=config_dir)


def test_boots_to_default_listener_slot():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert mgr.active_listener_slot == 1  # DEFAULT_SETTINGS default


def test_boots_to_configured_default_slot_on_relaunch():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.settings["default_listener_slot"] = 3
        mgr.save()
        mgr2 = _manager(tmp)  # simulate relaunch: fresh instance, same data_dir
        assert mgr2.active_listener_slot == 3


def test_manual_switch_is_not_sticky_across_relaunch():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.set_active_listener_slot(2)
        assert mgr.active_listener_slot == 2
        mgr2 = _manager(tmp)  # relaunch: default_listener_slot was never touched
        assert mgr2.active_listener_slot == 1


def test_set_active_listener_slot_rejects_invalid_slot():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        try:
            mgr.set_active_listener_slot(5)
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_set_active_listener_slot_fires_callback():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        seen = []
        mgr.on_listener_changed.append(seen.append)
        mgr.set_active_listener_slot(4)
        assert seen == [4]


def test_get_active_correction_reflects_saved_test_a_data():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert mgr.get_active_correction().is_flat
        listener_slots.save_slot_result(
            mgr.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}},
            {"left": {}, "right": {}})
        mgr.invalidate_listener_correction_cache()
        corr = mgr.get_active_correction()
        assert not corr.is_flat
        assert dict(corr.left)[1000.0] == 4.0


def test_correction_toggle_disables_and_reenables_without_losing_data():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        listener_slots.save_slot_result(
            mgr.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}},
            {"left": {}, "right": {}})
        mgr.invalidate_listener_correction_cache()
        assert not mgr.get_active_correction().is_flat
        mgr.settings["listener_correction_enabled"] = False
        assert mgr.get_active_correction().is_flat      # off: profiles uncorrected
        mgr.settings["listener_correction_enabled"] = True
        corr = mgr.get_active_correction()
        assert not corr.is_flat                          # back on: data intact
        assert dict(corr.left)[1000.0] == 4.0


def test_correction_toggle_off_renders_profile_without_correction():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        listener_slots.save_slot_result(
            mgr.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}},
            {"left": {}, "right": {}})
        mgr.invalidate_listener_correction_cache()
        meta = mgr.create_from_apo_text("Test", "Channel: all\nGraphicEQ: 1000 0.0\n")
        assert mgr.activate_profile(meta.id) is True
        pointer = mgr.config_path / "rumble_active.txt"
        assert "rumble_listener_active.txt" in pointer.read_text(encoding="utf-8")

        mgr.settings["listener_correction_enabled"] = False
        assert mgr.refresh_render_layers() is True      # what the settings-save path calls
        # Both layers flat now -> the pointer goes straight at the profile file.
        assert meta.file in pointer.read_text(encoding="utf-8")


def test_switching_slot_invalidates_correction_cache():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        listener_slots.save_slot_result(
            mgr.data_dir, 2, {"left": {}, "right": {}, "imbalance": {1000: 3.0}},
            {"left": {}, "right": {}})
        assert mgr.get_active_correction().is_flat  # slot 1 (default) has no data
        mgr.set_active_listener_slot(2)
        assert not mgr.get_active_correction().is_flat


def test_legacy_hearing_history_migrates_into_slot_1_on_first_boot():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        legacy_dir = data_dir / "hearing" / "frequency"
        legacy_dir.mkdir(parents=True)
        payload = {"norm": {"left": {}, "right": {}, "imbalance": {1000: 5.0}},
                   "raw": {"left": {}, "right": {}}}
        (legacy_dir / "20260101T000000Z.json").write_text(json.dumps(payload), encoding="utf-8")

        mgr = ProfileManager(data_dir, config_path=Path(tmp) / "apo")
        assert mgr.settings["legacy_hearing_migrated"] is True
        runs = listener_slots.load_slot_runs(data_dir, 1)
        assert len(runs) == 1 and runs[0]["norm"]["imbalance"][1000] == 5.0

        # relaunch must not re-migrate / re-copy
        (legacy_dir / "20260102T000000Z.json").write_text(json.dumps(payload), encoding="utf-8")
        mgr2 = ProfileManager(data_dir, config_path=Path(tmp) / "apo")
        assert len(listener_slots.load_slot_runs(data_dir, 1)) == 1


from rumble.core import apo_config

LISTENER_ACTIVE_FILENAME_FOR_TEST = "rumble_listener_active.txt"


def test_preview_config_applies_active_listener_correction():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        listener_slots.save_slot_result(
            mgr.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}},
            {"left": {}, "right": {}})
        mgr.invalidate_listener_correction_cache()
        config = apo_config.ProfileConfig(channel_sections=[apo_config.ChannelSection(channel="all")])
        assert mgr.preview_config(config) is True
        preview_text = (mgr.rumble_dir / "rumble_preview.txt").read_text(encoding="utf-8")
        assert "Channel: L" in preview_text and "Channel: R" in preview_text
        assert "1000" in preview_text and "4.0" in preview_text


def test_preview_config_unchanged_when_no_correction_active():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        config = apo_config.ProfileConfig(channel_sections=[apo_config.ChannelSection(channel="all")])
        assert mgr.preview_config(config) is True
        preview_text = (mgr.rumble_dir / "rumble_preview.txt").read_text(encoding="utf-8")
        assert "Channel: L" not in preview_text  # unsplit -- untouched fallback


def test_activate_profile_composes_when_listener_correction_active():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        meta = mgr.create_from_apo_text(
            "Test", "Channel: all\nGraphicEQ: 1000 0.0\n")
        assert meta is not None
        listener_slots.save_slot_result(
            mgr.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}},
            {"left": {}, "right": {}})
        mgr.invalidate_listener_correction_cache()
        assert mgr.activate_profile(meta.id) is True
        pointer_text = (mgr.config_path / "rumble_active.txt").read_text(encoding="utf-8")
        assert LISTENER_ACTIVE_FILENAME_FOR_TEST in pointer_text
        composed_text = (mgr.rumble_dir / LISTENER_ACTIVE_FILENAME_FOR_TEST).read_text(encoding="utf-8")
        assert "Channel: L" in composed_text and "4.0" in composed_text


def test_activate_profile_unchanged_when_no_correction_active():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        meta = mgr.create_from_apo_text("Test", "Channel: all\nGraphicEQ: 1000 0.0\n")
        assert mgr.activate_profile(meta.id) is True
        pointer_text = (mgr.config_path / "rumble_active.txt").read_text(encoding="utf-8")
        assert meta.file in pointer_text  # direct Include:, exactly today's behavior


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
