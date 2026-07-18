"""Round-trip and model tests for LoudnessCorrection support.

Run:  python tests/selftest_loudness_correction.py
Covers parsing the APO LoudnessCorrection command into a typed, ordered
token-pair model (get/set/enabled), verbatim round-trip (including unknown
keys and their original order), edits re-rendering correctly, absence
rendering nothing, and malformed lines falling back to extra_lines. Also
covers the Advanced-panel `LoudnessEditor` UI round-trip (offscreen Qt), the
real APO-adoption import path (ProfileManager.sync_apo_imports ->
import_apo_candidates, the same path behind the boot-reconcile "Import
Equalizer APO configuration" dialog and Settings -> Import), and the AI
profile-generation guide surfacing the command's syntax.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from rumble.core import apo_config
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

theme = get_theme(DEFAULT_THEME_NAME)

SAMPLE = "LoudnessCorrection: State 1 ReferenceLevel -23 ReferenceOffset 5.5"


def test_parse_basic():
    cfg = apo_config.parse_config(
        f"Preamp: -3 dB\n{SAMPLE}\nFilter 1: ON PK Fc 1000.0 Hz Gain 2.0 dB Q 1.00\n")
    lc = cfg.loudness_correction
    assert lc is not None
    assert lc.enabled is True
    assert lc.get("ReferenceLevel") == "-23"
    assert lc.get("ReferenceOffset") == "5.5"
    assert SAMPLE not in "\n".join(cfg.extra_lines)   # promoted out of extra_lines


def test_render_roundtrip_verbatim():
    cfg = apo_config.parse_config(SAMPLE + "\n")
    assert SAMPLE in apo_config.render_config_body(cfg)


def test_unknown_keys_survive():
    line = "LoudnessCorrection: State 0 FutureKnob 7 ReferenceLevel -20"
    cfg = apo_config.parse_config(line + "\n")
    assert cfg.loudness_correction.enabled is False
    assert line in apo_config.render_config_body(cfg)  # order + unknown key preserved


def test_edit_and_render():
    cfg = apo_config.parse_config(SAMPLE + "\n")
    cfg.loudness_correction.set("ReferenceLevel", "-20")
    cfg.loudness_correction.enabled = False
    out = apo_config.render_config_body(cfg)
    assert "ReferenceLevel -20" in out and "State 0" in out


def test_absent_means_none():
    cfg = apo_config.parse_config("Preamp: -3 dB\n")
    assert cfg.loudness_correction is None
    assert "LoudnessCorrection" not in apo_config.render_config_body(cfg)


def test_malformed_falls_back_to_extra_lines():
    bad = "LoudnessCorrection:"   # no tokens at all
    cfg = apo_config.parse_config(bad + "\n")
    assert cfg.loudness_correction is None
    assert bad in cfg.extra_lines


def test_malformed_token_count_falls_back_to_extra_lines():
    bad = "LoudnessCorrection: State 1 Extra"   # regex matches, but odd token count (3)
    cfg = apo_config.parse_config(bad + "\n")
    assert cfg.loudness_correction is None
    assert bad in cfg.extra_lines


def test_ui_roundtrip():
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    cfg = apo_config.parse_config(SAMPLE + "\n")
    panel.load_from_config(cfg)
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    assert out.loudness_correction is not None
    assert out.loudness_correction.get("ReferenceLevel") == "-23"


def test_ui_disabled_when_absent():
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    panel.load_from_config(apo_config.ProfileConfig())
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    assert out.loudness_correction is None             # toggle off → no block emitted


def test_ui_toggle_off_keeps_existing_block():
    """Toggling off a pre-existing block keeps it (disabled), never deletes it."""
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    cfg = apo_config.parse_config(SAMPLE + "\n")
    panel.load_from_config(cfg)
    panel.loudness.btn_toggle.setChecked(False)
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    assert out.loudness_correction is not None
    assert out.loudness_correction.enabled is False


def test_ui_load_apply_noop_when_untouched():
    """Loading a block with only State (no ReferenceLevel/ReferenceOffset)
    and rebuilding without touching the spin boxes must not inject default
    keys the block never had — see docs/superpowers/plans/phase1-errata.md #3."""
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    line = "LoudnessCorrection: state 0"
    cfg = apo_config.parse_config(line + "\n")
    panel.load_from_config(cfg)
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    assert out.loudness_correction.to_line() == line, out.loudness_correction.to_line()


