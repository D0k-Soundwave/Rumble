"""Self-test for per-band solo, A/B compare, custom layouts, GEQ scaffolds.

Solo auditions one band without mutating stored state (preview-only); A/B
holds the saved profile audible while unsaved edits are parked; Custom…
channel layouts accept arbitrary names; the GEQ ISO scaffolds resample the
current curve onto standard 15/31-band centres.

Run:  python tests/selftest_solo_ab.py
Uses Qt's offscreen platform plugin — no window is shown."""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

app = QApplication([])

QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)

from rumble.core import apo_config
from rumble.core.profile_manager import (
    ACTIVE_POINTER_FILENAME, PREVIEW_FILENAME, RUMBLE_SUBDIR,
    ProfileManager, make_flat_config,
)
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
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
    tmp = Path(tempfile.mkdtemp(prefix="rumble_soloab_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.activate_profile("alpha")

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    w = MainWindow(manager, theme_engine, QIcon())
    ep = w.editor_panel
    pointer = tmp / "config" / ACTIVE_POINTER_FILENAME
    preview_file = tmp / "config" / RUMBLE_SUBDIR / PREVIEW_FILENAME

    def pointer_target() -> str:
        return pointer.read_text(encoding="utf-8").splitlines()[-1].replace("\\", "/")

    def flush_preview() -> None:
        app.processEvents()
        if w._preview_timer.isActive():
            w._preview_timer.stop()
            w._push_live_preview()

    w._on_profile_selected("alpha")
    app.processEvents()

    # ── per-band solo ─────────────────────────────────────────────────
    def solo_previews_without_mutating() -> None:
        expect(hasattr(ep._filter_rows[0], "btn_solo"), "rows should have a solo button")
        ep._filter_rows[1].btn_solo.setChecked(True)
        app.processEvents()
        expect(not w._dirty, "solo is an audition, not a tracked edit")
        flush_preview()
        body = preview_file.read_text(encoding="utf-8")
        expect("Filter 2: ON" in body, "the soloed band must stay on in the preview")
        expect("Filter 1: OFF" in body and "Filter 3: OFF" in body,
               f"other bands must be off in the preview:\n{body}")
        built = ep.build_config()
        expect(all(f.enabled for f in built.channel_sections[0].filters),
               "the saved-state config must keep every band enabled")
        disk = (manager.config_path / manager.get_profile("alpha").file).read_text(
            encoding="utf-8")
        expect("OFF" not in disk, "the profile on disk must be untouched by solo")

    step("solo previews one band without mutating stored state",
         solo_previews_without_mutating)

    def solo_is_exclusive() -> None:
        ep._filter_rows[2].btn_solo.setChecked(True)
        app.processEvents()
        expect(not ep._filter_rows[1].btn_solo.isChecked(),
               "soloing another band must release the previous solo")
        expect(ep._solo_index == 2, "the new band should hold the solo")

    step("solo is exclusive", solo_is_exclusive)

    def unsolo_restores_all_bands() -> None:
        ep._filter_rows[2].btn_solo.setChecked(False)
        app.processEvents()
        flush_preview()
        body = preview_file.read_text(encoding="utf-8")
        expect("OFF" not in body, "releasing solo must restore every band in the preview")
        expect(ep._solo_index is None, "solo state should be cleared")

    step("releasing solo restores all bands", unsolo_restores_all_bands)

    def section_switch_clears_solo() -> None:
        manager.create_profile("Beta", config=apo_config.parse_config(
            "Channel: L\nFilter 1: ON PK Fc 100.0 Hz Gain 2.0 dB Q 1.00\n"
            "Channel: R\nFilter 1: ON PK Fc 100.0 Hz Gain 4.0 dB Q 1.00\n"))
        w._load_profile_list()
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        w._on_profile_selected("beta")
        app.processEvents()
        expect(ep._solo_index is None, "loading a profile must clear any solo")
        ep._filter_rows[0].btn_solo.setChecked(True)
        app.processEvents()
        expect(ep._solo_index == 0, "precondition: a band is soloed")
        ep._switch_section(1)
        app.processEvents()
        expect(ep._solo_index is None, "switching channel sections must clear the solo")

    step("profile load and section switch clear the solo", section_switch_clears_solo)

    # ── A/B saved vs edited ───────────────────────────────────────────
    def ab_enables_only_when_diverged() -> None:
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        w._on_profile_selected("alpha")
        app.processEvents()
        expect(not ep.btn_ab.isEnabled(), "A/B is disabled on a clean profile")
        ep.preamp_spin.setValue(-5.0)
        app.processEvents()
        expect(ep.btn_ab.isEnabled(), "A/B enables once edits diverge on the active profile")

    step("A/B enables only when the active profile diverges", ab_enables_only_when_diverged)

    def ab_holds_the_saved_profile() -> None:
        flush_preview()
        expect(manager.is_previewing, "precondition: edits are previewing")
        ep.btn_ab.setChecked(True)
        app.processEvents()
        expect(not manager.is_previewing, "A/B must fall back to the saved profile")
        expect("Include: rumble/user/alpha.txt" in pointer_target(),
               f"pointer should hold the saved file, got {pointer_target()}")
        ep.preamp_spin.setValue(-8.0)
        app.processEvents()
        expect(not w._preview_timer.isActive(),
               "edits made while comparing must not re-arm the preview")
        expect("Include: rumble/user/alpha.txt" in pointer_target(),
               "the saved side must stay audible during further edits")

    step("A/B holds the saved profile while edits are parked", ab_holds_the_saved_profile)

    def ab_release_resumes_edits() -> None:
        ep.btn_ab.setChecked(False)
        app.processEvents()
        flush_preview()
        expect(manager.is_previewing, "releasing A/B must resume the edited preview")
        expect("Preamp: -8.0 dB" in preview_file.read_text(encoding="utf-8"),
               "the resumed preview must carry the edits made while comparing")

    step("releasing A/B resumes the edited preview", ab_release_resumes_edits)

    def save_releases_ab() -> None:
        ep.btn_ab.setChecked(True)
        app.processEvents()
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Save)
        w._on_save_current()
        app.processEvents()
        expect(not ep.btn_ab.isChecked(), "saving must release the A/B hold")
        expect(not ep.btn_ab.isEnabled(), "A/B disables once nothing diverges")
        expect(not w._ab_comparing, "the compare flag must clear on save")

    step("saving releases and disables A/B", save_releases_ab)

    # ── custom channel layouts ────────────────────────────────────────
    def custom_layout_applies() -> None:
        QInputDialog.getText = staticmethod(lambda *a, **k: ("FL FR SW", True))
        ep._preset_combo.setCurrentText("Custom…")
        app.processEvents()
        expect([s.channel for s in ep._channel_sections] == ["FL", "FR", "SW"],
               f"custom names should build the layout, got "
               f"{[s.channel for s in ep._channel_sections]}")
        expect(w._dirty, "a layout change is a tracked edit")
        expect(ep._preset_combo.currentIndex() == -1,
               "a custom layout shows a blank preset combo")

    step("Custom… layout accepts arbitrary channel names", custom_layout_applies)

    def custom_layout_cancel_reverts() -> None:
        before = [s.channel for s in ep._channel_sections]
        QInputDialog.getText = staticmethod(lambda *a, **k: ("", False))
        ep._preset_combo.setCurrentText("Stereo / Headphones")
        app.processEvents()
        ep._preset_combo.setCurrentText("Custom…")
        app.processEvents()
        expect([s.channel for s in ep._channel_sections] == ["L", "R"],
               "cancelling the dialog must leave the prior layout")
        _ = before

    step("cancelling the Custom… dialog keeps the prior layout",
         custom_layout_cancel_reverts)

    # ── GEQ ISO scaffolds ─────────────────────────────────────────────
    def geq_scaffolds_resample() -> None:
        geq = ep.advanced_panel.graphic_eq
        geq.set_points_from_string("100 3; 1000 3")
        geq.btn_iso15.click()
        app.processEvents()
        pts = sorted((p[0], p[1]) for p in geq._points)
        expect(len(pts) == 15, f"15-band scaffold should give 15 points, got {len(pts)}")
        expect(all(abs(g - 3.0) < 1e-6 for _, g in pts),
               "a flat +3 dB curve must stay +3 dB at every ISO centre")
        expect(abs(pts[0][0] - 25.0) < 1e-6 and abs(pts[-1][0] - 16000.0) < 1e-6,
               "15-band centres span 25 Hz – 16 kHz")
        geq.btn_iso31.click()
        app.processEvents()
        expect(len(geq._points) == 31, "31-band scaffold should give 31 points")

    step("GEQ scaffolds resample the current curve onto ISO centres",
         geq_scaffolds_resample)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
