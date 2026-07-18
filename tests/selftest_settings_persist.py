"""Self-test for settings persistence (core only, no Qt).

Run:  python tests/selftest_settings_persist.py

Guards the promise that RUMBLE saves its settings cleanly and fully and keeps
them across a restart. Exercises, against temporary directories:

  * fresh defaults when no JSON exists
  * every DEFAULT_SETTINGS key written to disk and reloaded (full round-trip)
  * active_profile_id persistence
  * atomic write (no stray .tmp, valid JSON, real file is what's read)
  * unknown/stale keys dropped, absent keys fall back to defaults
  * corrupt / empty JSON falls back to defaults instead of crashing

The point of this test is that adding a settings key without wiring it through
save/load — or breaking the atomic write — fails the gate. Exits non-zero on
the first failure class.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core.profile_manager import DEFAULT_SETTINGS, ProfileManager

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def fresh_manager(tmp: Path, sub: str) -> ProfileManager:
    return ProfileManager(data_dir=tmp / sub / "data", config_path=tmp / sub / "config")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_settings_"))
    print(f"workdir: {tmp}\n")

    # ── 1. fresh defaults when nothing is persisted ──────────────────
    print("[1] fresh defaults")
    m = fresh_manager(tmp, "a")
    check(m.settings == DEFAULT_SETTINGS, "fresh manager exposes the documented defaults")
    check(m.settings is not DEFAULT_SETTINGS, "settings is a copy, not the shared default dict")
    check(not m.metadata_path.exists(), "no JSON written merely by constructing the manager")

    # ── 2. full round-trip: every key survives a reload ──────────────
    print("[2] full settings round-trip")
    # A non-default value for every key, so a key that silently fails to persist
    # would reload as its default and be caught below.
    overrides = {
        "config_path_override": str(tmp / "custom_apo"),
        "default_bands": 31,
        "auto_preamp": False,
        "start_with_windows": False,
        "start_minimised": False,
        "theme": "Onyx",
        "ui_double_size": True,
        "graph_y_range": 24,
        "graph_double_height": True,
        "sample_rate": 96000,
        "sample_rate_mode": "manual",
        "bypassed": True,
        "show_safety_warning": False,
        "last_active_profile": "my_profile",
        "show_builtin_profiles": False,
        "show_microphone_profiles": True,
        "ignored_apo_sources": ["some_old_config.txt"],
        "apo_managed": False,
        "spinorama_default_tilt": -1.5,
        "spinorama_smoothing_oct": 1 / 6,
        "spinorama_add_crossover": True,
        "spinorama_directivity_limiting": False,
        "adaptive_smoothing": False,
        "auto_switch_enabled": True,
        "auto_switch_rules": [{"trigger": "app", "match": "game.exe", "profile_id": "p1", "enabled": True}],
        "spectrum_enabled": True,
        "spectrum_mode": "both",
        "spectrum_peak_hold": True,
        "spectrum_display": "bars",
        "spectrum_compare_front": "input",
        "meter_mode": "peak",
        "meter_k_reference": "k20",
        "hearing_frequency_set": "coarse",
        "hearing_screening_last_seed": 12345,
        "default_listener_slot": 3,
        "listener_correction_enabled": False,
        "warn_unsaved_changes": False,
        "legacy_hearing_migrated": True,
        "iso226_loudness_enabled": True,
        "iso226_reference_phon": 75.0,
        "iso226_reference_volume_db": -10.0,
        "apo_coverage_dismissed_guids": ["{some-endpoint-guid}"],
        "profile_loudness_matching": True,
        "crossfeed_enabled": True,
        "crossfeed_preset": "chumoy",
        "crossfeed_strength": 65,
        "crossfeed_custom_gain_db": -11.5,
        "crossfeed_custom_freq_hz": 900.0,
        "crossfeed_custom_delay_us": 150.0,
        "crossfeed_soundstage_blend": 35,
        "crossfeed_soundstage_width": 70,
        "device_profile_assignments": {"{some-endpoint-guid}": "my_profile"},
    }
    check(set(overrides) == set(DEFAULT_SETTINGS),
          "test covers every DEFAULT_SETTINGS key (update this test when keys change)")
    check(all(overrides[k] != DEFAULT_SETTINGS[k] for k in overrides),
          "every override differs from its default (so a dropped key is detectable)")

    m.settings.update(overrides)
    m.save()

    raw = json.loads(m.metadata_path.read_text(encoding="utf-8"))
    check(set(raw["settings"]) == set(DEFAULT_SETTINGS), "every key written to disk, none missing")

    m2 = ProfileManager(data_dir=m.data_dir, config_path=m.config_path)
    mismatched = [k for k in overrides if m2.settings.get(k) != overrides[k]]
    check(not mismatched, f"every value survives reload (mismatched: {mismatched})")

    # ── 3. active profile id persists alongside settings ─────────────
    print("[3] active profile id persistence")
    check(raw.get("active_profile_id") is None, "no active profile id before activation")
    from rumble.core import apo_config
    mc = fresh_manager(tmp, "c")
    mc.ensure_first_run()
    created = mc.create_profile("Daily", config=apo_config.ProfileConfig())
    check(mc.activate_profile(created.id), "activation succeeds")
    rawc = json.loads(mc.metadata_path.read_text(encoding="utf-8"))
    check(rawc.get("active_profile_id") == created.id, "active id written to disk")
    mc2 = ProfileManager(data_dir=mc.data_dir, config_path=mc.config_path)
    check(mc2.get_active_profile() is not None and mc2.get_active_profile().id == created.id,
          "active profile restored on reload")

    # ── 4. atomic write hygiene ──────────────────────────────────────
    print("[4] atomic write")
    tmp_files = list(m.data_dir.glob("*.tmp"))
    check(not tmp_files, "no .tmp file left behind after save")
    # The committed file is always complete, valid JSON — never a half-written
    # body — because save() writes to a temp file then atomically replaces.
    json.loads(m.metadata_path.read_text(encoding="utf-8"))
    check(True, "committed JSON is parseable (atomic replace leaves no torn write)")
    # A stale .tmp from a prior interrupted run is ignored; the real file is read.
    m.metadata_path.with_suffix(".json.tmp").write_text("{ this is not json", encoding="utf-8")
    m3 = ProfileManager(data_dir=m.data_dir, config_path=m.config_path)
    check(m3.settings.get("theme") == "Onyx", "stale .tmp ignored, real file still loads")

    # ── 5. unknown keys dropped, absent keys defaulted ───────────────
    print("[5] forward/backward compatibility")
    poisoned = {
        "settings": {
            "theme": "Platinum",          # known -> kept
            "graph_y_range": 18,          # known -> kept
            "obsolete_flag": True,        # unknown -> dropped
            "tts_voice": "Zira",          # removed feature -> dropped
        },
        "active_profile_id": None,
    }
    md = fresh_manager(tmp, "d")
    md.metadata_path.write_text(json.dumps(poisoned), encoding="utf-8")
    md2 = ProfileManager(data_dir=md.data_dir, config_path=md.config_path)
    check(md2.settings.get("theme") == "Platinum", "known key loaded")
    check(md2.settings.get("graph_y_range") == 18, "second known key loaded")
    check("obsolete_flag" not in md2.settings and "tts_voice" not in md2.settings,
          "unknown / removed keys are dropped on load")
    check(md2.settings.get("auto_preamp") == DEFAULT_SETTINGS["auto_preamp"],
          "a key absent from the file falls back to its default")
    check(set(md2.settings) == set(DEFAULT_SETTINGS),
          "loaded settings always carry exactly the known key set")

    # ── 6. corrupt / empty file degrades to defaults, no crash ───────
    print("[6] corruption resilience")
    me = fresh_manager(tmp, "e")
    me.metadata_path.write_text("}{ totally corrupt", encoding="utf-8")
    me2 = ProfileManager(data_dir=me.data_dir, config_path=me.config_path)
    check(me2.settings == DEFAULT_SETTINGS, "corrupt JSON -> clean defaults, no crash")
    mf = fresh_manager(tmp, "f")
    mf.metadata_path.write_text("", encoding="utf-8")
    mf2 = ProfileManager(data_dir=mf.data_dir, config_path=mf.config_path)
    check(mf2.settings == DEFAULT_SETTINGS, "empty file -> clean defaults")

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
