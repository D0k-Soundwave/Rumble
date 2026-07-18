"""Self-test for screen-reader label coverage.

Run:  python tools/selftest_a11y_labels.py

Builds the whole UI (main window + the on-demand dialogs) offscreen and checks
that every app-defined interactive control exposes a non-empty accessible name —
either set explicitly via ui.a11y.describe or derived from visible button text.
Controls with no text and no accessible name are invisible to screen readers, so
any such control fails the test. Qt-internal widgets (spin/combo/scrollbar parts)
are excluded. Exits non-zero on failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QListWidget,
    QMessageBox, QPlainTextEdit, QScrollBar, QSlider, QToolButton, QTreeWidget,
    QWidget,
)

app = QApplication([])

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)

from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

# Widget types that carry no intrinsic visible text and therefore must be given
# an explicit accessible name to be usable with a screen reader.
NEEDS_NAME = (QComboBox, QSlider, QListWidget, QTreeWidget, QPlainTextEdit)

MISSING: list[str] = []
_seen: set[int] = set()


def _ancestor_is(w: QWidget, types: tuple) -> bool:
    node = w.parent()
    depth = 0
    while node is not None and depth < 8:
        if isinstance(node, types):
            return True
        node = node.parent() if hasattr(node, "parent") else None
        depth += 1
    return False


def _internal(w: QWidget) -> bool:
    """Qt-internal sub-widgets (spinbox/combo/scrollbar parts) we must not flag."""
    if w.objectName().startswith("qt_"):
        return True
    if isinstance(w, QToolButton):
        return True
    return _ancestor_is(w, (QAbstractSpinBox, QComboBox, QScrollBar))


def _path(w: QWidget) -> str:
    parts, node, depth = [], w, 0
    while node is not None and depth < 6:
        parts.append(type(node).__name__)
        node = node.parent() if hasattr(node, "parent") else None
        depth += 1
    return " < ".join(parts)


def _label(w: QWidget) -> str:
    name = w.accessibleName()
    if name:
        return name
    if isinstance(w, QAbstractButton):
        return w.text()
    return ""


def audit(w: QWidget) -> None:
    if id(w) in _seen or _internal(w):
        return
    candidate = isinstance(w, NEEDS_NAME) or (
        isinstance(w, QAbstractButton) and not w.text())
    if not candidate:
        return
    _seen.add(id(w))
    if not _label(w):
        MISSING.append(f"{type(w).__name__}  [{_path(w)}]")


def walk(root: QWidget) -> None:
    for child in root.findChildren(QWidget):
        audit(child)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_a11y_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Label A", config=make_flat_config(10, channels=["L", "R"]))

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)

    w = MainWindow(manager, theme_engine, QIcon())
    w._open_profile("label_a")
    ep = w.editor_panel
    ep._switch_main_tab(1)
    ep._switch_main_tab(2)
    ep._switch_main_tab(0)
    print("=== Main window ===")
    walk(w)

    from rumble.ui.new_profile_dialog import NewProfileDialog
    walk(NewProfileDialog(get_theme(DEFAULT_THEME_NAME), default_bands=10, parent=w))
    print("=== NewProfileDialog walked ===")

    from rumble.ui.settings_dialog import SettingsDialog
    walk(SettingsDialog(manager.settings, get_theme(DEFAULT_THEME_NAME),
                        tmp / "data", parent=w))
    print("=== SettingsDialog walked ===")

    if MISSING:
        print(f"\n{len(MISSING)} interactive control(s) with NO accessible name:")
        for m in MISSING:
            print("  " + m)
        return 1
    print("\nAll interactive controls have accessible names.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
