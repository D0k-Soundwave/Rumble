"""Hearing test suite dialog (Phase 7).

Consumes the Qt-free core built in Tasks 1-5 (``rumble/core/hearing_test.py``)
without reimplementing any of its logic -- Test A (the per-ear frequency
staircase) end to end.

Test B (the noise-screening sub-flow that once lived here, consent gate ->
digit-triplet-in-noise run -> results) was permanently abandoned as a
product decision (2026-07-09) and its UI was removed 2026-07-16 -- its
data never fed anything the Frequency Test doesn't already cover, and the
digit-clip voice needed a redo it was never going to get. The underlying
Qt-free core (``rumble/core/digit_screening.py``) is kept, fully tested, in
case it is ever reused for something else, but nothing in this dialog
references it anymore.

Top-level shape (mirrors ``rumble/ui/abx_dialog.py``'s established
``QStackedWidget``/``_build_x_page()`` pattern):

- ``self.stack`` — picker page, then Test A's own internal
  ``self.freq_stack`` (dashboard -> volume prep -> test -> results).
- **Picker** (`_build_picker_page`): a single "Frequency Calibration"
  button routing to that sub-flow.
- **Test A dashboard** (`_build_freq_dash_page`, 2026-07-13 single-test
  redesign): THE calibration is one persistent per-point list (11
  frequencies x 2 ears, listener_slots.load_calibration_state) shown as a
  grid of per-point chips -- measured points show their relative-dB value
  and can be clicked to re-test individually, in any order, across any
  number of sessions. "Continue Calibration" queues the unmeasured points
  in hearing_test.progressive_order() (baseline five frequencies first --
  the old "Quick" set -- then detail infill); "Re-test Flagged" re-queues
  frequency_flags() points; "Clear All" (confirm-gated) starts over. A
  volume guard compares the state's stored volume reference against the
  current master volume and offers a one-click restore, since thresholds
  are only comparable at the volume they were measured at.
- **Test A volume prep** (`_build_freq_intro_page`): disclaimer, the
  reference-voice volume-setting step, Start. Shown once per dialog
  session before the first pass; later passes in the same sitting skip
  straight to the test page (the volume is already anchored).
- **Test A test** (`_build_freq_test_page`): current ear/frequency, a
  painted "tone playing" light driven by a 30 ms poll of
  ``engine.is_playing`` (playback is non-blocking via
  hearing_test.start_tone -- the three repeats are sequenced by that same
  poll timer, so the event loop never freezes mid-tone), a per-point
  response trail + reversal-convergence meter, a live chart that gains
  each point as it locks in, heard/not-heard/skip, pass progress, a
  baseline-complete milestone note, Pause/Resume. Every completed point is
  written into the persistent state IMMEDIATELY (and live correction
  invalidated), so leaving mid-pass loses nothing but the current point's
  partial staircase.
- **Test A results** (`_build_freq_results_page`): a pyqtgraph left/right
  relative-dB chart of the WHOLE working state (not just the pass that
  just finished), ``summarise()`` text, the softer ``frequency_flags()``
  note, Save to file, a Previous Results overlay combo. Completing a pass
  also snapshots the state into the slot's saved-run history
  (listener_slots.save_slot_result), which is what the overlay combo and
  10-run retention keep serving.

Test A takes over Equalizer APO via ``BypassGuard`` (bypasses all EQ
processing for the run's duration, since the test is measuring the
listener's own hearing, not the effect of any profile) for as long as the
intro/test/results pages are showing — released only when the dialog closes
or the user navigates back to the picker page, via the single idempotent
``_end_frequency_test()`` teardown, mirroring ``ABXDialog``'s
``_ensure_restored()`` guaranteed-restore shape exactly: every exit path
(``closeEvent``, ``reject()``, ``accept()``, the "Back to Menu" buttons)
funnels through it, and it is always safe to call more than once.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from rumble.core.audio.engine import ToneEngine
from rumble.core import listener_slots, master_volume
from rumble.core.hearing_test import (
    REFERENCE_SOUNDS, STANDARD_FREQS, BypassGuard, FrequencyTest, baseline_complete,
    export_text, frequency_flags, load_reference_sound, load_volume_reference, normalise,
    progressive_order, start_tone, summarise, volume_drift_note,
)
from rumble.ui.a11y import describe
from rumble.ui.calibration_history_dialog import CalibrationHistoryDialog
from rumble.ui.density import dp
from rumble.ui.eq_graph import LogFreqAxis
from rumble.ui.frameless import CHROME_STRIP_HEIGHT, FramelessDialog
from rumble.ui.hud_frame import HudFrame

# Global Constraints (progress.md), verbatim -- Test A's disclaimer, per spec
# §12a's framing rule: "audio response profile", never "audiogram"/"hearing
# loss". Wording updated 2026-07-08 to drop "hearing"/"test" from the
# user-facing name (now branded "Ear Calibration" / "Frequency Calibration");
# the underlying meaning is unchanged.
_FREQ_DISCLAIMER = (
    "Relative measurement on your current hardware — not a medical "
    "evaluation.")

# Pause before each auto-played tone: 2x the 150 ms inter-presentation gap
# already used WITHIN one trial's three tone repeats, so the silence before
# a new trial reads as clearly longer than the silences inside one -- the
# listener can tell "next tone" from "same tone again" by gap alone.
_AUTO_PLAY_DELAY_MS = 300


def _page_heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "heading")
    return label


def _freq_label(freq: int) -> str:
    if freq >= 1000:
        return f"{freq / 1000:g} kHz"
    return f"{freq} Hz"


def _tested_freq_ticks() -> list[list[tuple[float, str]]]:
    """Explicit x-axis ticks at exactly the frequencies the calibration
    tests. pyqtgraph's own log-scale tick picker otherwise lands on values
    like 3.16228 kHz (10^0.5), which is both ugly and meaningless here --
    and the shared eq_graph._format_freq() that renders them is the main EQ
    graph's formatter, not ours to change for a local axis problem."""
    major = [(float(np.log10(f)),
              f"{f // 1000}k" if f >= 1000 else str(f))
             for f in STANDARD_FREQS]
    return [major, []]


def _qcolor(value: str):
    """Parse a theme token colour string (#RRGGBB or #RRGGBBAA) into a
    QColor. Mirrors eq_graph.py's private helper of the same name exactly
    (deliberately duplicated rather than imported, since that one is a
    module-private helper of a different widget, not a shared utility)."""
    from PySide6.QtGui import QColor
    s = value.lstrip("#")
    if len(s) == 8:
        r, g, b, a = (int(s[i:i + 2], 16) for i in (0, 2, 4, 6))
        return QColor(r, g, b, a)
    return QColor(value)


def _alpha(value: str, alpha: int):
    from PySide6.QtGui import QColor
    c = QColor(value)
    c.setAlpha(alpha)
    return c


# The 150 ms inter-repeat gap present_tone()'s old blocking loop slept for,
# now enforced by the poll-timer sequencer instead (see _on_play_tick).
_REPEAT_GAP_S = 0.15

# Poll interval for the playback light / repeat sequencer -- fast enough
# that the light visibly tracks each 200 ms tone, cheap enough to leave
# running for the whole test page.
_PLAY_POLL_MS = 30


