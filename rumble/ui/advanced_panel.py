"""Tabbed advanced panel: Graphic EQ, Processing, Routing, Scripting."""
from __future__ import annotations

import re

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from rumble.core.apo_config import LoudnessCorrection, VSTPlugin
from rumble.ui import icons
from rumble.ui.a11y import describe
from rumble.ui.density import dp
from rumble.ui.eq_graph import LogFreqAxis, _qcolor
from rumble.ui.frameless import scrollable_page
from rumble.ui.raw_config_view import ConfigHighlighter

# ─────────────────────────────────────────────────────────────────────────
# GraphicEQ point editor
# ─────────────────────────────────────────────────────────────────────────

_DEFAULT_GEQ_POINTS = [(25.0, 0.0), (100.0, 0.0), (1000.0, 0.0), (10000.0, 0.0), (16000.0, 0.0)]

# Standard ISO fixed-band graphic-EQ centre frequencies (1-oct, 2/3-oct and
# 1/3-oct), matching Equalizer APO's own 15-band and 31-band graphic EQ
# presets. ISO_10_BAND is the ISO 266 preferred full-octave series (each
# centre double the last, 31.5 Hz -> 16 kHz) — the classic 10-band consumer
# graphic-EQ layout (Winamp/foobar2000 10-band EQ use the same ten centres).
ISO_10_BAND = [31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0,
               8000.0, 16000.0]
ISO_15_BAND = [25.0, 40.0, 63.0, 100.0, 160.0, 250.0, 400.0, 630.0, 1000.0,
               1600.0, 2500.0, 4000.0, 6300.0, 10000.0, 16000.0]
ISO_31_BAND = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0,
               200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0, 1250.0,
               1600.0, 2000.0, 2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0,
               10000.0, 12500.0, 16000.0, 20000.0]


def _format_geq_string(points: list[tuple[float, float]]) -> str:
    ordered = sorted(points, key=lambda p: p[0])
    return "; ".join(f"{freq:g} {gain:g}" for freq, gain in ordered)


