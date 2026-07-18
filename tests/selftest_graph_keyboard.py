"""Self-test for keyboard operability of the EQ response graph.

Run:  python tools/selftest_graph_keyboard.py

Drives ui.eq_graph.EQGraph entirely from the keyboard and asserts that the same
band_dragged / band_selected signals a mouse drag uses fire with the right
index and gain, that gains clamp to the visible scale, fine steps work, and
no-gain filters are skipped. Exits non-zero on failure.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

from rumble.core.apo_config import Filter
from rumble.ui.eq_graph import EQGraph, install_accessibility
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def main() -> int:
    install_accessibility()  # must not raise
    graph = EQGraph(get_theme(DEFAULT_THEME_NAME))

    # Three peaking bands (gain-capable) plus a high-pass (no gain) to confirm
    # the no-gain branch is skipped for gain edits.
    filters = [
        Filter(type="PK", fc=100, gain=0.0),
        Filter(type="PK", fc=1000, gain=2.0),
        Filter(type="PK", fc=8000, gain=-3.0),
        Filter(type="HP", fc=30, gain=0.0),
    ]
    graph.set_filters(filters)

    # Emulate the editor: band_dragged mutates the shared Filter in place and
    # re-highlights, exactly like EditorPanel._on_band_dragged.
    def editor_drag(i, fc, gain):
        filters[i].fc = fc
        if filters[i].supports_gain():
            filters[i].gain = gain
        graph.highlight_band(i)

    dragged: list[tuple[int, float]] = []
    selected: list[int] = []
    graph.band_dragged.connect(lambda i, fc, g: dragged.append((i, round(g, 2))))
    graph.band_dragged.connect(editor_drag)
    graph.band_selected.connect(lambda i: selected.append(i))
    graph.band_selected.connect(graph.highlight_band)

    def press(key, mod=Qt.KeyboardModifier.NoModifier):
        graph.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mod))

    print("[1] focus selects a band")
    graph.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
    check(graph._highlighted_index == 0, "first band selected on focus")

    print("\n[2] Left/Right move the selection (skipping the no-gain band only "
          "if it had no handle — here HP has a handle, so it is navigable)")
    press(Qt.Key.Key_Right)
    check(graph._highlighted_index == 1, "Right moves to band 2")
    press(Qt.Key.Key_Left)
    check(graph._highlighted_index == 0, "Left moves back to band 1")

    print("\n[3] Up/Down adjust gain via band_dragged")
    graph.band_selected.emit(1)        # select band 2 (gain 2.0)
    press(Qt.Key.Key_Up)
    press(Qt.Key.Key_Up)
    check(abs(filters[1].gain - 3.0) < 1e-6, "two Up = +1.0 dB (0.5 step)")
    press(Qt.Key.Key_Down)
    check(abs(filters[1].gain - 2.5) < 1e-6, "Down = -0.5 dB")
    press(Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    check(abs(filters[1].gain - 2.6) < 1e-6, "Shift+Up = +0.1 dB fine step")

    print("\n[4] Home/End clamp to the visible scale")
    press(Qt.Key.Key_End)
    check(abs(filters[1].gain - graph._y_limit) < 1e-6, "End = +limit")
    press(Qt.Key.Key_Up)               # cannot exceed the limit
    check(abs(filters[1].gain - graph._y_limit) < 1e-6, "Up past limit clamps")
    press(Qt.Key.Key_Home)
    check(abs(filters[1].gain + graph._y_limit) < 1e-6, "Home = -limit")

    print("\n[5] no-gain filter is not gain-edited")
    # Navigate to the HP band (index 3) and try to change gain.
    graph.band_selected.emit(3)
    before = filters[3].gain
    dragged.clear()
    press(Qt.Key.Key_Up)
    check(filters[3].gain == before, "HP gain unchanged by Up")
    check(dragged == [], "no band_dragged emitted for no-gain band")

    print("\n[6] value text reflects the selected band")
    graph.band_selected.emit(0)
    txt = graph._band_value_text()
    check("Band 1" in txt and "hertz" in txt, f"value text sane: {txt!r}")

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