class _PlayLight(QWidget):
    """A small painted indicator lamp: filled with the theme accent while a
    tone is actually sounding, a dim outline otherwise. Painted directly
    (no glow/shadow effects -- effects.py's own rule) so it is safe at any
    size and theme."""

    def __init__(self, theme: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._on = False
        side = dp(18, 14)
        self.setFixedSize(side, side)

    def set_theme(self, theme: dict) -> None:
        self._theme = theme
        self.update()

    def set_on(self, on: bool) -> None:
        if on != self._on:
            self._on = on
            self.update()

    @property
    def is_on(self) -> bool:
        return self._on

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = _qcolor(self._theme.get("accent", "#4660E6"))
        muted = _qcolor(self._theme.get("text_muted", "#8589A8"))
        rect = self.rect().adjusted(2, 2, -2, -2)
        if self._on:
            painter.setBrush(accent)
            painter.setPen(accent)
        else:
            dim = _alpha(self._theme.get("text_muted", "#8589A8"), 70)
            painter.setBrush(dim)
            painter.setPen(muted)
        painter.drawEllipse(rect)
        painter.end()


class HearingSuiteDialog(FramelessDialog):
    """Hearing test suite: picker -> Test A (frequency test) sub-flow."""

    def __init__(self, manager, theme: dict, parent: QWidget | None = None,
                 tone_engine_factory=None):
        super().__init__(parent)
        self.setWindowTitle("Ear Calibration")
        # Prior resize() target was dp(680,600) x dp(640,560) = 600x560 --
        # measured (real, non-offscreen show()) as already sufficient for
        # every page in the wizard, including the fully-populated 22-point
        # dashboard grid (widest/tallest content case) -- no page needed
        # more room. Grown by CHROME_STRIP_HEIGHT for the new chrome strip.
        self.setFixedSize(600, 560 + CHROME_STRIP_HEIGHT)
        self.manager = manager
        self.theme = theme
        # Test seam: production callers never pass this, so real runs always
        # build a real ToneEngine (which owns a live PortAudio stream); tests
        # inject a fake with is_playing always False so present_tone() never
        # blocks.
        self._tone_engine_factory = tone_engine_factory or ToneEngine

        self._ft: FrequencyTest | None = None
        self._engine = None
        self._bypass_guard: BypassGuard | None = None
        self._paused = False
        self._total_pairs = 0
        self._point_trial = 0
        self._played_volume_ref = False
        self._volume_reference: dict | None = None
        self._prior_volume_reference: dict | None = None
        self._norm: dict = {}
        self._raw: dict = {}
        self._no_response: dict = {}
        # Single progressive test (2026-07-13 redesign): the persistent
        # per-point working state, the queue for the pass being started,
        # which pairs this pass has already written into the state, the
        # current point's response trail, and the repeat sequencer's state.
        self._cal_state: dict = {"points": {"left": {}, "right": {}},
                                 "volume_reference": None}
        self._pending_pairs: list[tuple[str, int]] = []
        self._recorded_pairs: set[tuple[str, int]] = set()
        self._trail: list[bool] = []
        self._seq_reps_left = 0
        self._seq_gap_deadline: float | None = None
        self._baseline_was_complete = False
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(_PLAY_POLL_MS)
        self._play_timer.timeout.connect(self._on_play_tick)

        root = QVBoxLayout(self.body)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.picker_page = self._build_picker_page()
        self.stack.addWidget(self.picker_page)

        self.freq_stack = QStackedWidget()
        self.freq_dash_page = self._build_freq_dash_page()
        self.freq_intro_page = self._build_freq_intro_page()
        self.freq_test_page = self._build_freq_test_page()
        self.freq_results_page = self._build_freq_results_page()
        self.freq_stack.addWidget(self.freq_dash_page)
        self.freq_stack.addWidget(self.freq_intro_page)
        self.freq_stack.addWidget(self.freq_test_page)
        self.freq_stack.addWidget(self.freq_results_page)
        self.stack.addWidget(self.freq_stack)

        self._apply_freq_chart_theme()
        self.stack.setCurrentWidget(self.picker_page)

    # ── picker page ───────────────────────────────────────────────────

    def _build_picker_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("EAR CALIBRATION"))

        intro = QLabel(
            "A short, informal calibration to help you (or an AI you share "
            "the exported results with) understand your own listening setup "
            "better. Not a medical evaluation.")
        intro.setWordWrap(True)
        intro.setProperty("class", "muted")
        lay.addWidget(intro)

        frame = HudFrame(class_name="panel")
        frame.set_theme(self.theme)
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(dp(14, 10), dp(14, 10), dp(14, 10), dp(14, 10))
        frame_lay.setSpacing(14)

        self.btn_pick_frequency = QPushButton("Frequency Calibration")
        self.btn_pick_frequency.setProperty("class", "primary")
        self.btn_pick_frequency.setMinimumHeight(dp(48, 40))
        self.btn_pick_frequency.clicked.connect(self._open_frequency_test)
        describe(self.btn_pick_frequency, "Frequency Calibration",
                 "Measure how each ear responds across the frequency range "
                 "on your current hardware, building a relative audio "
                 "response profile you can use (or hand to an AI) to author "
                 "a gentle compensation profile.")
        frame_lay.addWidget(self.btn_pick_frequency)

        freq_hint = QLabel(
            "Plays short tones ear-by-ear and asks whether you can hear "
            "them. ~5-10 minutes depending on the frequency set chosen.")
        freq_hint.setWordWrap(True)
        freq_hint.setProperty("class", "muted")
        frame_lay.addWidget(freq_hint)

        lay.addWidget(frame)
        lay.addStretch(1)
        return page

    def _open_frequency_test(self) -> None:
        # The dashboard is pure navigation -- no engine, no bypass; those
        # engage in _begin_pass() when a pass is actually about to make
        # sound (the volume-reference clip must play through the same raw,
        # unbypassed-by-anything-except-us signal path as the test tones,
        # so the guard goes up before the prep page, not before the list).
        self._cal_state = listener_slots.load_calibration_state(
            self.manager.data_dir, self.manager.active_listener_slot)
        self._populate_dashboard()
        self.freq_stack.setCurrentWidget(self.freq_dash_page)
        self.stack.setCurrentWidget(self.freq_stack)

    # ── Test A: dashboard (the persistent per-point list) ─────────────

    def _build_freq_dash_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(10)
        lay.addWidget(_page_heading("FREQUENCY CALIBRATION"))

        self.lbl_dash_status = QLabel("")
        self.lbl_dash_status.setWordWrap(True)
        lay.addWidget(self.lbl_dash_status)

        self.lbl_dash_volume = QLabel("")
        self.lbl_dash_volume.setWordWrap(True)
        self.lbl_dash_volume.setProperty("class", "muted")
        lay.addWidget(self.lbl_dash_volume)

        # A real (bordered) button, not a ghost: it only appears when the
        # volume has actually drifted, and it is the fix for that -- an
        # actionable warning, not a nav link.
        volume_row = QHBoxLayout()
        self.btn_dash_restore_volume = QPushButton("Restore Calibration Volume")
        self.btn_dash_restore_volume.clicked.connect(self._on_restore_volume)
        describe(self.btn_dash_restore_volume, "Restore calibration volume",
                 "Set your Windows volume back to what it was when these "
                 "points were measured -- thresholds are only comparable "
                 "at the volume they were measured at. External amp or "
                 "speaker knobs still need setting by hand (see your own "
                 "note above).")
        self.btn_dash_restore_volume.setVisible(False)
        volume_row.addWidget(self.btn_dash_restore_volume)
        volume_row.addStretch(1)
        lay.addLayout(volume_row)

        frame = HudFrame(class_name="panel")
        frame.set_theme(self.theme)
        grid = QGridLayout(frame)
        grid.setContentsMargins(dp(14, 10), dp(10, 8), dp(14, 10), dp(10, 8))
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)

        hdr_left = QLabel("Left ear")
        hdr_right = QLabel("Right ear")
        for hdr in (hdr_left, hdr_right):
            hdr.setProperty("class", "muted")
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(hdr_left, 0, 1)
        grid.addWidget(hdr_right, 0, 2)

        # One chip per (ear, freq) point -- the manually-listed, re-run-in-
        # any-order surface. Chips show the measured relative-dB value (or
        # skipped/limit/not-yet) and clicking one queues just that point.
        self._dash_chips: dict[tuple[str, int], QPushButton] = {}
        for row, freq in enumerate(STANDARD_FREQS, start=1):
            lbl = QLabel(_freq_label(freq))
            grid.addWidget(lbl, row, 0)
            for col, ear in ((1, "left"), (2, "right")):
                # Deliberately the DEFAULT QPushButton look (bordered, raised),
                # not the borderless "ghost" class the surrounding nav buttons
                # use: clicking a chip to re-test that one point is the whole
                # point of this page, and as flat text a chip reads as a
                # read-only label -- nobody would discover it is a control.
                chip = QPushButton("")
                chip.setMinimumHeight(dp(26, 22))
                chip.setCursor(Qt.CursorShape.PointingHandCursor)
                chip.clicked.connect(
                    lambda _=False, e=ear, f=freq: self._on_dash_point_clicked(e, f))
                self._dash_chips[(ear, freq)] = chip
                grid.addWidget(chip, row, col)
        # Pack the rows to the top: the frame takes the page's spare vertical
        # space (so it fills the dialog), but the GRID must not distribute
        # that space between its own rows -- without this the point list
        # floats apart with a dead band above the column headers.
        grid.setRowStretch(len(STANDARD_FREQS) + 1, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        lay.addWidget(frame, 1)

        btn_row = QHBoxLayout()
        self.btn_dash_back = QPushButton("Back to Menu")
        self.btn_dash_back.clicked.connect(self._go_to_picker)
        describe(self.btn_dash_back, "Back to Menu",
                 "Return to the Ear Calibration menu. Measured points are "
                 "already saved -- nothing is lost by leaving.")
        btn_row.addWidget(self.btn_dash_back)

        self.btn_dash_clear = QPushButton("Clear All")
        self.btn_dash_clear.clicked.connect(self._on_dash_clear)
        describe(self.btn_dash_clear, "Clear all",
                 "Forget every measured point and start this listener's "
                 "calibration over. Saved run history is kept.")
        btn_row.addWidget(self.btn_dash_clear)

        self.btn_dash_history = QPushButton("Manage History…")
        self.btn_dash_history.clicked.connect(self._on_manage_history)
        describe(self.btn_dash_history, "Manage calibration history",
                 "Delete one, several, or all of this listener's saved "
                 "calibration runs.")
        self.btn_dash_history.setVisible(False)
        btn_row.addWidget(self.btn_dash_history)

        self.btn_dash_retest_flagged = QPushButton("Re-test Flagged")
        self.btn_dash_retest_flagged.clicked.connect(self._on_dash_retest_flagged)
        describe(self.btn_dash_retest_flagged, "Re-test flagged points",
                 "Re-run only the points that read unusually relative to "
                 "the rest of this calibration.")
        btn_row.addWidget(self.btn_dash_retest_flagged)
        btn_row.addStretch(1)

        self.btn_dash_start = QPushButton("Start Calibration")
        self.btn_dash_start.setProperty("class", "primary")
        self.btn_dash_start.clicked.connect(self._on_dash_start)
        describe(self.btn_dash_start, "Start or continue calibration",
                 "Work through the unmeasured points, most informative "
                 "first: after the five baseline frequencies the "
                 "calibration is already usable, and you can stop at any "
                 "point -- every finished point is saved immediately, so "
                 "you can pick up where you left off any time, even days "
                 "later.")
        btn_row.addWidget(self.btn_dash_start)
        lay.addLayout(btn_row)
        return page

    def _state_entry(self, ear: str, freq: int) -> dict | None:
        return self._cal_state.get("points", {}).get(ear, {}).get(freq)

    def _remaining_pairs(self) -> list[tuple[str, int]]:
        """Unmeasured (never resolved) points, in progressive order --
        skipped points count as resolved; re-testing those is an explicit
        per-chip choice, not something Continue drags the listener back
        through."""
        return [(ear, freq) for freq in progressive_order()
                for ear in ("left", "right")
                if self._state_entry(ear, freq) is None]

    def _populate_dashboard(self) -> None:
        # measured_only throughout the dialog's math: a gave-up point's stored
        # value is the ceiling the ear FAILED to hear, not a threshold. It gets
        # its own "limit" chip below; it must not masquerade as a reading in the
        # counts, the curves, the outlier flags or the correction.
        raw = listener_slots.calibration_raw(self._cal_state, measured_only=True)
        norm = normalise(raw)
        flags = frequency_flags(norm)
        measured = sum(
            1 for ear in ("left", "right")
            for value in raw.get(ear, {}).values() if value is not None)
        resolved = sum(len(raw.get(ear, {})) for ear in ("left", "right"))
        total = len(STANDARD_FREQS) * 2

        for (ear, freq), chip in self._dash_chips.items():
            entry = self._state_entry(ear, freq)
            if entry is None:
                chip.setText("not yet")
                hint = "Not measured yet. Click to test this point now."
            elif entry.get("threshold") is None:
                chip.setText("skipped")
                hint = "Skipped. Click to test this point now."
            elif entry.get("gave_up"):
                chip.setText("limit")
                hint = ("Not heard at the loudest testable level. Click to "
                        "re-test this point.")
            else:
                norm_val = norm.get(ear, {}).get(freq)
                chip.setText(f"{norm_val:+.1f} dB" if norm_val is not None
                             else "measured")
                when = entry.get("measured_utc") or "a previous run"
                hint = f"Measured {when}. Click to re-test this point."
                if freq in flags:
                    chip.setText(chip.text() + " (?)")
                    hint += (" Reads unusually relative to the rest of this "
                             "calibration -- worth a second look.")
            describe(chip, f"{ear.capitalize()} ear, {_freq_label(freq)}", hint)

        if resolved == 0:
            status = ("No points measured yet. The calibration works "
                      "through one point at a time -- you can stop after "
                      "any point and continue later.")
        else:
            status = f"{measured} of {total} points measured."
            if baseline_complete(raw):
                status += (" Baseline complete -- your calibration is "
                           "usable now; remaining points add detail.")
        self.lbl_dash_status.setText(status)

        remaining = self._remaining_pairs()
        if resolved == 0:
            self.btn_dash_start.setText("Start Calibration")
        elif remaining:
            self.btn_dash_start.setText(f"Continue ({len(remaining)} points left)")
        else:
            self.btn_dash_start.setText("Re-run Everything")
        self.btn_dash_retest_flagged.setVisible(bool(flags))
        self.btn_dash_clear.setVisible(resolved > 0)
        self.btn_dash_history.setVisible(bool(listener_slots.load_slot_runs(
            self.manager.data_dir, self.manager.active_listener_slot)))
        self._populate_dash_volume_note()

    def _populate_dash_volume_note(self) -> None:
        ref = self._cal_state.get("volume_reference") or {}
        ref_db = ref.get("windows_volume_db")
        ref_pct = ref.get("windows_volume_percent")
        cur_db = master_volume.get_master_volume_db()
        gear_note = (ref.get("gear_note") or "").strip()
        if ref_db is None or cur_db is None or not self._dash_chips:
            self.lbl_dash_volume.setText("")
            self.btn_dash_restore_volume.setVisible(False)
            return
        drifted = abs(cur_db - ref_db) > 6.0  # volume_drift_note's threshold
        if drifted and ref_pct is not None:
            note = (f"Your Windows volume differs from the {ref_pct:.0f}% "
                    "these points were measured at -- new points would not "
                    "be comparable until it's restored.")
            if gear_note:
                note += f' Your hardware note: "{gear_note}".'
            self.lbl_dash_volume.setText(note)
            self.btn_dash_restore_volume.setVisible(True)
        else:
            self.lbl_dash_volume.setText(
                f'Hardware note from your calibration: "{gear_note}".'
                if gear_note else "")
            self.btn_dash_restore_volume.setVisible(False)

    def _on_restore_volume(self) -> None:
        ref = self._cal_state.get("volume_reference") or {}
        pct = ref.get("windows_volume_percent")
        if pct is not None:
            master_volume.set_master_volume_percent(pct)
        self._populate_dash_volume_note()

    def _on_dash_point_clicked(self, ear: str, freq: int) -> None:
        self._begin_pass([(ear, freq)])

    def _on_dash_start(self) -> None:
        remaining = self._remaining_pairs()
        if not remaining:
            # Everything measured: Start means a full re-run.
            remaining = [(ear, freq) for freq in progressive_order()
                         for ear in ("left", "right")]
        self._begin_pass(remaining)

    def _on_dash_retest_flagged(self) -> None:
        # A gave-up point is not an "outlier" -- it is a known limit, and it has
        # its own re-test affordance (its chip). Flagged means "reads oddly
        # RELATIVE to the rest of this calibration", which needs real readings.
        raw = listener_slots.calibration_raw(self._cal_state, measured_only=True)
        flags = frequency_flags(normalise(raw))
        pairs = [(ear, freq) for freq in progressive_order()
                 for ear in ("left", "right") if freq in flags]
        if pairs:
            self._begin_pass(pairs)

    def _on_dash_clear(self) -> None:
        # Same directly-constructed, Cancel-default confirmation shape as
        # Test B's consent gate -- a stray Enter/Escape never clears.
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Clear Calibration")
        msg.setText("Forget every measured point for this listener and "
                    "start over? Saved run history is kept.")
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        msg.setEscapeButton(QMessageBox.StandardButton.Cancel)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return
        listener_slots.clear_calibration_state(
            self.manager.data_dir, self.manager.active_listener_slot)
        self._cal_state = {"points": {"left": {}, "right": {}},
                           "volume_reference": None}
        self.manager.invalidate_listener_correction_cache()
        self._populate_dashboard()

    def _on_manage_history(self) -> None:
        slot_id = self.manager.active_listener_slot
        label = listener_slots.load_slot_names(self.manager.data_dir).get(
            slot_id, f"Listener {slot_id}")
        dlg = CalibrationHistoryDialog(
            self.manager.data_dir, slot_id, label, self.theme, parent=self)
        dlg.history_changed.connect(self._on_history_changed)
        dlg.exec()

    def _on_history_changed(self) -> None:
        # A deletion may have rolled the working state back (or cleared it
        # outright) -- reload from disk rather than patching self._cal_state
        # in place, since recompile_calibration_from_runs() rewrites it as a
        # whole new dict.
        self._cal_state = listener_slots.load_calibration_state(
            self.manager.data_dir, self.manager.active_listener_slot)
        self.manager.invalidate_listener_correction_cache()
        self._populate_dashboard()

    def _begin_pass(self, pairs: list[tuple[str, int]]) -> None:
        """Queue *pairs* and route to the right page: the volume-prep page
        the first time this dialog session makes sound (its reference clip
        must play through the raw signal path, so bypass/engine engage
        here), straight to the test page afterwards."""
        self._pending_pairs = list(pairs)
        if self._engine is None:
            self._engine = self._tone_engine_factory()
        if self._bypass_guard is None:
            self._bypass_guard = BypassGuard(self.manager)
            self._bypass_guard.__enter__()
        if self._played_volume_ref:
            self._start_pass()
            return
        self._volume_reference = None
        self._populate_volume_reference_hint()
        self.freq_stack.setCurrentWidget(self.freq_intro_page)

    def _populate_volume_reference_hint(self) -> None:
        """Show what volume the listener settled on last time (if this
        Listener Slot has a prior run with one), and prefill the gear-note
        and measured-SPL fields with their own last values as a starting
        point rather than a blank box -- see
        listener_slots.latest_volume_reference's docstring for why this is
        a same-machine anchor, not a portable number."""
        last = listener_slots.latest_volume_reference(
            self.manager.data_dir, self.manager.active_listener_slot)
        self._prior_volume_reference = last
        if last is None:
            self.lbl_volume_ref_hint.setText("")
            self.edit_gear_note.setText("")
            self.spin_measured_spl.setValue(0.0)
            return
        percent = last.get("windows_volume_percent")
        percent_text = f"{percent:.0f}%" if percent is not None else "not recorded"
        gear_note = (last.get("gear_note") or "").strip()
        measured_spl = last.get("measured_spl_db")
        captured_at = last.get("captured_at") or "a previous run"
        hint = f"Last time ({captured_at}): Windows volume was {percent_text}."
        if gear_note:
            hint += f' You noted: "{gear_note}".'
        if measured_spl is not None:
            hint += f" You measured {measured_spl:.1f} dB SPL."
        self.lbl_volume_ref_hint.setText(hint)
        self.edit_gear_note.setText(gear_note)
        self.spin_measured_spl.setValue(float(measured_spl) if measured_spl is not None else 0.0)

    # ── Test A: intro page ───────────────────────────────────────────

    def _build_freq_intro_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("FREQUENCY CALIBRATION"))

        disclaimer = QLabel(_FREQ_DISCLAIMER)
        disclaimer.setWordWrap(True)
        disclaimer.setProperty("class", "muted")
        lay.addWidget(disclaimer)

        instruction = QLabel(
            "RUMBLE will temporarily bypass your EQ profile for the "
            "duration of the calibration, and restore it when you're done.")
        instruction.setWordWrap(True)
        lay.addWidget(instruction)

        ref_frame = HudFrame(class_name="panel")
        ref_frame.set_theme(self.theme)
        ref_lay = QVBoxLayout(ref_frame)
        ref_lay.setContentsMargins(dp(14, 10), dp(14, 10), dp(14, 10), dp(14, 10))
        ref_lay.setSpacing(8)

        ref_heading = QLabel("Set your volume")
        ref_heading.setProperty("class", "display")
        ref_lay.addWidget(ref_heading)

        ref_instruction = QLabel(
            "Play the reference voice below, then adjust your volume — "
            "Windows volume AND any external amp, speaker, or headphone "
            "volume knob. It plays quieter, then at the target level, then "
            "louder, so you can hear where \"too quiet\" and \"too loud\" "
            "sit either side. Set your volume so the middle (target) part "
            "feels right — roughly as loud as a hair dryer, a whistling "
            "kettle, a school bell, a pedestrian crossing signal, or a "
            "whistle blast. Replay as many times as you need while you "
            "adjust.")
        ref_instruction.setWordWrap(True)
        ref_instruction.setProperty("class", "muted")
        ref_lay.addWidget(ref_instruction)

        self.lbl_volume_ref_hint = QLabel("")
        self.lbl_volume_ref_hint.setWordWrap(True)
        self.lbl_volume_ref_hint.setProperty("class", "muted")
        ref_lay.addWidget(self.lbl_volume_ref_hint)

        self.btn_play_volume_ref = QPushButton("Play Reference Voice")
        self.btn_play_volume_ref.setMinimumHeight(dp(48, 40))
        self.btn_play_volume_ref.clicked.connect(self._on_play_volume_reference)
        describe(self.btn_play_volume_ref, "Play reference voice",
                 "Play the bracketed reference clip -- quieter, target, "
                 "louder -- at fixed, known levels. Adjust your real-world "
                 "volume against the middle part, and replay it as many "
                 "times as you like -- this is here to help you set a good "
                 "level, not a requirement to start.")
        ref_lay.addWidget(self.btn_play_volume_ref)

        sounds_row = QHBoxLayout()
        sounds_row.setSpacing(dp(6, 4))
        sounds_row.addWidget(QLabel("Or check a single sound:"))
        for key, label in REFERENCE_SOUNDS:
            btn = QPushButton(label)
            btn.setProperty("class", "ghost")
            btn.clicked.connect(lambda _checked=False, k=key: self._on_play_reference_sound(k))
            describe(btn, f"Play {label.lower()}",
                     f"Play a short {label.lower()} sample at the same "
                     "target level as the reference voice's middle "
                     "segment.")
            sounds_row.addWidget(btn)
        sounds_row.addStretch(1)
        ref_lay.addLayout(sounds_row)

        spl_row = QHBoxLayout()
        spl_row.addWidget(QLabel("Measured level (dB SPL, optional)"))
        self.spin_measured_spl = QDoubleSpinBox()
        self.spin_measured_spl.setRange(0.0, 120.0)
        self.spin_measured_spl.setDecimals(1)
        self.spin_measured_spl.setSpecialValueText("Not measured")
        self.spin_measured_spl.setValue(0.0)
        describe(self.spin_measured_spl, "Measured level in dB SPL",
                 "If you have a real SPL meter (many free phone apps "
                 "exist) and measured the actual level near your ear while "
                 "the reference voice's target segment played, enter it "
                 "here for a more accurate calibration than eyeballing it "
                 "alone. Leave at \"Not measured\" to use the default "
                 "~75 dB SPL assumption.")
        spl_row.addWidget(self.spin_measured_spl)
        spl_row.addStretch(1)
        ref_lay.addLayout(spl_row)

        gear_row = QHBoxLayout()
        gear_row.addWidget(QLabel("External volume note"))
        self.edit_gear_note = QLineEdit()
        self.edit_gear_note.setPlaceholderText(
            "e.g. amp dial at 2 o'clock, speaker knob at 40% (optional)")
        describe(self.edit_gear_note, "External volume note",
                 "Optional reminder for yourself -- e.g. where an external "
                 "amp or speaker volume knob is set -- since RUMBLE can "
                 "read your Windows volume but has no way to read a "
                 "physical knob. Saved with this run and shown again next "
                 "time so you know what to set it back to.")
        gear_row.addWidget(self.edit_gear_note, 1)
        ref_lay.addLayout(gear_row)

        ref_scope_note = QLabel(
            "This only lines up your volume the same way each time on THIS "
            "machine with THIS exact hardware — it isn't a portable "
            "measurement and won't mean anything on different speakers, "
            "headphones, or a different computer.")
        ref_scope_note.setWordWrap(True)
        ref_scope_note.setProperty("class", "muted")
        ref_scope_font = ref_scope_note.font()
        ref_scope_font.setPointSize(max(7, ref_scope_font.pointSize() - 1))
        ref_scope_note.setFont(ref_scope_font)
        ref_lay.addWidget(ref_scope_note)

        self.chk_set_loudness_ref = QCheckBox(
            "Also use this volume as my loudness-compensation baseline")
        self.chk_set_loudness_ref.setChecked(True)
        describe(self.chk_set_loudness_ref, "Set loudness baseline",
                 "When on, starting the calibration also anchors the ISO 226 "
                 "loudness compensation (Settings → Enhancements) from this "
                 "volume: the reference clip's target segment sits at "
                 "roughly 75 dB SPL (or your entered measured level, if "
                 "any), and the layer extrapolates its louder mix-reference "
                 "level from there. The compensation itself still turns on "
                 "and off in Settings.")
        ref_lay.addWidget(self.chk_set_loudness_ref)

        lay.addWidget(ref_frame)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_freq_intro_back = QPushButton("Back to List")
        self.btn_freq_intro_back.clicked.connect(self._go_to_dashboard)
        describe(self.btn_freq_intro_back, "Back to List",
                 "Return to the calibration point list without starting.")
        btn_row.addWidget(self.btn_freq_intro_back)
        btn_row.addStretch(1)

        self.btn_freq_start = QPushButton("Start")
        self.btn_freq_start.setProperty("class", "primary")
        self.btn_freq_start.clicked.connect(self._on_start_frequency_test)
        describe(self.btn_freq_start, "Start",
                 "Begin measuring the queued points. Playing the reference "
                 "voice above is there to help you set a comfortable "
                 "volume first -- it's optional, not required to start.")
        btn_row.addWidget(self.btn_freq_start)
        lay.addLayout(btn_row)

        return page

    def _on_play_volume_reference(self) -> None:
        if self._engine is None:
            return
        buffer, sample_rate = load_volume_reference()
        # Deliberately non-blocking (unlike _on_play_tone's short 3x0.2s
        # loop) -- this clip runs ~35s (bracketed quieter/target/louder
        # sequence), and freezing the whole UI for that long on every
        # replay would be a worse version of the exact "clicking does
        # nothing" feel this whole feature exists to fix. ToneEngine.play()
        # already stops any in-flight playback first, so clicking Replay
        # mid-clip just restarts it cleanly.
        self._engine.play(buffer, sample_rate)
        self._played_volume_ref = True

    def _on_play_reference_sound(self, key: str) -> None:
        """One of the short standalone reference-sound buttons (hair dryer,
        kettle whistle, etc.) -- same non-blocking play-and-forget as
        _on_play_volume_reference, reusing the same engine (already live by
        the time the intro page is showing -- _begin_pass() creates it
        before routing here)."""
        if self._engine is None:
            return
        buffer, sample_rate = load_reference_sound(key)
        self._engine.play(buffer, sample_rate)

    def _on_start_frequency_test(self) -> None:
        # Captured NOW, not when Play Reference was clicked -- Start is the
        # moment the listener has actually settled on a volume, and they
        # may have nudged Windows volume after their last replay without
        # clicking Play again.
        # spin_measured_spl's minimum (0.0) is its "Not measured" special
        # value -- a real SPL reading is never actually 0.0 (that would be
        # silence), so treating it as "no reading given" is unambiguous.
        measured_spl = self.spin_measured_spl.value()
        measured_spl_db = measured_spl if measured_spl > 0.0 else None
        self._volume_reference = {
            "windows_volume_percent": master_volume.get_master_volume_percent(),
            "windows_volume_db": master_volume.get_master_volume_db(),
            "gear_note": self.edit_gear_note.text().strip(),
            "measured_spl_db": measured_spl_db,
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        # The working state anchors to this volume too: points measured in
        # later sessions are only comparable at the same volume, which the
        # dashboard's restore-volume guard enforces from this reference.
        self._cal_state["volume_reference"] = self._volume_reference
        if self.chk_set_loudness_ref.isChecked():
            # The listener just set their volume against the reference
            # clip's target segment (~75 dB SPL assumed, see
            # loudness_layer.REFERENCE_CLIP_TARGET_PHON) -- anchor the
            # ISO 226 loudness layer from it by extrapolation (no-op when
            # the volume reading above was unavailable). A real SPL meter
            # reading, if given, replaces the assumed default for a more
            # accurate anchor.
            self.manager.calibrate_loudness_reference_from_conversation(
                self._volume_reference.get("windows_volume_db"),
                measured_level_db=measured_spl_db)
        self._start_pass()

    def _start_pass(self) -> None:
        """Begin measuring the queued points (engine/bypass already engaged
        by _begin_pass). Seeds every staircase from the persistent state,
        so a re-test or a resumed session starts near the known answer.

        Seeds are measured_only: seeding a gave-up point's re-test from the
        ceiling it already failed would open the retry at the loudest tone in
        the test and bias it straight back into another give-up. With it
        excluded, the seed interpolates from the neighbouring measured points
        instead -- an honest starting guess."""
        seed = listener_slots.calibration_raw(self._cal_state, measured_only=True)
        self._ft = FrequencyTest(pairs=self._pending_pairs, seed_thresholds=seed)
        self._total_pairs = len(self._pending_pairs)
        self._recorded_pairs = set()
        self._trail = []
        self._baseline_was_complete = baseline_complete(seed)
        self.lbl_freq_milestone.setVisible(False)
        self._paused = False
        self.btn_pause.setChecked(False)
        self._point_trial = 0
        self._seq_reps_left = 0
        self._seq_gap_deadline = None
        self._set_test_controls_enabled(True)
        self._update_test_labels()
        self._update_progress()
        self._update_point_feedback()
        self._update_live_chart()
        self._play_timer.start()
        self.freq_stack.setCurrentWidget(self.freq_test_page)
        self._queue_auto_play()

    # ── Test A: test page ─────────────────────────────────────────────

    def _build_freq_test_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("FREQUENCY CALIBRATION — LISTENING"))

        self.lbl_freq_progress = QLabel("")
        self.lbl_freq_progress.setProperty("class", "muted")
        lay.addWidget(self.lbl_freq_progress)

        self.progress_freq = QProgressBar()
        lay.addWidget(self.progress_freq)

        self.lbl_freq_milestone = QLabel(
            "Baseline complete — your calibration is usable now. Keep "
            "going for finer detail, or stop here; every finished point "
            "is already saved.")
        self.lbl_freq_milestone.setWordWrap(True)
        self.lbl_freq_milestone.setVisible(False)
        lay.addWidget(self.lbl_freq_milestone)

        # Live chart: each point lands here the moment it locks in, so the
        # listener watches their curve form instead of answering into a
        # void. Completed thresholds only -- the CURRENT tone's level is
        # deliberately never shown (seeing the level invites answering by
        # expectation instead of by ear).
        self.plot_freq_live = pg.PlotWidget(
            axisItems={"bottom": LogFreqAxis(orientation="bottom")})
        self.plot_freq_live.setMinimumHeight(dp(120, 100))
        self.plot_freq_live.showGrid(x=True, y=True, alpha=0.15)
        self.plot_freq_live.setLabel("left", "dB rel. 1 kHz")
        live_item = self.plot_freq_live.getPlotItem()
        live_item.setMouseEnabled(x=False, y=False)
        live_item.hideButtons()
        live_item.getAxis("bottom").setTicks(_tested_freq_ticks())
        self._live_curve_left = self.plot_freq_live.plot(
            [], [], symbol="o", symbolSize=5)
        self._live_curve_right = self.plot_freq_live.plot(
            [], [], symbol="o", symbolSize=5)
        lay.addWidget(self.plot_freq_live, 1)

        frame = HudFrame(class_name="panel")
        frame.set_theme(self.theme)
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(dp(14, 10), dp(14, 10), dp(14, 10), dp(14, 10))
        frame_lay.setSpacing(10)

        current_row = QHBoxLayout()
        self.lbl_current = QLabel("")
        self.lbl_current.setProperty("class", "display")
        current_row.addWidget(self.lbl_current)
        current_row.addStretch(1)
        light_caption = QLabel("TONE")
        light_caption.setProperty("class", "muted")
        current_row.addWidget(light_caption)
        self.play_light = _PlayLight(self.theme)
        describe(self.play_light, "Tone playing light",
                 "Lit while a tone is actually sounding -- if it blinks "
                 "three times and you heard nothing, answer \"I didn't "
                 "hear it\" with confidence.")
        current_row.addWidget(self.play_light)
        frame_lay.addLayout(current_row)

        # Per-point staircase feedback: the response trail ("v" = heard,
        # the level stepped down; "^" = not heard, it stepped up) and how
        # close this point is to locking in -- makes "the same tone keeps
        # coming back" legible as convergence instead of a broken button.
        self.lbl_point_trail = QLabel("")
        self.lbl_point_trail.setProperty("class", "muted")
        frame_lay.addWidget(self.lbl_point_trail)

        self.btn_play_tone = QPushButton("Replay Tone")
        self.btn_play_tone.setMinimumHeight(dp(56, 44))
        self.btn_play_tone.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play_tone.clicked.connect(self._on_play_tone)
        describe(self.btn_play_tone, "Replay tone",
                 "Each tone plays by itself after you answer -- click here "
                 "to hear the current one again (three repeats with short "
                 "pauses), as many times as you like before answering. "
                 "(R)")
        frame_lay.addWidget(self.btn_play_tone)

        answer_row = QHBoxLayout()
        self.btn_heard = QPushButton("I Heard It")
        self.btn_not_heard = QPushButton("I Didn't Hear It")
        for btn in (self.btn_heard, self.btn_not_heard):
            btn.setProperty("class", "primary")
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        describe(self.btn_heard, "I heard it",
                 "Record that you heard the tone. (Space or H)")
        describe(self.btn_not_heard, "I didn't hear it",
                 "Record that you did not hear the tone. (N)")
        self.btn_heard.clicked.connect(lambda: self._on_response(True))
        self.btn_not_heard.clicked.connect(lambda: self._on_response(False))
        answer_row.addWidget(self.btn_heard)
        answer_row.addWidget(self.btn_not_heard)
        frame_lay.addLayout(answer_row)

        lay.addWidget(frame)

        bottom_row = QHBoxLayout()
        self.btn_skip = QPushButton("Skip")
        self.btn_skip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_skip.clicked.connect(self._on_skip)
        describe(self.btn_skip, "Skip",
                 "Mark this point as unmeasured and move on -- useful for "
                 "16 kHz on hardware/listeners that can't reproduce it.")
        bottom_row.addWidget(self.btn_skip)

        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._on_pause_toggled)
        describe(self.btn_pause, "Pause",
                 "Pause the calibration -- input is ignored until you resume. "
                 "The EQ bypass stays engaged while paused.")
        bottom_row.addWidget(self.btn_pause)
        bottom_row.addStretch(1)

        self.btn_freq_test_back = QPushButton("Back to List")
        self.btn_freq_test_back.clicked.connect(self._go_to_dashboard)
        describe(self.btn_freq_test_back, "Back to List",
                 "Stop and return to the point list. Every point you "
                 "finished is already saved -- only the tone currently "
                 "being narrowed down is dropped.")
        bottom_row.addWidget(self.btn_freq_test_back)
        lay.addLayout(bottom_row)

        # Keyboard shortcuts, scoped to this page (WidgetWithChildrenShortcut)
        # so Space/H/N never fire while another page is showing. The answer
        # buttons themselves are NoFocus (above) so a stray Space activating
        # a focused button can't also double-fire the shortcut.
        self._freq_shortcuts = []
        for key, target in (
            ("Space", self.btn_heard), ("H", self.btn_heard), ("N", self.btn_not_heard),
            ("R", self.btn_play_tone),
        ):
            sc = QShortcut(QKeySequence(key), page)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(target.click)
            self._freq_shortcuts.append(sc)

        return page

    def _set_test_controls_enabled(self, enabled: bool) -> None:
        for w in (self.btn_play_tone, self.btn_heard, self.btn_not_heard, self.btn_skip):
            w.setEnabled(enabled)

    def _on_pause_toggled(self, checked: bool) -> None:
        self._paused = checked
        self.btn_pause.setText("Resume" if checked else "Pause")
        self._set_test_controls_enabled(not checked)
        if not checked:
            # Resuming: replay the current tone rather than leaving the
            # listener staring at an answer prompt for a tone they may not
            # have heard before pausing.
            self._queue_auto_play()

    def _update_test_labels(self) -> None:
        if self._ft is None or self._ft.current is None:
            return
        ear, freq = self._ft.current
        self.lbl_current.setText(f"{ear.capitalize()} ear — {_freq_label(freq)}")

    def _update_progress(self) -> None:
        if self._ft is None:
            return
        completed = len(self._ft.results.get("left", {})) + len(self._ft.results.get("right", {}))
        self.progress_freq.setMaximum(max(self._total_pairs, 1))
        self.progress_freq.setValue(completed)
        # Confirms every click actually registers, even on points that take
        # several responses to narrow in on a threshold (the staircase
        # search needs multiple reversals per point -- one click does not
        # always mean one point) -- without this, a point mid-search looks
        # visually identical before and after a response, which reads as
        # "the button did nothing."
        trial_note = (
            f" — response {self._point_trial} recorded on this tone"
            if self._point_trial > 0 else "")
        self.lbl_freq_progress.setText(
            f"{completed}/{self._total_pairs} points measured{trial_note}")

    def _on_play_tone(self) -> None:
        """Begin one three-repeat presentation of the current tone --
        non-blocking: the first repeat starts here, the poll timer
        (_on_play_tick) sequences the rest and drives the playing light."""
        if self._ft is None or self._engine is None or self._paused:
            return
        if self._ft.current is None:
            return
        self._seq_reps_left = 3
        self._seq_gap_deadline = None
        self._play_rep()

    def _play_rep(self) -> None:
        if self._ft is None or self._engine is None or self._ft.current is None:
            self._seq_reps_left = 0
            return
        ear, freq = self._ft.current
        level = self._ft.staircase.level if self._ft.staircase is not None else -30.0
        self._seq_reps_left -= 1
        start_tone(self._engine, freq, level, ear,
                   self.manager.effective_sample_rate(), duration_s=0.2)
        self.play_light.set_on(True)

    def _cancel_tone_sequence(self) -> None:
        self._seq_reps_left = 0
        self._seq_gap_deadline = None
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                pass
        self.play_light.set_on(False)

    def _on_play_tick(self) -> None:
        """30 ms poll: light tracks engine.is_playing exactly, and the
        150 ms inter-repeat gaps are timed here instead of time.sleep --
        the event loop keeps running, so answer/pause clicks land
        immediately even mid-tone."""
        playing = self._engine is not None and self._engine.is_playing
        self.play_light.set_on(playing)
        if playing or self._paused or self._seq_reps_left <= 0:
            return
        now = time.monotonic()
        if self._seq_gap_deadline is None:
            self._seq_gap_deadline = now + _REPEAT_GAP_S
        elif now >= self._seq_gap_deadline:
            self._seq_gap_deadline = None
            self._play_rep()

    def _queue_auto_play(self) -> None:
        """Schedule the current tone to play by itself shortly (see
        _AUTO_PLAY_DELAY_MS) -- called on test start, after every answer,
        and on resume, so the listener only ever clicks the Replay button
        when they actually want to hear a tone again. The _auto_play guard
        re-checks state at fire time because a queued singleShot can outlive
        the situation that queued it (test finished, paused, page left)."""
        QTimer.singleShot(_AUTO_PLAY_DELAY_MS, self._auto_play)

    def _auto_play(self) -> None:
        if (self._ft is None or self._ft.done or self._paused
                or self.freq_stack.currentWidget() is not self.freq_test_page):
            return
        self._on_play_tone()

    def _on_response(self, heard: bool) -> None:
        if self._ft is None or self._paused or self._ft.done:
            return
        self._cancel_tone_sequence()  # answering mid-tone cuts it off
        pair_before = self._ft.current
        self._trail.append(heard)
        self._ft.respond(heard)
        self._after_advance(pair_before)

    def _on_skip(self) -> None:
        if self._ft is None or self._paused or self._ft.done:
            return
        self._cancel_tone_sequence()
        pair_before = self._ft.current
        self._ft.skip()
        self._after_advance(pair_before)

    def _record_completed_points(self) -> None:
        """Write every pair this pass has resolved (measured, skipped, or
        auto-skipped by the directional give-up) into the persistent state
        the moment it completes -- leaving mid-pass keeps everything but
        the point still being narrowed down."""
        changed = False
        for ear in ("left", "right"):
            for freq, value in self._ft.results[ear].items():
                pair = (ear, freq)
                if pair in self._recorded_pairs:
                    continue
                self._recorded_pairs.add(pair)
                listener_slots.record_calibration_point(
                    self._cal_state, ear, freq, value,
                    gave_up=pair in self._ft.gave_up_pairs)
                changed = True
        if changed:
            listener_slots.save_calibration_state(
                self.manager.data_dir, self.manager.active_listener_slot,
                self._cal_state)
            self.manager.invalidate_listener_correction_cache()
            self._update_live_chart()
            raw = listener_slots.calibration_raw(self._cal_state, measured_only=True)
            if not self._baseline_was_complete and baseline_complete(raw):
                self._baseline_was_complete = True
                self.lbl_freq_milestone.setVisible(True)

    def _after_advance(self, pair_before: tuple[str, int] | None) -> None:
        # Same (ear, freq) pair as before this response -> the staircase
        # isn't done narrowing in yet, so count this response toward this
        # point's on-screen counter. A new pair (or finished) -> the point
        # that just completed is behind us, so the counter resets for
        # whatever's next.
        if self._ft.current == pair_before:
            self._point_trial += 1
        else:
            self._point_trial = 0
            self._trail = []
            self._record_completed_points()
        self._update_progress()
        self._update_point_feedback()
        if self._ft.done:
            self._finish_frequency_test()
        else:
            self._update_test_labels()
            self._queue_auto_play()

    def _update_point_feedback(self) -> None:
        """The per-point response trail + convergence meter."""
        if self._ft is None or self._ft.staircase is None:
            self.lbl_point_trail.setText("")
            return
        st = self._ft.staircase
        trail = " ".join("v" if heard else "^" for heard in self._trail)
        parts = []
        if trail:
            parts.append(f"Responses: {trail}")
        parts.append(f"Narrowing: {st.reversals}/{st.reversals_needed} reversals")
        self.lbl_point_trail.setText("   ".join(parts))

    def _plottable_curve(self, norm: dict, ear: str) -> tuple[list[float], list[float]]:
        """This ear's normalised curve with GAVE-UP points removed.

        A gave-up point's stored threshold is the ceiling level -- "at or
        beyond the loudest we can present", not a measured threshold. Drawn
        as a real datum it is both a lie and a scale-wrecker: normalised
        against a typical 1 kHz anchor it lands near +48 dB, flattening every
        genuine reading into the bottom of the chart. They stay visible on
        the dashboard as "limit" chips, which is the honest way to show them.

        Every `norm` this dialog builds is now already measured_only (see
        _populate_dashboard), so this is a backstop rather than the primary
        filter -- it also covers a `norm` arriving from a saved run, whose raw
        dict predates the exclusion.
        """
        data = norm.get(ear, {})
        entries = self._cal_state.get("points", {}).get(ear, {})
        freqs = [f for f in sorted(data)
                 if not (entries.get(f) or {}).get("gave_up")]
        return [float(np.log10(f)) for f in freqs], [data[f] for f in freqs]

    def _update_live_chart(self) -> None:
        norm = normalise(
            listener_slots.calibration_raw(self._cal_state, measured_only=True))
        for curve, ear in ((self._live_curve_left, "left"),
                           (self._live_curve_right, "right")):
            curve.setData(*self._plottable_curve(norm, ear))

    def _finish_frequency_test(self) -> None:
        self._record_completed_points()  # the pass's last point
        self._play_timer.stop()
        self.play_light.set_on(False)
        # The results page shows the WHOLE working calibration (every
        # session's points), not just the pass that finished.
        self._raw = listener_slots.calibration_raw(self._cal_state, measured_only=True)
        self._norm = normalise(self._raw)
        # ...and the gave-up points it excludes, kept for the export, which
        # must still REPORT what the math drops.
        self._no_response = listener_slots.no_response_freqs(self._cal_state)
        # Snapshot the state into the slot's immutable run history -- the
        # overlay combo / 10-run retention keep working, and live
        # correction already reads the state itself.
        listener_slots.save_slot_result(
            self.manager.data_dir, self.manager.active_listener_slot, self._norm, self._raw,
            volume_reference=self._volume_reference
            or self._cal_state.get("volume_reference"))
        self.manager.invalidate_listener_correction_cache()
        # Deliberately does NOT call _end_frequency_test() here: the
        # BypassGuard stays engaged while the results page is showing (spec:
        # "Exiting this page ... exits the BypassGuard") -- only leaving the
        # results page (closing the dialog, or Back to List) restores it.
        self._populate_results_page()
        self.freq_stack.setCurrentWidget(self.freq_results_page)

    def _go_to_dashboard(self) -> None:
        """Back to the point list from the prep/test/results pages --
        releases bypass (the list itself makes no sound) and re-renders the
        chips from the state, which already holds every finished point."""
        self._end_frequency_test()
        self._populate_dashboard()
        self.freq_stack.setCurrentWidget(self.freq_dash_page)

    def _go_to_picker(self) -> None:
        self._end_frequency_test()
        self.stack.setCurrentWidget(self.picker_page)

    # ── Test A: results page ──────────────────────────────────────────

    def _build_freq_results_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("FREQUENCY CALIBRATION — RESULTS"))

        self.plot_freq = pg.PlotWidget(axisItems={"bottom": LogFreqAxis(orientation="bottom")})
        self.plot_freq.setMinimumHeight(dp(220, 180))
        self.plot_freq.showGrid(x=True, y=True, alpha=0.15)
        self.plot_freq.setLabel("left", "dB rel. own 1 kHz")
        plot_item = self.plot_freq.getPlotItem()
        plot_item.setMouseEnabled(x=False, y=False)
        plot_item.hideButtons()
        plot_item.getAxis("bottom").setTicks(_tested_freq_ticks())

        self._freq_curve_left = self.plot_freq.plot([], [], name="Left")
        self._freq_curve_right = self.plot_freq.plot([], [], name="Right")
        self._freq_curve_prior = self.plot_freq.plot([], [])
        self._freq_fill = pg.FillBetweenItem(self._freq_curve_left, self._freq_curve_right)
        plot_item.addItem(self._freq_fill)

        lay.addWidget(self.plot_freq, 1)

        self.lbl_freq_summary = QLabel("")
        self.lbl_freq_summary.setProperty("class", "display")
        self.lbl_freq_summary.setWordWrap(True)
        lay.addWidget(self.lbl_freq_summary)

        self.lbl_freq_flags = QLabel("")
        self.lbl_freq_flags.setProperty("class", "muted")
        self.lbl_freq_flags.setWordWrap(True)
        flags_font = self.lbl_freq_flags.font()
        flags_font.setPointSize(max(7, flags_font.pointSize() - 1))
        self.lbl_freq_flags.setFont(flags_font)
        lay.addWidget(self.lbl_freq_flags)

        self.lbl_freq_volume_drift = QLabel("")
        self.lbl_freq_volume_drift.setProperty("class", "muted")
        self.lbl_freq_volume_drift.setWordWrap(True)
        self.lbl_freq_volume_drift.setFont(flags_font)
        lay.addWidget(self.lbl_freq_volume_drift)

        prev_row = QHBoxLayout()
        prev_row.addWidget(QLabel("Previous results"))
        self.cmb_freq_previous = QComboBox()
        self.cmb_freq_previous.addItem("(none)", None)
        self.cmb_freq_previous.currentIndexChanged.connect(self._on_previous_result_selected)
        describe(self.cmb_freq_previous, "Previous results",
                 "Overlay an earlier saved run as a dashed reference curve.")
        prev_row.addWidget(self.cmb_freq_previous, 1)
        lay.addLayout(prev_row)

        btn_row = QHBoxLayout()
        self.btn_freq_results_back = QPushButton("Back to List")
        self.btn_freq_results_back.clicked.connect(self._go_to_dashboard)
        describe(self.btn_freq_results_back, "Back to List",
                 "Return to the calibration point list -- re-test any "
                 "point from there, or continue with unmeasured ones.")
        btn_row.addWidget(self.btn_freq_results_back)

        self.btn_freq_save = QPushButton("Save Results to File…")
        self.btn_freq_save.clicked.connect(self._on_save_freq_results)
        describe(self.btn_freq_save, "Save results to file",
                 "Save a plain-text export of this calibration's per-ear "
                 "frequency table and imbalance table -- intended to be "
                 "shared with an AI to author a compensation profile.")
        btn_row.addWidget(self.btn_freq_save)
        btn_row.addStretch(1)

        self.btn_freq_done = QPushButton("Done")
        self.btn_freq_done.setProperty("class", "primary")
        self.btn_freq_done.clicked.connect(self.accept)
        describe(self.btn_freq_done, "Done", "Close Ear Calibration.")
        btn_row.addWidget(self.btn_freq_done)
        lay.addLayout(btn_row)

        return page

    def _apply_freq_chart_theme(self) -> None:
        t = self.theme
        muted = t.get("text_muted", "#8589A8")
        self.plot_freq.setBackground(_qcolor(t.get("graph_bg", "#101018")))
        plot_item = self.plot_freq.getPlotItem()
        for axis_name in ("bottom", "left"):
            axis = plot_item.getAxis(axis_name)
            axis.setPen(pg.mkPen(_qcolor(t.get("graph_text", muted))))
            axis.setTextPen(pg.mkPen(_qcolor(t.get("graph_text", muted))))

        left_color = t.get("accent", "#4660E6")
        right_color = t.get("graph_band_sel", "#C76A0E")
        self._freq_curve_left.setPen(pg.mkPen(_qcolor(left_color), width=2.0))
        self._freq_curve_right.setPen(pg.mkPen(_qcolor(right_color), width=2.0))

        # The test page's live chart mirrors the results chart's theming
        # (same left/right colour identity, so the curve the listener
        # watched form is recognisably the one the results page shows).
        self.plot_freq_live.setBackground(_qcolor(t.get("graph_bg", "#101018")))
        live_item = self.plot_freq_live.getPlotItem()
        for axis_name in ("bottom", "left"):
            axis = live_item.getAxis(axis_name)
            axis.setPen(pg.mkPen(_qcolor(t.get("graph_text", muted))))
            axis.setTextPen(pg.mkPen(_qcolor(t.get("graph_text", muted))))
        for curve, color in ((self._live_curve_left, left_color),
                             (self._live_curve_right, right_color)):
            curve.setPen(pg.mkPen(_qcolor(color), width=1.5))
            curve.setSymbolBrush(_qcolor(color))
            curve.setSymbolPen(pg.mkPen(_qcolor(color)))
        self._freq_curve_prior.setPen(
            pg.mkPen(_alpha(muted, 200), width=1.5, style=Qt.PenStyle.DashLine))
        self._freq_fill.setBrush(pg.mkBrush(_alpha(left_color, 40)))

        zero_pen = pg.mkPen(_qcolor(t.get("graph_zero", muted)), width=1,
                             style=Qt.PenStyle.DashLine)
        zero_line = pg.InfiniteLine(pos=0.0, angle=0, pen=zero_pen)
        zero_line.setZValue(-10)
        plot_item.addItem(zero_line)

    def _populate_results_page(self) -> None:
        # Same gave-up exclusion as the live chart -- see _plottable_curve().
        self._freq_curve_left.setData(*self._plottable_curve(self._norm, "left"))
        self._freq_curve_right.setData(*self._plottable_curve(self._norm, "right"))
        self._freq_curve_prior.setData([], [])

        self.lbl_freq_summary.setText(summarise(self._norm))
        flags = frequency_flags(self._norm)
        if flags:
            flagged = ", ".join(_freq_label(f) for f in flags)
            self.lbl_freq_flags.setText(
                f"Note: {flagged} read unusually relative to the rest of "
                "this same calibration -- not a diagnosis, just worth a "
                "second look.")
        else:
            self.lbl_freq_flags.setText("")

        self.lbl_freq_volume_drift.setText(
            volume_drift_note(self._volume_reference, self._prior_volume_reference))

        self.cmb_freq_previous.blockSignals(True)
        self.cmb_freq_previous.clear()
        self.cmb_freq_previous.addItem("(none)", None)
        try:
            previous = listener_slots.load_slot_runs(
                self.manager.data_dir, self.manager.active_listener_slot)
        except Exception:
            previous = []
        for i, payload in enumerate(previous):
            self.cmb_freq_previous.addItem(f"Run {i + 1}", payload)
        self.cmb_freq_previous.setCurrentIndex(0)
        self.cmb_freq_previous.blockSignals(False)

    def _on_previous_result_selected(self, index: int) -> None:
        payload = self.cmb_freq_previous.itemData(index)
        if not payload:
            self._freq_curve_prior.setData([], [])
            return
        # Overlays the left ear's curve as the single dashed reference (the
        # spec calls for "a dashed prior curve", singular) -- left is as
        # representative a choice as right; both ears' full data is still in
        # the loaded payload if a future task wants to show more.
        prior_left = payload.get("norm", {}).get("left", {})
        freqs = sorted(prior_left)
        self._freq_curve_prior.setData(
            [np.log10(f) for f in freqs], [prior_left[f] for f in freqs])

    def _on_save_freq_results(self) -> None:
        try:
            from rumble.core import device_format as _df
            fmt = _df.get_default_device_format()
            device_label = fmt.label if fmt is not None else "Unknown device"
        except Exception:
            device_label = "Unknown device"
        text = export_text(self._norm, self._raw, device_label,
                            volume_reference=self._volume_reference,
                            no_response=self._no_response)
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save Results to File", "hearing_frequency_test.txt",
            "Text File (*.txt)")
        if not path_str:
            return
        Path(path_str).write_text(text, encoding="utf-8")

    # ── exit paths always restore bypass / stop any live playback ──────

    def _end_frequency_test(self) -> None:
        self._play_timer.stop()
        self._seq_reps_left = 0
        self._seq_gap_deadline = None
        if hasattr(self, "play_light"):
            self.play_light.set_on(False)
        if self._bypass_guard is not None:
            guard = self._bypass_guard
            self._bypass_guard = None
            guard.__exit__(None, None, None)
        if self._engine is not None:
            engine = self._engine
            self._engine = None
            try:
                engine.stop()
            except Exception:
                pass
        self._ft = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._end_frequency_test()
        super().closeEvent(event)

    def reject(self) -> None:
        self._end_frequency_test()
        super().reject()

    def accept(self) -> None:
        self._end_frequency_test()
        super().accept()
