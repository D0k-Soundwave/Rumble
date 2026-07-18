"""A single filter band column: enable toggle, type dropdown, Fc/Gain/Q controls, delete."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QStyle, QStyleOptionComboBox, QStylePainter, QVBoxLayout,
    QWidget,
)

from rumble.core.apo_config import (
    DEFAULT_SAMPLE_RATE, FILTER_CATEGORIES, FILTER_TYPE_LABELS, IIR_EDITOR_AVAILABLE, Filter,
)
from rumble.core.eq_math import bw_to_q_exact, q_to_bw_exact
from rumble.ui import icons
from rumble.ui.a11y import describe
from rumble.ui.density import dp

# Shared row metrics for the spreadsheet-style band grid — BandGridHeader and
# every FilterRow column must agree on these so the rows line up exactly.
# Functions rather than constants: the values depend on the UI density, which
# is only known at widget construction time.
ROW_SPACING = 2

# Nyquist–Shannon sampling theorem: no frequency at or above half the sample
# rate can be represented, so 0.5 is a hard mathematical ceiling, not a
# tuned value. 0.45 is a soft warning margin inside that ceiling (Rumble
# 1.1/5.2) so a band nearing the wall gets a visible cue before the hard
# clamp kicks in.
NYQUIST_FRACTION = 0.5
NYQUIST_WARN_FRACTION = 0.45


def row_h_index() -> int:
    return dp(18, 16)


def row_h_field() -> int:
    return dp(24, 20)


def row_margin() -> int:
    return dp(4, 3)


def column_width() -> int:
    # Every band column is the same fixed width so the grid reads as aligned
    # columns; wide enough for the longest field text ("96000.0 Hz").
    return dp(100, 86)


class BandGridHeader(QWidget):
    """Fixed row labels down the left edge of the band grid. Heights, spacing
    and vertical margins mirror FilterRow so each label lines up with its row.
    Sits outside the band-strip frame: plain labels, no panel behind them."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedWidth(dp(58, 50))
        rows = (
            ("BAND", row_h_index()),
            ("FREQ", row_h_field()),
            ("GAIN", row_h_field()),
            ("Q", row_h_field()),
            ("FILTER", row_h_field()),
        )
        layout = QVBoxLayout(self)
        # The +1 on the top margin matches the band-strip frame's 1px border,
        # which shifts the columns down relative to this gutter.
        layout.setContentsMargins(6, row_margin() + 1, 8, row_margin())
        layout.setSpacing(ROW_SPACING)
        self._lbl_q: QLabel | None = None
        for text, height in rows:
            lbl = QLabel(text)
            lbl.setProperty("class", "muted")
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lbl.setFixedHeight(height)
            layout.addWidget(lbl)
            if text == "Q":
                self._lbl_q = lbl
        layout.addStretch(1)

    def set_bw_mode(self, bw_mode: bool) -> None:
        if self._lbl_q is not None:
            self._lbl_q.setText("BW" if bw_mode else "Q")


class _TypeCombo(QComboBox):
    """Shows the short type code (item userData) centred when closed so the
    column can stay slim; the popup still lists the full filter type names."""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        opt.currentText = ""
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.setPen(opt.palette.color(QPalette.ColorRole.ButtonText))
        # Centre across the whole widget (not the edit-field subrect, which
        # excludes the drop-down arrow) so the code lines up with the
        # centre-aligned spinbox values in the rows above.
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                         str(self.currentData() or self.currentText()))


