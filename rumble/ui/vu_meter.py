"""Stereo VU/Peak/Reference level meter panel: a small segmented LED
bargraph that sits beside the frequency-response graph, driven by the live
Analyser capture. Always shows four bars whenever active: measured OUTPUT
L/R (from `EQGraph.stereo_level_ready`) and estimated INPUT (pre-EQ) L/R
(from `EQGraph.estimated_input_level_ready`, a broadband dB offset applied
to the same measured readings -- see `spectrum.broadband_offset_db`).

Fixed colours (green/yellow/red), independent of the app theme -- a real VU
meter's red must always read as red. `set_theme()` only restyles chrome
(background, border, tick-label colour), never the bar segments themselves.

Public API:
    VuMeterWidget -- the panel widget
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from rumble.core.audio.metering import StereoLevels
from rumble.core.level_meter import (BAND_GREEN_MAX_DBFS, BAND_YELLOW_MAX_DBFS,
                                     FLOOR_DBFS, K_REFERENCE_DBFS, LevelMeter)
from rumble.ui import a11y, fonts
from rumble.ui.density import dp

# ── fixed segment colours, independent of the app theme ──────────────────
_COLOR_GREEN = QColor("#2ECC71")
_COLOR_YELLOW = QColor("#F5C518")
_COLOR_RED = QColor("#E63946")
_COLOR_UNLIT = QColor("#2A2A30")
_COLOR_HOLD = QColor("#F4F6FA")  # bright, distinct from every band colour above
# Reference-mode's K-System marker line -- distinct from every band/hold
# colour above so it never gets mistaken for a lit segment.
_COLOR_REFERENCE = QColor("#4DA6FF")

# ── segmentation ───────────────────────────────────────────────────────────
# 2 dB per segment over the meter's 60 dB (FLOOR_DBFS..0) range -- matches a
# typical hardware LED bargraph's resolution (finer than the widget's own
# printed scale, which only labels a handful of reference points).
_N_SEGMENTS = 30
# Reuses the fraction the removed in-graph LED-grid overlay used for its own
# gap-to-block ratio (same visual language: a classic hifi VU ladder look).
_SEGMENT_GAP_FRAC = 0.22

# dB values the scale prints along the meter's left edge, top (0, loudest) to
# bottom (FLOOR_DBFS, quietest). Evenly spaced (10 dB apart) deliberately --
# the meter's dBFS-to-pixel mapping is linear, so uneven tick spacing (e.g.
# the finer 0/-3/-6/-10 groupings some hardware meters print) reads as a
# "squeeze" at the loud end when drawn on a linear scale this narrow. The
# fixed colour bands (see level_meter.py's BAND_GREEN_MAX_DBFS/
# BAND_YELLOW_MAX_DBFS, -10/-3) intentionally do NOT need to land on a
# printed tick -- a colour transition and a labelled gradation are different
# things on a real meter too.
_SCALE_TICKS_DBFS = (0.0, -10.0, -20.0, -30.0, -40.0, -50.0, FLOOR_DBFS)

# Mode row: VU | Peak | Ref.
_MODES = ("vu", "peak", "ref")
_MODE_LABELS = {"vu": "VU", "peak": "Peak", "ref": "Ref"}
# K-level row (Reference mode only): K12 | K14 | K20, ordered loudest-
# reference-first to match the mode row's own loud-to-quiet reading order.
_K_LEVELS = ("k12", "k14", "k20")
_K_LABELS = {"k12": "K12", "k14": "K14", "k20": "K20"}


def _segment_color(seg_top_dbfs: float) -> QColor:
    if seg_top_dbfs <= BAND_GREEN_MAX_DBFS:
        return _COLOR_GREEN
    if seg_top_dbfs <= BAND_YELLOW_MAX_DBFS:
        return _COLOR_YELLOW
    return _COLOR_RED


class _MeterCanvas(QWidget):
    """The painted LED-bargraph area (everything below the mode/K-level
    rows) -- a plain `QPainter` surface, not pyqtgraph, since this is a
    small fixed decoration rather than a data-heavy plot."""

    def __init__(self, owner: "VuMeterWidget") -> None:
        super().__init__()
        self._owner = owner
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        t = self._owner.theme
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor(t.get("graph_bg", "#101014")))

        scale_font = fonts.mono_font(8)
        painter.setFont(scale_font)
        metrics = painter.fontMetrics()
        label_w = max(metrics.horizontalAdvance(f"{int(v):d}") for v in _SCALE_TICKS_DBFS) + 4

        margin_top = dp(0, 6)
        margin_bottom = dp(0, 6)
        gutter = dp(0, 4)
        bar_area_top = margin_top
        bar_area_bottom = h - margin_bottom
        bar_area_h = max(1, bar_area_bottom - bar_area_top)

        def y_for_dbfs(dbfs: float) -> float:
            frac = (dbfs - FLOOR_DBFS) / (0.0 - FLOOR_DBFS)
            frac = min(1.0, max(0.0, frac))
            return bar_area_bottom - frac * bar_area_h

        # Scale labels, inner-left edge (the side facing the graph).
        pen = QPen(QColor(t.get("graph_text", "#9AA0AA")))
        painter.setPen(pen)
        for dbfs in _SCALE_TICKS_DBFS:
            y = y_for_dbfs(dbfs)
            text = "0" if dbfs == 0.0 else f"{int(dbfs):d}"
            painter.drawText(0, int(y - metrics.height() / 2), label_w,
                              metrics.height(), Qt.AlignmentFlag.AlignVCenter |
                              Qt.AlignmentFlag.AlignRight, text)

        # Four columns, always: measured OUTPUT L/R (closest to the dB scale,
        # the "real" reading) then estimated INPUT L/R (farthest, the
        # synthesized pre-EQ reading) -- grouped visually by a wider gap
        # between the two pairs than within each pair, per editor_panel.py's
        # own "Show output+input, no separate toggle, whenever the meter is
        # on" requirement (there's no Output/Input/Both selector to label
        # this any other way).
        bars_x0 = label_w + gutter
        bars_w = max(2, w - bars_x0)
        col_gap = dp(0, 2)
        pair_gap = dp(0, 6)
        col_w = max(1, (bars_w - 2 * col_gap - pair_gap) // 4)

        om = self._owner.level_meter
        im = self._owner.input_level_meter
        x0 = bars_x0
        x1 = x0 + col_w + col_gap
        x2 = x1 + col_w + pair_gap
        x3 = x2 + col_w + col_gap
        cols = (
            (x0, om.bar_l, om.hold_l, om.clipping[0]),
            (x1, om.bar_r, om.hold_r, om.clipping[1]),
            (x2, im.bar_l, im.hold_l, im.clipping[0]),
            (x3, im.bar_r, im.hold_r, im.clipping[1]),
        )
        for x, bar_dbfs, hold_dbfs, clipping in cols:
            self._paint_column(painter, x, col_w, bar_area_top, bar_area_bottom,
                                bar_dbfs, hold_dbfs, clipping)

        if om.mode == "ref":
            self._paint_reference_line(painter, bars_x0, bars_w, y_for_dbfs, metrics)

    def _paint_column(self, painter: QPainter, x: int, col_w: int,
                       area_top: float, area_bottom: float,
                       bar_dbfs: float, hold_dbfs: float | None,
                       clipping: bool) -> None:
        area_h = area_bottom - area_top
        seg_h = area_h / _N_SEGMENTS
        gap = seg_h * _SEGMENT_GAP_FRAC
        step_db = (0.0 - FLOOR_DBFS) / _N_SEGMENTS

        # Strictly-above comparisons throughout: a reading sitting exactly AT
        # the floor (silence, or the meter reset to idle) must light zero
        # segments/no hold marker, not segment 0 -- `>=` would treat "at the
        # floor" the same as "just above it".
        hold_seg = None
        if hold_dbfs is not None and hold_dbfs > FLOOR_DBFS:
            hold_seg = min(_N_SEGMENTS - 1,
                            max(0, int((hold_dbfs - FLOOR_DBFS) / step_db)))

        for i in range(_N_SEGMENTS):
            seg_lo_db = FLOOR_DBFS + i * step_db
            seg_top_db = seg_lo_db + step_db
            lit = bar_dbfs > seg_lo_db
            is_hold = hold_seg == i
            is_top_segment = i == _N_SEGMENTS - 1

            if is_hold:
                color = _COLOR_HOLD
            elif lit:
                color = _segment_color(seg_top_db)
            else:
                color = _COLOR_UNLIT

            if clipping and is_top_segment:
                color = _COLOR_RED if lit or is_hold else color

            y_bottom = area_bottom - i * seg_h
            y_top = y_bottom - seg_h + gap
            painter.fillRect(x, int(y_top), col_w, int(seg_h - gap), color)

    def _paint_reference_line(self, painter: QPainter, bars_x0: int, bars_w: int,
                               y_for_dbfs, metrics) -> None:
        """The K-System reference marker: a distinct line across both bars at
        the chosen K-level's dBFS point, standing in for an analog VU meter's
        bold "0 VU" mark -- see level_meter.py's K_REFERENCE_DBFS docstring
        for why a digital scale needs a chosen reference rather than reusing
        0 dBFS itself (0 dBFS has no headroom above it to show)."""
        ref_dbfs = K_REFERENCE_DBFS[self._owner.k_reference]
        y = int(y_for_dbfs(ref_dbfs))
        painter.fillRect(bars_x0, y - 1, bars_w, 2, _COLOR_REFERENCE)

        label = "0"
        text_w = metrics.horizontalAdvance(label) + 4
        text_x = bars_x0 + bars_w - text_w
        text_y = y - metrics.height() - 1 if y - metrics.height() - 1 >= 0 else y + 2
        painter.fillRect(text_x, text_y, text_w, metrics.height(), QColor(0, 0, 0, 160))
        painter.setPen(QPen(_COLOR_REFERENCE))
        painter.drawText(text_x, text_y, text_w, metrics.height(),
                          Qt.AlignmentFlag.AlignCenter, label)


class VuMeterWidget(QWidget):
    """Right-side stereo VU/Peak/Reference meter panel. Owns two
    `LevelMeter`s (measured output, estimated input); the caller
    (editor_panel.py) feeds them `on_stereo_level()`/
    `on_estimated_input_level()` from the live capture and drives
    `set_active()` from the Analyser on/off toggle."""

    mode_changed = Signal(str)  # "vu" | "peak" | "ref", only on user click
    k_reference_changed = Signal(str)  # "k12" | "k14" | "k20", only on user click

    def __init__(self, theme: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.level_meter = LevelMeter(mode="vu")
        # Estimated pre-EQ reading, driven by `on_estimated_input_level()` --
        # see the module docstring and `EQGraph._on_spectrum_stereo_level()`
        # for how this is synthesized (there's no separate "input" capture).
        self.input_level_meter = LevelMeter(mode="vu")
        self.k_reference = "k14"
        self._active = False

        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFixedWidth(dp(0, 108))

        a11y.describe(self, "Level meter",
                       "Live stereo level meter for the system's audio output, "
                       "shown whenever the Analyser is on. Four bars: the two "
                       "nearest the scale are the measured output, the two "
                       "furthest are the estimated pre-EQ input. Switch between "
                       "VU (slow, loudness-like), Peak (fast, clip-conscious) "
                       "and Reference (Bob Katz's K-System: an analog-VU-style "
                       "target level on the digital scale) ballistics with "
                       "the buttons above.")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(dp(0, 3))

        self.btn_vu, self.btn_peak, self.btn_ref = self._build_segmented_row(
            root, _MODES, _MODE_LABELS, self._on_mode_clicked)
        a11y.describe(self.btn_vu, "VU mode",
                       "Classic VU-meter ballistics: slow, symmetric, tracks "
                       "average loudness rather than instantaneous peaks.")
        a11y.describe(self.btn_peak, "Peak mode",
                       "Fast peak-programme ballistics with a decaying "
                       "peak-hold cap: tracks instantaneous level, useful "
                       "for spotting clipping.")
        a11y.describe(self.btn_ref, "Reference mode",
                       "Same slow ballistics as VU mode, but marks a chosen "
                       "K-System reference level on the scale -- an analog- "
                       "VU-style target line with headroom above it, rather "
                       "than reading straight dBFS. Pick K12/K14/K20 below "
                       "once this mode is selected.")

        self.btn_k12, self.btn_k14, self.btn_k20 = self._build_segmented_row(
            root, _K_LEVELS, _K_LABELS, self._on_k_clicked)
        a11y.describe(self.btn_k12, "K-12 reference",
                       "0 on the K-System scale = -12 dBFS, 12 dB of headroom "
                       "above it. Suits already-loud, heavily-limited material.")
        a11y.describe(self.btn_k14, "K-14 reference",
                       "0 on the K-System scale = -14 dBFS, 14 dB of headroom "
                       "above it. Bob Katz's general-purpose recommendation.")
        a11y.describe(self.btn_k20, "K-20 reference",
                       "0 on the K-System scale = -20 dBFS, 20 dB of headroom "
                       "above it. Suits wide-dynamic-range or critical-"
                       "listening material.")
        self._k_row = self.btn_k12.parentWidget()

        self._canvas = _MeterCanvas(self)
        root.addWidget(self._canvas, 1)

        # Both rows default-check their first button (_build_segmented_row);
        # the mode row's "vu" happens to already match LevelMeter's own
        # constructor default above, but the K-row's default reference is
        # "k14" (Katz's general-purpose recommendation), not k12 (first in
        # _K_LEVELS' loud-to-quiet display order) -- these two explicit calls
        # are what actually put every button/visibility/style in sync.
        self.set_mode("vu")
        self.set_k_reference("k14")

    # ── construction helper ─────────────────────────────────────────

    def _build_segmented_row(self, root: QVBoxLayout, keys: tuple[str, ...],
                              labels: dict[str, str], on_click) -> tuple[QPushButton, ...]:
        """A row of N equal-width checkable buttons that read as one
        segmented control (rounded outer corners, square/borderless-between
        inner joins) -- the same visual pattern editor_panel.py's Q/BW
        toggle uses for two buttons, generalised to N."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        buttons = []
        for key in keys:
            btn = QPushButton(labels[key])
            btn.setCheckable(True)
            btn.setFixedHeight(dp(0, 20))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, k=key: on_click(k))
            layout.addWidget(btn)
            buttons.append(btn)
        buttons[0].setChecked(True)
        root.addWidget(row)
        return tuple(buttons)

    # ── public API ───────────────────────────────────────────────────

    def set_theme(self, theme: dict) -> None:
        """Restyle chrome only (background, tick-label colour, button
        styling) -- the bar segments themselves are theme-invariant."""
        self.theme = theme
        self._update_segmented_styles()
        self._canvas.update()

    def set_mode(self, mode: str) -> None:
        """Programmatic mode change (settings restore) -- does not emit
        `mode_changed`. Applies to both the output and input meters -- they
        always share one ballistics mode."""
        mode = mode if mode in _MODES else "vu"
        self.level_meter.set_mode(mode)
        self.input_level_meter.set_mode(mode)
        for btn, key in ((self.btn_vu, "vu"), (self.btn_peak, "peak"), (self.btn_ref, "ref")):
            btn.blockSignals(True)
            btn.setChecked(key == mode)
            btn.blockSignals(False)
        self._k_row.setVisible(mode == "ref")
        self._update_segmented_styles()
        self._canvas.update()

    def set_k_reference(self, level: str) -> None:
        """Programmatic K-level change (settings restore) -- does not emit
        `k_reference_changed`."""
        level = level if level in _K_LEVELS else "k14"
        self.k_reference = level
        for btn, key in ((self.btn_k12, "k12"), (self.btn_k14, "k14"), (self.btn_k20, "k20")):
            btn.blockSignals(True)
            btn.setChecked(key == level)
            btn.blockSignals(False)
        self._update_segmented_styles()
        self._canvas.update()

    def set_active(self, active: bool) -> None:
        """`False` (Analyser off): reset both meters to the floor, all
        segments dark -- the panel stays visible/reserved, it just goes
        idle rather than disappearing. `True`: resumes responding to
        `on_stereo_level()`/`on_estimated_input_level()`."""
        self._active = active
        if not active:
            self.level_meter.reset()
            self.input_level_meter.reset()
        self._canvas.update()

    def on_stereo_level(self, levels: StereoLevels) -> None:
        if not self._active:
            return
        self.level_meter.update(levels)
        self._canvas.update()

    def on_estimated_input_level(self, levels: StereoLevels | None) -> None:
        """`None` whenever no calculated EQ response is known yet (e.g. no
        filters set, or a mic-fallback capture device -- see
        `EQGraph._on_spectrum_ready()`'s offset computation) -- the input
        bars simply idle at the floor until a real estimate is available,
        the same "goes dark, doesn't disappear" idle behaviour as the whole
        panel going inactive."""
        if not self._active:
            return
        if levels is None:
            self.input_level_meter.reset()
        else:
            self.input_level_meter.update(levels)
        self._canvas.update()

    # ── internal ─────────────────────────────────────────────────────

    def _on_mode_clicked(self, mode: str) -> None:
        self.set_mode(mode)
        self.mode_changed.emit(mode)

    def _on_k_clicked(self, level: str) -> None:
        self.set_k_reference(level)
        self.k_reference_changed.emit(level)

    def _update_segmented_styles(self) -> None:
        t = self.theme
        active = (
            f"background: {t['accent']}; color: {t['text_on_accent']}; "
            f"border: 1px solid {t['accent_active']}; font-weight: 600;"
        )
        inactive = (
            f"background: transparent; color: {t['text_muted']}; "
            f"border: 1px solid {t['border_subtle']};"
        )
        rows = (
            (self.btn_vu, self.btn_peak, self.btn_ref),
            (self.btn_k12, self.btn_k14, self.btn_k20),
        )
        for buttons in rows:
            n = len(buttons)
            for i, btn in enumerate(buttons):
                is_left, is_right = i == 0, i == n - 1
                radius_tl = radius_bl = "3px" if is_left else "0"
                radius_tr = radius_br = "3px" if is_right else "0"
                border_right = "border-right: none;" if not is_right else ""
                btn.setStyleSheet(
                    f"QPushButton {{ {active if btn.isChecked() else inactive} "
                    f"border-top-left-radius: {radius_tl}; "
                    f"border-bottom-left-radius: {radius_bl}; "
                    f"border-top-right-radius: {radius_tr}; "
                    f"border-bottom-right-radius: {radius_br}; "
                    f"{border_right} padding: 0; font-size: 10px; }}")
