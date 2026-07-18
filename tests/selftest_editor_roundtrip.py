"""Self-test: the editor must round-trip every config field it does not own.

Historically EditorPanel.build_config() constructed a fresh ProfileConfig,
silently dropping extra_lines, and AdvancedPanel collapsed multiple
conditional blocks into one. This test loads a kitchen-sink profile into the
real editor, rebuilds the config, and asserts nothing is lost; it also covers
the per-section Trim spin, the delay unit edit policy (untouched samples
delays survive verbatim; an actual edit rewrites in ms), the Stage selector
and the Eval editor.

Run:  python tests/selftest_editor_roundtrip.py
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

QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Save)

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


RAW = (
    "Stage: pre-mix\n"
    "Device: all\n"
    "Eval: linGain = 0.5\n"
    "Eval: bassBoost = 2\n"
    "Preamp: -4.0 dB\n"
    "Channel: L R\n"
    "Preamp: -1.5 dB\n"
    "Delay: 480 samples\n"
    "Filter 1: ON PK Fc 100.0 Hz Gain 2.0 dB Q 1.00\n"
    'If: deviceName == "Speakers"\n'
    "Filter 2: ON PK Fc 500.0 Hz Gain -3.0 dB Q 1.00\n"
    "EndIf\n"
    "If: sampleRate == 44100\n"
    "Filter 2: ON PK Fc 900.0 Hz Gain -1.0 dB Q 1.00\n"
    "EndIf\n"
    "LoudnessCorrection: state 0\n"
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_roundtrip_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    # Pin the sample rate deterministically: this test asserts exact ms<->samples
    # conversions (e.g. "480 samples @48k should show 10 ms"), so it must not
    # depend on effective_sample_rate() auto-detecting whatever this machine's
    # real default playback device happens to report (Task 4).
    manager.settings["sample_rate_mode"] = "manual"
    manager.settings["sample_rate"] = 48000
    manager.ensure_first_run()
    manager.create_profile("Fidelity", config=apo_config.parse_config(RAW))

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    w = MainWindow(manager, theme_engine, QIcon())
    ep = w.editor_panel

    w._on_profile_selected("fidelity")
    app.processEvents()

    def nothing_lost() -> None:
        built = ep.build_config()
        # LoudnessCorrection is now a structured field (Phase 1) that the
        # Advanced panel's Loudness group owns (Task 3). Loading "state 0"
        # (no ReferenceLevel/ReferenceOffset keys) and rebuilding without
        # touching the Loudness fields must be a true no-op — the editor
        # must not inject default keys the block never had (see
        # docs/superpowers/plans/phase1-errata.md #3).
        expect(built.loudness_correction is not None
               and built.loudness_correction.to_line() == "LoudnessCorrection: state 0",
               f"loudness_correction must survive build_config, got {built.loudness_correction!r}")
        expect(len(built.conditional_blocks) == 2,
               f"two conditional blocks must stay two, got {len(built.conditional_blocks)}")
        expect(built.stage == "pre-mix", f"stage must survive, got {built.stage!r}")
        expect(built.eval_lines == ["linGain = 0.5", "bassBoost = 2"],
               f"eval lines must survive, got {built.eval_lines!r}")
        sec = built.channel_sections[0]
        expect(sec.preamp_db == -1.5, f"section trim must survive, got {sec.preamp_db!r}")
        expect(sec.delay_samples == 480,
               f"untouched samples delay must stay verbatim, got {sec.delay_samples!r}")

    step("build_config loses nothing (extra lines, blocks, stage, eval, trim, delay)",
         nothing_lost)

    step("trim spin shows the per-section preamp",
         lambda: expect(abs(ep._section_trim_spin.value() + 1.5) < 1e-9,
                        f"trim spin should read -1.5, got {ep._section_trim_spin.value()}"))

    step("delay spin shows the ms conversion of a samples delay",
         lambda: expect(abs(ep._section_delay_spin.value() - 10.0) < 1e-6,
                        f"480 samples @48k should show 10 ms, got {ep._section_delay_spin.value()}"))

    def stage_selector_reflects_value() -> None:
        expect(ep.advanced_panel.stage_combo.currentData() == "pre-mix",
               f"stage combo should hold pre-mix, got {ep.advanced_panel.stage_combo.currentData()!r}")

    step("stage selector reflects the loaded value", stage_selector_reflects_value)

    step("eval editor holds the expressions",
         lambda: expect(ep.advanced_panel.eval_editor.value()
                        == ["linGain = 0.5", "bassBoost = 2"],
                        f"eval editor mismatch: {ep.advanced_panel.eval_editor.value()!r}"))

    def trim_edit_marks_dirty_and_renders() -> None:
        w._dirty = False
        ep._section_trim_spin.setValue(-2.5)
        app.processEvents()
        expect(w._dirty, "editing the trim must mark the profile dirty")
        body = apo_config.render_config_body(ep.build_config())
        expect("Channel: L R\nPreamp: -2.5 dB" in body,
               f"edited trim must render inside its section:\n{body}")

    step("trim edit marks dirty and renders inside the section",
         trim_edit_marks_dirty_and_renders)

    def untouched_delay_survives_other_edits() -> None:
        built = ep.build_config()
        expect(built.channel_sections[0].delay_samples == 480,
               "editing the trim must not clobber the untouched samples delay")

    step("untouched samples delay survives unrelated edits",
         untouched_delay_survives_other_edits)

    def delay_edit_rewrites_in_ms() -> None:
        ep._section_delay_spin.setValue(12.0)
        app.processEvents()
        sec = ep.build_config().channel_sections[0]
        expect(sec.delay_samples is None and abs(sec.delay_ms - 12.0) < 1e-9,
               f"an actual delay edit converts to ms, got samples={sec.delay_samples!r} "
               f"ms={sec.delay_ms!r}")

    step("editing the delay rewrites it in ms", delay_edit_rewrites_in_ms)

    def stage_edit_applies() -> None:
        combo = ep.advanced_panel.stage_combo
        idx = combo.findData("capture")
        expect(idx >= 0, "stage combo must offer a capture-only entry")
        w._dirty = False
        combo.setCurrentIndex(idx)
        app.processEvents()
        expect(w._dirty, "changing the stage must mark the profile dirty")
        expect(ep.build_config().stage == "capture",
               "the chosen stage must reach the built config")

    step("stage selector edit applies and marks dirty", stage_edit_applies)

    def foreign_stage_value_preserved() -> None:
        manager.create_profile(
            "Foreign Stage",
            config=apo_config.parse_config("Stage: pre-mix capture\nDevice: all\n"
                                           "Filter 1: ON PK Fc 100.0 Hz Gain 1.0 dB Q 1.00\n"))
        w._load_profile_list()
        # The previous steps left the open profile dirty; the switch prompt
        # offers Discard/Cancel only.
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        w._on_profile_selected("foreign_stage")
        app.processEvents()
        expect(ep.build_config().stage == "pre-mix capture",
               "a non-curated stage value must load and rebuild unchanged")

    step("foreign stage value round-trips through the selector",
         foreign_stage_value_preserved)

    def save_persists_everything() -> None:
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        w._on_profile_selected("fidelity")
        app.processEvents()
        ep._section_trim_spin.setValue(-2.5)
        app.processEvents()
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Save)
        w._on_save_current()
        text = (manager.config_path / manager.get_profile("fidelity").file).read_text(
            encoding="utf-8")
        for needle in ("LoudnessCorrection: state 0", "Stage: pre-mix",
                       "Eval: linGain = 0.5", "Eval: bassBoost = 2",
                       "Preamp: -2.5 dB", "Delay: 480 samples",
                       'If: deviceName == "Speakers"', "If: sampleRate == 44100"):
            expect(needle in text, f"saved file must keep {needle!r}:\n{text}")

    step("save persists every foreign field to disk", save_persists_everything)

    def divergent_trim_loads_unlinked() -> None:
        manager.create_profile(
            "Trim Diverged",
            config=apo_config.parse_config(
                "Channel: L\nPreamp: -1.0 dB\n"
                "Filter 1: ON PK Fc 100.0 Hz Gain 1.0 dB Q 1.00\n"
                "Channel: R\n"
                "Filter 1: ON PK Fc 100.0 Hz Gain 1.0 dB Q 1.00\n"))
        w._load_profile_list()
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        w._on_profile_selected("trim_diverged")
        app.processEvents()
        expect(not ep._btn_link_channels.isChecked(),
               "sections differing only in trim must load unlinked")

    step("sections differing only in trim load unlinked", divergent_trim_loads_unlinked)

    def sample_rate_plumbing() -> None:
        ep.set_sample_rate(96000)
        expect(ep._sample_rate == 96000, "editor must store the sample rate")
        expect(ep.graph._sample_rate == 96000, "graph must receive the sample rate")

    step("set_sample_rate reaches the graph", sample_rate_plumbing)

    def ir_reaches_graph_and_preamp() -> None:
        import struct

        import numpy as np

        # A +6.02 dB flat "IR": a single impulse of amplitude 2.0, written as
        # an IEEE-float WAV (the format stdlib wave cannot write).
        ir_dir = manager.rumble_dir / "irs"
        ir_dir.mkdir(parents=True, exist_ok=True)
        samples = np.zeros(4096, dtype="<f4")
        samples[0] = 2.0
        data = samples.tobytes()
        fmt = struct.pack("<HHIIHH", 3, 1, 48000, 48000 * 4, 4, 32)
        body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
                + b"data" + struct.pack("<I", len(data)) + data)
        (ir_dir / "boost6.wav").write_bytes(
            b"RIFF" + struct.pack("<I", len(body)) + body)

        manager.create_profile("With IR", config=apo_config.parse_config(
            "Device: all\nConvolution: irs/boost6.wav\n"
            "Filter 1: ON PK Fc 100.0 Hz Gain 0.0 dB Q 1.00\n"))
        w._load_profile_list()
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        w._on_profile_selected("with_ir")
        app.processEvents()
        expect(ep._ir_result is not None, "the profile's IR should analyse")
        expect(abs(ep._ir_result.peak_db - 6.02) < 0.05,
               f"IR peak should be +6.02 dB, got {ep._ir_result.peak_db:.3f}")
        expect(ep.graph._ir_curve.isVisible(), "the IR curve should be drawn")
        ep.btn_auto_preamp.setChecked(True)
        app.processEvents()
        expect(ep.preamp_spin.value() <= -6.9,
               f"auto preamp must absorb the IR gain, got {ep.preamp_spin.value()}")

    step("convolution IR reaches the graph and auto-preamp",
         ir_reaches_graph_and_preamp)

    def rate_change_updates_delay_display() -> None:
        ep.set_sample_rate(48000)  # resync after the plumbing step above
        manager.create_profile("Rate Delay", config=apo_config.parse_config(
            "Channel: L R\nDelay: 480 samples\n"
            "Filter 1: ON PK Fc 100.0 Hz Gain 1.0 dB Q 1.00\n"))
        w._load_profile_list()
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        w._on_profile_selected("rate_delay")
        app.processEvents()
        expect(abs(ep._section_delay_spin.value() - 10.0) < 1e-6,
               "precondition: 480 samples @48k shows 10 ms")
        w._dirty = False
        ep.set_sample_rate(96000)
        app.processEvents()
        expect(abs(ep._section_delay_spin.value() - 5.0) < 1e-6,
               f"a rate change re-derives the ms display immediately, got "
               f"{ep._section_delay_spin.value()}")
        expect(not w._dirty, "a display conversion is not an edit")
        expect(ep.build_config().channel_sections[0].delay_samples == 480,
               "the stored samples figure stays untouched")
        ep.set_sample_rate(48000)

    step("sample-rate change refreshes the delay display in place",
         rate_change_updates_delay_display)

    def missing_ir_flags_warning() -> None:
        ep.advanced_panel.convolution.path_edit.setText("irs/does_not_exist.wav")
        app.processEvents()
        expect(ep._ir_result is None, "a missing IR yields no analysis")
        expect("not found" in ep.advanced_panel.convolution.warning.text(),
               "the hint should flip to the missing-file warning")
        expect(not ep.graph._ir_curve.isVisible(),
               "the IR curve hides when the file is unresolvable")

    step("missing IR file flags the warning and drops the overlay",
         missing_ir_flags_warning)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
