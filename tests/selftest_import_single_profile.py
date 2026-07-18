"""Self-test for the "Import Single Profile…" menu action.

Run:  python tests/selftest_import_single_profile.py
Uses Qt's offscreen platform plugin — no window is shown. Exits non-zero if
any step raises or a check fails.
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
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

app = QApplication([])

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)

from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_import_single_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    window = MainWindow(manager, theme_engine, QIcon())

    print("[1] menu action exists and is wired")
    check(hasattr(window, "action_import_single"), "action_import_single exists")
    check(hasattr(window, "_on_import_single"), "_on_import_single handler exists")

    print("\n[2] importing a single .txt file adds and opens the profile")
    src = tmp / "external_export.txt"
    src.write_text(
        "# RUMBLE Profile: Imported Curve\n"
        "Preamp: -3.0 dB\n\nChannel: L R\n"
        "Filter 1: ON PK Fc 1000.0 Hz Gain 2.0 dB Q 1.0\n"
        "GraphicEQ: 20 0; 20000 0\n",
        encoding="utf-8")
    before = {p.id for p in manager.list_profiles()}
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(src), ""))
    try:
        window._on_import_single()
    except Exception:
        FAILURES.append("_on_import_single raised: " + traceback.format_exc())
    else:
        after = {p.id for p in manager.list_profiles()}
        new_ids = after - before
        check(len(new_ids) == 1, "exactly one new profile created")
        if new_ids:
            new_id = next(iter(new_ids))
            check(manager.get_profile(new_id).name == "Imported Curve",
                  "imported profile carries the embedded name")
            check(window._loaded_profile_id == new_id,
                  "imported profile opens in the editor")

    print("\n[3] cancelling the file picker is a no-op")
    before2 = {p.id for p in manager.list_profiles()}
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
    window._on_import_single()
    after2 = {p.id for p in manager.list_profiles()}
    check(before2 == after2, "no profile created when the dialog is cancelled")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
