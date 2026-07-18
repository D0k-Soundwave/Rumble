"""Self-test: the New Profile dialog offers Custom… channel layouts.

Parity with the editor's preset combo — Custom… prompts for space-separated
channel names, remembers them for values()/channels(), and cancelling the
prompt reverts to the previously selected preset.

Run:  python tests/selftest_new_profile_custom.py
Uses Qt's offscreen platform plugin — no window is shown."""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QInputDialog

app = QApplication([])

from rumble.core.profile_manager import ProfileManager
from rumble.ui.editor_panel import CUSTOM_LAYOUT_LABEL
from rumble.ui.new_profile_dialog import NewProfileDialog
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_npd_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    theme = get_theme(DEFAULT_THEME_NAME)
    dlg = NewProfileDialog(theme, default_bands=10, manager=manager)

    def combo_offers_custom() -> None:
        labels = [dlg.layout_combo.itemText(i) for i in range(dlg.layout_combo.count())]
        expect(CUSTOM_LAYOUT_LABEL in labels,
               f"layout combo should offer {CUSTOM_LAYOUT_LABEL!r}, got {labels}")

    step("layout combo offers Custom…", combo_offers_custom)

    def custom_names_apply() -> None:
        QInputDialog.getText = staticmethod(lambda *a, **k: ("FL FR SW", True))
        dlg.layout_combo.setCurrentText(CUSTOM_LAYOUT_LABEL)
        app.processEvents()
        expect(dlg.channels() == ["FL", "FR", "SW"],
               f"custom names should reach channels(), got {dlg.channels()}")
        expect(dlg.layout_combo.currentText() == CUSTOM_LAYOUT_LABEL,
               "the combo stays on Custom… after entering names")
        expect(dlg.values()["channels"] == ["FL", "FR", "SW"],
               "values() carries the custom channels")

    step("Custom… prompts for names and applies them", custom_names_apply)

    def preset_after_custom() -> None:
        dlg.layout_combo.setCurrentText("Stereo / Headphones")
        app.processEvents()
        expect(dlg.channels() == ["L", "R"],
               f"selecting a preset after Custom… restores it, got {dlg.channels()}")

    step("selecting a preset after Custom… works", preset_after_custom)

    def cancel_reverts() -> None:
        QInputDialog.getText = staticmethod(lambda *a, **k: ("", False))
        dlg.layout_combo.setCurrentText(CUSTOM_LAYOUT_LABEL)
        app.processEvents()
        expect(dlg.layout_combo.currentText() == "Stereo / Headphones",
               f"cancelling reverts the combo, got {dlg.layout_combo.currentText()!r}")
        expect(dlg.channels() == ["L", "R"], "…and the channel list")

    step("cancelling the prompt reverts to the prior preset", cancel_reverts)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
