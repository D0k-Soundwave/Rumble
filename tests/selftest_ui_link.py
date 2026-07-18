"""Offscreen UI self-test for channel linking and the New Profile dialog.

Run:  python tools/selftest_ui_link.py
Uses Qt's offscreen platform plugin — no window is shown. Exits non-zero on
the first failure.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QMessageBox

from rumble.core.profile_manager import ProfileMeta, make_flat_config
from rumble.ui.editor_panel import EditorPanel
from rumble.ui.new_profile_dialog import NewProfileDialog
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def main() -> int:
    app = QApplication([])
    theme = get_theme(DEFAULT_THEME_NAME)

    meta = ProfileMeta(id="t", name="Test", created="2026-01-01T00:00:00",
                       modified="2026-01-01T00:00:00")

    # ── 1. linked stereo collapses to one combined strip; delay survives ─
    print("[1] load: linked stereo collapses; delay preservation")
    panel = EditorPanel(theme)
    config = make_flat_config(10, channels=["L", "R"])
    config.channel_sections[0].delay_ms = 3.0
    config.channel_sections[1].delay_ms = 3.0
    panel.load_profile(meta, config, is_active=False, is_bypassed=False)
    check(panel._btn_link_channels.isChecked(), "identical sections load linked")
    check(len(panel._channel_sections) == 1
          and panel._channel_sections[0].channel == "L R",
          "linked L+R collapses to a single combined 'L R' section")
    check(panel._channel_sections[0].delay_ms == 3.0,
          "delay survives the collapse")
    check(panel._section_delay_spin.value() == 3.0,
          "delay row reflects loaded value")

    # ── 2. linked edits land in the combined section ─────────────────
    print("[2] linked edits captured in the combined section")
    panel._filter_rows[0].filter.gain = 5.0
    panel._on_field_changed()
    built = panel.build_config()
    check(len(built.channel_sections) == 1
          and built.channel_sections[0].channel == "L R",
          "build_config emits one combined 'L R' section while linked")
    check(built.channel_sections[0].filters[0].gain == 5.0,
          "gain edit captured in the combined section")

    # delay edit applies while linked
    panel._section_delay_spin.setValue(7.5)
    check(panel._channel_sections[0].delay_ms == 7.5, "delay edit applies while linked")

    # ── 3. unlink → independent edits ────────────────────────────────
    print("[3] unlink allows divergence")
    panel._btn_link_channels.setChecked(False)
    panel._filter_rows[0].filter.gain = -2.0
    panel._on_field_changed()
    check(panel._channel_sections[1].filters[0].gain == 5.0,
          "other channel untouched after unlink")

    # ── 4. relink copies the Left channel to all others ──────────────
    print("[4] relink copies the Left channel everywhere")
    original_question = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    try:
        panel._btn_link_channels.setChecked(True)  # viewing L (idx 0), L = -2.0
    finally:
        QMessageBox.question = original_question
    check(len(panel._channel_sections) == 1
          and panel._channel_sections[0].channel == "L R"
          and panel._channel_sections[0].filters[0].gain == -2.0,
          "relink collapses to one combined section carrying the Left channel")

    # relink must take the Left channel even when a different channel is the
    # one on screen
    panel._btn_link_channels.setChecked(False)
    panel._switch_section(0)
    panel._filter_rows[0].filter.gain = 1.5            # L
    panel._collect_current_section_to(0)
    panel._switch_section(1)
    panel._filter_rows[0].filter.gain = 9.0            # R — and R is now visible
    panel._collect_current_section_to(1)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    try:
        panel._btn_link_channels.setChecked(True)
    finally:
        QMessageBox.question = original_question
    check(len(panel._channel_sections) == 1
          and panel._channel_sections[0].filters[0].gain == 1.5,
          "relink uses the Left channel while viewing R (R's 9.0 discarded)")
    check(panel._filter_rows[0].filter.gain == 1.5,
          "visible strip reloads to the Left channel after relink")

    # declining the confirmation keeps it unlinked
    panel._btn_link_channels.setChecked(False)
    panel._switch_section(0)
    panel._filter_rows[0].filter.gain = 4.0
    panel._collect_current_section_to(0)
    panel._channel_sections[1].filters[0].gain = -7.0  # leave R distinct
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel)
    try:
        panel._btn_link_channels.setChecked(True)
    finally:
        QMessageBox.question = original_question
    check(not panel._btn_link_channels.isChecked(), "cancel keeps channels unlinked")
    check(panel._channel_sections[1].filters[0].gain == -7.0, "cancel leaves other channel untouched")

    # ── 4b. unlinking a single combined section splits it per channel ─
    print("[4b] unlink splits a combined 'all' section into channels")
    combined = EditorPanel(theme)
    all_cfg = make_flat_config(6)  # channels=None → one "all" section
    combined.load_profile(meta, all_cfg, is_active=False, is_bypassed=False)
    check(len(combined._channel_sections) == 1
          and combined._channel_sections[0].channel == "all",
          "combined profile loads as a single 'all' section")
    check(combined._btn_link_channels.isChecked(), "single section loads linked")
    combined._btn_link_channels.setChecked(False)  # unlink → should split
    check([s.channel for s in combined._channel_sections] == ["L", "R"],
          "unlink splits 'all' into separate L and R sections")
    check(len(combined._section_btns) == 2, "two channel tabs appear after unlink")
    # the split channels must be independently editable
    combined._switch_section(1)
    combined._filter_rows[0].filter.gain = 8.0
    combined._collect_current_section_to(1)
    check(combined._channel_sections[0].filters[0].gain == 0.0
          and combined._channel_sections[1].filters[0].gain == 8.0,
          "split channels edit independently")
    # a genuinely single-channel section has nothing to split
    mono = EditorPanel(theme)
    mono_cfg = make_flat_config(4, channels=["L"])
    mono.load_profile(meta, mono_cfg, is_active=False, is_bypassed=False)
    mono._btn_link_channels.setChecked(False)
    check([s.channel for s in mono._channel_sections] == ["L"],
          "single real channel is left intact on unlink")

    # ── 5. diverged profile loads unlinked ───────────────────────────
    print("[5] diverged profile loads unlinked")
    diverged = make_flat_config(5, channels=["L", "R"])
    diverged.channel_sections[1].filters[0].gain = 4.0
    panel.load_profile(meta, diverged, is_active=False, is_bypassed=False)
    check(not panel._btn_link_channels.isChecked(), "diverged sections load unlinked")

    # ── 6. Add Channel while linked clones content ───────────────────
    print("[6] add channel while linked")
    linked_cfg = make_flat_config(6, channels=["L", "R"])
    panel.load_profile(meta, linked_cfg, is_active=False, is_bypassed=False)
    panel._filter_rows[0].filter.gain = 3.5
    panel._on_field_changed()
    # simulate the Add Channel flow without the input dialog
    from rumble.core.apo_config import ChannelSection
    panel._collect_current_section_to(panel._current_section_idx)
    new_section = ChannelSection(channel="C")
    src = panel._channel_sections[panel._current_section_idx]
    if panel._channels_linked():
        new_section.filters = panel._clone_filters(src.filters)
        new_section.graphic_eq = src.graphic_eq
        new_section.delay_ms = src.delay_ms
    panel._channel_sections.append(new_section)
    check(new_section.filters[0].gain == 3.5 and len(new_section.filters) == 6,
          "new channel section starts as a clone")

    # ── 7. New Profile dialog ────────────────────────────────────────
    print("[7] new profile dialog options")
    dlg = NewProfileDialog(theme, default_bands=12)
    check(not dlg._ok_button.isEnabled(), "Create disabled until name entered")
    dlg.name_edit.setText("Cinema 5.1")
    check(dlg._ok_button.isEnabled(), "Create enabled with name")
    dlg.desc_edit.setText("Living room")
    dlg.tags_edit.setText("Movies,  Speakers ,")
    dlg.layout_combo.setCurrentText("5.1 Surround")
    dlg.bands_spin.setValue(8)
    v = dlg.values()
    check(v["name"] == "Cinema 5.1" and v["description"] == "Living room", "name/description collected")
    check(v["tags"] == ["Movies", "Speakers"], "tags parsed")
    check(v["channels"] == ["L", "R", "C", "LFE", "RL", "RR"], "5.1 preset channels")
    from rumble.core.audio_devices import list_apo_devices
    expected_default = (list_apo_devices() or [None])[0]
    expected_default = expected_default.pattern if expected_default else "all"
    check(v["bands"] == 8 and v["device"] == expected_default,
          "bands collected, device defaults to first APO endpoint")
    check(dlg.bands_spin.maximum() == 32 and dlg.bands_spin.minimum() == 1, "band range 1..32")

    layout_items = [dlg.layout_combo.itemText(i) for i in range(dlg.layout_combo.count())]
    check("Stereo / Headphones" in layout_items, "Stereo / Headphones layout present")
    for removed in ("Custom", "Mono (all)", "Headphones", "Stereo"):
        check(removed not in layout_items, f"'{removed}' layout removed")
    dlg.layout_combo.setCurrentText("Stereo / Headphones")
    check(dlg.values()["channels"] == ["L", "R"], "Stereo / Headphones maps to L R")
    check(not hasattr(dlg, "curve_combo"), "no starting-curve preset dropdown")
    check("preset" not in dlg.values(), "values() no longer carries a preset key")

    # dialog defaults
    dlg2 = NewProfileDialog(theme, default_bands=10)
    check(dlg2.layout_combo.currentText() == "Stereo / Headphones",
          "default layout Stereo / Headphones")
    check(dlg2.bands_spin.value() == 10, "default bands from settings")

    # ── 8. device selector lists APO-enabled endpoints ───────────────
    print("[8] device selector enumeration")
    devices = list_apo_devices()
    check(isinstance(devices, list), "list_apo_devices returns a list")
    combo_items = [panel.device_combo.itemText(i) for i in range(panel.device_combo.count())]
    check(combo_items[-1] == "All", "'All' is always last")
    check(panel.device_combo.itemData(panel.device_combo.count() - 1) == "all",
          "'All' item carries the 'all' device value")
    check(combo_items[:-1] == [d.pattern for d in devices],
          "editor combo mirrors APO-enabled endpoints")
    check(not panel.device_combo.isEditable() and not dlg2.device_combo.isEditable(),
          "device combos are not editable")
    # the channel tabs are the single source of truth for the setup
    surround_cfg = make_flat_config(3, channels=["L", "R", "C", "LFE", "RL", "RR"])
    panel.load_profile(meta, surround_cfg, is_active=False, is_bypassed=False)
    tab_names = [b.text() for b in panel._section_btns]
    check(tab_names == ["L", "R", "C", "LFE", "RL", "RR"],
          "channel tabs show the 5.1 setup")
    # renaming a section updates its tab
    panel._channel_sections[0].channel = "SL SR"
    panel._rebuild_section_bar()
    check(panel._section_btns[0].text() == "SL SR",
          "renamed channel reflected in its tab")
    dlg_items = [dlg2.device_combo.itemText(i) for i in range(dlg2.device_combo.count())]
    check(dlg_items == combo_items, "dialog combo matches editor combo")
    for d in devices:
        check(bool(d.pattern.strip()), f"endpoint has non-empty pattern: {d.label}")
    if devices:
        print(f"        detected: {', '.join(d.label for d in devices)}")

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
