# tests/selftest_config_health.py
"""Selftest: config-health / effective-chain viewer (Rumble 4.6,
docs/PLANNED_WORK.md #3) — a multi-level Include: chain (including a
conditional If block, and RUMBLE's own managed indirection) resolves to the
correct fully-flattened effective config text, with each line traceable to
the file it came from."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, audio_devices
from rumble.core.audio_devices import ApoDevice
from rumble.core.profile_manager import CONFIG_FILENAME, ProfileManager


def test_resolve_includes_traced_multi_level_chain():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "sub.txt").write_text("Filter: ON PK Fc 1000 Hz Gain 2 dB Q 1\n", encoding="utf-8")
        (d / "mid.txt").write_text("Include: sub.txt\nPreamp: -3 dB\n", encoding="utf-8")
        text = "Include: mid.txt\nFilter: ON PK Fc 100 Hz Gain 1 dB Q 1\n"

        lines = apo_config.resolve_includes_traced(text, d, "config.txt")
        resolved_text = "\n".join(l.text for l in lines)

        assert "Fc 1000" in resolved_text
        assert "Preamp: -3 dB" in resolved_text
        assert "Fc 100" in resolved_text

        sources = {l.source for l in lines}
        assert {"sub.txt", "mid.txt", "config.txt"} <= sources
        # The Fc 1000 line must be traceable to sub.txt specifically, not the
        # top-level file — that's the whole point of the traced variant.
        assert next(l.source for l in lines if "Fc 1000" in l.text) == "sub.txt"


def test_resolve_includes_traced_keeps_rumbles_own_tree():
    """Unlike resolve_includes() (used for import discovery, which deliberately
    drops RUMBLE's own indirection to surface only the user's content), the
    traced viewer must keep it: it's real content that IS applying, and
    showing "what's actually running" is the entire point of this feature."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rumble_dir = d / "rumble"
        rumble_dir.mkdir()
        (d / "rumble_active.txt").write_text("Include: rumble/active_profile.txt\n", encoding="utf-8")
        (rumble_dir / "active_profile.txt").write_text("Preamp: -6 dB\n", encoding="utf-8")

        lines = apo_config.resolve_includes_traced("Include: rumble_active.txt\n", d, "config.txt")
        resolved_text = "\n".join(l.text for l in lines)
        assert "Preamp: -6 dB" in resolved_text


def test_resolve_includes_traced_cycle_guard_terminates():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "a.txt").write_text("Include: b.txt\nPreamp: -1 dB\n", encoding="utf-8")
        (d / "b.txt").write_text("Include: a.txt\nFilter: ON PK Fc 50 Hz Gain 1 dB Q 1\n", encoding="utf-8")

        lines = apo_config.resolve_includes_traced("Include: a.txt\n", d, "config.txt")
        resolved_text = "\n".join(l.text for l in lines)
        assert "Fc 50" in resolved_text
        assert "Preamp: -1 dB" in resolved_text


def test_resolve_includes_traced_missing_include_noted_not_silently_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        lines = apo_config.resolve_includes_traced("Include: missing.txt\nPreamp: -2 dB\n", d, "config.txt")
        resolved_text = "\n".join(l.text for l in lines)
        assert "missing.txt" in resolved_text  # noted, not silently dropped
        assert "Preamp: -2 dB" in resolved_text


def test_effective_config_report_reflects_sources_and_conditional_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        apo_dir = Path(tmp) / "apo"
        apo_dir.mkdir(parents=True)
        mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
        (apo_dir / "device_profile.txt").write_text(
            "Filter: ON PK Fc 200 Hz Gain -2 dB Q 1\n", encoding="utf-8")
        (apo_dir / CONFIG_FILENAME).write_text(
            "Preamp: -6 dB\n"
            "Include: device_profile.txt\n"
            'If: deviceName == "Speakers"\n'
            "  Preamp: -1 dB\n"
            "EndIf:\n",
            encoding="utf-8")

        report = mgr.effective_config_report()
        assert "Fc 200" in report.resolved_text
        assert "device_profile.txt" in report.sources
        assert CONFIG_FILENAME in report.sources
        assert report.conditional_block_count == 1