def test_ui_apply_untouched_preserves_precision():
    """Loading a block with high-precision ReferenceLevel/ReferenceOffset
    values and rebuilding without touching either spin box must preserve
    the ORIGINAL string values exactly — not the spin boxes' decimals(1)
    rounding. Value-comparison (loaded float vs current spin value) is not
    a reliable "untouched" signal because the spin box can't represent the
    original precision in the first place; see
    docs/superpowers/plans/phase1-errata.md #3."""
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    line = "LoudnessCorrection: State 1 ReferenceLevel -10.567 ReferenceOffset 5.02"
    cfg = apo_config.parse_config(line + "\n")
    panel.load_from_config(cfg)
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    assert out.loudness_correction.get("ReferenceLevel") == "-10.567"
    assert out.loudness_correction.get("ReferenceOffset") == "5.02"
    assert out.loudness_correction.to_line() == line, out.loudness_correction.to_line()


def test_ui_apply_touched_field_updates_only_that_field():
    """Actually editing one spin box updates that field (with the spin
    box's own precision) while the untouched field still preserves its
    original exact string — proving the touched-state fix doesn't just
    freeze the block, only the fields the user didn't interact with."""
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    line = "LoudnessCorrection: State 1 ReferenceLevel -10.567 ReferenceOffset 5.02"
    cfg = apo_config.parse_config(line + "\n")
    panel.load_from_config(cfg)
    panel.loudness.level_spin.setValue(-12.3)   # real user edit
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    assert out.loudness_correction.get("ReferenceLevel") == "-12.3"
    assert out.loudness_correction.get("ReferenceOffset") == "5.02"   # untouched, preserved


def test_ui_preserves_unknown_keys():
    """Loading a block with an unrecognised key (e.g. Attenuation) must not
    rebuild tokens from scratch — the extra key survives apply_to_config."""
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    line = "LoudnessCorrection: State 1 ReferenceLevel -23 ReferenceOffset 5.5 Attenuation 1.0"
    cfg = apo_config.parse_config(line + "\n")
    panel.load_from_config(cfg)
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    assert out.loudness_correction.get("Attenuation") == "1.0"


def test_apo_adoption_import_preserves_loudness():
    """Feed a config carrying a LoudnessCorrection line through the real
    APO-adoption import path — ProfileManager.sync_apo_imports() ->
    import_apo_candidates(), the exact path `rumble/__main__.py`'s
    `_run_apo_adoption` (boot reconcile) and Settings -> Import both use, via
    `ApoImportDialog` (rumble/ui/apo_import_dialog.py). Confirms the block
    survives the whole pipeline: discovery, parse, profile creation, and the
    canonical file written to disk."""
    from rumble.core.profile_manager import ProfileManager

    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_loudness_import_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    cfg_dir = manager.config_path
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # A lone LoudnessCorrection line has no "real EQ" (no filters/GraphicEQ),
    # so it alone would not be offered for import (see profile_manager._has_real_eq)
    # — pair it with a real filter, exactly like test_parse_basic's fixture.
    (cfg_dir / "config.txt").write_text("Preamp: -6 dB\n", encoding="utf-8")
    text = (
        "Preamp: -3 dB\n"
        f"{SAMPLE}\n"
        "Filter 1: ON PK Fc 1000.0 Hz Gain 2.0 dB Q 1.00\n"
    )
    (cfg_dir / "myeq.txt").write_text(text, encoding="utf-8")
    manager.ensure_first_run()

    candidates = manager.sync_apo_imports()
    assert [c.source for c in candidates] == ["myeq.txt"], candidates

    created = manager.import_apo_candidates(candidates)
    assert len(created) == 1

    loaded = manager.load_profile_config(created[0].id)
    assert loaded.loudness_correction is not None
    assert loaded.loudness_correction.enabled is True
    assert loaded.loudness_correction.get("ReferenceLevel") == "-23"
    assert loaded.loudness_correction.get("ReferenceOffset") == "5.5"

    # Raw file on disk round-trips the command line byte-identically.
    raw = (cfg_dir / created[0].file).read_text(encoding="utf-8")
    assert SAMPLE in raw, raw


