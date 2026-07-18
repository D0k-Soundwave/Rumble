"""pyqtgraph-based live frequency response canvas.

Renders the combined EQ curve (with a soft glow) — parametric bands plus the
section's GraphicEQ, so the main plot always shows the completed result — a
dotted GraphicEQ contribution curve when one is in use, optional per-band
highlight curve, draggable band handles (frequency + gain), grid lines at
standard frequencies, a 0 dB reference line and a clip-warning shaded region.

A dashed "points" connector runs straight through each active band's own
gain value (held flat past the outermost bands, out to the graph edges) —
deliberately simplified and non-physical, but the one place gain deltas
between adjacent bands are labelled directly, which the true (per-band
Q/bandwidth-bent) combined curve can't show at a glance.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QMenu, QPushButton, QVBoxLayout, QWidget

from rumble.core import eq_math
from rumble.core.apo_config import Filter
from rumble.core.audio.metering import StereoLevels
from rumble.core.spectrum import broadband_offset_db, subtract_response
from rumble.ui import a11y, fonts, icons
from rumble.ui.spectrum_overlay import SpectrumController

X_TICKS_HZ = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]

# Phase 6 live spectrum analyzer (rumble/core/spectrum.py +
# rumble/ui/spectrum_overlay.py): status hint shown over the graph while the
# Analyzer is on and the capture reports "unavailable" -- the loopback stream
# could not be opened at all (exclusive-mode/ASIO stream holding the endpoint,
# or this endpoint has no loopback device). The "silent" status (stream open
# and healthy, just reading digital silence because nobody is playing
# anything) shows no hint -- an empty graph already says that.
_SPECTRUM_HINT_UNAVAILABLE = ("Can't capture this device — it may be in exclusive use, "
                              "or an ASIO stream bypassing Equalizer APO.")


def _format_freq(f: float) -> str:
    if f >= 1000:
        khz = f / 1000
        return f"{khz:g}k"
    return f"{f:.0f}"


def _qcolor(value: str) -> QColor:
    """Parse a theme token colour string (#RRGGBB or #RRGGBBAA) into a QColor."""
    s = value.lstrip("#")
    if len(s) == 8:
        r, g, b, a = (int(s[i:i + 2], 16) for i in (0, 2, 4, 6))
        return QColor(r, g, b, a)
    return QColor(value)


def _alpha(value: str, alpha: int) -> QColor:
    c = QColor(value)
    c.setAlpha(alpha)
    return c


class LogFreqAxis(pg.AxisItem):
    """X-axis that displays frequencies while the underlying data is log10(Hz)."""

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - pyqtgraph override
        return [_format_freq(10 ** v) for v in values]


class _BandHandle(pg.TargetItem):
    """TargetItem with an explicit click signal (sigClicked absent in older pyqtgraph)."""
    clicked = Signal(object)

    def mouseClickEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self)
            ev.accept()
        else:
            super().mouseClickEvent(ev)


