"""Right panel: profile header, controls, and a tabbed editing surface —
EQ editor (graph + filter table), Advanced, and Raw Config. The EQ editor
tab is sized to fit on screen without vertical scrolling."""
from __future__ import annotations

import copy
import threading
from dataclasses import replace as dataclass_replace

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QMessageBox, QPushButton, QScrollArea, QStackedWidget,
    QStyle, QStyleOptionComboBox, QStylePainter, QVBoxLayout, QWidget,
)

# Sanity bounds for the band strip height (derived from FilterRow's real
# size hint so no control gets clipped — vertical scrolling is disabled).
_BAND_STRIP_MIN_HEIGHT = 140
_BAND_STRIP_MAX_HEIGHT = 330

# The profile-status LED's only text. The label reserves this string's width
# permanently (see EditorPanel._reserve_led_width), so the two must stay the
# same string or the reservation stops matching what gets painted.
_LED_LIT_TEXT = "●  ACTIVE"

import numpy as np

from rumble.core import autoeq_merge, eq_math, ir_analysis, preamp_calc, time_alignment
from rumble.core.apo_config import MAX_BANDS, ChannelSection, Filter, ProfileConfig
from rumble.core.audio.engine import ToneEngine
from rumble.core.geq_peq_convert import fit_peq
from rumble.core.profile_manager import ProfileMeta
from rumble.ui import icons
from rumble.ui.a11y import describe, wrap_tooltip
from rumble.ui.advanced_panel import AdvancedPanel
from rumble.ui.density import dp
from rumble.ui.editable_marquee import EditableMarqueeField
from rumble.ui.eq_graph import EQGraph
from rumble.ui.filter_row import BandGridHeader, FilterRow, row_h_field
from rumble.ui.hud_frame import HudFrame
from rumble.ui.raw_config_view import RawConfigView
from rumble.ui.undo_history import EditorSnapshot, UndoHistory
from rumble.ui.vu_meter import VuMeterWidget

# MAX_BANDS is re-exported from rumble.core.apo_config (where its rationale is
# documented) so the long-standing `from rumble.ui.editor_panel import MAX_BANDS`
# importers — e.g. new_profile_dialog — keep working unchanged.

# Ordered channel names for each common speaker layout.
#
# Deliberately a curated consumer-facing list, NOT a mirror of Equalizer
# APO's own official "Channel configurations" table (Configuration reference
# wiki, "Channel (since version 0.8)" section, checked 2026-07-10). That
# table's rows are Mono, Stereo, Quadraphonic, Surround (L,R,C,RC — a
# different 4-channel layout than the one below), 5.1 Surround (listed
# TWICE, as two equally-official identifier variants: L,R,C,LFE,SL,SR *and*
# L,R,C,LFE,RL,RR for the same 6-channel positions), and 7.1 Surround. Real
# per-entry deviations, confirmed against that source and kept anyway:
#   - "Surround (4.0)" below uses APO's "Quadraphonic" identifiers (L,R,RL,RR)
#     under a friendlier name — APO's own "Surround" name means a different
#     4-channel layout (L,R,C,RC), which would confuse users expecting a
#     basic 4-speaker setup.
#   - "5.1 Surround" below picks the RL/RR identifier variant (not SL/SR).
#     Both are equally valid per APO's table for the same physical channels;
#     RL/RR was picked as the more universally recognized consumer term.
#   - "2.1" and "4.1" have NO official row in APO's table at all (APO's own
#     table jumps from Quadraphonic straight to 5.1/7.1) — they're RUMBLE
#     convenience combos (Quadraphonic/Stereo + LFE). Per APO's own docs,
#     "If a channel configuration is not supported, channels can only be
#     selected by number" — so on real hardware whose Windows-reported
#     format APO doesn't recognize as one of its named configurations, the
#     "LFE" identifier specifically may not resolve to any channel. Renaming
#     these two presets' channels to bare numeric indices was considered and
#     rejected: several other features (multi-listener correction in
#     listener_correction.py, the LFE-only-bass-filter warning in
#     main_window.py, time_alignment.py's STANDARD_CHANNEL_ORDER) pattern-
#     match the literal "L"/"R"/"LFE" strings, so silently swapping them for
#     numbers would break those working features for anyone using these two
#     presets. Instead, NON_STANDARD_CHANNEL_LAYOUTS below drives a one-time
#     notice at the point of selection (new_profile_dialog.py's inline hint,
#     this editor's preset-change notice) so the user can verify the LFE
#     channel is actually reaching their hardware rather than discovering a
#     silent no-op later.
CHANNEL_PRESETS: dict[str, list[str]] = {
    "Stereo / Headphones":     ["L", "R"],
    "Stereo + Sub (2.1)":      ["L", "R", "LFE"],
    "Surround (4.0)":          ["L", "R", "RL", "RR"],
    "Surround + Sub (4.1)":    ["L", "R", "RL", "RR", "LFE"],
    "5.1 Surround":            ["L", "R", "C", "LFE", "RL", "RR"],
    "7.1 Surround":            ["L", "R", "C", "LFE", "RL", "RR", "SL", "SR"],
}

# Presets with no corresponding row in APO's official channel-configuration
# table (see the comment above) — their LFE identifier is not guaranteed to
# resolve on hardware APO doesn't recognize as one of its six named
# configurations.
NON_STANDARD_CHANNEL_LAYOUTS = {"Stereo + Sub (2.1)", "Surround + Sub (4.1)"}
NON_STANDARD_CHANNEL_LAYOUT_WARNING = (
    'Equalizer APO has no official "{name}" channel configuration — it only '
    "defines the LFE identifier for 5.1/7.1 setups. If your device isn't "
    "recognized by APO as one of those, the LFE section may not receive any "
    "audio. Worth confirming with a test tone after saving."
)

# Extra preset-combo entry that prompts for arbitrary channel names.
CUSTOM_LAYOUT_LABEL = "Custom…"


def layout_channel_tokens(channel_sections: list[ChannelSection]) -> set[str]:
    """The set of individual output channels *channel_sections* targets.

    A single Channel: block may name several channels at once ("L R") or
    use APO's catch-all ("all"); both are expanded so a profile matches a
    CHANNEL_PRESETS layout however it happens to group its sections. Free
    function (not tied to a live EditorPanel instance) so other tools --
    e.g. bass_management_dialog.py's Tools-menu enablement and layout
    detection -- can reuse the same detection against an arbitrary
    ProfileConfig without needing it loaded into the editor first."""
    tokens: set[str] = set()
    for section in channel_sections:
        tokens.update(tok.upper() for tok in section.channel.split())
    # A lone "all" section is APO's catch-all — for an EQ that is the
    # standard stereo pair (L + R).
    if tokens == {"ALL"}:
        return {"L", "R"}
    tokens.discard("ALL")
    return tokens


def detect_preset(channel_sections: list[ChannelSection]) -> str:
    """The CHANNEL_PRESETS name matching *channel_sections*, or "" when the
    layout is not one of the presets (e.g. an imported config).

    Matching is by the *set* of channels covered, not the literal section
    list, so the same layout is recognised whether the profile stores it
    as one combined section ("Channel: L R"), one section per channel
    ("Channel: L" + "Channel: R"), or the "all" catch-all."""
    tokens = layout_channel_tokens(channel_sections)
    if not tokens:
        return ""
    for name, preset_channels in CHANNEL_PRESETS.items():
        if {c.upper() for c in preset_channels} == tokens:
            return name
    return ""

# Default headroom-label tooltip (calculated success/error state). Phase 6
# Task 4's measured-clipping override temporarily replaces this with a
# different tooltip; _update_headroom_label() restores this one once
# measured clipping clears — see that method.
_HEADROOM_TOOLTIP = ("How much margin remains before the EQ output clips at 0 dB. "
                      "Positive is safe; negative warns of possible distortion.")

# Screen-reader text for the measured-clipping override state (see
# _update_headroom_label()'s clipping-override branch below). Mirrors
# a11y.describe()'s convention of keeping the accessible description as a
# plain, unwrapped sentence distinct from the (possibly wrapped) tooltip.
_HEADROOM_CLIPPING_DESCRIPTION = (
    "Measured clipping: the Analyser has measured sustained real output "
    "peaks at or above 0 dBFS, overriding the calculated headroom "
    "estimate below.")

# Rumble 3.7: "measured drift" indicator — distinct from the headroom
# label's clipping override above (that's about output LEVEL; this is about
# the measured spectrum's SHAPE no longer tracking the calculated EQ curve
# at all, e.g. a competing device-level DSP, a muted/bypassed EQ, or the
# wrong output device selected).
_DRIFT_TOOLTIP = ("The Analyser's measured output doesn't correlate "
                   "with this profile's calculated EQ curve. Possible causes: another "
                   "app or driver-level enhancement is also processing the audio, "
                   "Equalizer APO is bypassed for the current device, or the wrong "
                   "output device is selected.")
_DRIFT_DESCRIPTION = ("Measured drift: the Analyser's measured output "
                       "has not tracked this profile's calculated EQ curve for a "
                       "sustained period.")


class CenteredCombo(QComboBox):
    """QComboBox that paints its closed text centred in the edit field so the
    value lines up with the centre-aligned entry fields around it (the popup
    list is unchanged)."""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        opt.currentText = ""
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.setPen(opt.palette.color(QPalette.ColorRole.ButtonText))
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt,
            QStyle.SubControl.SC_ComboBoxEditField, self)
        painter.drawText(field, Qt.AlignmentFlag.AlignCenter, self.currentText())


