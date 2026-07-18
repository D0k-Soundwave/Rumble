"""Bass-management wizard (Rumble Campaign D, D3b).

Builds crossover/LFE-routing profile content via
``rumble.core.bass_management.build_bass_management()`` and applies it into
the currently-loaded editor for the active profile through the normal
dirty/save flow -- this is profile CONTENT, not a render layer (the same
distinction bass_management.py's own module docstring draws). Reachable
from Tools -> Bass Management…, enabled only when the active profile's
layout includes the sub/LFE channel (``rumble.ui.editor_panel.
layout_channel_tokens``/``detect_preset`` -- the same channel-preset
detection the editor's own preset combo uses, factored into free functions
this session so this dialog and MainWindow's menu-enablement check can both
reuse it without a live EditorPanel instance).

Page flow: layout confirm -> crossover -> alignment -> trims -> distances ->
preview (live ``manager.preview_config()``) -> Apply, which emits
``apply_requested(profile_id, ProfileConfig)`` for MainWindow to load into
the editor as a dirty, unsaved change (mirrors ``_on_bake_tone_trim`` --
the dialog itself never touches editor_panel).

"Use measured distances" (the roadmap's D3 "later synergy" note) is NOT
wired in this session. Per the S23 -> S24 Amendment: this wizard has no
Campaign C measurement data to reach for -- measure_dialog.py stores only
single-position frequency-response curves, never multi-channel timing
data. (The Amendment's second blocker -- the S23 [REVIEW] sign-bug finding
in ``measure_channel_delays()``'s rebase direction -- was confirmed and
fixed 2026-07-13: it now rebases to the latest arrival, so its output is
safe to feed into Delay: lines. Note the editor's own Measure Delay button
(editor_panel.py ``_on_measure_delay_clicked``) has always called
``run_measurement()`` directly, contrary to the Amendment's "not called
from any shipped UI" claim.) The distances page therefore offers manual
cm/ms entry only, with the "use measured distances" affordance shown
disabled and an explanatory label; see the progress file's Follow-ups for
the open item this leaves.
"""
from __future__ import annotations

from dataclasses import replace as dataclass_replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout, QWidget,
)

from rumble.core import bass_management
from rumble.core.apo_config import ProfileConfig
from rumble.core.time_alignment import STANDARD_CHANNEL_ORDER
from rumble.ui.a11y import describe
from rumble.ui.editor_panel import CHANNEL_PRESETS, detect_preset, layout_channel_tokens
from rumble.ui.eq_graph import EQGraph
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH, FramelessDialog,
)

#: THX's published subwoofer-crossover convention (THX certification
#: standard, since the 1990s) -- the practical range either side is generous
#: enough for real speaker/sub pairings without wandering into frequencies
#: neither a "sub" nor a "main" crossover point in practice.
DEFAULT_XOVER_HZ = 80.0
MIN_XOVER_HZ = 40.0
MAX_XOVER_HZ = 160.0

ALIGNMENT_CHOICES = [
    ("LR2", "LR2 -- 12 dB/octave (one cascaded section)"),
    ("LR4", "LR4 -- 24 dB/octave (two cascaded sections)"),
]

TRIM_RANGE_DB = 12.0
#: Generously past any realistic room -- 20 m.
MAX_DISTANCE_CM = 2000.0
#: MAX_DISTANCE_CM expressed in ms, for the ms-unit spinbox range.
MAX_DISTANCE_MS = MAX_DISTANCE_CM / (bass_management.SPEED_OF_SOUND_M_PER_S * 100.0 / 1000.0)

DISTANCE_UNITS = [("cm", "Centimetres"), ("ms", "Milliseconds")]


def _ordered_layout(tokens: set[str]) -> list[str]:
    """*tokens* (from layout_channel_tokens) in a stable, physically
    sensible channel order: the matching CHANNEL_PRESETS entry's own order
    if there is one, else STANDARD_CHANNEL_ORDER's physical-channel order
    with any unrecognised tokens appended at the end."""
    for channels in CHANNEL_PRESETS.values():
        if {c.upper() for c in channels} == tokens:
            return list(channels)
    ordered = [c for c in STANDARD_CHANNEL_ORDER if c in tokens]
    ordered += sorted(tokens - set(ordered))
    return ordered