class EQGraph(QWidget):
    """Frequency response graph: 20 Hz – 20 kHz log scale, ±30 dB
    (switchable to ±15 dB via set_y_range — see the Settings EQ page)."""

    band_dragged = Signal(int, float, float)   # band index, new fc (Hz), new gain (dB)
    band_selected = Signal(int)               # band index clicked
    # Phase 6: emitted whenever the user changes Analyzer/mode/peak-hold via the
    # graph's own controls — {"spectrum_enabled", "spectrum_mode",
    # "spectrum_peak_hold"} — for the caller to persist (see
    # configure_spectrum_settings()'s docstring for the read-back half).
    spectrum_settings_changed = Signal(dict)
    # Phase 6 Task 4: mirrors SpectrumController.clipping_changed straight
    # through -- see _build_spectrum_controller()'s connection and
    # _on_spectrum_clipping() below. editor_panel.py listens to feed the
    # headroom indicator's "measured clipping" warning state.
    measured_clipping_changed = Signal(bool)
    # Rumble 3.7: mirrors SpectrumController.drift_changed straight through,
    # the same pattern as measured_clipping_changed above -- see
    # _on_spectrum_drift() below. editor_panel.py listens to show a distinct
    # "measured spectrum doesn't match the calculated curve" indicator.
    measured_drift_changed = Signal(bool)
    # Passthrough of SpectrumController.stereo_level_ready -- feeds the
    # right-side VU/Peak/Reference meter panel's OUTPUT bars (editor_panel.py
    # owns the widget; this widget owns the capture pipeline, same split as
    # the signals above).
    stereo_level_ready = Signal(object)  # metering.StereoLevels
    # Companion signal for the same meter panel's INPUT(estimated) bars --
    # `metering.StereoLevels | None` (`None` whenever no calculated EQ
    # response is known yet, so the input estimate can't be synthesized).
    # Emitted from `_on_spectrum_stereo_level()` on the same tick as
    # `stereo_level_ready` above, using the offset computed in
    # `_on_spectrum_ready()` moments earlier the same tick.
    estimated_input_level_ready = Signal(object)

    def __init__(self, theme: dict, parent: QWidget | None = None,
                 spectrum_capture_factory=None):
        super().__init__(parent)
        self.theme = theme
        self._y_limit = 30  # vertical scale: ±30 (full gain range) or ±15 dB
        self._filters: list[Filter] = []
        self._geq_string = ""
        self._ir_response: np.ndarray | None = None
        self._listener_left_db: np.ndarray | None = None
        self._listener_right_db: np.ndarray | None = None
        self._sample_rate = eq_math.DEFAULT_SAMPLE_RATE
        self._highlighted_index: int | None = None
        self._handles: list[pg.TargetItem] = []
        self._header_labels: list[QLabel] = []
        self._band_lines: list[pg.InfiniteLine] = []
        self._delta_labels: list[pg.TextItem] = []
        self._suppress_handle_events = False

        # Phase 6 live spectrum analyzer state — see _build_spectrum_controller()
        # and _build_spectrum_controls(). `spectrum_capture_factory` is the
        # test injection seam (forwarded straight to SpectrumController),
        # matching Task 2's own `capture_factory` seam; production callers
        # never pass it (defaults to a real LoopbackCapture).
        # False while the Analyzer/gear buttons live in this widget's own
        # header strip and are hand-positioned there; True once an owner has
        # taken them over (see take_spectrum_controls).
        self._spectrum_controls_detached = False
        self._spectrum_enabled = False
        self._spectrum_mode = "output"  # "output" | "source" (input) | "both"
        self._spectrum_compare_front = "output"  # which series is on top in "both"
        self._spectrum_peak_hold = False
        # Which endpoint the live capture listens to — an APO `Device:`
        # pattern, or None for "follow the system default output". See
        # `set_spectrum_device()`.
        self._spectrum_device: str | None = None
        # True while the capture is deliberately listening to something OTHER
        # than the device this profile targets (a capture/mic endpoint has no
        # loopback tap, so we fall back to the default output). Drift
        # detection must stay off in that state — see set_spectrum_device().
        self._spectrum_is_fallback = False
        self._spectrum_response_db: np.ndarray | None = None
        # Broadband dB offset between the estimated pre-EQ source and the
        # measured post-EQ capture, recomputed every `_on_spectrum_ready()`
        # tick (see `spectrum_core.broadband_offset_db`) and consumed by
        # `_on_spectrum_stereo_level()` on the SAME tick to synthesize
        # estimated-input L/R levels for the VU/Peak/Reference meter panel.
        # None whenever `_spectrum_response_db` is unknown, matching
        # `_input_of()`'s own None-when-unknown contract.
        self._last_input_offset_db: float | None = None

        # Keyboard-operable: the graph takes focus so arrow keys can select a
        # band and adjust its gain without a mouse (WCAG 2.1.1). A 2px content
        # margin leaves room for the accent focus ring drawn in paintEvent.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Equalizer response graph")
        self.setAccessibleDescription(
            "Interactive frequency response. Press Left or Right to select a "
            "band, Up or Down to change its gain (hold Shift for fine steps).")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)

        # Separate header strip — band number + frequency, sits above the graph
        self._header = QWidget()
        self._header.setFixedHeight(26)
        layout.addWidget(self._header)

        self.plot = pg.PlotWidget(axisItems={"bottom": LogFreqAxis(orientation="bottom")})
        layout.addWidget(self.plot)

        self._zero_line: pg.InfiniteLine | None = None
        self._dragging = False

        self._setup_plot()
        self._build_curves()
        self._build_spectrum_controller(spectrum_capture_factory)
        self._build_spectrum_controls()
        self._apply_theme_colors()
        self.set_theme(theme)  # styles info bar

    # ── construction ────────────────────────────────────────────────

    def _setup_plot(self) -> None:
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()

        plot_item = self.plot.getPlotItem()
        plot_item.setContentsMargins(8, 4, 8, 4)
        plot_item.showGrid(x=False, y=False)
        self.plot.setXRange(np.log10(20), np.log10(20000), padding=0.02)
        self.plot.setYRange(-self._y_limit, self._y_limit, padding=0.02)

        bottom = plot_item.getAxis("bottom")
        left = plot_item.getAxis("left")
        bottom.setTicks([[(np.log10(f), _format_freq(f)) for f in X_TICKS_HZ]])
        left.setWidth(34)
        self._update_y_ticks()

    def _update_y_ticks(self) -> None:
        """Label every 6 dB on the ±30 scale (3 dB on ±15), doubling the step
        when the plot is too short for nine labels to keep clear of each
        other. Either way the axis edges themselves stay unlabelled."""
        base = 6 if self._y_limit >= 30 else 3
        step = base if self.plot.height() >= 220 else base * 2
        ticks = [v for v in range(-self._y_limit + base, self._y_limit, base)
                 if v % step == 0]
        left = self.plot.getPlotItem().getAxis("left")
        left.setTicks([[(v, ("0" if v == 0 else f"{v:+d}")) for v in ticks]])

    def set_y_range(self, limit: int) -> None:
        """Switch the vertical scale between ±30 and ±15 dB. The plot keeps
        its size — curves simply draw proportionally taller on the tighter
        scale. Band gains beyond the visible range stay intact; their handles
        pin to the plot edge."""
        limit = 15 if int(limit) == 15 else 30
        if limit == self._y_limit:
            return
        self._y_limit = limit
        self.plot.setYRange(-limit, limit, padding=0.02)
        self._update_y_ticks()
        self._rebuild_handles()

    def _build_curves(self) -> None:
        plot_item = self.plot.getPlotItem()

        self._glow_curve = plot_item.plot([], [])
        self._glow_curve.setZValue(0)
        self._main_curve = plot_item.plot([], [])
        self._main_curve.setZValue(1)

        self._band_curve = plot_item.plot([], [])
        self._band_curve.setZValue(2)
        self._band_curve.setVisible(False)

        # Dotted GraphicEQ contribution curve (main curve shows the total)
        self._geq_curve = plot_item.plot([], [])
        self._geq_curve.setZValue(0.8)
        self._geq_curve.setVisible(False)

        # Dashed convolution IR contribution curve (main curve shows the total)
        self._ir_curve = plot_item.plot([], [])
        self._ir_curve.setZValue(0.7)
        self._ir_curve.setVisible(False)

        # Optional L/R-effective overlay: base combined response plus the
        # active Listener Slot's per-ear correction (design spec §5.1).
        # Dash-dot so it's visually distinct from the existing dashed IR
        # curve and dotted GEQ curve without needing new per-theme color
        # tokens across all 10 themes.
        self._listener_l_curve = plot_item.plot([], [])
        self._listener_l_curve.setZValue(0.6)
        self._listener_l_curve.setVisible(False)
        self._listener_r_curve = plot_item.plot([], [])
        self._listener_r_curve.setZValue(0.6)
        self._listener_r_curve.setVisible(False)

        # Dashed connector: straight lines through each band's actual gain value
        self._points_curve = plot_item.plot([], [])
        self._points_curve.setZValue(1.5)

        self._clip_region = pg.LinearRegionItem(
            values=(0, 30), orientation="horizontal", movable=False,
        )
        self._clip_region.setZValue(-15)
        for line in self._clip_region.lines:
            line.setPen(pg.mkPen(None))
        self._clip_region.setVisible(False)
        plot_item.addItem(self._clip_region)

        # Phase 6 live spectrum overlay: drawn BEHIND every response curve
        # above (glow=0, main=1). The OUTPUT (post-EQ) series is the filled warm
        # trace; the INPUT (pre-EQ, source-estimated) series is a second, cool
        # trace shown only in "both"/"input" modes. Each has an undamped
        # peak-hold trace. All hidden until "Analyzer" is toggled on.
        self._spectrum_curve = plot_item.plot([], [])
        self._spectrum_curve.setZValue(-1)
        self._spectrum_curve.setVisible(False)
        self._spectrum_peak_curve = plot_item.plot([], [])
        self._spectrum_peak_curve.setZValue(-0.9)
        self._spectrum_peak_curve.setVisible(False)

        self._spectrum_in_curve = plot_item.plot([], [])
        self._spectrum_in_curve.setZValue(-1.05)
        self._spectrum_in_curve.setVisible(False)
        self._spectrum_in_peak_curve = plot_item.plot([], [])
        self._spectrum_in_peak_curve.setZValue(-0.95)
        self._spectrum_in_peak_curve.setVisible(False)

        # Capture-status hint — shown centred over the plot only while the Analyzer is
        # on and the capture reports "unavailable" (see _SPECTRUM_HINT_UNAVAILABLE).
        # It is set in `_on_spectrum_status`, so the value passed here is only a
        # placeholder for the never-shown initial state.
        mid_x = float((np.log10(X_TICKS_HZ[0]) + np.log10(X_TICKS_HZ[-1])) / 2)
        self._spectrum_hint = pg.TextItem(text=_SPECTRUM_HINT_UNAVAILABLE, anchor=(0.5, 0.5))
        self._spectrum_hint.setPos(mid_x, 0.0)
        self._spectrum_hint.setZValue(5)
        self._spectrum_hint.setVisible(False)
        plot_item.addItem(self._spectrum_hint)

    def _build_spectrum_controller(self, capture_factory) -> None:
        """Owns the Phase 6 live capture pipeline. Constructed disabled/
        stopped -- `configure_spectrum_settings()` (settings-driven, called
        by the caller the same way `set_y_range()` already is) or the "Analyzer"
        button starts it."""
        self._spectrum_ctrl = SpectrumController(
            eq_math.frequency_array(), sample_rate=self._sample_rate,
            capture_factory=capture_factory, parent=self,
        )
        self._spectrum_ctrl.spectrum_ready.connect(self._on_spectrum_ready)
        self._spectrum_ctrl.status_changed.connect(self._on_spectrum_status)
        self._spectrum_ctrl.clipping_changed.connect(self._on_spectrum_clipping)
        self._spectrum_ctrl.drift_changed.connect(self._on_spectrum_drift)
        self._spectrum_ctrl.stereo_level_ready.connect(self._on_spectrum_stereo_level)

    def _build_spectrum_controls(self) -> None:
        """Builds the "Analyzer" ghost toggle plus a gear ghost button opening a
        manually-built QMenu ("Peak hold" checkable, "Output"/"Source
        (estimated)" radio),
        right-aligned in the existing header strip. Built manually rather
        than via `setMenuEnabled`/right-click, which are deliberately off
        for this widget (see _setup_plot) — a left-click-triggered QMenu
        doesn't touch either.

        The header strip is the default home, used when EQGraph stands alone.
        An owner that has a better place for them (editor_panel puts them on
        the tab row, level with the tab buttons) calls
        `take_spectrum_controls()` to reparent them out — see that method."""
        self._btn_live = QPushButton("Analyser", self._header)
        self._btn_live.setProperty("class", "ghost")
        self._btn_live.setCheckable(True)
        self._btn_live.setFixedSize(66, 20)
        a11y.describe(self._btn_live, "Analyser",
                       "Show what your audio is actually doing right now: a live "
                       "spectrum trace, captured from the system's audio output, "
                       "drawn under the response curve.")
        self._btn_live.toggled.connect(self._on_live_toggled)

        self._btn_spectrum_menu = QPushButton(self._header)
        self._btn_spectrum_menu.setProperty("class", "ghost")
        self._btn_spectrum_menu.setFixedSize(22, 20)
        self._btn_spectrum_menu.setIcon(icons.icon("settings", self.theme["text_muted"], 14))
        a11y.describe(self._btn_spectrum_menu, "Analyser options",
                       "Choose the analyser's peak hold, and whether it shows the "
                       "output, the input, or both.")
        self._btn_spectrum_menu.clicked.connect(self._show_spectrum_menu)

        self._act_peak_hold = QAction("Peak hold", self)
        self._act_peak_hold.setCheckable(True)
        self._act_peak_hold.toggled.connect(self._on_peak_hold_toggled)

        # Show: Output (post-EQ) / Input (pre-EQ, estimated) / Both (overlay for
        # live before/after comparison).
        self._act_mode_output = QAction("Output", self)
        self._act_mode_output.setCheckable(True)
        self._act_mode_output.setChecked(True)
        self._act_mode_output.triggered.connect(lambda: self._on_mode_changed("output"))
        self._act_mode_source = QAction("Input (estimated)", self)
        self._act_mode_source.setCheckable(True)
        self._act_mode_source.triggered.connect(lambda: self._on_mode_changed("source"))
        self._act_mode_both = QAction("Both (compare)", self)
        self._act_mode_both.setCheckable(True)
        self._act_mode_both.triggered.connect(lambda: self._on_mode_changed("both"))
        self._spectrum_mode_group = QActionGroup(self)
        self._spectrum_mode_group.setExclusive(True)
        for a in (self._act_mode_output, self._act_mode_source, self._act_mode_both):
            self._spectrum_mode_group.addAction(a)

        # In "both", swap which series is drawn in front (the other dims behind).
        self._act_swap_layers = QAction("Input in front", self)
        self._act_swap_layers.setCheckable(True)
        self._act_swap_layers.toggled.connect(self._on_swap_layers_toggled)

        self._spectrum_menu = QMenu(self)
        self._spectrum_menu.addAction(self._act_peak_hold)
        self._spectrum_menu.addSection("Show")
        self._spectrum_menu.addAction(self._act_mode_output)
        self._spectrum_menu.addAction(self._act_mode_source)
        self._spectrum_menu.addAction(self._act_mode_both)
        self._spectrum_menu.addAction(self._act_swap_layers)

        self._reposition_spectrum_controls()

    def take_spectrum_controls(self) -> tuple[QPushButton, QPushButton]:
        """Hand the Analyzer toggle and its gear button to a caller that will
        parent and lay them out itself, and stop hand-positioning them in the
        graph header. Returns (toggle, gear); all signals stay wired here."""
        self._spectrum_controls_detached = True
        return self._btn_live, self._btn_spectrum_menu

    def _reposition_spectrum_controls(self) -> None:
        """Right-align the Analyzer/gear buttons in the header strip, clear of
        the per-band frequency labels which grow from the left/plot area. A
        no-op once an owner has taken them (see take_spectrum_controls)."""
        if self._spectrum_controls_detached:
            return
        h = self._header.height()
        y = max(0, (h - 20) // 2)
        margin, gap = 6, 4
        x = self._header.width() - margin - self._btn_spectrum_menu.width()
        self._btn_spectrum_menu.move(x, y)
        x -= gap + self._btn_live.width()
        self._btn_live.move(x, y)

    def _show_spectrum_menu(self) -> None:
        pos = self._btn_spectrum_menu.mapToGlobal(self._btn_spectrum_menu.rect().bottomLeft())
        self._spectrum_menu.exec(pos)

    # ── live spectrum overlay (Phase 6) ─────────────────────────────

    def configure_spectrum_settings(self, enabled: bool = False, mode: str = "output",
                                     peak_hold: bool = False,
                                     compare_front: str = "output") -> None:
        """Apply persisted spectrum-overlay settings. EQGraph has no
        ProfileManager/settings reference of its own — the caller (currently
        main_window.py) reads the persisted `spectrum_*` keys and calls this once
        after construction, exactly the way `set_y_range()` is already driven by
        `graph_y_range`. Starts live capture immediately if `enabled` was
        persisted True."""
        self._spectrum_mode = mode if mode in ("output", "source", "both") else "output"
        self._spectrum_compare_front = "input" if compare_front == "input" else "output"
        self._spectrum_peak_hold = bool(peak_hold)

        self._btn_live.blockSignals(True)
        self._btn_live.setChecked(bool(enabled))
        self._btn_live.blockSignals(False)

        self._act_peak_hold.blockSignals(True)
        self._act_peak_hold.setChecked(self._spectrum_peak_hold)
        self._act_peak_hold.blockSignals(False)

        {"source": self._act_mode_source, "both": self._act_mode_both}.get(
            self._spectrum_mode, self._act_mode_output).setChecked(True)
        self._act_swap_layers.setEnabled(self._spectrum_mode == "both")
        self._act_swap_layers.blockSignals(True)
        self._act_swap_layers.setChecked(self._spectrum_compare_front == "input")
        self._act_swap_layers.blockSignals(False)

        self._apply_spectrum_enabled(bool(enabled))

    def stop_spectrum(self) -> None:
        """Unconditionally stop the underlying `SpectrumController` (its
        `LoopbackCapture` background thread + WASAPI loopback stream),
        regardless of the Analyzer toggle / `_spectrum_enabled` state.

        This is for tearing down THIS EQGraph instance itself -- not for
        the user turning the Analyzer off (that's `_apply_spectrum_enabled(False)`,
        which also updates curve visibility/settings and is wrong to reuse
        here). `MainWindow._apply_ui_size_change()`'s in-process window
        rebuild calls this on the outgoing EQGraph before discarding it, the
        same way it calls `self._auto_switch_adapter.close()` on the
        outgoing auto-switch worker -- otherwise the old capture thread
        (a daemon thread, only reaped at real process exit) survives into
        the new window's lifetime and the freshly-built EQGraph opens a
        SECOND loopback stream against the same device."""
        self._spectrum_ctrl.stop()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Auto-pause capture + FFT work while this graph isn't visible --
        design spec Sec11's "auto-pauses when the graph is hidden". Qt
        delivers hide events to child widgets whenever their EFFECTIVE
        visibility changes, including when a top-level ancestor window is
        hidden (e.g. MainWindow.closeEvent's tray-hide `self.hide()`), not
        only on a direct `self.hide()` call on this widget itself."""
        super().hideEvent(event)
        if self._spectrum_enabled:
            self._spectrum_ctrl.set_visible(False)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Resume capture (see `hideEvent` above) when this graph becomes
        visible again -- including when a top-level ancestor window is
        restored from the tray (`showNormal()`). No-op unless the Analyzer is on:
        must not spuriously start capture the first time this widget is
        shown if the user never enabled the Analyzer."""
        super().showEvent(event)
        if self._spectrum_enabled:
            self._spectrum_ctrl.set_visible(True)

    def set_spectrum_device(self, pattern: str | None, *, has_loopback: bool = True) -> None:
        """Point the live capture at the endpoint this profile actually EQs.

        `pattern` is an APO `Device:` line value (e.g. "Speakers FIIO K13
        R2R") — exactly what the editor's device selector carries as its item
        data — and is passed through to `LoopbackCapture`, whose
        `_resolve_loopback_device()` matches it against each WASAPI loopback
        device's name ("<endpoint> [Loopback]") with the project's own
        `pattern_matches_text`. "all" and `None` both mean "follow the system
        default output".

        Without this the capture was ALWAYS started with no fragment, i.e.
        `pa.get_default_wasapi_loopback()` — the system default output. If the
        profile's device wasn't also the Windows default (very common: a
        virtual endpoint like a Voicemod/NVIDIA Broadcast device grabs
        default, or you simply EQ a device you don't currently play to), the
        capture read a healthy, permanently-digital-silent stream from the
        WRONG endpoint. Result: "silent" forever, and the overlay hint fired
        even with music playing on the device being edited.

        `has_loopback=False` marks an endpoint that has no WASAPI loopback tap
        to capture at all — in practice a CAPTURE (microphone) endpoint, which
        `list_apo_devices()` happily returns alongside render ones whenever APO
        is hooked on a mic (the Microphone built-in profiles exist precisely to
        be used that way). A loopback tap only exists for RENDER endpoints, so
        passing a mic's pattern to the resolver finds nothing, fails the open,
        and reports "unavailable" — i.e. the exclusive/ASIO hint, blaming the
        user for a device that simply has no tap. Instead we fall back to the
        system default output's loopback.

        That fallback means the trace no longer corresponds to the profile
        being edited, so drift detection is switched OFF for its duration:
        `drift_correlation()` would be comparing the SPEAKERS' measured
        spectrum against a MICROPHONE's calculated curve, which will never
        correlate, and the "measured doesn't track calculated" flag would
        latch on permanently and falsely report that the EQ isn't working.
        `set_filters()` honours the same flag when it refreshes the calculated
        response (it runs after this, on the editor's shared sync path).

        Restarts capture only when the target actually changes — this is
        called from the editor's central graph-sync path, which runs on every
        recompute, and reopening the stream each time would thrash it.
        """
        fragment = (pattern or "").strip() or None
        if fragment is not None and fragment.lower() == "all":
            fragment = None
        fallback = fragment is not None and not has_loopback
        if fallback:
            fragment = None  # the default output's loopback, not the mic
        if fragment == self._spectrum_device and fallback == self._spectrum_is_fallback:
            return
        self._spectrum_device = fragment
        self._spectrum_is_fallback = fallback
        if fallback:
            self._spectrum_ctrl.set_calculated_response(None)
            self.measured_drift_changed.emit(False)
        if self._spectrum_enabled:
            self._spectrum_ctrl.start(fragment)

    def _update_spectrum_visibility(self) -> None:
        """Show exactly the items the current (enabled, mode, peak-hold)
        combination calls for. The OUTPUT and INPUT series show per the
        output/input/both mode."""
        on = self._spectrum_enabled
        out = on and self._spectrum_mode in ("output", "both")
        inp = on and self._spectrum_mode in ("source", "both")
        ph = self._spectrum_peak_hold
        self._spectrum_curve.setVisible(out)
        self._spectrum_peak_curve.setVisible(out and ph)
        self._spectrum_in_curve.setVisible(inp)
        self._spectrum_in_peak_curve.setVisible(inp and ph)
        if not on:
            self._spectrum_hint.setVisible(False)

    def _apply_spectrum_enabled(self, enabled: bool) -> None:
        self._spectrum_enabled = enabled
        self._update_spectrum_visibility()
        if enabled:
            self._spectrum_ctrl.start(self._spectrum_device)
        else:
            self._spectrum_ctrl.stop()
            # Belt-and-suspenders alongside `_spectrum_hint.setVisible(False)`
            # just above: `SpectrumController.stop()` now resets+emits its
            # own sustained clipping flag (see its docstring), so this is
            # normally a harmless duplicate emit of `False`. It's forced
            # unconditionally here anyway, the same way the hint's
            # visibility is forced regardless of any signal from the
            # controller, so the headroom indicator can never end up
            # stuck on a stale clipping-active state just because the Analyzer was
            # turned off -- this doesn't depend on the controller's own
            # bookkeeping being correct.
            self.measured_clipping_changed.emit(False)

    def _emit_spectrum_settings_changed(self) -> None:
        self.spectrum_settings_changed.emit({
            "spectrum_enabled": self._spectrum_enabled,
            "spectrum_mode": self._spectrum_mode,
            "spectrum_peak_hold": self._spectrum_peak_hold,
            "spectrum_compare_front": self._spectrum_compare_front,
        })

    def _on_live_toggled(self, checked: bool) -> None:
        self._apply_spectrum_enabled(checked)
        self._emit_spectrum_settings_changed()

    def _on_peak_hold_toggled(self, checked: bool) -> None:
        self._spectrum_peak_hold = checked
        self._update_spectrum_visibility()
        self._emit_spectrum_settings_changed()

    def _on_mode_changed(self, mode: str) -> None:
        if mode == self._spectrum_mode:
            return
        self._spectrum_mode = mode
        self._act_swap_layers.setEnabled(mode == "both")
        self._update_spectrum_visibility()
        self._emit_spectrum_settings_changed()

    def _on_swap_layers_toggled(self, input_in_front: bool) -> None:
        self._spectrum_compare_front = "input" if input_in_front else "output"
        self._emit_spectrum_settings_changed()

    def _input_of(self, series: np.ndarray) -> np.ndarray | None:
        """The pre-EQ (source-estimated) counterpart of a captured series:
        capture minus the EQ's own response. None when no response is known
        (e.g. a mic-fallback device), so the input series can't be estimated."""
        if self._spectrum_response_db is None:
            return None
        return subtract_response(series, self._spectrum_response_db)

    def _on_spectrum_ready(self, spectrum: np.ndarray) -> None:
        if not self._spectrum_enabled:
            return
        # Independent of `_spectrum_mode` (the GRAPH's own Output/Input/Both
        # trace selector, below) -- the meter panel always wants both, so
        # this offset is refreshed every tick regardless of which trace(s)
        # the graph itself is currently drawing.
        self._last_input_offset_db = (
            None if self._spectrum_response_db is None
            else broadband_offset_db(spectrum, self._spectrum_response_db))
        want_out = self._spectrum_mode in ("output", "both")
        want_in = self._spectrum_mode in ("source", "both")
        out_data = spectrum if want_out else None            # raw capture = post-EQ
        in_data = self._input_of(spectrum) if want_in else None
        log_freqs = np.log10(eq_math.frequency_array())
        lim = self._y_limit
        if out_data is not None:
            ref = float(np.mean(out_data))  # zero-reference this frame's own average
            self._spectrum_curve.setData(log_freqs, np.clip(out_data - ref, -lim, lim))
            if self._spectrum_peak_hold:
                self._spectrum_peak_curve.setData(
                    log_freqs, np.clip(self._spectrum_ctrl.peak - ref, -lim, lim))
        if in_data is not None:
            ref = float(np.mean(in_data))
            self._spectrum_in_curve.setData(log_freqs, np.clip(in_data - ref, -lim, lim))
            if self._spectrum_peak_hold:
                ip = self._input_of(self._spectrum_ctrl.peak)
                if ip is not None:
                    self._spectrum_in_peak_curve.setData(log_freqs, np.clip(ip - ref, -lim, lim))

    def _on_spectrum_stereo_level(self, levels: StereoLevels) -> None:
        """`SpectrumController.stereo_level_ready` handler: re-exposes the
        measured OUTPUT levels unchanged, and synthesizes an ESTIMATED INPUT
        reading by applying `_last_input_offset_db` (computed moments earlier
        this same tick, in `_on_spectrum_ready()`) to all four numbers.
        Applying one broadband offset to both channels equally assumes the
        EQ's spectral shape doesn't differ meaningfully between L and R --
        true for the vast majority of profiles (channel-specific EQ is the
        exception), and unlike attempting a true per-channel estimate (which
        would need a second FFT per channel per tick), this reuses the
        single mono analysis already being done for the graph at no extra
        capture/FFT cost."""
        self.stereo_level_ready.emit(levels)
        offset = self._last_input_offset_db
        if offset is None:
            self.estimated_input_level_ready.emit(None)
            return
        self.estimated_input_level_ready.emit(StereoLevels(
            peak_l=levels.peak_l + offset, peak_r=levels.peak_r + offset,
            rms_l=levels.rms_l + offset, rms_r=levels.rms_r + offset,
        ))

    def _on_spectrum_status(self, status: str) -> None:
        if not self._spectrum_enabled or status != "unavailable":
            self._spectrum_hint.setVisible(False)
            return
        self._spectrum_hint.setText(_SPECTRUM_HINT_UNAVAILABLE)
        self._spectrum_hint.setVisible(True)

    def _on_spectrum_clipping(self, is_clipping: bool) -> None:
        # Re-exposed regardless of `_spectrum_enabled`/Analyzer-toggle state --
        # unlike the graph overlay curve, the headroom indicator this feeds
        # (editor_panel.py) is always visible, so a caller only starts the
        # controller (via configure_spectrum_settings()/the Analyzer toggle) if
        # it actually wants measured clip evidence at all.
        self.measured_clipping_changed.emit(is_clipping)

    def _on_spectrum_drift(self, is_drifting: bool) -> None:
        # Same re-exposure contract as _on_spectrum_clipping() above.
        self.measured_drift_changed.emit(is_drifting)

    # ── theming ──────────────────────────────────────────────────────

    def _apply_header_style(self) -> None:
        t = self.theme
        self._header.setStyleSheet(
            f"background: {t['bg_raised']};"
            f"border-bottom: 1px solid {t['border_default']};"
        )

    def _apply_theme_colors(self) -> None:
        t = self.theme
        self._apply_header_style()
        self.plot.setBackground(_qcolor(t["graph_bg"]))
        plot_item = self.plot.getPlotItem()

        # dB / Hz scale ticks are numeric readouts, so they take the mono face
        # to match the band header labels and the spinbox value fields.
        tick_font = fonts.mono_font()
        tick_font.setPointSize(8)
        for axis_name in ("bottom", "left"):
            axis = plot_item.getAxis(axis_name)
            axis.setPen(pg.mkPen(_qcolor(t["graph_text"])))
            axis.setTextPen(pg.mkPen(_qcolor(t["graph_text"])))
            axis.setStyle(tickFont=tick_font)

        if self._zero_line is not None:
            plot_item.removeItem(self._zero_line)

        zero_pen = pg.mkPen(_qcolor(t["graph_zero"]), width=1, style=Qt.PenStyle.DashLine)
        self._zero_line = pg.InfiniteLine(pos=0.0, angle=0, pen=zero_pen)
        self._zero_line.setZValue(-10)
        plot_item.addItem(self._zero_line)

        points_color = t.get("graph_points", t["graph_band_sel"])
        self._glow_curve.setPen(pg.mkPen(_qcolor(t["accent_glow"]), width=8))
        self._main_curve.setPen(pg.mkPen(_qcolor(t["graph_curve"]), width=2.5))
        self._band_curve.setPen(pg.mkPen(_qcolor(t["graph_band_sel"]), width=1.5))
        self._points_curve.setPen(pg.mkPen(_alpha(points_color, 180), width=1.5,
                                            style=Qt.PenStyle.DashLine))
        geq_color = t.get("graph_geq", t["graph_band_sel"])
        self._geq_curve.setPen(pg.mkPen(_alpha(geq_color, 190), width=1.5,
                                         style=Qt.PenStyle.DotLine))
        ir_color = t.get("graph_ir", t.get("graph_points", t["graph_band_sel"]))
        self._ir_curve.setPen(pg.mkPen(_alpha(ir_color, 170), width=1.5,
                                        style=Qt.PenStyle.DashLine))
        accent_color = t.get("accent", "#4DA3FF")
        self._listener_l_curve.setPen(pg.mkPen(_alpha(accent_color, 200), width=1.5,
                                                style=Qt.PenStyle.DashDotLine))
        self._listener_r_curve.setPen(pg.mkPen(_alpha(accent_color, 120), width=1.5,
                                                style=Qt.PenStyle.DashDotLine))
        self._clip_region.setBrush(pg.mkBrush(_alpha(t["error"], 26)))

        # Phase 6 live spectrum overlay: translucent accent fill (BEHIND the
        # response curves — see the z-values set in _build_curves) plus a
        # thin undamped peak-hold trace.
        accent = t.get("accent", "#4DA3FF")
        # OUTPUT trace: warm — the theme "success" (green).
        # The waveform is zero-referenced (its own per-frame average sits on the
        # 0 line), so it fills to 0: bands hotter than average bump UP off the
        # centre line, quieter ones dip DOWN below it — reading directly against
        # the EQ curve, which also lives around 0.
        out_col = t.get("success", accent)
        self._spectrum_curve.setPen(pg.mkPen(_alpha(out_col, 200), width=1.5))
        self._spectrum_curve.setFillLevel(0.0)
        self._spectrum_curve.setFillBrush(pg.mkBrush(_alpha(out_col, 55)))
        self._spectrum_peak_curve.setPen(pg.mkPen(_alpha(out_col, 230), width=1,
                                                   style=Qt.PenStyle.DotLine))
        # INPUT trace: cool — the theme accent, contrasting with the warm output.
        self._spectrum_in_curve.setPen(pg.mkPen(_alpha(accent, 200), width=1.5))
        self._spectrum_in_curve.setFillLevel(0.0)
        self._spectrum_in_curve.setFillBrush(pg.mkBrush(_alpha(accent, 45)))
        self._spectrum_in_peak_curve.setPen(pg.mkPen(_alpha(accent, 230), width=1,
                                                      style=Qt.PenStyle.DotLine))
        self._spectrum_hint.setColor(_qcolor(t["graph_text"]))

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self._apply_theme_colors()
        self._rebuild_handles()
        self._update_points_curve(self._filters)

    # ── data binding ─────────────────────────────────────────────────

    def set_filters(self, filters: list[Filter], sample_rate: int = eq_math.DEFAULT_SAMPLE_RATE) -> None:
        self._filters = filters
        self._sample_rate = sample_rate
        self._update_response_curves()
        self._update_points_curve(filters)
        self._rebuild_handles()
        self._update_band_curve()

    def set_graphic_eq(self, geq_string: str) -> None:
        """Set the section's GraphicEQ string ('' when unused) so the main
        curve shows the completed result and the GEQ contribution is drawn."""
        if geq_string == self._geq_string:
            return
        self._geq_string = geq_string
        self._update_response_curves()

    def set_ir_response(self, response_db: np.ndarray | None) -> None:
        """Set the convolution IR's response (dB on the frequency_array grid,
        None when no/unreadable IR) so the main curve shows the completed
        result and the IR contribution is drawn dashed."""
        if response_db is None and self._ir_response is None:
            return
        self._ir_response = response_db
        self._update_response_curves()

    def set_listener_correction(self, left_db: "np.ndarray | None",
                                 right_db: "np.ndarray | None") -> None:
        """Set the active Listener Slot's per-ear correction (dB on the
        frequency_array grid, both None when flat/no active correction) so
        the graph can show L-effective/R-effective overlay curves without
        implying the profile itself becomes unlinked for editing purposes
        (design spec §5.1)."""
        if left_db is None and right_db is None \
                and self._listener_left_db is None and self._listener_right_db is None:
            return
        self._listener_left_db = left_db
        self._listener_right_db = right_db
        self._update_response_curves()

    def _update_response_curves(self) -> None:
        freqs = eq_math.frequency_array()
        log_freqs = np.log10(freqs)
        combined = eq_math.combined_response(self._filters, freqs, self._sample_rate)

        if self._geq_string:
            geq = eq_math.graphic_eq_response(self._geq_string, freqs)
            combined = combined + geq
            self._geq_curve.setData(log_freqs, geq)
            self._geq_curve.setVisible(True)
        else:
            self._geq_curve.setVisible(False)

        if self._ir_response is not None:
            combined = combined + self._ir_response
            self._ir_curve.setData(log_freqs, self._ir_response)
            self._ir_curve.setVisible(True)
        else:
            self._ir_curve.setVisible(False)

        if self._listener_left_db is not None or self._listener_right_db is not None:
            left = combined + (self._listener_left_db if self._listener_left_db is not None
                               else np.zeros_like(combined))
            right = combined + (self._listener_right_db if self._listener_right_db is not None
                                else np.zeros_like(combined))
            self._listener_l_curve.setData(log_freqs, left)
            self._listener_r_curve.setData(log_freqs, right)
            self._listener_l_curve.setVisible(True)
            self._listener_r_curve.setVisible(True)
        else:
            self._listener_l_curve.setVisible(False)
            self._listener_r_curve.setVisible(False)

        self._glow_curve.setData(log_freqs, combined)
        self._main_curve.setData(log_freqs, combined)

        # Phase 6 "Source (estimated)" mode subtracts exactly this rendered
        # response (parametric bands + GraphicEQ + convolution IR, whichever
        # apply) from the live spectrum — see _on_spectrum_ready().
        self._spectrum_response_db = combined
        # Rumble 3.7: the drift detector compares the measured spectrum
        # against this same rendered response (not the per-ear listener
        # overlay, which is display-only and never reflects a single
        # measurable output — see set_listener_correction()'s docstring).
        # Suppressed entirely while the capture is on a fallback device: the
        # measured trace is then the default output's, not this profile's
        # endpoint, so correlating the two is meaningless and would latch the
        # drift flag on forever — see set_spectrum_device().
        self._spectrum_ctrl.set_calculated_response(
            None if self._spectrum_is_fallback else combined)

        peak = float(np.max(combined)) if combined.size else 0.0
        self._clip_region.setVisible(peak > 0)

    def highlight_band(self, index: int | None) -> None:
        self._highlighted_index = index
        self._update_band_curve()

    def _update_band_curve(self) -> None:
        if self._highlighted_index is None or not (0 <= self._highlighted_index < len(self._filters)):
            self._band_curve.setVisible(False)
            return
        freqs = eq_math.frequency_array()
        response = eq_math.filter_response(self._filters[self._highlighted_index], freqs)
        self._band_curve.setData(np.log10(freqs), response)
        self._band_curve.setVisible(True)

    def _update_points_curve(self, filters: list[Filter]) -> None:
        """Dashed straight-line connector through each active band's gain
        value, sorted by frequency. Held flat (zero-order-hold) out to the
        graph edges so it always reads as one continuous line rather than a
        stub floating mid-graph — the outermost band's own gain is what's
        held, never an invented value, so it stays truthful past the last
        real band. Each real inter-band segment is labelled with the gain
        delta between its two bands: the one thing this simplified,
        non-physical connector can show at a glance that the true summed
        curve (bent per-band by Q/bandwidth) can't."""
        plot_item = self.plot.getPlotItem()
        for label in self._delta_labels:
            plot_item.removeItem(label)
        self._delta_labels = []

        pts = sorted(
            [(f.fc, f.gain) for f in filters if f.enabled and f.supports_gain() and f.type != "IIR"],
            key=lambda p: p[0],
        )
        if len(pts) < 2:
            self._points_curve.setData([], [])
            return

        xs = [float(np.log10(max(p[0], 1.0))) for p in pts]
        ys = [p[1] for p in pts]

        t = self.theme
        color = _qcolor(t.get("graph_points", t["graph_band_sel"]))
        label_font = fonts.mono_font()
        label_font.setPointSize(8)
        for i in range(len(xs) - 1):
            delta = ys[i + 1] - ys[i]
            mid_x = (xs[i] + xs[i + 1]) / 2
            mid_y = (ys[i] + ys[i + 1]) / 2
            label = pg.TextItem(f"{delta:+.1f} dB", color=color, anchor=(0.5, 1.0))
            label.setFont(label_font)
            label.setPos(mid_x, mid_y)
            label.setZValue(3)
            plot_item.addItem(label)
            self._delta_labels.append(label)

        edge_lo = float(np.log10(X_TICKS_HZ[0]))
        edge_hi = float(np.log10(X_TICKS_HZ[-1]))
        draw_xs, draw_ys = list(xs), list(ys)
        if draw_xs[0] > edge_lo:
            draw_xs.insert(0, edge_lo)
            draw_ys.insert(0, ys[0])
        if draw_xs[-1] < edge_hi:
            draw_xs.append(edge_hi)
            draw_ys.append(ys[-1])
        self._points_curve.setData(draw_xs, draw_ys)

    # ── draggable band handles ───────────────────────────────────────

    @staticmethod
    def _format_freq_short(hz: float) -> str:
        if hz >= 1000:
            v = hz / 1000
            s = f"{v:.3g}"
            return f"{s}k"
        return f"{hz:.4g}"

    def _rebuild_handles(self) -> None:
        if self._dragging:
            return
        plot_item = self.plot.getPlotItem()
        for handle in self._handles:
            plot_item.removeItem(handle)
        self._handles = []
        for lbl in self._header_labels:
            lbl.deleteLater()
        self._header_labels = []
        for line in self._band_lines:
            plot_item.removeItem(line)
        self._band_lines = []

        t = self.theme
        handle_brush = pg.mkBrush(_qcolor(t["graph_handle"]))
        handle_pen = pg.mkPen(_qcolor(t["bg_sunken"]), width=2)
        line_color = _alpha(t["graph_handle"], 60)

        for index, filt in enumerate(self._filters):
            if not filt.enabled or filt.type == "IIR":
                continue
            x = float(np.log10(max(filt.fc, 1.0)))
            if filt.supports_gain():
                # Pin to the plot edge if the gain exceeds the visible scale
                # (possible on ±15) so the handle stays grabbable.
                y = float(np.clip(filt.gain, -self._y_limit, self._y_limit))
            else:
                # Place handle on the actual curve so it doesn't float above it
                y = float(np.clip(
                    eq_math.filter_response(filt, np.array([filt.fc]))[0],
                    -self._y_limit, self._y_limit))

            # Vertical frequency marker line
            vline = pg.InfiniteLine(
                pos=x, angle=90, movable=False,
                pen=pg.mkPen(line_color, width=1, style=Qt.PenStyle.DashLine),
            )
            vline.setZValue(-5)
            plot_item.addItem(vline)
            self._band_lines.append(vline)

            # Header label (QLabel in the header strip, not inside the plot)
            lbl = QLabel(f"{index + 1}\n{self._format_freq_short(filt.fc)}", self._header)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"font-size: 9px; font-family: 'IBM Plex Mono', 'Consolas', monospace;"
                f"color: {t['text_muted']}; background: transparent; border: none;"
            )
            lbl._rumble_fc = filt.fc
            lbl.show()
            self._header_labels.append(lbl)

            handle = _BandHandle(pos=(x, y), size=10, movable=True,
                                  pen=handle_pen, brush=handle_brush, label=None)
            handle._rumble_index = index
            handle._rumble_locks_gain = not filt.supports_gain()
            handle.sigPositionChanged.connect(self._on_handle_moved)
            handle.clicked.connect(lambda _h, idx=index: self.band_selected.emit(idx))
            plot_item.addItem(handle)
            self._handles.append(handle)

        self._reposition_header_labels()

    def _reposition_header_labels(self) -> None:
        """Align header QLabels with their handle's x pixel position on the plot."""
        if not self._header_labels:
            return
        vb = self.plot.getPlotItem().getViewBox()
        lbl_w, h = 50, self._header.height()
        for lbl in self._header_labels:
            x_log = float(np.log10(max(lbl._rumble_fc, 1.0)))
            scene_pt = vb.mapViewToScene(pg.Point(x_log, 0))
            x_px = int(self.plot.mapFromScene(scene_pt).x())
            lbl.setGeometry(x_px - lbl_w // 2, 0, lbl_w, h)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_y_ticks()
        self._reposition_header_labels()
        self._reposition_spectrum_controls()

    def _on_handle_moved(self, handle: pg.TargetItem) -> None:
        if self._suppress_handle_events:
            return
        index = handle._rumble_index
        if index >= len(self._filters):
            return
        pos = handle.pos()
        y = float(pos.y())

        # Frequency is manual-entry only — lock x to the filter's actual fc
        locked_x = float(np.log10(max(self._filters[index].fc, 1.0)))
        self._suppress_handle_events = True
        handle.setPos((locked_x, y))
        self._suppress_handle_events = False

        fc = self._filters[index].fc

        if handle._rumble_locks_gain:
            curve_y = float(np.clip(
                eq_math.filter_response(self._filters[index], np.array([self._filters[index].fc]))[0],
                -self._y_limit, self._y_limit))
            self._suppress_handle_events = True
            handle.setPos((locked_x, curve_y))
            self._suppress_handle_events = False
            gain = self._filters[index].gain
        else:
            gain = float(np.clip(y, -self._y_limit, self._y_limit))

        self._dragging = True
        self.band_dragged.emit(index, fc, gain)
        self._dragging = False

    # ── keyboard operability & accessibility ─────────────────────────

    def _navigable_indices(self) -> list[int]:
        """Indices of bands that have a handle — the ones a user can select
        (enabled, non-IIR), matching the handles built in _rebuild_handles."""
        return [i for i, f in enumerate(self._filters)
                if f.enabled and f.type != "IIR"]

    def _current_band_index(self) -> int | None:
        """The band the keyboard acts on: the highlighted one if it is
        navigable, else the first navigable band, else None."""
        nav = self._navigable_indices()
        if not nav:
            return None
        if self._highlighted_index in nav:
            return self._highlighted_index
        return nav[0]

    def _band_value_text(self, index: int | None = None) -> str:
        """Spoken/queried description of the selected band's state."""
        if index is None:
            index = self._current_band_index()
        if index is None or not (0 <= index < len(self._filters)):
            return "No band selected"
        filt = self._filters[index]
        freq = self._format_freq_short(filt.fc)
        if filt.supports_gain():
            return f"Band {index + 1}, {freq} hertz, {filt.gain:+.1f} decibels"
        return f"Band {index + 1}, {freq} hertz, {filt.type}"

    def _announce_band(self, index: int | None = None) -> None:
        a11y.announce(self, self._band_value_text(index), assertive=True)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        # On first focus, point the keyboard at a band so arrows have a target.
        if self._highlighted_index is None:
            nav = self._navigable_indices()
            if nav:
                self.band_selected.emit(nav[0])
        self._announce_band()
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def keyPressEvent(self, event) -> None:
        nav = self._navigable_indices()
        if not nav:
            super().keyPressEvent(event)
            return

        key = event.key()
        cur = self._current_band_index()

        # Left/Right — move the selection between bands.
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            pos = nav.index(cur)
            pos += -1 if key == Qt.Key.Key_Left else 1
            pos = max(0, min(len(nav) - 1, pos))
            new_index = nav[pos]
            self.band_selected.emit(new_index)
            self._announce_band(new_index)
            event.accept()
            return

        # Up/Down/PageUp/PageDown/Home/End — adjust the selected band's gain.
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp,
                   Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End):
            filt = self._filters[cur]
            if not filt.supports_gain():
                self._announce_band(cur)  # nothing to change — re-state the band
                event.accept()
                return
            fine = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            step = 0.1 if fine else 0.5
            if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                step = 3.0
            if key in (Qt.Key.Key_Up, Qt.Key.Key_PageUp):
                gain = filt.gain + step
            elif key in (Qt.Key.Key_Down, Qt.Key.Key_PageDown):
                gain = filt.gain - step
            elif key == Qt.Key.Key_Home:
                gain = -self._y_limit
            else:  # End
                gain = self._y_limit
            gain = round(float(np.clip(gain, -self._y_limit, self._y_limit)), 2)
            # Same path a mouse drag uses — the editor writes the spinbox and
            # mutates this filter in place, so the next keypress sees the update.
            self.band_dragged.emit(cur, filt.fc, gain)
            self._announce_band(cur)
            event.accept()
            return

        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.hasFocus():
            return
        painter = QPainter(self)
        try:
            pen = QPen(_qcolor(self.theme.get("accent", "#4DA3FF")))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 4, 4)
        finally:
            painter.end()


