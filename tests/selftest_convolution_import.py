"""Self-test: Convolution/IR profile creation
(ProfileManager.create_profile_with_convolution) — dual-rate IR storage plus
the APO `If: sampleRate == …` conditional block that lets Equalizer APO pick
the right file at runtime.

The load-bearing check here is the DOUBLE-CONVOLUTION guard: APO *adds* a
convolver per `Convolution:` line, so a multi-rate profile that carries both a
bare line and an exhaustive conditional convolves its IR twice and applies
every dB of the correction at 2x. The rendered body must contain exactly one
`Convolution:` line per branch and no bare one. Fully offline, temp-dir based."""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rumble.core import apo_config
from rumble.core.profile_manager import ProfileManager

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_"))
mgr = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
meta = mgr.create_profile_with_convolution(
    "Test IR", {44100: b"RIFF44", 48000: b"RIFF48"})
check(meta is not None, "dual-rate IR profile is created")
cfg = mgr.load_profile_config(meta.id)
check(not cfg.convolution,
      "multi-rate profile writes NO bare Convolution line (would double-convolve)")
check(len(cfg.conditional_blocks) == 1, "a sampleRate conditional block is written")
check("If: sampleRate == 44100" in cfg.conditional_blocks[0], "conditional selects 44.1kHz")
check("Else:" in cfg.conditional_blocks[0], "conditional has an Else fallback (covers every rate)")
check("EndIf:" in cfg.conditional_blocks[0], "conditional block is well-formed")

# What APO actually reads. Exactly one Convolution line per branch, none outside.
body = apo_config.render_config_body(cfg)
conv_lines = [ln.strip() for ln in body.splitlines()
              if re.match(r"^\s*Convolution\s*:", ln, re.IGNORECASE)]
check(len(conv_lines) == 3,
      f"rendered body convolves once per branch, 3 branches (got {len(conv_lines)})")
bare = [ln for ln in body.splitlines()
        if re.match(r"^Convolution\s*:", ln, re.IGNORECASE)]  # no leading indent = outside the block
check(not bare, "rendered body has no unconditional Convolution line")

check(apo_config.fallback_convolution_path(cfg).endswith("_48000.wav"),
      "Else branch falls back to the 48kHz default")
resolved = mgr.resolve_convolution_ir(meta.id)
check(resolved is not None and resolved.name.endswith("_48000.wav"),
      "resolve_convolution_ir finds the conditional's default-rate file")

single = mgr.create_profile_with_convolution("Single Rate", {44100: b"RIFF44"})
cfg2 = mgr.load_profile_config(single.id)
check(cfg2.conditional_blocks == [], "single-rate IR produces no conditional block")
check(cfg2.convolution.endswith("_44100.wav"),
      "single-rate IR keeps its plain unconditional Convolution line")
single_body = apo_config.render_config_body(cfg2)
check(len([ln for ln in single_body.splitlines()
           if re.match(r"^\s*Convolution\s*:", ln, re.IGNORECASE)]) == 1,
      "single-rate rendered body convolves exactly once")

# Round-trip through the bundle: every branch's IR must survive export+import,
# and the re-imported profile must still convolve only once per branch.
bundle_path = tmp / "ir_bundle.zip"
check(mgr.export_profile_bundle(meta.id, bundle_path), "multi-rate profile exports as a bundle")
import zipfile
with zipfile.ZipFile(bundle_path) as zf:
    members = zf.namelist()
check(sum(1 for m in members if m.lower().endswith(".wav")) == 2,
      "bundle carries BOTH rates' IRs (not just the default)")
bundle_txt = next(m for m in members if m.lower().endswith(".txt"))
with zipfile.ZipFile(bundle_path) as zf:
    exported = zf.read(bundle_txt).decode("utf-8")
check(not [ln for ln in exported.splitlines()
           if re.match(r"^Convolution\s*:", ln, re.IGNORECASE)],
      "exported text has no unconditional Convolution line either")

# ── Security: a hand-crafted bundle with an oversized (but highly
#    compressible, so tiny on disk) IR member must have that IR refused
#    rather than fully decompressed and stored — the profile itself should
#    still import (zip-bomb size-cap guard in
#    ProfileManager._read_zip_member_capped(), used by _rehome_bundle_irs()).
from rumble.core.profile_manager import _MAX_IR_MEMBER_BYTES

bomb_bundle = tmp / "ir_bomb_bundle.zip"
oversized_ir = b"0" * (_MAX_IR_MEMBER_BYTES + 1)
with zipfile.ZipFile(bomb_bundle, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("Bomb Profile.txt",
                "Preamp: 0.0 dB\nChannel: L R\nConvolution: bomb.wav\n")
    zf.writestr("bomb.wav", oversized_ir)
check(bomb_bundle.stat().st_size < 1024 * 1024, "the crafted IR-bomb bundle itself stays small on disk")

result = mgr.import_profiles([bomb_bundle])
check(len(result.imported) == 1, "the profile in a bundle still imports despite its oversized IR")
if result.imported:
    bomb_cfg = mgr.load_profile_config(result.imported[0].id)
    # Same fallback as a genuinely missing bundle member: the reference is
    # left alone (unresolved), never rehomed under rumble/irs/.
    check(bomb_cfg.convolution == "bomb.wav",
          "the oversized IR was refused: left unresolved, not stored/repointed")
ir_dir = mgr.rumble_dir / "irs"
oversized_stored = [f for f in ir_dir.glob("*") if f.is_file() and f.stat().st_size > _MAX_IR_MEMBER_BYTES]
check(not oversized_stored, "no oversized IR file was ever written to the IR store")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
