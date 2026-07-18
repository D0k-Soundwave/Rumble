"""Self-test for D1a per-device profile dispatch (core only, no Qt).

Covers: zero-assignment byte-identity with the pre-D1a single-profile
render, N-assignment dispatch structure (Device: block order, per-branch
Include targets), render-layer composition per branch, graceful fallback
for an assignment to a missing profile, stale rumble_device_*.txt scratch
cleanup, drift/ownership detection unaffected, bypass/preview overriding
the whole dispatch (D1a), and an ABX trial mid-assignments (D1c).

Run:  python tests/selftest_device_dispatch.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import abx, apo_config, audio_devices, listener_slots
from rumble.core.audio_devices import ApoDevice
from rumble.core.profile_manager import (
    ACTIVE_POINTER_FILENAME, DEVICE_SCRATCH_PREFIX, LISTENER_ACTIVE_FILENAME,
    MANAGED_MARKER, RUMBLE_SUBDIR, ProfileManager,
)

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def fresh(tmp: Path, sub: str) -> ProfileManager:
    return ProfileManager(data_dir=tmp / sub / "data", config_path=tmp / sub / "config")


def _fake_devices(*devices: ApoDevice):
    """Context-manager-free monkeypatch, restored by the caller in finally --
    same pattern selftest_apo_coverage.py uses for audio_devices functions."""
    real = audio_devices.list_apo_devices
    audio_devices.list_apo_devices = lambda: list(devices)
    return real


GUID_A = "{AAAAAAAA-0000-0000-0000-000000000001}"
GUID_B = "{BBBBBBBB-0000-0000-0000-000000000002}"
GUID_MIC = "{DDDDDDDD-0000-0000-0000-000000000004}"
DEVICE_A = ApoDevice(guid=GUID_A, device_name="Speakers", connection_name="Creative BT-W6", flow="Render", state=1)
DEVICE_B = ApoDevice(guid=GUID_B, device_name="Speakers", connection_name="FIIO K13 R2R", flow="Render", state=1)
DEVICE_MIC = ApoDevice(guid=GUID_MIC, device_name="Microphone", connection_name="USB Mic", flow="Capture", state=1)


def test_zero_assignments_byte_identical(tmp: Path) -> None:
    print("[1] zero assignments -> byte-identical to the pre-D1a single-profile render")
    m = fresh(tmp, "zero")
    m.ensure_first_run()
    meta = m.create_profile("Daily", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta.id)
    pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
    check(pointer_text == apo_config.render_include(meta.file),
          "no render layers, no assignments -> plain single Include:, no Device: line")
    check("Device:" not in pointer_text, "no Device: scoping at all when unassigned")

    # Now activate a real render layer (listener correction) with assignments
    # still empty -- must match the OLD composed-fallback shape exactly
    # (LISTENER_ACTIVE_FILENAME, same header).
    listener_slots.save_slot_result(
        m.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}}, {"left": {}, "right": {}})
    m.invalidate_listener_correction_cache()
    meta_content = m.create_from_apo_text("Daily EQ", "Channel: all\nGraphicEQ: 1000 0.0\n")
    m.activate_profile(meta_content.id)
    pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
    check(pointer_text == apo_config.render_include(f"{RUMBLE_SUBDIR}/{LISTENER_ACTIVE_FILENAME}"),
          "composed fallback (layers active, no assignments) -> same Include target as before D1a")
    scratch_text = (m.rumble_dir / LISTENER_ACTIVE_FILENAME).read_text(encoding="utf-8")
    check(scratch_text.startswith("# RUMBLE listener-corrected scratch file"),
          "composed scratch keeps its original header text")
    check("Channel: L" in scratch_text and "4.0" in scratch_text,
          "composed scratch actually carries the listener correction")
    check("Device:" not in pointer_text, "still no Device: scoping with zero assignments")


def test_dispatch_structure_and_order(tmp: Path) -> None:
    print("[2] N assignments -> Device: blocks before the Device: all fallback")
    m = fresh(tmp, "dispatch")
    m.ensure_first_run()
    meta_a = m.create_profile("Profile A", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_b = m.create_profile("Profile B", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)

    real_list = _fake_devices(DEVICE_A, DEVICE_B)
    try:
        m.settings["device_profile_assignments"] = {GUID_A: meta_a.id, GUID_B: meta_b.id}
        m.save()
        m.activate_profile(meta_global.id)  # re-render with assignments now set
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        directive_lines = [l for l in pointer_text.splitlines() if l.startswith("Device:")]
        directives = "\n".join(directive_lines)

        idx_a = directives.find(f"Device: {DEVICE_A.pattern}")
        idx_b = directives.find(f"Device: {DEVICE_B.pattern}")
        idx_all = directives.find("Device: all")
        check(idx_a != -1 and idx_b != -1 and idx_all != -1, "all three Device: blocks present")
        check(idx_a < idx_all and idx_b < idx_all,
              "assignment blocks render before the Device: all fallback (KB Sec 20.3 ordering)")
        check(f"Include: {meta_a.file}" in pointer_text, "device A includes its assigned profile directly (no active layers)")
        check(f"Include: {meta_b.file}" in pointer_text, "device B includes its assigned profile directly")
        check(f"Include: {meta_global.file}" in pointer_text, "fallback includes the global active profile directly")

        # round-trip through real parsing/matching functions: each block's
        # own Device: pattern selects only its intended endpoint.
        device_a_line = next(l for l in pointer_text.splitlines() if l.startswith(f"Device: {DEVICE_A.pattern}"))
        pattern_a = device_a_line.split("Device:", 1)[1].strip()
        check(audio_devices.pattern_matches_any(pattern_a, [DEVICE_A])
              and not audio_devices.pattern_matches_any(pattern_a, [DEVICE_B]),
              "device A's own pattern matches only device A, never device B")
    finally:
        audio_devices.list_apo_devices = real_list


def test_missing_profile_assignment_falls_back(tmp: Path) -> None:
    print("[3] assignment to a missing profile falls back gracefully")
    m = fresh(tmp, "missing")
    m.ensure_first_run()
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig())
    m.activate_profile(meta_global.id)

    real_list = _fake_devices(DEVICE_A)
    try:
        # Only an assignment to a nonexistent profile id -> equivalent to no
        # valid assignments at all -> plain single-Include fast path.
        m.settings["device_profile_assignments"] = {GUID_A: "does-not-exist"}
        m.save()
        m.activate_profile(meta_global.id)
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check("Device:" not in pointer_text,
              "an assignment to a nonexistent profile is dropped entirely, not rendered broken")
        check(pointer_text == apo_config.render_include(meta_global.file),
              "falls back to the exact zero-assignment render")

        # Mixed: one valid + one invalid -> exactly one Device: block, plus fallback.
        meta_a = m.create_profile("A", config=apo_config.ProfileConfig())
        m.settings["device_profile_assignments"] = {GUID_A: meta_a.id, "{unknown-guid}": "still-missing"}
        m.save()
        m.activate_profile(meta_global.id)
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        directive_count = sum(1 for l in pointer_text.splitlines() if l.startswith("Device:"))
        check(directive_count == 2,
              "exactly one assignment block + the fallback block (invalid entry dropped)")
        check(f"Include: {meta_a.file}" in pointer_text and "Device: all" in pointer_text,
              "the valid assignment and the fallback both render")
    finally:
        audio_devices.list_apo_devices = real_list


def test_layers_compose_per_branch(tmp: Path) -> None:
    print("[4] render layers compose onto EVERY dispatched branch")
    m = fresh(tmp, "layers")
    m.ensure_first_run()
    meta_a = m.create_from_apo_text("A", "Channel: all\nGraphicEQ: 1000 0.0\n")
    meta_b = m.create_from_apo_text("B", "Channel: all\nGraphicEQ: 1000 0.0\n")
    meta_global = m.create_from_apo_text("Global", "Channel: all\nGraphicEQ: 1000 0.0\n")
    m.activate_profile(meta_global.id)

    listener_slots.save_slot_result(
        m.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}}, {"left": {}, "right": {}})
    m.invalidate_listener_correction_cache()

    real_list = _fake_devices(DEVICE_A, DEVICE_B)
    try:
        m.settings["device_profile_assignments"] = {GUID_A: meta_a.id, GUID_B: meta_b.id}
        m.save()
        m.activate_profile(meta_global.id)

        scratch_a = m.rumble_dir / m._device_scratch_filename(GUID_A)
        scratch_b = m.rumble_dir / m._device_scratch_filename(GUID_B)
        fallback_scratch = m.rumble_dir / LISTENER_ACTIVE_FILENAME
        check(scratch_a.exists() and scratch_b.exists() and fallback_scratch.exists(),
              "every branch (2 assignments + fallback) got its own composed scratch file")
        for path, label in ((scratch_a, "device A"), (scratch_b, "device B"), (fallback_scratch, "fallback")):
            text = path.read_text(encoding="utf-8")
            check("Channel: L" in text and "4.0" in text,
                  f"listener correction composed onto the {label} branch")

        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check(f"Include: {RUMBLE_SUBDIR}/{scratch_a.name}" in pointer_text
              and f"Include: {RUMBLE_SUBDIR}/{scratch_b.name}" in pointer_text,
              "dispatch points each assignment at its own composed scratch, not the raw profile file")
    finally:
        audio_devices.list_apo_devices = real_list


def test_stale_scratch_cleanup(tmp: Path) -> None:
    print("[5] stale rumble_device_*.txt scratch files are cleaned on render")
    m = fresh(tmp, "cleanup")
    m.ensure_first_run()
    meta_a = m.create_profile("A", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)
    listener_slots.save_slot_result(
        m.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}}, {"left": {}, "right": {}})
    m.invalidate_listener_correction_cache()

    real_list = _fake_devices(DEVICE_A)
    try:
        m.settings["device_profile_assignments"] = {GUID_A: meta_a.id}
        m.save()
        m.activate_profile(meta_global.id)
        scratch_a = m.rumble_dir / m._device_scratch_filename(GUID_A)
        check(scratch_a.exists(), "scratch created while the assignment is active")

        # Remove the assignment -> the scratch is now stale and must be swept.
        m.settings["device_profile_assignments"] = {}
        m.save()
        m.activate_profile(meta_global.id)
        check(not scratch_a.exists(), "stale per-device scratch removed once its assignment is gone")

        remaining = list(m.rumble_dir.glob(f"{DEVICE_SCRATCH_PREFIX}*.txt"))
        check(remaining == [], "no orphaned rumble_device_*.txt files left behind")
    finally:
        audio_devices.list_apo_devices = real_list


def test_scratch_never_scanned_as_a_profile(tmp: Path) -> None:
    print("[6] rumble_device_*.txt / rumble_listener_active.txt never scanned as user profiles")
    m = fresh(tmp, "scanguard")
    m.ensure_first_run()
    meta_a = m.create_profile("A", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)
    listener_slots.save_slot_result(
        m.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}}, {"left": {}, "right": {}})
    m.invalidate_listener_correction_cache()

    real_list = _fake_devices(DEVICE_A)
    try:
        m.settings["device_profile_assignments"] = {GUID_A: meta_a.id}
        m.save()
        m.activate_profile(meta_global.id)
        before_ids = {p.id for p in m.list_profiles()}
        m.refresh_profiles()
        after_ids = {p.id for p in m.list_profiles()}
        check(before_ids == after_ids, "rescanning profile folders doesn't absorb scratch files as new profiles")
    finally:
        audio_devices.list_apo_devices = real_list


def test_ownership_drift_unaffected(tmp: Path) -> None:
    print("[7] config.txt ownership/drift detection unaffected by an active dispatch")
    m = fresh(tmp, "ownership")
    m.ensure_first_run()
    meta_a = m.create_profile("A", config=apo_config.ProfileConfig())
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig())
    m.activate_profile(meta_global.id)

    real_list = _fake_devices(DEVICE_A)
    try:
        m.settings["device_profile_assignments"] = {GUID_A: meta_a.id}
        m.save()
        m.activate_profile(meta_global.id)
        cfg_txt = (m.config_path / "config.txt").read_text(encoding="utf-8")
        check(MANAGED_MARKER in cfg_txt and f"Include: {ACTIVE_POINTER_FILENAME}" in cfg_txt,
              "config.txt is unchanged by dispatch -- still marker + single active-pointer include")
        actions = m.ensure_first_run()
        check(not actions["owned_config"] and not actions["reasserted"],
              "an active multi-device dispatch never trips a false drift re-assert")
    finally:
        audio_devices.list_apo_devices = real_list


def test_bypass_and_preview_override_whole_dispatch(tmp: Path) -> None:
    print("[8] bypass and preview still override the entire dispatch, not per-branch")
    m = fresh(tmp, "override")
    m.ensure_first_run()
    meta_a = m.create_profile("A", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)

    real_list = _fake_devices(DEVICE_A)
    try:
        m.settings["device_profile_assignments"] = {GUID_A: meta_a.id}
        m.save()
        m.activate_profile(meta_global.id)
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check("Device:" in pointer_text, "sanity: dispatch is active before overriding it")

        m.set_bypass(True)
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check(pointer_text == apo_config.render_bypass(), "bypass replaces the whole dispatch, no Device: blocks")
        check(list(m.rumble_dir.glob(f"{DEVICE_SCRATCH_PREFIX}*.txt")) == [],
              "bypass sweeps stale per-device scratch too")
        m.set_bypass(False)

        preview_cfg = apo_config.ProfileConfig(channel_sections=[apo_config.ChannelSection(channel="all")])
        assert m.preview_config(preview_cfg) is True
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check("Device:" not in pointer_text and "rumble_preview.txt" in pointer_text,
              "preview stays a full unscoped override, not scoped per device")
        m.end_preview()
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check("Device:" in pointer_text, "ending preview restores the dispatch")
    finally:
        audio_devices.list_apo_devices = real_list


def test_abx_trial_overrides_whole_dispatch(tmp: Path) -> None:
    """D1c: an ABX trial mid-assignments must behave exactly like any other
    preview -- ABXRunner.present()/finish() go through
    ProfileManager.preview_config()/end_preview() (see abx.py's own
    module docstring), the same choke point S19/S20's own preview test above
    already covers, so this reuses selftest_abx.py's own `_make_manager()`
    fixture SHAPE (temp data_dir AND config_path, a real activated profile)
    rather than rewriting anything in that file, per the S21 spec."""
    print("[9] an ABX trial mid-assignments overrides the whole dispatch, restores on finish")
    m = fresh(tmp, "abx_dispatch")
    m.ensure_first_run()
    meta_a = m.create_profile("A", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)

    real_list = _fake_devices(DEVICE_A)
    try:
        m.settings["device_profile_assignments"] = {GUID_A: meta_a.id}
        m.save()
        m.activate_profile(meta_global.id)
        dispatch_pointer = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check("Device:" in dispatch_pointer, "sanity: dispatch is active before the ABX trial")

        config_x = apo_config.parse_config(
            "Preamp: -2 dB\nFilter 1: ON PK Fc 1000.0 Hz Gain 3 dB Q 1.00\n")
        config_y = apo_config.parse_config(
            "Preamp: -2 dB\nFilter 1: ON PK Fc 1000.0 Hz Gain -3 dB Q 1.00\n")
        runner = abx.ABXRunner(m, [config_x, config_y], 48000)
        try:
            runner.present(0)
            pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
            check("Device:" not in pointer_text and "rumble_preview.txt" in pointer_text,
                  "ABX trial replaces the ENTIRE dispatch, not just the assigned device's branch")
            check(m.is_previewing, "manager reports a live preview during the trial")

            runner.present(1)
            pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
            check("Device:" not in pointer_text,
                  "switching ABX candidates mid-trial still has no Device: scoping")
        finally:
            runner.finish()

        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check(pointer_text == dispatch_pointer,
              "finishing the ABX trial restores the dispatch VERBATIM (byte-identical)")
        check(not m.is_previewing, "preview flag cleared once the trial ends")
    finally:
        audio_devices.list_apo_devices = real_list


def test_capture_assignment_emits_stage_capture(tmp: Path) -> None:
    print("[10] D2: a Capture-flow assignment carries a leading Stage: capture line, a Render one doesn't")
    m = fresh(tmp, "capture_stage")
    m.ensure_first_run()
    meta_render = m.create_profile("Speakers", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_mic = m.create_profile("Voice", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)

    real_list = _fake_devices(DEVICE_A, DEVICE_MIC)
    try:
        # Capture branches only render while microphone profiles are shown --
        # see test_hidden_microphone_profiles_suppress_capture_branch below.
        m.settings["show_microphone_profiles"] = True
        m.settings["device_profile_assignments"] = {GUID_A: meta_render.id, GUID_MIC: meta_mic.id}
        m.save()
        m.activate_profile(meta_global.id)
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")

        check(f"Stage: capture\nDevice: {DEVICE_MIC.pattern}\n" in pointer_text,
              "the mic's block has Stage: capture immediately before its Device: line")
        check(f"Include: {meta_mic.file}" in pointer_text, "mic block includes its assigned profile")

        render_block_start = pointer_text.find(f"Device: {DEVICE_A.pattern}")
        check(render_block_start != -1, "render device's block is present")
        preceding_render = pointer_text[:render_block_start].rstrip("\n")
        check(not preceding_render.endswith("Stage: capture"),
              "the render device's block carries no Stage: scoping")

        fallback_start = pointer_text.find("Device: all")
        check(fallback_start != -1, "fallback block is present")
        preceding_fallback = pointer_text[:fallback_start].rstrip("\n")
        check(not preceding_fallback.endswith("Stage: capture"),
              "the trailing Device: all fallback carries no Stage: scoping either")

        idx_render = pointer_text.find(f"Device: {DEVICE_A.pattern}")
        idx_mic = pointer_text.find("Stage: capture")
        idx_all = pointer_text.find("Device: all")
        check(idx_render < idx_all and idx_mic < idx_all,
              "both assignment blocks (render and capture) still render before the fallback")
    finally:
        audio_devices.list_apo_devices = real_list


def test_capture_layers_compose_and_stay_stage_scoped(tmp: Path) -> None:
    print("[11] D2: render layers compose onto a capture branch too, still Stage: capture scoped")
    m = fresh(tmp, "capture_layers")
    m.ensure_first_run()
    meta_mic = m.create_from_apo_text("Voice", "Channel: all\nGraphicEQ: 1000 0.0\n")
    meta_global = m.create_from_apo_text("Global", "Channel: all\nGraphicEQ: 1000 0.0\n")
    m.activate_profile(meta_global.id)

    listener_slots.save_slot_result(
        m.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}}, {"left": {}, "right": {}})
    m.invalidate_listener_correction_cache()

    real_list = _fake_devices(DEVICE_MIC)
    try:
        m.settings["show_microphone_profiles"] = True
        m.settings["device_profile_assignments"] = {GUID_MIC: meta_mic.id}
        m.save()
        m.activate_profile(meta_global.id)

        scratch_mic = m.rumble_dir / m._device_scratch_filename(GUID_MIC)
        check(scratch_mic.exists(), "capture branch got its own composed scratch file")
        text = scratch_mic.read_text(encoding="utf-8")
        check("Channel: L" in text and "4.0" in text,
              "listener correction composed onto the capture branch")

        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check(f"Stage: capture\nDevice: {DEVICE_MIC.pattern}\n"
              f"Include: {RUMBLE_SUBDIR}/{scratch_mic.name}" in pointer_text,
              "the capture branch's Stage:+Device:+Include: block points at its own composed scratch")
    finally:
        audio_devices.list_apo_devices = real_list


def test_unresolved_device_assignment_has_no_stage_line(tmp: Path) -> None:
    print("[12] D2: an assignment whose endpoint can't be resolved gets no Stage: line (flow unknown)")
    m = fresh(tmp, "capture_unresolved")
    m.ensure_first_run()
    meta_a = m.create_profile("A", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)

    real_list = _fake_devices()  # nothing currently resolvable/hooked
    try:
        m.settings["device_profile_assignments"] = {GUID_MIC: meta_a.id}
        m.save()
        m.activate_profile(meta_global.id)
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        directive_lines = [l for l in pointer_text.splitlines() if not l.startswith("#")]
        check(not any(l.strip() == "Stage: capture" for l in directive_lines),
              "an unresolved endpoint's flow is unknown, so no Stage: directive line is added "
              "(the module docstring/header comment mentions the phrase, directive lines don't)")
        check(f"Device: {GUID_MIC}" in pointer_text,
              "falls back to the bare GUID as the Device: pattern, same as pre-D2")
    finally:
        audio_devices.list_apo_devices = real_list


def test_hidden_microphone_profiles_suppress_capture_branch(tmp: Path) -> None:
    print("[13] Hiding microphone profiles DISABLES mic EQ: the capture branch leaves the dispatch, "
          "the render branch and the saved assignment both survive")
    m = fresh(tmp, "capture_hidden")
    m.ensure_first_run()
    meta_render = m.create_profile("Speakers", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_mic = m.create_profile("Voice", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)

    real_list = _fake_devices(DEVICE_A, DEVICE_MIC)
    try:
        m.settings["show_microphone_profiles"] = False
        m.settings["device_profile_assignments"] = {GUID_A: meta_render.id, GUID_MIC: meta_mic.id}
        m.save()
        m.activate_profile(meta_global.id)
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")

        directive_lines = [l for l in pointer_text.splitlines() if not l.startswith("#")]
        check(not any(l.strip() == "Stage: capture" for l in directive_lines),
              "no capture branch is dispatched at all while microphone profiles are hidden")
        check(f"Device: {DEVICE_MIC.pattern}" not in pointer_text,
              "the hidden microphone gets no Device: block, so APO applies nothing to it")
        check(f"Device: {DEVICE_A.pattern}" in pointer_text
              and f"Include: {meta_render.file}" in pointer_text,
              "the playback assignment is untouched by the microphone setting")
        check(not (m.rumble_dir / m._device_scratch_filename(GUID_MIC)).exists(),
              "the suppressed capture branch leaves no composed scratch file behind")

        check(m.settings["device_profile_assignments"].get(GUID_MIC) == meta_mic.id,
              "the mic's assignment is REMEMBERED, not cleared -- hiding disables, it never destroys")

        # ...and showing them again brings the mic's EQ straight back.
        m.settings["show_microphone_profiles"] = True
        m.save()
        m.activate_profile(meta_global.id)
        pointer_text = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check(f"Stage: capture\nDevice: {DEVICE_MIC.pattern}\n" in pointer_text,
              "re-showing microphone profiles restores the capture branch exactly as it was")
    finally:
        audio_devices.list_apo_devices = real_list


def test_hidden_microphone_profiles_leave_playback_dispatch_byte_identical(tmp: Path) -> None:
    print("[14] A mic-only assignment suppressed by hiding collapses to the plain single-profile "
          "pointer, byte-identical to having no assignments at all")
    m = fresh(tmp, "capture_hidden_only")
    m.ensure_first_run()
    meta_mic = m.create_profile("Voice", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))

    real_list = _fake_devices(DEVICE_MIC)
    try:
        m.settings["show_microphone_profiles"] = False
        m.settings["device_profile_assignments"] = {}
        m.save()
        m.activate_profile(meta_global.id)
        baseline = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")

        m.settings["device_profile_assignments"] = {GUID_MIC: meta_mic.id}
        m.save()
        m.activate_profile(meta_global.id)
        suppressed = (m.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")

        check(suppressed == baseline,
              "suppressing the only assignment empties the dict, so the render takes the same "
              "no-assignments fast path rather than emitting a dispatch with just a fallback block")
    finally:
        audio_devices.list_apo_devices = real_list


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_device_dispatch_"))
    print(f"workdir: {tmp}\n")
    test_zero_assignments_byte_identical(tmp)
    test_dispatch_structure_and_order(tmp)
    test_missing_profile_assignment_falls_back(tmp)
    test_layers_compose_per_branch(tmp)
    test_stale_scratch_cleanup(tmp)
    test_scratch_never_scanned_as_a_profile(tmp)
    test_ownership_drift_unaffected(tmp)
    test_bypass_and_preview_override_whole_dispatch(tmp)
    test_abx_trial_overrides_whole_dispatch(tmp)
    test_capture_assignment_emits_stage_capture(tmp)
    test_capture_layers_compose_and_stay_stage_scoped(tmp)
    test_unresolved_device_assignment_has_no_stage_line(tmp)
    test_hidden_microphone_profiles_suppress_capture_branch(tmp)
    test_hidden_microphone_profiles_leave_playback_dispatch_byte_identical(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL DEVICE DISPATCH TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
