"""Self-test: only direct user edits may raise the unsaved-changes warning.

Session and app-level controls -- theme, listener slots, tone trim, bypass,
auto-preamp, profile selection/activation, crossfeed, auto-switch -- must never
mark the loaded profile dirty. The regression this guards: recolouring the
Conditional/Eval syntax highlighters calls rehighlight(), which emits
textChanged with no actual text change, and textChanged is wired to the
editor's tracked-edit handler -- so merely picking a theme dirtied an untouched
profile and made every later profile switch prompt to discard changes.

Also checks the two things the fix must not break: the highlighter still
follows the theme, and genuine edits still mark dirty.

Run:  python tests/selftest_theme_not_dirty.py
Uses Qt's offscreen platform plugin -- no window is shown."""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication([])

QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard)

from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme, ordered_theme_names

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
    tmp = Path(tempfile.mkdtemp(prefix="rumble_themedirty_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    manager.create_profile("Alpha", config=make_flat_config(10, channels=["L", "R"]))
    manager.create_profile("Beta", config=make_flat_config(5))
    manager.activate_profile("alpha")

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)

    w = MainWindow(manager, theme_engine, QIcon())
    ep = w.editor_panel
    cond = ep.advanced_panel.conditional
    w._load_profile_list()

    other_themes = [n for n in ordered_theme_names() if n != DEFAULT_THEME_NAME]

    # Real, highlightable content: with an empty editor the highlighter has
    # nothing to reformat and the regression cannot reproduce.
    cond.set_value("If: sampleRate == 44100\n"
                   "Filter 1: ON PK Fc 1000 Hz Gain -3.0 dB Q 2.0\n"
                   "EndIf:")
    app.processEvents()

    def clean(label: str, fn) -> None:
        """Run fn and assert it left the profile undirtied."""
        def check() -> None:
            w._dirty = False
            fn()
            app.processEvents()
            expect(not w._dirty, f"{label} must not mark the profile dirty")
        step(label + " leaves the profile clean", check)

    for name in other_themes:
        clean(f"switching to theme '{name}'", lambda n=name: w._switch_theme(n))
    clean("switching back to the default theme",
          lambda: w._switch_theme(DEFAULT_THEME_NAME))

    clean("switching listener slot", lambda: w._on_listener_slot_changed(2))
    clean("switching listener slot back", lambda: w._on_listener_slot_changed(1))
    clean("applying a session tone trim", lambda: w._on_tone_trim_changed(3.0, 0.0, 0.0))
    clean("resetting the session tone trim", lambda: w._on_tone_trim_changed(0.0, 0.0, 0.0))
    clean("toggling auto pre-amp", lambda: ep.btn_auto_preamp.toggle())
    clean("untoggling auto pre-amp", lambda: ep.btn_auto_preamp.toggle())
    clean("engaging bypass", lambda: w._on_bypass_toggled(True))
    clean("releasing bypass", lambda: w._on_bypass_toggled(False))
    clean("selecting another profile", lambda: w._on_profile_selected("beta"))
    clean("selecting the original profile", lambda: w._on_profile_selected("alpha"))
    clean("activating another profile", lambda: w._on_activate_profile("beta"))
    clean("activating the original profile", lambda: w._on_activate_profile("alpha"))
    clean("toggling crossfeed", lambda: w._on_crossfeed_toggled(True))
    clean("untoggling crossfeed", lambda: w._on_crossfeed_toggled(False))
    clean("toggling auto-switch", lambda: w._on_auto_switch_toggled(True))
    clean("untoggling auto-switch", lambda: w._on_auto_switch_toggled(False))

    def keyword_colour() -> str | None:
        """The colour actually painted on the 'If:' keyword."""
        block = cond.text_edit.document().firstBlock()
        for r in block.layout().formats():
            return r.format.foreground().color().name()
        return None

    def highlighter_follows_theme() -> None:
        # Re-seed the content: the profile selection/activation checks above
        # reload the editor, which clears whatever was typed here.
        cond.set_value("If: sampleRate == 44100\n"
                       "Filter 1: ON PK Fc 1000 Hz Gain -3.0 dB Q 2.0\n"
                       "EndIf:")
        w._switch_theme(DEFAULT_THEME_NAME)
        app.processEvents()
        before = keyword_colour()
        expect(before is not None, "precondition: syntax formats are painted")
        # Find a theme that actually recolours keywords, so the check can't
        # pass vacuously on a theme that happens to share the accent colour.
        for name in other_themes:
            w._switch_theme(name)
            app.processEvents()
            if keyword_colour() != before:
                return
        raise AssertionError(
            "recolouring broke: the highlighter no longer follows the theme "
            f"(stuck at {before} across every theme)")

    step("syntax highlighting still follows the theme", highlighter_follows_theme)

    def real_edits_still_dirty() -> None:
        for label, edit in (
            ("conditional editor", lambda: cond.text_edit.setPlainText("Eval: x = 1")),
            ("eval editor",
             lambda: ep.advanced_panel.eval_editor.text_edit.setPlainText("linGain = 0.5")),
            ("pre-amp spin", lambda: ep.preamp_spin.setValue(-4.0)),
        ):
            w._dirty = False
            edit()
            app.processEvents()
            expect(w._dirty, f"a real edit to the {label} must still mark the profile dirty")

    step("real user edits still mark the profile dirty", real_edits_still_dirty)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
