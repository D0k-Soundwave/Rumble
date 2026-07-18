"""Self-test: non-standard channel-layout notice (Rumble 6.4 follow-up).

Equalizer APO's own official "Channel configurations" table has no row for
RUMBLE's "Stereo + Sub (2.1)" / "Surround + Sub (4.1)" presets (see the
CHANNEL_PRESETS comment in editor_panel.py and
docs/RUMBLE_APP_SPECIFIC_KB.md §8.1.1) — on hardware APO doesn't recognize
under one of its six named configurations, the LFE identifier specifically
may not resolve to any channel. Rather than silently renaming those two
presets' channels to numeric indices (which would break other features that
pattern-match the literal "L"/"R"/"LFE" strings — multi-listener correction,
the LFE-only-bass-filter warning, time_alignment's STANDARD_CHANNEL_ORDER),
RUMBLE surfaces a one-time notice at the point of selection instead. This
drives both surfaces: EditorPanel's preset combo (existing profile) and
NewProfileDialog's inline hint (new profile).

Run:  python tests/selftest_channel_layout_warning.py
Uses Qt's offscreen platform plugin — no window is shown."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication([])

from rumble.core.apo_config import ChannelSection, Filter, ProfileConfig
from rumble.core.profile_manager import ProfileMeta
from rumble.ui.editor_panel import (
    CHANNEL_PRESETS, NON_STANDARD_CHANNEL_LAYOUT_WARNING,
    NON_STANDARD_CHANNEL_LAYOUTS, EditorPanel,
)
from rumble.ui.new_profile_dialog import FLAT_HINT, NewProfileDialog
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


theme = ThemeEngine(get_theme(DEFAULT_THEME_NAME)).t


def test_non_standard_set_matches_documented_gap() -> None:
    expect(NON_STANDARD_CHANNEL_LAYOUTS == {"Stereo + Sub (2.1)", "Surround + Sub (4.1)"},
           f"unexpected set: {NON_STANDARD_CHANNEL_LAYOUTS}")
    for name in NON_STANDARD_CHANNEL_LAYOUTS:
        expect(name in CHANNEL_PRESETS, f"{name} must still be a real preset")
    text = NON_STANDARD_CHANNEL_LAYOUT_WARNING.format(name="Stereo + Sub (2.1)")
    expect("Stereo + Sub (2.1)" in text, "warning text must name the preset")
    expect("LFE" in text, "warning text must mention the at-risk channel")


def test_editor_panel_notices_on_select() -> None:
    calls: list[str] = []
    original = QMessageBox.information
    QMessageBox.information = staticmethod(
        lambda *a, **k: calls.append(a[2] if len(a) > 2 else k.get("text", "")))
    try:
        panel = EditorPanel(theme)
        meta = ProfileMeta(id="t", name="Test", description="", tags=[])
        cfg = ProfileConfig(device="all", preamp=0.0,
                             channel_sections=[ChannelSection("L R", [Filter(type="PK", fc=1000.0, gain=0.0, q=1.41)])])
        panel.load_profile(meta, cfg, is_active=False, is_bypassed=False)
        app.processEvents()

        panel._preset_combo.setCurrentText("Stereo + Sub (2.1)")
        app.processEvents()
        expect(len(calls) == 1, f"expected exactly one notice, got {len(calls)}")
        expect("Stereo + Sub (2.1)" in calls[0], "notice must name the selected preset")

        calls.clear()
        panel._preset_combo.setCurrentText("5.1 Surround")
        app.processEvents()
        expect(len(calls) == 0, f"standard preset must not notice, got {calls}")
    finally:
        QMessageBox.information = original


def test_new_profile_dialog_hint_updates() -> None:
    dlg = NewProfileDialog(theme, default_bands=10, manager=None)
    try:
        expect(dlg._hint.text() == FLAT_HINT, "default layout must show the plain hint")

        dlg.layout_combo.setCurrentText("Surround + Sub (4.1)")
        app.processEvents()
        expect(FLAT_HINT in dlg._hint.text(), "hint must keep the base flat-start text")
        expect("Surround + Sub (4.1)" in dlg._hint.text(), "hint must name the layout")
        expect("LFE" in dlg._hint.text(), "hint must mention the at-risk channel")

        dlg.layout_combo.setCurrentText("Stereo / Headphones")
        app.processEvents()
        expect(dlg._hint.text() == FLAT_HINT, "reverting to a standard layout must drop the notice")
    finally:
        dlg.deleteLater()


step("non-standard set matches the documented APO-table gap", test_non_standard_set_matches_documented_gap)
step("EditorPanel notices only on 2.1/4.1 preset selection", test_editor_panel_notices_on_select)
step("NewProfileDialog hint reflects the selected layout", test_new_profile_dialog_hint_updates)

print("-" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("ALL TESTS PASSED")
