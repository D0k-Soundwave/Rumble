"""Regression test: a profile load that raises part-way through must still
clear EditorPanel._loading, or every field-change handler guarded by that flag
(link channels, preset combo, band recompute…) silently stops responding until
the next clean load — the "UI buttons mysteriously stop working" bug.

Run:  python tools/selftest_loading_guard.py
Exits non-zero on regression."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)

from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_loadguard_"))
    m = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    m.ensure_first_run()
    m.create_profile("P", config=make_flat_config(5, channels=["L", "R"]))
    te = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    te.apply(app)
    w = MainWindow(m, te, QIcon())
    ep = w.editor_panel
    pid = next(p.id for p in m.profiles if p.name == "P")

    # Force a failure part-way through the load.
    original = ep._refresh_graph_and_raw
    ep._refresh_graph_and_raw = lambda: (_ for _ in ()).throw(
        RuntimeError("simulated mid-load failure"))
    try:
        w._open_profile(pid)
    except RuntimeError:
        pass
    finally:
        ep._refresh_graph_and_raw = original

    if ep._loading is not False:
        print("FAIL: _loading stuck True after a failed load")
        return 1

    # A clean reload, then the guarded link toggle must respond.
    w._open_profile(pid)
    before = ep._channels_linked()
    ep._btn_link_channels.click()
    if ep._channels_linked() == before:
        print("FAIL: link channels dead after a failed load")
        return 1

    print("PASS: _loading resets on failure; guarded controls stay live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