def test_effective_config_report_empty_when_no_config():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(Path(tmp) / "data", config_path=Path(tmp) / "apo")
        report = mgr.effective_config_report()
        assert report.resolved_text == ""
        assert report.conditional_block_count == 0
        assert report.sources == []


def test_effective_config_report_traces_per_device_dispatch_legibly():
    """D1c: with device_profile_assignments active, rumble_active.txt becomes a
    per-device dispatch (S19); the traced viewer must still show each
    Device: block immediately followed by ITS OWN branch content, and list
    each per-device scratch file as its own source -- not collapse the
    branches together or mislabel one branch's content as another's."""
    with tempfile.TemporaryDirectory() as tmp:
        apo_dir = Path(tmp) / "apo"
        mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
        mgr.ensure_first_run()
        meta_a = mgr.create_from_apo_text("A", "Channel: all\nGraphicEQ: 1000 0.0\n")
        meta_global = mgr.create_from_apo_text("Global", "Channel: all\nGraphicEQ: 1000 0.0\n")
        mgr.activate_profile(meta_global.id)

        guid_a = "{AAAAAAAA-0000-0000-0000-000000000001}"
        device_a = ApoDevice(guid=guid_a, device_name="Speakers",
                              connection_name="Creative BT-W6", flow="Render", state=1)
        real_list = audio_devices.list_apo_devices
        audio_devices.list_apo_devices = lambda: [device_a]
        try:
            from rumble.core import listener_slots
            listener_slots.save_slot_result(
                mgr.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}}, {"left": {}, "right": {}})
            mgr.invalidate_listener_correction_cache()
            mgr.settings["device_profile_assignments"] = {guid_a: meta_a.id}
            mgr.save()
            mgr.activate_profile(meta_global.id)  # re-render with the assignment + listener correction

            report = mgr.effective_config_report()
            assert f"Device: {device_a.pattern}" in report.resolved_text
            assert "Device: all" in report.resolved_text
            # Each branch's own composed scratch file is a distinct, named source.
            device_scratch = mgr._device_scratch_filename(guid_a)
            assert any(device_scratch in s for s in report.sources), report.sources
            assert any("rumble_listener_active.txt" in s for s in report.sources), report.sources

            # Legibility: the Device: <pattern A> block must be followed (before
            # the next Device: line) by ITS OWN branch's traced content, not the
            # fallback's -- both branches carry the same listener-corrected
            # "Channel: L ... 4.0" shape here, so distinguish by which scratch
            # file each line is tagged with.
            device_a_lines = [l for l in apo_config.resolve_includes_traced(
                (apo_dir / "rumble_active.txt").read_text(encoding="utf-8"), apo_dir, "rumble_active.txt")]
            idx_device_a = next(i for i, l in enumerate(device_a_lines)
                                 if l.text.strip() == f"Device: {device_a.pattern}")
            idx_all = next(i for i, l in enumerate(device_a_lines) if l.text.strip() == "Device: all")
            assert idx_device_a < idx_all
            # Exclude lines sourced from rumble_active.txt itself (the Device:
            # lines/blank separators the dispatch writes directly) -- only
            # compare the INLINED branch content each Device: block pulls in.
            branch_a_sources = {l.source for l in device_a_lines[idx_device_a:idx_all]
                                 if l.source != "rumble_active.txt"}
            fallback_sources = {l.source for l in device_a_lines[idx_all:]
                                 if l.source != "rumble_active.txt"}
            assert device_scratch in {Path(s).name for s in branch_a_sources}
            assert "rumble_listener_active.txt" in {Path(s).name for s in fallback_sources}
            assert branch_a_sources.isdisjoint(fallback_sources), \
                "each branch's content must be tagged with its OWN scratch file, not the other branch's"
        finally:
            audio_devices.list_apo_devices = real_list


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
