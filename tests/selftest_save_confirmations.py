"""Self-test: Rumble 5.1 (save-time confirmation for negative headroom with
Auto off) and 5.4 (warn against LFE-only bass filters). Both live in
MainWindow._on_save_current(); this drives a real MainWindow/EditorPanel in
offscreen Qt mode and intercepts QMessageBox rather than mocking internals,
mirroring selftest_editor_roundtrip.py's harness.

Run:  python tests/selftest_save_confirmations.py
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
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication([])

from rumble.core import apo_config
from rumble.core.profile_manager import ProfileManager
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


def make_window(raw_config: str, name: str = "Test"):
    tmp = Path(tempfile.mkdtemp(prefix="rumble_saveconfirm_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.settings["sample_rate_mode"] = "manual"
    manager.settings["sample_rate"] = 48000
    manager.ensure_first_run()
    meta = manager.create_profile(name, config=apo_config.parse_config(raw_config))
    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    w = MainWindow(manager, theme_engine, QIcon())
    w._on_profile_selected(meta.id)
    app.processEvents()
    return manager, w, meta.id


class Recorder:
    """Records every QMessageBox.question/.warning call and hands back
    pre-scripted answers for .question() in call order."""

    def __init__(self, question_answers=()):
        self.question_calls: list[str] = []
        self.warning_calls: list[str] = []
        self._answers = iter(question_answers)

    def question(self, *args, **kwargs):
        # QMessageBox.question(parent, title, text, buttons, default)
        text = args[2] if len(args) > 2 else kwargs.get("text", "")
        self.question_calls.append(text)
        return next(self._answers)

    def warning(self, *args, **kwargs):
        text = args[2] if len(args) > 2 else kwargs.get("text", "")
        self.warning_calls.append(text)
        return QMessageBox.StandardButton.Ok


def install(recorder: Recorder) -> None:
    QMessageBox.question = staticmethod(recorder.question)
    QMessageBox.warning = staticmethod(recorder.warning)
    QMessageBox.information = staticmethod(lambda *a, **k: None)


FLAT = (
    "Device: all\nChannel: L R\n"
    "Filter 1: ON PK Fc 1000.0 Hz Gain 0.0 dB Q 1.00\n"
)

CLIPPING = (
    "Device: all\nChannel: L R\n"
    "Filter 1: ON PK Fc 1000.0 Hz Gain 24.0 dB Q 1.00\n"
)

LFE_BASS = (
    "Device: all\nChannel: L R\n"
    "Filter 1: ON PK Fc 1000.0 Hz Gain 1.0 dB Q 1.00\n"
    "Channel: LFE\n"
    "Filter 1: ON PK Fc 60.0 Hz Gain 3.0 dB Q 1.00\n"
)

LFE_TREBLE = (
    "Device: all\nChannel: L R\n"
    "Filter 1: ON PK Fc 1000.0 Hz Gain 1.0 dB Q 1.00\n"
    "Channel: LFE\n"
    "Filter 1: ON PK Fc 8000.0 Hz Gain 3.0 dB Q 1.00\n"
)

SHARED_BASS = (
    "Device: all\nChannel: L R LFE\n"
    "Filter 1: ON PK Fc 60.0 Hz Gain 3.0 dB Q 1.00\n"
)


def clipping_with_auto_off_prompts_and_blocks_on_cancel() -> None:
    manager, w, pid = make_window(CLIPPING)
    ep = w.editor_panel
    ep.btn_auto_preamp.setChecked(False)
    app.processEvents()
    expect(ep.current_headroom_db() < 0, "precondition: this profile must clip")
    expect(not ep.is_auto_preamp_enabled(), "precondition: Auto must be off")
    rec = Recorder(question_answers=[QMessageBox.StandardButton.Cancel])
    install(rec)
    saved_text_before = (manager.config_path / manager.get_profile(pid).file).read_text(
        encoding="utf-8")
    w._on_save_current()
    expect(len(rec.question_calls) == 1,
           f"only the clip-risk prompt should fire, got {rec.question_calls!r}")
    expect("clip" in rec.question_calls[0].lower() or "headroom" in rec.question_calls[0].lower(),
           f"the prompt should mention the clipping risk, got {rec.question_calls[0]!r}")
    saved_text_after = (manager.config_path / manager.get_profile(pid).file).read_text(
        encoding="utf-8")
    expect(saved_text_before == saved_text_after, "cancelling the clip-risk prompt must not save")


def clipping_with_auto_off_saves_when_both_confirmed() -> None:
    manager, w, pid = make_window(CLIPPING)
    ep = w.editor_panel
    ep.btn_auto_preamp.setChecked(False)
    app.processEvents()
    rec = Recorder(question_answers=[
        QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Save])
    install(rec)
    w._on_save_current()
    expect(len(rec.question_calls) == 2,
           f"both prompts should fire in order, got {rec.question_calls!r}")
    saved_text = (manager.config_path / manager.get_profile(pid).file).read_text(encoding="utf-8")
    expect("Gain 24.0 dB" in saved_text, "confirming both prompts must save the profile")


def positive_headroom_never_prompts_clip_risk() -> None:
    manager, w, pid = make_window(FLAT)
    ep = w.editor_panel
    ep.btn_auto_preamp.setChecked(False)
    app.processEvents()
    expect(ep.current_headroom_db() >= 0, "precondition: this profile must not clip")
    rec = Recorder(question_answers=[QMessageBox.StandardButton.Save])
    install(rec)
    w._on_save_current()
    expect(len(rec.question_calls) == 1,
           f"only the normal save prompt should fire, got {rec.question_calls!r}")


def auto_on_never_prompts_clip_risk() -> None:
    manager, w, pid = make_window(CLIPPING)
    ep = w.editor_panel
    ep.btn_auto_preamp.setChecked(True)
    app.processEvents()
    expect(ep.is_auto_preamp_enabled(), "precondition: Auto must be on")
    expect(ep.current_headroom_db() >= 0, "Auto preamp should have resolved the clipping")
    rec = Recorder(question_answers=[QMessageBox.StandardButton.Save])
    install(rec)
    w._on_save_current()
    expect(len(rec.question_calls) == 1,
           f"Auto on must skip the clip-risk prompt, got {rec.question_calls!r}")


def lfe_only_bass_filter_warns_but_does_not_block() -> None:
    manager, w, pid = make_window(LFE_BASS)
    ep = w.editor_panel
    ep.btn_auto_preamp.setChecked(True)
    app.processEvents()
    rec = Recorder(question_answers=[QMessageBox.StandardButton.Save])
    install(rec)
    w._on_save_current()
    expect(len(rec.warning_calls) == 1,
           f"the LFE-bass warning should fire once, got {rec.warning_calls!r}")
    expect("lfe" in rec.warning_calls[0].lower(),
           f"the warning should mention LFE, got {rec.warning_calls[0]!r}")
    saved_text = (manager.config_path / manager.get_profile(pid).file).read_text(encoding="utf-8")
    expect("Channel: LFE" in saved_text, "the warning must not block the save")


def lfe_only_treble_filter_does_not_warn() -> None:
    manager, w, pid = make_window(LFE_TREBLE)
    ep = w.editor_panel
    ep.btn_auto_preamp.setChecked(True)
    app.processEvents()
    rec = Recorder(question_answers=[QMessageBox.StandardButton.Save])
    install(rec)
    w._on_save_current()
    expect(len(rec.warning_calls) == 0,
           f"a filter above the LFE band limit should not warn, got {rec.warning_calls!r}")


def shared_channel_bass_filter_does_not_warn() -> None:
    manager, w, pid = make_window(SHARED_BASS)
    ep = w.editor_panel
    ep.btn_auto_preamp.setChecked(True)
    app.processEvents()
    rec = Recorder(question_answers=[QMessageBox.StandardButton.Save])
    install(rec)
    w._on_save_current()
    expect(len(rec.warning_calls) == 0,
           f"a bass filter scoped to more than just LFE should not warn, got {rec.warning_calls!r}")


def main() -> int:
    step("negative headroom + Auto off prompts a clip-risk confirmation, cancelling blocks the save",
         clipping_with_auto_off_prompts_and_blocks_on_cancel)
    step("negative headroom + Auto off saves once both prompts are confirmed",
         clipping_with_auto_off_saves_when_both_confirmed)
    step("positive headroom never triggers the clip-risk prompt",
         positive_headroom_never_prompts_clip_risk)
    step("Auto on never triggers the clip-risk prompt",
         auto_on_never_prompts_clip_risk)
    step("an LFE-only bass filter warns but does not block the save",
         lfe_only_bass_filter_warns_but_does_not_block)
    step("an LFE-only filter above the band limit does not warn",
         lfe_only_treble_filter_does_not_warn)
    step("a bass filter scoped to more than just LFE does not warn",
         shared_channel_bass_filter_does_not_warn)
    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
