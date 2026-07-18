"""Self-test for keyboard-focus accessibility on the EQ graph.

Run:  python tests/selftest_a11y_focus.py

The EQ response graph is the one keyboard-navigable control, so it must be
focusable (StrongFocus) and carry a visible focus frame. The earlier global
focus outlines on ordinary controls (buttons, lists, tabs) were removed to
restore the baseline look, so this also asserts those are gone. Exits non-zero
on failure.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

app = QApplication([])

from rumble.ui.eq_graph import EQGraph
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import THEMES, get_theme

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def main() -> int:
    print("[1] EQ graph is keyboard-focusable")
    graph = EQGraph(get_theme(next(iter(THEMES))))
    check(graph.focusPolicy() == Qt.FocusPolicy.StrongFocus,
          "EQGraph focusPolicy is StrongFocus")
    check(bool(graph.accessibleName()), "EQGraph has an accessible name")

    print("\n[2] EQ graph keeps a focus frame; ordinary controls have no outline")
    # The EQ graph keeps its accent focus frame; the global button/list/tab
    # focus outlines were removed (baseline look). Verify both per theme.
    removed = ["QPushButton:focus", "QListWidget:focus",
               "QTreeWidget:focus", "QTabBar::tab:focus"]
    for name in THEMES:
        qss = ThemeEngine(get_theme(name)).generate_qss() \
            if hasattr(ThemeEngine, "generate_qss") else \
            ThemeEngine(get_theme(name))._generate_qss()
        has_graph_focus = 'eq-graph-focus"]:focus' in qss
        lingering = [sel for sel in removed if sel in qss]
        check(has_graph_focus and not lingering,
              f"theme '{name}': EQ-graph focus kept, control outlines removed"
              + (f" (lingering {lingering})" if lingering else ""))

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
