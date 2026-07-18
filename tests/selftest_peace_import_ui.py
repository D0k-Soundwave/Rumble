"""Self-test for the "Import from Peace…" menu action (Rumble 4.5).

Mirrors selftest_import_single_profile.py's pattern: an offscreen QApplication
drives the real menu handler with QFileDialog stubbed out, proving the wiring
from menu action -> ProfileManager.import_peace_file() -> profile list/editor
actually works end-to-end (peace_import.py's own parsing logic has its own
dedicated unit test in selftest_peace_import.py).

Run:  python tests/selftest_peace_import_ui.py
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

from rumble.core.profile_manager import ProfileManager
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


BASS_BOOST_PEACE = """\
[Frequencies]
Frequency1=10
Frequency2=21
[Gains]
Gain1=9.5
Gain2=7.5
[Qualities]
Quality1=1
Quality2=1
[General]
Description=Bass boost using peak filters
[Speakers]
SpeakerId0=0
SpeakerTargets0=all
SpeakerName0=All
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_peace_import_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    window = MainWindow(manager, theme_engine, QIcon())

    print("[1] menu action exists and is wired")
    check(hasattr(window, "action_import_peace"), "action_import_peace exists")
    check(hasattr(window, "_on_import_peace"), "_on_import_peace handler exists")

    print("\n[2] importing a .peace file adds and opens the profile")
    src = tmp / "Bass Boost.peace"
    src.write_text(BASS_BOOST_PEACE, encoding="utf-8")
    before = {p.id for p in manager.list_profiles()}
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(src), ""))
    try:
        window._on_import_peace()
    except Exception:
        FAILURES.append("_on_import_peace raised: " + traceback.format_exc())
    else:
        after = {p.id for p in manager.list_profiles()}
        new_ids = after - before
        check(len(new_ids) == 1, "exactly one new profile created")
        if new_ids:
            new_id = next(iter(new_ids))
            meta = manager.get_profile(new_id)
            check(meta.name == "Bass Boost", "imported profile is named from the filename")
            check("Imported" in meta.tags, "imported profile is tagged Imported")
            check(window._loaded_profile_id == new_id, "imported profile opens in the editor")
            config = manager.load_profile_config(new_id)
            check(len(config.channel_sections) == 1 and len(config.channel_sections[0].filters) == 2,
                  "the imported profile's config round-trips the 2 PK bands")

    print("\n[3] cancelling the file picker is a no-op")
    before2 = {p.id for p in manager.list_profiles()}
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
    window._on_import_peace()
    after2 = {p.id for p in manager.list_profiles()}
    check(before2 == after2, "no profile created when the dialog is cancelled")

    print("\n[4] a Peace file with no importable EQ warns instead of crashing")
    empty_src = tmp / "Empty.peace"
    empty_src.write_text("[General]\nDescription=nothing here\n", encoding="utf-8")
    before3 = {p.id for p in manager.list_profiles()}
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(empty_src), ""))
    try:
        window._on_import_peace()
    except Exception:
        FAILURES.append("_on_import_peace raised on an empty preset: " + traceback.format_exc())
    else:
        after3 = {p.id for p in manager.list_profiles()}
        check(before3 == after3, "no profile created from a preset with no real EQ")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
