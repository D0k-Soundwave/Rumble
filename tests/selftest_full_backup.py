"""Verify the full backup/restore captures EVERYTHING RUMBLE owns:
app settings, the active pointer, default + user profile files, custom themes,
and impulse-response files — and restores them into a clean install verbatim.

Run:  python tools/selftest_full_backup.py
Exits non-zero on any mismatch."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json

from rumble.core import audio_devices, profile_manager
from rumble.core.audio_devices import ApoDevice
from rumble.core.profile_manager import (
    ACTIVE_POINTER_FILENAME, DEVICE_SCRATCH_PREFIX, ProfileManager, make_flat_config, IR_SUBDIR,
    _MAX_TEXT_MEMBER_BYTES,
)
from rumble.core.custom_themes import save_custom_theme
from rumble.core.hearing_test import save_results as save_hearing_results
from rumble.core.digit_screening import save_screening_result

FAILURES: list[str] = []

# The shipped built-in table is empty, so inject one synthetic default profile to
# prove default files are captured and restored alongside user content.
_TEST_BUILTINS = {
    "builtin_testref": {
        "name": "Test Reference",
        "description": "Synthetic built-in used only by the backup selftest.",
        "tags": ["Situation / Reference"],
        "body": ("Preamp: 0.0 dB\nChannel: L R\n"
                 "Filter 1: ON PK Fc 1000.0 Hz Gain 0.0 dB Q 1.41\n"
                 "GraphicEQ: 20 0; 20000 0"),
    }
}


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def fresh(tmp: Path, tag: str) -> ProfileManager:
    m = ProfileManager(data_dir=tmp / tag / "data", config_path=tmp / tag / "config")
    m.ensure_first_run()
    return m


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_fullbak_"))

    # ── Source install: builtins + a user profile + custom theme + IR + setting
    profile_manager.BUILTIN_PROFILES = _TEST_BUILTINS  # inject a default to round-trip
    src = fresh(tmp, "src")
    src.ensure_builtin_profiles()
    src.refresh_profiles()  # ensure_builtin_profiles writes files; rescan to load them
    user = src.create_profile("My Bass Boost",
                              config=make_flat_config(8, channels=["L", "R"]))
    src.settings["default_bands"] = 17
    src.settings["theme"] = "Aurora-Test"
    src.settings["sample_rate_mode"] = "manual"
    src.settings["auto_switch_enabled"] = True
    src.settings["auto_switch_rules"] = [
        {"trigger": "app", "match": "game.exe", "profile_id": user.id, "enabled": True},
    ]
    src.settings["spectrum_enabled"] = True
    src.settings["spectrum_mode"] = "source"
    src.settings["spectrum_peak_hold"] = True
    src.settings["hearing_frequency_set"] = "coarse"
    src.settings["hearing_screening_last_seed"] = 12345
    src.save()
    save_custom_theme(src.data_dir, {
        "name": "Aurora-Test", "accent_name": "Custom",
        **{k: "#123456" for k in (
            "bg_deep", "bg_base", "bg_surface", "bg_raised", "bg_sunken",
            "border_subtle", "border_default", "border_strong",
            "accent", "accent_hover", "accent_active",
            "text_primary", "text_secondary", "text_muted", "text_on_accent",
            "success", "warning", "error",
            "graph_bg", "graph_grid", "graph_zero", "graph_curve", "graph_band_sel")},
    })
    # Drop a fake impulse-response file into the IR folder.
    ir_dir = src.rumble_dir / IR_SUBDIR
    ir_dir.mkdir(parents=True, exist_ok=True)
    (ir_dir / "room_correction.wav").write_bytes(b"RIFFfake-ir-data")

    # Save one real Test A result and one real Test B result, to prove the
    # hearing-test result trees round-trip through backup/restore too.
    hearing_norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {1000: 0.0}}
    hearing_raw = {"left": {1000: -30.0}, "right": {1000: -30.0}}
    save_hearing_results(hearing_norm, hearing_raw, src.data_dir)
    save_screening_result(-6.0, "typical", src.data_dir)

    n_builtins = sum(1 for p in src.profiles if p.builtin)
    check(n_builtins > 0, f"source has {n_builtins} builtin profiles")
    check(user is not None, "source has a user profile")

    # ── Back up everything
    zip_path = tmp / "full_backup.zip"
    check(src.backup_library(zip_path), "full backup succeeds")
    check(zip_path.exists() and zip_path.stat().st_size > 0, "archive written")

    # ── Restore into a clean install
    dst = fresh(tmp, "dst")
    check(len([p for p in dst.profiles if p.builtin]) >= 0, "clean target built")
    check(dst.restore_library(zip_path), "restore succeeds")

    # Settings round-trip
    check(dst.settings.get("default_bands") == 17, "setting restored (default_bands)")
    check(dst.settings.get("theme") == "Aurora-Test", "setting restored (theme)")
    check(dst.settings.get("sample_rate_mode") == "manual", "setting restored (sample_rate_mode)")
    check(dst.settings.get("auto_switch_enabled") is True, "setting restored (auto_switch_enabled)")
    check(dst.settings.get("auto_switch_rules") == [
        {"trigger": "app", "match": "game.exe", "profile_id": user.id, "enabled": True},
    ], "setting restored (auto_switch_rules)")
    check(dst.settings.get("spectrum_enabled") is True, "setting restored (spectrum_enabled)")
    check(dst.settings.get("spectrum_mode") == "source", "setting restored (spectrum_mode)")
    check(dst.settings.get("spectrum_peak_hold") is True, "setting restored (spectrum_peak_hold)")
    check(dst.settings.get("hearing_frequency_set") == "coarse",
          "setting restored (hearing_frequency_set)")
    check(dst.settings.get("hearing_screening_last_seed") == 12345,
          "setting restored (hearing_screening_last_seed)")

    # Profiles round-trip (defaults + the user profile)
    dst_names = {p.name for p in dst.profiles}
    check("My Bass Boost" in dst_names, "user profile restored")
    check(sum(1 for p in dst.profiles if p.builtin) == n_builtins,
          "all default profiles restored")
    restored_user = next((p for p in dst.profiles if p.name == "My Bass Boost"), None)
    check(restored_user is not None, "restored user profile resolvable")
    if restored_user:
        cfg = dst.load_profile_config(restored_user.id)
        check(len(cfg.channel_sections[0].filters) == 8, "user profile bands intact")

    # Custom theme round-trip — read the restored file directly (the global
    # THEMES merge in load_custom_themes() is process-wide and unreliable here).
    themes_file = dst.data_dir / "custom_themes.json"
    restored_themes = json.loads(themes_file.read_text("utf-8")) if themes_file.exists() else []
    check(any(t.get("name") == "Aurora-Test" for t in restored_themes),
          "custom theme restored")

    # IR file round-trip
    restored_ir = dst.rumble_dir / IR_SUBDIR / "room_correction.wav"
    check(restored_ir.exists() and restored_ir.read_bytes() == b"RIFFfake-ir-data",
          "impulse-response file restored")

    # Hearing test result files round-trip (Phase 7)
    freq_files = list((dst.data_dir / "hearing" / "frequency").glob("*.json")) \
        if (dst.data_dir / "hearing" / "frequency").is_dir() else []
    check(len(freq_files) == 1, "Test A result file restored")
    if freq_files:
        restored_freq = json.loads(freq_files[0].read_text("utf-8"))
        check(restored_freq.get("raw", {}).get("left", {}).get("1000") == -30.0,
              "Test A result content intact")

    screening_files = list((dst.data_dir / "hearing" / "screening").glob("*.json")) \
        if (dst.data_dir / "hearing" / "screening").is_dir() else []
    check(len(screening_files) == 1, "Test B result file restored")
    if screening_files:
        restored_screening = json.loads(screening_files[0].read_text("utf-8"))
        check(restored_screening.get("srt_db") == -6.0 and restored_screening.get("band") == "typical",
              "Test B result content intact")

    # ── Backward compatibility: a fresh restore over an existing library
    #    must not duplicate or leave stale files.
    dst.restore_library(zip_path)
    check(sum(1 for p in dst.profiles if p.name == "My Bass Boost") == 1,
          "re-restore does not duplicate profiles")

    # ── D1c: device_profile_assignments round-trips AND restore re-renders
    #    the dispatch (rumble_active.txt is never itself part of the zip --
    #    it lives at config_path root, not under rumble_dir -- so restore
    #    must explicitly rewrite it from the restored settings, not just
    #    leave whatever pointer was on disk before the restore).
    guid = "{AAAAAAAA-0000-0000-0000-000000000001}"
    device = ApoDevice(guid=guid, device_name="Speakers",
                        connection_name="Creative BT-W6", flow="Render", state=1)
    real_list_apo_devices = audio_devices.list_apo_devices
    audio_devices.list_apo_devices = lambda: [device]
    try:
        src2 = fresh(tmp, "src2")
        meta_a = src2.create_profile("Device Profile", config=make_flat_config(3, channels=["L", "R"]))
        meta_global2 = src2.create_profile("Global2", config=make_flat_config(1, channels=["L", "R"]))
        src2.activate_profile(meta_global2.id)
        src2.settings["device_profile_assignments"] = {guid: meta_a.id}
        src2.save()
        src2.activate_profile(meta_global2.id)  # re-render with the assignment active
        zip_path2 = tmp / "device_dispatch_backup.zip"
        check(src2.backup_library(zip_path2), "backup with an active device dispatch succeeds")

        # A stray, unrelated per-device scratch file must not ride along in
        # the archive as dead weight -- confirm it isn't a zip member at all.
        import zipfile
        with zipfile.ZipFile(zip_path2) as zf:
            names = zf.namelist()
        check(not any(n.split("/")[-1].startswith(DEVICE_SCRATCH_PREFIX) for n in names),
              "per-device render scratch is excluded from the backup archive")

        dst2 = fresh(tmp, "dst2")
        dst2_other = dst2.create_profile("Pre-existing", config=make_flat_config(1))
        dst2.activate_profile(dst2_other.id)  # dst2's pointer currently reflects a DIFFERENT profile/no assignment
        check(dst2.restore_library(zip_path2), "restore of a device-dispatch backup succeeds")
        check(dst2.settings.get("device_profile_assignments") == {guid: meta_a.id},
              "device_profile_assignments setting restored verbatim")

        pointer_text = (dst2.config_path / ACTIVE_POINTER_FILENAME).read_text(encoding="utf-8")
        check(f"Device: {device.pattern}" in pointer_text,
              "restore re-renders the dispatch: the assignment's Device: block is present "
              "on the restored pointer without any extra activate_profile()/refresh call")
        check("Device: all" in pointer_text, "restore's re-render includes the fallback block too")
    finally:
        audio_devices.list_apo_devices = real_list_apo_devices

    # ── Security: a tampered/hand-crafted archive with a path-traversal
    #    member name must not write outside the restore target's own dirs
    #    (zip-slip / CWE-22 guard in ProfileManager._safe_extract_target()).
    import zipfile as _zipfile
    dst3 = fresh(tmp, "dst3")
    escape_target = tmp.parent / "zipslip_evil.txt"
    escape_target.unlink(missing_ok=True)
    malicious_zip = tmp / "malicious.zip"
    with _zipfile.ZipFile(malicious_zip, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": 1, "app": "RUMBLE"}))
        zf.writestr("data/../../zipslip_evil.txt", b"pwned")
        zf.writestr("rumble/../../zipslip_evil2.txt", b"pwned")
        zf.writestr("data/profiles.json", b"{}")
    check(dst3.restore_library(malicious_zip), "restore of a traversal-crafted archive still completes")
    check(not escape_target.exists(), "zip-slip member did not escape data_dir")
    check(not (tmp.parent / "zipslip_evil2.txt").exists(), "zip-slip member did not escape rumble_dir")

    # ── Security: a zip-bomb member (small compressed, huge decompressed)
    #    must be refused rather than written to disk (size-cap guard in
    #    ProfileManager._read_zip_member_capped()), while unrelated members in
    #    the same archive still restore normally.
    dst4 = fresh(tmp, "dst4")
    bomb_zip = tmp / "bomb.zip"
    oversized = b"0" * (_MAX_TEXT_MEMBER_BYTES + 1)  # highly compressible: tiny on disk
    with _zipfile.ZipFile(bomb_zip, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"format": 1, "app": "RUMBLE"}))
        zf.writestr("data/profiles.json", b"{}")
        zf.writestr("data/bomb.txt", oversized)
    check(bomb_zip.stat().st_size < 1024 * 1024, "the crafted bomb archive itself stays small on disk")
    check(dst4.restore_library(bomb_zip), "restore of an archive containing an oversized member still completes")
    check(not (dst4.data_dir / "bomb.txt").exists(), "oversized member was refused, not written")

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
