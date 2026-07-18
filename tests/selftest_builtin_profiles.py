"""Self-test for the built-in profile library (core only, no Qt).

Run:  python tests/selftest_builtin_profiles.py

Verifies the shipped built-in profile table (BUILTIN_PROFILES) is
well-formed and that ProfileManager's seeding/system-protection/drift-
refresh/stale-cleanup machinery behaves correctly against an injected
temporary entry. Exits non-zero on failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, preamp_calc, profile_manager
from rumble.core.builtin_profiles_data import BUILTIN_PROFILES
from rumble.core.profile_manager import ProfileManager

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def _test_body(preamp: float) -> str:
    lines = [f"Preamp: {preamp:.1f} dB", "", "Channel: L R",
             "Filter 1: ON PK Fc 1000.0 Hz Gain 4.0 dB Q 1.41",
             "GraphicEQ: 20 0; 20000 0"]
    return "\n".join(lines) + "\n"


def main() -> int:
    print("[1] shipped built-in library is well-formed")
    check(len(BUILTIN_PROFILES) > 0, "built-in profiles are present")
    for pid, entry in BUILTIN_PROFILES.items():
        check(all(entry.get(k) for k in ("name", "description", "tags", "body")),
              f"{pid}: has name/description/tags/body")
        cfg_b = apo_config.parse_config(entry["body"])
        sec = cfg_b.channel_sections[0] if cfg_b.channel_sections else None
        check(sec is not None, f"{pid}: body parses to a channel section")
        if sec is not None:
            hr = preamp_calc.calculate_headroom(sec.filters, cfg_b.preamp or 0.0)
            check(hr >= -0.15, f"{pid}: baked preamp prevents clipping "
                               f"(headroom {hr:+.2f} dB)")

    print("\n[2] D2: microphone built-ins present, hardware-agnostic, shield-badged like every built-in")
    mic_ids = ["rumble_voice_clean_hpf", "rumble_voice_presence", "rumble_voice_broadcast"]
    for pid in mic_ids:
        check(pid in BUILTIN_PROFILES, f"{pid}: shipped in BUILTIN_PROFILES")
    for pid in mic_ids:
        entry = BUILTIN_PROFILES.get(pid)
        if entry is None:
            continue
        check("Microphone" in entry["tags"], f"{pid}: tagged Microphone/...")
        cfg = apo_config.parse_config(entry["body"])
        sec = cfg.channel_sections[0] if cfg.channel_sections else None
        check(sec is not None and sec.channel == "all",
              f"{pid}: Channel: all (mono-safe, not L R)")

    tmp_mic = Path(tempfile.mkdtemp(prefix="rumble_selftest_builtin_mic_"))
    mb_mic = ProfileManager(data_dir=tmp_mic / "data", config_path=tmp_mic / "config")
    mb_mic.ensure_first_run()
    mb_mic.ensure_builtin_profiles()
    mb_mic.refresh_profiles()
    for pid in mic_ids:
        meta = mb_mic.get_profile(pid)
        check(meta is not None and meta.builtin and not meta.readonly,
              f"{pid}: seeded as system-protected (shield-badged), not user-locked")

    print("\n[3] seeding, system protection, drift refresh, stale cleanup")
    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_builtin_profiles_"))
    saved = profile_manager.BUILTIN_PROFILES
    profile_manager.BUILTIN_PROFILES = {
        "builtin_testcurve": {
            "name": "Test Curve",
            "description": "Synthetic entry used only by the selftest.",
            "tags": ["Music / Test"],
            "body": _test_body(-4.0),
        }
    }
    try:
        mb = ProfileManager(data_dir=tmp / "b" / "data", config_path=tmp / "b" / "config")
        mb.ensure_first_run()
        check(mb.ensure_builtin_profiles() is True, "first seeding reports changes")
        mb.refresh_profiles()
        seeded = [p for p in mb.profiles if p.builtin]
        check(len(seeded) == 1 and seeded[0].id == "builtin_testcurve",
              "one built-in profile per injected entry")
        check(mb.ensure_builtin_profiles() is False, "second seeding is a no-op")

        bp = mb.get_profile("builtin_testcurve")
        check(bp is not None and bp.builtin and not bp.readonly,
              "built-in is system protected but not user-locked")
        check(bp.protected, "protected property covers builtin")
        check(mb.delete_profile("builtin_testcurve") is False, "delete refused")
        check(mb.rename_profile("builtin_testcurve", "Hacked") is False, "rename refused")
        check(mb.update_profile_meta("builtin_testcurve", description="x") is False,
              "meta update refused")
        check(mb.set_profile_locked("builtin_testcurve", True) is False, "locking refused")
        dup = mb.duplicate_profile("builtin_testcurve", "My Curve")
        check(dup is not None and not dup.builtin and not dup.readonly,
              "duplicate becomes a normal user profile")

        bp_path = tmp / "b" / "config" / "rumble" / "defaults" / "builtin_testcurve.txt"
        text = bp_path.read_text(encoding="utf-8")
        check("# Builtin: true" in text, "file header carries Builtin flag")
        check("# Readonly: true" not in text, "file header has no Readonly flag")

        bp_path.unlink()
        check(mb.ensure_builtin_profiles() is True, "missing file regenerated")
        check(bp_path.exists(), "regenerated file exists")

        drifted = bp_path.read_text(encoding="utf-8").replace("Gain 4.0", "Gain 1.0")
        bp_path.write_text(drifted, encoding="utf-8")
        check(mb.ensure_builtin_profiles() is True, "drifted curve detected and refreshed")
        refreshed = mb.load_profile_config("builtin_testcurve")
        check(refreshed.channel_sections[0].filters[0].gain == 4.0,
              "refreshed file carries the canonical curve")
        check(mb.ensure_builtin_profiles() is False, "refresh is then a no-op")

        profile_manager.BUILTIN_PROFILES = {}
        check(mb.ensure_builtin_profiles() is True, "stale built-in removal reports a change")
        check(not bp_path.exists(), "stale built-in file removed")
    finally:
        profile_manager.BUILTIN_PROFILES = saved

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
