"""Room-measurement wizard (Campaign C3).

Consumes the Qt-free measurement toolbox built across S9-S12
(``rumble/core/audio/mic_capture.py``, ``rumble/core/mic_cal.py``,
``rumble/core/measurement.py``) without reimplementing any of its logic.
Shape mirrors ``rumble/ui/hearing_dialog.py``'s established pattern: a single
``QStackedWidget`` (``self.stack``), one ``_build_x_page()`` method per page,
constructor-injectable seams so tests never touch real audio hardware, and a
``BypassGuard`` (reused from ``rumble/core/hearing_test.py`` -- the same
class ``HearingSuiteDialog`` uses) engaged once real measurement work starts
and released on every exit path via one idempotent ``_teardown()``.

Page flow: intro/safety -> mic & output setup -> level + AGC sanity check ->
baseline measurement (N positions) -> target choice -> results -> verify
(STUB, filled by S15) -> done.

S14 ("C3b: baseline -> target -> correction wiring") replaced S13's stubs
with the real chain: ``_synchronized_record`` (a shared, properly
synchronized play+record helper -- mirrors
``rumble/core/time_alignment.py``'s ``run_measurement()``: record on a
background thread, sleep ``lead_in_s``, play, wait for playback, join the
recording thread with a timeout) is now the ONE real ``record_fn`` the
wizard uses everywhere it needs play+record, replacing S13's crude,
unsynchronized ``_default_record_fn`` entirely (per the S14 Amendment in the
progress file). ``_default_measure_position`` runs a real ESS sweep through
it, deconvolves, converts to magnitude, and subtracts the selected mic
calibration (S10). The results page runs the same
``eq_target.make_correction`` pipeline the Spinorama import flow uses and
saves the result as a profile tagged "Room Correction".

Seams (constructor kwargs, all default to the real S9-S12 functions; tests
inject fakes):
- ``tone_engine_factory`` -- defaults to ``ToneEngine`` (mirrors
  ``HearingSuiteDialog``'s own seam of the same name).
- ``mic_recorder_factory`` -- defaults to ``MicRecorder``.
- ``list_input_devices_fn`` -- defaults to ``mic_capture.list_input_devices``.
- ``list_cals_fn`` -- defaults to ``mic_cal.list_cals``.
- ``correction_band_fn`` -- defaults to ``mic_cal.correction_band``.
- ``agc_check_fn`` -- defaults to ``measurement.agc_check``; its
  ``record_fn`` is ``self._synchronized_record``.
- ``measure_position_fn`` -- defaults to ``_default_measure_position``, the
  real baseline capture (ESS sweep via ``_synchronized_record`` -> deconvolve
  -> magnitude -> mic-cal subtraction).
- ``transfer_capture_fn`` -- defaults to ``_capture_raw_curve`` (S16), the
  same ESS sweep chain with NO cal subtraction.

S15 ("C3c: verify pass + results overlay") fills in ``_build_verify_page()``
(S13's stub): the saved room-correction profile is previewed via
``manager.preview_config()`` (NOT activated), the SAME ``measure_position_fn``
seam re-measures the corrected system across the SAME position count as the
baseline, and the averaged/smoothed result is plotted against the baseline
and target curves with an achieved-vs-predicted RMS-deviation-from-target
summary. The verify pass's own preview/bypass override
(``_start_verify_preview``/``_end_verify_preview``) is layered on top of the
wizard-wide ``BypassGuard`` (which stays forced-True the whole time) -- ending
it is idempotent and happens on every exit path (measurement completion,
Back, and ``_teardown()``), including when a position measurement raises.

S16 ("C4a: transfer calibration wizard") adds a compact sub-flow reachable
from the mic setup page's "Calibrate this mic against a reference..."
button: pick a reference calibration -> measure ``TRANSFER_PASSES`` (2)
passes with the reference mic, averaged via ``autoeq_merge.average_curves``
-> swap to the unknown mic, measure 2 more passes, averaged the same way ->
``mic_cal.derive_transfer_cal`` against the reference cal's own loaded curve
-> saved as a normal ``source="transfer"`` calibration file and selected on
the setup page for the rest of the session. Reuses ``_capture_raw_curve``
(the same ESS sweep + ``_synchronized_record`` + deconvolve + magnitude
chain the baseline page uses, factored out of ``_default_measure_position``
this session) rather than duplicating any measurement machinery -- per
roadmap C4, "pure reuse of C2's sweep machinery + one curve subtraction".

S17 ("C4b: spinorama-referenced mic estimation") adds a second, similarly
compact sub-flow from the same setup page ("Estimate calibration from a
speaker I own..."): pick a speaker from the Spinorama database (reusing
``rumble.ui.autoeq_dialog._MeasurementPanel`` -- the same reusable search
panel the unified measurement browser already uses, given a bare
``SpinoramaProvider`` catalogue so only speakers are offered, per roadmap
C4) -> run ONE gated ESS measurement (``_capture_raw_ir``, the IR-only half
of ``_capture_raw_curve`` split out this session so the IR can be
time-gated before magnitude conversion; the gate itself is chosen from the
IR's own first-reflection arrival via
``measurement.detect_first_reflection_gate_ms``, shown to the user) -> fetch
the speaker's published anechoic On Axis curve (``spinorama.fetch_curve``,
NOT the ``SpinoramaProvider.fetch_curve`` wrapper other importers use --
that wrapper turns a speaker's curve into a finished EQ correction via
``eq_target.make_correction``, whereas this sub-flow needs the raw
published curve as ground truth for ``mic_cal.derive_estimated_cal``) ->
save as a normal ``source="estimated"`` calibration (band from
``mic_cal.estimated_band(gate_ms)``) and select it on the setup page.
``_generate_correction()`` reads ``mic_cal.ESTIMATED_TIER_MAX_BOOST_DB``
(instead of the standard 6 dB cap) whenever the selected calibration's
source is "estimated", per the roadmap's tier-integration requirement.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from rumble.core import apo_config, autoeq_merge, eq_target, mic_cal, spinorama, target_curves
from rumble.core import measurement
from rumble.core.audio.engine import ToneEngine
from rumble.core.audio.mic_capture import InputDevice, MicRecorder, list_input_devices
from rumble.core.hearing_test import BypassGuard
from rumble.core.measurements.spinorama_provider import SpinoramaProvider
from rumble.ui.a11y import describe
from rumble.ui.autoeq_dialog import _MeasurementPanel
from rumble.ui.density import dp
from rumble.ui.eq_graph import LogFreqAxis
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH, FramelessDialog,
    scrollable_page,
)
from rumble.ui.hud_frame import HudFrame

# Shared synchronized play+record helper's lead-in/trail margins (S14
# Amendment): identical values to rumble/core/time_alignment.py's
# run_measurement() defaults -- this helper mirrors that function's pattern
# exactly, not a new scheme.
_RECORD_LEAD_IN_S = 0.3
_RECORD_TRAIL_S = 0.3

# Baseline sweep range/duration (S14). Longer than the AGC check's or
# time_alignment's own 1 s sweeps (neither needs full-band spectral
# accuracy, only a level or arrival-time comparison) -- a documented,
# considered choice for adequate low-frequency deconvolution SNR across a
# multi-position pass, not derived from an external standard (same category
# as measurement.AGC_TOLERANCE_DB's own "explicit allowance, not a cited
# standard").
_BASELINE_SWEEP_DURATION_S = 3.0
# S11 progress-file handoff ("Sweep with margin"): request f1/f2 wider than
# the frequency range you intend to report accuracy for. f1/f2 reuse the
# exact ratio tests/selftest_measurement.py's own accuracy checks use
# (10.0 Hz / 23500.0 Hz at 48000 Hz = a 0.4896 Nyquist fraction), scaled to
# whatever sample rate is actually in effect.
_BASELINE_SWEEP_F1_HZ = 10.0
_BASELINE_SWEEP_F2_CEILING_HZ = 23500.0
_BASELINE_SWEEP_F2_NYQUIST_FRACTION = 23500.0 / 48000.0


def _baseline_sweep_range(sample_rate: int) -> tuple[float, float]:
    f2 = min(_BASELINE_SWEEP_F2_CEILING_HZ, _BASELINE_SWEEP_F2_NYQUIST_FRACTION * sample_rate)
    return _BASELINE_SWEEP_F1_HZ, f2


def _estimate_low_extension_hz(curve: list) -> float:
    """Estimate a speaker's low-frequency -6 dB extension from the measured
    baseline curve itself.

    A room measurement has no manufacturer-published ref_3db/ref_6db the way
    a Spinorama catalog entry does (rumble/core/spinorama.py) -- so the
    reference level here is the mean gain across a typically-flat 200-2000 Hz
    midband, and the extension is the lowest frequency (scanning up from the
    bottom) at which the curve first comes back within 6 dB of that
    reference. Feeds eq_target.suggested_highpass_hz() the same way
    Spinorama's published extension estimate does, per the roadmap's "the
    subsonic high-pass offer mirrors the Spinorama import's existing
    behavior" instruction.
    """
    if not curve:
        return 0.0
    mid = [g for f, g in curve if 200.0 <= f <= 2000.0]
    reference = (sum(mid) / len(mid)) if mid else (sum(g for _, g in curve) / len(curve))
    threshold = reference - 6.0
    for f, g in curve:
        if g >= threshold:
            return f
    return curve[0][0]

# S17: the standard boost cap _correction_max_boost_db() falls back to for
# every tier except "estimated" -- eq_target.make_correction's own default,
# named here so _generate_correction() never has to special-case "pass
# nothing" vs. "pass the tightened cap".
_STANDARD_MAX_BOOST_DB = 6.0

# Same tilt range as Settings -> Listeners' spinorama_default_tilt control
# (rumble/ui/settings_dialog.py's spin_default_tilt): -3.0 dB/oct (cut) to
# +1.0 dB/oct (boost), the asymmetric range documented there (S4's Decisions
# log flagged the asymmetry; reused verbatim here rather than re-deriving a
# different range for the same physical control).
_TILT_MIN_DB_PER_OCT = -3.0
_TILT_MAX_DB_PER_OCT = 1.0

# Sentinel combo-box item data for "linear tilt" (not a named target_curves
# entry) on the target page.
_TILT_TARGET_KEY = "_tilt"

_SAFETY_TEXT = (
    "This plays test sweeps and noise through your speakers at a moderate "
    "but clearly audible level. Turn your volume down before starting, then "
    "bring it back up to a normal, safe level once sound is playing "
    "steadily. RUMBLE will temporarily bypass your EQ profile for the "
    "duration of the measurement, and restore it when you're done or if "
    "you cancel."
)

_SPEAKERS_ONLY_NOTE = (
    "Room measurement only works for speakers -- headphone measurement "
    "without a dedicated coupler rig isn't meaningful, so it isn't offered "
    "here. Use Tools -> Browse Measurement Database (AutoEQ) for "
    "headphones."
)

_AGC_EXPLANATION = (
    "RUMBLE will play a short sweep twice, 6 dB apart, and check that your "
    "microphone captures the same 6 dB difference. Windows sound "
    "\"enhancements\" (automatic gain control, noise suppression, "
    "beamforming) can silently compress this and corrupt every measurement "
    "that follows."
)

_AGC_FAIL_INSTRUCTION = (
    "Detected level compression consistent with active microphone "
    "processing. In Windows Sound settings, open your microphone's "
    "properties and disable \"AGC\" / \"Automatic Gain Control\", \"Noise "
    "Suppression\", and \"Beamforming\", then run the check again."
)


def _page_heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "heading")
    return label


def _qcolor(value: str):
    """Parse a theme token colour string (#RRGGBB or #RRGGBBAA) into a
    QColor. Deliberately duplicated from rumble/ui/hearing_dialog.py's
    private helper of the same name rather than imported -- that one is a
    module-private helper of a different dialog, not a shared utility."""
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


# S16 (roadmap C4, "transfer calibration"): "the wizard instructs a fixed
# mic stand and does 2 passes per mic, averaging via average_curves()" --
# cited verbatim, not derived.
TRANSFER_PASSES = 2

_TRANSFER_CAL_INTRO_TEXT = (
    "Borrow any microphone you already have a calibration file for. Put "
    "both mics on a FIXED stand, in the EXACT SAME position, aimed at the "
    "same speaker -- moving the stand between passes is the single biggest "
    "source of error here (position repeatability is the error floor, not "
    "the math). You'll measure twice with the reference mic, then swap to "
    "the mic you want to calibrate and measure twice more."
)

_TRANSFER_REFERENCE_INSTRUCTIONS = (
    "Make sure the REFERENCE mic (the one whose calibration you selected) "
    "is in place, in the same position and using the same input device "
    "chosen on the previous page. Measure two passes; averaging them keeps "
    "any small position drift from dominating the result."
)

_TRANSFER_UNKNOWN_INSTRUCTIONS = (
    "Now swap in the mic you want to calibrate, in the EXACT SAME physical "
    "position -- do not move the stand, the speaker, or the output volume. "
    "Measure two passes the same way."
)

_TRANSFER_NO_CAL_TEXT = (
    "No saved calibrations available to use as a reference -- import one "
    "on the mic setup page first."
)

# S17 (roadmap C4, "Spinorama-referenced estimation"): experimental --
# honest about what it can and can't establish (a single reference speaker's
# own unit variance and the user's positioning are folded into the same
# uncertainty as the mic's own response).
_ESTIMATE_CAL_INTRO_TEXT = (
    "Pick a speaker you own that has a published measurement in the "
    "Spinorama database. This is experimental: the result is only as "
    "accurate as that speaker's own published on-axis curve and how "
    "closely you can position the mic -- treat it as a rough estimate, not "
    "a lab calibration."
)

_ESTIMATE_CAL_MEASURE_INSTRUCTIONS = (
    "Place the microphone about 1 m from the speaker, on the tweeter axis "
    "(pointing directly at the tweeter, roughly ear height). Keep the room "
    "otherwise quiet -- the sweep will be time-gated to the direct sound, "
    "before the first room reflection arrives."
)

_ESTIMATE_CAL_NO_ON_AXIS_TEXT = (
    "The selected speaker has no published on-axis curve to compare "
    "against -- try a different measurement."
)

# S15 (roadmap C3 verify paragraph): "the VERIFY pass is tier-independent:
# before-vs-after through the SAME mic cancels the mic's own response in the
# difference, so even standard-mic users get the full closed loop with
# honest numbers" -- surfaced verbatim as UI copy on the verify page.
_VERIFY_TIER_NOTE = (
    "Verification works the same way for every microphone tier: measuring "
    "the SAME microphone before and after correction cancels its own "
    "capsule response in the comparison, so even a standard, uncalibrated "
    "mic gets an honest closed-loop result."
)


class MeasureDialog(FramelessDialog):
    """Room-measurement wizard: page flow and state machine only (S13)."""

    def __init__(
        self,
        manager,
        theme: dict,
        parent: QWidget | None = None,
        tone_engine_factory: Optional[Callable] = None,
        mic_recorder_factory: Optional[Callable] = None,
        list_input_devices_fn: Optional[Callable] = None,
        list_cals_fn: Optional[Callable] = None,
        correction_band_fn: Optional[Callable] = None,
        agc_check_fn: Optional[Callable] = None,
        measure_position_fn: Optional[Callable] = None,
        transfer_capture_fn: Optional[Callable] = None,
        estimate_index_fn: Optional[Callable] = None,
        estimate_fetch_on_axis_fn: Optional[Callable] = None,
        estimate_capture_fn: Optional[Callable] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Measure Room")
        self.setFixedSize(STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT + CHROME_STRIP_HEIGHT)
        self.manager = manager
        self.theme = theme

        # Test seams -- production callers never pass these, so a real run
        # always builds the real S9-S12 objects/functions; tests inject
        # fakes (same pattern HearingSuiteDialog established for its own
        # tone_engine_factory).
        self._tone_engine_factory = tone_engine_factory or ToneEngine
        self._mic_recorder_factory = mic_recorder_factory or MicRecorder
        self._list_input_devices_fn = list_input_devices_fn or list_input_devices
        self._list_cals_fn = list_cals_fn or mic_cal.list_cals
        self._correction_band_fn = correction_band_fn or mic_cal.correction_band
        self._agc_check_fn = agc_check_fn or measurement.agc_check
        self._measure_position_fn = measure_position_fn or self._default_measure_position
        # S16: defaults to the real raw-capture path (no cal subtraction --
        # see _capture_raw_curve's own docstring for why); tests inject a
        # fake fn() -> list[Point].
        self._transfer_capture_fn = transfer_capture_fn or self._capture_raw_curve
        # S17: the mic-estimation sub-flow's three seams. estimate_index_fn
        # and estimate_fetch_on_axis_fn are network calls (Spinorama), so
        # tests fake both to stay fully offline; estimate_capture_fn defaults
        # to the real IR-only capture path (_capture_raw_ir, below).
        self._estimate_provider = SpinoramaProvider(self.manager.data_dir)
        self._estimate_index_fn = estimate_index_fn or (
            lambda: self._estimate_provider.load_index(force_refresh=False))
        self._estimate_fetch_on_axis_fn = estimate_fetch_on_axis_fn or self._fetch_estimate_on_axis
        self._estimate_capture_fn = estimate_capture_fn or self._capture_raw_ir

        self._engine = None
        self._recorder = None
        self._bypass_guard: BypassGuard | None = None

        self._input_devices: list[InputDevice] = []
        self._cal_entries: list[mic_cal.MicCalEntry] = []
        self._selected_cal: mic_cal.MicCalEntry | None = None
        self._agc_result: measurement.AgcCheckResult | None = None
        self._n_positions = measurement.MIN_POSITIONS
        self._position_curves: list[list | None] = [None] * self._n_positions
        self._baseline_curve: list = []
        self._target_choice = {"curve_name": _TILT_TARGET_KEY, "tilt_db_per_oct": 0.0}
        self._correction_curve: list = []
        self._correction_band: tuple[float, float] = (20.0, 20000.0)
        self._clamp_summary = eq_target.ClampSummary()
        self._extra_filters: list = []
        self._saved_profile_id: str | None = None

        # S15 verify-pass state.
        self._verify_position_curves: list[list | None] = [None] * self._n_positions
        self._verify_curve: list = []
        self._verify_previewing = False
        self._verify_prior_bypass: bool | None = None
        self._verify_baseline_rms = 0.0
        self._verify_predicted_rms = 0.0
        self._verify_achieved_rms = 0.0

        # S16 transfer-calibration sub-flow state -- entered from the setup
        # page, always returns to it.
        self._transfer_reference_cal: mic_cal.MicCalEntry | None = None
        self._transfer_stage: str = "reference"  # "reference" | "unknown"
        self._transfer_pass_curves: dict[str, list] = {
            "reference": [None] * TRANSFER_PASSES,
            "unknown": [None] * TRANSFER_PASSES,
        }
        self._transfer_reference_curve: list = []
        self._transfer_unknown_curve: list = []
        self._transfer_result_curve: list = []
        self._transfer_saved_cal_id: str | None = None

        # S17 mic-estimation sub-flow state -- entered from the setup page,
        # always returns to it.
        self._estimate_entry = None  # a SpinoramaEntry
        self._estimate_gate_ms: float = 0.0
        self._estimate_curve: list = []
        self._estimate_band: tuple[float, float] = mic_cal.FULL_RANGE_HZ
        self._estimate_saved_cal_id: str | None = None

        root = QVBoxLayout(self.body)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # QStackedWidget's minimumSizeHint is the maximum over EVERY page it
        # holds (not just the current one) -- the verify page's RMS-
        # deviation chart alone needs far more than the dialog's standard
        # fixed height. Each page wraps itself in its own scrollable_page
        # (same helper SettingsDialog uses) so only a genuinely tall page
        # scrolls; short pages keep their own natural height instead of
        # inheriting the tallest page's.
        self.intro_page = scrollable_page(self._build_intro_page())
        self.setup_page = scrollable_page(self._build_setup_page())
        self.level_page = scrollable_page(self._build_level_page())
        self.baseline_page = scrollable_page(self._build_baseline_page())
        self.target_page = scrollable_page(self._build_target_page())
        self.results_page = scrollable_page(self._build_results_page())
        self.verify_page = scrollable_page(self._build_verify_page())
        self.done_page = scrollable_page(self._build_done_page())
        self.transfer_cal_intro_page = scrollable_page(self._build_transfer_cal_intro_page())
        self.transfer_cal_measure_page = scrollable_page(self._build_transfer_cal_measure_page())
        self.transfer_cal_done_page = scrollable_page(self._build_transfer_cal_done_page())
        self.estimate_cal_pick_page = scrollable_page(self._build_estimate_cal_pick_page())
        self.estimate_cal_measure_page = scrollable_page(self._build_estimate_cal_measure_page())
        self.estimate_cal_done_page = scrollable_page(self._build_estimate_cal_done_page())

        for page in (
            self.intro_page, self.setup_page, self.level_page, self.baseline_page,
            self.target_page, self.results_page, self.verify_page, self.done_page,
            self.transfer_cal_intro_page, self.transfer_cal_measure_page,
            self.transfer_cal_done_page, self.estimate_cal_pick_page,
            self.estimate_cal_measure_page, self.estimate_cal_done_page,
        ):
            self.stack.addWidget(page)

        self._apply_verify_chart_theme()
        self.stack.setCurrentWidget(self.intro_page)

    # ── shared helpers ───────────────────────────────────────────────

    def _current_index(self) -> int:
        return self.stack.currentIndex()

    def _go_to(self, page: QWidget) -> None:
        self.stack.setCurrentWidget(page)

    def _go_back(self) -> None:
        idx = self._current_index()
        if idx > 0:
            self.stack.setCurrentIndex(idx - 1)

    def _engage_bypass_and_engine(self) -> None:
        """Starts the wizard's real audio work: engages bypass and builds
        the engine/recorder, exactly once (idempotent -- called from the
        intro page's Next, the only entry point into the functional
        pages)."""
        if self._engine is None:
            self._engine = self._tone_engine_factory()
        if self._recorder is None:
            self._recorder = self._mic_recorder_factory()
        if self._bypass_guard is None:
            self._bypass_guard = BypassGuard(self.manager)
            self._bypass_guard.__enter__()

    def _selected_input_device_index(self) -> int | None:
        if not hasattr(self, "cmb_input_device"):
            return None
        data = self.cmb_input_device.currentData()
        return data.index if isinstance(data, InputDevice) else None

    # ── intro/safety page ────────────────────────────────────────────

    def _build_intro_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("MEASURE ROOM"))

        intro = QLabel(_SAFETY_TEXT)
        intro.setWordWrap(True)
        intro.setProperty("class", "muted")
        lay.addWidget(intro)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_intro_cancel = QPushButton("Cancel")
        self.btn_intro_cancel.setProperty("class", "ghost")
        self.btn_intro_cancel.clicked.connect(self.reject)
        describe(self.btn_intro_cancel, "Cancel", "Close the room-measurement wizard.")
        btn_row.addWidget(self.btn_intro_cancel)
        btn_row.addStretch(1)

        self.btn_intro_next = QPushButton("Next")
        self.btn_intro_next.setProperty("class", "primary")
        self.btn_intro_next.clicked.connect(self._on_intro_next)
        describe(self.btn_intro_next, "Next",
                 "Continue to microphone and output setup. Engages the EQ "
                 "bypass for the rest of the wizard.")
        btn_row.addWidget(self.btn_intro_next)
        lay.addLayout(btn_row)

        return page

    def _on_intro_next(self) -> None:
        self._engage_bypass_and_engine()
        self._populate_setup_page()
        self._go_to(self.setup_page)

    # ── mic & output setup page ──────────────────────────────────────

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("MIC & OUTPUT SETUP"))

        note = QLabel(_SPEAKERS_ONLY_NOTE)
        note.setWordWrap(True)
        note.setProperty("class", "muted")
        lay.addWidget(note)

        frame = HudFrame(class_name="panel")
        frame.set_theme(self.theme)
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(dp(14, 10), dp(14, 10), dp(14, 10), dp(14, 10))
        frame_lay.setSpacing(10)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("Input device"))
        self.cmb_input_device = QComboBox()
        self.cmb_input_device.currentIndexChanged.connect(self._on_input_device_changed)
        describe(self.cmb_input_device, "Input device",
                 "The microphone RUMBLE records from during measurement.")
        input_row.addWidget(self.cmb_input_device, 1)
        frame_lay.addLayout(input_row)

        cal_row = QHBoxLayout()
        cal_row.addWidget(QLabel("Mic calibration"))
        self.cmb_cal_file = QComboBox()
        self.cmb_cal_file.currentIndexChanged.connect(self._on_cal_changed)
        describe(self.cmb_cal_file, "Mic calibration",
                 "A saved calibration file for this microphone, if you have "
                 "one. Without one, correction is limited to the low-"
                 "frequency modal region your room and any smooth-response "
                 "microphone agree on.")
        cal_row.addWidget(self.cmb_cal_file, 1)
        frame_lay.addLayout(cal_row)

        self.lbl_tier = QLabel("")
        self.lbl_tier.setWordWrap(True)
        self.lbl_tier.setProperty("class", "muted")
        frame_lay.addWidget(self.lbl_tier)

        self.btn_calibrate_transfer = QPushButton("Calibrate this mic against a reference...")
        self.btn_calibrate_transfer.setProperty("class", "ghost")
        self.btn_calibrate_transfer.clicked.connect(self._on_open_transfer_cal)
        describe(self.btn_calibrate_transfer, "Calibrate this mic against a reference",
                 "Derive a calibration file for the selected input device by "
                 "comparing it, in the same physical position, against a mic "
                 "you already have a calibration for.")
        frame_lay.addWidget(self.btn_calibrate_transfer)

        self.btn_estimate_cal = QPushButton("Estimate calibration from a speaker I own...")
        self.btn_estimate_cal.setProperty("class", "ghost")
        self.btn_estimate_cal.clicked.connect(self._on_open_estimate_cal)
        describe(self.btn_estimate_cal, "Estimate calibration from a speaker I own",
                 "Derive an experimental calibration for the selected input device by "
                 "comparing a gated measurement of a speaker you own against its "
                 "published anechoic on-axis curve.")
        frame_lay.addWidget(self.btn_estimate_cal)

        lay.addWidget(frame)
        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_setup_back = QPushButton("Back")
        self.btn_setup_back.setProperty("class", "ghost")
        self.btn_setup_back.clicked.connect(self._go_back)
        describe(self.btn_setup_back, "Back", "Return to the previous page.")
        btn_row.addWidget(self.btn_setup_back)
        btn_row.addStretch(1)

        self.btn_setup_next = QPushButton("Next")
        self.btn_setup_next.setProperty("class", "primary")
        self.btn_setup_next.clicked.connect(self._on_setup_next)
        describe(self.btn_setup_next, "Next", "Continue to the level and mic sanity check.")
        btn_row.addWidget(self.btn_setup_next)
        lay.addLayout(btn_row)

        return page

    def _populate_setup_page(self) -> None:
        self._input_devices = list(self._list_input_devices_fn())
        self.cmb_input_device.blockSignals(True)
        self.cmb_input_device.clear()
        default_idx = 0
        for i, dev in enumerate(self._input_devices):
            label = f"{dev.name} (default)" if dev.is_default else dev.name
            self.cmb_input_device.addItem(label, dev)
            if dev.is_default:
                default_idx = i
        if self._input_devices:
            self.cmb_input_device.setCurrentIndex(default_idx)
        self.cmb_input_device.blockSignals(False)

        self._cal_entries = list(self._list_cals_fn(self.manager.data_dir))
        self.cmb_cal_file.blockSignals(True)
        self.cmb_cal_file.clear()
        self.cmb_cal_file.addItem("(none -- standard/unknown mic)", None)
        for entry in self._cal_entries:
            self.cmb_cal_file.addItem(f"{entry.name} ({entry.source})", entry)
        self.cmb_cal_file.setCurrentIndex(0)
        self.cmb_cal_file.blockSignals(False)
        self._selected_cal = None
        self._update_tier_label()

    def _on_input_device_changed(self, index: int) -> None:
        pass  # selection itself is read lazily via _selected_input_device_index

    def _on_cal_changed(self, index: int) -> None:
        self._selected_cal = self.cmb_cal_file.itemData(index)
        self._update_tier_label()

    def _tier_label_text(self, cal) -> str:
        """Shared tier/confidence wording -- used by the setup page's own
        tier label AND (S15) the verify page's tier/confidence caveat, so
        the same honest-scope sentence appears wherever a mic tier is named."""
        f_lo, f_hi = self._correction_band_fn(cal)
        if cal is None:
            return (
                f"Standard mic -- auto-correction limited to {f_lo:.0f}-{f_hi:.0f} Hz "
                "(below this your room's own modal behavior dominates; above it, an "
                "uncalibrated mic's own coloring would get baked into the correction).")
        elif cal.source == "estimated":
            return f"Estimated calibration -- correction extended to {f_lo:.0f}-{f_hi:.0f} Hz."
        else:
            return (
                f"Calibrated mic ({cal.source}) -- full-range correction, "
                f"{f_lo:.0f}-{f_hi:.0f} Hz.")

    def _update_tier_label(self) -> None:
        self.lbl_tier.setText(self._tier_label_text(self._selected_cal))

    def _on_setup_next(self) -> None:
        self._go_to(self.level_page)

    # ── transfer-calibration sub-flow (S16) ──────────────────────────
    # Entered from the setup page's "Calibrate this mic against a
    # reference..." button, always returns to the setup page. Three pages:
    # intro (pick the reference calibration), measure (stage-based: 2 passes
    # with the reference mic, then 2 with the unknown mic, mirroring the
    # baseline page's own "Measure Next Position" pattern), done (save
    # status). Uses _synchronized_record via _capture_raw_curve -- the same
    # ESS sweep machinery the baseline page uses, per roadmap C4 ("pure
    # reuse of C2's sweep machinery + one curve subtraction").

    def _on_open_transfer_cal(self) -> None:
        self._populate_transfer_cal_intro_page()
        self._go_to(self.transfer_cal_intro_page)

    def _build_transfer_cal_intro_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("TRANSFER CALIBRATION"))

        note = QLabel(_TRANSFER_CAL_INTRO_TEXT)
        note.setWordWrap(True)
        note.setProperty("class", "muted")
        lay.addWidget(note)

        ref_row = QHBoxLayout()
        ref_row.addWidget(QLabel("Reference mic calibration"))
        self.cmb_transfer_reference_cal = QComboBox()
        describe(self.cmb_transfer_reference_cal, "Reference mic calibration",
                 "The saved calibration for the KNOWN, already-calibrated "
                 "mic you're borrowing. Required -- transfer calibration "
                 "only works against a genuine reference.")
        ref_row.addWidget(self.cmb_transfer_reference_cal, 1)
        lay.addLayout(ref_row)

        self.lbl_transfer_intro_status = QLabel("")
        self.lbl_transfer_intro_status.setWordWrap(True)
        self.lbl_transfer_intro_status.setProperty("class", "muted")
        lay.addWidget(self.lbl_transfer_intro_status)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_transfer_intro_back = QPushButton("Back")
        self.btn_transfer_intro_back.setProperty("class", "ghost")
        self.btn_transfer_intro_back.clicked.connect(lambda: self._go_to(self.setup_page))
        describe(self.btn_transfer_intro_back, "Back",
                 "Return to mic and output setup without calibrating.")
        btn_row.addWidget(self.btn_transfer_intro_back)
        btn_row.addStretch(1)

        self.btn_transfer_intro_next = QPushButton("Next")
        self.btn_transfer_intro_next.setProperty("class", "primary")
        self.btn_transfer_intro_next.clicked.connect(self._on_transfer_intro_next)
        describe(self.btn_transfer_intro_next, "Next",
                 "Begin measuring with the reference mic in place.")
        btn_row.addWidget(self.btn_transfer_intro_next)
        lay.addLayout(btn_row)

        return page

    def _populate_transfer_cal_intro_page(self) -> None:
        self.cmb_transfer_reference_cal.blockSignals(True)
        self.cmb_transfer_reference_cal.clear()
        for entry in self._cal_entries:
            self.cmb_transfer_reference_cal.addItem(f"{entry.name} ({entry.source})", entry)
        self.cmb_transfer_reference_cal.blockSignals(False)
        has_cals = bool(self._cal_entries)
        if has_cals:
            self.cmb_transfer_reference_cal.setCurrentIndex(0)
        self.lbl_transfer_intro_status.setText("" if has_cals else _TRANSFER_NO_CAL_TEXT)
        self.btn_transfer_intro_next.setEnabled(has_cals)

    def _on_transfer_intro_next(self) -> None:
        ref = self.cmb_transfer_reference_cal.currentData()
        if ref is None:
            return
        self._transfer_reference_cal = ref
        self._transfer_stage = "reference"
        self._transfer_pass_curves = {
            "reference": [None] * TRANSFER_PASSES,
            "unknown": [None] * TRANSFER_PASSES,
        }
        self._transfer_reference_curve = []
        self._transfer_unknown_curve = []
        self._populate_transfer_cal_measure_page()
        self._go_to(self.transfer_cal_measure_page)

    def _build_transfer_cal_measure_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        self.lbl_transfer_measure_heading = _page_heading("")
        lay.addWidget(self.lbl_transfer_measure_heading)

        self.lbl_transfer_measure_instructions = QLabel("")
        self.lbl_transfer_measure_instructions.setWordWrap(True)
        self.lbl_transfer_measure_instructions.setProperty("class", "muted")
        lay.addWidget(self.lbl_transfer_measure_instructions)

        self.list_transfer_passes = QListWidget()
        describe(self.list_transfer_passes, "Pass progress",
                 "Shows how many measurement passes have been captured for "
                 "the current microphone.")
        lay.addWidget(self.list_transfer_passes, 1)

        self.btn_transfer_measure_pass = QPushButton("Measure Next Pass")
        self.btn_transfer_measure_pass.setProperty("class", "ghost")
        self.btn_transfer_measure_pass.clicked.connect(self._on_transfer_measure_pass)
        describe(self.btn_transfer_measure_pass, "Measure next pass",
                 "Runs one sweep capture for the current microphone.")
        lay.addWidget(self.btn_transfer_measure_pass)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_transfer_measure_back = QPushButton("Back")
        self.btn_transfer_measure_back.setProperty("class", "ghost")
        self.btn_transfer_measure_back.clicked.connect(self._on_transfer_measure_back)
        describe(self.btn_transfer_measure_back, "Back",
                 "Return to reference-calibration choice. Discards passes "
                 "measured so far.")
        btn_row.addWidget(self.btn_transfer_measure_back)
        btn_row.addStretch(1)

        self.btn_transfer_measure_next = QPushButton("Next")
        self.btn_transfer_measure_next.setProperty("class", "primary")
        self.btn_transfer_measure_next.setEnabled(False)
        self.btn_transfer_measure_next.clicked.connect(self._on_transfer_measure_next)
        describe(self.btn_transfer_measure_next, "Next",
                 "Continue. Stays disabled until both passes for the "
                 "current microphone are measured.")
        btn_row.addWidget(self.btn_transfer_measure_next)
        lay.addLayout(btn_row)

        return page

    def _populate_transfer_cal_measure_page(self) -> None:
        stage = self._transfer_stage
        heading = ("MEASURE WITH REFERENCE MIC" if stage == "reference"
                   else "MEASURE WITH UNKNOWN MIC")
        self.lbl_transfer_measure_heading.setText(heading)
        self.lbl_transfer_measure_instructions.setText(
            _TRANSFER_REFERENCE_INSTRUCTIONS if stage == "reference"
            else _TRANSFER_UNKNOWN_INSTRUCTIONS)
        self._refresh_transfer_pass_list()

    def _refresh_transfer_pass_list(self) -> None:
        curves = self._transfer_pass_curves[self._transfer_stage]
        self.list_transfer_passes.clear()
        for i, curve in enumerate(curves):
            status = "measured" if curve is not None else "not measured"
            self.list_transfer_passes.addItem(f"Pass {i + 1}: {status}")
        all_measured = all(c is not None for c in curves)
        self.btn_transfer_measure_next.setEnabled(all_measured)
        self.btn_transfer_measure_pass.setEnabled(not all_measured)

    def _on_transfer_measure_pass(self) -> None:
        curves = self._transfer_pass_curves[self._transfer_stage]
        try:
            index = next(i for i, c in enumerate(curves) if c is None)
        except StopIteration:
            return
        curve = self._transfer_capture_fn()
        curves[index] = curve if curve is not None else []
        self._refresh_transfer_pass_list()

    def _on_transfer_measure_back(self) -> None:
        self._go_to(self.transfer_cal_intro_page)

    def _on_transfer_measure_next(self) -> None:
        curves = [c for c in self._transfer_pass_curves[self._transfer_stage] if c]
        averaged = autoeq_merge.average_curves(curves) if curves else []
        if self._transfer_stage == "reference":
            self._transfer_reference_curve = averaged
            self._transfer_stage = "unknown"
            self._populate_transfer_cal_measure_page()
            return
        self._transfer_unknown_curve = averaged
        self._finish_transfer_cal()

    def _finish_transfer_cal(self) -> None:
        """Derive + save the new calibration (S16): mic_cal.derive_transfer_cal
        against the reference cal's own loaded curve, saved as a normal
        source="transfer" cal file. Always lands on the done page; a missing
        reference cal file (should not happen -- the intro page only lists
        real saved entries) is reported rather than raising."""
        ref_entry = self._transfer_reference_cal
        loaded = (mic_cal.load_cal(self.manager.data_dir, ref_entry.id)
                  if ref_entry is not None else None)
        if loaded is None:
            self._transfer_saved_cal_id = None
            self._populate_transfer_cal_done_page(saved=False)
            self._go_to(self.transfer_cal_done_page)
            return
        _, reference_cal_curve = loaded
        result = mic_cal.derive_transfer_cal(
            self._transfer_reference_curve, reference_cal_curve, self._transfer_unknown_curve)
        self._transfer_result_curve = result
        name = f"Transfer cal ({ref_entry.name})"
        self._transfer_saved_cal_id = mic_cal.save_cal(
            self.manager.data_dir, name, result, source="transfer")
        self._populate_transfer_cal_done_page(saved=True)
        self._go_to(self.transfer_cal_done_page)

    def _build_transfer_cal_done_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("TRANSFER CALIBRATION SAVED"))

        self.lbl_transfer_done_status = QLabel("")
        self.lbl_transfer_done_status.setWordWrap(True)
        self.lbl_transfer_done_status.setProperty("class", "muted")
        lay.addWidget(self.lbl_transfer_done_status)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_transfer_done_close = QPushButton("Back to Setup")
        self.btn_transfer_done_close.setProperty("class", "primary")
        self.btn_transfer_done_close.clicked.connect(self._on_transfer_done_close)
        describe(self.btn_transfer_done_close, "Back to setup",
                 "Return to mic and output setup with the new calibration selected.")
        btn_row.addWidget(self.btn_transfer_done_close)
        lay.addLayout(btn_row)

        return page

    def _populate_transfer_cal_done_page(self, saved: bool) -> None:
        if saved:
            self.lbl_transfer_done_status.setText(
                "Saved as a new calibration and selected for this session.")
        else:
            self.lbl_transfer_done_status.setText(
                "Could not load the reference calibration -- nothing was saved.")

    def _select_cal_by_id(self, cal_id: str) -> None:
        for i in range(self.cmb_cal_file.count()):
            data = self.cmb_cal_file.itemData(i)
            if isinstance(data, mic_cal.MicCalEntry) and data.id == cal_id:
                self.cmb_cal_file.setCurrentIndex(i)
                return

    def _on_transfer_done_close(self) -> None:
        if self._transfer_saved_cal_id:
            self._populate_setup_page()
            self._select_cal_by_id(self._transfer_saved_cal_id)
        self._go_to(self.setup_page)

    # ── mic-estimation sub-flow (S17) ────────────────────────────────
    # Entered from the setup page's "Estimate calibration from a speaker I
    # own..." button, always returns to the setup page. Three pages: pick
    # (search the Spinorama database via the same reusable _MeasurementPanel
    # the unified measurement browser uses -- a bare SpinoramaProvider
    # catalogue naturally limits it to speakers only, per roadmap C4),
    # measure (one gated ESS measurement, gate chosen from the IR's own
    # first-reflection arrival, then derive + review before saving), done
    # (save status).

    def _run_sync(self, fn, on_done, on_fail) -> None:
        """A synchronous stand-in for _MeasurementPanel's run_async seam.
        The panel only ever invokes it from merge_selected(), which is
        never reachable here (checkable=False builds no merge button) --
        this exists so the panel's constructor contract is honestly
        fulfilled rather than passed a silent no-op, matching the wizard's
        own established style of blocking the UI thread during measurement
        work (_on_measure_position, _on_run_agc_check, etc. all call their
        seams directly rather than threading them)."""
        try:
            result = fn()
        except Exception as exc:
            on_fail(str(exc))
            return
        on_done(result)

    def _on_open_estimate_cal(self) -> None:
        self._populate_estimate_cal_pick_page()
        self._go_to(self.estimate_cal_pick_page)

    def _build_estimate_cal_pick_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("ESTIMATE CALIBRATION"))

        note = QLabel(_ESTIMATE_CAL_INTRO_TEXT)
        note.setWordWrap(True)
        note.setProperty("class", "muted")
        lay.addWidget(note)

        self.lbl_estimate_pick_status = QLabel("")
        self.lbl_estimate_pick_status.setWordWrap(True)
        self.lbl_estimate_pick_status.setProperty("class", "muted")
        lay.addWidget(self.lbl_estimate_pick_status)

        self.estimate_panel = _MeasurementPanel(
            self._estimate_provider, self._run_sync, checkable=False, parent=page)
        self.estimate_panel.selection_changed.connect(self._update_estimate_pick_next_enabled)
        lay.addWidget(self.estimate_panel, 1)

        btn_row = QHBoxLayout()
        self.btn_estimate_pick_back = QPushButton("Back")
        self.btn_estimate_pick_back.setProperty("class", "ghost")
        self.btn_estimate_pick_back.clicked.connect(lambda: self._go_to(self.setup_page))
        describe(self.btn_estimate_pick_back, "Back",
                 "Return to mic and output setup without estimating a calibration.")
        btn_row.addWidget(self.btn_estimate_pick_back)
        btn_row.addStretch(1)

        self.btn_estimate_pick_next = QPushButton("Next")
        self.btn_estimate_pick_next.setProperty("class", "primary")
        self.btn_estimate_pick_next.setEnabled(False)
        self.btn_estimate_pick_next.clicked.connect(self._on_estimate_pick_next)
        describe(self.btn_estimate_pick_next, "Next",
                 "Continue with the highlighted speaker. Stays disabled until one "
                 "is selected.")
        btn_row.addWidget(self.btn_estimate_pick_next)
        lay.addLayout(btn_row)

        return page

    def _populate_estimate_cal_pick_page(self) -> None:
        self.lbl_estimate_pick_status.setText("Loading speaker database...")
        try:
            entries = list(self._estimate_index_fn())
        except Exception as exc:
            self.lbl_estimate_pick_status.setText(
                f"Could not load the speaker database: {exc}")
            entries = []
        else:
            self.lbl_estimate_pick_status.setText("")
        self.estimate_panel.set_entries(entries)
        self._update_estimate_pick_next_enabled()

    def _update_estimate_pick_next_enabled(self) -> None:
        self.btn_estimate_pick_next.setEnabled(self.estimate_panel.current_entry() is not None)

    def _on_estimate_pick_next(self) -> None:
        entry = self.estimate_panel.current_entry()
        if entry is None:
            return
        self._estimate_entry = entry
        self._estimate_gate_ms = 0.0
        self._estimate_curve = []
        self._estimate_band = mic_cal.FULL_RANGE_HZ
        self._estimate_saved_cal_id = None
        self._populate_estimate_cal_measure_page()
        self._go_to(self.estimate_cal_measure_page)

    def _build_estimate_cal_measure_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        self.lbl_estimate_measure_heading = _page_heading("")
        lay.addWidget(self.lbl_estimate_measure_heading)

        instructions = QLabel(_ESTIMATE_CAL_MEASURE_INSTRUCTIONS)
        instructions.setWordWrap(True)
        instructions.setProperty("class", "muted")
        lay.addWidget(instructions)

        self.btn_estimate_measure = QPushButton("Measure")
        self.btn_estimate_measure.setProperty("class", "ghost")
        self.btn_estimate_measure.clicked.connect(self._on_estimate_measure)
        describe(self.btn_estimate_measure, "Measure",
                 "Runs one gated sweep capture and compares it against the "
                 "selected speaker's published on-axis curve.")
        lay.addWidget(self.btn_estimate_measure)

        self.lbl_estimate_result = QLabel("")
        self.lbl_estimate_result.setWordWrap(True)
        lay.addWidget(self.lbl_estimate_result)

        self.btn_estimate_save = QPushButton("Save Calibration")
        self.btn_estimate_save.setProperty("class", "ghost")
        self.btn_estimate_save.setEnabled(False)
        self.btn_estimate_save.clicked.connect(self._on_estimate_save)
        describe(self.btn_estimate_save, "Save calibration",
                 "Saves the estimated response as a new calibration and selects "
                 "it for this session.")
        lay.addWidget(self.btn_estimate_save)

        self.lbl_estimate_save_status = QLabel("")
        self.lbl_estimate_save_status.setWordWrap(True)
        lay.addWidget(self.lbl_estimate_save_status)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_estimate_measure_back = QPushButton("Back")
        self.btn_estimate_measure_back.setProperty("class", "ghost")
        self.btn_estimate_measure_back.clicked.connect(
            lambda: self._go_to(self.estimate_cal_pick_page))
        describe(self.btn_estimate_measure_back, "Back",
                 "Return to speaker choice. Discards the current measurement.")
        btn_row.addWidget(self.btn_estimate_measure_back)
        btn_row.addStretch(1)

        self.btn_estimate_measure_next = QPushButton("Next")
        self.btn_estimate_measure_next.setProperty("class", "primary")
        self.btn_estimate_measure_next.setEnabled(False)
        self.btn_estimate_measure_next.clicked.connect(self._on_estimate_measure_next)
        describe(self.btn_estimate_measure_next, "Next",
                 "Continue. Stays disabled until the estimated calibration is saved.")
        btn_row.addWidget(self.btn_estimate_measure_next)
        lay.addLayout(btn_row)

        return page

    def _populate_estimate_cal_measure_page(self) -> None:
        self.lbl_estimate_measure_heading.setText(f"MEASURE -- {self._estimate_entry.name}")
        self.lbl_estimate_result.setText("")
        self.lbl_estimate_save_status.setText("")
        self.btn_estimate_save.setEnabled(False)
        self.btn_estimate_measure_next.setEnabled(False)

    def _fetch_estimate_on_axis(self, entry) -> list:
        """Real on-axis fetch (S17): the raw published curve, NOT run through
        SpinoramaProvider.fetch_curve() (that wrapper turns a speaker's
        curve into a finished EQ correction via eq_target.make_correction --
        this sub-flow needs the raw curve itself as ground truth)."""
        return spinorama.fetch_curve(entry, curve="On Axis", data_dir=self.manager.data_dir)

    def _on_estimate_measure(self) -> None:
        ir, sample_rate = self._estimate_capture_fn()
        gate_ms = measurement.detect_first_reflection_gate_ms(ir, sample_rate)
        gated_ir = measurement.time_gate(ir, gate_ms, sample_rate)
        measured_curve = measurement.ir_to_magnitude(gated_ir, sample_rate)

        try:
            on_axis = self._estimate_fetch_on_axis_fn(self._estimate_entry)
        except Exception as exc:
            self.lbl_estimate_result.setText(
                f"Could not fetch the speaker's published curve: {exc}")
            return
        if not on_axis:
            self.lbl_estimate_result.setText(_ESTIMATE_CAL_NO_ON_AXIS_TEXT)
            return

        curve = mic_cal.derive_estimated_cal(measured_curve, on_axis)
        band = mic_cal.estimated_band(gate_ms)
        self._estimate_gate_ms = gate_ms
        self._estimate_curve = curve
        self._estimate_band = band
        self.lbl_estimate_result.setText(
            f"Estimated calibration, +/-2-3 dB, {band[0]:.0f}-{band[1]:.0f} Hz "
            f"(gate {gate_ms:.1f} ms, from the measured first reflection).")
        self.btn_estimate_save.setEnabled(True)

    def _on_estimate_save(self) -> None:
        if not self._estimate_curve or self._estimate_entry is None:
            return
        name = f"Estimated cal ({self._estimate_entry.name})"
        self._estimate_saved_cal_id = mic_cal.save_cal(
            self.manager.data_dir, name, self._estimate_curve,
            source="estimated", band=self._estimate_band)
        self.lbl_estimate_save_status.setText(
            "Saved as a new calibration and selected for this session.")
        self.btn_estimate_measure_next.setEnabled(True)

    def _on_estimate_measure_next(self) -> None:
        self._populate_estimate_cal_done_page()
        self._go_to(self.estimate_cal_done_page)

    def _build_estimate_cal_done_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("ESTIMATED CALIBRATION SAVED"))

        self.lbl_estimate_done_status = QLabel("")
        self.lbl_estimate_done_status.setWordWrap(True)
        self.lbl_estimate_done_status.setProperty("class", "muted")
        lay.addWidget(self.lbl_estimate_done_status)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_estimate_done_close = QPushButton("Back to Setup")
        self.btn_estimate_done_close.setProperty("class", "primary")
        self.btn_estimate_done_close.clicked.connect(self._on_estimate_done_close)
        describe(self.btn_estimate_done_close, "Back to setup",
                 "Return to mic and output setup with the new calibration selected.")
        btn_row.addWidget(self.btn_estimate_done_close)
        lay.addLayout(btn_row)

        return page

    def _populate_estimate_cal_done_page(self) -> None:
        if self._estimate_saved_cal_id:
            self.lbl_estimate_done_status.setText(
                "Saved as a new calibration and selected for this session.")
        else:
            self.lbl_estimate_done_status.setText("No calibration was saved.")

    def _on_estimate_done_close(self) -> None:
        if self._estimate_saved_cal_id:
            self._populate_setup_page()
            self._select_cal_by_id(self._estimate_saved_cal_id)
        self._go_to(self.setup_page)

    # ── level + AGC sanity check page ────────────────────────────────

    def _build_level_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("LEVEL & MIC SANITY CHECK"))

        explanation = QLabel(_AGC_EXPLANATION)
        explanation.setWordWrap(True)
        explanation.setProperty("class", "muted")
        lay.addWidget(explanation)

        self.btn_run_agc_check = QPushButton("Run Sanity Check")
        self.btn_run_agc_check.setProperty("class", "ghost")
        self.btn_run_agc_check.clicked.connect(self._on_run_agc_check)
        describe(self.btn_run_agc_check, "Run sanity check",
                 "Play a short sweep twice, 6 dB apart, and compare the "
                 "captured level difference.")
        lay.addWidget(self.btn_run_agc_check)

        self.lbl_agc_result = QLabel("")
        self.lbl_agc_result.setWordWrap(True)
        lay.addWidget(self.lbl_agc_result)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_level_back = QPushButton("Back")
        self.btn_level_back.setProperty("class", "ghost")
        self.btn_level_back.clicked.connect(self._go_back)
        describe(self.btn_level_back, "Back", "Return to the previous page.")
        btn_row.addWidget(self.btn_level_back)
        btn_row.addStretch(1)

        self.btn_level_next = QPushButton("Next")
        self.btn_level_next.setProperty("class", "primary")
        self.btn_level_next.setEnabled(False)
        self.btn_level_next.clicked.connect(self._on_level_next)
        describe(self.btn_level_next, "Next",
                 "Continue to the baseline measurement. Stays disabled "
                 "until the sanity check passes.")
        btn_row.addWidget(self.btn_level_next)
        lay.addLayout(btn_row)

        return page

    def _synchronized_record(self, signal, sample_rate: int):
        """Shared, properly synchronized play+record helper (S14 Amendment).

        Mirrors rumble/core/time_alignment.py's run_measurement() pattern
        exactly: start the recording on a background thread, sleep
        lead_in_s, play, wait for playback to finish, then join() the
        recording thread with a timeout. This is the ONE real record_fn
        implementation the wizard uses everywhere it needs play+record --
        both the AGC sanity check and baseline position capture -- replacing
        S13's crude, unsynchronized _default_record_fn entirely.

        MicRecorder.record() is itself already asynchronous (returns
        immediately, fires on_done later off the caller's thread), unlike
        time_alignment's own blocking sd.rec()/sd.wait() default record
        path -- so the background thread here blocks on a threading.Event
        set from on_done, rather than blocking directly inside record()
        itself. This is a necessary adaptation of the mirrored pattern to
        MicRecorder's actual (async) API, not a divergent scheme: the
        caller-visible shape (background thread + lead-in + join-with-
        timeout) is identical.
        """
        duration_s = len(signal) / float(sample_rate)
        record_seconds = _RECORD_LEAD_IN_S + duration_s + _RECORD_TRAIL_S
        device = self._selected_input_device_index()

        done_event = threading.Event()
        error: dict[str, BaseException] = {}

        def _on_done() -> None:
            done_event.set()

        def _do_record() -> None:
            try:
                self._recorder.record(record_seconds, sample_rate,
                                      device=device, on_done=_on_done)
            except Exception as exc:
                error["exc"] = exc
                done_event.set()
                return
            done_event.wait(timeout=record_seconds + 5.0)

        rec_thread = threading.Thread(target=_do_record, daemon=True)
        rec_thread.start()
        time.sleep(_RECORD_LEAD_IN_S)
        self._engine.play(signal, sample_rate)
        while self._engine.is_playing:
            time.sleep(0.01)
        rec_thread.join(timeout=record_seconds + 5.0)

        if "exc" in error:
            raise error["exc"]
        recorded = self._recorder.buffer
        # Trim the lead-in margin so recorded[0] lines up with the instant
        # playback actually began -- deconvolve()'s own documented
        # assumption (rumble/core/measurement.py), the same alignment
        # responsibility time_alignment.py's lead_in_s establishes for its
        # own (GCC-PHAT-based) measurement, which finds the offset itself
        # rather than needing the caller to trim it.
        lead_in_samples = int(round(_RECORD_LEAD_IN_S * sample_rate))
        if recorded is not None and len(recorded) > lead_in_samples:
            return recorded[lead_in_samples:]
        return recorded

    def _on_run_agc_check(self) -> None:
        result = self._agc_check_fn(
            record_fn=self._synchronized_record,
            sample_rate=self.manager.effective_sample_rate())
        self._agc_result = result
        if result.processing_detected:
            self.lbl_agc_result.setText(
                f"Measured delta: {result.delta_db:.1f} dB (expected ~6 dB). "
                + _AGC_FAIL_INSTRUCTION)
        else:
            self.lbl_agc_result.setText(
                f"Measured delta: {result.delta_db:.1f} dB (expected ~6 dB) -- looks clean.")
        self.btn_level_next.setEnabled(not result.processing_detected)

    def _on_level_next(self) -> None:
        if self._agc_result is None or self._agc_result.processing_detected:
            return
        self._populate_baseline_page()
        self._go_to(self.baseline_page)

    # ── baseline measurement page ────────────────────────────────────

    def _build_baseline_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("BASELINE MEASUREMENT"))

        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Mic positions"))
        self.spin_n_positions = QSpinBox()
        self.spin_n_positions.setRange(measurement.MIN_POSITIONS, measurement.MAX_POSITIONS)
        self.spin_n_positions.setValue(self._n_positions)
        self.spin_n_positions.valueChanged.connect(self._on_n_positions_changed)
        describe(self.spin_n_positions, "Mic positions",
                 f"How many microphone positions to average "
                 f"({measurement.MIN_POSITIONS}-{measurement.MAX_POSITIONS}), per the "
                 "roadmap's spatial-averaging convention.")
        count_row.addWidget(self.spin_n_positions)
        count_row.addStretch(1)
        lay.addLayout(count_row)

        self.list_positions = QListWidget()
        describe(self.list_positions, "Position progress",
                 "Shows which mic positions have been measured so far.")
        lay.addWidget(self.list_positions, 1)

        self.btn_measure_position = QPushButton("Measure Next Position")
        self.btn_measure_position.setProperty("class", "ghost")
        self.btn_measure_position.clicked.connect(self._on_measure_position)
        describe(self.btn_measure_position, "Measure next position",
                 "Runs the baseline sweep for the next unmeasured mic position.")
        lay.addWidget(self.btn_measure_position)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_baseline_back = QPushButton("Back")
        self.btn_baseline_back.setProperty("class", "ghost")
        self.btn_baseline_back.clicked.connect(self._go_back)
        describe(self.btn_baseline_back, "Back", "Return to the previous page.")
        btn_row.addWidget(self.btn_baseline_back)
        btn_row.addStretch(1)

        self.btn_baseline_next = QPushButton("Next")
        self.btn_baseline_next.setProperty("class", "primary")
        self.btn_baseline_next.setEnabled(False)
        self.btn_baseline_next.clicked.connect(self._on_baseline_next)
        describe(self.btn_baseline_next, "Next",
                 "Continue to target choice. Stays disabled until every "
                 "position is measured.")
        btn_row.addWidget(self.btn_baseline_next)
        lay.addLayout(btn_row)

        return page

    def _populate_baseline_page(self) -> None:
        self._refresh_position_list()

    def _on_n_positions_changed(self, value: int) -> None:
        self._n_positions = value
        self._position_curves = [None] * value
        self._refresh_position_list()

    def _refresh_position_list(self) -> None:
        self.list_positions.clear()
        for i, curve in enumerate(self._position_curves):
            status = "measured" if curve is not None else "not measured"
            self.list_positions.addItem(f"Position {i + 1}: {status}")
        all_measured = all(c is not None for c in self._position_curves)
        self.btn_baseline_next.setEnabled(all_measured)
        self.btn_measure_position.setEnabled(not all_measured)

    def _capture_raw_ir(self) -> tuple:
        """One raw ESS sweep capture: play + synchronized record,
        deconvolve to an impulse response. No magnitude conversion, no
        calibration subtraction -- split out of _capture_raw_curve (below)
        this session (S17) so the mic-estimation sub-flow can time-gate the
        IR before converting to magnitude, which _capture_raw_curve's own
        callers never need. Returns (ir, sample_rate)."""
        sample_rate = self.manager.effective_sample_rate()
        f1, f2 = _baseline_sweep_range(sample_rate)
        sweep, inverse = measurement.generate_ess(
            _BASELINE_SWEEP_DURATION_S, sample_rate, f1=f1, f2=f2)
        recorded = self._synchronized_record(sweep, sample_rate)
        return measurement.deconvolve(recorded, inverse, sample_rate), sample_rate

    def _capture_raw_curve(self) -> list:
        """One raw ESS sweep capture with no calibration subtraction,
        converted straight to magnitude (no gating): the common capture
        chain, with no cal subtraction applied, shared by baseline position
        capture (which then optionally subtracts the selected session cal,
        below) and the transfer-calibration sub-flow (S16), which needs the
        mic's own raw, uncorrected output to derive a NEW calibration from
        -- subtracting whatever cal happens to be selected on the setup
        page would corrupt that derivation, so this raw step is not itself
        cal-aware."""
        ir, sample_rate = self._capture_raw_ir()
        return measurement.ir_to_magnitude(ir, sample_rate)

    def _default_measure_position(self, position_index: int) -> list:
        """Real baseline capture for one mic position (S14): a raw capture
        (above), then subtract the selected mic calibration (S10) if one is
        chosen."""
        curve = self._capture_raw_curve()
        if self._selected_cal is not None:
            loaded = mic_cal.load_cal(self.manager.data_dir, self._selected_cal.id)
            if loaded is not None:
                _, cal_curve = loaded
                curve = mic_cal.subtract_cal(curve, cal_curve)
        return curve

    def _on_measure_position(self) -> None:
        try:
            index = next(i for i, c in enumerate(self._position_curves) if c is None)
        except StopIteration:
            return
        curve = self._measure_position_fn(index)
        self._position_curves[index] = curve if curve is not None else []
        self._refresh_position_list()

    def _on_baseline_next(self) -> None:
        if not all(c is not None for c in self._position_curves):
            return
        measured = [c for c in self._position_curves if c]
        self._baseline_curve = (
            measurement.smooth_measurement(measurement.average_positions(measured))
            if measured else [])
        self._go_to(self.target_page)

    # ── target choice page ───────────────────────────────────────────

    def _build_target_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("TARGET CURVE"))

        combo_row = QHBoxLayout()
        combo_row.addWidget(QLabel("Target"))
        self.cmb_target_curve = QComboBox()
        self.cmb_target_curve.addItem("Linear tilt", _TILT_TARGET_KEY)
        for label, name in target_curves.options_for_device_type("Speaker"):
            self.cmb_target_curve.addItem(label, name)
        self.cmb_target_curve.currentIndexChanged.connect(self._on_target_curve_changed)
        describe(self.cmb_target_curve, "Target curve",
                 "The reference response the generated correction aims for.")
        combo_row.addWidget(self.cmb_target_curve, 1)
        lay.addLayout(combo_row)

        tilt_row = QHBoxLayout()
        tilt_row.addWidget(QLabel("Tilt"))
        self.spin_target_tilt = QDoubleSpinBox()
        self.spin_target_tilt.setRange(_TILT_MIN_DB_PER_OCT, _TILT_MAX_DB_PER_OCT)
        self.spin_target_tilt.setSingleStep(0.1)
        self.spin_target_tilt.setDecimals(2)
        self.spin_target_tilt.setSuffix(" dB/oct")
        self.spin_target_tilt.valueChanged.connect(self._on_target_tilt_changed)
        describe(self.spin_target_tilt, "Target tilt",
                 "Downward slope per octave for a linear-tilt target "
                 "(ignored when a named target curve is selected).")
        tilt_row.addWidget(self.spin_target_tilt)
        tilt_row.addStretch(1)
        lay.addLayout(tilt_row)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_target_back = QPushButton("Back")
        self.btn_target_back.setProperty("class", "ghost")
        self.btn_target_back.clicked.connect(self._go_back)
        describe(self.btn_target_back, "Back", "Return to the previous page.")
        btn_row.addWidget(self.btn_target_back)
        btn_row.addStretch(1)

        self.btn_target_next = QPushButton("Next")
        self.btn_target_next.setProperty("class", "primary")
        self.btn_target_next.clicked.connect(self._on_target_next)
        describe(self.btn_target_next, "Next", "Continue to results.")
        btn_row.addWidget(self.btn_target_next)
        lay.addLayout(btn_row)

        return page

    def _on_target_curve_changed(self, index: int) -> None:
        name = self.cmb_target_curve.itemData(index)
        self._target_choice["curve_name"] = name
        self.spin_target_tilt.setEnabled(name == _TILT_TARGET_KEY)

    def _on_target_tilt_changed(self, value: float) -> None:
        self._target_choice["tilt_db_per_oct"] = value

    def _on_target_next(self) -> None:
        self._generate_correction()
        self._populate_results_page()
        self._go_to(self.results_page)

    # ── correction generation (S14) ───────────────────────────────────

    def _target_label(self) -> str:
        name = self._target_choice["curve_name"]
        if name != _TILT_TARGET_KEY:
            return target_curves.label(name)
        slope = self._target_choice["tilt_db_per_oct"]
        return "flat (anechoic)" if slope == 0 else f"{slope:+.2f} dB/oct tilt"

    def _build_subsonic_filter(self) -> list:
        """Subsonic high-pass offer, mirroring the Spinorama import's
        existing behavior (rumble/ui/autoeq_dialog.py's
        "spinorama_add_crossover" setting + eq_target.suggested_highpass_hz)
        -- a room measurement has no manufacturer-published extension
        estimate, so the -6 dB point is derived from the measured baseline
        curve itself via _estimate_low_extension_hz()."""
        if not self.manager.settings.get("spinorama_add_crossover", False):
            return []
        ref6 = _estimate_low_extension_hz(self._baseline_curve)
        fc = eq_target.suggested_highpass_hz(ref_6db=ref6)
        if not fc:
            return []
        return [apo_config.Filter(type="HPQ", fc=fc, q=0.707)]

    def _correction_max_boost_db(self) -> float:
        """The boost cap _generate_correction() fits into (S17 tier
        integration): tightened to mic_cal.ESTIMATED_TIER_MAX_BOOST_DB when
        the selected calibration is source="estimated" (its own +-2-3 dB
        uncertainty shouldn't get compounded with a full-strength
        correction on top of it -- see that constant's own docstring for
        the cited precedent); the standard eq_target.make_correction
        default (_STANDARD_MAX_BOOST_DB) otherwise."""
        cal = self._selected_cal
        if cal is not None and cal.source == "estimated":
            return mic_cal.ESTIMATED_TIER_MAX_BOOST_DB
        return _STANDARD_MAX_BOOST_DB

    def _generate_correction(self) -> None:
        """Correction generation (S14): the SAME eq_target.make_correction
        pipeline the Spinorama import flow uses
        (rumble/core/measurements/spinorama_provider.py), with the tier's
        correction_band as the fit range (S10: no-cal mics correct only
        <=300 Hz) and the established boost/cut caps (S17: tightened for
        the "estimated" tier, see _correction_max_boost_db)."""
        band = self._correction_band_fn(self._selected_cal)
        choice = self._target_choice
        if choice["curve_name"] != _TILT_TARGET_KEY:
            target_curve = target_curves.get_curve(choice["curve_name"])
            slope = 0.0
        else:
            target_curve = None
            slope = choice["tilt_db_per_oct"]
        corr, clamp = eq_target.make_correction(
            self._baseline_curve, slope_db_per_oct=slope, pivot_hz=1000.0,
            band=band, max_boost_db=self._correction_max_boost_db(),
            adaptive_smoothing=True, target_curve=target_curve)
        self._correction_curve = corr
        self._correction_band = band
        self._clamp_summary = clamp
        self._extra_filters = self._build_subsonic_filter()

    # ── results page ──────────────────────────────────────────────────

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("RESULTS"))

        self.lbl_results_summary = QLabel("")
        self.lbl_results_summary.setWordWrap(True)
        self.lbl_results_summary.setProperty("class", "muted")
        lay.addWidget(self.lbl_results_summary)

        self.btn_save_profile = QPushButton("Save as Profile")
        self.btn_save_profile.setProperty("class", "ghost")
        self.btn_save_profile.clicked.connect(self._on_save_profile)
        describe(self.btn_save_profile, "Save as profile",
                 "Creates a new profile from the generated room correction, "
                 "tagged \"Room Correction\".")
        lay.addWidget(self.btn_save_profile)

        self.lbl_save_status = QLabel("")
        self.lbl_save_status.setWordWrap(True)
        lay.addWidget(self.lbl_save_status)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_results_back = QPushButton("Back")
        self.btn_results_back.setProperty("class", "ghost")
        self.btn_results_back.clicked.connect(self._go_back)
        describe(self.btn_results_back, "Back", "Return to the previous page.")
        btn_row.addWidget(self.btn_results_back)
        btn_row.addStretch(1)

        self.btn_results_next = QPushButton("Next")
        self.btn_results_next.setProperty("class", "primary")
        self.btn_results_next.setEnabled(False)
        self.btn_results_next.clicked.connect(self._on_results_next)
        describe(self.btn_results_next, "Next",
                 "Continue to verify. Stays disabled until the correction "
                 "is saved as a profile.")
        btn_row.addWidget(self.btn_results_next)
        lay.addLayout(btn_row)

        return page

    def _populate_results_page(self) -> None:
        clamp = self._clamp_summary
        band = self._correction_band
        n = len(self._correction_curve)
        summary = (
            f"Correction generated from {n} points across "
            f"{band[0]:.0f}-{band[1]:.0f} Hz toward {self._target_label()}. "
            f"Boost capped at {clamp.points_clamped_boost} point(s), cut "
            f"floored at {clamp.points_clamped_cut} point(s)")
        summary += (f" (up to {clamp.max_clamped_delta_db:.1f} dB)."
                    if clamp.notable() else ".")
        self.lbl_results_summary.setText(summary)
        self.btn_save_profile.setEnabled(bool(self._correction_curve))
        self.lbl_save_status.setText("")
        self.btn_results_next.setEnabled(False)
        self._saved_profile_id = None

    def _on_save_profile(self) -> None:
        """Save-as-profile (S14): the existing manager creation path, the
        same shape rumble/ui/autoeq_dialog.py's _save_profile() uses for a
        Spinorama import, tagged "Room Correction"."""
        curve = self._correction_curve
        if not curve:
            self.lbl_save_status.setText("No correction to save -- measure a baseline first.")
            return
        sample_rate = self.manager.effective_sample_rate()
        config = autoeq_merge.build_curve_config(
            curve, sample_rate=sample_rate,
            extra_filters=list(self._extra_filters or []))
        n_positions = sum(1 for c in self._position_curves if c)
        target_label = self._target_label()
        name = f"Room Correction ({target_label})"
        description = (
            f"Room correction measured from {n_positions} mic position(s) "
            f"toward a {target_label} target.")
        meta = self.manager.create_profile(
            name, description=description, tags=["Room Correction"], config=config,
            fit_sample_rate=sample_rate)
        self._saved_profile_id = meta.id
        self.lbl_save_status.setText(f"Saved as “{meta.name}”.")
        self.btn_results_next.setEnabled(True)

    def _on_results_next(self) -> None:
        self._populate_verify_page()
        self._go_to(self.verify_page)

    # ── verify page (S15: re-measure the corrected system + overlay) ──

    def _build_verify_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(10)
        lay.addWidget(_page_heading("VERIFY"))

        note = QLabel(_VERIFY_TIER_NOTE)
        note.setWordWrap(True)
        note.setProperty("class", "muted")
        lay.addWidget(note)

        self.list_verify_positions = QListWidget()
        self.list_verify_positions.setMaximumHeight(dp(90, 70))
        describe(self.list_verify_positions, "Verify position progress",
                 "Shows which mic positions have been re-measured against "
                 "the corrected profile so far.")
        lay.addWidget(self.list_verify_positions)

        self.btn_measure_verify_position = QPushButton("Measure Next Verify Position")
        self.btn_measure_verify_position.setProperty("class", "ghost")
        self.btn_measure_verify_position.clicked.connect(self._on_measure_verify_position)
        describe(self.btn_measure_verify_position, "Measure next verify position",
                 "Previews the saved room-correction profile (without activating "
                 "it) and re-runs the baseline sweep for the next unmeasured "
                 "position.")
        lay.addWidget(self.btn_measure_verify_position)

        self.lbl_verify_status = QLabel("")
        self.lbl_verify_status.setWordWrap(True)
        self.lbl_verify_status.setProperty("class", "muted")
        lay.addWidget(self.lbl_verify_status)

        self.plot_verify = pg.PlotWidget(
            axisItems={"bottom": LogFreqAxis(orientation="bottom")})
        self.plot_verify.setMinimumHeight(dp(180, 150))
        self.plot_verify.showGrid(x=True, y=True, alpha=0.15)
        self.plot_verify.setLabel("left", "dB")
        plot_item = self.plot_verify.getPlotItem()
        plot_item.setMouseEnabled(x=False, y=False)
        plot_item.hideButtons()

        self._verify_baseline_item = self.plot_verify.plot([], [], name="Measured before")
        self._verify_target_item = self.plot_verify.plot([], [], name="Target")
        self._verify_after_item = self.plot_verify.plot([], [], name="Measured after")
        lay.addWidget(self.plot_verify, 1)

        self.lbl_verify_summary = QLabel("")
        self.lbl_verify_summary.setWordWrap(True)
        lay.addWidget(self.lbl_verify_summary)

        self.lbl_verify_tier = QLabel("")
        self.lbl_verify_tier.setWordWrap(True)
        self.lbl_verify_tier.setProperty("class", "muted")
        lay.addWidget(self.lbl_verify_tier)

        self.btn_export_verify = QPushButton("Export Report…")
        self.btn_export_verify.setProperty("class", "ghost")
        self.btn_export_verify.setEnabled(False)
        self.btn_export_verify.clicked.connect(self._on_export_verify)
        describe(self.btn_export_verify, "Export report",
                 "Save a plain-text summary of the verify pass -- baseline, "
                 "target and verified-after tables, the achieved-vs-predicted "
                 "delta, and the tier's honest scope caveat.")
        lay.addWidget(self.btn_export_verify)

        btn_row = QHBoxLayout()
        self.btn_verify_back = QPushButton("Back")
        self.btn_verify_back.setProperty("class", "ghost")
        self.btn_verify_back.clicked.connect(self._on_verify_back)
        describe(self.btn_verify_back, "Back", "Return to the previous page.")
        btn_row.addWidget(self.btn_verify_back)
        btn_row.addStretch(1)

        self.btn_verify_next = QPushButton("Next")
        self.btn_verify_next.setProperty("class", "primary")
        self.btn_verify_next.setEnabled(False)
        self.btn_verify_next.clicked.connect(lambda: self._go_to(self.done_page))
        describe(self.btn_verify_next, "Next",
                 "Continue to done. Stays disabled until the verify "
                 "measurement completes.")
        btn_row.addWidget(self.btn_verify_next)
        lay.addLayout(btn_row)

        return page

    def _apply_verify_chart_theme(self) -> None:
        t = self.theme
        muted = t.get("text_muted", "#8589A8")
        self.plot_verify.setBackground(_qcolor(t.get("graph_bg", "#101018")))
        plot_item = self.plot_verify.getPlotItem()
        for axis_name in ("bottom", "left"):
            axis = plot_item.getAxis(axis_name)
            axis.setPen(pg.mkPen(_qcolor(t.get("graph_text", muted))))
            axis.setTextPen(pg.mkPen(_qcolor(t.get("graph_text", muted))))

        accent = t.get("accent", "#4660E6")
        self._verify_baseline_item.setPen(
            pg.mkPen(_alpha(muted, 200), width=1.5, style=Qt.PenStyle.DashLine))
        self._verify_target_item.setPen(
            pg.mkPen(_qcolor(t.get("graph_zero", muted)), width=1.5,
                     style=Qt.PenStyle.DotLine))
        self._verify_after_item.setPen(pg.mkPen(_qcolor(accent), width=2.0))

    def _populate_verify_page(self) -> None:
        self._verify_position_curves = [None] * self._n_positions
        self._verify_curve = []
        self.lbl_verify_status.setText("")
        self.lbl_verify_summary.setText("")
        self.lbl_verify_tier.setText(self._tier_label_text(self._selected_cal))
        self.btn_export_verify.setEnabled(False)
        self.btn_verify_next.setEnabled(False)
        self._verify_baseline_item.setData([], [])
        self._verify_target_item.setData([], [])
        self._verify_after_item.setData([], [])
        self._refresh_verify_position_list()

    def _refresh_verify_position_list(self) -> None:
        self.list_verify_positions.clear()
        for i, curve in enumerate(self._verify_position_curves):
            status = "measured" if curve is not None else "not measured"
            self.list_verify_positions.addItem(f"Position {i + 1}: {status}")
        all_measured = all(c is not None for c in self._verify_position_curves)
        self.btn_measure_verify_position.setEnabled(not all_measured)

    # ── verify preview lifecycle (S15) ───────────────────────────────

    def _start_verify_preview(self) -> bool:
        """Preview the saved room-correction profile (NOT activation) so the
        re-measurement actually captures the corrected system. Layered on
        top of the wizard-wide BypassGuard (forced True since the intro
        page): temporarily un-bypasses so preview_config() takes effect,
        same as ABXRunner's own choke point through the manager. Idempotent
        -- a second call while already previewing is a no-op success."""
        if self._verify_previewing:
            return True
        if self._saved_profile_id is None:
            return False
        config = self.manager.load_profile_config(self._saved_profile_id)
        if config is None:
            return False
        prior_bypass = self.manager.is_bypassed()
        self.manager.set_bypass(False)
        if not self.manager.preview_config(config):
            self.manager.set_bypass(prior_bypass)
            return False
        self._verify_prior_bypass = prior_bypass
        self._verify_previewing = True
        return True

    def _end_verify_preview(self) -> None:
        """Restore preview/bypass state -- called on measurement completion,
        Back, and unconditionally from _teardown() (so cancel/close mid
        verify-measurement restores it too), and directly from the
        exception path in _on_measure_verify_position() (so a mid-position
        failure restores it as well). Idempotent."""
        if not self._verify_previewing:
            return
        self.manager.end_preview()
        self.manager.set_bypass(self._verify_prior_bypass)
        self._verify_previewing = False
        self._verify_prior_bypass = None

    def _on_measure_verify_position(self) -> None:
        try:
            index = next(i for i, c in enumerate(self._verify_position_curves) if c is None)
        except StopIteration:
            return
        if not self._start_verify_preview():
            self.lbl_verify_status.setText(
                "Could not preview the corrected profile for verification.")
            return
        try:
            curve = self._measure_position_fn(index)
        except Exception:
            self._end_verify_preview()
            raise
        self._verify_position_curves[index] = curve if curve is not None else []
        self._refresh_verify_position_list()
        if all(c is not None for c in self._verify_position_curves):
            self._finish_verify_measurement()

    def _on_verify_back(self) -> None:
        self._end_verify_preview()
        self._go_back()

    def _finish_verify_measurement(self) -> None:
        measured = [c for c in self._verify_position_curves if c]
        self._verify_curve = (
            measurement.smooth_measurement(measurement.average_positions(measured))
            if measured else [])
        self._end_verify_preview()
        self._populate_verify_results()
        self.btn_verify_next.setEnabled(bool(self._verify_curve))

    # ── verify results: overlay + achieved-vs-predicted delta (S15) ──

    def _target_curve_on_grid(self, grid: list[float]) -> list:
        choice = self._target_choice
        if choice["curve_name"] != _TILT_TARGET_KEY:
            curve = target_curves.get_curve(choice["curve_name"])
            values = autoeq_merge.interp_log(curve, grid)
            return list(zip(grid, values))
        return eq_target.tilt_target(grid, slope_db_per_oct=choice["tilt_db_per_oct"],
                                      pivot_hz=1000.0)

    @staticmethod
    def _rms_deviation(values: list[float], target_values: list[float],
                        grid: list[float], band: tuple[float, float]) -> float:
        lo, hi = band
        diffs = [v - t for f, v, t in zip(grid, values, target_values) if lo <= f <= hi]
        if not diffs:
            return 0.0
        return math.sqrt(sum(d * d for d in diffs) / len(diffs))

    def _populate_verify_results(self) -> None:
        grid = [f for f, _ in self._correction_curve] or eq_target.log_grid(20.0, 20000.0, 12)
        baseline_vals = (autoeq_merge.interp_log(self._baseline_curve, grid)
                         if self._baseline_curve else [0.0] * len(grid))
        target_vals = [v for _, v in self._target_curve_on_grid(grid)]
        verified_vals = (autoeq_merge.interp_log(self._verify_curve, grid)
                         if self._verify_curve else [0.0] * len(grid))
        corr_vals = ([g for _, g in self._correction_curve]
                     if self._correction_curve else [0.0] * len(grid))
        predicted_vals = [b + c for b, c in zip(baseline_vals, corr_vals)]

        band = self._correction_band
        self._verify_baseline_rms = self._rms_deviation(baseline_vals, target_vals, grid, band)
        self._verify_predicted_rms = self._rms_deviation(predicted_vals, target_vals, grid, band)
        self._verify_achieved_rms = self._rms_deviation(verified_vals, target_vals, grid, band)

        log_x = [math.log10(f) for f in grid]
        self._verify_baseline_item.setData(log_x, baseline_vals)
        self._verify_target_item.setData(log_x, target_vals)
        self._verify_after_item.setData(log_x, verified_vals)

        delta = self._verify_achieved_rms - self._verify_predicted_rms
        self.lbl_verify_summary.setText(
            f"Achieved {self._verify_achieved_rms:.1f} dB RMS deviation from target "
            f"(baseline was {self._verify_baseline_rms:.1f} dB, predicted "
            f"{self._verify_predicted_rms:.1f} dB) -- {delta:+.1f} dB vs. predicted.")
        self.lbl_verify_tier.setText(self._tier_label_text(self._selected_cal))
        self.btn_export_verify.setEnabled(bool(self._verify_curve))

    def _verify_export_text(self) -> str:
        """Plain-text export in the house style (rumble/core/hearing_test.py's
        export_text): header + disclaimer, a before/target/after table, the
        achieved-vs-predicted delta, the tier caveat (same wording as the
        setup page's own tier label), and an honest scope line."""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        grid = [f for f, _ in self._correction_curve] or eq_target.log_grid(20.0, 20000.0, 12)
        baseline_vals = (autoeq_merge.interp_log(self._baseline_curve, grid)
                         if self._baseline_curve else [0.0] * len(grid))
        target_vals = [v for _, v in self._target_curve_on_grid(grid)]
        verified_vals = (autoeq_merge.interp_log(self._verify_curve, grid)
                         if self._verify_curve else [0.0] * len(grid))

        lines = [
            "RUMBLE Room Correction -- Verify Report",
            f"Date: {date_str}",
            f"Target: {self._target_label()}",
            "Relative in-room measurement -- not an anechoic or professional "
            "acoustic report.",
            "",
            "freq_hz | baseline_db | target_db | verified_after_db",
        ]
        for f, b, t, a in zip(grid, baseline_vals, target_vals, verified_vals):
            lines.append(f"{f:.0f} | {b:.1f} | {t:.1f} | {a:.1f}")
        lines.append("")

        band = self._correction_band
        lines.append(f"Correction fit range: {band[0]:.0f}-{band[1]:.0f} Hz.")
        lines.append(
            f"Achieved {self._verify_achieved_rms:.1f} dB RMS deviation from target "
            f"(baseline was {self._verify_baseline_rms:.1f} dB, predicted "
            f"{self._verify_predicted_rms:.1f} dB).")
        lines.append(self._tier_label_text(self._selected_cal))
        lines.append("")
        lines.append(
            "Scope: this is a same-microphone before/after comparison -- the "
            "microphone's own capsule response cancels in the difference, so "
            "this verify pass is meaningful even without a calibrated mic. It "
            "does not confirm absolute tonal accuracy beyond the tier's own "
            "trust range stated above.")
        return "\n".join(lines)

    def _on_export_verify(self) -> None:
        if not self._verify_curve:
            return
        text = self._verify_export_text()
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export Verify Report", "room_correction_verify.txt",
            "Text File (*.txt)")
        if not path_str:
            return
        Path(path_str).write_text(text, encoding="utf-8")

    # ── done page ─────────────────────────────────────────────────────

    def _build_done_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        lay.addWidget(_page_heading("DONE"))
        done_note = QLabel(
            "Your room-correction profile is saved to the library but has NOT "
            "been activated -- switch to it from the main profile list when "
            "you're ready to use it, the same as any imported profile.")
        done_note.setWordWrap(True)
        done_note.setProperty("class", "muted")
        lay.addWidget(done_note)
        lay.addStretch(1)

        return page

    # ── exit paths always restore bypass / stop any live audio ────────

    def _teardown(self) -> None:
        self._end_verify_preview()
        if self._bypass_guard is not None:
            guard = self._bypass_guard
            self._bypass_guard = None
            guard.__exit__(None, None, None)
        if self._recorder is not None:
            recorder = self._recorder
            self._recorder = None
            try:
                recorder.stop()
            except Exception:
                pass
        if self._engine is not None:
            engine = self._engine
            self._engine = None
            try:
                engine.stop()
            except Exception:
                pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._teardown()
        super().closeEvent(event)

    def reject(self) -> None:
        self._teardown()
        super().reject()

    def accept(self) -> None:
        self._teardown()
        super().accept()