class _SectionButton(QPushButton):
    """Channel section tab that also reports double-clicks (rename)."""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class EditorPanel(QWidget):
    """The full right-hand profile editor."""

    save_requested = Signal()
    bypass_requested = Signal(bool)
    clear_requested = Signal()
    # A/B compare toggle: True = hold the saved profile audible, parking the
    # live preview of unsaved edits; False = resume the edited preview.
    ab_compare_toggled = Signal(bool)
    dirty = Signal()
    # Like dirty (refresh + push the live preview) but the change is a
    # per-session control, not a tracked edit — it must not raise the
    # unsaved-changes warning. Used by the Pre-Amp on/off and Auto toggles.
    preview_only = Signal()
    # (can_undo, can_redo) whenever the snapshot history moves. Undo/redo are
    # keyboard-only, so this exists for the owner's Edit-menu items.
    history_changed = Signal(bool, bool)
    # The right-side VU/Peak meter's own settings (currently just its
    # ballistics mode) — {"meter_mode"} — for the caller to persist, the same
    # pattern as EQGraph.spectrum_settings_changed.
    meter_settings_changed = Signal(dict)

    def __init__(self, theme: dict, parent: QWidget | None = None,
                 tone_engine_factory=None):
        super().__init__(parent)
        self.theme = theme
        # Injectable per the project's stream_factory/record_factory DI
        # convention (see rumble/core/time_alignment.py) so tests can swap in
        # a fake engine for _on_measure_delay_clicked without real hardware.
        self._tone_engine_factory = tone_engine_factory or ToneEngine
        self._meta: ProfileMeta | None = None
        self._loading = False
        # APO `Device:` patterns whose endpoint is a capture (microphone) flow
        # — they have no WASAPI loopback tap for the live spectrum to read.
        # Filled by _populate_device_combo(); declared here so the graph-sync
        # path can never read it before the combo is first populated.
        self._capture_only_patterns: set[str] = set()
        self._filter_rows: list[FilterRow] = []
        self._channel_sections: list[ChannelSection] = [ChannelSection("L"), ChannelSection("R")]
        self._current_section_idx: int = 0
        self._section_btns: list[QPushButton] = []
        self._preamp_fallback: float = 0.0
        self._preamp_saved: float = 0.0
        # The config the loaded profile parsed to — build_config() starts from
        # a deep copy of it so fields the editor does not own (extra_lines,
        # later Stage lines, …) round-trip instead of being dropped.
        self._loaded_config: ProfileConfig | None = None
        # Spin value the current section's delay loaded as (post-quantisation):
        # while the spin still reads exactly this, the stored delay (possibly
        # in samples) is left untouched; only a real edit rewrites it in ms.
        self._delay_loaded_value: float = 0.0
        self._sample_rate: int = eq_math.DEFAULT_SAMPLE_RATE
        # Convolution IR analysis: the resolver (injected by MainWindow) maps
        # a Convolution: value to an on-disk file; the result feeds the graph
        # overlay and the auto-preamp/headroom maths.
        self._ir_resolver = None  # Callable[[str], Path | None]
        self._ir_result: ir_analysis.IRResult | None = None
        self._listener_correction_provider = None  # Callable[[], EarCorrection] | None
        # Index of the soloed band in the current section, None when no solo.
        # Solo is an audition: it masks bands in the graph and live preview
        # only — stored Filter.enabled flags are never touched.
        self._solo_index: int | None = None
        # Phase 6 Task 4: sustained "measured clipping" flag fed by the live
        # spectrum analyzer's SpectrumController (via EQGraph), independent
        # of the calculated preamp_calc headroom math below — see
        # _on_measured_clipping()/_update_headroom_label().
        self._measured_clipping: bool = False
        # Rumble 3.7: sustained "measured drift" flag from the same live
        # spectrum analyzer — see _on_measured_drift()/lbl_drift below.
        self._measured_drift: bool = False

        # Undo/redo: whole-state snapshots with a commit debounce, so a burst
        # of edits (a spinbox drag, a graph drag) collapses into one step.
        self._history = UndoHistory()
        self._undo_commit_timer = QTimer(self)
        self._undo_commit_timer.setSingleShot(True)
        self._undo_commit_timer.setInterval(300)
        self._undo_commit_timer.timeout.connect(self._commit_undo_snapshot)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header_bar())
        root.addWidget(self._build_controls_row())

        # Flat divided tab row — plain checkable buttons over a QStackedWidget
        # instead of a QTabWidget, whose native selected tab paints upward over
        # the divider line above it.
        self._main_tab_buttons: list[QPushButton] = []
        self._tab_row = QFrame()
        # Named so _restyle_main_tab_row can target the row itself — a bare
        # QFrame selector would also style the divider QFrame inside it.
        self._tab_row.setObjectName("mainTabRow")
        self._tab_row.setFixedHeight(dp(32, 22))
        tab_row_layout = QHBoxLayout(self._tab_row)
        tab_row_layout.setContentsMargins(dp(16, 8), 0, dp(16, 8), 0)
        tab_row_layout.setSpacing(2)
        tab_summaries = (
            "Show the main EQ editor: the frequency response graph and the filter bands.",
            "Show advanced tools: Graphic EQ curve, convolution, VST plugins, "
            "channel routing and conditional rules.",
            "Show the exact Equalizer APO configuration text this profile will write.",
        )
        for i, (label, summary) in enumerate(zip(("EQ Editor", "Advanced", "Raw Config"),
                                                 tab_summaries)):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(dp(32, 22))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, idx=i: self._switch_main_tab(idx))
            describe(btn, f"{label} tab", summary)
            tab_row_layout.addWidget(btn)
            self._main_tab_buttons.append(btn)
        tab_row_layout.addStretch(1)
        root.addWidget(self._tab_row)

        editor_tab = QWidget()
        editor_layout = QVBoxLayout(editor_tab)
        editor_layout.setContentsMargins(0, 0, 0, dp(10, 4))
        editor_layout.setSpacing(dp(8, 2))
        editor_layout.addWidget(self._build_graph_section(), 1)
        editor_layout.addWidget(self._build_filter_table_section())

        # The Analyzer toggle + its options gear are built and wired inside
        # EQGraph (all the capture state lives there) but belong up here, level
        # with the tabs: the analyzer is a view of what the whole editor is
        # doing to your audio, not one more knob on the EQ graph. The graph
        # hand-positions them in its own header strip until we take them, so
        # this has to run after _build_graph_section() has made the graph.
        #
        # A rule divides them from the tabs. Both are unselected grey text at
        # rest, and the analyzer toggles an overlay rather than switching pane
        # — without the rule it reads as a fourth tab.
        self._tab_row_divider = QFrame()
        self._tab_row_divider.setFrameShape(QFrame.Shape.VLine)
        self._tab_row_divider.setFixedHeight(dp(14, 12))
        tab_row_layout.addWidget(self._tab_row_divider, 0, Qt.AlignmentFlag.AlignVCenter)
        tab_row_layout.addSpacing(dp(4, 2))
        for btn in self.graph.take_spectrum_controls():
            btn.setParent(self._tab_row)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            tab_row_layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.advanced_panel = AdvancedPanel(theme)
        self.advanced_panel.changed.connect(self._on_field_changed)
        # GEQ channel selector mirrors the filter bands selector — switching
        # or renaming there is the same operation as in the bands area.
        self.advanced_panel.geq_channel_selected.connect(self._switch_section)
        self.advanced_panel.geq_channel_rename_requested.connect(self._on_rename_section)
        # The GEQ-tab Link Channels control drives the same link state as the
        # primary one in the bands area.
        self.advanced_panel.geq_link_toggle_requested.connect(self._on_geq_link_requested)
        self.advanced_panel.convert_geq_to_peq_requested.connect(self._on_convert_geq_to_peq)
        self._sync_geq_channel_bar()
        advanced_tab = QWidget()
        advanced_layout = QVBoxLayout(advanced_tab)
        advanced_layout.setContentsMargins(dp(16, 8), dp(8, 4), dp(16, 8), dp(10, 6))
        advanced_layout.addWidget(self.advanced_panel)

        self.raw_config_view = RawConfigView(theme)
        raw_tab = QWidget()
        raw_layout = QVBoxLayout(raw_tab)
        raw_layout.setContentsMargins(dp(16, 8), dp(8, 4), dp(16, 8), dp(10, 6))
        raw_layout.addWidget(self.raw_config_view)

        self.main_stack = QStackedWidget()
        for page in (editor_tab, advanced_tab, raw_tab):
            self.main_stack.addWidget(page)
        root.addWidget(self.main_stack, 1)
        self._switch_main_tab(0)

        self._refresh_raw_config()

    def _switch_main_tab(self, idx: int) -> None:
        self.main_stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._main_tab_buttons):
            btn.setChecked(i == idx)
        self._restyle_main_tab_row()

    def _restyle_main_tab_row(self) -> None:
        t = self.theme
        self._tab_row.setStyleSheet(
            f"QFrame#mainTabRow {{ background-color: {t['bg_deep']}; "
            f"border-bottom: 1px solid {t['border_subtle']}; }}"
        )
        self._tab_row_divider.setStyleSheet(
            f"QFrame {{ background: transparent; border: none; "
            f"border-left: 1px solid {t['border_subtle']}; }}")
        for btn in self._main_tab_buttons:
            if btn.isChecked():
                btn.setStyleSheet(
                    f"QPushButton {{ background: transparent; border: none; "
                    f"border-bottom: 2px solid {t['accent']}; color: {t['text_primary']}; "
                    f"font-size: 11px; font-weight: 600; padding: 0 14px; }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background: transparent; border: none; "
                    f"border-bottom: 2px solid transparent; color: {t['text_muted']}; "
                    f"font-size: 11px; padding: 0 14px; }} "
                    f"QPushButton:hover {{ color: {t['text_secondary']}; }}"
                )

    # ── header bar ───────────────────────────────────────────────────

    def _build_header_bar(self) -> QWidget:
        bar = QFrame()
        bar.setProperty("class", "surface")
        bar.setFixedHeight(dp(56, 38))
        self._header_bar = bar
        self._restyle_header_bar()

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(dp(16, 8), dp(6, 2), dp(16, 8), dp(6, 2))
        layout.setSpacing(dp(12, 8))

        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        self.name_edit = EditableMarqueeField(
            label_class="display", edit_class="display", placeholder="Profile name",
            text_color=self.theme["text_primary"],
            placeholder_color=self.theme["text_muted"])
        self.name_edit.textEdited.connect(self._on_name_edited)
        describe(self.name_edit, "Profile name",
                 "The name of this EQ profile. Click to edit; saved with the profile.")
        self.desc_edit = EditableMarqueeField(
            label_class="secondary", edit_class="ghost-input", placeholder="Add a description…",
            text_color=self.theme["text_secondary"],
            placeholder_color=self.theme["text_muted"])
        self.desc_edit.textEdited.connect(self._on_description_edited)
        describe(self.desc_edit, "Profile description",
                 "An optional note about what this profile is for "
                 "(e.g. which headphones or speakers it suits).")
        name_col.addWidget(self.name_edit)
        name_col.addWidget(self.desc_edit)
        layout.addLayout(name_col, 1)

        # A real status LED: lit green only while you are hearing this profile.
        # See _set_led_state(). Starts dark; every load_profile and
        # set_active_state drives it.
        self.badge_live = QLabel()
        self.badge_live.setProperty("class", "badge-live")
        self._set_led_state(is_active=False, is_bypassed=False)
        layout.addWidget(self.badge_live)

        self.btn_ab = QPushButton(" A/B")
        self.btn_ab.setProperty("class", "ghost")
        self.btn_ab.setCheckable(True)
        self.btn_ab.setEnabled(False)
        self.btn_ab.toggled.connect(self.ab_compare_toggled.emit)
        describe(self.btn_ab, "A/B compare",
                 "Hold to hear the saved version of this profile instead of "
                 "your unsaved edits — release to hear the edits again. "
                 "Available while the active profile has unsaved changes.")

        self.btn_save = QPushButton(" Save")
        self.btn_save.setProperty("class", "primary")
        self.btn_save.setProperty("no_glow", True)  # keep the primary fill, drop the accent halo
        self.btn_save.setIcon(icons.icon("save", self.theme["text_on_accent"], 16))
        self.btn_save.clicked.connect(self.save_requested.emit)
        describe(self.btn_save, "Save",
                 "Save this profile's changes to disk. You hear edits live as you "
                 "make them; saving makes them permanent.")

        self.btn_bypass = QPushButton(" Bypass")
        self.btn_bypass.setProperty("class", "warning-outline")
        self.btn_bypass.setIcon(icons.icon("pause", self.theme["warning"], 16))
        self.btn_bypass.setCheckable(True)
        self.btn_bypass.toggled.connect(self.bypass_requested.emit)
        describe(self.btn_bypass, "Bypass",
                 "Temporarily switch all EQ processing off to hear the unprocessed sound. "
                 "Click again to switch the EQ back on; the profile itself is not changed.")

        self.btn_clear = QPushButton(" Clear")
        self.btn_clear.setProperty("class", "danger")
        self.btn_clear.setIcon(icons.icon("delete", self.theme["error"], 16))
        self.btn_clear.clicked.connect(self.clear_requested.emit)
        describe(self.btn_clear, "Clear",
                 "Reset every band and setting in this profile to a blank slate. "
                 "Nothing is written to disk until you click Save.")

        for btn in (self.btn_ab, self.btn_save, self.btn_bypass, self.btn_clear):
            layout.addWidget(btn)

        return bar

    def set_ab_enabled(self, enabled: bool) -> None:
        """A/B is only meaningful while the live profile has unsaved edits;
        disabling it also releases an active hold (silently — the caller
        clears its own compare flag)."""
        if not enabled and self.btn_ab.isChecked():
            self.release_ab()
        self.btn_ab.setEnabled(enabled)

    def release_ab(self) -> None:
        self.btn_ab.blockSignals(True)
        self.btn_ab.setChecked(False)
        self.btn_ab.blockSignals(False)

    def _on_name_edited(self, text: str) -> None:
        if self._meta is not None:
            self._meta.name = text
        self._on_field_changed()

    def _on_description_edited(self, text: str) -> None:
        if self._meta is not None:
            self._meta.description = text
        self._on_field_changed()

    # ── controls row ─────────────────────────────────────────────────

    def _build_controls_row(self) -> QWidget:
        row = QFrame()
        row.setFixedHeight(dp(40, 26))
        layout = QHBoxLayout(row)
        layout.setContentsMargins(dp(16, 8), 0, dp(16, 8), 0)
        layout.setSpacing(dp(8, 6))

        layout.addWidget(self._dim_label("Device:"))
        self.device_combo = CenteredCombo()
        # Width follows the longest endpoint name reported by Equalizer APO,
        # bounded so an extreme name cannot push the other controls off-row.
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.device_combo.setMinimumWidth(dp(160, 130))
        self.device_combo.setMaximumWidth(dp(420, 300))
        self.device_combo.setFixedHeight(dp(28, 22))
        self._populate_device_combo()
        self.device_combo.currentIndexChanged.connect(lambda _: self._on_field_changed())
        describe(self.device_combo, "Device",
                 "Which audio output device this profile applies to — one of "
                 "the endpoints Equalizer APO is installed on, or 'All' for "
                 "every device.")
        layout.addWidget(self.device_combo)

        layout.addSpacing(dp(12, 6))
        self.btn_preamp_toggle = QPushButton("Pre-Amp")
        self.btn_preamp_toggle.setProperty("class", "preamp-toggle")
        self.btn_preamp_toggle.setCheckable(True)
        self.btn_preamp_toggle.setChecked(True)
        self.btn_preamp_toggle.setFixedHeight(dp(28, 22))
        self.btn_preamp_toggle.toggled.connect(self._on_preamp_toggled)
        describe(self.btn_preamp_toggle, "Pre-Amp",
                 "Enable or disable preamp. When off, preamp = 0 dB (no volume adjustment). "
                 "When on, the preamp compensates for boosted bands to prevent clipping.")
        layout.addWidget(self.btn_preamp_toggle)
        self.preamp_spin = QDoubleSpinBox()
        self.preamp_spin.setRange(preamp_calc.PREAMP_MIN_DB, 0.0)
        self.preamp_spin.setDecimals(1)
        self.preamp_spin.setSuffix(" dB")
        self.preamp_spin.setFixedHeight(dp(28, 22))
        self.preamp_spin.setFixedWidth(dp(100, 84))
        self.preamp_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preamp_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.preamp_spin.valueChanged.connect(self._on_preamp_changed)
        describe(self.preamp_spin, "Pre-Amp",
                 "A volume offset in decibels applied before the EQ. "
                 "Set it negative to compensate for boosted bands so the output cannot clip.")
        layout.addWidget(self.preamp_spin)

        self.btn_auto_preamp = QPushButton("Auto")
        self.btn_auto_preamp.setProperty("class", "ghost")
        self.btn_auto_preamp.setCheckable(True)
        self.btn_auto_preamp.setFixedHeight(dp(28, 22))
        self.btn_auto_preamp.toggled.connect(self._on_auto_preamp_toggled)
        describe(self.btn_auto_preamp, "Auto preamp",
                 "Calculate the preamp automatically from the loudest peak across "
                 "all channels (filter bands plus Graphic EQ) so nothing can clip. "
                 "While on, the preamp follows your edits; click again to switch it "
                 "off and reset the preamp to 0 dB for manual editing.")
        layout.addWidget(self.btn_auto_preamp)

        self.lbl_headroom = QLabel()
        self.lbl_headroom.setProperty("class", "muted")
        describe(self.lbl_headroom, "Headroom", _HEADROOM_TOOLTIP)
        layout.addWidget(self.lbl_headroom)

        # Rumble 3.7: distinct from the headroom label above — that one
        # reports calculated/measured output LEVEL; this reports whether the
        # measured spectrum's SHAPE still tracks the calculated EQ curve at
        # all. Hidden unless the live spectrum analyzer has sustained
        # evidence of drift (see _on_measured_drift()).
        self.lbl_drift = QLabel("Measured drift")
        self.lbl_drift.setProperty("class", "value-warning")
        describe(self.lbl_drift, "Measured drift", _DRIFT_TOOLTIP)
        self.lbl_drift.setAccessibleDescription(_DRIFT_DESCRIPTION)
        self.lbl_drift.setVisible(False)
        layout.addWidget(self.lbl_drift)

        layout.addStretch(1)

        # Tone Trim opens the bass/treble/tilt popover. It sits at the end of
        # this row because everything on the row is the same kind of control:
        # what you are hearing right now, not what the profile saves. Owned by
        # MainWindow (the trim is session state on the manager, and the tray
        # menu drives the same values), which wires the click and builds the
        # popover — see MainWindow._toggle_tone_trim_popover.
        self.btn_tone_trim = QPushButton(" Tone Trim")
        self.btn_tone_trim.setProperty("class", "ghost-outline")
        self.btn_tone_trim.setIcon(icons.icon("tune", self.theme["text_secondary"], 14))
        self.btn_tone_trim.setFixedHeight(dp(28, 22))
        self.btn_tone_trim.setCursor(Qt.CursorShape.PointingHandCursor)
        describe(self.btn_tone_trim, "Tone Trim",
                 "Open bass/treble/tilt trim controls -- an instant taste "
                 "adjustment that never touches the saved profile.")
        layout.addWidget(self.btn_tone_trim)
        return row

    def _dim_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("class", "secondary")
        return label

    def _populate_device_combo(self) -> None:
        """Fill the device selector with every endpoint Equalizer APO is
        actually installed on (read from the registry), with 'All' last.
        Each item's userData carries the `Device:` line value; the current
        selection is preserved across refreshes."""
        from rumble.core.audio_devices import list_apo_devices
        current = self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        try:
            devices = list_apo_devices()
        except Exception:
            devices = []
        # list_apo_devices() returns CAPTURE (microphone) endpoints alongside
        # render ones whenever APO is hooked on a mic. Only render endpoints
        # have a WASAPI loopback tap the live spectrum can read, so remember
        # which patterns are capture-flow and tell the graph — see
        # EQGraph.set_spectrum_device(has_loopback=...).
        self._capture_only_patterns = {
            d.pattern for d in devices if d.flow != "Render"}
        for device in devices:
            index = self.device_combo.count()
            self.device_combo.addItem(device.pattern, device.pattern)
            self.device_combo.setItemData(index, wrap_tooltip(device.label),
                                          Qt.ItemDataRole.ToolTipRole)
        index = self.device_combo.count()
        self.device_combo.addItem("All", "all")
        self.device_combo.setItemData(
            index, wrap_tooltip("Every device Equalizer APO is installed on"),
            Qt.ItemDataRole.ToolTipRole)
        self.device_combo.setCurrentIndex(0)
        if current:
            self._select_device(str(current))
        self.device_combo.blockSignals(False)

    def _select_device(self, value: str) -> None:
        """Select the entry whose `Device:` value matches `value`. If value is
        "all" or not found, fall back to the first active device. Unmatched
        saved patterns are inserted above 'All' to preserve existing profiles."""
        from rumble.core.audio_devices import first_active_device_pattern

        value = (value or "all").strip() or "all"

        # If explicitly "all" or no match found, use first active device
        if value.lower() == "all":
            value = first_active_device_pattern()

        index = self.device_combo.findData(value)
        if index < 0:
            # Pattern doesn't match any current device; fall back to first active
            if value.lower() != "all":
                value = first_active_device_pattern()
                index = self.device_combo.findData(value)

            # Still not found? Insert the unmatched pattern for reference
            if index < 0:
                index = self.device_combo.count() - 1
                self.device_combo.insertItem(index, value, value)
                self.device_combo.setItemData(
                    index, wrap_tooltip("Saved device pattern (not currently detected)"),
                    Qt.ItemDataRole.ToolTipRole)

        self.device_combo.setCurrentIndex(index)

    def _on_preamp_changed(self, _value: float) -> None:
        if not self.btn_auto_preamp.isChecked():
            self._preamp_fallback = self.preamp_spin.value()
        self._update_headroom_label()
        self._on_field_changed()

    def _on_preamp_toggled(self, checked: bool) -> None:
        self.preamp_spin.setReadOnly(not checked)
        self.btn_auto_preamp.setEnabled(checked)

        if checked:
            self.preamp_spin.blockSignals(True)
            self.preamp_spin.setValue(self._preamp_saved)
            self.preamp_spin.blockSignals(False)
        else:
            self._preamp_saved = self.preamp_spin.value()
            self.btn_auto_preamp.blockSignals(True)
            self.btn_auto_preamp.setChecked(False)
            self.btn_auto_preamp.blockSignals(False)
            self.preamp_spin.blockSignals(True)
            self.preamp_spin.setValue(0.0)
            self.preamp_spin.blockSignals(False)

        # Push the new preamp value to the live preview without marking the
        # profile dirty: Pre-Amp on/off is a per-session control, not a tracked
        # edit, so it must stay audible but never raise the unsaved-changes
        # warning. (Refreshing only the graph/raw view left APO playing the old
        # preamp, so the toggle made no audible difference.)
        self._refresh_preview_only()

    def _on_auto_preamp_toggled(self, checked: bool) -> None:
        self.preamp_spin.setReadOnly(checked)
        if checked:
            self._preamp_fallback = self.preamp_spin.value()
        else:
            self.preamp_spin.blockSignals(True)
            self.preamp_spin.setValue(self._preamp_fallback)
            self.preamp_spin.blockSignals(False)
        # Applies auto-preamp when enabled, updates headroom, refreshes the
        # graph/raw view, and pushes the live preview — but as a per-session
        # control it must not mark the profile dirty (no unsaved-changes
        # warning). The user can Save to persist the resulting preamp value.
        self._refresh_preview_only()

    def set_ir_resolver(self, resolver) -> None:
        """Inject the Convolution-value → on-disk-path resolver (MainWindow
        passes ProfileManager.resolve_ir_path)."""
        self._ir_resolver = resolver

    def set_listener_correction_provider(self, provider) -> None:
        """*provider* is a zero-arg callable returning the active Listener
        Slot's current EarCorrection (typically ProfileManager.get_active_correction).
        """
        self._listener_correction_provider = provider

    def _refresh_ir_result(self) -> None:
        """Re-derive the analysed IR from the live Convolution value. Cheap on
        the unchanged path: resolving is two stat() calls and the analysis is
        cached per (path, mtime), so this is safe to run on every recompute —
        which also picks up the file being edited or (re)appearing on disk."""
        value = self.advanced_panel.convolution.value()
        result = None
        if value and self._ir_resolver is not None:
            path = self._ir_resolver(value)
            if path is not None:
                result = ir_analysis.get_ir_response(path)
        self._ir_result = result
        mismatch = None
        if result is not None:
            mismatch = ir_analysis.check_rate_mismatch(result.ir_sample_rate, self._sample_rate)
        self.advanced_panel.convolution.set_file_status(not value or result is not None, mismatch)

    def _ir_curve_db(self):
        return self._ir_result.response_db if self._ir_result is not None else None

    def refresh_listener_overlay(self) -> None:
        """Recompute and push the L/R-effective overlay from the current
        listener-correction provider onto the graph. Call after loading a
        profile and whenever the active Listener Slot changes."""
        if self._listener_correction_provider is None:
            return
        correction = self._listener_correction_provider()
        if correction.is_flat:
            self.graph.set_listener_correction(None, None)
            return
        freqs = list(eq_math.frequency_array())
        left_db = np.array(autoeq_merge.interp_log(correction.left, freqs)) \
            if correction.left else np.zeros(len(freqs))
        right_db = np.array(autoeq_merge.interp_log(correction.right, freqs)) \
            if correction.right else np.zeros(len(freqs))
        self.graph.set_listener_correction(left_db, right_db)

    def _apply_auto_preamp(self) -> None:
        chains = self._section_signal_chains()
        if not chains:
            return
        suggested = min(
            preamp_calc.calculate_auto_preamp(
                filters, sample_rate=self._sample_rate, geq_string=geq,
                section_offset_db=trim, ir_response=self._ir_curve_db())
            for filters, geq, trim in chains
        )
        self.preamp_spin.blockSignals(True)
        self.preamp_spin.setValue(suggested)
        self.preamp_spin.blockSignals(False)
        self._update_headroom_label()

    def _section_signal_chains(self) -> list[tuple[list[Filter], str, float]]:
        """(filters, geq_string, trim_db) per channel section for preamp math —
        the current section is read from the live UI, the rest from the saved
        sections list. Pre-Amp is global, so clipping anywhere matters."""
        chains: list[tuple[list[Filter], str, float]] = []
        for i, section in enumerate(self._channel_sections):
            if i == self._current_section_idx:
                chains.append((self._collect_filters(),
                               self.advanced_panel.current_geq(),
                               float(self._section_trim_spin.value())))
            else:
                chains.append((section.filters, section.graphic_eq,
                               section.preamp_db or 0.0))
        return chains

    def current_headroom_db(self) -> float:
        """The worst-case (minimum, across sections) headroom for the live
        editor state — the same computation the headroom label already
        renders, exposed so callers like MainWindow's save flow don't
        duplicate it (Rumble 5.1)."""
        preamp = self.preamp_spin.value()
        return min(
            preamp_calc.calculate_headroom(
                filters, preamp, sample_rate=self._sample_rate, geq_string=geq,
                section_offset_db=trim, ir_response=self._ir_curve_db())
            for filters, geq, trim in self._section_signal_chains()
        )

    def is_auto_preamp_enabled(self) -> bool:
        return self.btn_auto_preamp.isChecked()

    def _update_headroom_label(self) -> None:
        headroom = self.current_headroom_db()
        if headroom >= 0:
            self.lbl_headroom.setProperty("class", "value-success")
            self.lbl_headroom.setText(f"Headroom: +{headroom:.1f} dB")
        else:
            self.lbl_headroom.setProperty("class", "value-error")
            self.lbl_headroom.setText(f"Clipping risk: {headroom:.1f} dB")

        # Phase 6 Task 4: this calculated success/error state is theoretical
        # (derived from the filter math, not from what's actually playing).
        # If the live spectrum analyzer has measured sustained real clipping
        # (see EQGraph.measured_clipping_changed / SpectrumController's
        # clipping_changed), that overrides to a third, visually distinct
        # QSS class ("value-warning" — defined in theme_engine.py but unused
        # until now) regardless of what the calculated headroom says, since
        # measured evidence of real output clipping is more actionable than
        # a clean calculation that real content still happens to clip.
        if self._measured_clipping:
            self.lbl_headroom.setProperty("class", "value-warning")
            self.lbl_headroom.setToolTip("Measured output peaks at/above 0 dBFS")
            self.lbl_headroom.setAccessibleDescription(_HEADROOM_CLIPPING_DESCRIPTION)
        else:
            self.lbl_headroom.setToolTip(wrap_tooltip(_HEADROOM_TOOLTIP))
            self.lbl_headroom.setAccessibleDescription(_HEADROOM_TOOLTIP)

        self.lbl_headroom.style().unpolish(self.lbl_headroom)
        self.lbl_headroom.style().polish(self.lbl_headroom)

    def _on_measured_clipping(self, is_clipping: bool) -> None:
        self._measured_clipping = is_clipping
        self._update_headroom_label()

    def _on_measured_drift(self, is_drifting: bool) -> None:
        self._measured_drift = is_drifting
        self.lbl_drift.setVisible(is_drifting)

    # ── VU/Peak/Reference meter ──────────────────────────────────────

    def configure_meter_settings(self, mode: str = "vu", k_reference: str = "k14",
                                  active: bool = False) -> None:
        """Apply persisted meter settings. Mirrors
        `EQGraph.configure_spectrum_settings()`'s pattern: the caller (main_window.py)
        reads the persisted keys and calls this once after construction."""
        self.vu_meter.set_mode(mode)
        self.vu_meter.set_k_reference(k_reference)
        self.vu_meter.set_active(active)

    def _on_meter_mode_changed(self, mode: str) -> None:
        self.meter_settings_changed.emit({"meter_mode": mode})

    def _on_meter_k_reference_changed(self, level: str) -> None:
        self.meter_settings_changed.emit({"meter_k_reference": level})

    def _on_graph_spectrum_settings_changed(self, settings: dict) -> None:
        self.vu_meter.set_active(bool(settings.get("spectrum_enabled", False)))

    # ── frequency graph ──────────────────────────────────────────────

    def _build_graph_section(self) -> QWidget:
        # The graph lives in a HudFrame so the sci-fi corner brackets hug the EQ
        # response — the headline glass-HUD detail. HudFrame paints the ticks in
        # paintEvent (no QGraphicsEffect), so it's safe over the pyqtgraph graph
        # where a drop-shadow would break rendering.
        frame = HudFrame(class_name="panel")
        self._graph_frame = frame
        self._restyle_graph_frame()
        # Low enough that the editor tab fits the fixed window height with the
        # band strip fully visible; the graph takes any spare space.
        frame.setMinimumHeight(dp(150, 110))
        layout = QVBoxLayout(frame)
        # Inset the graph from the frame edge so the corner brackets stay
        # visible in the surrounding band instead of being covered by the graph.
        layout.setContentsMargins(dp(8, 6), dp(8, 6), dp(8, 6), dp(8, 6))
        self.graph = EQGraph(self.theme)
        self.graph.band_dragged.connect(self._on_band_dragged)
        self.graph.band_selected.connect(self._on_band_selected)
        self.graph.measured_clipping_changed.connect(self._on_measured_clipping)
        self.graph.measured_drift_changed.connect(self._on_measured_drift)
        describe(self.graph, "Frequency response graph",
                 "Live preview of the profile's total EQ curve (filter bands plus "
                 "Graphic EQ), 20 Hz to 20 kHz. Drag a handle up or down to change "
                 "that band's gain; click a handle to select its band. With the "
                 "graph focused, use Left/Right to select a band and Up/Down to "
                 "adjust its gain (hold Shift for fine steps).")
        layout.addWidget(self.graph)
        frame.set_theme(self.theme)

        # Right-side VU/Peak/Reference meter, same row as the graph (not
        # spanning the filter-band strip below it) -- always present/
        # reserved, goes idle rather than disappearing when the Analyser is
        # off (see set_active).
        self.vu_meter = VuMeterWidget(self.theme)
        self.vu_meter.mode_changed.connect(self._on_meter_mode_changed)
        self.vu_meter.k_reference_changed.connect(self._on_meter_k_reference_changed)
        self.graph.stereo_level_ready.connect(self.vu_meter.on_stereo_level)
        self.graph.estimated_input_level_ready.connect(self.vu_meter.on_estimated_input_level)
        self.graph.spectrum_settings_changed.connect(self._on_graph_spectrum_settings_changed)

        wrapper = QWidget()
        wrapper_layout = QHBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(dp(16, 8), dp(10, 2), dp(16, 8), 0)
        wrapper_layout.setSpacing(dp(8, 6))
        wrapper_layout.addWidget(frame, 1)
        wrapper_layout.addWidget(self.vu_meter, 0)
        return wrapper

    def _restyle_graph_frame(self) -> None:
        self._graph_frame.setStyleSheet(
            f"QFrame[class=\"panel\"] {{ background-color: {self.theme['graph_bg']}; "
            f"border: 1px solid {self.theme['border_subtle']}; border-radius: 6px; }}")

    def _on_band_dragged(self, index: int, fc: float, gain: float) -> None:
        if 0 <= index < len(self._filter_rows):
            row = self._filter_rows[index]
            row.spn_freq.blockSignals(True)
            row.spn_freq.setValue(fc)
            row.spn_freq.blockSignals(False)
            row.filter.fc = fc
            if row.filter.supports_gain():
                row.spn_gain.blockSignals(True)
                row.spn_gain.setValue(gain)
                row.spn_gain.blockSignals(False)
                row.filter.gain = gain
            self._select_band_row(index)
            self._on_field_changed()

    def _on_band_selected(self, index: int) -> None:
        self._select_band_row(index)

    def _select_band_row(self, index: int) -> None:
        for i, row in enumerate(self._filter_rows):
            row.set_selected(i == index)
        if 0 <= index < len(self._filter_rows):
            self.band_scroll.ensureWidgetVisible(self._filter_rows[index])
            self.graph.highlight_band(index)

    # ── filter band table ────────────────────────────────────────────

    def _build_filter_table_section(self) -> QWidget:
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(dp(16, 8), 0, dp(16, 8), 0)
        wrapper_layout.setSpacing(0)

        # Title row: label + channel-setup preset + Clear All
        title_row = QHBoxLayout()
        title = QLabel("FILTER BANDS")
        title.setProperty("class", "heading")
        # Kept so the heading can name the channel currently being edited.
        self._bands_title = title

        self._preset_combo = CenteredCombo()
        self._preset_combo.addItems(list(CHANNEL_PRESETS.keys()) + [CUSTOM_LAYOUT_LABEL])
        self._preset_combo.setFixedWidth(dp(190, 165))
        self._preset_combo.setFixedHeight(dp(26, 20))
        self._preset_combo.currentTextChanged.connect(self._on_preset_changed)
        describe(self._preset_combo, "Channel layout preset",
                 "Apply a common speaker layout (Stereo / Headphones, 5.1…) or "
                 "Custom… for any channel names your device exposes. Existing "
                 "bands are kept and copied to any newly created channels.")

        self.btn_clear_all = QPushButton(" Clear All")
        self.btn_clear_all.setProperty("class", "ghost")
        self.btn_clear_all.setIcon(icons.icon("delete", self.theme["text_muted"], 14))
        self.btn_clear_all.clicked.connect(self._on_clear_all_clicked)
        describe(self.btn_clear_all, "Clear all bands",
                 "Remove every filter band from the selected channel section.")

        # Editor-wide Q/BW segmented toggle: every band's width field shows
        # (and is edited in) the selected representation.
        self._bw_mode = False
        qbw_label = self._dim_label("Width:")
        self.btn_q_mode = QPushButton("Q")
        self.btn_bw_mode = QPushButton("BW")
        for btn, is_bw in ((self.btn_q_mode, False), (self.btn_bw_mode, True)):
            btn.setCheckable(True)
            btn.setFixedSize(dp(40, 34), dp(26, 20))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, bw=is_bw: self._set_bw_mode(bw))
        self.btn_q_mode.setChecked(True)
        describe(self.btn_q_mode, "Q factor mode",
                 "Show every band's width as a Q factor — higher Q means a "
                 "narrower band.")
        describe(self.btn_bw_mode, "Bandwidth mode",
                 "Show every band's width as a bandwidth in octaves — the same "
                 "value as Q, expressed the other way round.")
        qbw_widget = QWidget()
        qbw_layout = QHBoxLayout(qbw_widget)
        qbw_layout.setContentsMargins(0, 0, 0, 0)
        qbw_layout.setSpacing(0)
        qbw_layout.addWidget(self.btn_q_mode)
        qbw_layout.addWidget(self.btn_bw_mode)
        self._update_qbw_mode_styles()

        # Per-section preamp ("trim") — APO's Preamp inside a Channel block.
        trim_label = self._dim_label("Trim:")
        self._section_trim_spin = QDoubleSpinBox()
        self._section_trim_spin.setRange(-30.0, 30.0)
        self._section_trim_spin.setDecimals(1)
        self._section_trim_spin.setSuffix(" dB")
        self._section_trim_spin.setFixedWidth(80)
        self._section_trim_spin.setFixedHeight(row_h_field())
        self._section_trim_spin.setStyleSheet(
            "QDoubleSpinBox { padding: 2px 5px; min-height: 16px; }")
        self._section_trim_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._section_trim_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        describe(self._section_trim_spin, "Channel trim",
                 "A volume offset for this channel only, in dB — balances "
                 "channels against each other (e.g. lower a hot subwoofer). "
                 "Written as a per-channel Preamp line; 0 means no line. The "
                 "global Pre-Amp still applies on top.")
        self._section_trim_spin.valueChanged.connect(self._on_section_trim_value_changed)

        # Per-section delay lives in the title row — its old dedicated row was
        # reclaimed to give the graph more height; the tooltip now carries the
        # explanation the inline hint used to show.
        delay_label = self._dim_label("Delay:")
        self._section_delay_spin = QDoubleSpinBox()
        self._section_delay_spin.setRange(0.0, 5000.0)
        self._section_delay_spin.setDecimals(1)
        self._section_delay_spin.setSuffix(" ms")
        self._section_delay_spin.setFixedWidth(90)
        # Same metrics as the band-grid fields (FilterRow): the global QSS
        # padding/min-height doesn't fit in a compact field, so override it
        # the same way FilterRow does or the text clips at the bottom.
        self._section_delay_spin.setFixedHeight(row_h_field())
        self._section_delay_spin.setStyleSheet(
            "QDoubleSpinBox { padding: 2px 5px; min-height: 16px; }")
        self._section_delay_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._section_delay_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        describe(self._section_delay_spin, "Delay amount",
                 "How long this channel's sound is delayed, in milliseconds — "
                 "time-aligns this channel relative to the others; leave at 0 "
                 "for no delay. Roughly 1 ms per 34 cm of extra speaker "
                 "distance. Delays stored in samples are shown converted at "
                 "the configured sample rate and are rewritten in "
                 "milliseconds if edited.")
        self._section_delay_spin.valueChanged.connect(self._on_section_delay_value_changed)

        self.btn_measure_delay = QPushButton(" Measure")
        self.btn_measure_delay.setFixedHeight(dp(24, 20))
        self.btn_measure_delay.setProperty("class", "ghost")
        self.btn_measure_delay.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_measure_delay.clicked.connect(self._on_measure_delay_clicked)
        describe(self.btn_measure_delay, "Measure channel delays",
                 "Plays a short calibrated sweep through each channel in turn "
                 "while a measurement microphone (the system's default "
                 "recording device) listens from the main listening position, "
                 "then fills in every channel's Delay field automatically so "
                 "all speakers arrive in sync. Needs an unlinked layout (each "
                 "channel its own section, standard names like L/R/C/LFE) "
                 "and a quiet room.")
        title_row.addWidget(title)
        title_row.addSpacing(10)
        title_row.addWidget(self._preset_combo)
        title_row.addStretch(1)
        title_row.addWidget(trim_label)
        title_row.addSpacing(4)
        title_row.addWidget(self._section_trim_spin)
        # These three gaps were 16px each; the row's full content (title
        # through Clear All) no longer fits every combination of preset-combo
        # width and system font metrics within the fixed-width editor column,
        # which starves the addStretch(1) above down to 0px and reads as
        # "Trim" crowding into the preset combo. Trimmed to 10px each --
        # still a clear visual break between groups, but enough reclaimed
        # width to keep the stretch (and the gap it gives Trim) alive.
        title_row.addSpacing(10)
        title_row.addWidget(delay_label)
        title_row.addSpacing(4)
        title_row.addWidget(self._section_delay_spin)
        title_row.addSpacing(6)
        title_row.addWidget(self.btn_measure_delay)
        title_row.addSpacing(10)
        title_row.addWidget(qbw_label)
        title_row.addSpacing(2)
        title_row.addWidget(qbw_widget)
        title_row.addSpacing(10)
        title_row.addWidget(self.btn_clear_all)
        wrapper_layout.addLayout(title_row)
        wrapper_layout.addSpacing(dp(6, 2))

        # ── Channel section selector tabs ────────────────────────────
        section_bar = QWidget()
        section_bar.setFixedHeight(dp(30, 22))
        section_bar_layout = QHBoxLayout(section_bar)
        section_bar_layout.setContentsMargins(0, 0, 0, 0)
        section_bar_layout.setSpacing(4)

        self._section_btns_widget = QWidget()
        self._section_btns_layout = QHBoxLayout(self._section_btns_widget)
        self._section_btns_layout.setContentsMargins(0, 0, 0, 0)
        self._section_btns_layout.setSpacing(4)
        section_bar_layout.addWidget(self._section_btns_widget)
        section_bar_layout.addStretch(1)

        self._btn_link_channels = QPushButton(" Link Channels")
        self._btn_link_channels.setFixedHeight(dp(24, 20))
        self._btn_link_channels.setCheckable(True)
        self._btn_link_channels.setChecked(True)
        self._btn_link_channels.setProperty("class", "ghost")
        self._btn_link_channels.toggled.connect(self._on_link_channels_toggled)
        self._update_link_channels_icon()
        describe(self._btn_link_channels, "Link channels",
                 "Keep every channel identical: edit one channel and the bands, "
                 "Graphic EQ and delay are copied to all the others. "
                 "Turn off to give each channel its own independent EQ.")
        section_bar_layout.addWidget(self._btn_link_channels)

        self.btn_add_section = QPushButton(" Add Channel")
        self.btn_add_section.setFixedHeight(dp(24, 20))
        self.btn_add_section.setProperty("class", "ghost")
        self.btn_add_section.setIcon(icons.icon("add", self.theme["text_muted"], 12))
        self.btn_add_section.clicked.connect(self._on_add_section_clicked)
        describe(self.btn_add_section, "Add channel",
                 "Add another channel section (e.g. a subwoofer or rear channel) "
                 "with its own set of filter bands.")
        section_bar_layout.addWidget(self.btn_add_section)

        self.btn_add_band = QPushButton(" Add Band")
        self.btn_add_band.setFixedHeight(dp(24, 20))
        self.btn_add_band.setProperty("class", "ghost")
        self.btn_add_band.setIcon(icons.icon("add", self.theme["text_muted"], 12))
        self.btn_add_band.clicked.connect(self._on_add_band_clicked)
        describe(self.btn_add_band, "Add filter band",
                 "Add a new filter band to the selected channel (up to 32 bands). "
                 "Each band shapes the sound around one frequency.")
        section_bar_layout.addWidget(self.btn_add_band)

        self.btn_remove_section = QPushButton(" Remove")
        self.btn_remove_section.setFixedHeight(dp(24, 20))
        self.btn_remove_section.setProperty("class", "ghost")
        self.btn_remove_section.setEnabled(False)
        self.btn_remove_section.clicked.connect(self._on_remove_section_clicked)
        describe(self.btn_remove_section, "Remove channel",
                 "Delete the selected channel section and all of its filter bands.")
        section_bar_layout.addWidget(self.btn_remove_section)

        self.btn_reorder = QPushButton(" Re-order")
        self.btn_reorder.setFixedHeight(dp(24, 20))
        self.btn_reorder.setProperty("class", "ghost")
        self.btn_reorder.clicked.connect(self._on_reorder_clicked)
        describe(self.btn_reorder, "Re-order bands",
                 "Sort the filter bands by frequency so band 1 is the lowest. "
                 "Bands never move on their own while you edit.")
        section_bar_layout.addWidget(self.btn_reorder)

        wrapper_layout.addWidget(section_bar)
        wrapper_layout.addSpacing(dp(8, 3))

        # Horizontal band strip inside a scroll area
        self.rows_container = QWidget()
        self.rows_layout = QHBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(6, 0, 6, 0)
        self.rows_layout.setSpacing(1)
        self.rows_layout.addStretch(1)

        self.band_scroll = QScrollArea()
        self.band_scroll.setWidget(self.rows_container)
        self.band_scroll.setWidgetResizable(True)
        # Vertical scrolling is off, so the strip must be exactly tall enough
        # for a full band column — measure one instead of hardcoding.
        probe = FilterRow(Filter(), 1, self.theme)
        strip_height = (probe.sizeHint().height()
                        + self.band_scroll.horizontalScrollBar().sizeHint().height() + 4)
        probe.deleteLater()
        self.band_scroll.setFixedHeight(
            max(dp(_BAND_STRIP_MIN_HEIGHT, 110), min(strip_height, _BAND_STRIP_MAX_HEIGHT)))
        self.band_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.band_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.band_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.band_scroll.setProperty("class", "band-strip")
        band_scroll = self.band_scroll

        # Spreadsheet layout: a fixed row-label gutter on the left (plain
        # labels, outside the panel frame) next to the framed band strip,
        # whose columns scroll horizontally while the labels stay put.
        strip_frame = QFrame()
        strip_frame.setProperty("class", "panel")
        strip_layout = QHBoxLayout(strip_frame)
        # 1px all round keeps the columns off the frame's 1px border; the
        # gutter compensates for it via BandGridHeader.TOP_MARGIN.
        strip_layout.setContentsMargins(1, 1, 1, 1)
        strip_layout.setSpacing(0)
        strip_layout.addWidget(band_scroll, 1)

        self._grid_header = BandGridHeader()
        strip_row = QHBoxLayout()
        strip_row.setContentsMargins(0, 0, 0, 0)
        strip_row.setSpacing(0)
        strip_row.addWidget(self._grid_header)
        strip_row.addWidget(strip_frame, 1)
        wrapper_layout.addLayout(strip_row)

        return wrapper

    def _on_add_band_clicked(self) -> None:
        if len(self._filter_rows) >= MAX_BANDS:
            return
        self._add_filter_row(Filter())
        self._refresh_graph_and_raw()
        self._on_field_changed()

    def _on_clear_all_clicked(self) -> None:
        for row in list(self._filter_rows):
            self._remove_filter_row(row)
        self._refresh_graph_and_raw()
        self._on_field_changed()

    def _add_filter_row(self, filt: Filter) -> FilterRow:
        index = len(self._filter_rows) + 1
        row = FilterRow(filt, index, self.theme, bw_mode=self._bw_mode)
        row.set_sample_rate(self._sample_rate)
        row.changed.connect(self._on_band_row_changed)
        row.delete_requested.connect(self._on_band_delete_requested)
        row.hovered.connect(self._on_band_hovered)
        row.solo_toggled.connect(self._on_band_solo_toggled)
        self._filter_rows.append(row)
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        return row

    def _remove_filter_row(self, row: FilterRow) -> None:
        self._filter_rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self._renumber_rows()

    def _renumber_rows(self) -> None:
        for i, row in enumerate(self._filter_rows, start=1):
            row.set_index(i)

    def _on_band_row_changed(self) -> None:
        self._refresh_graph_and_raw()
        self._on_field_changed()

    def _on_reorder_clicked(self) -> None:
        if not self._sort_filter_rows():
            return
        self._refresh_graph_and_raw()
        self._on_field_changed()

    def _on_band_delete_requested(self, row: FilterRow) -> None:
        # Deleting shifts band indices, so any solo is released first.
        self._clear_solo()
        self._remove_filter_row(row)
        self._refresh_graph_and_raw()
        self._on_field_changed()

    def _on_band_hovered(self, row: FilterRow, is_hovered: bool) -> None:
        if row in self._filter_rows:
            index = self._filter_rows.index(row)
            self.graph.highlight_band(index if is_hovered else None)

    def _collect_filters(self) -> list[Filter]:
        return [row.filter for row in self._filter_rows]

    # ── per-band solo (audition) ──────────────────────────────────────

    def _on_band_solo_toggled(self, row: FilterRow, on: bool) -> None:
        if self._loading:
            return
        if on and row in self._filter_rows:
            for other in self._filter_rows:
                if other is not row:
                    other.set_solo_silently(False)
            self._solo_index = self._filter_rows.index(row)
        else:
            self._solo_index = None
        # An audition, never a tracked edit: refresh + preview without dirty.
        self._refresh_preview_only()

    def _clear_solo(self) -> None:
        """Release any solo without emitting (used when the band list or the
        visible section is about to change under it)."""
        if self._solo_index is None:
            return
        self._solo_index = None
        for row in self._filter_rows:
            row.set_solo_silently(False)

    def _effective_filters(self) -> list[Filter]:
        """The current section's filters as the audition hears them: identical
        to the stored list unless a band is soloed, in which case every other
        band is masked off in cloned Filter objects (stored flags untouched)."""
        filters = self._collect_filters()
        if self._solo_index is None:
            return filters
        return [dataclass_replace(f, enabled=(i == self._solo_index))
                for i, f in enumerate(filters)]

    # ── Q/BW display mode ─────────────────────────────────────────────

    def _set_bw_mode(self, bw_mode: bool) -> None:
        """Switch every band's width field between Q factor and Bandwidth
        (octaves). Display-only: values are converted for display and stored
        values are untouched until edited, so toggling never dirties a profile."""
        self._bw_mode = bw_mode
        self.btn_q_mode.setChecked(not bw_mode)
        self.btn_bw_mode.setChecked(bw_mode)
        self._update_qbw_mode_styles()
        self._grid_header.set_bw_mode(bw_mode)
        for row in self._filter_rows:
            row.set_bw_mode(bw_mode)

    def _update_qbw_mode_styles(self) -> None:
        t = self.theme
        active = (
            f"background: {t['accent']}; color: {t['text_on_accent']}; "
            f"border: 1px solid {t['accent_active']}; font-weight: 600;"
        )
        inactive = (
            f"background: transparent; color: {t['text_muted']}; "
            f"border: 1px solid {t['border_subtle']};"
        )
        self.btn_q_mode.setStyleSheet(
            f"QPushButton {{ {active if not self._bw_mode else inactive} "
            f"border-top-left-radius: 3px; border-bottom-left-radius: 3px; "
            f"border-top-right-radius: 0; border-bottom-right-radius: 0; "
            f"border-right: none; padding: 0; font-size: 11px; }}"
            f"QPushButton:hover {{ color: {t['text_on_accent'] if not self._bw_mode else t['text_primary']}; }}"
        )
        self.btn_bw_mode.setStyleSheet(
            f"QPushButton {{ {active if self._bw_mode else inactive} "
            f"border-top-right-radius: 3px; border-bottom-right-radius: 3px; "
            f"border-top-left-radius: 0; border-bottom-left-radius: 0; "
            f"padding: 0; font-size: 11px; }}"
            f"QPushButton:hover {{ color: {t['text_on_accent'] if self._bw_mode else t['text_primary']}; }}"
        )

    # ── channel section management ────────────────────────────────────

    def _rebuild_section_bar(self) -> None:
        """Recreate section selector buttons from _channel_sections."""
        while self._section_btns_layout.count():
            item = self._section_btns_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._section_btns.clear()

        for i, section in enumerate(self._channel_sections):
            name = section.channel or f"Section {i + 1}"
            btn = _SectionButton(name)
            btn.setFixedHeight(dp(24, 20))
            btn.clicked.connect(lambda _checked, idx=i: self._switch_section(idx))
            btn.double_clicked.connect(lambda idx=i: self._on_rename_section(idx))
            describe(btn, f"Channel {name}",
                     f"Show and edit the filter bands for the '{name}' channel. "
                     "Double-click to rename the channel.")
            self._section_btns_layout.addWidget(btn)
            self._section_btns.append(btn)

        self._update_section_button_styles()
        self.btn_remove_section.setEnabled(len(self._channel_sections) > 1)

        self._sync_preset_combo()

    def _on_rename_section(self, idx: int) -> None:
        if idx >= len(self._channel_sections):
            return
        section = self._channel_sections[idx]
        channel, ok = QInputDialog.getText(
            self, "Rename Channel",
            "Channel name (e.g. L, R, C, LFE, all):",
            text=section.channel,
        )
        channel = channel.strip()
        if not ok or not channel or channel == section.channel:
            return
        section.channel = channel
        self._rebuild_section_bar()
        self._on_field_changed()

    def _update_section_button_styles(self) -> None:
        t = self.theme
        # While linked every channel holds the same values, so all the
        # indicators light up; unlinked, only the selected channel is lit.
        linked = self._channels_linked()
        for i, btn in enumerate(self._section_btns):
            if linked or i == self._current_section_idx:
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
        self._update_bands_title()
        self._sync_geq_channel_bar()

    def _update_bands_title(self) -> None:
        """Name the channel being edited in the band-strip heading so it is
        obvious which channel the visible bands belong to. With one section
        there is nothing to disambiguate; while linked every channel is
        identical, so the heading says so rather than singling one out."""
        if len(self._channel_sections) <= 1:
            self._bands_title.setText("FILTER BANDS")
            return
        if self._channels_linked():
            self._bands_title.setText("FILTER BANDS  ·  All channels")
            return
        idx = min(self._current_section_idx, len(self._channel_sections) - 1)
        channel = self._channel_sections[idx].channel or f"Section {idx + 1}"
        self._bands_title.setText(f"FILTER BANDS  ·  {channel}")

    def _sync_geq_channel_bar(self) -> None:
        """Mirror the channel sections into the Graphic EQ tab's selector."""
        if not hasattr(self, "advanced_panel"):
            return
        names = [s.channel or f"Section {i + 1}"
                 for i, s in enumerate(self._channel_sections)]
        self.advanced_panel.set_geq_channels(
            names, self._current_section_idx, self._channels_linked())

    # ── channel linking ───────────────────────────────────────────────

    def _channels_linked(self) -> bool:
        return self._btn_link_channels.isChecked()

    def _update_link_channels_icon(self) -> None:
        linked = self._btn_link_channels.isChecked()
        color = self.theme["text_on_accent"] if linked else self.theme["text_muted"]
        self._btn_link_channels.setIcon(icons.icon("link", color, 13))
        # Label names the action the button performs, not the current state.
        self._btn_link_channels.setText(
            " Unlink Channels" if linked else " Link Channels")

    @staticmethod
    def _clone_filters(filters: list[Filter]) -> list[Filter]:
        return [dataclass_replace(f) for f in filters]

    def _sections_identical(self) -> bool:
        if len(self._channel_sections) <= 1:
            return True
        first = self._channel_sections[0]
        return all(
            s.filters == first.filters
            and s.graphic_eq == first.graphic_eq
            and s.delay_ms == first.delay_ms
            and s.preamp_db == first.preamp_db
            for s in self._channel_sections[1:]
        )

    def _left_channel_index(self) -> int:
        """Index of the Left channel — the canonical source when (re)linking.
        Falls back to the first section when nothing is named 'L'."""
        for i, section in enumerate(self._channel_sections):
            if section.channel.strip().upper() == "L":
                return i
        return 0

    def _propagate_linked_sections(self, source_idx: int | None = None) -> None:
        """Clone one section's bands, GraphicEQ and delay to all other sections
        (channel names stay per-section). The source defaults to the section
        being edited — used to keep channels mirrored while already linked —
        but relinking passes the Left channel so its values become canonical."""
        self._collect_current_section_to(self._current_section_idx)
        if source_idx is None:
            source_idx = self._current_section_idx
        src = self._channel_sections[source_idx]
        for i, section in enumerate(self._channel_sections):
            if i != source_idx:
                section.filters = self._clone_filters(src.filters)
                section.graphic_eq = src.graphic_eq
                section.delay_ms = src.delay_ms
                section.delay_samples = src.delay_samples
                section.preamp_db = src.preamp_db

    @staticmethod
    def _expand_channel_tokens(channel: str) -> list[str]:
        """The individual output channels a section's Channel string targets.
        APO's catch-all ("all", or unset) is the standard stereo pair; a
        combined section like "L R" lists each channel explicitly."""
        tokens = [t for t in channel.split() if t.lower() != "all"]
        seen: list[str] = []
        for t in tokens:
            if t not in seen:
                seen.append(t)
        return seen or ["L", "R"]

    def _split_into_separate_channels(self) -> None:
        """Unlinking: give every output channel its own editable section so the
        channels can diverge. A profile stored as one combined section ("all"
        or "L R") is expanded into one section per channel, each a clone of the
        combined values. Sections that already target a single channel are left
        as-is (nothing to separate)."""
        self._collect_current_section_to(self._current_section_idx)
        new_sections: list[ChannelSection] = []
        for section in self._channel_sections:
            for name in self._expand_channel_tokens(section.channel):
                clone = section.clone()
                clone.channel = name
                new_sections.append(clone)
        # Already one channel per section → no structural change needed.
        if len(new_sections) == len(self._channel_sections):
            return
        self._channel_sections = new_sections
        self._current_section_idx = 0
        self._rebuild_section_bar()
        self._load_section_from(0)

    def _collapse_into_single_section(self) -> bool:
        """Linking: fold an identical L+R pair back into one combined 'L R'
        strip — the inverse of _split_into_separate_channels — so a linked
        stereo layout always shows as a single channel strip regardless of
        whether the file stored it as one block or two. Only the standard
        stereo pair collapses: surround/other layouts keep one tab per channel,
        and divergent or single sections are left as-is. Returns True if the
        structure changed (caller must rebuild the section bar / reload)."""
        if len(self._channel_sections) <= 1 or not self._sections_identical():
            return False
        names: list[str] = []
        for section in self._channel_sections:
            for name in self._expand_channel_tokens(section.channel):
                if name not in names:
                    names.append(name)
        if {n.upper() for n in names} != {"L", "R"}:
            return False
        combined = self._channel_sections[0].clone()
        combined.channel = "L R"
        self._channel_sections = [combined]
        self._current_section_idx = 0
        return True

    def _on_link_channels_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        if checked and len(self._channel_sections) > 1:
            self._collect_current_section_to(self._current_section_idx)
            src_idx = self._left_channel_index()
            src = self._channel_sections[src_idx]
            others = ", ".join(
                s.channel for i, s in enumerate(self._channel_sections)
                if i != src_idx)
            reply = QMessageBox.question(
                self, "Link Channels",
                f'Channel values will be synchronised from the "{src.channel}" '
                f'channel to: {others}.\n'
                "Bands, Graphic EQ and delay on the other channel(s) will be "
                "overwritten and kept identical while linked.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._set_link_state_silently(False)
                return
        if checked:
            # Relink: the Left channel is canonical — copy it over every other
            # channel, then collapse the now-identical sections into one combined
            # 'L R' strip (mirroring the split done on unlink) and reload it.
            self._propagate_linked_sections(self._left_channel_index())
            self._collapse_into_single_section()
            self._rebuild_section_bar()
            self._load_section_from(self._current_section_idx)
        else:
            self._split_into_separate_channels()
        self._update_link_channels_icon()
        self._update_section_button_styles()
        self._on_field_changed()

    def _on_geq_link_requested(self, linked: bool) -> None:
        """Mirror of the Graphic EQ tab's Link Channels control: drive the
        primary button so the full toggle flow (confirmation, split/propagate)
        runs once. set_geq_channels then syncs the GEQ button to the result, so
        a cancelled relink leaves both controls back in the unlinked state."""
        if self._btn_link_channels.isChecked() == linked:
            return
        self._btn_link_channels.setChecked(linked)

    def _on_convert_geq_to_peq(self) -> None:
        """Convert the active channel's Graphic EQ curve into parametric bands.

        Uses geq_peq_convert.fit_peq(), which PLACES bands where the curve
        actually deviates and solves their Fc/gain/Q jointly, rather than the
        older point-mapper that assigned one band per grid point (and so filled
        the band list with narrow, sub-1 dB grid-chasing junk when given a lot of
        bands). The band count is what the curve earns, not the ceiling — a
        gentle curve converts to a handful of bands, not 32.

        Still lossy by design: this REPLACES the curve with bands, discarding the
        residual that the import path (autoeq_merge.build_curve_config) would
        keep, because a Convert that left a GraphicEQ layer behind would look
        like it had done nothing. So it still confirms first, and now quotes a
        much smaller deviation.
        """
        geq_string = self.advanced_panel.current_geq()
        if not geq_string:
            QMessageBox.information(
                self, "Convert to PEQ",
                "The Graphic EQ curve is flat — there's nothing to convert.")
            return

        available = MAX_BANDS - len(self._filter_rows)
        if available < 1:
            QMessageBox.warning(
                self, "Convert to PEQ",
                f"There's no room for any more bands ({MAX_BANDS}-band limit "
                "already reached). Remove some existing bands first.")
            return

        result = fit_peq(geq_string, max_bands=available, sample_rate=self._sample_rate)
        if result is None:
            QMessageBox.information(
                self, "Convert to PEQ",
                "The Graphic EQ curve is effectively flat — there's nothing to convert.")
            return

        message = (
            f"This replaces the Graphic EQ curve with {len(result.filters)} new "
            f"parametric band(s) fitted to its shape (up to "
            f"~{result.max_error_db:.2f} dB deviation from the original curve). "
            "This is lossy, not an exact match."
        )
        if result.warning:
            message += "\n\n" + result.warning
        message += "\n\nContinue?"
        reply = QMessageBox.question(
            self, "Convert to PEQ", message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for filt in result.filters:
            self._add_filter_row(filt)
        channel = self._channel_sections[self._current_section_idx].channel
        self.advanced_panel.load_geq_for_channel(channel, "")
        self._refresh_graph_and_raw()
        self._on_field_changed()

    def _set_link_state_silently(self, linked: bool) -> None:
        self._btn_link_channels.blockSignals(True)
        self._btn_link_channels.setChecked(linked)
        self._btn_link_channels.blockSignals(False)
        self._update_link_channels_icon()
        self._update_section_button_styles()

    def _collect_current_section_to(self, idx: int) -> None:
        """Save the live UI state into _channel_sections[idx]."""
        if idx >= len(self._channel_sections):
            return
        section = self._channel_sections[idx]
        section.filters = [row.filter for row in self._filter_rows]
        section.graphic_eq = self.advanced_panel.current_geq()
        # A zero trim means "no per-channel preamp" — no line is written.
        trim = float(self._section_trim_spin.value())
        section.preamp_db = trim if abs(trim) >= 0.05 else None
        # Delay: while the spin still reads exactly the value it was loaded
        # with, leave the stored delay untouched — a delay stored in samples
        # must round-trip verbatim, not be rewritten as its ms display value.
        # Only a real edit rewrites it (in ms; zero means "no delay").
        delay = float(self._section_delay_spin.value())
        if delay != self._delay_loaded_value:
            section.delay_ms = delay if delay > 0 else None
            section.delay_samples = None

    def _load_section_from(self, idx: int) -> None:
        """Load _channel_sections[idx] into the filter strip, trim/delay row
        and GEQ editor. The GEQ editor must be loaded before the graph/raw
        refresh below — refreshing collects the current section from the UI,
        and a stale editor would leak the previous section's GEQ in."""
        section = self._channel_sections[idx]
        self.advanced_panel.load_geq_for_channel(section.channel, section.graphic_eq)

        self._section_trim_spin.blockSignals(True)
        self._section_trim_spin.setValue(section.preamp_db or 0.0)
        self._section_trim_spin.blockSignals(False)

        self._section_delay_spin.blockSignals(True)
        self._section_delay_spin.setValue(section.delay_ms or 0.0)
        self._section_delay_spin.blockSignals(False)
        # Remember the post-quantisation spin value: "unchanged" is defined
        # against what the spin actually displays, not the raw float.
        self._delay_loaded_value = float(self._section_delay_spin.value())

        for row in list(self._filter_rows):
            self.rows_layout.removeWidget(row)
            row.deleteLater()
        self._filter_rows.clear()

        for filt in section.filters:
            self._add_filter_row(filt)

        self._refresh_graph_and_raw()

    def _switch_section(self, new_idx: int) -> None:
        if new_idx == self._current_section_idx:
            return
        self._clear_solo()
        self._collect_current_section_to(self._current_section_idx)
        self._current_section_idx = new_idx
        self._update_section_button_styles()
        self._load_section_from(new_idx)
        self._on_field_changed()

    # ── channel preset helpers ────────────────────────────────────────

    def _layout_channel_tokens(self) -> set[str]:
        return layout_channel_tokens(self._channel_sections)

    def _detect_preset(self) -> str:
        return detect_preset(self._channel_sections)

    def _sync_preset_combo(self) -> None:
        """Show the preset matching the current sections; blank for a layout
        that is not one of the presets."""
        detected = self._detect_preset()
        self._preset_combo.blockSignals(True)
        if detected:
            self._preset_combo.setCurrentText(detected)
        else:
            self._preset_combo.setCurrentIndex(-1)
        self._preset_combo.blockSignals(False)

    def _on_preset_changed(self, preset_name: str) -> None:
        if self._loading:
            return
        if preset_name == CUSTOM_LAYOUT_LABEL:
            current = " ".join(dict.fromkeys(
                tok for s in self._channel_sections for tok in s.channel.split()))
            text, ok = QInputDialog.getText(
                self, "Custom Channel Layout",
                "Space-separated channel names (e.g. L R C LFE, or channel "
                "numbers):", text=current)
            tokens = list(dict.fromkeys(text.split())) if ok else []
            if not tokens:
                self._sync_preset_combo()  # cancelled — keep the prior layout
                return
            self._apply_channel_layout(tokens, "Custom")
            return
        channels = CHANNEL_PRESETS.get(preset_name)
        if not channels:
            return
        self._apply_channel_layout(list(channels), preset_name)
        # _apply_channel_layout can bail out early (user cancelled a
        # destructive-change confirmation) without applying anything — only
        # notice if the layout actually took effect.
        if (preset_name in NON_STANDARD_CHANNEL_LAYOUTS
                and self._detect_preset() == preset_name):
            QMessageBox.information(
                self, "Channel Layout Notice",
                NON_STANDARD_CHANNEL_LAYOUT_WARNING.format(name=preset_name))

    def _apply_channel_layout(self, channels: list[str], layout_name: str) -> None:
        """Rebuild the section list for *channels* (preset or custom), keeping
        matching sections and seeding new ones from the current section."""
        self._clear_solo()
        # Save current section before potentially replacing it
        self._collect_current_section_to(self._current_section_idx)

        # When linked every section is a clone, and a lone section (e.g. "all")
        # is cloned into every new channel — in both cases nothing unique can
        # be lost. Otherwise warn about content sections the layout would drop.
        new_set = set(channels)
        seed_new_sections = self._channels_linked() or len(self._channel_sections) == 1
        losing = [] if seed_new_sections else [
            s.channel for s in self._channel_sections
            if s.channel not in new_set and s.has_content()
        ]
        if losing:
            reply = QMessageBox.question(
                self, "Apply Channel Layout",
                f"Applying \"{layout_name}\" will remove sections for: "
                f"{', '.join(losing)}.\nTheir filter bands will be lost. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._sync_preset_combo()  # revert to the unchanged layout
                return

        # Build new section list, re-using existing sections where channel
        # names match. Newly created channels are seeded with the current
        # section's bands, GraphicEQ and delay instead of starting blank.
        existing = {s.channel: s for s in self._channel_sections}
        src = self._channel_sections[self._current_section_idx]
        new_sections: list[ChannelSection] = []
        for ch in channels:
            section = existing.get(ch)
            if section is None:
                if seed_new_sections:
                    section = src.clone()
                    section.channel = ch
                else:
                    section = ChannelSection(channel=ch)
            new_sections.append(section)
        self._channel_sections = new_sections
        self._current_section_idx = 0
        self._rebuild_section_bar()
        self._load_section_from(0)
        self._sync_preset_combo()  # custom layouts show as a blank combo
        self._on_field_changed()

    def _on_section_delay_value_changed(self, _value: float) -> None:
        # When channels are linked, _on_field_changed propagates the delay
        # to every section along with the bands and Graphic EQ.
        self._on_field_changed()

    def _measurable_channel_names(self) -> list[str] | None:
        """The single-channel token for every current section, in section
        order, or None with a user-facing explanation already shown if the
        current layout can't be measured (linked channels, fewer than two
        sections, a multi-channel or non-standard section name)."""
        if self._channels_linked():
            QMessageBox.information(
                self, "Unlink Channels First",
                "Automatic time-alignment measures each physical channel "
                "independently, so channels must be unlinked first — turn "
                "off \"Link Channels\" — before measuring.")
            return None
        if len(self._channel_sections) < 2:
            QMessageBox.information(
                self, "Nothing to Align",
                "Add at least two channel sections before measuring "
                "time-alignment — there's nothing to align with only one.")
            return None

        names: list[str] = []
        for section in self._channel_sections:
            tokens = [t for t in section.channel.split() if t.upper() != "ALL"]
            if len(tokens) != 1 or tokens[0].upper() not in time_alignment.STANDARD_CHANNEL_ORDER:
                QMessageBox.warning(
                    self, "Unsupported Channel Layout",
                    f"Section {section.channel!r} isn't a single standard "
                    "physical channel. Measurement only supports "
                    f"{', '.join(time_alignment.STANDARD_CHANNEL_ORDER)}, one "
                    "per section.")
                return None
            names.append(tokens[0].upper())
        return names

    def _on_measure_delay_clicked(self) -> None:
        channel_names = self._measurable_channel_names()
        if channel_names is None:
            return

        reply = QMessageBox.question(
            self, "Measure Time Alignment",
            "This plays a short sweep through each speaker in turn while a "
            "measurement microphone (the system's default recording device) "
            "listens from the main listening position. The room should be "
            "quiet for the few seconds this takes. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._collect_current_section_to(self._current_section_idx)
        self.btn_measure_delay.setEnabled(False)
        self.setCursor(Qt.CursorShape.WaitCursor)

        engine = self._tone_engine_factory()
        sample_rate = self._sample_rate
        result: dict = {}

        def _worker() -> None:
            try:
                result["delays"] = time_alignment.run_measurement(
                    channel_names, engine, sample_rate)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user below
                result["error"] = exc

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        timer = QTimer(self)

        def _poll() -> None:
            if thread.is_alive():
                return
            timer.stop()
            self.unsetCursor()
            self.btn_measure_delay.setEnabled(True)
            if "error" in result:
                QMessageBox.warning(self, "Measurement Failed", str(result["error"]))
                return
            delays = result["delays"]
            for section in self._channel_sections:
                tokens = [t for t in section.channel.split() if t.upper() != "ALL"]
                name = tokens[0].upper()
                if name in delays:
                    section.delay_ms = delays[name] if delays[name] > 0 else None
                    section.delay_samples = None
            self._load_section_from(self._current_section_idx)
            self._on_field_changed()
            QMessageBox.information(
                self, "Measurement Complete",
                "Measured delays have been applied to every channel.")

        timer.timeout.connect(_poll)
        timer.start(150)

    def _on_section_trim_value_changed(self, _value: float) -> None:
        # A tracked edit like the delay; linked propagation carries it too.
        self._on_field_changed()

    def _on_add_section_clicked(self) -> None:
        channel, ok = QInputDialog.getText(
            self, "Add Channel Section",
            "Channel name (e.g. L, R, C, LFE, all):",
            text="L",
        )
        if not ok or not channel.strip():
            return
        self._collect_current_section_to(self._current_section_idx)
        if self._channels_linked():
            new_section = self._channel_sections[self._current_section_idx].clone()
            new_section.channel = channel.strip()
        else:
            new_section = ChannelSection(channel=channel.strip())
        self._channel_sections.append(new_section)
        self._current_section_idx = len(self._channel_sections) - 1
        self._rebuild_section_bar()
        self._load_section_from(self._current_section_idx)
        self._on_field_changed()

    def _on_remove_section_clicked(self) -> None:
        if len(self._channel_sections) <= 1:
            return
        section = self._channel_sections[self._current_section_idx]
        reply = QMessageBox.question(
            self, "Remove Channel Section",
            f'Remove the "{section.channel}" channel section and its filters?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._channel_sections.pop(self._current_section_idx)
        self._current_section_idx = max(0, self._current_section_idx - 1)
        self._rebuild_section_bar()
        self._load_section_from(self._current_section_idx)
        self._on_field_changed()

    # ── load / save ──────────────────────────────────────────────────

    def load_profile(self, meta: ProfileMeta, config: ProfileConfig, *, is_active: bool, is_bypassed: bool, is_readonly: bool = False) -> None:
        # `_loading` suppresses field-change handlers (link channels, preset,
        # recompute…) while widgets are populated. It MUST be cleared even if a
        # step below raises, or every guarded control silently no-ops until the
        # next clean load — so the whole body runs under try/finally.
        self._loading = True
        try:
            self._meta = meta
            self._solo_index = None

            self.name_edit.setText(meta.name)
            self.desc_edit.setText(meta.description)
            self._set_led_state(is_active=is_active, is_bypassed=is_bypassed)

            self._populate_device_combo()  # pick up newly APO-configured endpoints

            # Preamp on/off and auto preamp are per-session UI states, not saved properties —
            # every profile opens with preamp enabled and auto preamp disabled.
            self.btn_preamp_toggle.blockSignals(True)
            self.btn_preamp_toggle.setChecked(True)
            self.btn_preamp_toggle.blockSignals(False)
            self.btn_auto_preamp.blockSignals(True)
            self.btn_auto_preamp.setChecked(False)
            self.btn_auto_preamp.blockSignals(False)
            self.preamp_spin.setReadOnly(False)

            self.btn_bypass.blockSignals(True)
            self.btn_bypass.setChecked(is_bypassed)
            self.btn_bypass.blockSignals(False)

            self._load_config_into_ui(config)

            self._refresh_ir_result()
            self._update_headroom_label()
            self._refresh_graph_and_raw()
        finally:
            self._loading = False

        # A freshly loaded profile is the undo baseline — nothing to undo yet.
        self._undo_commit_timer.stop()
        self._history.reset(self._capture_snapshot())
        self._update_undo_buttons()

        # Loading a profile is a *view*, not an edit: it must not mark the
        # editor dirty or schedule a live preview, or single-clicking a row in
        # the sidebar would push that profile to Equalizer APO and make it
        # audible (effectively "active") without the user activating it.
        # Callers that genuinely change state (Clear, restoring an unsaved
        # edit) declare their own dirty/preview intent after this returns.

        self.btn_save.setEnabled(not is_readonly)
        self.btn_clear.setEnabled(not is_readonly)
        self.btn_clear_all.setEnabled(not is_readonly)
        self.name_edit.setReadOnly(is_readonly)
        self.desc_edit.setReadOnly(is_readonly)

    # ── undo / redo ──────────────────────────────────────────────────

    def _capture_snapshot(self) -> EditorSnapshot:
        # build_config() already returns a fully owned structure (deep copy of
        # the baseline + cloned sections), so the snapshot needs no extra copy.
        return EditorSnapshot(
            config=self.build_config(),
            name=self.name_edit.text(),
            description=self.desc_edit.text(),
            section_idx=self._current_section_idx,
        )

    def _commit_undo_snapshot(self) -> None:
        self._history.commit(self._capture_snapshot())
        self._update_undo_buttons()

    def _flush_pending_undo_commit(self) -> None:
        """Commit an in-flight debounced edit so it becomes its own undo step
        before an undo/redo navigates the history."""
        if self._undo_commit_timer.isActive():
            self._undo_commit_timer.stop()
            self._commit_undo_snapshot()

    def undo(self) -> None:
        self._flush_pending_undo_commit()
        snapshot = self._history.undo()
        if snapshot is not None:
            self._restore_snapshot(snapshot)

    def redo(self) -> None:
        self._flush_pending_undo_commit()
        snapshot = self._history.redo()
        if snapshot is not None:
            self._restore_snapshot(snapshot)

    def _restore_snapshot(self, snapshot: EditorSnapshot) -> None:
        # Same guard contract as load_profile: populate under _loading in
        # try/finally so field-change handlers stay quiet and the flag can
        # never be left stuck.
        self._loading = True
        try:
            self._clear_solo()
            self.name_edit.setText(snapshot.name)
            self.desc_edit.setText(snapshot.description)
            if self._meta is not None:
                self._meta.name = snapshot.name
                self._meta.description = snapshot.description
            self._load_config_into_ui(snapshot.config)
            # Put the user back on the section they were editing (the restore
            # collapses/relinks, so clamp to the rebuilt list).
            idx = min(snapshot.section_idx, len(self._channel_sections) - 1)
            if idx != self._current_section_idx:
                self._current_section_idx = idx
                self._update_section_button_styles()
                self._load_section_from(idx)
            self._refresh_ir_result()
            self._update_headroom_label()
            self._refresh_graph_and_raw()
        finally:
            self._loading = False
        self._update_undo_buttons()
        # A restore is a tracked state change: the profile diverges from disk
        # again, so mark dirty and push the live preview — but do NOT arm the
        # commit debounce (the history already holds this state).
        self.dirty.emit()

    def _update_undo_buttons(self) -> None:
        """Undo/redo have no buttons of their own — they are keyboard-only
        (Ctrl+Z / Ctrl+Y) plus the Edit menu. This tells the owner when each
        is available so the menu items grey out when there is nothing to do."""
        self.history_changed.emit(
            self._history.can_undo or self._undo_commit_timer.isActive(),
            self._history.can_redo)

    def _load_config_into_ui(self, config: ProfileConfig) -> None:
        """Populate every config-driven widget from *config*: device, preamp
        value, channel sections (+ link state, section bar, primary section's
        rows/trim/delay) and the advanced tabs. Callers hold the `_loading`
        guard; per-session controls (preamp on/off, auto, bypass) and profile
        metadata are the caller's business — this is shared between
        load_profile and undo/redo snapshot restores."""
        self.device_combo.blockSignals(True)
        self._select_device(config.device)
        self.device_combo.blockSignals(False)

        self.preamp_spin.blockSignals(True)
        preamp_value = config.preamp if config.preamp is not None else 0.0
        self.preamp_spin.setValue(preamp_value)
        self._preamp_fallback = preamp_value
        self._preamp_saved = preamp_value
        self.preamp_spin.blockSignals(False)

        # Keep the parsed config as the round-trip baseline: build_config()
        # starts from a copy of it, so fields the editor does not own
        # (extra_lines, later Stage lines, …) survive an edit+save.
        self._loaded_config = copy.deepcopy(config)

        # Build the channel sections list via clone() so every field —
        # including ones added later — is carried and independent of the
        # baseline (dropping delay_ms here once wiped per-channel delays).
        if config.channel_sections:
            self._channel_sections = [s.clone() for s in config.channel_sections]
        else:
            self._channel_sections = [ChannelSection("L"), ChannelSection("R")]
        self._current_section_idx = 0
        # Linked by default; a profile whose sections diverge loads unlinked.
        # A linked layout collapses to one combined section so it shows as a
        # single 'L R' strip regardless of whether the file stored it as one
        # block or as separate identical L/R blocks.
        linked = self._sections_identical()
        if linked:
            self._collapse_into_single_section()
        self._set_link_state_silently(linked)
        self._rebuild_section_bar()

        # Open in BW mode when the profile's filters are stored that way.
        self._set_bw_mode(any(
            f.use_bw for s in self._channel_sections for f in s.filters))

        # Load primary section
        primary = self._channel_sections[0]
        self._section_trim_spin.blockSignals(True)
        self._section_trim_spin.setValue(primary.preamp_db or 0.0)
        self._section_trim_spin.blockSignals(False)
        self._section_delay_spin.blockSignals(True)
        self._section_delay_spin.setValue(primary.delay_ms or 0.0)
        self._section_delay_spin.blockSignals(False)
        self._delay_loaded_value = float(self._section_delay_spin.value())

        for row in list(self._filter_rows):
            self.rows_layout.removeWidget(row)
            row.deleteLater()
        self._filter_rows.clear()
        for filt in primary.filters:
            self._add_filter_row(filt)

        # Load advanced panel
        self.advanced_panel.load_from_config(config)
        self.advanced_panel.load_geq_for_channel(primary.channel, primary.graphic_eq)

    def current_meta_fields(self) -> tuple[str, str]:
        return self.name_edit.text().strip(), self.desc_edit.text().strip()

    def build_config(self, *, for_preview: bool = False) -> ProfileConfig:
        # Flush current UI state into the sections list before building
        self._collect_current_section_to(self._current_section_idx)

        device = str(self.device_combo.currentData() or "")
        if device.lower() == "all" or not device:
            from rumble.core.audio_devices import first_active_device_pattern
            device = first_active_device_pattern()

        # Start from a deep copy of the loaded config so fields the editor
        # does not own (extra_lines, …) round-trip instead of being dropped.
        # Deep because build_config runs on every graph refresh — an aliased
        # baseline would let preview-side mutation leak into "saved" state.
        config = (copy.deepcopy(self._loaded_config)
                  if self._loaded_config is not None else ProfileConfig())
        config.device = device
        config.preamp = float(self.preamp_spin.value())
        config.channel_sections = [s.clone() for s in self._channel_sections]

        # Solo masking applies only to the preview config (what APO plays
        # during the audition) — saves and the raw view keep every band.
        if for_preview and self._solo_index is not None:
            targets = (range(len(config.channel_sections))
                       if self._channels_linked() else [self._current_section_idx])
            for idx in targets:
                if idx < len(config.channel_sections):
                    section = config.channel_sections[idx]
                    section.filters = [
                        dataclass_replace(f, enabled=(i == self._solo_index))
                        for i, f in enumerate(section.filters)
                    ]

        self.advanced_panel.apply_to_config(config)
        return config

    # ── change tracking / live preview ───────────────────────────────

    def _on_field_changed(self) -> None:
        """A tracked edit: recompute, push the live preview, and mark dirty."""
        self._recompute(mark_dirty=True)

    def _refresh_preview_only(self) -> None:
        """A per-session control changed (Pre-Amp on/off, Auto): recompute and
        push the live preview, but do not mark the profile dirty so the
        unsaved-changes warning is not raised. Saving still persists the
        resulting preamp value because Save reads the live editor state."""
        self._recompute(mark_dirty=False)

    def _recompute(self, *, mark_dirty: bool) -> None:
        if self._loading:
            return
        if mark_dirty:
            # (Re)arm the undo commit debounce — only tracked edits create
            # history steps; preview-only auditions never do.
            self._undo_commit_timer.start()
            self._update_undo_buttons()
        if self._channels_linked() and len(self._channel_sections) > 1:
            self._propagate_linked_sections()
        self._refresh_ir_result()
        if self.btn_auto_preamp.isChecked():
            self._apply_auto_preamp()
        self._update_headroom_label()
        self._refresh_graph_and_raw()
        (self.dirty if mark_dirty else self.preview_only).emit()

    def _sort_filter_rows(self) -> bool:
        """Sort the band rows by frequency and renumber 1..n. Only invoked by
        the Re-order button — bands never move on their own while editing.
        Returns True if the order changed."""
        sorted_rows = sorted(self._filter_rows, key=lambda r: r.filter.fc)
        if sorted_rows == self._filter_rows:
            return False
        self._filter_rows = sorted_rows
        for row in self._filter_rows:
            self.rows_layout.removeWidget(row)
        for i, row in enumerate(self._filter_rows):
            self.rows_layout.insertWidget(i, row)
            row.set_index(i + 1)
        return True

    def set_sample_rate(self, sample_rate: int) -> None:
        """Set the processing sample rate used for response and preamp maths
        (a global setting; changing it re-derives the graph, headroom and the
        ms display of samples-denominated delays)."""
        sample_rate = int(sample_rate or eq_math.DEFAULT_SAMPLE_RATE)
        if sample_rate == self._sample_rate:
            return
        self._sample_rate = sample_rate

        # Rumble 1.1/5.2: re-check every existing band's Fc against the new
        # rate's Nyquist ceiling — not just newly-typed values — since a band
        # that was safe at the old rate can become invalid or borderline the
        # moment the effective rate drops (e.g. an output-device switch).
        for row in self._filter_rows:
            row.set_sample_rate(sample_rate)

        # Re-derive the ms display of delays stored in samples at the new
        # rate — a display conversion, not an edit: the stored samples figure
        # is untouched and nothing is marked dirty.
        for section in self._channel_sections:
            if section.delay_samples is not None:
                section.delay_ms = (section.delay_samples / sample_rate) * 1000.0
        if self._current_section_idx < len(self._channel_sections):
            current = self._channel_sections[self._current_section_idx]
            spin_untouched = (float(self._section_delay_spin.value())
                              == self._delay_loaded_value)
            if current.delay_samples is not None and spin_untouched:
                self._section_delay_spin.blockSignals(True)
                self._section_delay_spin.setValue(current.delay_ms or 0.0)
                self._section_delay_spin.blockSignals(False)
                self._delay_loaded_value = float(self._section_delay_spin.value())

        if not self._loading:
            # The IR's own rate never changes, but the mismatch label compares
            # it against self._sample_rate, which just did — re-derive it.
            # _refresh_ir_result() re-reads via ir_analysis.get_ir_response(),
            # which is cached per (path, mtime) [see editor_panel.py:~538 /
            # ir_analysis.py], so this is a cache hit here, not a re-read; it
            # is called on essentially every recompute elsewhere in this file
            # (_recompute(), load_profile(), undo/redo) so reusing it here
            # keeps set_sample_rate consistent with that pattern instead of
            # duplicating the mismatch-derivation logic.
            self._refresh_ir_result()
            self._update_headroom_label()
            self._refresh_graph_and_raw()

    def _refresh_graph_and_raw(self) -> None:
        # Keep the live spectrum capturing the endpoint this profile targets,
        # not whatever Windows currently calls the default output — see
        # EQGraph.set_spectrum_device(). This is the one path every device
        # change reaches (the combo feeds _on_field_changed -> _recompute),
        # alongside load_profile and undo/redo; set_spectrum_device() itself
        # no-ops unless the target actually changed. It must run BEFORE
        # set_filters() below, which reads the fallback flag it sets.
        device = self.device_combo.currentData()
        self.graph.set_spectrum_device(
            device,
            has_loopback=str(device or "") not in self._capture_only_patterns,
        )
        self.graph.set_graphic_eq(self.advanced_panel.current_geq())
        self.graph.set_ir_response(self._ir_curve_db())
        # The graph shows what the audition hears (solo masking applied);
        # the raw view below keeps showing what would be saved.
        self.graph.set_filters(self._effective_filters(), self._sample_rate)
        self._refresh_raw_config()

    def _refresh_raw_config(self) -> None:
        from rumble.core import profile_file
        meta = self._meta
        config = self.build_config()
        doc = profile_file.ProfileDocument(
            name=self.name_edit.text().strip() or (meta.name if meta else "New Profile"),
            description=self.desc_edit.text().strip(),
            tags=list(meta.tags) if meta else [],
            created=meta.created if meta else "",
            modified=meta.modified if meta else "",
            readonly=bool(meta.readonly) if meta else False,
            builtin=bool(meta.builtin) if meta else False,
            config=config,
        )
        self.raw_config_view.set_text(profile_file.render_profile_text(doc))

    def _reserve_led_width(self) -> None:
        """Pin the LED to exactly its lit width, forever.

        Without this the label sizes itself to its text, so it grew when it lit
        and shrank when it went dark — and since the profile name/description
        column beside it takes the leftover space with a stretch factor, that
        growth ate into the name, re-eliding it every time the LED toggled.

        The width is measured, not guessed, and measured from sizeHint() rather
        than fontMetrics(): the label's real width includes frame margins that
        the raw text advance doesn't know about (44px advance vs 50px laid out,
        which is why an earlier minimum-width attempt never actually bound).
        ensurePolished() forces Qt to resolve the QSS font first — the stylesheet
        sets font-size, so before polish the metrics are for the wrong font.
        """
        label = self.badge_live
        restore = label.text()
        label.setText(_LED_LIT_TEXT)
        label.ensurePolished()
        label.setFixedWidth(label.sizeHint().width())
        label.setText(restore)

    def _set_led_state(self, *, is_active: bool, is_bypassed: bool) -> None:
        """Drive the header LED. It answers one question — is what I'm editing
        what I'm hearing? — and it answers it the way an LED does. Off is off,
        on is on: lit green while you are hearing this profile, dark otherwise.
        There is no third colour and no word for the dark state.

        Bypassed is dark rather than a state of its own. Dark already means
        "you are not hearing this profile", which is exactly true while
        bypassed, and the Bypass button sits inches away saying the rest. The
        bug worth fixing was the LED claiming ACTIVE while bypassed, not a
        missing colour.

        The label holds the lit width while dark, so lighting up never nudges
        A/B, Save, Bypass and Clear along the row.
        """
        lit = is_active and not is_bypassed
        self.badge_live.setText(_LED_LIT_TEXT if lit else "")
        self._reserve_led_width()
        if lit:
            describe(self.badge_live, "Profile status",
                     "This profile is running in Equalizer APO — you are hearing it.")
        else:
            # Dark: say nothing, to a screen reader too. An empty label with a
            # tooltip is an invisible hover target.
            self.badge_live.setToolTip("")
            self.badge_live.setAccessibleName("")
            self.badge_live.setAccessibleDescription("")

    def set_active_state(self, is_active: bool, is_bypassed: bool) -> None:
        self._set_led_state(is_active=is_active, is_bypassed=is_bypassed)
        self.btn_bypass.blockSignals(True)
        self.btn_bypass.setChecked(is_bypassed)
        self.btn_bypass.blockSignals(False)

    # ── theming ──────────────────────────────────────────────────────

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.btn_save.setIcon(icons.icon("save", theme["text_on_accent"], 16))
        self.btn_bypass.setIcon(icons.icon("pause", theme["warning"], 16))
        self.btn_clear.setIcon(icons.icon("delete", theme["error"], 16))
        self.btn_clear_all.setIcon(icons.icon("delete", theme["text_muted"], 14))
        self.btn_add_section.setIcon(icons.icon("add", theme["text_muted"], 12))
        self.btn_add_band.setIcon(icons.icon("add", theme["text_muted"], 12))
        self._update_link_channels_icon()
        self.graph.set_theme(theme)
        self._restyle_graph_frame()
        self._graph_frame.set_theme(theme)
        self.vu_meter.set_theme(theme)
        self.advanced_panel.set_theme(theme)
        self.raw_config_view.set_theme(theme)
        for row in self._filter_rows:
            row.set_theme(theme)
        self.name_edit.set_colors(theme["text_primary"], theme["text_muted"])
        self.desc_edit.set_colors(theme["text_secondary"], theme["text_muted"])
        # A theme can carry a different font size, so the LED's pinned width has
        # to be re-measured against the new one.
        self._reserve_led_width()
        self._restyle_header_bar()
        self._restyle_main_tab_row()
        self._update_section_button_styles()
        self._update_qbw_mode_styles()

    def _restyle_header_bar(self) -> None:
        self._header_bar.setStyleSheet(
            f"QFrame {{ background-color: {self.theme['bg_surface']}; "
            f"border-bottom: 1px solid {self.theme['border_subtle']}; }}"
        )
