"""Verify the FILTER BANDS channel-setup dropdown reflects a loaded profile's
channel layout, however the profile groups its Channel: blocks.

Run: QT_QPA_PLATFORM=offscreen python tools/verify_channel_preset_detection.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from rumble.core.apo_config import ChannelSection, Filter, ProfileConfig
from rumble.ui.editor_panel import EditorPanel
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme


def _bands(*gains: float) -> list[Filter]:
    freqs = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
    g = list(gains) + [0.0] * (len(freqs) - len(gains))
    return [Filter(type="PK", fc=float(f), gain=float(x), q=1.41) for f, x in zip(freqs, g)]


def _meta():
    from rumble.core.profile_manager import ProfileMeta  # lazy: avoid hard dep at import
    return ProfileMeta(id="t", name="Test", description="", tags=[])


def _case(name: str, sections: list[ChannelSection]) -> tuple[str, str]:
    cfg = ProfileConfig(device="all", preamp=-6.0, channel_sections=sections)
    panel.load_profile(_meta(), cfg, is_active=False, is_bypassed=False)
    return name, panel._preset_combo.currentText()


app = QApplication.instance() or QApplication([])
# EditorPanel needs a theme dict; build it the same way main.py does.
theme = ThemeEngine(get_theme(DEFAULT_THEME_NAME)).t

panel = EditorPanel(theme)

cases = [
    # (label, sections, expected dropdown text)
    ("built-in  'Channel: L R'",        [ChannelSection("L R", _bands(3, 2, 1))],                 "Stereo / Headphones"),
    ("AI/import 'Channel: all'",        [ChannelSection("all", _bands(4, 4, 3))],                 "Stereo / Headphones"),
    ("dialog    'L' + 'R' sections",    [ChannelSection("L", _bands(1)), ChannelSection("R", _bands(1))], "Stereo / Headphones"),
    ("2.1  'L R' + 'LFE'",              [ChannelSection("L R", _bands(1)), ChannelSection("LFE", _bands(5))], "Stereo + Sub (2.1)"),
    ("5.1  combined groups",            [ChannelSection("L R C", _bands(1)), ChannelSection("LFE", _bands(5)), ChannelSection("RL RR", _bands(1))], "5.1 Surround"),
    ("7.1  one per channel",            [ChannelSection(c, _bands(1)) for c in ["L", "R", "C", "LFE", "RL", "RR", "SL", "SR"]], "7.1 Surround"),
    ("unknown  'L' only (mono)",        [ChannelSection("L", _bands(1))],                         ""),
]

print(f"{'CASE':<34} {'EXPECTED':<22} {'GOT':<22} OK")
print("-" * 84)
ok_all = True
for label, sections, expected in cases:
    _, got = _case(label, sections)
    ok = got == expected
    ok_all = ok_all and ok
    print(f"{label:<34} {expected!r:<22} {got!r:<22} {'PASS' if ok else 'FAIL'}")

print("-" * 84)
print("ALL PASS" if ok_all else "SOME FAILED")
sys.exit(0 if ok_all else 1)