def _parse_geq_string(text: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for chunk in text.split(";"):
        parts = chunk.strip().split()
        if len(parts) >= 2:
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return points or list(_DEFAULT_GEQ_POINTS)


class GraphicEQEditor(QWidget):
    """Draggable-point editor producing a `GraphicEQ:` value string."""

    changed = Signal()
    convert_to_peq_requested = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self._points: list[list[float]] = [list(p) for p in _DEFAULT_GEQ_POINTS]
        self._handles: list[pg.TargetItem] = []
        self._suppress = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        hint = QLabel("Drag points to shape the curve. Values interpolate between points; outside the range the response is flat.")
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.plot = pg.PlotWidget(axisItems={"bottom": LogFreqAxis(orientation="bottom")})
        self.plot.setFixedHeight(dp(340, 250))
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        plot_item = self.plot.getPlotItem()
        plot_item.setContentsMargins(8, 8, 8, 8)
        self.plot.setXRange(np.log10(20), np.log10(20000), padding=0.02)
        self.plot.setYRange(-24, 24, padding=0.05)
        layout.addWidget(self.plot)

        self._curve = plot_item.plot([], [])
        self._curve.setZValue(1)

        button_row = QHBoxLayout()
        self.btn_add = QPushButton(" Add Point")
        self.btn_add.setIcon(icons.icon("add", theme["text_muted"], 14))
        self.btn_add.setProperty("class", "ghost")
        describe(self.btn_add, "Add Graphic EQ point",
                 "Add another draggable point to the curve for finer control.")
        self.btn_remove = QPushButton(" Remove Last")
        self.btn_remove.setIcon(icons.icon("delete", theme["text_muted"], 14))
        self.btn_remove.setProperty("class", "ghost")
        describe(self.btn_remove, "Remove last point",
                 "Remove the highest-frequency point from the curve "
                 "(at least two points always remain).")
        self.btn_reset = QPushButton(" Reset")
        self.btn_reset.setProperty("class", "ghost")
        describe(self.btn_reset, "Reset Graphic EQ",
                 "Flatten the curve back to 0 dB — the Graphic EQ then has no "
                 "effect and is left out of the saved profile.")
        self.btn_iso10 = QPushButton(" 10-Band")
        self.btn_iso10.setProperty("class", "ghost")
        describe(self.btn_iso10, "10-band ISO scaffold",
                 "Re-lay the curve on the standard 10 ISO octave-band centres "
                 "(31.5 Hz – 16 kHz, 1 octave). The current shape is kept — "
                 "each new point takes the curve's value at its frequency.")
        self.btn_iso15 = QPushButton(" 15-Band")
        self.btn_iso15.setProperty("class", "ghost")
        describe(self.btn_iso15, "15-band ISO scaffold",
                 "Re-lay the curve on the standard 15 ISO band centres "
                 "(25 Hz – 16 kHz, ⅔ octave). The current shape is kept — "
                 "each new point takes the curve's value at its frequency.")
        self.btn_iso31 = QPushButton(" 31-Band")
        self.btn_iso31.setProperty("class", "ghost")
        describe(self.btn_iso31, "31-band ISO scaffold",
                 "Re-lay the curve on the standard 31 ISO band centres "
                 "(20 Hz – 20 kHz, ⅓ octave). The current shape is kept — "
                 "each new point takes the curve's value at its frequency.")
        self.btn_convert_peq = QPushButton(" Convert to PEQ")
        self.btn_convert_peq.setIcon(icons.icon("tune", theme["text_muted"], 14))
        self.btn_convert_peq.setProperty("class", "ghost")
        describe(self.btn_convert_peq, "Convert Graphic EQ to parametric bands",
                 "Replace this curve with an equivalent set of PK filter bands "
                 "on the Bands tab, sized from this curve's own point spacing. "
                 "This is an approximation, not an exact match — asks for "
                 "confirmation before replacing anything.")
        button_row.addWidget(self.btn_add)
        button_row.addWidget(self.btn_remove)
        button_row.addWidget(self.btn_reset)
        button_row.addWidget(self.btn_iso10)
        button_row.addWidget(self.btn_iso15)
        button_row.addWidget(self.btn_iso31)
        button_row.addWidget(self.btn_convert_peq)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.lbl_string = QLabel()
        self.lbl_string.setProperty("class", "mono")
        self.lbl_string.setWordWrap(True)
        describe(self.lbl_string, "Graphic EQ text",
                 "The exact GraphicEQ line that will be written into the "
                 "Equalizer APO configuration for this curve.")
        layout.addWidget(self.lbl_string)

        describe(self.plot, "Graphic EQ curve",
                 "A freeform tone curve applied on top of the filter bands. "
                 "Drag the points up or down to boost or cut; the curve blends "
                 "smoothly between points.")

        self.btn_add.clicked.connect(self._add_point)
        self.btn_remove.clicked.connect(self._remove_last_point)
        self.btn_reset.clicked.connect(self._reset_points)
        self.btn_iso10.clicked.connect(lambda: self._apply_iso_scaffold(ISO_10_BAND))
        self.btn_iso15.clicked.connect(lambda: self._apply_iso_scaffold(ISO_15_BAND))
        self.btn_iso31.clicked.connect(lambda: self._apply_iso_scaffold(ISO_31_BAND))
        self.btn_convert_peq.clicked.connect(self.convert_to_peq_requested.emit)

        self._apply_theme_colors()
        self._rebuild()

    def _apply_theme_colors(self) -> None:
        t = self.theme
        self.plot.setBackground(_qcolor(t["graph_bg"]))
        plot_item = self.plot.getPlotItem()
        for axis_name in ("bottom", "left"):
            axis = plot_item.getAxis(axis_name)
            axis.setPen(pg.mkPen(_qcolor(t["graph_text"])))
            axis.setTextPen(pg.mkPen(_qcolor(t["graph_text"])))
        self._curve.setPen(pg.mkPen(_qcolor(t["graph_curve"]), width=2))

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self._apply_theme_colors()
        self.btn_add.setIcon(icons.icon("add", theme["text_muted"], 14))
        self.btn_remove.setIcon(icons.icon("delete", theme["text_muted"], 14))
        self.btn_convert_peq.setIcon(icons.icon("tune", theme["text_muted"], 14))
        self._rebuild()

    def _add_point(self) -> None:
        ordered = sorted(self._points, key=lambda p: p[0])
        if len(ordered) >= 2:
            f0, f1 = ordered[-2][0], ordered[-1][0]
            new_freq = min((f0 * f1) ** 0.5, 19000.0)
        else:
            new_freq = 1000.0
        self._points.append([new_freq, 0.0])
        self._rebuild()
        self._emit_changed()

    def _remove_last_point(self) -> None:
        if len(self._points) > 2:
            ordered = sorted(self._points, key=lambda p: p[0])
            self._points.remove(ordered[-1])
            self._rebuild()
            self._emit_changed()

    def _reset_points(self) -> None:
        self._points = [list(p) for p in _DEFAULT_GEQ_POINTS]
        self._rebuild()
        self._emit_changed()

    def _apply_iso_scaffold(self, centres: list[float]) -> None:
        """Re-lay the points on standard ISO band centres, resampling the
        current curve so its shape is preserved (a flat curve stays flat) —
        no destructive reset, just a denser/standard grid to work on."""
        from rumble.core import autoeq_merge
        curve = sorted((p[0], p[1]) for p in self._points)
        gains = autoeq_merge.interp_log(curve, centres)
        self._points = [[f, g] for f, g in zip(centres, gains)]
        self._rebuild()
        self._emit_changed()

    def set_points_from_string(self, text: str) -> None:
        # try/finally so a parse/rebuild error can't leave _suppress stuck True,
        # which would silently kill handle dragging and the `changed` signal.
        self._suppress = True
        try:
            self._points = [list(p) for p in _parse_geq_string(text)]
            self._rebuild()
        finally:
            self._suppress = False

    def to_string(self) -> str:
        return _format_geq_string([(p[0], p[1]) for p in self._points])

    def to_save_string(self) -> str:
        """Returns '' if all gains are 0 dB (flat/no-op), otherwise the formatted string."""
        if all(abs(p[1]) < 0.001 for p in self._points):
            return ""
        return self.to_string()

    def _rebuild(self) -> None:
        plot_item = self.plot.getPlotItem()
        for handle in self._handles:
            plot_item.removeItem(handle)
        self._handles = []

        ordered = sorted(self._points, key=lambda p: p[0])
        if ordered:
            log_freqs = np.log10(np.clip([p[0] for p in ordered], 1.0, 96000.0))
            gains = [p[1] for p in ordered]
            self._curve.setData(log_freqs, gains)

        t = self.theme
        brush = pg.mkBrush(_qcolor(t["graph_handle"]))
        pen = pg.mkPen(_qcolor(t["bg_sunken"]), width=2)
        for point in self._points:
            x = float(np.log10(max(point[0], 1.0)))
            handle = pg.TargetItem(pos=(x, point[1]), size=10, movable=True,
                                   pen=pen, brush=brush, label=None)
            handle._rumble_point = point
            handle.sigPositionChanged.connect(self._on_handle_moved)
            plot_item.addItem(handle)
            self._handles.append(handle)

        self.lbl_string.setText(f"GraphicEQ: {self.to_string()}")

    def _on_handle_moved(self, handle: pg.TargetItem) -> None:
        if self._suppress:
            return
        pos = handle.pos()
        freq = float(np.clip(10 ** pos.x(), 1.0, 96000.0))
        gain = float(np.clip(pos.y(), -30.0, 30.0))
        point = handle._rumble_point
        point[0], point[1] = freq, gain

        ordered = sorted(self._points, key=lambda p: p[0])
        log_freqs = np.log10(np.clip([p[0] for p in ordered], 1.0, 96000.0))
        gains = [p[1] for p in ordered]
        self._curve.setData(log_freqs, gains)
        self.lbl_string.setText(f"GraphicEQ: {self.to_string()}")
        self._emit_changed()

    def _emit_changed(self) -> None:
        if not self._suppress:
            self.changed.emit()


# ─────────────────────────────────────────────────────────────────────────
# Convolution
# ─────────────────────────────────────────────────────────────────────────

class ConvolutionEditor(QWidget):
    changed = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("No impulse response file selected")
        describe(self.path_edit, "Impulse response file",
                 "Path to a WAV/FLAC impulse response that Equalizer APO convolves "
                 "with the audio — used for room correction or speaker simulation.")
        self.btn_browse = QPushButton(" Browse…")
        self.btn_browse.setIcon(icons.icon("folder_open", theme["text_muted"], 14))
        describe(self.btn_browse, "Browse for impulse response",
                 "Choose the impulse response audio file from disk.")
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.btn_browse)
        layout.addLayout(row)

        self._warning_default = ("Warning: The impulse response sample rate must match your audio "
                                 "device's output sample rate, or the convolution will sound "
                                 "incorrect.")
        self._warning_missing = ("Warning: Impulse response file not found or unreadable — the "
                                 "response preview and auto-preamp exclude it.")
        self.warning = QLabel(self._warning_default)
        self.warning.setProperty("class", "muted")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        self.btn_browse.clicked.connect(self._browse)
        self.path_edit.textChanged.connect(lambda _: self.changed.emit())

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Impulse Response File", "",
            "Impulse Response Files (*.wav *.flac *.ogg);;All Files (*)",
        )
        if path:
            self.path_edit.setText(path)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.btn_browse.setIcon(icons.icon("folder_open", theme["text_muted"], 14))

    def set_file_status(self, ok: bool, mismatch: str | None = None) -> None:
        """Update the hint label from the current IR analysis state.

        Three states: a missing/unreadable file (``ok=False``) always shows
        the missing-file warning regardless of *mismatch*; a readable file
        with a detected sample-rate mismatch (``ok=True``, *mismatch* set)
        shows that specific warning naming both rates
        (``ir_analysis.check_rate_mismatch``); a readable, matched (or
        rate-unknown) file (``ok=True``, *mismatch* None) falls back to the
        generic sample-rate hint."""
        if not ok:
            self.warning.setText(self._warning_missing)
        elif mismatch:
            self.warning.setText(f"Warning: {mismatch}")
        else:
            self.warning.setText(self._warning_default)

    def value(self) -> str:
        return self.path_edit.text().strip()

    def set_value(self, value: str) -> None:
        self.path_edit.blockSignals(True)
        self.path_edit.setText(value)
        self.path_edit.blockSignals(False)