def test_ui_fresh_enable_writes_full_editor_exe_defaults():
    """Enabling loudness correction on a profile that never had a block must
    write the same 4-key default line APO's own Editor.exe writes the first
    time a user turns loudness correction on: State, ReferenceLevel,
    ReferenceOffset, AND Attenuation — see
    docs/superpowers/plans/phase1-grammar-notes.md. Omitting Attenuation
    would leave APO to apply its own (unknown/undocumented) default, which
    may not match "no attenuation" (1.0)."""
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    panel.load_from_config(apo_config.ProfileConfig())   # no pre-existing block
    panel.loudness.btn_toggle.setChecked(True)
    panel.loudness.level_spin.setValue(-18.0)
    panel.loudness.offset_spin.setValue(2.0)
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    lc = out.loudness_correction
    assert lc is not None
    assert lc.get("State") == "1"
    assert lc.get("ReferenceLevel") == "-18"
    assert lc.get("ReferenceOffset") == "2"
    assert lc.get("Attenuation") == "1.0", lc.to_line()


def test_ui_untouched_toggle_preserves_nonnumeric_state():
    """An untouched toggle on a pre-existing block must not renormalize a
    non-numeric State value (e.g. "on") to "1"/"0" — only writing State when
    the user actually touched the toggle keeps the same byte-preservation
    guarantee already given to ReferenceLevel/ReferenceOffset."""
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    line = "LoudnessCorrection: State on ReferenceLevel -10 ReferenceOffset 2"
    cfg = apo_config.parse_config(line + "\n")
    panel.load_from_config(cfg)
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)               # toggle never touched
    assert out.loudness_correction.to_line() == line, out.loudness_correction.to_line()


def test_ui_touched_toggle_still_writes_state():
    """Actually clicking the toggle still normalizes State the usual way —
    proving the touched-state fix only freezes State when truly untouched."""
    from rumble.ui.advanced_panel import AdvancedPanel
    panel = AdvancedPanel(theme)
    line = "LoudnessCorrection: State on ReferenceLevel -10 ReferenceOffset 2"
    cfg = apo_config.parse_config(line + "\n")
    panel.load_from_config(cfg)
    panel.loudness.btn_toggle.setChecked(False)   # real user edit (was True)
    out = apo_config.ProfileConfig()
    panel.apply_to_config(out)
    assert out.loudness_correction.get("State") == "0", out.loudness_correction.to_line()


def test_duplicate_loudness_lines_first_wins_second_preserved():
    """A malformed/hand-edited config with two LoudnessCorrection: lines must
    keep the FIRST one as the structured block and preserve the second one
    verbatim in extra_lines, rather than silently overwriting (and losing)
    the first."""
    first = "LoudnessCorrection: State 1 ReferenceLevel -10 ReferenceOffset 2"
    second = "LoudnessCorrection: State 0 ReferenceLevel -99 ReferenceOffset 99"
    cfg = apo_config.parse_config(f"{first}\n{second}\n")
    assert cfg.loudness_correction is not None
    assert cfg.loudness_correction.get("ReferenceLevel") == "-10"
    assert second in cfg.extra_lines, cfg.extra_lines


def test_ai_guide_mentions_loudness_correction():
    """The AI profile-generation guide page (About -> AI Profiles) must
    surface LoudnessCorrection's syntax so an AI (or a human) authoring a
    profile by hand knows it exists and where RUMBLE exposes it for editing."""
    from rumble.ui.ai_profiles_dialog import _EXTRA_SYNTAX

    entry = next((s for s in _EXTRA_SYNTAX if s["command"] == "LoudnessCorrection"), None)
    assert entry is not None, "_EXTRA_SYNTAX has no LoudnessCorrection entry"
    assert entry["example"].startswith("LoudnessCorrection:")
    assert "State" in entry["example"]
    assert "Advanced" in entry["editor"] and "Loudness" in entry["editor"]
    # low-volume/night-listening guidance, per the plan
    low_volume_language = ("volume", "quiet", "night", "low")
    assert any(word in entry["use"].lower() for word in low_volume_language), entry["use"]


def test_ai_guide_page_builds_with_extra_syntax_card():
    """Smoke-test: the guide page actually constructs (offscreen Qt) with the
    new card wired in — not just that the data list exists unused."""
    from rumble.ui.ai_profiles_dialog import AIPromptGuidePage

    page = AIPromptGuidePage(theme)
    assert page is not None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