class FilterRow(QWidget):
    """One band column in the horizontal filter strip, bound to a `core.apo_config.Filter`.

    The Q/BW display mode is editor-wide (set via `set_bw_mode`), not
    per-band: the Q field shows whichever representation is selected,
    converting from the filter's stored form when they differ. The stored
    form only changes when the user edits the value.
    """

    changed = Signal()
    delete_requested = Signal(object)
    hovered = Signal(object, bool)
    solo_toggled = Signal(object, bool)

    def __init__(self, filt: Filter, index: int, theme: dict,
                 bw_mode: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.filter = filt
        self.theme = theme
        self._index = index
        self._bw_mode = bw_mode
        self._sample_rate = DEFAULT_SAMPLE_RATE
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFixedWidth(column_width())
        self.setProperty("class", "band-column")
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # Compact fields: override the app-wide padding/min-height so the
        # band strip doesn't waste vertical space.
        self.setStyleSheet(
            "QDoubleSpinBox, QComboBox { padding: 2px 5px; min-height: 16px; }"
        )

        self._build_ui()
        self._load_from_filter()
        self._wire_signals()
        self._update_field_visibility()

    # ── construction ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        margin = row_margin()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(ROW_SPACING)

        # Band number chip (the column header) doubles as the on/off switch:
        # filled with the theme accent while the band is active, hollow when
        # bypassed. Delete button at the right edge with a matching spacer on
        # the left so the chip stays centred.
        self.btn_index = QPushButton(str(self._index))
        self.btn_index.setFixedSize(26, row_h_index())
        self.btn_index.setCursor(Qt.CursorShape.PointingHandCursor)
        describe(self.btn_index, "Band number / on-off",
                 "This band's position in the filter list, matching the numbered "
                 "markers on the graph above. Click to switch the band on or "
                 "off — a filled number is active; a bypassed band stays in the "
                 "profile but has no effect on the sound.")
        self.btn_delete = QPushButton()
        self.btn_delete.setProperty("class", "icon")
        self.btn_delete.setFixedSize(16, 16)
        self.btn_delete.setIcon(icons.icon("close", self.theme["text_muted"], 11))
        describe(self.btn_delete, "Delete band",
                 "Remove this filter band from the profile.")
        # Solo occupies the left slot that mirrors the delete button, keeping
        # the number chip centred.
        self.btn_solo = QPushButton("S")
        self.btn_solo.setFixedSize(16, 16)
        self.btn_solo.setCheckable(True)
        self.btn_solo.setCursor(Qt.CursorShape.PointingHandCursor)
        describe(self.btn_solo, "Solo band",
                 "Audition this band alone: every other band is muted in the "
                 "live preview while solo is on. Nothing is changed or saved — "
                 "release solo (or switch channels/profiles) to hear the full "
                 "EQ again.")
        index_row = QHBoxLayout()
        index_row.setContentsMargins(0, 0, 0, 0)
        index_row.setSpacing(0)
        index_row.addWidget(self.btn_solo)
        index_row.addStretch(1)
        index_row.addWidget(self.btn_index)
        index_row.addStretch(1)
        index_row.addWidget(self.btn_delete)
        layout.addLayout(index_row)
        self._update_index_style()
        self._update_solo_style()

        # FREQ row — max is derived from the active sample rate (Nyquist
        # ceiling), see set_sample_rate(); starts at the default rate.
        self.spn_freq = QDoubleSpinBox()
        self.spn_freq.setRange(1.0, self._sample_rate * NYQUIST_FRACTION)
        self.spn_freq.setDecimals(1)
        self.spn_freq.setSuffix(" Hz")
        self.spn_freq.setFixedHeight(row_h_field())
        self.spn_freq.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spn_freq.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        describe(self.spn_freq, "Frequency",
                 "The centre frequency of this band in Hertz — where on the "
                 "bass-to-treble range the filter acts.")
        layout.addWidget(self.spn_freq)

        # GAIN row
        self.spn_gain = QDoubleSpinBox()
        self.spn_gain.setRange(-30.0, 30.0)
        self.spn_gain.setDecimals(1)
        self.spn_gain.setSuffix(" dB")
        self.spn_gain.setFixedHeight(row_h_field())
        self.spn_gain.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spn_gain.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        describe(self.spn_gain, "Gain",
                 "How much this band boosts (positive) or cuts (negative) the "
                 "sound at its centre frequency, in decibels.")
        layout.addWidget(self.spn_gain)

        # Q / BW row — a plain field like FREQ and GAIN; which representation
        # it shows is governed by the editor-wide Q/BW toggle.
        self.spn_q = QDoubleSpinBox()
        self.spn_q.setRange(0.1, 100.0)
        self.spn_q.setDecimals(2)
        self.spn_q.setFixedHeight(row_h_field())
        self.spn_q.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spn_q.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        layout.addWidget(self.spn_q)

        # FILTER type row — shows the short code centred when closed
        self.cmb_type = _TypeCombo()
        self.cmb_type.setFixedHeight(row_h_field())
        self.cmb_type.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_type.setMinimumContentsLength(4)
        self._populate_type_dropdown()
        describe(self.cmb_type, "Filter type",
                 "The shape of this band: Peak boosts or cuts around the centre "
                 "frequency, shelves tilt everything above or below it, and "
                 "High/Low Pass filters remove sound beyond it.")
        layout.addWidget(self.cmb_type)

        layout.addStretch(1)

    def _populate_type_dropdown(self) -> None:
        """Rumble 1.2: "Custom IIR" is hidden from new authoring (no
        coefficient editor exists yet, PLANNED_WORK #16) — but a band already
        bound to an IIR filter (loaded from an imported/round-tripped
        profile) still lists it, so its dropdown displays the true current
        type instead of silently showing the wrong one."""
        self.cmb_type.clear()
        for type_codes in FILTER_CATEGORIES.values():
            codes = [c for c in type_codes
                     if c != "IIR" or IIR_EDITOR_AVAILABLE or self.filter.type == "IIR"]
            if not codes:
                continue
            if self.cmb_type.count() > 0:
                self.cmb_type.insertSeparator(self.cmb_type.count())
            for code in codes:
                self.cmb_type.addItem(FILTER_TYPE_LABELS.get(code, code), userData=code)

    # ── state sync ───────────────────────────────────────────────────

    def _update_index_style(self) -> None:
        """Filled accent chip while the band is active, hollow when bypassed —
        flat theme colours only, same language as the channel tabs."""
        t = self.theme
        # QSS min/max sizes override setFixedSize, so the chip's dimensions
        # are stated here; padding 0 keeps the digit from being clipped. The
        # hollow variant's 1px border sits outside the QSS box, so its
        # min/max are 2px smaller to keep both states exactly 26x18.
        base = "padding: 0px; margin: 0px; border-radius: 3px; font-size: 10px;"
        if self.filter.enabled:
            self.btn_index.setStyleSheet(
                f"QPushButton {{ {base} min-width: 26px; max-width: 26px; "
                f"min-height: 18px; max-height: 18px; "
                f"background: {t['accent']}; color: {t['text_on_accent']}; "
                f"border: none; font-weight: 600; }}"
            )
        else:
            self.btn_index.setStyleSheet(
                f"QPushButton {{ {base} min-width: 24px; max-width: 24px; "
                f"min-height: 16px; max-height: 16px; "
                f"background-color: {t['bg_deep']}; background-image: none; "
                f"color: {t['text_muted']}; "
                f"border: 1px solid {t['border_subtle']}; }}"
                f"QPushButton:hover {{ border-color: {t['border_default']}; }}"
            )

    def _load_from_filter(self) -> None:
        f = self.filter
        self._update_index_style()
        idx = self.cmb_type.findData(f.type)
        if idx >= 0:
            self.cmb_type.setCurrentIndex(idx)
        self.spn_freq.setValue(f.fc)
        self._update_freq_warning_state()
        self.spn_gain.setValue(f.gain)
        self._sync_q_widget_value()

    def _shows_bw(self) -> bool:
        return self._bw_mode and self.filter.supports_bw()

    def _sync_q_widget_value(self) -> None:
        """Show the filter's width in the selected representation, converting
        from the stored one when they differ (display only — the filter is
        not modified until the user edits the value)."""
        f = self.filter
        self.spn_q.blockSignals(True)
        if self._shows_bw():
            self.spn_q.setRange(0.01, 12.0)
            self.spn_q.setValue(f.bw_oct if f.use_bw
                                else q_to_bw_exact(f.q, f.fc, self._sample_rate))
            describe(self.spn_q, "Bandwidth",
                     "How wide this band is, in octaves — larger values affect a "
                     "broader range of frequencies.")
        else:
            self.spn_q.setRange(0.1, 100.0)
            self.spn_q.setValue(bw_to_q_exact(f.bw_oct, f.fc, self._sample_rate)
                                if (f.use_bw and f.supports_bw()) else f.q)
            describe(self.spn_q, "Q factor",
                     "How narrow this band is — higher Q affects a smaller range "
                     "of frequencies around the centre; lower Q is broader.")
        self.spn_q.blockSignals(False)

    def set_bw_mode(self, bw_mode: bool) -> None:
        """Switch the Q field between Q-factor and Bandwidth display."""
        if bw_mode == self._bw_mode:
            return
        self._bw_mode = bw_mode
        self._sync_q_widget_value()

    def _update_solo_style(self) -> None:
        t = self.theme
        base = ("padding: 0px; margin: 0px; border-radius: 3px; "
                "font-size: 9px; min-width: 16px; max-width: 16px; "
                "min-height: 14px; max-height: 14px;")
        if self.btn_solo.isChecked():
            self.btn_solo.setStyleSheet(
                f"QPushButton {{ {base} background: {t['accent']}; "
                f"color: {t['text_on_accent']}; border: none; font-weight: 600; }}")
        else:
            self.btn_solo.setStyleSheet(
                f"QPushButton {{ {base} background: transparent; "
                f"color: {t['text_muted']}; border: 1px solid {t['border_subtle']}; }}"
                f"QPushButton:hover {{ border-color: {t['border_default']}; }}")

    def set_solo_silently(self, on: bool) -> None:
        """Set the solo state without emitting — the editor uses this to
        release the previous solo when another band takes it."""
        self.btn_solo.blockSignals(True)
        self.btn_solo.setChecked(on)
        self.btn_solo.blockSignals(False)
        self._update_solo_style()

    def _on_solo_toggled(self, on: bool) -> None:
        self._update_solo_style()
        self.solo_toggled.emit(self, on)

    def _wire_signals(self) -> None:
        self.btn_index.clicked.connect(self._on_enabled_clicked)
        self.btn_solo.toggled.connect(self._on_solo_toggled)
        self.cmb_type.currentIndexChanged.connect(self._on_type_changed)
        self.spn_freq.valueChanged.connect(self._on_freq_changed)
        self.spn_gain.valueChanged.connect(self._on_gain_changed)
        self.spn_q.valueChanged.connect(self._on_q_changed)
        self.btn_delete.clicked.connect(lambda: self.delete_requested.emit(self))

    # ── slots ────────────────────────────────────────────────────────

    def _on_enabled_clicked(self) -> None:
        self.filter.enabled = not self.filter.enabled
        self._update_index_style()
        self.changed.emit()

    def _on_type_changed(self, _index: int) -> None:
        new_type = self.cmb_type.currentData()
        if new_type:
            self.filter.type = new_type
        self._update_field_visibility()
        self._sync_q_widget_value()
        self.changed.emit()

    def _on_freq_changed(self, value: float) -> None:
        self.filter.fc = value
        self._update_freq_warning_state()
        self.changed.emit()

    def _update_freq_warning_state(self) -> None:
        """Soft-warn (Rumble 5.2) once Fc enters the band between the warn
        fraction and true Nyquist; the spinbox's own range already hard-clamps
        at Nyquist itself, so this only ever flags the softer inner zone."""
        warn_at = self._sample_rate * NYQUIST_WARN_FRACTION
        is_warning = self.spn_freq.value() >= warn_at
        self.spn_freq.setProperty("class", "warning" if is_warning else "")
        self.spn_freq.style().unpolish(self.spn_freq)
        self.spn_freq.style().polish(self.spn_freq)

    def set_sample_rate(self, sample_rate: int) -> None:
        """Re-derive the Fc ceiling for a new active sample rate (Rumble
        1.1): hard-clamps the spinbox range at true Nyquist — which also
        clamps an out-of-range stored value and, via valueChanged, updates
        `self.filter.fc` to match — then re-checks the soft warning zone."""
        if sample_rate == self._sample_rate:
            return
        self._sample_rate = sample_rate
        self.spn_freq.setRange(1.0, sample_rate * NYQUIST_FRACTION)
        self._update_freq_warning_state()

    def _on_gain_changed(self, value: float) -> None:
        self.filter.gain = value
        self.changed.emit()

    def _on_q_changed(self, value: float) -> None:
        # Store in the representation being edited; keep the other one in
        # sync so switching modes round-trips cleanly.
        f = self.filter
        if self._shows_bw():
            f.use_bw = True
            f.bw_oct = value
            f.q = bw_to_q_exact(value, f.fc, self._sample_rate)
        else:
            f.use_bw = False
            f.q = value
            f.bw_oct = q_to_bw_exact(value, f.fc, self._sample_rate)
        self.changed.emit()

    # ── visibility / index ───────────────────────────────────────────

    def _update_field_visibility(self) -> None:
        f = self.filter
        is_iir = f.type == "IIR"
        gain_ok = f.supports_gain() and not is_iir
        q_ok = f.supports_q() and not is_iir

        self.spn_gain.setEnabled(gain_ok)
        self.spn_q.setEnabled(q_ok)
        self.spn_freq.setEnabled(not is_iir)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("class", "band-column-selected" if selected else "band-column")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_index(self, index: int) -> None:
        self._index = index
        self.btn_index.setText(str(index))

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.btn_delete.setIcon(icons.icon("close", theme["text_muted"], 11))
        self._update_index_style()
        self._update_solo_style()

    # ── hover tracking for graph highlight ───────────────────────────

    def event(self, ev) -> bool:  # noqa: N802
        if ev.type() == QEvent.Type.HoverEnter:
            self.hovered.emit(self, True)
        elif ev.type() == QEvent.Type.HoverLeave:
            self.hovered.emit(self, False)
        return super().event(ev)
