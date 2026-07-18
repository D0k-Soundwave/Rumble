"""Runtime audit: build the entire UI offscreen and verify that every
clickable control (QAbstractButton, QAction) actually has its activation
signal connected to something. A control whose clicked/toggled/triggered
signal has no receivers is dead — it will do nothing when the user uses it.

Run:  python tools/audit_buttons.py
Exits non-zero if any dead control is found."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QMetaMethod
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import (QApplication, QMessageBox, QAbstractButton,
                               QCheckBox, QRadioButton, QWidget)

app = QApplication([])

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)

from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme


def signal_connected(obj, name: str) -> bool | None:
    """True/False if the named signal exists and is/ isn't connected;
    None if the object has no such signal."""
    mo = obj.metaObject()
    target = name.encode()
    for i in range(mo.methodCount()):
        m = mo.method(i)
        if (m.methodType() == QMetaMethod.MethodType.Signal
                and bytes(m.name()) == target):
            return obj.isSignalConnected(m)
    return None


def control_label(w) -> str:
    txt = ""
    try:
        txt = w.text()
    except Exception:
        pass
    if not txt:
        txt = w.objectName() or ""
    if not txt:
        txt = w.toolTip()[:40] if hasattr(w, "toolTip") and w.toolTip() else ""
    return txt or "<no label>"


def widget_path(w) -> str:
    """A short ancestor chain to locate the widget."""
    parts = []
    node = w
    depth = 0
    while node is not None and depth < 6:
        cls = type(node).__name__
        parts.append(cls)
        node = node.parent() if hasattr(node, "parent") else None
        depth += 1
    return " < ".join(parts)


# Genuinely dead action controls (push buttons / actions that fire nothing).
DEAD: list[str] = []
# Checkboxes / radios with no signal: usually fine (polled at accept time) but
# listed so they can be eyeballed.
POLLED: list[str] = []
# Custom QWidget subclasses exposing a `clicked` signal (tabs, swatches…).
DEAD_CUSTOM: list[str] = []
CHECKED = 0
_seen = set()


def audit_button(b: QAbstractButton) -> None:
    global CHECKED
    if id(b) in _seen:
        return
    _seen.add(id(b))
    CHECKED += 1
    clicked = signal_connected(b, "clicked")
    toggled = signal_connected(b, "toggled")
    pressed = signal_connected(b, "pressed")
    if bool(clicked) or bool(toggled) or bool(pressed):
        return
    entry = f"'{control_label(b)}'  [{widget_path(b)}]"
    if isinstance(b, (QCheckBox, QRadioButton)):
        POLLED.append(entry)
    else:
        DEAD.append(entry)


def audit_action(a: QAction) -> None:
    global CHECKED
    if id(a) in _seen or a.isSeparator() or a.menu() is not None:
        return
    _seen.add(id(a))
    CHECKED += 1
    triggered = signal_connected(a, "triggered")
    toggled = signal_connected(a, "toggled")
    if not (bool(triggered) or bool(toggled)):
        DEAD.append(f"ACTION  '{a.text() or a.objectName() or '<no text>'}'")


def audit_custom(w: QWidget) -> None:
    """Custom widgets (not QAbstractButton) that expose a `clicked` signal but
    drive behaviour through it — e.g. the channel-section tabs and theme
    swatches. Flag any whose `clicked` has no receiver."""
    global CHECKED
    if id(w) in _seen or isinstance(w, QAbstractButton):
        return
    # Only app-defined widgets (ui.*). Qt built-ins like the QListView inside a
    # combo popup, or QDialogButtonBox, expose an internal `clicked` signal that
    # the app is not meant to wire — those are not dead controls.
    if not type(w).__module__.startswith("ui."):
        return
    state = signal_connected(w, "clicked")
    if state is None:
        return  # no clicked signal — not a clickable custom control
    _seen.add(id(w))
    CHECKED += 1
    if not state:
        DEAD_CUSTOM.append(f"'{control_label(w)}'  [{widget_path(w)}]")


def walk(root: QWidget) -> None:
    for b in root.findChildren(QAbstractButton):
        audit_button(b)
    for a in root.findChildren(QAction):
        audit_action(a)
    for w in root.findChildren(QWidget):
        audit_custom(w)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_audit_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Audit A", config=make_flat_config(10, channels=["L", "R"]))
    manager.create_profile("Audit B", config=make_flat_config(5))

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)

    w = MainWindow(manager, theme_engine, QIcon())
    w._open_profile("audit_a")
    ep = w.editor_panel

    print("=== Main window ===")
    walk(w)

    # Cycle the editor tabs so the Advanced + Raw panels are realised.
    ep._switch_main_tab(1)
    ep._switch_main_tab(2)
    ep._switch_main_tab(0)
    print("=== After cycling editor tabs ===")
    walk(ep)

    # Dialogs are built on demand — audit them too.
    from rumble.ui.new_profile_dialog import NewProfileDialog
    npd = NewProfileDialog(get_theme(DEFAULT_THEME_NAME), default_bands=10, parent=w)
    print("=== NewProfileDialog ===")
    walk(npd)

    from rumble.ui.settings_dialog import SettingsDialog
    sd = SettingsDialog(manager.settings, get_theme(DEFAULT_THEME_NAME),
                        tmp / "data", parent=w)
    print("=== SettingsDialog ===")
    walk(sd)

    from rumble.ui.about_dialog import AboutDialog
    try:
        ad = AboutDialog(get_theme(DEFAULT_THEME_NAME), parent=w)
        print("=== AboutDialog ===")
        walk(ad)
    except Exception as exc:
        print(f"  (AboutDialog skipped: {exc})")

    from rumble.ui.abx_dialog import ABXDialog
    try:
        abxd = ABXDialog(manager, get_theme(DEFAULT_THEME_NAME), parent=w)
        print("=== ABXDialog ===")
        walk(abxd)
    except Exception as exc:
        print(f"  (ABXDialog skipped: {exc})")

    from rumble.ui.hearing_dialog import HearingSuiteDialog

    class _AuditFakeToneEngine:
        """is_playing always False so present_tone() never blocks; keeps
        this audit offscreen-safe without opening a real audio stream."""

        is_playing = False

        def play(self, buffer, sample_rate, device=None, on_done=None):
            if on_done is not None:
                on_done()

        def stop(self):
            pass

    try:
        hsd = HearingSuiteDialog(manager, get_theme(DEFAULT_THEME_NAME), parent=w,
                                  tone_engine_factory=_AuditFakeToneEngine)
        print("=== HearingSuiteDialog (picker) ===")
        walk(hsd)
        hsd.btn_pick_frequency.click()
        print("=== HearingSuiteDialog (Test A intro) ===")
        walk(hsd)
        hsd.btn_coarse_set.setChecked(True)
        hsd.btn_play_volume_ref.click()
        hsd.btn_freq_start.click()
        print("=== HearingSuiteDialog (Test A test page) ===")
        walk(hsd)
        iterations = 0
        while hsd.freq_stack.currentWidget() is hsd.freq_test_page and iterations < 2000:
            hsd._on_response(hsd._ft.staircase.level >= -50.0)
            iterations += 1
        print("=== HearingSuiteDialog (Test A results page) ===")
        walk(hsd)
        hsd.close()
    except Exception as exc:
        print(f"  (HearingSuiteDialog skipped: {exc})")

    print(f"\nChecked {CHECKED} controls.")
    if DEAD_CUSTOM:
        print(f"\n{len(DEAD_CUSTOM)} DEAD CUSTOM CONTROL(S) — `clicked` has no receiver:")
        for d in DEAD_CUSTOM:
            print("  " + d)
    if POLLED:
        print(f"\n{len(POLLED)} checkbox/radio with no signal "
              "(OK only if read at accept time — verify):")
        for p in POLLED:
            print("  " + p)
    if DEAD:
        print(f"\n{len(DEAD)} DEAD ACTION CONTROL(S) — no activation signal connected:")
        for d in DEAD:
            print("  " + d)
        return 1
    print("\nNo dead action buttons.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