# ─────────────────────────────────────────────────────────────────────────
# Loudness correction
# ─────────────────────────────────────────────────────────────────────────

def _fmt_db(value: float) -> str:
    return f"{value:g}"


class LoudnessEditor(QWidget):
    """`LoudnessCorrection:` block editor — a nullable, profile-level block.

    Unlike the sibling editors here (which always write their value), this
    one can produce *no* line at all: toggling off a profile that never had
    loudness correction leaves `loudness_correction` at None. The original
    `LoudnessCorrection` object (with any unrecognised keys, e.g.
    `Attenuation`) is kept as the backing store across a load — only the two
    known keys are ever written through `.set()`, so unknown tokens and
    their order always survive untouched.
    """

    changed = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self._lc: LoudnessCorrection | None = None
        self._loading = False
        # Track actual user interaction (not value comparison) so an
        # unedited load-then-rebuild never rewrites ReferenceLevel/
        # ReferenceOffset — value comparison is unreliable because the
        # spin boxes' decimals(1) can't represent a pre-existing value's
        # original precision, so "no visible change" doesn't mean "field
        # wasn't serialized" (see docs/superpowers/plans/phase1-errata.md #3).
        self._level_touched = False
        self._offset_touched = False
        self._state_touched = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top_row = QHBoxLayout()
        self.btn_toggle = QPushButton("Loudness Correction")
        self.btn_toggle.setCheckable(True)
        self.btn_toggle.setProperty("class", "ghost")
        self.btn_toggle.toggled.connect(self._on_toggled)
        describe(self.btn_toggle, "Loudness correction",
                 "Enable Equalizer APO's loudness-compensation curve, which "
                 "boosts bass and treble relative to the reference level as "
                 "playback volume drops below it — closer perceived tonal "
                 "balance at low listening volumes.")
        top_row.addWidget(self.btn_toggle)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        fields_row = QHBoxLayout()
        fields_row.setSpacing(16)

        level_col = QVBoxLayout()
        level_col.setSpacing(2)
        level_col.addWidget(QLabel("Reference level"))
        self.level_spin = QDoubleSpinBox()
        self.level_spin.setRange(-100.0, 100.0)
        self.level_spin.setDecimals(1)
        self.level_spin.setSuffix(" dB")
        self.level_spin.setFixedHeight(dp(28, 22))
        self.level_spin.setFixedWidth(dp(100, 84))
        self.level_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.level_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.level_spin.valueChanged.connect(self._on_level_edit)
        describe(self.level_spin, "Reference level",
                 "The calibrated playback level (dB) above which the curve "
                 "has no effect — set from a sound-level-meter calibration.")
        level_col.addWidget(self.level_spin)
        fields_row.addLayout(level_col)

        offset_col = QVBoxLayout()
        offset_col.setSpacing(2)
        offset_col.addWidget(QLabel("Offset"))
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-100.0, 100.0)
        self.offset_spin.setDecimals(1)
        self.offset_spin.setSuffix(" dB")
        self.offset_spin.setFixedHeight(dp(28, 22))
        self.offset_spin.setFixedWidth(dp(100, 84))
        self.offset_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.offset_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.offset_spin.valueChanged.connect(self._on_offset_edit)
        describe(self.offset_spin, "Reference offset",
                 "An adjustment (dB) applied against the reference level, "
                 "for taste or to fine-tune the calibration.")
        offset_col.addWidget(self.offset_spin)
        fields_row.addLayout(offset_col)

        fields_row.addStretch(1)
        layout.addLayout(fields_row)

        hint = QLabel("Response varies with system volume — not drawn on the graph.")
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._set_fields_enabled(False)

    def _set_fields_enabled(self, on: bool) -> None:
        self.level_spin.setEnabled(on)
        self.offset_spin.setEnabled(on)

    def _on_toggled(self, checked: bool) -> None:
        self._set_fields_enabled(checked)
        if self._loading:
            return
        self._state_touched = True
        self.changed.emit()

    def _on_level_edit(self, _value: float) -> None:
        if not self._loading:
            self._level_touched = True
        self._on_edit(_value)

    def _on_offset_edit(self, _value: float) -> None:
        if not self._loading:
            self._offset_touched = True
        self._on_edit(_value)

    def _on_edit(self, _value: float) -> None:
        if self._loading:
            return
        self.changed.emit()

    def set_theme(self, theme: dict) -> None:
        self.theme = theme

    def load(self, lc: LoudnessCorrection | None) -> None:
        self._loading = True
        try:
            self._lc = lc
            enabled = lc.enabled if lc is not None else False
            level = float(lc.get("ReferenceLevel", "0") or 0) if lc is not None else 0.0
            offset = float(lc.get("ReferenceOffset", "0") or 0) if lc is not None else 0.0
            self.btn_toggle.setChecked(enabled)
            self.level_spin.setValue(level)
            self.offset_spin.setValue(offset)
            self._set_fields_enabled(enabled)
            # A fresh load means nothing has been touched yet.
            self._level_touched = False
            self._offset_touched = False
            self._state_touched = False
        finally:
            self._loading = False

    def apply_to(self, config) -> None:
        checked = self.btn_toggle.isChecked()
        if not checked and self._lc is None:
            config.loudness_correction = None
            return
        if self._lc is None:
            # No pre-existing block: write full defaults for a freshly
            # enabled block, mirroring APO's own Editor.exe output. Ground
            # truth (strings embedded in Editor.exe itself — see
            # docs/superpowers/plans/phase1-grammar-notes.md) shows it
            # writes FOUR keys the first time loudness correction is turned
            # on: State, ReferenceLevel, ReferenceOffset, and Attenuation
            # 1.0. Omitting Attenuation would leave APO to fall back to its
            # own (undocumented) default, which may not behave as "no
            # attenuation" — so it's written explicitly here too.
            lc = LoudnessCorrection()
            lc.enabled = checked
            lc.set("ReferenceLevel", _fmt_db(self.level_spin.value()))
            lc.set("ReferenceOffset", _fmt_db(self.offset_spin.value()))
            lc.set("Attenuation", "1.0")
            config.loudness_correction = lc
            return
        # Pre-existing block: only write a key the user actually touched.
        # An untouched field is left completely alone on the backing
        # LoudnessCorrection object — including any precision (e.g.
        # "-10.567") the decimals(1) spin box can't itself represent, or a
        # non-numeric State value (e.g. "on") the enabled setter would
        # otherwise renormalize to "1"/"0" — so an unedited load-then-
        # rebuild is a true no-op.
        lc = self._lc
        if self._state_touched:
            lc.enabled = checked
        if self._level_touched:
            lc.set("ReferenceLevel", _fmt_db(self.level_spin.value()))
        if self._offset_touched:
            lc.set("ReferenceOffset", _fmt_db(self.offset_spin.value()))
        config.loudness_correction = lc


