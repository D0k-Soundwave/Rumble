"""Self-test: selecting a profile must not scroll the tree, and the active
row must show no accent bars — only the normal selection highlight.

Drives the real path: setCurrentItem fires itemSelectionChanged ->
profile_selected -> MainWindow._on_profile_selected -> _open_profile (opens it
in the editor; the tree rebuild that follows preserves the scroll position).

Run:  python tools/selftest_no_scroll_on_select.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication

from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

failures: list[str] = []


def expect(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        failures.append(label)


def first_visible_profile(panel):
    """A (profile_id, item) pair currently visible inside the tree viewport."""
    vp = panel.tree.viewport().rect()
    for pid, item in panel._item_by_id.items():
        rect = panel.tree.visualItemRect(item)
        if rect.height() > 0 and vp.intersects(rect):
            return pid, item
    return None, None


def main() -> int:
    app = QApplication([])
    for font_file in (Path(__file__).resolve().parents[1] / "assets" / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(font_file))
    engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    engine.apply(app)

    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_"))
    m = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    m.ensure_first_run()
    m.settings["show_safety_warning"] = False  # modal box would block the test
    # The built-in library is empty now, so create enough user profiles to
    # overflow the viewport (under one tag so they nest in a single branch).
    for i in range(24):
        m.create_profile(f"Profile {i:02d}", tags=["Test"],
                         config=make_flat_config(8, channels=["L", "R"]))

    w = MainWindow(m, engine, QIcon())
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    w.move(-4000, 0)
    w.show()
    app.processEvents()

    panel = w.profile_panel
    panel.tree.expandAll()  # open every tag branch so the profile rows show
    app.processEvents()

    bar = panel.tree.verticalScrollBar()
    expect(len(panel._item_by_id) >= 20, f"enough profiles to scroll (count={len(panel._item_by_id)})")
    expect(bar.maximum() > 0, f"tree overflows the viewport (max={bar.maximum()})")

    # Scroll partway down, then select a row visible at that position.
    bar.setValue(bar.maximum() // 2)
    app.processEvents()
    target_id, target_item = first_visible_profile(panel)
    expect(target_item is not None, "found a visible profile row to select")
    before = bar.value()

    panel.tree.setCurrentItem(target_item)  # fires the real selection->activate chain
    app.processEvents()

    expect(bar.value() == before,
           f"scroll position unchanged after select ({before} -> {bar.value()})")
    expect(panel.selected_profile_id() == target_id,
           "clicked profile is still the selected one after rebuild")
    expect(w._loaded_profile_id == target_id,
           "clicked profile is opened in the editor")

    # The active row widget must carry no accent border styling.
    widget = panel._row_widgets[target_id]
    expect("border-left" not in widget.styleSheet(),
           f"no accent bar styling on active row (qss='{widget.styleSheet()}')")

    # Select another visible row — again, no scrolling.
    bar.setValue(bar.maximum() // 2)
    app.processEvents()
    other_id, other_item = first_visible_profile(panel)
    if other_id == target_id:  # pick a different one if the first is still on top
        for pid, item in panel._item_by_id.items():
            rect = panel.tree.visualItemRect(item)
            if pid != target_id and rect.height() > 0 and panel.tree.viewport().rect().intersects(rect):
                other_id, other_item = pid, item
                break
    before = bar.value()
    panel.tree.setCurrentItem(other_item)
    app.processEvents()
    expect(bar.value() == before,
           f"scroll position unchanged on second select ({before} -> {bar.value()})")
    expect(panel.selected_profile_id() == other_id, "second selection stuck")

    panel.grab().save(str(Path(__file__).parent / "selftest_no_scroll_panel.png"))
    print(f"panel grab saved (scroll={bar.value()}, selected={other_id})", flush=True)

    print(("ALL PASS" if not failures else f"{len(failures)} FAILURE(S)"), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
