"""Shared bass/treble/tilt trim strip -- roadmap B1 UI. One class, used
identically by the tray toast (toast_menu.py) and the main window's header
popover (main_window.py), so the two surfaces can never drift apart in
behavior.

This widget never touches ProfileManager directly -- same host/widget
separation the rest of toast_menu.py's controls use (set_listeners(),
set_bypass_state()): it only emits signals and exposes a setter so each host
can keep it synced with the live ProfileManager.session_trim state (see
S4 handoff, docs/superpowers/plans/2026-07-12-adaptive-frontend-progress.md).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from rumble.core import tone_trim
from rumble.ui.a11y import describe

# Same 250 ms debounce main_window.py's own _preview_timer uses to collapse a
# burst of live-tuning edits into one apply (main_window.py:
# `self._preview_timer.setInterval(250)`) -- reused here for the identical
# reason: a slider drag fires many valueChanged signals per second, and each
# one would otherwise trigger a full render-layer recompute.
_DEBOUNCE_MS = 250

# Slider resolution: tenths of a dB (or dB/octave) per step -- fine enough to
# feel continuous over the ~1.2-2 inch travel a "compact" strip affords,
# without inventing a new unit convention (values round-trip exactly through
# tone_trim's own float dB inputs).
_STEPS_PER_DB = 10


def _make_slider(max_db: float) -> QSlider:
    slider = QSlider(Qt.Orientation.Horizontal)
    span = round(max_db * _STEPS_PER_DB)
    slider.setRange(-span, span)
    slider.setValue(0)
    return slider


class ToneTrimWidget(QWidget):
    """Bass / Treble / Tilt sliders, a Reset button, and a "Bake into
    profile..." button. Emits `trim_changed` (debounced 250 ms, or
    immediately on Reset) with the full trim triple, and `bake_requested`
    with no arguments -- baking needs the editor's dirty state and profile
    list, which only the host (MainWindow) has."""

    trim_changed = Signal(float, float, float)  # bass_db, treble_db, tilt_db_per_oct
    bake_requested = Signal()

    def __init__(self, theme: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self._bass_db = 0.0
        self._treble_db = 0.0
        self._tilt_db_per_oct = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        self._slider_bass, self._label_bass = self._add_row(
            layout, "Bass", tone_trim.MAX_SHELF_DB, "dB", "Bass Trim",
            "Boost or cut everything below the bass shelf "
            f"({tone_trim.BASS_SHELF_HZ:.0f} Hz) -- an instant taste "
            "adjustment that never touches the saved profile.")
        self._slider_treble, self._label_treble = self._add_row(
            layout, "Treble", tone_trim.MAX_SHELF_DB, "dB", "Treble Trim",
            "Boost or cut everything above the treble shelf "
            f"({tone_trim.TREBLE_SHELF_HZ:.0f} Hz) -- an instant taste "
            "adjustment that never touches the saved profile.")
        self._slider_tilt, self._label_tilt = self._add_row(
            layout, "Tilt", tone_trim.MAX_TILT_DB_PER_OCT, "dB/oct", "Tone Tilt",
            "Tilt the whole spectrum warmer or brighter around 1 kHz -- an "
            "instant taste adjustment that never touches the saved profile.")

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.btn_reset = QPushButton("Reset")
        self.btn_reset.clicked.connect(self._on_reset_clicked)
        describe(self.btn_reset, "Reset Tone Trim",
                 "Return bass, treble and tilt trim to flat.")
        buttons.addWidget(self.btn_reset)

        self.btn_bake = QPushButton("Bake into profile...")
        self.btn_bake.clicked.connect(self.bake_requested.emit)
        describe(self.btn_bake, "Bake Tone Trim Into Profile",
                 "Write the current trim into the active profile as real EQ "
                 "bands, then reset the trim to flat. Refused while there "
                 "are unsaved editor changes.")
        buttons.addWidget(self.btn_bake)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self._emit_trim_changed)

    def _add_row(self, layout: QVBoxLayout, name: str, max_value: float, unit: str,
                 a11y_name: str, a11y_text: str) -> tuple[QSlider, QLabel]:
        row = QHBoxLayout()
        row.setSpacing(6)
        name_label = QLabel(name)
        name_label.setFixedWidth(40)
        row.addWidget(name_label)
        slider = _make_slider(max_value)
        slider.valueChanged.connect(lambda _v: self._on_slider_changed())
        describe(slider, a11y_name, a11y_text)
        row.addWidget(slider, 1)
        value_label = QLabel(f"+0.0 {unit}")
        value_label.setFixedWidth(64)
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(value_label)
        layout.addLayout(row)
        return slider, value_label

    def _on_slider_changed(self) -> None:
        self._bass_db = self._slider_bass.value() / _STEPS_PER_DB
        self._treble_db = self._slider_treble.value() / _STEPS_PER_DB
        self._tilt_db_per_oct = self._slider_tilt.value() / _STEPS_PER_DB
        self._update_labels()
        self._timer.start()

    def _emit_trim_changed(self) -> None:
        self.trim_changed.emit(self._bass_db, self._treble_db, self._tilt_db_per_oct)

    def _on_reset_clicked(self) -> None:
        self._timer.stop()
        self.set_trim(0.0, 0.0, 0.0)
        self._emit_trim_changed()

    def _update_labels(self) -> None:
        self._label_bass.setText(f"{self._bass_db:+.1f} dB")
        self._label_treble.setText(f"{self._treble_db:+.1f} dB")
        self._label_tilt.setText(f"{self._tilt_db_per_oct:+.1f} dB/oct")

    def set_trim(self, bass_db: float, treble_db: float, tilt_db_per_oct: float) -> None:
        """Sync the sliders to externally-known state (e.g. on popover open,
        or after Reset/Bake) without re-emitting trim_changed."""
        self._bass_db, self._treble_db, self._tilt_db_per_oct = bass_db, treble_db, tilt_db_per_oct
        for slider, value in (
            (self._slider_bass, bass_db), (self._slider_treble, treble_db),
            (self._slider_tilt, tilt_db_per_oct),
        ):
            slider.blockSignals(True)
            slider.setValue(round(value * _STEPS_PER_DB))
            slider.blockSignals(False)
        self._update_labels()