# ─────────────────────────────────────────────────────────────────────────
# VST Plugin list
# ─────────────────────────────────────────────────────────────────────────

class VSTPluginEditor(QWidget):
    """Editable list of VSTPlugin entries."""

    changed = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self._rows: list[dict] = []  # each: {"path": QLineEdit, "chunks": str, "container": QWidget}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        hint = QLabel(
            "Add VST 2.x plugins to be loaded by Equalizer APO. Use the path as shown in APO Configurator. "
            "Preset chunk data (from APO Configurator) is preserved automatically."
        )
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(4)
        outer.addLayout(self.rows_layout)

        self.btn_add = QPushButton(" Add VST Plugin")
        self.btn_add.setIcon(icons.icon("add", theme["text_muted"], 14))
        self.btn_add.setProperty("class", "ghost")
        self.btn_add.clicked.connect(lambda: self._add_row("", ""))
        describe(self.btn_add, "Add VST plugin",
                 "Add a VST 2.x effect plugin for Equalizer APO to load as part "
                 "of this profile's processing chain.")
        outer.addWidget(self.btn_add, 0, Qt.AlignmentFlag.AlignLeft)

    def _add_row(self, path: str, chunks: str) -> None:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        path_edit = QLineEdit(path)
        path_edit.setPlaceholderText("plugin.dll or C:\\path\\to\\plugin.dll")
        path_edit.textChanged.connect(lambda _: self.changed.emit())
        describe(path_edit, "VST plugin path",
                 "The plugin's DLL file, written exactly as APO Configurator shows it.")

        btn_browse = QPushButton(" Browse…")
        btn_browse.setIcon(icons.icon("folder_open", self.theme["text_muted"], 14))
        btn_browse.setProperty("class", "ghost")
        describe(btn_browse, "Browse for VST plugin",
                 "Choose the plugin's DLL file from disk.")

        btn_remove = QPushButton()
        btn_remove.setProperty("class", "icon")
        btn_remove.setFixedSize(28, 28)
        btn_remove.setIcon(icons.icon("close", self.theme["text_muted"], 14))
        describe(btn_remove, "Remove VST plugin",
                 "Remove this plugin from the profile's processing chain.")

        row_data = {"path": path_edit, "chunks": chunks, "container": container}

        def _browse() -> None:
            p, _ = QFileDialog.getOpenFileName(
                self, "Select VST Plugin", "",
                "VST Plugin (*.dll);;All Files (*)",
            )
            if p:
                path_edit.setText(p)

        def _remove() -> None:
            self._rows.remove(row_data)
            self.rows_layout.removeWidget(container)
            container.deleteLater()
            self.changed.emit()

        btn_browse.clicked.connect(_browse)
        btn_remove.clicked.connect(_remove)

        row.addWidget(path_edit, 1)
        row.addWidget(btn_browse)
        row.addWidget(btn_remove)

        self._rows.append(row_data)
        self.rows_layout.addWidget(container)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.btn_add.setIcon(icons.icon("add", theme["text_muted"], 14))

    def value(self) -> list[VSTPlugin]:
        result = []
        for row_data in self._rows:
            path = row_data["path"].text().strip()
            if path:
                result.append(VSTPlugin(path=path, chunks=row_data["chunks"]))
        return result

    def set_value(self, plugins: list[VSTPlugin]) -> None:
        for row_data in list(self._rows):
            self.rows_layout.removeWidget(row_data["container"])
            row_data["container"].deleteLater()
        self._rows.clear()
        for vst in plugins:
            self._add_row(vst.path, vst.chunks)


