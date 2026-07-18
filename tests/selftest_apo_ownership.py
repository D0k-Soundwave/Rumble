"""Self-test for RUMBLE taking ownership of the Equalizer APO config and
importing existing configs (core only, no Qt).

Covers: the include resolver, the config.txt swap + managed marker + drift
re-assert, release/resume, discovery (samples & boilerplate ignored), import
compliance + provenance, the idempotent reconcile, and the built-in toggle.
Runs entirely against temp directories. Exits non-zero on the first failure.

Run:  python tests/selftest_apo_ownership.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, audio_devices, listener_slots
from rumble.core.audio_devices import ApoDevice
from rumble.core.profile_manager import (
    CONFIG_BACKUP_SUFFIX, CONFIG_DRIFT_SUFFIX, CONFIG_FILENAME, DEVICE_SCRATCH_PREFIX,
    LISTENER_ACTIVE_FILENAME, MANAGED_MARKER, PREVIEW_FILENAME,
    ProfileManager, _config_hash,
)

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def fresh(tmp: Path, sub: str) -> ProfileManager:
    return ProfileManager(data_dir=tmp / sub / "data", config_path=tmp / sub / "config")


def test_resolve_includes(tmp: Path) -> None:
    print("[1] include resolver")
    d = tmp / "inc"
    d.mkdir(parents=True)
    (d / "sub.txt").write_text("Filter: ON PK Fc 1000 Hz Gain 2 dB Q 1\n", encoding="utf-8")
    (d / "loop_a.txt").write_text("Include: loop_b.txt\nPreamp: -1 dB\n", encoding="utf-8")
    (d / "loop_b.txt").write_text("Include: loop_a.txt\nFilter: ON PK Fc 50 Hz Gain 1 dB Q 1\n",
                                  encoding="utf-8")
    flat = apo_config.resolve_includes(
        "Include: rumble_active.txt\nInclude: sub.txt\nInclude: missing.txt\nPreamp: -6 dB\n", d)
    check("rumble_active" not in flat, "drops RUMBLE's own include pointer")
    check("Fc 1000" in flat, "inlines a real include")
    check("Preamp: -6 dB" in flat, "keeps non-include lines")
    check("missing" not in flat, "missing include skipped, not inlined")
    # cycle guard: must terminate and inline each file once
    flat2 = apo_config.resolve_includes("Include: loop_a.txt\n", d)
    check("Fc 50" in flat2 and "Preamp: -1 dB" in flat2, "cycle resolved without hanging")
    # skip_stems drops sample includes
    flat3 = apo_config.resolve_includes("Include: sub.txt\n", d, skip_stems={"sub"})
    check("Fc 1000" not in flat3, "skip_stems drops sample includes")


def test_swap_and_drift(tmp: Path) -> None:
    print("[2] ownership swap + marker + drift + release/resume")
    m = fresh(tmp, "swap")
    cfg = m.config_path
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.txt").write_text(
        "Preamp: -6 dB\nInclude: peace.txt\nFilter: ON PK Fc 100 Hz Gain 3 dB Q 1\n",
        encoding="utf-8")
    orig_bytes = (cfg / "config.txt").read_bytes()

    a = m.ensure_first_run()
    cfg_txt = (cfg / "config.txt").read_text(encoding="utf-8")
    backup = (cfg / ("config.txt" + CONFIG_BACKUP_SUFFIX)).read_text(encoding="utf-8")
    check(MANAGED_MARKER in cfg_txt and "Include: rumble_active.txt" in cfg_txt,
          "config.txt becomes the managed marker + include")
    check("Preamp: -6 dB" in backup and "peace.txt" in backup, "original saved to backup verbatim")
    # #5: the backup must be a byte-for-byte copy of the original (copy2, not a
    # re-serialised strip) so "Hand back to APO" / uninstall restores it exactly.
    check((cfg / ("config.txt" + CONFIG_BACKUP_SUFFIX)).read_bytes() == orig_bytes,
          "backup is byte-for-byte identical to the original config.txt")
    check(a["backed_up"] and a["owned_config"] and not a["reasserted"], "first run: owned + backed up")
    check(a["foreign_includes"] == ["peace.txt"], "foreign include (peace.txt) flagged")

    a2 = m.ensure_first_run()
    check(not a2["owned_config"] and not a2["backed_up"], "second run is a no-op (marker present)")

    # drift: a foreign tool overwrites config.txt
    (cfg / "config.txt").write_text("Preamp: -9 dB\n", encoding="utf-8")
    a3 = m.ensure_first_run()
    backup2 = (cfg / ("config.txt" + CONFIG_BACKUP_SUFFIX)).read_text(encoding="utf-8")
    check(MANAGED_MARKER in (cfg / "config.txt").read_text(encoding="utf-8"), "drift re-asserts ownership")
    check(a3["reasserted"] and not a3["backed_up"], "drift reasserts without re-backing-up")
    check("Preamp: -6 dB" in backup2, "original backup preserved (not clobbered by drift)")

    check(m.release_control(), "release_control succeeds")
    rel = (cfg / "config.txt").read_text(encoding="utf-8")
    check(MANAGED_MARKER not in rel and "peace.txt" in rel, "release restores the original config")
    check((cfg / "config.txt").read_bytes() == orig_bytes,
          "release restores config.txt byte-for-byte from the verbatim backup")
    check(not (cfg / "rumble_active.txt").exists(), "release removes the active pointer")
    check(not m.is_apo_managed(), "release flips apo_managed off")
    # while handed back, a launch must not re-take ownership
    a4 = m.ensure_first_run()
    check(not a4["owned_config"], "no re-ownership while handed back")
    # resume re-owns
    m.resume_control()
    check(MANAGED_MARKER in (cfg / "config.txt").read_text(encoding="utf-8"), "resume re-takes ownership")


def test_release_control_cleans_device_dispatch_footprint(tmp: Path) -> None:
    print("[2a] D1c: release_control() removes the whole dispatch render footprint")
    m = fresh(tmp, "release_dispatch")
    m.ensure_first_run()
    meta_a = m.create_profile("A", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    meta_global = m.create_profile("Global", config=apo_config.ProfileConfig(
        channel_sections=[apo_config.ChannelSection(channel="all")]))
    m.activate_profile(meta_global.id)

    guid = "{AAAAAAAA-0000-0000-0000-000000000001}"
    device = ApoDevice(guid=guid, device_name="Speakers",
                        connection_name="Creative BT-W6", flow="Render", state=1)
    real_list = audio_devices.list_apo_devices
    audio_devices.list_apo_devices = lambda: [device]
    try:
        # Turn on a render layer (listener correction) so both the per-device
        # branch AND the fallback branch render composed scratch, not a bare
        # Include: of the profile file directly -- release_control() must
        # clean up all of it, not just the top-level dispatch pointer.
        listener_slots.save_slot_result(
            m.data_dir, 1, {"left": {}, "right": {}, "imbalance": {1000: 4.0}}, {"left": {}, "right": {}})
        m.invalidate_listener_correction_cache()
        m.settings["device_profile_assignments"] = {guid: meta_a.id}
        m.save()
        m.activate_profile(meta_global.id)

        scratch_a = m.rumble_dir / m._device_scratch_filename(guid)
        fallback_scratch = m.rumble_dir / LISTENER_ACTIVE_FILENAME
        check(scratch_a.exists() and fallback_scratch.exists(),
              "sanity: both a per-device scratch and the composed fallback scratch exist")
        assert m.preview_config(apo_config.ProfileConfig(
            channel_sections=[apo_config.ChannelSection(channel="all")])) is True
        preview_scratch = m.rumble_dir / PREVIEW_FILENAME
        check(preview_scratch.exists(), "sanity: a live preview scratch file exists too")

        check(m.release_control(), "release_control succeeds with an active dispatch")
        check(not (m.config_path / "rumble_active.txt").exists(),
              "release removes the active pointer even with per-device assignments")
        check(not preview_scratch.exists(), "release removes the (correct, rumble_dir) preview scratch")
        check(not fallback_scratch.exists(), "release removes the composed-fallback scratch")
        check(list(m.rumble_dir.glob(f"{DEVICE_SCRATCH_PREFIX}*.txt")) == [],
              "release removes every per-device dispatch scratch file")
        # Never touches the profile library itself (unlike the uninstaller's
        # DelTree of the whole rumble/ tree) -- release is reversible.
        check(m.get_profile(meta_a.id) is not None and m.get_profile(meta_global.id) is not None,
              "release_control never deletes user profiles -- only render scratch/pointers")
    finally:
        audio_devices.list_apo_devices = real_list


def test_marker_present_foreign_reassert(tmp: Path) -> None:
    print("[2b] marker present + foreign tool appends -> re-assert, drift-preserve")
    m = fresh(tmp, "coexist")
    cfg = m.config_path
    cfg.mkdir(parents=True, exist_ok=True)
    orig = "Preamp: -4 dB\n"
    (cfg / CONFIG_FILENAME).write_text(orig, encoding="utf-8")
    m.ensure_first_run()  # take ownership; sacred backup == orig
    sacred = cfg / (CONFIG_FILENAME + CONFIG_BACKUP_SUFFIX)
    sacred_bytes = sacred.read_bytes()

    # A foreign tool appends its own include BELOW RUMBLE's managed block.
    managed = (cfg / CONFIG_FILENAME).read_text(encoding="utf-8")
    (cfg / CONFIG_FILENAME).write_text(managed + "Include: peace.txt\n", encoding="utf-8")

    a = m.ensure_first_run()
    cfg_txt = (cfg / CONFIG_FILENAME).read_text(encoding="utf-8")
    drifts = list(cfg.glob(CONFIG_FILENAME + CONFIG_DRIFT_SUFFIX + "*"))
    check(a["owned_config"] and a["foreign_includes"] == ["peace.txt"],
          "marker-present foreign Include is detected and re-asserted")
    check("peace.txt" not in cfg_txt and MANAGED_MARKER in cfg_txt,
          "config.txt is cleaned back to RUMBLE's sole managed block")
    check(a["drift_backed_up"] and len(drifts) == 1
          and "peace.txt" in drifts[0].read_text(encoding="utf-8"),
          "the appended foreign content is preserved in a drift backup")
    check(sacred.read_bytes() == sacred_bytes,
          "the sacred first backup is never touched by a drift re-assert")

    # Clean again => fully idempotent, no second drift file.
    a2 = m.ensure_first_run()
    check(not a2["owned_config"] and not a2["drift_backed_up"]
          and len(list(cfg.glob(CONFIG_FILENAME + CONFIG_DRIFT_SUFFIX + "*"))) == 1,
          "a clean managed config is a no-op (no spurious drift backups)")


def test_discover_and_reconcile(tmp: Path) -> None:
    print("[3] discover + import + reconcile")
    m = fresh(tmp, "disc")
    cfg = m.config_path
    cfg.mkdir(parents=True, exist_ok=True)
    # boilerplate-only config.txt (lone preamp + a sample include) -> nothing offered
    (cfg / "config.txt").write_text("Preamp: -6 dB\nInclude: example.txt\n", encoding="utf-8")
    (cfg / "example.txt").write_text("Filter: ON PK Fc 50 Hz Gain 4 dB Q 1\n", encoding="utf-8")  # sample
    (cfg / "demo.txt").write_text("Filter: ON PK Fc 80 Hz Gain 6 dB Q 1\n", encoding="utf-8")      # sample
    (cfg / "myeq.txt").write_text("Filter: ON PK Fc 2000 Hz Gain -3 dB Q 1.5\n", encoding="utf-8")  # real

    m.ensure_first_run()
    cands = m.sync_apo_imports()
    sources = sorted(c.source for c in cands)
    check(sources == ["myeq.txt"], "samples + lone-preamp boilerplate ignored; real file offered")

    created = m.import_apo_candidates(cands)
    check(len(created) == 1 and created[0].apo_source == "myeq.txt", "import creates a provenance-stamped profile")
    text = (cfg / created[0].file).read_text(encoding="utf-8")
    check("# APO Source: myeq.txt" in text, "provenance written into the profile header")
    sections = m.load_profile_config(created[0].id).channel_sections
    check(len(sections) == 1 and sections[0].channel == "L R",
          "channel-less import is stereo-normalised to a combined 'L R' section")

    # #8: reloading an unedited import through the same pipeline must hash equal
    # to its recorded import_signature, or sync would either falsely treat it as
    # user-edited (never re-syncs a changed source) or falsely clobber it.
    reloaded = m.load_profile_config(created[0].id)
    check(_config_hash(reloaded) == created[0].import_signature,
          "round-trip: reloaded import hashes equal to its import_signature")

    check(m.sync_apo_imports() == [], "idempotent: already-imported source not re-offered")

    # source changes -> pristine profile refreshes
    (cfg / "myeq.txt").write_text("Filter: ON PK Fc 2000 Hz Gain -6 dB Q 1.5\n", encoding="utf-8")
    m.sync_apo_imports()
    check("Gain -6" in (cfg / created[0].file).read_text(encoding="utf-8"),
          "source change refreshes a pristine imported profile")

    # user edits the profile, source changes again -> must not clobber
    conf = m.load_profile_config(created[0].id)
    conf.channel_sections[0].filters[0].gain = 9.0
    m.save_profile_config(created[0].id, conf)
    (cfg / "myeq.txt").write_text("Filter: ON PK Fc 2000 Hz Gain -1 dB Q 1.5\n", encoding="utf-8")
    m.sync_apo_imports()
    check("Gain 9" in (cfg / created[0].file).read_text(encoding="utf-8"),
          "user-edited imported profile is never clobbered")

    # declined source isn't re-offered
    (cfg / "another.txt").write_text("Filter: ON PK Fc 500 Hz Gain 2 dB Q 1\n", encoding="utf-8")
    check([c.source for c in m.sync_apo_imports()] == ["another.txt"], "new source offered")
    m.ignore_apo_sources(["another.txt"])
    check(m.sync_apo_imports() == [], "declined source not re-offered")


def test_demo_only_noop(tmp: Path) -> None:
    print("[4] APO with only demo content offers nothing")
    m = fresh(tmp, "demo")
    cfg = m.config_path
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.txt").write_text("Preamp: -6 dB\nInclude: example.txt\n", encoding="utf-8")
    (cfg / "example.txt").write_text("Filter: ON PK Fc 20 Hz Gain 4 dB Q 1\n", encoding="utf-8")
    (cfg / "demo.txt").write_text("Filter: ON PK Fc 100 Hz Gain 6 dB Q 1\n", encoding="utf-8")
    m.ensure_first_run()
    check(m.sync_apo_imports() == [], "a stock-sample-only APO yields no import candidates")


def test_builtin_toggle(tmp: Path) -> None:
    print("[5] built-in profiles enable/disable")
    m = fresh(tmp, "builtin")
    m.ensure_first_run()
    m.ensure_builtin_profiles()
    defaults = m.rumble_dir / "defaults"
    n = len(list(defaults.glob("*.txt")))
    check(n > 0, f"built-ins generated when enabled ({n})")
    # activate a built-in, then disable
    builtin_ids = [p.id for p in m.list_profiles() if p.builtin]
    check(bool(builtin_ids), "built-in profiles present in the library")
    m.activate_profile(builtin_ids[0])
    m.settings["show_builtin_profiles"] = False
    m.ensure_builtin_profiles()
    check(len(list(defaults.glob("*.txt"))) == 0, "disabling removes the generated built-ins")
    check(m.get_active_profile() is None, "an active built-in falls back to bypass when removed")
    # #7: the cleared active id must be persisted, not just held in memory, or a
    # restart resurrects a pointer to a profile that no longer exists.
    import json as _json
    saved = _json.loads((m.data_dir / "profiles.json").read_text(encoding="utf-8"))
    check(saved.get("active_profile_id") is None,
          "disabling built-ins persists the cleared active_profile_id")
    m.settings["show_builtin_profiles"] = True
    m.ensure_builtin_profiles()
    check(len(list(defaults.glob("*.txt"))) == n, "re-enabling regenerates the built-ins")


def test_importable_candidates_dedup(tmp: Path) -> None:
    print("[6] manual import shares the boot reconcile's exclusion (no dupes)")
    m = fresh(tmp, "dedup")
    cfg = m.config_path
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.txt").write_text("Preamp: -6 dB\n", encoding="utf-8")
    (cfg / "gizmo.txt").write_text("Filter: ON PK Fc 1500 Hz Gain -4 dB Q 1.2\n", encoding="utf-8")
    m.ensure_first_run()

    cands = m.importable_candidates()
    check([c.source for c in cands] == ["gizmo.txt"], "a fresh source is offered to the manual import")
    created = m.import_apo_candidates(cands)
    check(len(created) == 1, "manual import creates one profile")
    # The Settings → Import path now filters exactly like sync_apo_imports, so an
    # already-imported source is never re-offered (was the duplicate-spawning bug).
    check(m.importable_candidates() == [], "an imported source is not re-offered (no duplicates)")
    before = len(m.list_profiles())
    again = m.import_apo_candidates(m.importable_candidates())
    check(again == [] and len(m.list_profiles()) == before,
          "re-running import creates no duplicate profile")

    # Declined sources are excluded from the manual path too.
    (cfg / "widget.txt").write_text("Filter: ON PK Fc 800 Hz Gain 3 dB Q 1\n", encoding="utf-8")
    check([c.source for c in m.importable_candidates()] == ["widget.txt"], "another new source is offered")
    m.ignore_apo_sources(["widget.txt"])
    check(m.importable_candidates() == [], "a declined source is excluded from manual import")


def test_bad_override_fallback(tmp: Path) -> None:
    print("[7] a bad config_path_override falls back instead of stranding (#4)")
    m = fresh(tmp, "override")
    # An uncreatable override: a child of a regular file, so the mkdir in
    # ensure_first_run raises NotADirectoryError (an OSError) -> config not
    # accessible. This is exactly the user-set override that used to strand
    # startup with a fatal error and no in-app way to fix it.
    blocker = tmp / "override" / "not_a_dir"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("x", encoding="utf-8")
    bad_override = str(blocker / "config")
    m.settings["config_path_override"] = bad_override
    a = m.ensure_first_run()
    check(not a["config_accessible"], "an uncreatable override reports config_accessible=False")
    # The startup recovery (rumble/__main__): set the session-only ignore flag and
    # retry against the default APO path (here the test's config_path hint), which
    # must now succeed so the window — and Settings — opens.
    m._ignore_config_override = True
    a2 = m.ensure_first_run()
    check(a2["config_accessible"], "ignoring the bad override restores config access")
    check(MANAGED_MARKER in (m.config_path / "config.txt").read_text(encoding="utf-8"),
          "the fallback path takes ownership so the app can start instead of exiting")
    # The persisted override must be left intact (session-only fallback), so a
    # later launch retries it once the path is reachable again. A save() — as the
    # boot adoption does when it imports/ignores sources — must not wipe it.
    m.save()
    import json as _json
    saved = _json.loads((m.data_dir / "profiles.json").read_text(encoding="utf-8"))
    check(saved.get("settings", {}).get("config_path_override") == bad_override,
          "the persisted override survives the session fallback (retried next launch)")
    # Issue 1: while the fallback flag is set the override is ignored; once the
    # user corrects the path in Settings the UI clears the flag, and config_path
    # must then honour the corrected override (not stay parked on the fallback).
    good = tmp / "override" / "fixed_config"
    m.settings["config_path_override"] = str(good)
    check(m.config_path != good, "while ignoring, a corrected override is not yet used")
    m._ignore_config_override = False  # what Settings Save does when the path changes
    check(m.config_path == good, "clearing the ignore flag honours the corrected override")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_apo_own_"))
    print(f"workdir: {tmp}\n")
    test_resolve_includes(tmp)
    test_swap_and_drift(tmp)
    test_release_control_cleans_device_dispatch_footprint(tmp)
    test_marker_present_foreign_reassert(tmp)
    test_discover_and_reconcile(tmp)
    test_demo_only_noop(tmp)
    test_builtin_toggle(tmp)
    test_importable_candidates_dedup(tmp)
    test_bad_override_fallback(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL APO-OWNERSHIP TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