class BassManagementDialog(FramelessDialog):
    """Configure crossover/LFE routing/trims/distances for a sub/LFE-
    equipped layout and apply the result into the editor."""

    apply_requested = Signal(str, object)  # profile_id, ProfileConfig

    def __init__(self, manager, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager
        self.theme = theme
        self._sample_rate = manager.effective_sample_rate()
        self._previewing = False
        self._prior_bypass = False

        self.setWindowTitle("Bass Management")
        self.setModal(True)
        self.setFixedSize(STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT + CHROME_STRIP_HEIGHT)

        self._active_meta = manager.get_active_profile()
        self._config: ProfileConfig | None = (
            manager.load_profile_config(self._active_meta.id)
            if self._active_meta is not None else None)
        tokens = (layout_channel_tokens(self._config.channel_sections)
                  if self._config is not None else set())
        self._layout = _ordered_layout(tokens) if tokens else []
        self._mains = [c for c in self._layout if c != bass_management.SUB_CHANNEL]

        root = QVBoxLayout(self.body)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        if (self._config is None or self._active_meta is None
                or bass_management.SUB_CHANNEL not in self._layout or not self._mains):
            root.addWidget(self._build_unavailable_page())
            return

        from PySide6.QtWidgets import QStackedWidget
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.layout_page = self._build_layout_page()
        self.crossover_page = self._build_crossover_page()
        self.alignment_page = self._build_alignment_page()
        self.trims_page = self._build_trims_page()
        self.distances_page = self._build_distances_page()
        self.preview_page = self._build_preview_page()
        for page in (self.layout_page, self.crossover_page, self.alignment_page,
                     self.trims_page, self.distances_page, self.preview_page):
            self.stack.addWidget(page)
        self.stack.setCurrentWidget(self.layout_page)

    # ── unavailable fallback (defensive -- the Tools menu action is
    # disabled in this state, but a directly-constructed dialog, e.g. from
    # a test, should still degrade gracefully rather than crash) ─────────

    def _build_unavailable_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel(
            "Bass management needs an active profile whose layout includes "
            "a sub/LFE channel (Stereo + Sub, Surround + Sub, or a 5.1/7.1 "
            "layout). Activate one and try again.")
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    # ── shared nav helper ─────────────────────────────────────────────

    def _nav_row(self, back_slot, next_label: str, next_slot) -> QHBoxLayout:
        row = QHBoxLayout()
        if back_slot is not None:
            btn_back = QPushButton("Back")
            btn_back.clicked.connect(back_slot)
            row.addWidget(btn_back)
        else:
            btn_cancel = QPushButton("Cancel")
            btn_cancel.clicked.connect(self.reject)
            row.addWidget(btn_cancel)
        row.addStretch(1)
        btn_next = QPushButton(next_label)
        btn_next.clicked.connect(next_slot)
        row.addWidget(btn_next)
        return row

    def _go_to(self, page: QWidget) -> None:
        self.stack.setCurrentWidget(page)

    # ── page 1: layout confirm ────────────────────────────────────────

    def _build_layout_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        preset_name = detect_preset(self._config.channel_sections) or "Custom layout"
        title = QLabel(f"Detected layout: {preset_name}")
        title.setProperty("class", "heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("Channels: " + ", ".join(self._layout)))
        info = QLabel(
            "This wizard rebuilds the crossover and LFE-routing content for "
            "every channel above -- any existing filters on these channels "
            "will be replaced. Nothing is written until you Apply, and "
            "nothing is saved until you click Save afterward.")
        info.setWordWrap(True)
        info.setProperty("class", "secondary")
        layout.addWidget(info)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(None, "Next", lambda: self._go_to(self.crossover_page)))
        return page

    # ── page 2: crossover frequency ───────────────────────────────────

    def _build_crossover_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Crossover frequency"))
        form = QFormLayout()
        self.spn_xover = QDoubleSpinBox()
        self.spn_xover.setRange(MIN_XOVER_HZ, MAX_XOVER_HZ)
        self.spn_xover.setDecimals(0)
        self.spn_xover.setSuffix(" Hz")
        self.spn_xover.setValue(DEFAULT_XOVER_HZ)
        describe(self.spn_xover, "Crossover frequency",
                 "The frequency where the main speakers hand off to the "
                 "sub -- 80 Hz is THX's published convention and this "
                 "wizard's default; the practical range is 40-160 Hz.")
        form.addRow("Crossover frequency", self.spn_xover)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(
            lambda: self._go_to(self.layout_page), "Next",
            lambda: self._go_to(self.alignment_page)))
        return page

    # ── page 3: alignment ─────────────────────────────────────────────

    def _build_alignment_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Crossover alignment"))
        form = QFormLayout()
        self.cmb_alignment = QComboBox()
        for value, label in ALIGNMENT_CHOICES:
            self.cmb_alignment.addItem(label, value)
        describe(self.cmb_alignment, "Crossover alignment",
                 "Linkwitz-Riley alignment -- both choices sum flat across "
                 "the crossover; LR4's steeper 24 dB/octave slope reduces "
                 "driver overlap at the cost of one extra cascaded section "
                 "per channel.")
        form.addRow("Alignment", self.cmb_alignment)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(
            lambda: self._go_to(self.crossover_page), "Next",
            lambda: self._go_to(self.trims_page)))
        return page

    # ── page 4: trims ─────────────────────────────────────────────────

    def _build_trims_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Per-channel level trims"))
        form = QFormLayout()
        self._trim_spins: dict[str, QDoubleSpinBox] = {}
        for ch in self._layout:
            spin = QDoubleSpinBox()
            spin.setRange(-TRIM_RANGE_DB, TRIM_RANGE_DB)
            spin.setDecimals(1)
            spin.setSuffix(" dB")
            spin.setValue(0.0)
            describe(spin, f"{ch} trim",
                     f"Level trim for the {ch} channel, applied as its own "
                     "Preamp: line on top of the profile-level safety preamp.")
            form.addRow(f"{ch} trim", spin)
            self._trim_spins[ch] = spin
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(
            lambda: self._go_to(self.alignment_page), "Next",
            lambda: self._go_to(self.distances_page)))
        return page

    # ── page 5: distances ─────────────────────────────────────────────

    def _build_distances_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Distance-based delay alignment (optional)"))
        intro = QLabel(
            "Enter each speaker's distance from the listening position to "
            "delay closer channels so all channels arrive together. Leave a "
            "channel unchecked to give it no Delay: line at all.")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        layout.addWidget(intro)

        unit_row = QHBoxLayout()
        unit_row.addWidget(QLabel("Units"))
        self.cmb_distance_unit = QComboBox()
        for value, label in DISTANCE_UNITS:
            self.cmb_distance_unit.addItem(label, value)
        describe(self.cmb_distance_unit, "Distance units",
                 "Whether the distance fields below are centimetres or "
                 "milliseconds of transit time.")
        self.cmb_distance_unit.currentIndexChanged.connect(self._on_distance_unit_changed)
        unit_row.addWidget(self.cmb_distance_unit)
        unit_row.addStretch(1)
        layout.addLayout(unit_row)

        self.btn_use_measured_distances = QPushButton("Use measured distances")
        self.btn_use_measured_distances.setEnabled(False)
        describe(self.btn_use_measured_distances, "Use measured distances",
                 "Not available yet -- no stored multi-channel timing "
                 "measurement exists to reuse (Campaign C's room-measurement "
                 "wizard records a single-position frequency curve, not "
                 "per-channel arrival timing). Enter distances manually "
                 "below instead.")
        layout.addWidget(self.btn_use_measured_distances)

        form = QFormLayout()
        self._distance_checks: dict[str, QCheckBox] = {}
        self._distance_spins: dict[str, QDoubleSpinBox] = {}
        for ch in self._layout:
            row = QHBoxLayout()
            chk = QCheckBox("Measured")
            describe(chk, f"{ch} distance entered",
                     f"Whether a distance has been entered for {ch} -- "
                     "unchecked means no Delay: line for this channel.")
            spin = QDoubleSpinBox()
            spin.setRange(0.0, MAX_DISTANCE_CM)
            spin.setDecimals(1)
            spin.setSuffix(" cm")
            spin.setEnabled(False)
            chk.toggled.connect(spin.setEnabled)
            describe(spin, f"{ch} distance",
                     f"Distance from the listening position to the {ch} "
                     "speaker.")
            row.addWidget(chk)
            row.addWidget(spin)
            form.addRow(f"{ch} distance", row)
            self._distance_checks[ch] = chk
            self._distance_spins[ch] = spin
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(
            lambda: self._go_to(self.trims_page), "Next",
            self._on_distances_next))
        return page

    def _on_distance_unit_changed(self) -> None:
        unit = self.cmb_distance_unit.currentData()
        suffix = " cm" if unit == "cm" else " ms"
        max_value = MAX_DISTANCE_CM if unit == "cm" else MAX_DISTANCE_MS
        for spin in self._distance_spins.values():
            spin.setSuffix(suffix)
            spin.setRange(0.0, max_value)

    def _distances_cm(self) -> dict[str, float]:
        unit = self.cmb_distance_unit.currentData()
        result: dict[str, float] = {}
        for ch, chk in self._distance_checks.items():
            if not chk.isChecked():
                continue
            value = self._distance_spins[ch].value()
            if unit == "ms":
                value = value * bass_management.SPEED_OF_SOUND_M_PER_S * 100.0 / 1000.0
            result[ch] = value
        return result

    def _on_distances_next(self) -> None:
        self._go_to(self.preview_page)
        self._refresh_preview()
        self._start_preview()

    # ── page 6: preview ───────────────────────────────────────────────

    def _build_preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Preview"))
        self.lbl_preview_summary = QLabel("")
        self.lbl_preview_summary.setWordWrap(True)
        layout.addWidget(self.lbl_preview_summary)

        layout.addWidget(QLabel("Main channels (high-pass):"))
        self.graph_hp = EQGraph(self.theme)
        self.graph_hp.setMinimumHeight(140)
        layout.addWidget(self.graph_hp)

        layout.addWidget(QLabel(f"{bass_management.SUB_CHANNEL} (low-pass):"))
        self.graph_lp = EQGraph(self.theme)
        self.graph_lp.setMinimumHeight(140)
        layout.addWidget(self.graph_lp)

        nav = QHBoxLayout()
        btn_back = QPushButton("Back")
        btn_back.clicked.connect(self._on_preview_back)
        nav.addWidget(btn_back)
        nav.addStretch(1)
        btn_apply = QPushButton("Apply to Profile")
        describe(btn_apply, "Apply to Profile",
                 "Load the generated bass-management sections into the "
                 "editor as an unsaved change -- click Save afterward to "
                 "keep it.")
        btn_apply.clicked.connect(self._on_apply)
        nav.addWidget(btn_apply)
        layout.addLayout(nav)
        return page

    def _build_result(self) -> bass_management.BassManagementResult:
        return bass_management.build_bass_management(
            layout=self._layout,
            xover_hz=self.spn_xover.value(),
            alignment=self.cmb_alignment.currentData(),
            sub_trim_db=self._trim_spins[bass_management.SUB_CHANNEL].value(),
            distances=self._distances_cm() or None,
            main_trims_db={ch: self._trim_spins[ch].value() for ch in self._mains},
            sample_rate=self._sample_rate,
        )

    def _refresh_preview(self) -> None:
        result = self._build_result()
        sections = {s.channel: s for s in result.channel_sections}
        main_filters = sections[self._mains[0]].filters
        sub_filters = sections[bass_management.SUB_CHANNEL].filters
        self.graph_hp.set_filters(main_filters, self._sample_rate)
        self.graph_lp.set_filters(sub_filters, self._sample_rate)
        copy_line = result.copy_lines[0] if result.copy_lines else "(none)"
        alignment = self.cmb_alignment.currentData()
        self.lbl_preview_summary.setText(
            f"Crossover: {self.spn_xover.value():.0f} Hz, alignment {alignment}\n"
            f"Sub feed: {copy_line}\n"
            f"Profile preamp: {result.preamp_db:.1f} dB (safety margin included)")

    def _merged_config(self) -> ProfileConfig:
        result = self._build_result()
        existing_copy_lines = [
            c for c in self._config.copy_lines
            if not c.upper().startswith(f"{bass_management.SUB_CHANNEL.upper()}=")
        ]
        return dataclass_replace(
            self._config,
            channel_sections=result.channel_sections,
            copy_lines=existing_copy_lines + result.copy_lines,
            preamp=result.preamp_db,
        )

    # ── live preview lifecycle ────────────────────────────────────────

    def _start_preview(self) -> None:
        if self._previewing:
            return
        config = self._merged_config()
        prior_bypass = self.manager.is_bypassed()
        self.manager.set_bypass(False)
        if self.manager.preview_config(config):
            self._previewing = True
            self._prior_bypass = prior_bypass
        else:
            self.manager.set_bypass(prior_bypass)

    def _end_preview(self) -> None:
        if not self._previewing:
            return
        self.manager.end_preview()
        self.manager.set_bypass(self._prior_bypass)
        self._previewing = False

    def _on_preview_back(self) -> None:
        self._end_preview()
        self._go_to(self.distances_page)

    def _on_apply(self) -> None:
        config = self._merged_config()
        self._end_preview()
        self.apply_requested.emit(self._active_meta.id, config)
        self.accept()

    # ── exit paths always restore preview/bypass ──────────────────────

    def _teardown(self) -> None:
        self._end_preview()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._teardown()
        super().closeEvent(event)

    def reject(self) -> None:
        self._teardown()
        super().reject()

    def accept(self) -> None:
        self._teardown()
        super().accept()
