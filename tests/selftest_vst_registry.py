"""Self-test for VST plugin library aggregation, relink, remove and
discovery (core only, no Qt).

Run:  python tests/selftest_vst_registry.py
Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core.apo_config import VSTPlugin
from rumble.core.profile_manager import ProfileManager
from rumble.core.vst_registry import (
    aggregate_vst_usage, apply_relink, apply_remove, scan_common_vst_folders,
)

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def fresh_manager(tmp: Path, sub: str) -> ProfileManager:
    return ProfileManager(data_dir=tmp / sub / "data", config_path=tmp / sub / "config")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_"))
    print(f"workdir: {tmp}\n")

    # ── 1. aggregate_vst_usage ────────────────────────────────────────
    print("[1] aggregate_vst_usage")
    real_dll = tmp / "Real.dll"
    real_dll.write_text("stand-in dll")
    missing_dll = str(tmp / "Missing.dll")

    from rumble.core.apo_config import ProfileConfig

    cfg_a = ProfileConfig()
    cfg_a.vst_plugins = [VSTPlugin(path=str(real_dll).upper(), chunks="AAA")]
    cfg_b = ProfileConfig()
    cfg_b.vst_plugins = [
        VSTPlugin(path=str(real_dll).lower(), chunks="BBB"),
        VSTPlugin(path=missing_dll, chunks=""),
    ]
    cfg_c = ProfileConfig()  # no VST plugins at all
    cfg_c.vst_plugins = [VSTPlugin(path="", chunks="")]  # blank path, ignored

    report = aggregate_vst_usage([
        ("p_a", "Profile A", cfg_a),
        ("p_b", "Profile B", cfg_b),
        ("p_c", "Profile C", cfg_c),
    ])
    check(len(report.entries) == 2, "two distinct plugins aggregated")
    real_entry = next((e for e in report.entries if e.exists), None)
    missing_entry = next((e for e in report.entries if not e.exists), None)
    check(real_entry is not None, "real path marked existing")
    check(missing_entry is not None, "missing path marked missing")
    check(real_entry is not None and set(real_entry.profile_ids) == {"p_a", "p_b"},
          "case-differing paths collapse into one usage, both profiles attached")
    check(missing_entry is not None and missing_entry.profile_ids == ["p_b"],
          "missing path attributed to the right profile")
    check(report.profile_count == 2, "profile_count counts distinct profiles, not entries")

    # ── 2. apply_relink ────────────────────────────────────────────────
    print("\n[2] apply_relink")
    cfg = ProfileConfig()
    cfg.vst_plugins = [
        VSTPlugin(path=str(real_dll).upper(), chunks="keep-me"),
        VSTPlugin(path=missing_dll, chunks=""),
    ]
    new_path = str(tmp / "Replacement.dll")
    changed = apply_relink(cfg, str(real_dll), new_path)
    check(changed is True, "apply_relink reports a change when a path matches")
    check(cfg.vst_plugins[0].path == new_path, "matching entry's path replaced")
    check(cfg.vst_plugins[0].chunks == "keep-me", "chunks left untouched by relink")
    check(cfg.vst_plugins[1].path == missing_dll, "non-matching entry untouched")
    no_change = apply_relink(cfg, "C:\\nothing\\matches.dll", new_path)
    check(no_change is False, "apply_relink reports no change when nothing matches")

    # ── 2b. apply_remove ─────────────────────────────────────────────────
    print("\n[2b] apply_remove")
    cfg_r = ProfileConfig()
    cfg_r.vst_plugins = [
        VSTPlugin(path=str(real_dll).upper(), chunks="keep-me"),
        VSTPlugin(path=missing_dll, chunks=""),
    ]
    removed = apply_remove(cfg_r, str(real_dll))
    check(removed is True, "apply_remove reports a change when a path matches")
    check(len(cfg_r.vst_plugins) == 1 and cfg_r.vst_plugins[0].path == missing_dll,
          "only the matching entry is dropped, others untouched")
    not_removed = apply_remove(cfg_r, "C:\\nothing\\matches.dll")
    check(not_removed is False, "apply_remove reports no change when nothing matches")

    # ── 3. scan_common_vst_folders ─────────────────────────────────────
    print("\n[3] scan_common_vst_folders (real environment, best-effort)")
    try:
        found = scan_common_vst_folders()
        check(isinstance(found, list), "scan returns a list without raising")
    except Exception as exc:  # pragma: no cover - the whole point is it never raises
        check(False, f"scan_common_vst_folders raised: {exc}")

    # ── 4. ProfileManager.vst_report / relink_vst_plugin ───────────────
    print("\n[4] ProfileManager integration")
    mgr = fresh_manager(tmp, "mgr")
    meta1 = mgr.create_profile("Profile One")
    meta2 = mgr.create_profile("Profile Two")
    cfg1 = mgr.load_profile_config(meta1.id)
    cfg1.vst_plugins = [VSTPlugin(path=missing_dll, chunks="")]
    mgr.save_profile_config(meta1.id, cfg1)
    cfg2 = mgr.load_profile_config(meta2.id)
    cfg2.vst_plugins = [VSTPlugin(path=missing_dll, chunks="")]
    mgr.save_profile_config(meta2.id, cfg2)

    report = mgr.vst_report()
    check(len(report.entries) == 1, "one distinct plugin shared by two profiles")
    check(set(report.entries[0].profile_ids) == {meta1.id, meta2.id},
          "both profiles attributed to the shared missing path")

    result = mgr.relink_vst_plugin(missing_dll, str(real_dll))
    check(set(result.updated_profile_ids) == {meta1.id, meta2.id},
          "relink updates every profile referencing the old path")
    check(result.skipped_protected_ids == [], "no protected profiles to skip")
    reloaded1 = mgr.load_profile_config(meta1.id)
    check(reloaded1.vst_plugins[0].path == str(real_dll),
          "relinked path persisted to disk for profile one")

    # a locked profile must never be rewritten by a relink
    meta3 = mgr.create_profile("Profile Three")
    cfg3 = mgr.load_profile_config(meta3.id)
    cfg3.vst_plugins = [VSTPlugin(path=missing_dll, chunks="")]
    mgr.save_profile_config(meta3.id, cfg3)
    mgr.set_profile_locked(meta3.id, True)
    result2 = mgr.relink_vst_plugin(missing_dll, str(real_dll))
    check(meta3.id in result2.skipped_protected_ids, "locked profile reported as skipped")
    check(meta3.id not in result2.updated_profile_ids, "locked profile not reported as updated")
    reloaded3 = mgr.load_profile_config(meta3.id)
    check(reloaded3.vst_plugins[0].path == missing_dll,
          "locked profile's VST path left unchanged on disk")

    # remove_vst_plugin: bulk-drop the relinked path from profiles one and two
    remove_result = mgr.remove_vst_plugin(str(real_dll))
    check(set(remove_result.updated_profile_ids) == {meta1.id, meta2.id},
          "remove drops the plugin from every profile referencing it")
    reloaded1b = mgr.load_profile_config(meta1.id)
    check(reloaded1b.vst_plugins == [], "removed profile has no VST plugins left")

    # locked profile still untouched by remove
    remove_result2 = mgr.remove_vst_plugin(missing_dll)
    check(meta3.id in remove_result2.skipped_protected_ids,
          "locked profile reported as skipped by remove")
    reloaded3b = mgr.load_profile_config(meta3.id)
    check(reloaded3b.vst_plugins[0].path == missing_dll,
          "locked profile's VST plugin left unremoved on disk")

    print(f"\n{'=' * 60}")
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
