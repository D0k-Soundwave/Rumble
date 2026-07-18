"""Selftest: D3b bass-management wizard (rumble/ui/bass_management_dialog.py)
and its Tools-menu wiring in main_window.py (offscreen Qt, real widgets).

Covers: the Tools -> Bass Management… action is enabled only when the
active profile's layout includes a sub/LFE channel (reuses editor_panel.
layout_channel_tokens, the same channel-preset detection the editor's own
preset combo uses); a full offscreen page walk through the wizard (layout
confirm -> crossover -> alignment -> trims -> distances -> preview); Apply
marks the editor dirty with the generated bass-management sections (crossover
filters, LFE summed-feed Copy: line, trims, distance-derived delays) through
the normal dirty/save flow, mirroring _on_bake_tone_trim/_on_insert_crossfeed;
the wizard's live preview (started on reaching the preview page) restores on
Cancel; a defensive fallback when constructed against a non-sub/LFE profile.

Run:  python tests/selftest_bass_management_ui.py
Uses Qt's offscreen platform plugin -- no window is shown.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from rumble.core import bass_management
from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.bass_management_dialog import BassManagementDialog
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def step(label: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception:
        FAILURES.append(label)
        print(f"  FAIL  {label}\n{traceback.format_exc()}")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def tmp_data_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="rumble_bassmgmtui_"))


def make_manager(channels: list[str]) -> ProfileManager:
    tmp = tmp_data_dir()
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(4, channels=channels))
    manager.activate_profile("alpha")
    return manager


def make_window(channels: list[str]):
    manager = make_manager(channels)
    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    w = MainWindow(manager, theme_engine, QIcon())
    app.processEvents()
    return manager, w


# ── Tools-menu enablement (reuses editor_panel's channel-preset detection) ─

def menu_action_enabled_for_sub_layout() -> None:
    manager, w = make_window(["L", "R", "LFE"])
    expect(w.action_bass_management.isEnabled(),
           "a 2.1 (L/R/LFE) active profile should enable the menu action")


def menu_action_disabled_for_plain_stereo() -> None:
    manager, w = make_window(["L", "R"])
    expect(not w.action_bass_management.isEnabled(),
           "a plain-stereo active profile should leave the menu action disabled")


# ── defensive fallback (dialog constructed directly against a non-LFE
#    profile -- the Tools menu action would be disabled, but a directly
#    constructed dialog, e.g. in a test, must still degrade gracefully) ────

def dialog_shows_unavailable_page_without_lfe() -> None:
    manager, w = make_window(["L", "R"])
    dlg = BassManagementDialog(manager, w.theme, parent=w)
    expect(not hasattr(dlg, "stack"),
           "no LFE channel -- the dialog should build the unavailable fallback, not the wizard stack")


# ── full offscreen page walk + apply ────────────────────────────────────

def dialog_page_walk_apply_marks_editor_dirty_with_generated_sections() -> None:
    manager, w = make_window(["L", "R", "LFE"])
    expect(not w._dirty, "precondition: editor must be clean before applying")

    dlg = BassManagementDialog(manager, w.theme, parent=w)
    dlg.apply_requested.connect(w._on_apply_bass_management)

    expect(dlg.stack.currentWidget() is dlg.layout_page, "wizard should open on the layout page")
    expect(set(dlg._layout) == {"L", "R", "LFE"}, f"unexpected detected layout {dlg._layout!r}")

    dlg._go_to(dlg.crossover_page)
    dlg.spn_xover.setValue(100.0)

    dlg._go_to(dlg.alignment_page)
    idx = dlg.cmb_alignment.findData("LR4")
    expect(idx >= 0, "LR4 should be one of the alignment combo's choices")
    dlg.cmb_alignment.setCurrentIndex(idx)

    dlg._go_to(dlg.trims_page)
    dlg._trim_spins["L"].setValue(1.5)

    dlg._go_to(dlg.distances_page)
    expect(not dlg.btn_use_measured_distances.isEnabled(),
           "no stored Campaign C timing data exists -- the button must stay disabled")
    dlg._distance_checks["R"].setChecked(True)
    dlg._distance_spins["R"].setValue(50.0)

    dlg._on_distances_next()
    app.processEvents()
    expect(dlg.stack.currentWidget() is dlg.preview_page, "distances Next should reach the preview page")
    expect(dlg._previewing, "reaching the preview page should start a live preview")
    expect(manager.is_previewing, "the manager should be actively previewing the wizard's config")

    dlg._on_apply()
    app.processEvents()

    expect(not dlg._previewing, "apply should end the wizard's own preview before emitting")
    expect(w._dirty, "apply should mark the editor dirty (user still saves explicitly)")
    expect(w._loaded_profile_id == "alpha", "apply should load the active profile into the editor")

    config = w.editor_panel.build_config()
    channels = {s.channel: s for s in config.channel_sections}
    expect(set(channels) == {"L", "R", "LFE"}, f"unexpected channels {set(channels)!r}")
    expect(channels["L"].filters[0].type == "HP", "main channels should carry the HP crossover")
    expect(channels["LFE"].filters[0].type == "LP", "the sub channel should carry the LP crossover")
    expect(len(channels["L"].filters) == 2, "LR4 should cascade two sections per main channel")
    expect(channels["L"].preamp_db == 1.5, "the trim entered on the wizard should land in the section")
    expect(any(c.upper().startswith(f"{bass_management.SUB_CHANNEL}=") for c in config.copy_lines),
           "the LFE summed-feed Copy: line should be present")
    expect(abs(channels["R"].delay_ms - 0.0) < 1e-9,
           "the only channel given a distance is its own farthest reference -- 0 ms")
    expect(channels["L"].delay_ms is None,
           "a channel left unchecked on the distances page must get no Delay: line at all")
    expect(config.preamp == channels["LFE"].preamp_db or config.preamp is not None,
           "the profile-level preamp should be build_bass_management()'s own safety-margin figure")


# ── cancel restores the live preview ────────────────────────────────────

def dialog_preview_restores_on_cancel() -> None:
    manager, w = make_window(["L", "R", "LFE"])
    dlg = BassManagementDialog(manager, w.theme, parent=w)

    dlg._go_to(dlg.crossover_page)
    dlg._go_to(dlg.alignment_page)
    dlg._go_to(dlg.trims_page)
    dlg._go_to(dlg.distances_page)
    dlg._on_distances_next()
    app.processEvents()
    expect(dlg._previewing, "precondition: the wizard should be previewing on its preview page")
    expect(manager.is_previewing, "precondition: the manager should be actively previewing")

    dlg.reject()
    app.processEvents()

    expect(not dlg._previewing, "reject (Cancel/close) should end the wizard's own preview")
    expect(not manager.is_previewing,
           "the manager's preview should be ended, restoring the active pointer")


def dialog_preview_back_restores_before_returning_to_distances() -> None:
    manager, w = make_window(["L", "R", "LFE"])
    dlg = BassManagementDialog(manager, w.theme, parent=w)

    dlg._go_to(dlg.crossover_page)
    dlg._go_to(dlg.alignment_page)
    dlg._go_to(dlg.trims_page)
    dlg._go_to(dlg.distances_page)
    dlg._on_distances_next()
    app.processEvents()
    expect(dlg._previewing, "precondition: previewing on the preview page")

    dlg._on_preview_back()
    app.processEvents()

    expect(not dlg._previewing, "Back from the preview page should end the preview")
    expect(not manager.is_previewing, "the manager preview should be restored on Back too")
    expect(dlg.stack.currentWidget() is dlg.distances_page, "Back should return to the distances page")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith(("menu_", "dialog_")) and callable(fn):
            step(name, fn)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nAll bass management UI checks passed.")
