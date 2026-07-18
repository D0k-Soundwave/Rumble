"""Self-test for the canonical single-file profile format (core only, no Qt).

Run:  python tools/selftest_profile_format.py
Exercises parse/render round-trips, legacy migration, auto-discovery,
import/export, metadata rewrites, flat-profile healing and backup/restore
against temporary directories. Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, profile_file
from rumble.core.profile_manager import ProfileManager, flat_band_frequencies, make_flat_config

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

    # ── 1. canonical render/parse round-trip ─────────────────────────
    print("[1] canonical document round-trip")
    cfg = apo_config.parse_config(
        "Device: all\nPreamp: -4.5 dB\nChannel: L R\n"
        "Filter 1: ON PK Fc 120.0 Hz Gain -2.5 dB Q 1.20\n"
        "Filter 2: ON HS Fc 12000.0 Hz Gain 1.0 dB Q 0.70\n"
        "GraphicEQ: 20 0; 20000 0\n"
    )
    doc = profile_file.ProfileDocument(
        name="Test Profile", description="A test", tags=["Music", "Headphones"],
        created="2026-01-01T10:00:00", modified="2026-06-09T12:00:00",
        readonly=False, config=cfg,
    )
    text = profile_file.render_profile_text(doc)
    doc2 = profile_file.parse_profile_text(text)
    check(doc2.name == "Test Profile", "name survives")
    check(doc2.description == "A test", "description survives")
    check(doc2.tags == ["Music", "Headphones"], "tags survive")
    check(doc2.created == "2026-01-01T10:00:00", "created survives")
    check(doc2.modified == "2026-06-09T12:00:00", "modified survives")
    check(doc2.has_full_header(), "full header detected")
    check(doc2.readonly is None, "readonly omitted when false")
    sec = doc2.config.channel_sections[0]
    check(sec.channel == "L R" and len(sec.filters) == 2, "config body survives")
    check(sec.graphic_eq == "20 0; 20000 0", "graphic eq survives")
    check(profile_file.render_profile_text(doc2) == text, "render is idempotent")

    # empty description/tags round-trip as stated-empty, not absent
    doc_empty = profile_file.ProfileDocument(name="X", description="", tags=[],
                                             created="2026-01-01T10:00:00",
                                             modified="2026-01-01T10:00:00", config=cfg)
    parsed_empty = profile_file.parse_profile_text(profile_file.render_profile_text(doc_empty))
    check(parsed_empty.description == "" and parsed_empty.tags == [], "empty fields stated explicitly")
    check(parsed_empty.has_full_header(), "empty fields still count as full header")

    # ── 2. legacy header parsing ─────────────────────────────────────
    print("[2] legacy 3-line header")
    legacy = (
        "# RUMBLE Profile: Old School\n"
        "# Created by RUMBLE\n"
        "# Modified: 2026-06-09 16:12:07\n\n"
        "Preamp: -3.0 dB\nFilter 1: ON PK Fc 1000.0 Hz Gain -3.0 dB Q 1.41\n"
    )
    ldoc = profile_file.parse_profile_text(legacy)
    check(ldoc.name == "Old School", "legacy name parsed")
    check(ldoc.modified == "2026-06-09T16:12:07", "legacy datetime normalised")
    check(not ldoc.has_full_header(), "legacy header flagged incomplete")
    check(ldoc.description is None and ldoc.tags is None, "absent fields are None")

    # foreign file with no header at all
    fdoc = profile_file.parse_profile_text("Filter 1: ON PK Fc 100 Hz Gain 1 dB Q 1\n")
    check(fdoc.name is None and not fdoc.has_full_header(), "foreign file: no metadata")
    check(len(fdoc.config.channel_sections[0].filters) == 1, "foreign file: config parsed")

    # replace_header preserves body verbatim and foreign comments
    rew = "# REW generated\nPreamp: -2.0 dB\nFilter  1: ON PK Fc 55 Hz Gain 2 dB Q 1.0\n"
    replaced = profile_file.replace_header(rew, doc)
    check("Filter  1: ON PK Fc 55 Hz Gain 2 dB Q 1.0" in replaced, "body preserved byte-for-byte")
    check("# REW generated" in replaced, "foreign comment preserved")
    check("# RUMBLE Profile: Test Profile" in replaced, "new header injected")

    # ── 3. manager lifecycle in temp dirs ────────────────────────────
    print("[3] manager: create / first run")
    m = fresh_manager(tmp, "a")
    m.ensure_first_run()
    check(m.get_profile("flat") is None, "no default profile is seeded")
    check(m.profiles == [], "library starts empty on first run")

    meta = m.create_profile("My Cans", description="daily driver", tags=["Music"], config=cfg)
    ptext = (m.config_path / meta.file).read_text(encoding="utf-8")
    check("# RUMBLE Profile: My Cans" in ptext and "# Tags: Music" in ptext, "created file has canonical header")

    # rename/update rewrite the header in place
    m.rename_profile(meta.id, "My Cans 2")
    m.update_profile_meta(meta.id, description="new desc", tags=["Music", "Ref"])
    ptext = (m.config_path / meta.file).read_text(encoding="utf-8")
    check("# RUMBLE Profile: My Cans 2" in ptext, "rename rewrites header")
    check("# Description: new desc" in ptext and "# Tags: Music, Ref" in ptext, "meta update rewrites header")
    check("Filter 1: ON PK Fc 120.0 Hz Gain -2.5 dB Q 1.20" in ptext, "body intact after header rewrites")

    # reload from disk: file metadata wins over the index
    raw = ptext.replace("# Description: new desc", "# Description: edited by hand")
    (m.config_path / meta.file).write_text(raw, encoding="utf-8")
    m2 = ProfileManager(data_dir=m.data_dir, config_path=m.config_path)
    check(m2.get_profile(meta.id).description == "edited by hand", "file metadata wins on reload")

    # ── 4. legacy file migration on load ─────────────────────────────
    # The file is the sole source of truth (no per-profile index), so loading a
    # legacy/foreign file upgrades it in place to the canonical full header:
    # name and body are kept, absent fields become stated-empty.
    print("[4] migration of legacy stored files")
    mb = fresh_manager(tmp, "b")
    mb.ensure_first_run()
    legacy_meta = mb.create_profile("Legacy One", description="kept desc", tags=["Gaming"])
    # overwrite with a legacy-format file (old 3-line header, no desc/tags)
    legacy_path = mb.config_path / legacy_meta.file
    legacy_path.write_text(legacy, encoding="utf-8")
    mb2 = ProfileManager(data_dir=mb.data_dir, config_path=mb.config_path)
    lm = mb2.get_profile(legacy_meta.id)
    check(lm.name == "Old School", "legacy file name adopted")
    check(lm.description == "" and lm.tags == [], "absent legacy fields become stated-empty")
    migrated = legacy_path.read_text(encoding="utf-8")
    check("# RUMBLE Profile: Old School" in migrated, "migrated header carries the name")
    check(profile_file.parse_profile_text(migrated).has_full_header(),
          "legacy file migrated to full canonical header")
    check("Filter 1: ON PK Fc 1000.0 Hz Gain -3.0 dB Q 1.41" in migrated, "legacy body preserved")

    # ── 5. auto-discovery of dropped files ───────────────────────────
    print("[5] discovery of unindexed canonical files")
    drop = mb2.rumble_dir / "dropped_profile.txt"
    drop.write_text(text, encoding="utf-8")  # canonical doc from step 1
    mb3 = ProfileManager(data_dir=mb.data_dir, config_path=mb.config_path)
    found = next((p for p in mb3.profiles if p.file.endswith("dropped_profile.txt")), None)
    check(found is not None, "dropped file registered")
    check(found.name == "Test Profile" and found.tags == ["Music", "Headphones"],
          "dropped file metadata adopted")
    check(not found.active, "discovered profile is not active")

    # ── 6. import / export round-trip ────────────────────────────────
    print("[6] import / export")
    exp = tmp / "exported.txt"
    check(mb3.export_profile(found.id, exp), "export succeeds")
    etext = exp.read_text(encoding="utf-8")
    check("# RUMBLE Profile: Test Profile" in etext and "# Readonly" not in etext,
          "export carries metadata, never readonly")
    mc = fresh_manager(tmp, "c")
    mc.ensure_first_run()
    imp, _ = mc.import_profile(exp)
    check(imp is not None and imp.name == "Test Profile", "import uses embedded name")
    check(imp.description == "A test" and imp.tags == ["Music", "Headphones"],
          "import preserves description and tags")
    check(imp.created == "2026-01-01T10:00:00", "import preserves created stamp")
    icfg = mc.load_profile_config(imp.id)
    check(icfg.channel_sections[0].channel == "L R" and len(icfg.channel_sections[0].filters) == 2,
          "imported config matches")

    # foreign import keeps old behaviour
    foreign = tmp / "rew_export.txt"
    foreign.write_text("Preamp: -2 dB\nFilter 1: ON PK Fc 55 Hz Gain 2 dB Q 1.0\n", encoding="utf-8")
    fimp, _ = mc.import_profile(foreign)
    check(fimp.name == "Rew Export", "foreign import: name from filename")
    check(fimp.description == "Imported from rew_export.txt", "foreign import: default description")

    # ── 7. activation, bypass, readonly guards ───────────────────────
    print("[7] activation / guards")
    mc.activate_profile(imp.id)
    pointer = (mc.config_path / "rumble_active.txt").read_text(encoding="utf-8")
    check(f"Include: {imp.file}" in pointer, "activation pointer references canonical file")
    mc.set_bypass(True)
    pointer = (mc.config_path / "rumble_active.txt").read_text(encoding="utf-8")
    check("no active commands" in pointer, "bypass pointer written")
    mc.set_bypass(False)
    guard = mc.create_profile("Guard", config=apo_config.ProfileConfig())
    mc.set_profile_locked(guard.id, True)
    check(not mc.save_profile_config(guard.id, apo_config.ProfileConfig()), "readonly save still blocked")
    check(not mc.rename_profile(guard.id, "Nope"), "readonly rename still blocked")
    check(not mc.delete_profile(guard.id), "readonly delete still blocked")

    # ── 8. backup / restore ──────────────────────────────────────────
    print("[8] backup / restore")
    zip_path = tmp / "backup.zip"
    check(mc.backup_library(zip_path), "backup succeeds")
    md = fresh_manager(tmp, "d")
    md.ensure_first_run()
    check(md.restore_library(zip_path), "restore succeeds")
    rest = md.get_profile(imp.id)
    check(rest is not None and rest.tags == ["Music", "Headphones"], "restored metadata intact")
    rtext = (md.config_path / rest.file).read_text(encoding="utf-8")
    check("# RUMBLE Profile: Test Profile" in rtext, "restored file is canonical")

    # ── 9. duplicate / delete still behave ───────────────────────────
    print("[9] duplicate / delete")
    dup = mc.duplicate_profile(imp.id, "Copy Of Test")
    check(dup is not None and dup.id != imp.id, "duplicate creates new id")
    dcfg = mc.load_profile_config(dup.id)
    check(len(dcfg.channel_sections[0].filters) == 2, "duplicate copies config")
    check(mc.delete_profile(dup.id), "delete succeeds")
    check(not (mc.config_path / f"rumble/{dup.id}.txt").exists(), "delete removes canonical file")

    # ── 10. full-feature template from PROFILE_FILE_CONSTRUCTION.md ──
    print("[10] kitchen-sink template (every supported APO feature)")
    doc_md = (Path(__file__).resolve().parents[1] / "docs" / "PROFILE_FILE_CONSTRUCTION.md").read_text(encoding="utf-8")
    start = doc_md.index("# RUMBLE Profile: Kitchen Sink Reference")
    end = doc_md.index("```", start)
    template = doc_md[start:end].strip("\n") + "\n"

    tdoc = profile_file.parse_profile_text(template)
    check(tdoc.name == "Kitchen Sink Reference" and tdoc.has_full_header(), "template header parsed")
    tcfg = tdoc.config
    check(tcfg.device == "all", "feature: Device")
    check(tcfg.preamp == -6.5, "feature: Preamp")
    check(tcfg.convolution == "room_correction.wav", "feature: Convolution")
    check(tcfg.copy_lines == ["L=0.9*L+0.1*R R=0.9*R+0.1*L"], "feature: Copy")
    check(len(tcfg.vst_plugins) == 1 and tcfg.vst_plugins[0].chunks.startswith("AAEXAMPLE"),
          "feature: VSTPlugin + Chunks")
    check(len(tcfg.channel_sections) == 2, "feature: multiple Channel sections")
    main_sec, lfe_sec = tcfg.channel_sections
    check(main_sec.channel == "L R" and lfe_sec.channel == "LFE", "channel scopes parsed")
    check(main_sec.delay_ms == 2.5 and lfe_sec.delay_ms == 8.0, "feature: per-channel Delay")
    check(lfe_sec.delay_samples == 384, "feature: Delay in samples kept verbatim")
    check(lfe_sec.preamp_db == -1.5, "feature: per-channel Preamp (trim)")
    check(main_sec.graphic_eq == "20 0; 100 -1.0; 1000 0; 10000 1.5; 20000 0", "feature: GraphicEQ")
    expected_types = {"PK", "PEQ", "Modal", "LP", "LPQ", "HP", "HPQ", "BP", "LS", "LSC",
                      "LS 6dB", "LS 12dB", "HS", "HSC", "HS 6dB", "HS 12dB", "NO", "AP", "IIR"}
    seen_types = {f.type for f in main_sec.filters}
    check(seen_types == expected_types, "feature: all 19 filter types present")
    check(any(not f.enabled for f in main_sec.filters), "feature: OFF (disabled) band")
    check(any(f.use_bw for f in main_sec.filters), "feature: BW Oct variant")
    iir = next(f for f in main_sec.filters if f.type == "IIR")
    check(iir.iir_order == 2 and len(iir.iir_coefficients.split()) == 6, "feature: IIR coefficients")
    check(len(tcfg.conditional_blocks) == 1 and "ElseIf:" in tcfg.conditional_blocks[0]
          and "Else:" in tcfg.conditional_blocks[0], "feature: If/ElseIf/Else/EndIf block")
    check(tcfg.stage == "post-mix", "feature: Stage (structured)")
    check(tcfg.eval_lines == ["linGain = 0.5"], "feature: Eval (structured)")
    check(tcfg.extra_lines == [], "feature: no unmodelled lines remain")
    check(tcfg.loudness_correction is not None
          and tcfg.loudness_correction.to_line() == "LoudnessCorrection: state 0",
          "feature: LoudnessCorrection (structured, Phase 1)")
    check(profile_file.render_profile_text(tdoc) == template,
          "template round-trips byte-identically")

    # ── 11. flat config builders (profile creation options) ──────────
    print("[11] flat config builders")
    check(flat_band_frequencies(10) == [31.0, 62.0, 125.0, 250.0, 500.0, 1000.0,
                                        2000.0, 4000.0, 8000.0, 16000.0],
          "classic 10-band frequencies preserved")
    check(flat_band_frequencies(4) == [31.0, 62.0, 125.0, 250.0], "small counts slice the classic list")
    f32 = flat_band_frequencies(32)
    check(len(f32) == 32 and f32[0] == 20.0 and f32[-1] == 20000.0
          and all(a < b for a, b in zip(f32, f32[1:])),
          "large counts log-spaced 20 Hz–20 kHz, ascending")
    check(flat_band_frequencies(99) == f32 and len(flat_band_frequencies(0)) == 1,
          "band count clamped to 1..32")

    mcfg = make_flat_config(8, channels=["L", "R", "LFE"], device="all")
    check([s.channel for s in mcfg.channel_sections] == ["L", "R", "LFE"],
          "one section per requested channel")
    check(all(len(s.filters) == 8 for s in mcfg.channel_sections), "band count per section")
    check(mcfg.preamp == 0.0 and mcfg.device == "all", "flat defaults")
    mcfg.channel_sections[0].filters[0].gain = 9.9
    check(mcfg.channel_sections[1].filters[0].gain == 0.0,
          "sections do not alias (independent Filter instances)")
    check(make_flat_config(10).channel_sections[0].channel == "all",
          "default stays single 'all' section (legacy behaviour)")

    # ── 12. device portability (foreign devices default to "all") ────
    print("[12] device portability")
    from rumble.core import audio_devices
    from rumble.core.profile_manager import _remap_unmatched_devices

    devices = [
        audio_devices.ApoDevice(guid="{guid-1}", device_name="Speakers",
                                connection_name="FIIO K13 R2R", flow="Render", state=1),
        audio_devices.ApoDevice(guid="{guid-2}", device_name="Headphones",
                                connection_name="USB Audio", flow="Render", state=1),
    ]
    check(audio_devices.pattern_matches_any("Speakers FIIO K13 R2R", devices),
          "full pattern matches its endpoint")
    check(audio_devices.pattern_matches_any("fiio", devices), "single word, case-insensitive")
    check(audio_devices.pattern_matches_any("all", devices), "'all' always matches")
    check(audio_devices.pattern_matches_any("{guid-2}", devices), "guid matches")
    check(not audio_devices.pattern_matches_any("Speakers Realtek", devices),
          "every word must occur in one endpoint")
    check(audio_devices.pattern_matches_any("Realtek; Headphones USB", devices),
          "';' alternatives — one matching alternative suffices")

    body = ("# RUMBLE Profile: Foreign\n"
            "Device: Speakers Realtek High Definition\n"
            "Preamp: -3.0 dB\n"
            "Filter 1: ON PK Fc 100 Hz Gain -2.0 dB Q 1.00\n")
    # Foreign devices remap to *our active device* (never "all" — see the
    # "default to first active device" decision). Mock enumeration so the
    # remap target is the deterministic first active endpoint.
    target = devices[0].pattern  # "Speakers FIIO K13 R2R"
    real_list = audio_devices.list_apo_devices
    audio_devices.list_apo_devices = lambda: devices
    try:
        out, remapped = _remap_unmatched_devices(body, devices)
        check(remapped and f"Device: {target}" in out and "Realtek" not in out,
              "unknown device rewritten to the active device")
        check(out.replace(f"Device: {target}", "Device: Speakers Realtek High Definition") == body,
              "only the Device line changed")
        _, rem2 = _remap_unmatched_devices(body.replace("Realtek High Definition", "FIIO"), devices)
        check(not rem2, "matching device left untouched")
        same, rem3 = _remap_unmatched_devices(body, [])
        check(not rem3 and same == body, "no endpoints enumerable -> no rewrite")

        mgr = fresh_manager(tmp, "devmap")
        src = tmp / "foreign_import.txt"
        src.write_text(body, encoding="utf-8")
        meta, _ = mgr.import_profile(src)
        imported = (mgr.config_path / meta.file).read_text(encoding="utf-8")
        check(f"Device: {target}" in imported and "Realtek" not in imported,
              "import remaps unknown device to the active device")

        mgr_a = fresh_manager(tmp, "devdrop")
        drop = mgr_a.rumble_dir / "dropped_foreign.txt"
        drop.parent.mkdir(parents=True, exist_ok=True)
        drop.write_text(body, encoding="utf-8")
        mgr_b = fresh_manager(tmp, "devdrop")
        dropped_text = drop.read_text(encoding="utf-8")
        check(any("dropped_foreign" in p.file for p in mgr_b.profiles),
              "dropped foreign file registered")
        check(f"Device: {target}" in dropped_text and "Realtek" not in dropped_text,
              "dropped file device remapped on discovery")
    finally:
        audio_devices.list_apo_devices = real_list

    # ── 13. IR bundles (zip export/import for Convolution profiles) ──
    print("[13] IR bundles")
    import zipfile

    ir_src = tmp / "room_correction.wav"
    ir_src.write_bytes(b"RIFF-FAKE-IR-PAYLOAD")
    mgr = fresh_manager(tmp, "irbundle")
    conv_cfg = apo_config.parse_config(
        f"Preamp: -2.0 dB\nConvolution: {ir_src}\n"
        "Filter 1: ON PK Fc 80 Hz Gain 2.0 dB Q 0.80\n")
    conv = mgr.create_profile("Room Corrected", config=conv_cfg)
    plain = mgr.create_profile("No Convolution")

    check(mgr.resolve_convolution_ir(conv.id) == ir_src, "absolute IR path resolves")
    check(mgr.resolve_convolution_ir(plain.id) is None, "no convolution -> no IR")

    bundle_path = tmp / "room_corrected.zip"
    check(mgr.export_profile_bundle(conv.id, bundle_path), "bundle export succeeds")
    check(not mgr.export_profile_bundle(plain.id, tmp / "nope.zip"),
          "bundle export refused without an IR")
    with zipfile.ZipFile(bundle_path) as zf:
        names = sorted(zf.namelist())
        check(names == sorted([f"{conv.id}.txt", "irs/room_correction.wav"]),
              "bundle layout: profile txt + irs/<file>")
        check(b"Convolution: irs/room_correction.wav" in zf.read(f"{conv.id}.txt"),
              "bundled profile references the relative IR path")

    other = fresh_manager(tmp, "irimport")
    imported, _ = other.import_profile(bundle_path)
    check(imported is not None and imported.name == "Room Corrected",
          "bundle imports with embedded name")
    stored_ir = other.rumble_dir / "irs" / "room_correction.wav"
    check(stored_ir.is_file() and stored_ir.read_bytes() == b"RIFF-FAKE-IR-PAYLOAD",
          "IR extracted to rumble/irs/")
    icfg = other.load_profile_config(imported.id)
    check(icfg.convolution == "irs/room_correction.wav",
          "imported Convolution points at the local copy")
    check(other.resolve_convolution_ir(imported.id) == stored_ir,
          "relative IR path resolves on the importing machine")

    again, _ = other.import_profile(bundle_path)
    ir_files = sorted(p.name for p in (other.rumble_dir / "irs").iterdir())
    check(ir_files == ["room_correction.wav"], "identical IR deduplicated on re-import")
    check(other.load_profile_config(again.id).convolution == "irs/room_correction.wav",
          "re-import reuses the stored IR")

    ir_src.write_bytes(b"DIFFERENT-IR-PAYLOAD")
    bundle2 = tmp / "room_corrected_v2.zip"
    check(mgr.export_profile_bundle(conv.id, bundle2), "second bundle export succeeds")
    third, _ = other.import_profile(bundle2)
    check(other.load_profile_config(third.id).convolution == "irs/room_correction_2.wav",
          "name collision with different bytes gets a numbered copy")
    check((other.rumble_dir / "irs" / "room_correction_2.wav").read_bytes()
          == b"DIFFERENT-IR-PAYLOAD", "numbered copy holds the new payload")

    reexport = tmp / "reexport.zip"
    check(other.export_profile_bundle(imported.id, reexport),
          "imported profile (relative IR) re-exports as a bundle")
    txt_export = tmp / "conv_plain.txt"
    check(mgr.export_profile(conv.id, txt_export)
          and str(ir_src) in txt_export.read_text(encoding="utf-8"),
          "plain txt export keeps the Convolution line as-is")

    # ── 14. per-channel Preamp (APO: preamps sum per selected channel) ─
    print("[14] per-channel preamp")
    pc = apo_config.parse_config(
        "Preamp: -2.0 dB\nPreamp: -1.5 dB\n"
        "Channel: L\nPreamp: -3.0 dB\nFilter 1: ON PK Fc 100.0 Hz Gain 2.0 dB Q 1.00\n"
        "Channel: R\nPreamp: -1.0 dB\nPreamp: -0.5 dB\n"
    )
    check(pc.preamp == -3.5, "global preamps sum in dB")
    check(pc.channel_sections[0].preamp_db == -3.0, "in-section preamp is per-channel")
    check(pc.channel_sections[1].preamp_db == -1.5, "multiple in-section preamps sum")
    check(len(pc.channel_sections) == 2, "preamp-only section survives")
    prendered = apo_config.render_config_body(pc)
    check("Channel: L\nPreamp: -3.0 dB\n" in prendered,
          "section preamp renders inside its section")
    preparsed = apo_config.parse_config(prendered)
    check(preparsed.preamp == -3.5
          and preparsed.channel_sections[0].preamp_db == -3.0
          and preparsed.channel_sections[1].preamp_db == -1.5,
          "per-channel preamp round-trips")
    pmerged = apo_config.parse_config(
        "Channel: L\nPreamp: -1.0 dB\nChannel: L\nPreamp: -2.0 dB\n")
    check(len(pmerged.channel_sections) == 1
          and pmerged.channel_sections[0].preamp_db == -3.0,
          "same-channel merge sums section preamps")
    pclone = pc.channel_sections[0].clone()
    pclone.filters[0].gain = 9.9
    pclone.preamp_db = -9.9
    check(pc.channel_sections[0].filters[0].gain == 2.0
          and pc.channel_sections[0].preamp_db == -3.0,
          "ChannelSection.clone() is independent (filters and new fields)")

    # ── 15. GraphicEQ stacking (multiple lines sum like APO filters) ──
    print("[15] graphic eq stacking")
    from rumble.core import autoeq_merge
    summed = autoeq_merge.sum_curves([[(100.0, 3.0), (1000.0, 3.0)],
                                      [(1000.0, 1.0), (10000.0, 1.0)]])
    check([f for f, _ in summed] == [100.0, 1000.0, 10000.0],
          "sum uses the union of breakpoints")
    check(all(abs(g - 4.0) < 1e-9 for _, g in summed),
          "sum adds dB (flat extension outside each range)")
    gc = apo_config.parse_config(
        "Channel: L\nGraphicEQ: 100 3; 1000 3\nGraphicEQ: 1000 1; 10000 1\n")
    gpts = autoeq_merge.parse_graphiceq(gc.channel_sections[0].graphic_eq)
    check([f for f, _ in gpts] == [100.0, 1000.0, 10000.0]
          and all(abs(g - 4.0) < 1e-6 for _, g in gpts),
          "stacked GraphicEQ lines combine exactly")
    gm = apo_config.parse_config(
        "Channel: L\nGraphicEQ: 100 3; 1000 3\nChannel: L\nGraphicEQ: 1000 1; 10000 1\n")
    gmpts = autoeq_merge.parse_graphiceq(gm.channel_sections[0].graphic_eq)
    check(all(abs(g - 4.0) < 1e-6 for _, g in gmpts),
          "same-channel merge combines GraphicEQ")
    gsingle = apo_config.parse_config("GraphicEQ: 20 0; 20000 0\n")
    check(gsingle.channel_sections[0].graphic_eq == "20 0; 20000 0",
          "single GraphicEQ line stays byte-verbatim")

    # ── 16. delay units preserved ─────────────────────────────────────
    print("[16] delay units")
    dsam = apo_config.parse_config("Channel: L\nDelay: 480 samples\n", sample_rate=96000)
    dsec = dsam.channel_sections[0]
    check(dsec.delay_samples == 480, "samples value kept verbatim")
    check(abs(dsec.delay_ms - 5.0) < 1e-9, "ms conversion honours the sample rate")
    check("Delay: 480 samples" in apo_config.render_config_body(dsam),
          "samples delay re-renders in samples")
    dms = apo_config.parse_config("Channel: L\nDelay: 2.5 ms\n")
    check(dms.channel_sections[0].delay_samples is None
          and dms.channel_sections[0].delay_ms == 2.5, "ms delay parses as before")
    check("Delay: 2.5 ms" in apo_config.render_config_body(dms), "ms delay renders in ms")

    # ── 17. Stage / Eval structured commands ─────────────────────────
    print("[17] stage / eval")
    sv = apo_config.parse_config(
        "Eval: x = 1\nStage: Pre-mix Post-mix\nDevice: all\n"
        "Stage: capture\nEval: y = 2\nFilter 1: ON PK Fc 100.0 Hz Gain 1.0 dB Q 1.00\n"
    )
    check(sv.stage == "pre-mix post-mix", "first Stage structured, normalised lowercase")
    check(sv.extra_lines == ["Stage: capture"], "later Stage lines stay verbatim")
    check(sv.eval_lines == ["x = 1", "y = 2"], "all Eval lines collected")
    srendered = apo_config.render_config_body(sv)
    check(srendered.startswith("Stage: pre-mix post-mix\n"),
          "Stage renders as the first body line")
    check(srendered.index("Device: all") < srendered.index("Eval: x = 1")
          < srendered.index("Filter 1:"),
          "Eval lines render after Device, before the channel body")
    sround = apo_config.parse_config(srendered)
    check(sround.stage == "pre-mix post-mix" and sround.eval_lines == ["x = 1", "y = 2"]
          and sround.extra_lines == ["Stage: capture"], "stage/eval round-trip is stable")

    # ── 18. VST plugin import trust ──────────────────────────────────
    # A profile arriving from outside the app (plain .txt or a .zip bundle)
    # must never carry a VSTPlugin: reference through silently -- a plugin
    # path from another machine practically never resolves to anything real
    # here, and importing one unexamined would let a downloaded "EQ profile"
    # load an arbitrary native DLL into the live audio pipeline. Native VST
    # setup (a profile built/edited directly in this app, e.g. via the
    # Advanced panel) must be completely unaffected.
    print("[18] VST plugin import trust")
    import zipfile as _zipfile18

    vmgr = fresh_manager(tmp, "vsttrust")

    # Plain .txt import: the VSTPlugin line is stripped, with a note.
    vst_txt = tmp / "has_vst.txt"
    vst_txt.write_text(
        "Preamp: 0.0 dB\nChannel: L R\n"
        "Filter 1: ON PK Fc 1000.0 Hz Gain 0.0 dB Q 1.41\n"
        "VSTPlugin: C:\\some\\other\\machine\\plugin.dll\n"
        "VSTPlugin Chunks: deadbeef\n",
        encoding="utf-8")
    vmeta, vnotes = vmgr.import_profile(vst_txt)
    check(vmeta is not None, "profile with a VSTPlugin line still imports")
    check(len(vnotes) == 1 and "VST plugin" in vnotes[0],
          "a note is returned for the stripped VST reference")
    vcfg = vmgr.load_profile_config(vmeta.id) if vmeta else None
    check(vcfg is not None and vcfg.vst_plugins == [],
          "the VSTPlugin reference was not carried into the imported profile")

    # .zip bundle import: same guard applies per contained .txt member.
    vst_bundle = tmp / "has_vst_bundle.zip"
    with _zipfile18.ZipFile(vst_bundle, "w") as zf:
        zf.writestr("Bundled VST Profile.txt",
                    "Preamp: 0.0 dB\nChannel: L R\n"
                    "VSTPlugin: /another/machine/plugin.dll\n")
    bmeta, bnotes = vmgr.import_profile(vst_bundle)
    check(bmeta is not None, "bundled profile with a VSTPlugin line still imports")
    check(len(bnotes) == 1 and "VST plugin" in bnotes[0],
          "a note is returned for the bundle's stripped VST reference")
    bcfg = vmgr.load_profile_config(bmeta.id) if bmeta else None
    check(bcfg is not None and bcfg.vst_plugins == [],
          "the bundled VSTPlugin reference was not carried into the imported profile")

    # Native VST setup (built directly in the app, e.g. the Advanced panel's
    # VSTPluginEditor) is a completely different path -- create_profile() /
    # update_profile_config(), never the import path -- and must round-trip
    # a VSTPlugin entry exactly as before.
    native_cfg = apo_config.ProfileConfig(
        device="all", preamp=0.0,
        channel_sections=[apo_config.ChannelSection(channel="L R")],
        vst_plugins=[apo_config.VSTPlugin(path=r"C:\VST\MyPlugin.dll", chunks="cafef00d")])
    native_meta = vmgr.create_profile("Native VST", config=native_cfg)
    native_round = vmgr.load_profile_config(native_meta.id)
    check(len(native_round.vst_plugins) == 1
          and native_round.vst_plugins[0].path == r"C:\VST\MyPlugin.dll"
          and native_round.vst_plugins[0].chunks == "cafef00d",
          "a VST plugin added natively (not imported) round-trips untouched")

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
