"""Self-test for the profile recycle bin (core only, no Qt) — session A3.

Exercises delete->restore round-trip fidelity, retention cap-on-write,
purge, and the built-in/locked bypass, against a temporary directory tree.
Run:  python tests/selftest_profile_trash.py
Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config
from rumble.core.profile_manager import MAX_TRASHED, ProfileManager

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def fresh_manager(tmp: Path, sub: str) -> ProfileManager:
    return ProfileManager(data_dir=tmp / sub / "data", config_path=tmp / sub / "config")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_trash_"))
    print(f"workdir: {tmp}\n")

    # ── 1. delete -> restore round-trip preserves body + metadata ────
    print("[1] delete -> restore round-trip")
    mgr = fresh_manager(tmp, "roundtrip")
    cfg = apo_config.parse_config(
        "Device: all\nPreamp: -3.5 dB\nChannel: L R\n"
        "Filter 1: ON PK Fc 120.0 Hz Gain -2.5 dB Q 1.20\n"
        "Filter 2: ON HS Fc 12000.0 Hz Gain 1.0 dB Q 0.70\n"
    )
    meta = mgr.create_profile("Trash Me", description="A test profile",
                              tags=["Music", "Headphones"], config=cfg)
    original_body = apo_config.render_config_body(mgr.load_profile_config(meta.id))
    original_file = meta.file

    check(mgr.list_trashed() == [], "recycle bin starts empty")
    check(mgr.delete_profile(meta.id), "delete succeeds")
    check(mgr.get_profile(meta.id) is None, "profile no longer listed")
    check(not (mgr.config_path / original_file).exists(), "original file removed")

    trashed = mgr.list_trashed()
    check(len(trashed) == 1, "one recycle-bin entry")
    entry = trashed[0]
    check(entry.name == "Trash Me", "sidecar preserves name")
    check(entry.description == "A test profile", "sidecar preserves description")
    check(entry.tags == ["Music", "Headphones"], "sidecar preserves tags")
    check(entry.locked is False, "sidecar records locked flag (false for a normal delete)")
    check(entry.original_file == original_file, "sidecar preserves original location")

    restored = mgr.restore_trashed(entry)
    check(restored is not None, "restore returns a new profile")
    check(restored.name == "Trash Me", "restored name matches")
    check(restored.description == "A test profile", "restored description matches")
    check(restored.tags == ["Music", "Headphones"], "restored tags match")
    restored_body = apo_config.render_config_body(mgr.load_profile_config(restored.id))
    check(restored_body == original_body, "restored body is byte-for-byte identical")
    check(len(mgr.list_trashed()) == 1, "restoring leaves the recycle-bin entry in place")

    # ── 2. purge removes the entry for good ──────────────────────────
    print("[2] purge")
    entry = mgr.list_trashed()[0]
    check(mgr.purge_trashed(entry), "purge succeeds")
    check(mgr.list_trashed() == [], "recycle bin empty after purge")
    check(not entry.txt_path.exists() and not entry.json_path.exists(),
          "purge removes both files from disk")

    # ── 3. built-in / locked profiles bypass the trash entirely ──────
    print("[3] built-in / locked bypass")
    mgr2 = fresh_manager(tmp, "bypass")
    mgr2.ensure_builtin_profiles()
    builtin_ids = [p.id for p in mgr2.profiles if p.builtin]
    check(bool(builtin_ids), "built-ins exist to test against")
    if builtin_ids:
        check(mgr2.delete_profile(builtin_ids[0]) is False, "built-in delete refused")
    check(mgr2.list_trashed() == [], "refused built-in delete leaves no trash entry")

    locked = mgr2.create_profile("Locked One", config=apo_config.ProfileConfig())
    mgr2.set_profile_locked(locked.id, True)
    check(mgr2.delete_profile(locked.id) is False, "locked delete refused")
    check(mgr2.list_trashed() == [], "refused locked delete leaves no trash entry")

    # ── 4. retention cap: newest MAX_TRASHED kept, oldest purged ─────
    print("[4] retention cap")
    mgr3 = fresh_manager(tmp, "retention")
    total = MAX_TRASHED + 5
    ids = []
    for i in range(total):
        p = mgr3.create_profile(f"Bulk {i:02d}", config=apo_config.ProfileConfig())
        ids.append(p.id)
        check(mgr3.delete_profile(p.id), f"delete Bulk {i:02d} succeeds")

    kept = mgr3.list_trashed()
    check(len(kept) == MAX_TRASHED, f"recycle bin capped at {MAX_TRASHED}")
    kept_names = {e.name for e in kept}
    check("Bulk 00" not in kept_names, "oldest entry was purged on overflow")
    check(f"Bulk {total - 1:02d}" in kept_names, "newest entry survives")

    # ── 5. backup includes trash ──────────────────────────────────────
    print("[5] backup includes trash")
    mgr4 = fresh_manager(tmp, "backup")
    bp = mgr4.create_profile("Backed Up Then Trashed", config=apo_config.ProfileConfig())
    mgr4.delete_profile(bp.id)
    check(len(mgr4.list_trashed()) == 1, "one trash entry before backup")
    zip_path = tmp / "backup_with_trash.zip"
    check(mgr4.backup_library(zip_path), "backup succeeds")
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    check(any(n.startswith("data/trash/") and n.endswith(".json") for n in names),
          "trash sidecar rides into the backup archive")
    check(any(n.startswith("data/trash/") and n.endswith(".txt") for n in names),
          "trash body rides into the backup archive")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