# ─────────────────────────────────────────────────────────────────────────
# Copy / channel routing
# ─────────────────────────────────────────────────────────────────────────

class CopyEditor(QWidget):
    """Simplified channel-routing editor: a list of raw `Copy:` expressions."""

    changed = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self._rows: list[QLineEdit] = []

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        hint = QLabel('Each line is a channel routing expression, e.g. "L=L+0.5*R" or "LFE=L L=0.0 R=0.0".')
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        self._layout.addWidget(hint)

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(4)
        self._layout.addLayout(self.rows_layout)

        self.btn_add = QPushButton(" Add Routing")
        self.btn_add.setIcon(icons.icon("add", theme["text_muted"], 14))
        self.btn_add.setProperty("class", "ghost")
        self.btn_add.clicked.connect(lambda: self._add_row(""))
        describe(self.btn_add, "Add channel routing",
                 "Add a Copy expression that mixes or redirects audio between "
                 "channels — e.g. send half of the right channel into the left.")
        self._layout.addWidget(self.btn_add, 0, Qt.AlignmentFlag.AlignLeft)

    def _add_row(self, text: str) -> None:
        row = QHBoxLayout()
        edit = QLineEdit(text)
        edit.setPlaceholderText("L=L+0.5*R")
        edit.textChanged.connect(lambda _: self.changed.emit())
        describe(edit, "Channel routing expression",
                 "A Copy expression: target channel = mix of source channels, "
                 "e.g. 'L=L+0.5*R' adds half the right channel into the left.")
        btn_remove = QPushButton()
        btn_remove.setProperty("class", "icon")
        btn_remove.setFixedSize(28, 28)
        btn_remove.setIcon(icons.icon("close", self.theme["text_muted"], 14))
        describe(btn_remove, "Remove routing",
                 "Delete this channel routing expression.")

        container = QWidget()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        row.addWidget(btn_remove)
        container.setLayout(row)

        def _remove() -> None:
            self._rows.remove(edit)
            self.rows_layout.removeWidget(container)
            container.deleteLater()
            self.changed.emit()

        btn_remove.clicked.connect(_remove)
        self._rows.append(edit)
        self.rows_layout.addWidget(container)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.btn_add.setIcon(icons.icon("add", theme["text_muted"], 14))

    def value(self) -> list[str]:
        return [edit.text().strip() for edit in self._rows if edit.text().strip()]

    def set_value(self, expressions: list[str]) -> None:
        for edit in list(self._rows):
            self._rows.remove(edit)
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for expr in expressions:
            self._add_row(expr)


# ─────────────────────────────────────────────────────────────────────────
# Conditional (If/ElseIf/Else/EndIf)
# ─────────────────────────────────────────────────────────────────────────