# ── screen-reader interface (best-effort) ────────────────────────────
# Expose the graph to Windows UI Automation as a slider whose value is the
# selected band's state, so NVDA/JAWS read it. The keyboard handlers above also
# fire announcement events on every change, so the app stays usable even if this
# factory registration is unavailable on a given PySide6 build.
_ACCESSIBLE_KEEP: list = []  # hold references so Qt doesn't GC the interfaces


def install_accessibility() -> None:
    """Register a QAccessible factory for EQGraph. Call once at startup after
    the QApplication exists. Safe to call on builds where this is unsupported."""
    try:
        from PySide6.QtGui import QAccessible
        from PySide6.QtWidgets import QAccessibleWidget

        class _EQGraphAccessible(QAccessibleWidget):
            def __init__(self, widget: QWidget) -> None:
                super().__init__(widget, QAccessible.Role.Slider)

            def text(self, t):  # noqa: ANN001 - Qt override
                if t == QAccessible.Text.Value:
                    return self.widget()._band_value_text()
                return super().text(t)

        def _factory(key, obj):  # noqa: ANN001 - Qt override
            if isinstance(obj, EQGraph):
                iface = _EQGraphAccessible(obj)
                _ACCESSIBLE_KEEP.append(iface)
                return iface
            return None

        QAccessible.installFactory(_factory)
    except Exception:
        pass
