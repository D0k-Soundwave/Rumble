"""Linkwitz-Riley crossover-design tool (Rumble 4.10, RUMBLE_ACTION_GUIDE.md
Tier 4 #10) — matched high-pass/low-pass filter presets between a set of
main speakers and a subwoofer (or any two driver groups), with optional
all-pass phase alignment, previewed live and saved as a new profile via the
existing ProfileManager import path (same convention as autoeq_dialog.py's
`_save_profile`). Reuses `LP`/`HP`/`AP` filter primitives and `eq_math`'s
existing response math entirely as-is — the crossover math itself lives in
`rumble.core.crossover`; this module is UI/preset-assembly only.

Reachable from Tools -> Crossover Design…
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Signal

from rumble.core import apo_config, crossover, preamp_calc
from rumble.ui.a11y import describe
from rumble.ui.eq_graph import EQGraph
from rumble.ui.filter_row import NYQUIST_FRACTION
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH, FramelessDialog,
)

DEFAULT_FC_HZ = 80.0
SLOPE_CHOICES = [(24, "24 dB/octave (LR4)"), (48, "48 dB/octave (LR8)")]


class CrossoverDialog(FramelessDialog):
    """Design a Linkwitz-Riley crossover and save it as a new profile."""

    profile_created = Signal(str)

    def __init__(self, manager, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager
        self.theme = theme
        self._sample_rate = manager.effective_sample_rate()

        self.setWindowTitle("Crossover Design")
        self.setModal(True)
        self.setFixedSize(STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT + CHROME_STRIP_HEIGHT)

        # The form plus both filter-response graphs run taller than the
        # dialog's standard fixed height, so the whole body scrolls inside
        # the frame -- only the action bar at the bottom stays pinned.
        root = QVBoxLayout(self.body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        lay = QVBoxLayout(content)
        lay.setSpacing(14)
        lay.setContentsMargins(20, 20, 20, 20)

        intro = QLabel(
            "Build a matched high-pass/low-pass filter pair for a multi-way "
            "speaker + subwoofer setup, using Linkwitz-Riley cascaded-Butterworth "
            "presets. Save it as a profile with two channel sections: one for "
            "the main speakers (high-pass) and one for the subwoofer (low-pass).")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        lay.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(10)

        self.spn_fc = QDoubleSpinBox()
        self.spn_fc.setRange(20.0, self._sample_rate * NYQUIST_FRACTION)
        self.spn_fc.setDecimals(1)
        self.spn_fc.setSuffix(" Hz")
        self.spn_fc.setValue(DEFAULT_FC_HZ)
        describe(self.spn_fc, "Crossover frequency",
                 "The frequency where the main speakers hand off to the "
                 "subwoofer — the point where both branches are 6 dB down.")
        form.addRow("Crossover frequency", self.spn_fc)

        self.cmb_slope = QComboBox()
        for value, label in SLOPE_CHOICES:
            self.cmb_slope.addItem(label, value)
        describe(self.cmb_slope, "Crossover slope",
                 "Steeper slopes (48 dB/octave) roll off faster past the "
                 "crossover point, reducing driver overlap at the cost of "
                 "needing more filter bands.")
        form.addRow("Slope", self.cmb_slope)

        self.txt_main_channel = QLineEdit("L R")
        describe(self.txt_main_channel, "Main speakers channel(s)",
                 "The Channel: value for the high-pass (main speaker) section, "
                 "e.g. \"L R\" for a linked stereo pair.")
        form.addRow("Main speakers channel(s)", self.txt_main_channel)

        self.txt_sub_channel = QLineEdit("LFE")
        describe(self.txt_sub_channel, "Subwoofer channel",
                 "The Channel: value for the low-pass (subwoofer) section.")
        form.addRow("Subwoofer channel", self.txt_sub_channel)

        self.txt_name = QLineEdit()
        describe(self.txt_name, "Profile name",
                 "Name for the new profile this tool will create.")
        form.addRow("Profile name", self.txt_name)

        lay.addLayout(form)

        self.chk_allpass = QCheckBox(
            "Add all-pass phase alignment to the main speakers")
        describe(self.chk_allpass, "All-pass phase alignment",
                 "Adds an all-pass filter cascade (same frequency and Q as the "
                 "crossover) to the main-speaker branch, to correct the phase "
                 "difference introduced when the subwoofer's own built-in "
                 "low-pass filter can't be edited here directly.")
        lay.addWidget(self.chk_allpass)

        lay.addWidget(QLabel("Main speakers (high-pass):"))
        self.graph_hp = EQGraph(theme)
        self.graph_hp.setMinimumHeight(160)
        lay.addWidget(self.graph_hp)

        lay.addWidget(QLabel("Subwoofer (low-pass):"))
        self.graph_lp = EQGraph(theme)
        self.graph_lp.setMinimumHeight(160)
        lay.addWidget(self.graph_lp)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setProperty("class", "secondary")
        lay.addWidget(self.status)

        # Pinned outside the scroll area, always visible.
        buttons = QDialogButtonBox()
        self.btn_create = buttons.addButton(
            "Create Profile", QDialogButtonBox.ButtonRole.AcceptRole)
        describe(self.btn_create, "Create Profile",
                 "Save this crossover design as a new profile.")
        buttons.accepted.connect(self._on_create)
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 12, 20, 20)
        footer.addWidget(buttons)
        root.addLayout(footer)

        self.spn_fc.valueChanged.connect(self._update_preview)
        self.cmb_slope.currentIndexChanged.connect(self._update_preview)
        self.chk_allpass.toggled.connect(self._update_preview)
        self._update_preview()

    # ── preview ──────────────────────────────────────────────────────

    def _current_design(self) -> crossover.CrossoverDesign:
        fc = self.spn_fc.value()
        slope = self.cmb_slope.currentData()
        return crossover.design_crossover(
            fc, slope, sample_rate=self._sample_rate,
            allpass_alignment=self.chk_allpass.isChecked())

    def _update_preview(self) -> None:
        design = self._current_design()
        main_filters = design.highpass_filters + design.allpass_filters
        self.graph_hp.set_filters(main_filters, self._sample_rate)
        self.graph_lp.set_filters(design.lowpass_filters, self._sample_rate)
        if not self.txt_name.text().strip():
            slope = self.cmb_slope.currentData()
            self.txt_name.setPlaceholderText(
                f"Crossover {design.fc:.0f} Hz LR{slope // 12}")

    # ── save ─────────────────────────────────────────────────────────

    def _on_create(self) -> None:
        design = self._current_design()
        main_filters = design.highpass_filters + design.allpass_filters
        sub_filters = design.lowpass_filters

        main_channel = self.txt_main_channel.text().strip() or "L R"
        sub_channel = self.txt_sub_channel.text().strip() or "LFE"

        main_section = apo_config.ChannelSection(
            channel=main_channel, filters=main_filters,
            preamp_db=preamp_calc.calculate_auto_preamp(main_filters, self._sample_rate))
        sub_section = apo_config.ChannelSection(
            channel=sub_channel, filters=sub_filters,
            preamp_db=preamp_calc.calculate_auto_preamp(sub_filters, self._sample_rate))

        config = apo_config.ProfileConfig(
            device="", preamp=0.0,
            channel_sections=[main_section, sub_section])

        slope = self.cmb_slope.currentData()
        name = self.txt_name.text().strip() or f"Crossover {design.fc:.0f} Hz LR{slope // 12}"
        description = (
            f"Linkwitz-Riley crossover at {design.fc:.0f} Hz, {slope} dB/octave "
            f"({main_channel} high-pass / {sub_channel} low-pass)."
            + (" Includes all-pass phase alignment on the main speakers."
               if design.allpass_filters else ""))

        meta = self.manager.create_profile(
            name, description=description, tags=["Crossover"], config=config)
        if meta is not None:
            self.profile_created.emit(meta.id)
            self.status.setText(f"Created profile “{meta.name}”.")
        else:
            self.status.setText("Could not create the profile — check the name and try again.")