class ConditionalEditor(QWidget):
    changed = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        hint = QLabel("Raw conditional block, evaluated by Equalizer APO at runtime. "
                      "Available variables: sampleRate, inputChannelCount, outputChannelCount, "
                      "deviceName, connectionName, deviceGuid, stage.")
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setProperty("class", "raw-text-edit")
        self.text_edit.setPlaceholderText(
            "If: sampleRate == 44100\n"
            "    Filter 1: ON PK Fc 1000 Hz Gain -3.0 dB Q 2.0\n"
            "Else:\n"
            "    Filter 1: ON PK Fc 1000 Hz Gain -2.8 dB Q 2.0\n"
            "EndIf:"
        )
        self.text_edit.setMinimumHeight(dp(120, 90))
        self.text_edit.textChanged.connect(lambda: self.changed.emit())
        describe(self.text_edit, "Conditional block",
                 "Equalizer APO If/ElseIf/Else rules that apply different settings "
                 "depending on the device, sample rate or channel count at runtime.")
        layout.addWidget(self.text_edit)

        self._highlighter = ConfigHighlighter(self.text_edit.document(), theme)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        # Recolouring rehighlights the document, which emits textChanged even
        # though not one character changed -- and textChanged is a tracked
        # edit here. Block it, same as set_value() does, or picking a theme
        # would raise the unsaved-changes warning on an untouched profile.
        self.text_edit.blockSignals(True)
        self._highlighter.set_theme(theme)
        self.text_edit.blockSignals(False)

    def value(self) -> str:
        return self.text_edit.toPlainText().strip()

    def set_value(self, text: str) -> None:
        if self.text_edit.toPlainText() != text:
            self.text_edit.blockSignals(True)
            self.text_edit.setPlainText(text)
            self.text_edit.blockSignals(False)


# ─────────────────────────────────────────────────────────────────────────
# Eval expressions
# ─────────────────────────────────────────────────────────────────────────

class EvalEditor(QWidget):
    """Raw list of `Eval:` expressions — one APO expression per line."""

    changed = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        hint = QLabel("One expression per line; each becomes an 'Eval:' command. "
                      "Typically defines variables referenced (via `backticks`) in "
                      "later commands — definitions are written before the EQ body.")
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setProperty("class", "raw-text-edit")
        self.text_edit.setPlaceholderText("linGain = 0.5")
        self.text_edit.setMinimumHeight(dp(60, 45))
        self.text_edit.textChanged.connect(lambda: self.changed.emit())
        describe(self.text_edit, "Eval expressions",
                 "Equalizer APO expressions evaluated when the profile loads, "
                 "one per line — usually variable definitions for use in other "
                 "commands.")
        layout.addWidget(self.text_edit)

        self._highlighter = ConfigHighlighter(self.text_edit.document(), theme)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        # See ConditionalEditor.set_theme: rehighlighting emits textChanged
        # without an actual edit, which would wrongly mark the profile dirty.
        self.text_edit.blockSignals(True)
        self._highlighter.set_theme(theme)
        self.text_edit.blockSignals(False)

    def value(self) -> list[str]:
        return [ln.strip() for ln in self.text_edit.toPlainText().splitlines()
                if ln.strip()]

    def set_value(self, lines: list[str]) -> None:
        text = "\n".join(lines)
        if self.text_edit.toPlainText() != text:
            self.text_edit.blockSignals(True)
            self.text_edit.setPlainText(text)
            self.text_edit.blockSignals(False)


# The curated Stage choices (label, config value). None = no Stage line, which
# APO treats as "post-mix capture".
STAGE_CHOICES: list[tuple[str, str | None]] = [
    ("Default (playback + capture)", None),
    ("Pre-mix (before channel mixing)", "pre-mix"),
    ("Post-mix only", "post-mix"),
    ("Capture only (microphones)", "capture"),
    ("All stages", "pre-mix post-mix capture"),
]


# ─────────────────────────────────────────────────────────────────────────
# Master advanced panel — tabbed layout
# ─────────────────────────────────────────────────────────────────────────

class _GeqChannelButton(QPushButton):
    """GEQ channel tab that also reports double-clicks (rename)."""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class AdvancedPanel(QWidget):
    """Tabbed container for all advanced sections."""

    changed = Signal()
    geq_channel_selected = Signal(int)
    geq_channel_rename_requested = Signal(int)
    geq_link_toggle_requested = Signal(bool)   # mirror of the primary Link Channels control
    convert_geq_to_peq_requested = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs)

        # ── Tab 1: Graphic EQ ────────────────────────────────────────
        self.graphic_eq = GraphicEQEditor(theme)
        geq_container = QWidget()
        geq_layout = QVBoxLayout(geq_container)
        geq_layout.setContentsMargins(dp(12, 8), dp(12, 8), dp(12, 8), dp(12, 8))
        geq_layout.setSpacing(8)
        # Channel selector — same structure as the filter bands selector;
        # the selection is shared with it (switching here switches there).
        self._geq_channel_btns: list[QPushButton] = []
        self._geq_channel_state: tuple[list[str], int, bool] = ([], 0, True)
        # Heading names the channel whose curve is on screen — parity with the
        # filter bands editor so the active channel is obvious in both tabs.
        self._geq_title = QLabel("GRAPHIC EQ")
        self._geq_title.setProperty("class", "heading")
        geq_layout.addWidget(self._geq_title)
        geq_channel_bar = QWidget()
        geq_channel_bar.setFixedHeight(dp(30, 22))
        bar_layout = QHBoxLayout(geq_channel_bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(4)
        self._geq_channel_btns_layout = QHBoxLayout()
        self._geq_channel_btns_layout.setContentsMargins(0, 0, 0, 0)
        self._geq_channel_btns_layout.setSpacing(4)
        bar_layout.addLayout(self._geq_channel_btns_layout)
        bar_layout.addStretch(1)
        # Mirror of the EQ editor's Link Channels control so channels can be
        # locked/unlocked from this tab too; the editor owns the real state and
        # pushes it back via set_geq_channels.
        self._btn_geq_link = QPushButton(" Link Channels")
        self._btn_geq_link.setFixedHeight(dp(24, 20))
        self._btn_geq_link.setCheckable(True)
        self._btn_geq_link.setChecked(True)
        self._btn_geq_link.setProperty("class", "ghost")
        self._btn_geq_link.toggled.connect(self._on_geq_link_toggled)
        describe(self._btn_geq_link, "Link channels",
                 "Keep every channel identical: edit one channel and the bands, "
                 "Graphic EQ and delay are copied to all the others. "
                 "Turn off to give each channel its own independent EQ.")
        bar_layout.addWidget(self._btn_geq_link)
        self._update_geq_link_icon()
        geq_layout.addWidget(geq_channel_bar)
        geq_layout.addWidget(self.graphic_eq)
        self.tabs.addTab(geq_container, "Graphic EQ")
        self.tabs.setTabToolTip(0, "A freeform tone curve applied on top of the "
                                   "filter bands, per channel.")

        # ── Tab 2: Processing (Stage + Convolution + VST) ────────────
        self.convolution = ConvolutionEditor(theme)
        self.vst = VSTPluginEditor(theme)
        processing_container = QWidget()
        proc_layout = QVBoxLayout(processing_container)
        proc_layout.setContentsMargins(dp(12, 8), dp(12, 8), dp(12, 8), dp(12, 8))
        proc_layout.setSpacing(16)

        stage_label = QLabel("PROCESSING STAGE")
        stage_label.setProperty("class", "heading")
        proc_layout.addWidget(stage_label)
        stage_row = QHBoxLayout()
        stage_row.addWidget(QLabel("Apply during:"))
        self.stage_combo = QComboBox()
        for label, data in STAGE_CHOICES:
            self.stage_combo.addItem(label, data)
        self.stage_combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        describe(self.stage_combo, "Processing stage",
                 "When Equalizer APO runs this profile: the default covers "
                 "normal playback and recording; pre-mix runs before channels "
                 "are mixed down; capture-only limits the profile to recording "
                 "devices such as microphones.")
        stage_row.addWidget(self.stage_combo)
        stage_row.addStretch(1)
        proc_layout.addLayout(stage_row)

        sep0 = QFrame()
        sep0.setFrameShape(QFrame.Shape.HLine)
        sep0.setFrameShadow(QFrame.Shadow.Sunken)
        proc_layout.addWidget(sep0)

        conv_label = QLabel("CONVOLUTION")
        conv_label.setProperty("class", "heading")
        proc_layout.addWidget(conv_label)
        proc_layout.addWidget(self.convolution)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        proc_layout.addWidget(sep1)

        vst_label = QLabel("VST PLUGINS")
        vst_label.setProperty("class", "heading")
        proc_layout.addWidget(vst_label)
        proc_layout.addWidget(self.vst)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        proc_layout.addWidget(sep2)

        self.loudness = LoudnessEditor(theme)
        loudness_label = QLabel("LOUDNESS")
        loudness_label.setProperty("class", "heading")
        proc_layout.addWidget(loudness_label)
        proc_layout.addWidget(self.loudness)

        proc_layout.addStretch(1)
        self.tabs.addTab(scrollable_page(processing_container), "Processing")
        self.tabs.setTabToolTip(1, "Convolution (impulse responses) and VST plugins "
                                   "loaded as part of this profile.")

        # ── Tab 3: Routing (Copy) ────────────────────────────────────
        self.copy = CopyEditor(theme)
        routing_container = QWidget()
        routing_layout = QVBoxLayout(routing_container)
        routing_layout.setContentsMargins(dp(12, 8), dp(12, 8), dp(12, 8), dp(12, 8))
        routing_label = QLabel("CHANNEL ROUTING (Copy)")
        routing_label.setProperty("class", "heading")
        routing_layout.addWidget(routing_label)
        routing_layout.addWidget(self.copy)
        routing_layout.addStretch(1)
        self.tabs.addTab(routing_container, "Routing")
        self.tabs.setTabToolTip(2, "Copy expressions that mix or redirect audio "
                                   "between channels.")

        # ── Tab 4: Scripting (Conditional + Eval) ────────────────────
        self.conditional = ConditionalEditor(theme)
        self.eval_editor = EvalEditor(theme)
        cond_container = QWidget()
        cond_layout = QVBoxLayout(cond_container)
        cond_layout.setContentsMargins(dp(12, 8), dp(12, 8), dp(12, 8), dp(12, 8))
        cond_label = QLabel("CONDITIONAL (If / ElseIf / Else / EndIf)")
        cond_label.setProperty("class", "heading")
        cond_layout.addWidget(cond_label)
        cond_layout.addWidget(self.conditional, 2)
        eval_label = QLabel("EVAL EXPRESSIONS")
        eval_label.setProperty("class", "heading")
        cond_layout.addWidget(eval_label)
        cond_layout.addWidget(self.eval_editor, 1)
        self.tabs.addTab(cond_container, "Scripting")
        self.tabs.setTabToolTip(3, "If/Else rules that adapt the profile at runtime, "
                                   "plus Eval expressions that define variables.")

        for editor in (self.graphic_eq, self.convolution, self.vst, self.loudness,
                       self.copy, self.conditional, self.eval_editor):
            editor.changed.connect(self.changed.emit)
        self.graphic_eq.convert_to_peq_requested.connect(self.convert_geq_to_peq_requested.emit)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self._restyle_geq_channel_buttons()
        self._update_geq_link_icon()
        self.graphic_eq.set_theme(theme)
        self.convolution.set_theme(theme)
        self.vst.set_theme(theme)
        self.loudness.set_theme(theme)
        self.copy.set_theme(theme)
        self.conditional.set_theme(theme)
        self.eval_editor.set_theme(theme)

    # ── GEQ per-section binding ──────────────────────────────────────

    def set_geq_channels(self, names: list[str], current_idx: int, linked: bool) -> None:
        """Rebuild the GEQ channel selector tabs to mirror the filter bands
        channel selector (same names, selection and linked state)."""
        self._geq_channel_state = (list(names), current_idx, linked)
        while self._geq_channel_btns_layout.count():
            item = self._geq_channel_btns_layout.takeAt(0)
            if item.widget():
                # Hide before deleteLater: until the deferred delete runs the
                # button is still a visible child, and one that never got laid
                # out paints at Qt's default child geometry across the bar.
                item.widget().hide()
                item.widget().deleteLater()
        self._geq_channel_btns.clear()

        for i, name in enumerate(names):
            btn = _GeqChannelButton(name)
            btn.setFixedHeight(dp(24, 20))
            btn.clicked.connect(lambda _checked=False, idx=i: self.geq_channel_selected.emit(idx))
            btn.double_clicked.connect(lambda idx=i: self.geq_channel_rename_requested.emit(idx))
            describe(btn, f"Channel {name}",
                     f"Show and edit the Graphic EQ curve for the '{name}' channel. "
                     "Double-click to rename the channel.")
            self._geq_channel_btns_layout.addWidget(btn)
            self._geq_channel_btns.append(btn)

        # Reflect the editor's authoritative link state without re-emitting.
        self._btn_geq_link.blockSignals(True)
        self._btn_geq_link.setChecked(linked)
        self._btn_geq_link.blockSignals(False)
        self._update_geq_link_icon()

        self._update_geq_title()
        self._restyle_geq_channel_buttons()

    def _on_geq_link_toggled(self, checked: bool) -> None:
        """User toggled the GEQ-tab link control: ask the editor to run the
        real link/unlink flow (with its confirmation). The editor pushes the
        final state back through set_geq_channels, so we don't change our own
        state here."""
        self._update_geq_link_icon()
        self.geq_link_toggle_requested.emit(checked)

    def _update_geq_link_icon(self) -> None:
        color = (self.theme["text_on_accent"] if self._btn_geq_link.isChecked()
                 else self.theme["text_muted"])
        self._btn_geq_link.setIcon(icons.icon("link", color, 13))

    def _update_geq_title(self) -> None:
        """Name the channel being edited in the Graphic EQ heading, mirroring
        the filter bands editor: nothing to disambiguate with one channel, and
        a linked profile shares one curve across all channels."""
        names, current_idx, linked = self._geq_channel_state
        if len(names) <= 1:
            self._geq_title.setText("GRAPHIC EQ")
        elif linked:
            self._geq_title.setText("GRAPHIC EQ  ·  All channels")
        else:
            idx = min(current_idx, len(names) - 1)
            self._geq_title.setText(f"GRAPHIC EQ  ·  {names[idx]}")

    def _restyle_geq_channel_buttons(self) -> None:
        t = self.theme
        # Same convention as the filter bands selector: while linked every
        # channel holds the same curve, so all the tabs light up; unlinked,
        # only the selected channel is lit.
        _names, current_idx, linked = self._geq_channel_state
        for i, btn in enumerate(self._geq_channel_btns):
            if linked or i == current_idx:
                btn.setStyleSheet(
                    f"QPushButton {{ background: {t['accent']}; color: {t['text_on_accent']}; "
                    f"border: none; border-radius: 3px; padding: 1px 10px; font-size: 11px; }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background: transparent; color: {t['text_muted']}; "
                    f"border: 1px solid {t['border_subtle']}; border-radius: 3px; "
                    f"padding: 1px 10px; font-size: 11px; }}"
                    f"QPushButton:hover {{ color: {t['text_primary']}; "
                    f"border-color: {t['border_default']}; }}"
                )

    def load_geq_for_channel(self, channel: str, geq_string: str) -> None:
        """Update the Graphic EQ tab to show a specific channel section's GEQ."""
        if geq_string:
            self.graphic_eq.set_points_from_string(geq_string)
        else:
            self.graphic_eq.set_points_from_string(_format_geq_string(_DEFAULT_GEQ_POINTS))

    def current_geq(self) -> str:
        """Return the current GEQ string (empty string if flat/unused)."""
        return self.graphic_eq.to_save_string()

    # ── binding to ProfileConfig ─────────────────────────────────────

    def _set_stage_value(self, stage: str | None) -> None:
        """Select *stage* in the combo without emitting; a value outside the
        curated choices gets a passthrough "Custom" entry so loading a foreign
        profile never destroys its Stage line."""
        combo = self.stage_combo
        combo.blockSignals(True)
        # Drop any passthrough entry from a previously loaded profile.
        while combo.count() > len(STAGE_CHOICES):
            combo.removeItem(len(STAGE_CHOICES))
        if stage is None:
            combo.setCurrentIndex(0)
        else:
            idx = combo.findData(stage)
            if idx < 0:
                combo.addItem(f"Custom: {stage}", stage)
                idx = combo.count() - 1
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def load_from_config(self, config) -> None:
        """Load global config fields (stage, convolution, copy, vst,
        loudness correction, conditional blocks, eval expressions)."""
        self._set_stage_value(config.stage)
        self.convolution.set_value(config.convolution)
        self.copy.set_value(list(config.copy_lines))
        self.vst.set_value(list(config.vst_plugins))
        self.loudness.load(config.loudness_correction)
        combined_conditional = "\n\n".join(config.conditional_blocks)
        self.conditional.set_value(combined_conditional)
        self.eval_editor.set_value(list(config.eval_lines))

    def apply_to_config(self, config) -> None:
        """Write global config fields back to the config object."""
        config.stage = self.stage_combo.currentData()
        config.convolution = self.convolution.value()
        config.copy_lines = self.copy.value()
        config.vst_plugins = self.vst.value()
        self.loudness.apply_to(config)
        # Blank lines separate blocks — stable because parse_config strips
        # blank lines inside a block, so a block never legitimately has one.
        block_text = self.conditional.value()
        config.conditional_blocks = [
            b.strip("\n") for b in re.split(r"\n\s*\n", block_text) if b.strip()
        ]
        config.eval_lines = self.eval_editor.value()
