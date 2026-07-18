"""Main application window: profile panel + editor panel, menubar, status bar,
mandatory close-to-tray behaviour."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QAction, QActionGroup, QCloseEvent, QIcon, QKeySequence, QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QMainWindow, QMenuBar, QMessageBox, QSplitter, QStatusBar, QVBoxLayout, QWidget,
)

from rumble.core.custom_themes import export_theme, import_theme_file, save_custom_theme
from rumble.core import apo_path, audio_devices, bass_management, crossfeed, tone_trim
from rumble.core.apo_config import ProfileConfig
from rumble.core import hearing_test, ir_analysis, listener_slots, rew_import, settings_reset
from rumble.core.audio.engine import ToneEngine
from rumble.core.profile_manager import ProfileManager, make_flat_config
from rumble.ui.a11y import wrap_tooltip
from rumble.ui.abx_dialog import ABXDialog
from rumble.ui.about_dialog import AboutDialog
from rumble.ui.apo_coverage_banner import (REASON_ENHANCEMENTS_OFF, REASON_NOT_HOOKED,
                                          ApoCoverageBanner)
from rumble.ui.autoeq_dialog import MeasurementBrowserDialog
from rumble.ui.auto_switch_adapter import AutoSwitchAdapter
from rumble.ui.auto_switch_dialog import AutoSwitchDialog
from rumble.ui.bass_management_dialog import BassManagementDialog
from rumble.ui.density import dp
from rumble.ui.device_format_adapter import DeviceFormatAdapter
from rumble.ui.loudness_volume_adapter import LoudnessVolumeAdapter
from rumble.ui.editor_panel import EditorPanel, layout_channel_tokens
from rumble.ui.frameless import make_close_button, make_drag_filler, make_minimize_button
from rumble.ui.hearing_dialog import HearingSuiteDialog
from rumble.ui.measure_dialog import MeasureDialog
from rumble.ui.new_profile_dialog import NewProfileDialog
from rumble.ui.profile_panel import ProfilePanel, panel_width
from rumble.ui.rew_import_dialog import RewMeasurementImportDialog
from rumble.ui.settings_dialog import CATEGORIES as SETTINGS_CATEGORIES, SettingsDialog
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme, ordered_theme_names
from rumble.ui.tone_trim_widget import ToneTrimWidget
from rumble.ui.tray_icon import TrayIcon


# Rumble 5.4: Equalizer APO's own Configuration Reference wiki explicitly
# warns that a low-frequency filter scoped only to the LFE channel often has
# no audible effect, because most systems apply bass redirection downstream
# of APO's own processing (in the sound card driver or AV receiver), and
# recommends applying low-frequency filters to all channels instead:
# "Many audio systems apply bass redirection after Equalizer APO has
# processed the signal, so filters applied to the LFE channel won't be
# effective for redirected sound. [...] it's recommended to apply low
# frequency filters to all channels." (sourceforge.net/p/equalizerapo/wiki/
# Configuration reference/, "Channel" section, read 2026-07-10).
# 120 Hz is not an arbitrary pick: it is the Dolby Digital LFE channel's own
# defined band limit (a hard brickwall, not a rolloff, per Dolby's published
# spec) — content above it was never carried by the LFE channel to begin
# with, so a filter placed there targets frequencies the format itself
# reserves for full-range channels.
_LFE_BASS_FC_MAX_HZ = 120.0


def _config_has_lfe_only_bass_filter(config: ProfileConfig) -> bool:
    """True if any enabled boost/cut filter at or below the LFE channel's
    120 Hz band limit is scoped to a section carrying LFE and nothing else."""
    for section in config.channel_sections:
        tokens = {tok.upper() for tok in section.channel.split()}
        if tokens != {"LFE"}:
            continue
        for f in section.filters:
            if (f.enabled and f.supports_gain() and f.gain != 0.0
                    and f.fc <= _LFE_BASS_FC_MAX_HZ):
                return True
    return False


# Measured (offscreen, 100% scale): growing the window by exactly this many
# px grows the EQGraph widget by the same amount, 1:1 -- every other element
# between the window edge and the graph is fixed-height or horizontally
# scrolling, so the graph is the sole sink for any added window height. This
# is exactly the graph's own current height (237px), so adding it doubles
# the graph's vertical space -- see the "Graph height" Appearance setting.
_GRAPH_DOUBLE_HEIGHT_EXTRA_PX = 237


def window_fixed_size(graph_double_height: bool = False) -> tuple[int, int]:
    """Fixed window size: the compact 1000x580 layout is the app's one base
    layout, fitting any common screen with room to spare; the Double (2x)
    interface size scales it to 2000x1160, which fits displays of 2000x1280
    and up. The extra height over the original 520 goes entirely to the
    frequency graph (it takes all spare vertical space); the bands editor
    scrolls horizontally inside the window. `graph_double_height` (the
    Appearance "Graph height" setting) adds further height for the same
    reason, roughly doubling the graph on its own -- taller than 520->580's
    one-time layout compaction, so it needs a taller display."""
    height = dp(660, 580)
    if graph_double_height:
        height += _GRAPH_DOUBLE_HEIGHT_EXTRA_PX
    return (dp(1280, 1000), height)


class MainWindow(QMainWindow):
    """Top-level window combining the profile sidebar and the EQ editor."""

    def __init__(self, manager: ProfileManager, theme_engine: ThemeEngine, app_icon: QIcon):
        super().__init__()
        self.manager = manager
        self.theme_engine = theme_engine
        self.theme = theme_engine.t
        self._app_icon = app_icon
        self._dirty = False
        # The live preview reflects editor state that differs from the saved
        # profile. True whenever _dirty is, plus when a per-session control
        # (Pre-Amp/Auto toggle) has diverged the sound without a tracked edit.
        # Drives the preview lifecycle (resume on unbypass) independently of the
        # unsaved-changes warning, which stays keyed on _dirty alone.
        self._preview_diverged = False
        # True while A/B compare holds the saved profile audible: the preview
        # of unsaved edits is parked (never scheduled) until release.
        self._ab_comparing = False
        self._loaded_profile_id: str | None = None
        self._closing_to_quit = False
        self._safety_warning_shown = False
        # The crossfeed mode the tray toggle restores when switched back on
        # from Off -- seeded from the persisted mode so a user who left it on
        # "global" gets "global" back, defaulting to "profile" otherwise.
        persisted_cf_mode = str(self.manager.settings.get("crossfeed_mode", "profile"))
        self._last_crossfeed_mode = persisted_cf_mode if persisted_cf_mode != "off" else "profile"

        self.setWindowTitle("RUMBLE")
        self.setWindowIcon(app_icon)
        self.setFixedSize(*window_fixed_size(
            bool(self.manager.settings.get("graph_double_height"))))
        # Frameless: the menu row (built in _build_menubar) carries its own
        # drag strip + close/minimize buttons in place of the native titlebar.
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)

        # Debounce for live tuning: collapses a burst of band tweaks (e.g. a
        # graph drag) into one preview write shortly after the last change.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self._push_live_preview)

        # Settings -> Ear Calibration's "check your volume" sample buttons:
        # a lazily-created engine + bypass guard, released once playback
        # ends (polled non-blocking, mirrors hearing_dialog.py's own
        # tone-repeat sequencing) so the Settings dialog never freezes and
        # the sample is never coloured by whatever profile is active.
        self._reference_sound_engine: ToneEngine | None = None
        self._reference_sound_bypass_guard: hearing_test.BypassGuard | None = None
        self._reference_sound_poll_timer = QTimer(self)
        self._reference_sound_poll_timer.setInterval(200)
        self._reference_sound_poll_timer.timeout.connect(self._on_reference_sound_poll)

        self._build_menubar()
        self._build_central_widget()
        self._build_status_bar()
        self._build_tray_icon()

        # Live Windows device-format watcher. The adapter owns a Qt-free
        # DeviceFormatWatcher and re-emits its foreign-thread callback as the
        # main-thread `format_changed` signal (see device_format_adapter.py).
        self._format_adapter = DeviceFormatAdapter(self)
        self._format_adapter.format_changed.connect(self._on_device_format_changed)
        self._format_adapter.start()

        # Phase 3 automatic profile switching. The adapter is always
        # constructed (so the header/tray toggle can turn it on later
        # without a restart) but its SessionWatcher/QTimer only actually run
        # while auto_switch_enabled is true, to avoid needless pycaw polling
        # on the majority of machines that leave the feature off (its
        # DEFAULT_SETTINGS default).
        self._auto_switch_adapter = AutoSwitchAdapter(self.manager, self)
        self._auto_switch_adapter.switched.connect(self._on_auto_switched)
        if self.manager.settings.get("auto_switch_enabled", False):
            self._auto_switch_adapter.start()

        # Volume-knob tracking for the ISO 226 loudness layer: always
        # constructed and started (same shape as the format adapter above) --
        # its per-second tick early-outs to a couple of dict reads while the
        # layer is disabled or uncalibrated, so machines that never enable
        # it pay effectively nothing (see loudness_volume_adapter.py).
        self._loudness_volume_adapter = LoudnessVolumeAdapter(self.manager, self)
        self._loudness_volume_adapter.start()

        # Refresh profiles from disk to pick up any new/modified/deleted profiles
        self.manager.refresh_profiles()
        self._load_profile_list()
        # Warm the manager's detected-sample-rate cache BEFORE the first
        # profile load so effective_sample_rate() (called during profile
        # load) is a cache hit instead of making its own one-off live COM
        # query.
        self._refresh_device_format()
        self._check_apo_coverage()
        self._load_selected_or_active()
        self._update_status_bar()

        # Apply the glass drop-shadow/glow to this window's primary buttons and
        # cards. The startup theme is applied to the QApplication before this
        # window exists, so the per-widget effects must be (re)applied here.
        from rumble.ui.theme import effects
        effects.refresh_tree(self, self.theme)

    # ── construction ─────────────────────────────────────────────────

    def _build_menubar(self) -> None:
        # Built manually (not self.menuBar()) so it can be wrapped in a row
        # with the drag filler + minimize/close buttons and installed via
        # setMenuWidget() -- this piggybacks on the menu row's existing
        # height budget instead of costing the fixed window any extra space.
        menubar = QMenuBar(self)

        file_menu = menubar.addMenu("&File")
        self.action_new_profile = QAction("&New Profile…", self)
        self.action_new_profile.setShortcut(QKeySequence.StandardKey.New)
        self.action_new_profile.triggered.connect(self._on_new_profile)
        self.action_import = QAction("&Import Profiles…", self)
        self.action_import.setShortcut(QKeySequence.StandardKey.Open)
        self.action_import.triggered.connect(self._on_import)
        self.action_import_single = QAction("Import &Single Profile…", self)
        self.action_import_single.triggered.connect(self._on_import_single)
        self.action_import_peace = QAction("Import from &Peace…", self)
        self.action_import_peace.triggered.connect(self._on_import_peace)
        self.action_import_rew = QAction("Import from &REW…", self)
        self.action_import_rew.triggered.connect(self._on_import_rew)
        self.action_export = QAction("&Export Profile…", self)
        self.action_export.setShortcut(QKeySequence("Ctrl+E"))
        self.action_export.triggered.connect(self._on_export)
        self.action_backup = QAction("&Backup Library…", self)
        self.action_backup.triggered.connect(self._on_backup)
        self.action_restore = QAction("&Restore Library…", self)
        self.action_restore.triggered.connect(self._on_restore)
        self.action_import_theme = QAction("Import &Theme…", self)
        self.action_import_theme.triggered.connect(self._on_import_theme)
        self.action_export_theme = QAction("Export Current Theme…", self)
        self.action_export_theme.triggered.connect(self._on_export_theme)
        self.action_quit = QAction("&Quit RUMBLE", self)
        self.action_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.action_quit.triggered.connect(self._quit_application)
        for action in (self.action_new_profile, self.action_import,
                       self.action_import_single, self.action_import_peace,
                       self.action_import_rew, self.action_export):
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction(self.action_backup)
        file_menu.addAction(self.action_restore)
        file_menu.addSeparator()
        file_menu.addAction(self.action_import_theme)
        file_menu.addAction(self.action_export_theme)
        file_menu.addSeparator()
        file_menu.addAction(self.action_quit)

        edit_menu = menubar.addMenu("&Edit")
        self.action_undo = QAction("&Undo", self)
        self.action_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.action_undo.setEnabled(False)
        self.action_undo.triggered.connect(lambda: self.editor_panel.undo())
        self.action_redo = QAction("Re&do", self)
        self.action_redo.setEnabled(False)
        self.action_redo.setShortcuts([QKeySequence("Ctrl+Y"),
                                       QKeySequence("Ctrl+Shift+Z")])
        self.action_redo.triggered.connect(lambda: self.editor_panel.redo())
        edit_menu.addAction(self.action_undo)
        edit_menu.addAction(self.action_redo)
        edit_menu.addSeparator()
        self.action_rename = QAction("&Rename Profile…", self)
        self.action_rename.setShortcut(QKeySequence("F2"))
        self.action_rename.triggered.connect(self._on_rename_current)
        self.action_duplicate = QAction("&Duplicate Profile…", self)
        self.action_duplicate.setShortcut(QKeySequence("Ctrl+D"))
        self.action_duplicate.triggered.connect(self._on_duplicate_current)
        self.action_delete = QAction("De&lete Profile…", self)
        self.action_delete.triggered.connect(self._on_delete_current)
        self.action_lock = QAction("&Lock/Unlock Profile", self)
        self.action_lock.setShortcut(QKeySequence("Ctrl+L"))
        self.action_lock.triggered.connect(self._on_lock_current)
        for action in (self.action_rename, self.action_duplicate, self.action_delete,
                       self.action_lock):
            edit_menu.addAction(action)

        tools_menu = menubar.addMenu("&Tools")
        self.action_autoeq = QAction("&Browse Measurement Database…", self)
        self.action_autoeq.triggered.connect(self._open_autoeq)
        tools_menu.addAction(self.action_autoeq)
        self.action_config_health = QAction("Config &Health…", self)
        self.action_config_health.triggered.connect(self._open_config_health)
        tools_menu.addAction(self.action_config_health)
        self.action_coexistence = QAction("Foreign-Tool &Coexistence…", self)
        self.action_coexistence.triggered.connect(self._open_coexistence_dashboard)
        tools_menu.addAction(self.action_coexistence)
        self.action_crossover = QAction("Cross&over Design…", self)
        self.action_crossover.triggered.connect(self._open_crossover_design)
        tools_menu.addAction(self.action_crossover)
        self.action_measure_room = QAction("&Measure Room…", self)
        self.action_measure_room.triggered.connect(self._open_measure_dialog)
        tools_menu.addAction(self.action_measure_room)
        self.action_bass_management = QAction("&Bass Management…", self)
        self.action_bass_management.triggered.connect(self._open_bass_management_dialog)
        tools_menu.addAction(self.action_bass_management)
        self.action_vst_management = QAction("&VST Management…", self)
        self.action_vst_management.triggered.connect(self._open_vst_management)
        tools_menu.addAction(self.action_vst_management)
        tools_menu.addSeparator()
        self.action_settings = QAction("&Settings…", self)
        self.action_settings.setShortcut(QKeySequence.StandardKey.Preferences)
        self.action_settings.triggered.connect(self._open_settings)
        tools_menu.addAction(self.action_settings)
        tools_menu.addSeparator()
        self.action_about = QAction("&About RUMBLE", self)
        self.action_about.triggered.connect(self._open_about)
        tools_menu.addAction(self.action_about)

        # Global shortcuts for the two most frequent actions that live on the
        # editor header, not in a menu: Save (Ctrl+S) and Bypass toggle
        # (Ctrl+B). Added to the window so they fire from anywhere in the UI.
        self.action_save = QAction("Save Profile", self)
        self.action_save.setShortcut(QKeySequence.StandardKey.Save)
        self.action_save.triggered.connect(self._on_save_current)
        self.addAction(self.action_save)
        self.action_bypass = QAction("Toggle Bypass", self)
        self.action_bypass.setShortcut(QKeySequence("Ctrl+B"))
        self.action_bypass.triggered.connect(
            lambda: self._on_bypass_toggled(
                not bool(self.manager.settings.get("bypassed", False))))
        self.addAction(self.action_bypass)

        # Theme menu: lists every available theme (built-ins, imported/custom,
        # and live System) and switches instantly on click. Repopulated each
        # time it opens so newly added custom themes appear without a restart.
        self.theme_menu = menubar.addMenu("T&heme")
        self.theme_menu.aboutToShow.connect(self._populate_theme_menu)
        self._populate_theme_menu()

        # Status tips appear in the status bar while hovering a menu item.
        status_tips = {
            self.action_new_profile: "Create a new EQ profile",
            self.action_import: "Add one or more profiles from .txt files or .zip packs",
            self.action_import_single: "Add exactly one profile from a single .txt file or .zip bundle",
            self.action_autoeq: "Search and import EQ profiles for your headphones and speakers from the online Measurement Database",
            self.action_measure_room: "Measure your speakers and room, then generate a correction profile",
            self.action_bass_management: "Configure crossover, LFE routing, trims and distance alignment for a sub/LFE layout",
            self.action_vst_management: "View every VST plugin referenced across your profiles, check for missing paths, and relink or add one",
            self.action_export: "Save the selected profile as a portable text file",
            self.action_backup: "Save all profiles and settings into a zip archive",
            self.action_restore: "Replace the current library with a backup archive",
            self.action_import_theme: "Add a colour theme from a .rumble-theme file",
            self.action_export_theme: "Save the current colour theme as a shareable file",
            self.action_quit: "Exit RUMBLE completely (EQ processing keeps running in Equalizer APO)",
            self.action_rename: "Rename the selected profile",
            self.action_duplicate: "Create an editable copy of the selected profile",
            self.action_delete: "Permanently delete the selected profile",
            self.action_lock: "Toggle the lock that protects the selected profile from changes",
            self.action_settings: "Open RUMBLE settings: appearance, EQ defaults and APO integration",
            self.action_about: "About RUMBLE, plus AI-profile and import guides",
        }
        for action, tip in status_tips.items():
            action.setStatusTip(tip)
            action.setToolTip(wrap_tooltip(tip))

        # Merge the menu bar with the frameless-window chrome: menu items,
        # a stretchy drag-to-move filler, then minimize/close. Replaces the
        # native OS titlebar entirely -- see the FramelessWindowHint flag
        # set in __init__.
        row = QWidget(self)
        row.setProperty("class", "chrome-strip")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 4, 0)
        row_layout.setSpacing(4)
        row_layout.addWidget(menubar)
        row_layout.addWidget(make_drag_filler(self, row), 1)
        row_layout.addWidget(make_minimize_button(self, self.theme))
        row_layout.addWidget(make_close_button(self, self.theme))
        # QMenuBar's own sizeHint() (its actual required height for the
        # current font/style) is taller than its QSS min-height alone; when
        # the row's own layout-computed height comes out shorter than that,
        # Qt silently collapses all menu items into a single ">>" overflow
        # button. Pin the row to the menu bar's real sizeHint height so it
        # renders identically to the native QMenuBar it replaces.
        row.setFixedHeight(menubar.sizeHint().height())
        self.setMenuWidget(row)

    def _build_central_widget(self) -> None:
        self.profile_panel = ProfilePanel(self.theme, self.manager)
        self.editor_panel = EditorPanel(self.theme)
        self.editor_panel.graph.set_y_range(
            int(self.manager.settings.get("graph_y_range", 30) or 30))
        self.editor_panel.graph.configure_spectrum_settings(
            enabled=bool(self.manager.settings.get("spectrum_enabled", False)),
            mode=self.manager.settings.get("spectrum_mode", "output"),
            peak_hold=bool(self.manager.settings.get("spectrum_peak_hold", False)),
            compare_front=self.manager.settings.get("spectrum_compare_front", "output"),
        )
        self.editor_panel.graph.spectrum_settings_changed.connect(
            self._on_spectrum_settings_changed)
        self.editor_panel.configure_meter_settings(
            mode=self.manager.settings.get("meter_mode", "vu"),
            k_reference=self.manager.settings.get("meter_k_reference", "k14"),
            active=bool(self.manager.settings.get("spectrum_enabled", False)),
        )
        # Reuses the same generic settings.update(...)+save() slot as the
        # spectrum settings above -- no signal-specific logic to duplicate.
        self.editor_panel.meter_settings_changed.connect(self._on_spectrum_settings_changed)
        self.editor_panel.set_sample_rate(self.manager.effective_sample_rate())
        self.editor_panel.set_ir_resolver(self.manager.resolve_ir_path)
        self.editor_panel.set_listener_correction_provider(self.manager.get_active_correction)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.profile_panel)
        splitter.addWidget(self.editor_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setHandleWidth(1)
        # The panel is fixed-width, but the splitter sizes its first section to
        # the panel's (larger) sizeHint, leaving a dead gap before the editor.
        # Pin the section to the panel's real width so the two sit flush.
        splitter.setSizes([panel_width(), 10_000])

        # A1 coverage banner sits above the splitter, hidden by default; it
        # only claims vertical space (out of the fixed window height) while
        # actually shown -- see _check_apo_coverage().
        self.apo_banner = ApoCoverageBanner(self.theme)
        self.apo_banner.hide()
        self.apo_banner.configurator_requested.connect(self._on_apo_configurator_requested)
        self.apo_banner.sound_settings_requested.connect(self._on_sound_settings_requested)
        self.apo_banner.manage_devices_requested.connect(self._on_manage_devices_requested)
        self.apo_banner.dismissed.connect(self._on_apo_coverage_dismissed)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.apo_banner)
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self.profile_panel.new_profile_requested.connect(self._on_new_profile)
        self.profile_panel.import_requested.connect(self._on_import)
        self.profile_panel.export_requested.connect(self._on_export)
        self.profile_panel.profile_selected.connect(self._on_profile_selected)
        self.profile_panel.profile_activated.connect(self._on_activate_profile)
        self.profile_panel.profile_rename_requested.connect(self._on_rename_profile)
        self.profile_panel.profile_duplicate_requested.connect(self._on_duplicate_profile)
        self.profile_panel.profile_delete_requested.connect(self._on_delete_profile)
        self.profile_panel.profile_lock_toggle_requested.connect(self._on_toggle_lock_profile)
        self.profile_panel.profiles_duplicate_requested.connect(self._on_duplicate_profiles)
        self.profile_panel.profiles_delete_requested.connect(self._on_delete_profiles)
        self.profile_panel.profiles_lock_requested.connect(self._on_lock_profiles)
        self.profile_panel.auto_switch_toggled.connect(self._on_auto_switch_toggled)
        self.profile_panel.abx_requested.connect(self._open_abx_dialog)
        self.profile_panel.listener_slot_changed.connect(self._on_listener_slot_changed)

        self.editor_panel.save_requested.connect(self._on_save_current)
        self.editor_panel.bypass_requested.connect(self._on_bypass_toggled)
        self.editor_panel.clear_requested.connect(self._on_clear_current)
        self.editor_panel.ab_compare_toggled.connect(self._on_ab_toggled)
        self.editor_panel.dirty.connect(self._on_editor_dirty)
        self.editor_panel.preview_only.connect(self._on_editor_preview_only)
        self.editor_panel.crossfeed_mode_changed.connect(self._on_crossfeed_mode_changed)
        self.editor_panel.set_crossfeed_mode(
            str(self.manager.settings.get("crossfeed_mode", "profile")))
        # Undo/redo are keyboard-only (Ctrl+Z / Ctrl+Y); the Edit menu is their
        # only visible surface, so it follows the editor's history.
        self.editor_panel.history_changed.connect(self._on_editor_history_changed)
        # B1: the Tone Trim popover trigger lives on the editor's controls row,
        # but the trim itself is session state on the manager (and the tray menu
        # drives the same values), so MainWindow owns the behaviour.
        self.editor_panel.btn_tone_trim.clicked.connect(self._toggle_tone_trim_popover)

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.setFixedHeight(dp(24, 20))
        self.setStatusBar(bar)

        self.status_left = QLabel("")
        self.status_left.setToolTip(wrap_tooltip(
            "Which profile is currently active, and whether "
            "there are unsaved changes or an engaged bypass."))
        bar.addWidget(self.status_left, 1)

        # B1: the popover for the bass/treble/tilt session trim. Its trigger
        # button lives on the editor's controls row alongside the other
        # session controls (Pre-Amp, Auto) — see _build_central_widget, which
        # wires editor_panel.btn_tone_trim to _toggle_tone_trim_popover.
        self._tone_trim_widget = ToneTrimWidget(self.theme, self)
        self._tone_trim_widget.trim_changed.connect(self._on_tone_trim_changed)
        self._tone_trim_widget.bake_requested.connect(self._on_bake_tone_trim)
        self._tone_trim_popover = QWidget(self)
        self._tone_trim_popover.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._tone_trim_popover.setStyleSheet(
            f"background-color: {self.theme['bg_surface']}; "
            f"border: 1px solid {self.theme['border_subtle']}; border-radius: 4px;")
        popover_layout = QVBoxLayout(self._tone_trim_popover)
        popover_layout.setContentsMargins(0, 0, 0, 0)
        popover_layout.addWidget(self._tone_trim_widget)

        # Trim chip -- same visible-only-while-non-zero rule
        # _write_active_pointer() uses internally (any(session_trim.values())),
        # mirrors the "Auto" badge just below.
        self.status_tone_trim = QLabel("")
        self.status_tone_trim.setProperty("class", "chip-accent")
        self.status_tone_trim.setToolTip(wrap_tooltip(
            "A session tone trim (bass/treble/tilt) is currently active."))
        self.status_tone_trim.hide()
        bar.addPermanentWidget(self.status_tone_trim)

        # "Auto" badge — shown only while a rule currently holds the active
        # profile (not merely while the feature is enabled).
        self.status_auto = QLabel("")
        self.status_auto.setProperty("class", "chip-accent")
        self.status_auto.setToolTip(wrap_tooltip(
            "A rule is currently holding this profile active."))
        self.status_auto.hide()
        bar.addPermanentWidget(self.status_auto)

        # Auto-Switch, Blind A/B/X and Ear Calibration moved out of the status
        # bar: quick access now lives in the profile panel (Auto-Switch,
        # Blind A/B/X). In Settings each sits with the thing it acts on --
        # Auto-Switch on its own page, Blind A/B/X under Library, Ear
        # Calibration under Listeners. See profile_panel.py and
        # settings_dialog.py's NAV_GROUPS.

    def _build_tray_icon(self) -> None:
        self.tray_icon = TrayIcon(self._app_icon, self.theme, self)
        self.tray_icon.open_requested.connect(self._show_and_raise)
        self.tray_icon.profile_activate_requested.connect(self._on_activate_profile)
        self.tray_icon.bypass_toggled.connect(self._on_bypass_toggled)
        self.tray_icon.auto_switch_toggled.connect(self._on_auto_switch_toggled)
        self.tray_icon.auto_switch_rules_requested.connect(self._open_auto_switch_dialog)
        self.tray_icon.abx_requested.connect(self._open_abx_dialog)
        self.tray_icon.hearing_test_requested.connect(self._open_hearing_dialog)
        self.tray_icon.listener_slot_selected.connect(self._on_listener_slot_changed)
        self.tray_icon.tone_trim_changed.connect(self._on_tone_trim_changed)
        self.tray_icon.tone_trim_bake_requested.connect(self._on_bake_tone_trim)
        self.tray_icon.crossfeed_toggled.connect(self._on_crossfeed_toggled)
        self.tray_icon.settings_requested.connect(self._open_settings)
        self.tray_icon.quit_requested.connect(self._quit_application)
        self.tray_icon.set_profiles(self.manager.list_profiles(), self._active_profile_id())
        self.tray_icon.set_bypassed(self.manager.is_bypassed())
        self.tray_icon.set_crossfeed_state(
            str(self.manager.settings.get("crossfeed_mode", "profile")) != "off")
        self.tray_icon.show()
        self._refresh_tone_trim_ui()

    # ── profile list / selection ─────────────────────────────────────

    def _active_profile_id(self) -> str | None:
        active = self.manager.get_active_profile()
        return active.id if active else None

    def _load_profile_list(self) -> None:
        profiles = self.manager.list_profiles()
        # Capture-only (microphone) profiles are hidden from the library and
        # tray when the user opts out. NOT display-only: hiding also disables
        # microphone EQ outright -- ProfileManager._valid_device_assignments()
        # drops capture branches from the dispatch, and the settings dialog
        # drops its Capture tab, so nothing reaches a mic while this is off.
        # Nothing is destroyed either way: the files stay on disk and the
        # assignments stay in settings, so turning it back on restores both.
        # Mirrors the built-in-profile toggle, minus the disk removal (those
        # are app-managed; these are the user's own work). Measuring with a
        # calibration mic (core/mic_cal.py, core/audio/mic_capture.py) is a
        # separate path that never consults this setting and keeps working.
        if not self.manager.settings.get("show_microphone_profiles", False):
            profiles = [p for p in profiles if not p.capture_only]
        self.profile_panel.set_profiles(profiles)
        self.tray_icon.set_profiles(profiles, self._active_profile_id())
        self._refresh_listener_ui()

    def _load_selected_or_active(self) -> None:
        profiles = self.manager.list_profiles()
        if not profiles:
            return
        last_profile_id = self.manager.settings.get("last_active_profile")
        target = None
        if last_profile_id:
            target = self.manager.get_profile(last_profile_id)
        if target is None:
            target = self.manager.get_active_profile() or profiles[0]
        self.profile_panel.select_profile(target.id)
        self._open_profile(target.id)

    def _open_profile(self, profile_id: str) -> None:
        meta = self.manager.get_profile(profile_id)
        config = self.manager.load_profile_config(profile_id)
        if meta is None or config is None:
            return
        active = self.manager.get_active_profile()
        is_active = active is not None and active.id == profile_id
        self._preview_timer.stop()
        # Opening a profile shows its saved state with no divergence, so any
        # live preview of now-discarded edits must be torn down — otherwise APO
        # keeps playing the scratch file while the editor shows another profile.
        self.manager.end_preview()
        self.editor_panel.load_profile(meta, config, is_active=is_active, is_bypassed=self.manager.is_bypassed(), is_readonly=meta.protected)
        self.editor_panel.refresh_listener_overlay()
        self._loaded_profile_id = profile_id
        self._dirty = False
        self._preview_diverged = False
        self._release_ab()
        self._refresh_ab_enabled()
        self._update_status_bar()

    def _on_profile_selected(self, profile_id: str) -> None:
        """Clicking a profile in the sidebar opens it in the editor for editing."""
        if profile_id == self._loaded_profile_id:
            return
        if self._dirty and self.manager.settings.get("warn_unsaved_changes", True) \
                and not self._confirm_discard_changes():
            self.profile_panel.select_profile(self._loaded_profile_id)
            return
        self._open_profile(profile_id)

    def _confirm_discard_changes(self) -> bool:
        result = QMessageBox.question(
            self, "Unsaved Changes",
            "This profile has unsaved changes. Discard them and switch profiles?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return result == QMessageBox.StandardButton.Discard

    def _on_editor_dirty(self) -> None:
        self._dirty = True
        self._preview_diverged = True
        self._schedule_live_preview()
        self._refresh_ab_enabled()
        self._update_status_bar()

    def _on_editor_preview_only(self) -> None:
        """A per-session control (Pre-Amp / Auto toggle) changed the live sound
        but is not a tracked edit: refresh the preview without setting _dirty,
        so no unsaved-changes warning is raised. Save still persists the value
        because it reads the live editor state."""
        self._preview_diverged = True
        self._schedule_live_preview()
        self._refresh_ab_enabled()
        self._update_status_bar()

    # ── A/B compare (saved vs edited) ────────────────────────────────

    def _on_ab_toggled(self, checked: bool) -> None:
        """A = the saved profile file itself (pointer restored, byte-exact);
        B = the live preview of the unsaved edits. While comparing, edits
        still mark the profile dirty but stay parked until release."""
        if checked:
            self._ab_comparing = True
            self._preview_timer.stop()
            self.manager.end_preview()
        else:
            self._ab_comparing = False
            if self._preview_diverged:
                self._schedule_live_preview()
        self._update_status_bar()

    def _release_ab(self) -> None:
        self._ab_comparing = False
        self.editor_panel.release_ab()

    def _refresh_ab_enabled(self) -> None:
        """A/B is offered only while the loaded profile is the live one, has
        diverged from its saved state, and the EQ is not bypassed."""
        active = self.manager.get_active_profile()
        enabled = (active is not None
                   and active.id == self._loaded_profile_id
                   and (self._dirty or self._preview_diverged)
                   and not self.manager.is_bypassed())
        if not enabled:
            self._ab_comparing = False
        self.editor_panel.set_ab_enabled(enabled)

    # ── live tuning (real-time preview) ──────────────────────────────

    def _schedule_live_preview(self) -> None:
        # While A/B compare holds the saved profile, edits stay parked — they
        # become audible again when the hold is released.
        if self._ab_comparing:
            return
        # Live preview only ever applies to the active (live) profile — the one
        # actually playing through Equalizer APO. Single-clicking a profile just
        # shows its values as a static "pseudo" view; nothing it does reaches
        # APO until the user double-clicks to make it the live profile.
        active = self.manager.get_active_profile()
        if (not self.manager.is_bypassed()
                and self._loaded_profile_id is not None
                and active is not None
                and active.id == self._loaded_profile_id):
            self._preview_timer.start()

    def _push_live_preview(self) -> None:
        # for_preview applies audition-only state (a soloed band) that must
        # be audible but never saved.
        config = self.editor_panel.build_config(for_preview=True)
        self.manager.preview_config(config, self.editor_panel.crossfeed_spec())
        self._update_status_bar()

    # ── profile actions ──────────────────────────────────────────────

    def _on_new_profile(self) -> None:
        dialog = NewProfileDialog(
            self.theme,
            default_bands=int(self.manager.settings.get("default_bands", 10)),
            parent=self,
            manager=self.manager,
        )
        dialog.profile_created.connect(self._load_profile_list)
        accepted = dialog.exec() == NewProfileDialog.DialogCode.Accepted
        if dialog.imported_ids:
            self._finish_measurement_import(dialog.imported_ids)
            return
        if not accepted:
            return
        opts = dialog.values()
        if not opts["name"]:
            return
        config = make_flat_config(opts["bands"], channels=opts["channels"],
                                  device=opts["device"])
        meta = self.manager.create_profile(
            opts["name"], description=opts["description"], tags=opts["tags"], config=config,
        )
        self._load_profile_list()
        self.profile_panel.select_profile(meta.id)
        self._open_profile(meta.id)

    def _current_profile_id(self) -> str | None:
        return self.profile_panel.selected_profile_id()

    def _on_save_current(self) -> None:
        # Editor actions target the profile open in the editor, which can
        # briefly differ from the sidebar selection.
        profile_id = self._loaded_profile_id
        if profile_id is None:
            return
        meta = self.manager.get_profile(profile_id)
        if meta and meta.builtin:
            QMessageBox.information(
                self, "Save Profile",
                f"'{meta.name}' is a built-in profile and is system protected.\n"
                "Duplicate it to make an editable copy.")
            return
        if meta and meta.readonly:
            QMessageBox.information(
                self, "Save Profile",
                f"'{meta.name}' is locked and cannot be overwritten.\n"
                "Right-click it in the profile list to unlock.")
            return
        name, description = self.editor_panel.current_meta_fields()
        if not name:
            QMessageBox.warning(self, "Save Profile", "Profile name cannot be empty.")
            return
        config = self.editor_panel.build_config()

        # Rumble 5.1: this profile is calculated to clip on playback and Auto
        # preamp is off, so nothing will self-correct it — get an explicit
        # confirmation instead of saving silently behind the passive red label.
        if (self.editor_panel.current_headroom_db() < 0
                and not self.editor_panel.is_auto_preamp_enabled()):
            clip_result = QMessageBox.question(
                self, "Save Profile",
                "This profile's calculated headroom is negative and Auto "
                "preamp is off, so it may clip on playback.\nSave anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if clip_result != QMessageBox.StandardButton.Yes:
                return

        # Rumble 5.4: warn, don't block (see _config_has_lfe_only_bass_filter).
        if _config_has_lfe_only_bass_filter(config):
            QMessageBox.warning(
                self, "Save Profile",
                "One or more filters at or below 120 Hz are scoped to the "
                "LFE channel only. Bass redirection typically happens after "
                "Equalizer APO's own processing, so LFE-only low-frequency "
                "filters often have no audible effect — consider applying "
                "them to all channels instead.")

        result = QMessageBox.question(
            self, "Save Profile",
            f"Save the current changes over '{name}'?\n"
            "The previously saved version will be replaced.",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if result != QMessageBox.StandardButton.Save:
            return
        self._preview_timer.stop()
        self.manager.rename_profile(profile_id, name)
        self.manager.update_profile_meta(profile_id, description=description)
        # Per-profile crossfeed (edited in the editor) rides along on the same
        # save: set it on the meta first (write=False), then save_profile_config
        # writes the file -- header (incl. Crossfeed) + body -- and re-renders.
        self.manager.set_profile_crossfeed(profile_id, self.editor_panel.crossfeed_spec(), write=False)
        self.manager.save_profile_config(profile_id, config)
        self._dirty = False
        self._preview_diverged = False
        self._release_ab()
        self._refresh_ab_enabled()
        self._load_profile_list()
        self.profile_panel.select_profile(profile_id)
        self._update_status_bar()
        self.statusBar().showMessage("Profile saved", 3000)

    def _on_activate_profile(self, profile_id: str) -> None:
        if profile_id != self._loaded_profile_id:
            if self._dirty and self.manager.settings.get("warn_unsaved_changes", True) \
                    and not self._confirm_discard_changes():
                return
            self._open_profile(profile_id)
        self.manager.activate_profile(profile_id)
        self._load_profile_list()
        meta = self.manager.get_profile(profile_id)
        self.editor_panel.set_active_state(
            is_active=meta is not None and profile_id == self._loaded_profile_id,
            is_bypassed=self.manager.is_bypassed())
        self.tray_icon.set_bypassed(self.manager.is_bypassed())
        self._refresh_ab_enabled()
        self._update_status_bar()
        self.statusBar().showMessage(f"Activated '{meta.name}'" if meta else "Profile activated", 3000)

    def _on_bypass_toggled(self, bypassed: bool) -> None:
        if bypassed:
            self._preview_timer.stop()
        self.manager.set_bypass(bypassed)
        self.tray_icon.set_bypassed(bypassed)
        active = self.manager.get_active_profile()
        is_active = active is not None and active.id == self._loaded_profile_id
        self.editor_panel.set_active_state(is_active=is_active, is_bypassed=bypassed)
        # Resume the live preview if the editor diverges from the saved profile —
        # whether from a tracked edit or a per-session Pre-Amp/Auto toggle.
        if not bypassed and self._preview_diverged:
            self._schedule_live_preview()
        self._refresh_ab_enabled()
        self._update_status_bar()

    # ── live spectrum analyzer (Phase 6) ─────────────────────────────

    def _on_spectrum_settings_changed(self, settings: dict) -> None:
        """Slot for EQGraph.spectrum_settings_changed — persists Live/mode/
        peak-hold immediately, the same way `_on_auto_switch_toggled` and
        the theme switcher persist their own live toggles."""
        self.manager.settings.update(settings)
        self.manager.save()

    # ── automatic profile switching (Phase 3) ────────────────────────

    def _on_auto_switch_toggled(self, enabled: bool) -> None:
        self.manager.settings["auto_switch_enabled"] = enabled
        self.manager.save()
        self._auto_switch_adapter.set_enabled(enabled)
        if enabled:
            self._auto_switch_adapter.start()
        else:
            self._auto_switch_adapter.stop()
        self._refresh_auto_switch_ui()

    def _on_crossfeed_toggled(self, enabled: bool) -> None:
        # The tray quick toggle is a two-state view of the three-state
        # crossfeed_mode: On restores the last non-off mode the user had
        # (profile or global), Off parks it. The main-UI switch offers the
        # full three-way choice; both go through _apply_crossfeed_mode so
        # they stay in sync.
        self._apply_crossfeed_mode(self._last_crossfeed_mode if enabled else "off")

    def _on_crossfeed_mode_changed(self, mode: str) -> None:
        # The main-UI source switch picked a mode directly.
        self._apply_crossfeed_mode(mode)

    def _apply_crossfeed_mode(self, mode: str) -> None:
        # Single place the machine-level crossfeed source mode is changed:
        # persist immediately, re-render live, and keep every surface (the
        # editor switch, the tray two-state toggle) in sync -- same
        # "persist immediately, apply live" shape as _on_auto_switch_toggled.
        if mode != "off":
            self._last_crossfeed_mode = mode
        self.manager.settings["crossfeed_mode"] = mode
        self.manager.save()
        self.manager.refresh_render_layers()
        self.editor_panel.set_crossfeed_mode(mode)
        self.tray_icon.set_crossfeed_state(mode != "off")

    def _on_listener_slot_changed(self, slot_id: int) -> None:
        self.manager.set_active_listener_slot(slot_id)
        self._refresh_listener_ui()
        self.editor_panel.refresh_listener_overlay()

    def _refresh_listener_ui(self) -> None:
        names = listener_slots.load_slot_names(self.manager.data_dir)
        self.profile_panel.set_listener_slots(names, self.manager.active_listener_slot)
        self.tray_icon.set_listeners(names, self.manager.active_listener_slot)

    # ── B1 session tone trim (tray + controls-row popover) ───────────

    def _toggle_tone_trim_popover(self) -> None:
        if self._tone_trim_popover.isVisible():
            self._tone_trim_popover.hide()
            return
        self._tone_trim_widget.set_trim(**self.manager.session_trim)
        btn = self.editor_panel.btn_tone_trim
        self._tone_trim_popover.adjustSize()
        # Drop below the button and hang from its right edge: the button sits
        # at the right end of the controls row, so a left-aligned popover would
        # run off the window.
        anchor = btn.mapToGlobal(btn.rect().bottomRight())
        self._tone_trim_popover.move(anchor.x() - self._tone_trim_popover.width(),
                                     anchor.y() + 2)
        self._tone_trim_popover.show()
        self._tone_trim_popover.raise_()

    def _on_editor_history_changed(self, can_undo: bool, can_redo: bool) -> None:
        self.action_undo.setEnabled(can_undo)
        self.action_redo.setEnabled(can_redo)

    def _on_tone_trim_changed(self, bass_db: float, treble_db: float, tilt_db_per_oct: float) -> None:
        self.manager.set_session_trim(
            bass_db=bass_db, treble_db=treble_db, tilt_db_per_oct=tilt_db_per_oct)
        self._refresh_tone_trim_ui()

    def _refresh_tone_trim_ui(self) -> None:
        trim = self.manager.session_trim
        self._tone_trim_widget.set_trim(**trim)
        self.tray_icon.set_tone_trim(**trim)
        # Same expression _write_active_pointer() uses internally to decide
        # whether the trim layer is composed at all -- see S4 handoff.
        active = any(trim.values())
        self.status_tone_trim.setText("Trim" if active else "")
        self.status_tone_trim.setVisible(active)

    def _on_bake_tone_trim(self) -> None:
        # "Bake into profile...": writes the current session trim into the
        # ACTIVE profile's editor state as real GraphicEQ content through the
        # normal dirty/save flow (the user still saves explicitly), then
        # resets the trim. Refuses on a dirty editor, same tone as the ABX
        # opener's refusal (_open_abx_dialog) -- an editor full of unrelated
        # unsaved edits must not silently get the trim mixed into it.
        self._tone_trim_popover.hide()
        if self._dirty:
            QMessageBox.information(
                self, "Bake Into Profile",
                "Save or discard the unsaved changes before baking the "
                "tone trim into a profile.")
            return
        curve = tone_trim.derive_trim_curve(**self.manager.session_trim)
        if not curve:
            return
        active = self.manager.get_active_profile()
        if active is None:
            QMessageBox.information(
                self, "Bake Into Profile", "No active profile to bake the trim into.")
            return
        config = self.manager.load_profile_config(active.id)
        if config is None:
            return
        baked = self.manager.bake_session_trim(config)
        self.editor_panel.load_profile(
            active, baked, is_active=True, is_bypassed=self.manager.is_bypassed(),
            is_readonly=active.protected)
        self.editor_panel.refresh_listener_overlay()
        self._loaded_profile_id = active.id
        self._dirty = True
        self._preview_diverged = True
        self._schedule_live_preview()
        self.manager.set_session_trim(bass_db=0.0, treble_db=0.0, tilt_db_per_oct=0.0)
        self._refresh_tone_trim_ui()
        self.profile_panel.select_profile(active.id)
        self._update_status_bar()
        self.statusBar().showMessage(
            f"Tone trim baked into '{active.name}' — click Save to apply", 4000)

    def _open_auto_switch_dialog(self) -> None:
        # Pre-fill the app-match combo with whatever the watcher has most
        # recently observed playing, so the user can pick from real apps
        # instead of typing an exe name from memory.
        playing = self._auto_switch_adapter.playing
        dlg = AutoSwitchDialog(self.manager, self.theme, playing_exes=playing, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._auto_switch_adapter.reload_rules()

    def _open_abx_dialog(self) -> None:
        # ABXDialog takes over the live Equalizer APO config for the whole
        # test and, on exit, restores whatever is the *saved* active profile
        # (ABXRunner.finish() -> manager.end_preview()) — that would clobber
        # unsaved editor edits, or a per-session preview divergence (Pre-Amp/
        # Auto toggle) that isn't a tracked edit, neither of which it knows
        # how to restore. Refuse until both are clear, same tone as the
        # lock-profile refusal above.
        #
        # Note: guard on `_preview_diverged`, not `manager.is_previewing` —
        # the latter only flips True once the debounced 250ms preview timer
        # actually calls manager.preview_config(), so a Pre-Amp/Auto toggle
        # (which sets _preview_diverged synchronously but leaves _dirty
        # False) could sneak the dialog open inside that window, and the
        # timer would then fire mid-test and clobber the ABX trial's audio.
        if self._dirty or self._preview_diverged:
            QMessageBox.information(
                self, "Blind A/B/X Test",
                "Save or discard the unsaved changes (and let any live "
                "preview divergence settle) before running a blind "
                "listening test.")
            return
        if self.manager.is_bypassed():
            # preview_config() silently no-ops while bypassed (by design —
            # it must never override an intentional "EQ off"), so every A/B/X
            # button would play the same unprocessed passthrough regardless
            # of which side was requested. Refuse rather than let the user
            # run a whole test that can never show a difference.
            QMessageBox.information(
                self, "Blind A/B/X Test",
                "EQ is currently bypassed, so switching between the "
                "candidate profiles and X wouldn't actually change what you "
                "hear. Turn bypass off before running a blind listening test.")
            return
        dlg = ABXDialog(self.manager, self.theme, parent=self)
        dlg.exec()

    def _open_hearing_dialog(self) -> None:
        # Unlike ABXDialog, HearingSuiteDialog only bypasses EQ processing
        # (BypassGuard) rather than switching the live config to another
        # profile, and restores that bypass state on every exit path itself
        # -- it has no opinion on unsaved editor edits or live-preview
        # divergence, so (unlike _open_abx_dialog) there is nothing here to
        # refuse on.
        dlg = HearingSuiteDialog(self.manager, self.theme, parent=self)
        dlg.exec()

    def _on_play_reference_sound(self, key: str) -> None:
        """Settings -> Ear Calibration's "check your volume" sample
        buttons: play a reference sound with EQ bypassed for the duration
        -- otherwise the sample would be coloured by whatever profile is
        active and wouldn't represent the real-world sound it's named
        after. `key` is "" for the full bracketed voice clip
        (load_volume_reference) or a hearing_test.REFERENCE_SOUNDS key.
        Non-blocking: playback is polled off a QTimer so the Settings
        dialog stays responsive, same pattern hearing_dialog.py uses for
        its own tone sequencing."""
        if self._reference_sound_engine is not None:
            self._reference_sound_engine.stop()
        if self._reference_sound_bypass_guard is None:
            self._reference_sound_bypass_guard = hearing_test.BypassGuard(self.manager)
            self._reference_sound_bypass_guard.__enter__()
        if self._reference_sound_engine is None:
            self._reference_sound_engine = ToneEngine()
        buffer, sample_rate = (
            hearing_test.load_volume_reference() if not key
            else hearing_test.load_reference_sound(key))
        self._reference_sound_engine.play(buffer, sample_rate)
        self._reference_sound_poll_timer.start()

    def _on_reference_sound_poll(self) -> None:
        if self._reference_sound_engine is not None and self._reference_sound_engine.is_playing:
            return
        self._reference_sound_poll_timer.stop()
        self._release_reference_sound_bypass()

    def _release_reference_sound_bypass(self) -> None:
        if self._reference_sound_bypass_guard is not None:
            guard = self._reference_sound_bypass_guard
            self._reference_sound_bypass_guard = None
            guard.__exit__(None, None, None)

    def _open_measure_dialog(self) -> None:
        # Same shape as _open_hearing_dialog: MeasureDialog owns its own
        # bypass/engine lifecycle end-to-end (BypassGuard engaged on the
        # intro page's Next, released on every exit path), so there is
        # nothing here to refuse on.
        dlg = MeasureDialog(self.manager, self.theme, parent=self)
        dlg.exec()

    def _refresh_bass_management_action(self) -> None:
        # Tools -> Bass Management… is only meaningful when the active
        # profile's layout actually has a sub/LFE channel to manage --
        # reuses the same channel-preset detection the editor's own preset
        # combo (editor_panel.py) uses, factored into a free function this
        # session so it works against an arbitrary loaded config without a
        # live EditorPanel instance.
        enabled = False
        active = self.manager.get_active_profile()
        if active is not None:
            config = self.manager.load_profile_config(active.id)
            if config is not None:
                enabled = bass_management.SUB_CHANNEL in layout_channel_tokens(
                    config.channel_sections)
        self.action_bass_management.setEnabled(enabled)

    def _open_bass_management_dialog(self) -> None:
        # Unlike _open_measure_dialog/_open_hearing_dialog, this wizard's
        # end state (Apply) writes directly into the editor -- refuse
        # up front on a dirty editor rather than let the user walk the
        # whole wizard only to have the final Apply refused, same tone as
        # _on_bake_tone_trim's own refusal message.
        if self._dirty:
            QMessageBox.information(
                self, "Bass Management",
                "Save or discard the unsaved changes before opening the "
                "bass management wizard -- it applies directly into the "
                "editor.")
            return
        dialog = BassManagementDialog(self.manager, self.theme, parent=self)
        dialog.apply_requested.connect(self._on_apply_bass_management)
        dialog.exec()

    def _on_apply_bass_management(self, profile_id: str, config: ProfileConfig) -> None:
        # Mirrors _on_bake_tone_trim's shape exactly:
        # load the generated content into the editor as a dirty, unsaved
        # change through the normal save flow, rather than writing a new
        # profile file directly.
        if self._dirty:
            QMessageBox.information(
                self, "Bass Management",
                "Save or discard the unsaved changes before applying bass "
                "management into a profile.")
            return
        meta = self.manager.get_profile(profile_id)
        if meta is None:
            return
        active = self.manager.get_active_profile()
        self.editor_panel.load_profile(
            meta, config, is_active=active is not None and active.id == profile_id,
            is_bypassed=self.manager.is_bypassed(), is_readonly=meta.protected)
        self.editor_panel.refresh_listener_overlay()
        self._loaded_profile_id = profile_id
        self._dirty = True
        self._preview_diverged = True
        self._schedule_live_preview()
        self.profile_panel.select_profile(profile_id)
        self._update_status_bar()
        self.statusBar().showMessage(
            f"Bass management applied to '{meta.name}' — click Save to apply", 4000)

    def _on_auto_switched(self, profile_id: str, reason: str) -> None:
        # Deliberately does NOT touch the editor's loaded profile/content —
        # unlike manual activation (_on_activate_profile), which opens
        # whatever was clicked. Auto-switch only changes which profile is
        # ACTIVE (the APO pointer); forcing the editor to reload here would
        # either discard a non-dirty live-preview divergence for no reason
        # (if the switched-to profile happens to already be loaded) or yank
        # away whatever the user is calmly looking at — exactly what the
        # "suspended while dirty" rule exists to prevent, so this must not
        # reintroduce the same disruption through a back door.
        meta = self.manager.get_profile(profile_id)
        name = meta.name if meta is not None else profile_id
        self.tray_icon.announce_switch(name, reason)
        verb = "Auto-switched" if reason == "rule" else "Auto-switch restored"
        self.statusBar().showMessage(f"{verb} to '{name}'", 4000)
        self._load_profile_list()
        self.editor_panel.set_active_state(
            is_active=meta is not None and profile_id == self._loaded_profile_id,
            is_bypassed=self.manager.is_bypassed())
        self._refresh_ab_enabled()
        self._update_status_bar()

    def _refresh_auto_switch_ui(self) -> None:
        state = self._auto_switch_adapter.state()
        enabled = state != "off"
        suspended = state == "suspended-dirty"
        self.profile_panel.set_auto_switch_checked(enabled)
        self.tray_icon.set_auto_switch_state(enabled, suspended)
        holding = enabled and not suspended and self._auto_switch_adapter.is_holding()
        self.status_auto.setText("Auto" if holding else "")
        self.status_auto.setVisible(holding)

    def _on_clear_current(self) -> None:
        profile_id = self._loaded_profile_id
        if profile_id is None:
            return
        meta = self.manager.get_profile(profile_id)
        if meta and meta.builtin:
            QMessageBox.information(
                self, "Clear Profile",
                f"'{meta.name}' is a built-in profile and is system protected.\n"
                "Duplicate it to make an editable copy.")
            return
        if meta and meta.readonly:
            QMessageBox.information(
                self, "Clear Profile",
                f"'{meta.name}' is locked and cannot be modified.\n"
                "Right-click it in the profile list to unlock.")
            return
        name = meta.name if meta else "this profile"
        reply = QMessageBox.question(
            self, "Clear Profile",
            f"Clear all bands and settings from \"{name}\"?\n\nThis cannot be undone until you save.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        active = self.manager.get_active_profile()
        is_active = active is not None and active.id == profile_id
        blank = ProfileConfig()
        self.editor_panel.load_profile(
            meta, blank,
            is_active=is_active,
            is_bypassed=self.manager.is_bypassed(),
            is_readonly=meta.protected if meta else False,
        )
        self.editor_panel.refresh_listener_overlay()
        self._dirty = True
        self._preview_diverged = True
        self._schedule_live_preview()
        self._update_status_bar()
        self.statusBar().showMessage("Profile cleared — click Save to apply", 4000)

    def _on_rename_current(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is not None:
            self._on_rename_profile(profile_id, "")

    def _on_rename_profile(self, profile_id: str, _new_name: str) -> None:
        meta = self.manager.get_profile(profile_id)
        if meta is None:
            return
        if meta.builtin:
            QMessageBox.information(
                self, "Rename Profile",
                f"'{meta.name}' is a built-in profile and is system protected.\n"
                "Duplicate it to make an editable copy.")
            return
        if meta.readonly:
            QMessageBox.information(
                self, "Rename Profile",
                f"'{meta.name}' is locked and cannot be renamed.\n"
                "Right-click it in the profile list to unlock.")
            return
        name, ok = QInputDialog.getText(self, "Rename Profile", "Profile name:", text=meta.name)
        name = name.strip()
        if ok and name:
            self.manager.rename_profile(profile_id, name)
            self._load_profile_list()
            self._open_profile(profile_id)

    def _on_duplicate_current(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is not None:
            self._on_duplicate_profile(profile_id)

    def _on_duplicate_profile(self, profile_id: str) -> None:
        meta = self.manager.get_profile(profile_id)
        if meta is None:
            return
        name, ok = QInputDialog.getText(self, "Duplicate Profile", "New profile name:", text=f"{meta.name} Copy")
        name = name.strip()
        if not ok or not name:
            return
        new_meta = self.manager.duplicate_profile(profile_id, name)
        if new_meta is not None:
            self._load_profile_list()
            self.profile_panel.select_profile(new_meta.id)
            self._open_profile(new_meta.id)

    def _on_lock_current(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is not None:
            self._on_toggle_lock_profile(profile_id)

    def _on_toggle_lock_profile(self, profile_id: str) -> None:
        meta = self.manager.get_profile(profile_id)
        if meta is None:
            return
        if meta.builtin:
            QMessageBox.information(
                self, "Lock Profile",
                f"'{meta.name}' is a built-in profile and is always system "
                "protected — it cannot be locked or unlocked.")
            return
        locking = not meta.readonly
        if locking and self._dirty and profile_id == self._loaded_profile_id:
            QMessageBox.information(
                self, "Lock Profile",
                "Save or discard the unsaved changes before locking this profile.")
            return
        was_current = profile_id == self._loaded_profile_id
        self.manager.set_profile_locked(profile_id, locking)
        self._load_profile_list()
        if was_current:
            self.profile_panel.select_profile(profile_id)
            self._open_profile(profile_id)
        self.statusBar().showMessage(
            f"'{meta.name}' {'locked — protected from changes' if locking else 'unlocked'}", 3000)

    def _on_delete_current(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is not None:
            self._on_delete_profile(profile_id)

    def _on_delete_profile(self, profile_id: str) -> None:
        meta = self.manager.get_profile(profile_id)
        if meta is None:
            return
        if meta.builtin:
            QMessageBox.information(
                self, "Delete Profile",
                f"'{meta.name}' is a built-in profile and is system protected.\n"
                "It cannot be deleted.")
            return
        if meta.readonly:
            QMessageBox.information(
                self, "Delete Profile",
                f"'{meta.name}' is locked and cannot be deleted.\n"
                "Right-click it in the profile list to unlock.")
            return
        result = QMessageBox.question(
            self, "Delete Profile",
            f"Delete profile '{meta.name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self.manager.delete_profile(profile_id)
        self._load_profile_list()
        self._load_selected_or_active()

    # ── batch actions (multi-selection) ──────────────────────────────

    def _copy_name(self, base: str) -> str:
        """A non-colliding '<base> Copy' name for batch duplication."""
        existing = {p.name for p in self.manager.list_profiles()}
        name = f"{base} Copy"
        i = 2
        while name in existing:
            name = f"{base} Copy ({i})"
            i += 1
        existing.add(name)  # reserve within this batch
        return name

    def _on_duplicate_profiles(self, ids: list[str]) -> None:
        # Windows-style copy: auto-name each, no per-item prompt.
        last = None
        for pid in ids:
            meta = self.manager.get_profile(pid)
            if meta is None:
                continue
            new_meta = self.manager.duplicate_profile(pid, self._copy_name(meta.name))
            if new_meta is not None:
                last = new_meta
        if last is None:
            return
        self._load_profile_list()
        self.profile_panel.select_profile(last.id)
        self._open_profile(last.id)
        self.statusBar().showMessage(f"Duplicated {len(ids)} profiles", 3000)

    def _on_delete_profiles(self, ids: list[str]) -> None:
        metas = [m for m in (self.manager.get_profile(i) for i in ids) if m is not None]
        deletable = [m for m in metas if not m.protected]
        protected = [m for m in metas if m.protected]
        if not deletable:
            QMessageBox.information(
                self, "Delete Profiles",
                "The selected profiles are all built-in or locked and cannot be deleted.")
            return
        preview = "\n".join(f"  • {m.name}" for m in deletable[:8])
        if len(deletable) > 8:
            preview += f"\n  …and {len(deletable) - 8} more"
        note = ""
        if protected:
            note = (f"\n\n{len(protected)} built-in/locked profile(s) in the "
                    "selection will be skipped.")
        result = QMessageBox.question(
            self, "Delete Profiles",
            f"Delete {len(deletable)} profiles? This cannot be undone.\n\n{preview}{note}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        for m in deletable:
            self.manager.delete_profile(m.id)
        self._load_profile_list()
        self._load_selected_or_active()
        self.statusBar().showMessage(f"Deleted {len(deletable)} profiles", 3000)

    def _on_lock_profiles(self, ids: list[str], lock: bool) -> None:
        changed = 0
        for pid in ids:
            meta = self.manager.get_profile(pid)
            if meta is None or meta.builtin or meta.readonly == lock:
                continue
            # Can't lock a profile whose unsaved edits are open in the editor.
            if lock and self._dirty and pid == self._loaded_profile_id:
                continue
            if self.manager.set_profile_locked(pid, lock):
                changed += 1
        if not changed:
            return
        self._load_profile_list()
        if self._loaded_profile_id is not None:
            self.profile_panel.select_profile(self._loaded_profile_id)
        verb = "Locked" if lock else "Unlocked"
        self.statusBar().showMessage(f"{verb} {changed} profiles", 3000)

    # ── import / export / backup ─────────────────────────────────────

    def _on_import(self) -> None:
        path_strs, _ = QFileDialog.getOpenFileNames(
            self, "Import Profiles", "",
            "RUMBLE Profile / Bundle (*.txt *.zip);;All Files (*)")
        if not path_strs:
            return
        result = self.manager.import_profiles([Path(p) for p in path_strs])
        if result.imported:
            self._load_profile_list()
            last = result.imported[-1]
            self.profile_panel.select_profile(last.id)
            self._open_profile(last.id)

        if result.imported and not result.failures and not result.notes:
            n = result.count
            msg = f"Imported {n} profile{'s' if n != 1 else ''}"
            mismatches = self._count_ir_mismatches(result.imported)
            if mismatches:
                msg += (f" — {mismatches} has a sample-rate mismatch" if mismatches == 1
                        else f" — {mismatches} have a sample-rate mismatch")
            self.statusBar().showMessage(msg, 4000)
        else:
            self._show_import_summary(result)

    def _count_ir_mismatches(self, imported: list) -> int:
        """How many just-imported profiles carry a convolution IR whose own
        sample rate mismatches the device — a cheap nice-to-have surfaced in
        the import status message; the authoritative, always-visible warning
        is the ConvolutionEditor label shown when a profile is opened (see
        EditorPanel._refresh_ir_result / ir_analysis.check_rate_mismatch)."""
        device_rate = self.manager.effective_sample_rate()
        count = 0
        for meta in imported:
            ir_path = self.manager.resolve_convolution_ir(meta.id)
            if ir_path is None:
                continue
            ir_result = ir_analysis.get_ir_response(ir_path)
            if ir_result is None:
                continue
            if ir_analysis.check_rate_mismatch(ir_result.ir_sample_rate, device_rate):
                count += 1
        return count

    def _on_import_single(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import Single Profile", "",
            "RUMBLE Profile / Bundle (*.txt *.zip);;All Files (*)")
        if not path_str:
            return
        meta, notes = self.manager.import_profile(Path(path_str))
        if meta is None:
            QMessageBox.warning(self, "Import Profile",
                                "No importable profile was found in that file.")
            return
        self._load_profile_list()
        self.profile_panel.select_profile(meta.id)
        self._open_profile(meta.id)
        self.statusBar().showMessage(f"Imported \"{meta.name}\"", 4000)
        if notes:
            msg = QMessageBox(self)
            msg.setWindowTitle("Import Profile")
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setText(f"Imported \"{meta.name}\", but with a note:")
            msg.setDetailedText("\n".join(notes))
            msg.exec()

    def _on_import_peace(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import from Peace", "",
            "Peace configuration (*.peace);;All Files (*)")
        if not path_str:
            return
        meta, skipped = self.manager.import_peace_file(Path(path_str))
        if meta is None:
            QMessageBox.warning(self, "Import from Peace",
                                "No importable EQ was found in that Peace configuration.")
            return
        self._load_profile_list()
        self.profile_panel.select_profile(meta.id)
        self._open_profile(meta.id)
        if skipped:
            msg = QMessageBox(self)
            msg.setWindowTitle("Import from Peace")
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setText(f"Imported \"{meta.name}\", but {len(skipped)} band"
                        f"{'s' if len(skipped) != 1 else ''} could not be translated:")
            msg.setDetailedText("\n".join(skipped))
            msg.exec()
        else:
            self.statusBar().showMessage(f"Imported \"{meta.name}\" from Peace", 4000)

    def _on_import_rew(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import from REW", "",
            "REW export (*.txt);;All Files (*)")
        if not path_str:
            return
        path = Path(path_str)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            QMessageBox.warning(self, "Import from REW", "Could not read that file.")
            return

        if rew_import.looks_like_filter_settings(text):
            meta, skipped = self.manager.import_rew_filter_settings(path)
            if meta is None:
                QMessageBox.warning(self, "Import from REW",
                                    "No importable filter was found in that REW export.")
                return
            self._load_profile_list()
            self.profile_panel.select_profile(meta.id)
            self._open_profile(meta.id)
            if skipped:
                msg = QMessageBox(self)
                msg.setWindowTitle("Import from REW")
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.setText(f"Imported \"{meta.name}\", but {len(skipped)} filter"
                            f"{'s' if len(skipped) != 1 else ''} could not be translated "
                            "(REW's BW/60 bandwidth convention has no RUMBLE equivalent):")
                msg.setDetailedText("\n".join(skipped))
                msg.exec()
            else:
                self.statusBar().showMessage(f"Imported \"{meta.name}\" from REW", 4000)
            return

        dialog = RewMeasurementImportDialog(self.manager, path.name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        meta, clamp, warnings = self.manager.import_rew_measurement(
            path, blend_curve=dialog.blend_curve())
        if meta is None:
            detail = "\n".join(warnings) if warnings else None
            box = QMessageBox(self)
            box.setWindowTitle("Import from REW")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText("No usable measurement data was found in that file.")
            if detail:
                box.setDetailedText(detail)
            box.exec()
            return
        self._load_profile_list()
        self.profile_panel.select_profile(meta.id)
        self._open_profile(meta.id)
        if warnings or clamp.notable():
            msg = QMessageBox(self)
            msg.setWindowTitle("Import from REW")
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setText(f"Imported \"{meta.name}\" from REW.")
            details = list(warnings)
            if clamp.notable():
                n = clamp.points_clamped_boost + clamp.points_clamped_cut
                details.append(f"{n} point(s) were limited by the boost/cut cap "
                               f"(up to {clamp.max_clamped_delta_db:.1f} dB).")
            msg.setDetailedText("\n".join(details))
            msg.exec()
        else:
            self.statusBar().showMessage(f"Imported \"{meta.name}\" from REW", 4000)

    def _show_import_summary(self, result) -> None:
        """Report a partial or failed import — counts plus the skip reasons
        and any non-fatal notes (e.g. a stripped VST plugin reference)."""
        if not result.imported and not result.failures and not result.notes:
            return
        n = result.count
        lines = []
        if n:
            lines.append(f"Imported {n} profile{'s' if n != 1 else ''}.")
        if result.failures:
            lines.append(f"Skipped {len(result.failures)}:")
            lines.extend(f"  • {reason}" for reason in result.failures)
        if result.notes:
            lines.append("Notes:")
            lines.extend(f"  • {note}" for note in result.notes)
        box = QMessageBox(self)
        box.setWindowTitle("Import Profiles")
        box.setIcon(QMessageBox.Icon.Information if n else QMessageBox.Icon.Warning)
        box.setText("\n".join(lines))
        box.exec()

    def _on_export(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is None:
            return
        meta = self.manager.get_profile(profile_id)
        if meta is None:
            return

        bundle = False
        ir_path = self.manager.resolve_convolution_ir(profile_id)
        if ir_path is not None:
            box = QMessageBox(self)
            box.setWindowTitle("Export Profile")
            box.setText(f"This profile uses an impulse response file:  {ir_path.name}")
            box.setInformativeText(
                "Bundle it so the profile sounds identical on another machine, "
                "or export the profile text only? (Without the file, the "
                "convolution step is skipped.)")
            btn_bundle = box.addButton("Bundle IR (.zip)", QMessageBox.ButtonRole.ActionRole)
            btn_plain = box.addButton("Profile only (.txt)", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(btn_bundle)
            box.exec()
            if box.clickedButton() is btn_bundle:
                bundle = True
            elif box.clickedButton() is not btn_plain:
                return

        if bundle:
            path_str, _ = QFileDialog.getSaveFileName(self, "Export Profile", f"{meta.name}.zip",
                                                      "RUMBLE Profile Bundle (*.zip)")
            if not path_str:
                return
            ok = self.manager.export_profile_bundle(profile_id, Path(path_str))
        else:
            path_str, _ = QFileDialog.getSaveFileName(self, "Export Profile", f"{meta.name}.txt",
                                                      "RUMBLE Profile / APO Config (*.txt)")
            if not path_str:
                return
            ok = self.manager.export_profile(profile_id, Path(path_str))
        if not ok:
            QMessageBox.warning(self, "Export Profile", "Could not export the profile.")

    def _on_calibrate_loudness(self, dialog) -> None:
        if self.manager.calibrate_loudness_reference():
            dialog.note_loudness_reference_calibrated(
                self.manager.settings["iso226_reference_volume_db"])
            self.statusBar().showMessage(
                "Loudness reference calibrated to current volume", 4000)
        else:
            self.statusBar().showMessage(
                "Could not read the current system volume", 4000)

    def _on_backup(self) -> None:
        default_name = f"rumble_backup_{datetime.now():%Y-%m-%d}.zip"
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Back Up Everything", default_name, "Zip Archive (*.zip)")
        if not path_str:
            return
        if self.manager.backup_library(Path(path_str)):
            self.statusBar().showMessage("Backup created — profiles, settings, themes and IR files saved", 4000)
        else:
            QMessageBox.warning(self, "Back Up Everything", "Could not create the backup archive.")

    def _on_restore(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(self, "Restore From Backup", "", "Zip Archive (*.zip)")
        if not path_str:
            return
        result = QMessageBox.question(
            self, "Restore From Backup",
            "This replaces everything — all profiles, settings, custom themes and "
            "impulse-response files — with the contents of the backup. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        if self.manager.restore_library(Path(path_str)):
            self._load_profile_list()
            self._load_selected_or_active()
            self.statusBar().showMessage("Backup restored", 3000)
        else:
            QMessageBox.warning(self, "Restore From Backup", "Could not restore from the selected archive.")

    def _on_restore_drift_snapshot(self) -> None:
        snapshots = self.manager.list_drift_snapshots()
        if not snapshots:
            QMessageBox.information(
                self, "Restore Drift Snapshot",
                "No preserved config.txt snapshots were found.")
            return
        from rumble.ui.drift_restore_dialog import DriftRestoreDialog
        dialog = DriftRestoreDialog(snapshots, self.theme, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        snap = dialog.selected_snapshot()
        if snap is None:
            return
        result = QMessageBox.question(
            self, "Restore Drift Snapshot",
            f"Replace the current config.txt with the snapshot from "
            f"{snap.label}? RUMBLE will stop managing Equalizer APO until "
            "you choose to resume from Settings.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        if self.manager.restore_drift_snapshot(snap.path):
            self.statusBar().showMessage(f"Restored config.txt from {snap.label}", 4000)
        else:
            QMessageBox.warning(self, "Restore Drift Snapshot",
                                "Could not restore the selected snapshot.")

    def _on_open_profile_trash(self) -> None:
        entries = self.manager.list_trashed()
        if not entries:
            QMessageBox.information(
                self, "Deleted Profiles",
                "The recycle bin is empty.")
            return
        from rumble.ui.profile_trash_dialog import ProfileTrashDialog
        dialog = ProfileTrashDialog(entries, self.theme, self)
        dialog.restore_requested.connect(lambda e: self._on_restore_trashed_profile(dialog, e))
        dialog.purge_requested.connect(lambda e: self._on_purge_trashed_profile(dialog, e))
        dialog.exec()
        self._load_profile_list()

    def _on_restore_trashed_profile(self, dialog, entry) -> None:
        meta = self.manager.restore_trashed(entry)
        if meta is None:
            QMessageBox.warning(self, "Deleted Profiles",
                                "Could not restore the selected profile.")
            return
        self.statusBar().showMessage(f"Restored '{meta.name}'", 3000)
        dialog.set_entries(self.manager.list_trashed())

    def _on_purge_trashed_profile(self, dialog, entry) -> None:
        result = QMessageBox.question(
            self, "Purge Deleted Profile",
            f"Permanently remove '{entry.name}' from the recycle bin? "
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self.manager.purge_trashed(entry)
        dialog.set_entries(self.manager.list_trashed())

    # ── Equalizer APO config management ──────────────────────────────

    def _on_import_apo(self) -> None:
        """Scan the APO config folder and offer to import existing configs as
        profiles (triggered from Settings → APO Integration)."""
        candidates = self.manager.importable_candidates()
        if not candidates:
            QMessageBox.information(
                self, "Import APO configs",
                "No importable Equalizer APO configuration was found in the "
                "config folder.")
            return
        from rumble.ui.apo_import_dialog import ApoImportDialog
        dialog = ApoImportDialog(candidates, self.theme, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.selected()
        if not chosen:
            return
        created = self.manager.import_apo_candidates(chosen)
        self._load_profile_list()
        self.statusBar().showMessage(
            f"Imported {len(created)} profile(s) from Equalizer APO", 3000)

    def _on_toggle_apo_management(self) -> None:
        """Hand the APO config back (restore the original config.txt) or resume
        managing it — the Settings button toggles by current state."""
        if self.manager.is_apo_managed():
            result = QMessageBox.question(
                self, "Hand back to APO",
                "Restore your original config.txt and stop RUMBLE managing "
                "Equalizer APO?\n\nYour profiles stay saved. RUMBLE won't "
                "re-take over until you choose to resume.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if result != QMessageBox.StandardButton.Yes:
                return
            if self.manager.release_control():
                self.statusBar().showMessage("Equalizer APO config handed back", 4000)
            else:
                QMessageBox.warning(self, "Hand back to APO",
                                    "Could not restore the original config.")
        else:
            self.manager.resume_control()
            self._load_profile_list()
            self.statusBar().showMessage("RUMBLE is managing Equalizer APO again", 4000)

    # ── theme import / export ────────────────────────────────────────

    def _on_import_theme(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import Theme", "",
            "RUMBLE Theme (*.rumble-theme);;JSON Files (*.json);;All Files (*)"
        )
        if not path_str:
            return
        theme_dict, errors = import_theme_file(Path(path_str))
        if errors:
            QMessageBox.warning(self, "Import Theme",
                                "Could not import theme:\n\n" + "\n".join(errors))
            return
        save_custom_theme(self.manager.data_dir, theme_dict)
        self._switch_theme(theme_dict["name"])
        self.statusBar().showMessage(f"Theme '{theme_dict['name']}' imported", 3000)

    def _on_export_theme(self) -> None:
        current = self.manager.settings.get("theme", DEFAULT_THEME_NAME)
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export Current Theme", f"{current}.rumble-theme",
            "RUMBLE Theme (*.rumble-theme);;JSON Files (*.json)"
        )
        if not path_str:
            return
        theme_dict = get_theme(current)
        export_theme(theme_dict, Path(path_str))
        self.statusBar().showMessage(f"Theme exported to {Path(path_str).name}", 3000)

    def _on_custom_theme_saved(self, name: str) -> None:
        self._switch_theme(name)
        self.statusBar().showMessage(f"Theme '{name}' created", 3000)

    # ── theme / settings ─────────────────────────────────────────────

    def _populate_theme_menu(self) -> None:
        """Rebuild the Theme menu from the live THEMES registry plus System.

        Custom and imported themes are merged into THEMES at load/import time,
        so listing THEMES keys here keeps the menu in step with what the user
        has added. The active theme is marked with a checkable radio item."""
        self.theme_menu.clear()
        current = self.manager.settings.get("theme", DEFAULT_THEME_NAME)

        group = QActionGroup(self.theme_menu)
        group.setExclusive(True)

        names = ordered_theme_names()
        for name in names:
            action = QAction(name, self.theme_menu)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.setStatusTip(f"Switch the application to the '{name}' colour theme")
            action.triggered.connect(lambda _checked=False, n=name: self._switch_theme(n))
            group.addAction(action)
            self.theme_menu.addAction(action)

    def _switch_theme(self, theme_name: str) -> None:
        tokens = get_theme(theme_name)
        self.theme = tokens
        self.theme_engine.switch(QApplication.instance(), tokens)
        self.manager.settings["theme"] = theme_name
        self.manager.save()
        self.profile_panel.set_theme(tokens)
        self.editor_panel.set_theme(tokens)
        # The glass glow/elevation effects are recoloured by ThemeEngine.switch
        # → apply(), which already walks every top-level window (this one
        # included) calling effects.refresh_tree. No second walk is needed here.

    def _open_settings(self, restore: dict | None = None, initial_category: str | None = None) -> None:
        # Triggered by QAction (which passes a bool) as well as directly with
        # a carried dialog state after an instant interface-size rebuild.
        if not isinstance(restore, dict):
            restore = None
        active_meta = self.manager.get_active_profile()
        active_is_stereo = True
        if active_meta is not None:
            active_config = self.manager.load_profile_config(active_meta.id)
            if active_config is not None:
                active_is_stereo = crossfeed.can_crossfeed(active_config.channel_sections)
        apo_devices = audio_devices.list_apo_devices()
        apo_render_devices = [d for d in apo_devices if d.flow == "Render"]
        apo_capture_devices = [d for d in apo_devices if d.flow == "Capture"]
        device_profile_options = [(m.id, m.name) for m in self.manager.list_profiles()]
        dialog = SettingsDialog(self.manager.settings, self.theme, self.manager.data_dir, self,
                                restore=restore, active_profile_is_stereo=active_is_stereo,
                                apo_render_devices=apo_render_devices,
                                apo_capture_devices=apo_capture_devices,
                                device_profile_options=device_profile_options,
                                initial_category=initial_category)
        dialog.theme_preview_requested.connect(self._switch_theme)
        dialog.custom_theme_saved.connect(self._on_custom_theme_saved)
        dialog.backup_requested.connect(self._on_backup)
        dialog.restore_requested.connect(self._on_restore)
        dialog.drift_restore_requested.connect(self._on_restore_drift_snapshot)
        dialog.profile_trash_requested.connect(self._on_open_profile_trash)
        dialog.calibrate_loudness_requested.connect(lambda: self._on_calibrate_loudness(dialog))
        dialog.import_apo_requested.connect(self._on_import_apo)
        dialog.release_apo_requested.connect(self._on_toggle_apo_management)
        dialog.auto_switch_toggled.connect(self._on_auto_switch_toggled)
        dialog.auto_switch_rules_requested.connect(self._open_auto_switch_dialog)
        dialog.abx_requested.connect(self._open_abx_dialog)
        dialog.hearing_test_requested.connect(self._open_hearing_dialog)
        dialog.play_reference_sound_requested.connect(self._on_play_reference_sound)
        # The dialog has no Save button: it emits this as each control is
        # touched, so settings apply and persist as they are changed.
        dialog.settings_saved.connect(self._apply_settings_live)
        result = dialog.exec()
        if result == SettingsDialog.APPLY_SIZE_NOW:
            # Instant size change: the size key is the one setting the live
            # apply above cannot handle (Qt reads the scale factor only while
            # a QApplication is being constructed), so commit it, rebuild the
            # UI now, and have main() reopen the dialog exactly as it was.
            new_settings = dialog.result_settings()
            self.manager.settings["ui_double_size"] = bool(new_settings.get("ui_double_size"))
            self.manager.save()
            QApplication.instance().rumble_reopen_settings = dialog.carry_state()
            self._apply_ui_size_change()
        elif result == SettingsDialog.RESET_NOW:
            self._reset_settings(dialog.reset_groups())

    def _reset_settings(self, group_keys: list[str]) -> None:
        """Restore the chosen groups of settings to their defaults, apply the
        result live, and reopen Settings so the user sees the new values.

        The reset is computed against a COPY of the live settings and handed to
        _apply_settings_live() unmutated, so that method can still diff old
        against new and do only the work the changed keys need -- mutating
        manager.settings first would make every previous_* read equal to the
        new value and silently skip the theme switch and profile-list reload.
        """
        new_settings = dict(self.manager.settings)
        changed = settings_reset.reset_settings(new_settings, group_keys)
        if not changed:
            self.statusBar().showMessage(
                "Those settings were already at their defaults", 4000)
            self._reopen_settings_fresh()
            return

        # ui_double_size can only take effect by rebuilding the UI (Qt reads the
        # scale factor once, at QApplication construction) -- the same
        # constraint the size buttons live with. Commit it and rebuild; main()
        # reopens Settings for us afterwards.
        if "ui_double_size" in changed:
            self.manager.settings.update(new_settings)
            self.manager.save()
            self._apply_settings_live(new_settings)
            QApplication.instance().rumble_reopen_settings = self._fresh_settings_state()
            self._apply_ui_size_change()
            return

        self._apply_settings_live(new_settings)
        # _apply_settings_live drops the manager-owned keys, since the dialog's
        # snapshot of them is stale by design (see its comment). A reset is an
        # explicit, deliberate change to them, so write the ones it touched.
        for key in ("ignored_apo_sources",):
            if key in changed:
                self.manager.settings[key] = new_settings[key]
                self.manager.save()
        # Starting/stopping the watcher is the auto_switch_toggled signal's job,
        # not _apply_settings_live's, so a reset of the flag must do it here or
        # the watcher keeps running against a setting that says it is off.
        if "auto_switch_enabled" in changed:
            self._on_auto_switch_toggled(
                bool(self.manager.settings.get("auto_switch_enabled", False)))

        self.statusBar().showMessage(
            f"Settings reset — {len(changed)} "
            f"{'value' if len(changed) == 1 else 'values'} back to default", 5000)
        self._reopen_settings_fresh()

    def _fresh_settings_state(self) -> dict:
        """Dialog state that reopens Settings on Backup & Restore with its
        controls read fresh from the live settings. Deliberately carries no
        "working" dict: after a reset the old one is stale, and the dialog
        falls back to the manager's settings when it is absent."""
        return {"category": SETTINGS_CATEGORIES.index("Backup & Restore")}

    def _reopen_settings_fresh(self) -> None:
        # Reopen from the event loop rather than inline: this runs inside the
        # closing dialog's exec() return path, and opening the next modal
        # dialog there nests a second event loop inside the one unwinding.
        QTimer.singleShot(0, lambda: self._open_settings(restore=self._fresh_settings_state()))

    def _apply_settings_live(self, new_settings: dict) -> None:
        """Apply and persist a settings change the instant it is made.

        Called on every change from the Settings dialog, which hands over its
        whole working dict each time rather than a single key, so each
        previous_* below is read from manager.settings *before* the update to
        work out what actually changed -- only the changed setting does its
        (potentially expensive) work."""
        new_settings = dict(new_settings)
        # apo_managed / ignored_apo_sources are owned by the manager and can
        # be mutated live while the dialog is open (Hand back to APO, Import
        # APO). The dialog snapshots settings at open and never edits these
        # keys, so drop them here — otherwise its stale snapshot would clobber
        # the live values (e.g. silently re-take APO control right after the
        # user handed it back).
        for _owned in ("apo_managed", "ignored_apo_sources"):
            new_settings.pop(_owned, None)
        previous_theme = self.manager.settings.get("theme")
        previous_show_builtin = bool(self.manager.settings.get("show_builtin_profiles", True))
        previous_show_microphone = bool(
            self.manager.settings.get("show_microphone_profiles", False))
        previous_override = self.manager.settings.get("config_path_override")
        previous_graph_double_height = bool(self.manager.settings.get("graph_double_height"))
        self.manager.settings.update(new_settings)
        # If the user edited the APO config-path override, honour it now: a
        # bad value at startup parks the session on a fallback path (see
        # __main__), and that fallback must not outlive the user correcting
        # the very setting that caused it.
        if new_settings.get("config_path_override") != previous_override:
            self.manager._ignore_config_override = False
        self.manager.save()
        self._apply_autostart_setting()
        self.editor_panel.graph.set_y_range(
            int(new_settings.get("graph_y_range", 30) or 30))
        new_graph_double_height = bool(new_settings.get("graph_double_height"))
        if new_graph_double_height != previous_graph_double_height:
            # A plain resize, unlike ui_double_size -- no Qt scale factor
            # involved, so it takes effect immediately without rebuilding
            # the UI.
            self.setFixedSize(*window_fixed_size(new_graph_double_height))
        self.editor_panel.set_sample_rate(self.manager.effective_sample_rate())
        if bool(new_settings.get("show_builtin_profiles", True)) != previous_show_builtin:
            self.manager.ensure_builtin_profiles()
            self._load_profile_list()
        elif bool(new_settings.get("show_microphone_profiles", False)) != previous_show_microphone:
            # No disk reconcile (files stay put) — just re-filter the view.
            self._load_profile_list()
        if new_settings.get("theme") != previous_theme:
            self._switch_theme(new_settings.get("theme"))
        self._refresh_listener_ui()
        # A Reset can bring crossfeed_mode back to its default; re-sync both
        # crossfeed surfaces from the now-updated setting (the dialog's own
        # crossfeed card only edits the global value, not the mode).
        cf_mode = str(self.manager.settings.get("crossfeed_mode", "profile"))
        if cf_mode != "off":
            self._last_crossfeed_mode = cf_mode
        self.editor_panel.set_crossfeed_mode(cf_mode)
        self.tray_icon.set_crossfeed_state(cf_mode != "off")
        # Render-layer settings (the listener-correction toggle, the ISO
        # 226 loudness toggle / reference phon, crossfeed) must take effect
        # on the LIVE chain immediately, not at the next activation.
        #
        # refresh_render_layers() itself refuses while a live editor preview
        # is active (self.manager.is_previewing) or while bypassed, on
        # purpose -- it must never clobber the profile CONTENT a preview is
        # showing. But a render-layer toggle isn't profile content, it's a
        # session/machine overlay composed on TOP of whatever's playing, so
        # silently refusing here left a real, confusing bug: turn off ear
        # correction (or crossfeed, or ISO 226 loudness) while the Editor
        # has ANY unsaved live-tuning open, and the change would save to
        # disk correctly but never reach what you actually hear -- the
        # stale preview (with the OLD render-layer state still baked in)
        # would keep playing indefinitely, until the next actual edit
        # happened to re-push it. Re-pushing the live preview here (the
        # exact same call the edit-debounce timer makes) re-composes every
        # render layer fresh against the CURRENT unsaved editor state, so
        # the toggle takes effect immediately either way. Bypass is still
        # correctly left alone -- see refresh_render_layers()'s own guard.
        if self.manager.is_previewing:
            self._push_live_preview()
        else:
            self.manager.refresh_render_layers()

    def reopen_settings(self, state: dict) -> None:
        """Reopen the settings dialog from state carried across an in-process
        UI rebuild (instant interface-size change)."""
        self._open_settings(restore=state)

    def _open_autoeq(self) -> None:
        dialog = MeasurementBrowserDialog(self.manager, self.theme, self)
        dialog.workspace.profile_created.connect(self._load_profile_list)
        dialog.exec()
        self._finish_measurement_import(dialog.imported_ids)

    def _finish_measurement_import(self, imported_ids: list[str]) -> None:
        """Shared by the standalone Measurement Database browser (Tools menu)
        and New Profile's embedded "Measurement Database" tab: refresh the
        list, select/open the most recently created profile, and report how
        many were created."""
        if not imported_ids:
            return
        self._load_profile_list()
        last = imported_ids[-1]
        self.profile_panel.select_profile(last)
        self._open_profile(last)
        n = len(imported_ids)
        self.statusBar().showMessage(
            f"Imported {n} profile{'s' if n != 1 else ''} from the Measurement Database", 4000)

    def _open_about(self) -> None:
        dialog = AboutDialog(self.theme, self)
        dialog.exec()

    def _open_config_health(self) -> None:
        from rumble.ui.config_health_dialog import ConfigHealthDialog
        report = self.manager.effective_config_report()
        dialog = ConfigHealthDialog(report, self.theme, self)
        dialog.exec()

    def _open_coexistence_dashboard(self) -> None:
        from rumble.ui.coexistence_dialog import CoexistenceDialog
        report = self.manager.coexistence_report()
        dialog = CoexistenceDialog(report, self.theme, self)
        dialog.restore_drift_requested.connect(self._on_restore_drift_snapshot)
        dialog.exec()

    def _open_vst_management(self) -> None:
        from rumble.ui.vst_management_dialog import VSTManagementDialog
        dialog = VSTManagementDialog(self.manager, self.theme, self)
        dialog.profiles_changed.connect(self._on_vst_profiles_changed)
        dialog.exec()

    def _on_vst_profiles_changed(self, profile_ids: list[str]) -> None:
        """A relink/add/remove can rewrite the file behind the profile
        currently open in the editor. If it wasn't touched, do nothing; if
        it was and the editor has no unsaved changes, reload it so the UI
        matches; if it was touched AND the editor is dirty, don't silently
        discard the in-progress edit -- warn instead (an eventual Save will
        still overwrite the change on disk, same as any other external-edit
        race the app already tolerates elsewhere)."""
        if self._loaded_profile_id not in profile_ids:
            return
        if self._dirty:
            self.statusBar().showMessage(
                "Note: the currently open profile's VST plugins were changed on "
                "disk, but you have unsaved changes here — saving will overwrite that change.",
                6000)
            return
        self.profile_panel.select_profile(self._loaded_profile_id)
        self._open_profile(self._loaded_profile_id)

    def _open_crossover_design(self) -> None:
        from rumble.ui.crossover_dialog import CrossoverDialog
        dialog = CrossoverDialog(self.manager, self.theme, self)
        dialog.profile_created.connect(self._load_profile_list)
        dialog.exec()

    def _apply_ui_size_change(self) -> None:
        """Apply an interface-size change live. Qt only reads the scale factor
        while a QApplication is being constructed, so main() tears this window
        and the application object down and immediately rebuilds them at the
        new scale — same process, profile manager and any unsaved edit carried
        over, so to the user it is one seamless resize."""
        self._preview_timer.stop()
        app = QApplication.instance()
        if self._dirty and self._loaded_profile_id is not None:
            app.rumble_pending_edit = (self._loaded_profile_id,
                                      self.editor_panel.build_config())
        elif self.manager.is_previewing:
            # A non-dirty preview (Pre-Amp/Auto toggle) is not carried across the
            # rebuild, so restore the saved pointer now — otherwise APO would be
            # left playing the orphaned scratch file after the resize.
            self.manager.end_preview()
        # This MainWindow instance is being discarded for good against the
        # SAME long-lived self.manager (main() rebuilds a new MainWindow
        # around it) — close(), not stop(), so the old controller detaches
        # from manager.on_profile_activated and its SessionWatcher thread
        # doesn't keep running as an orphan after the rebuild.
        self._auto_switch_adapter.close()
        # Same reasoning as the auto-switch adapter just above, for the
        # Phase 6 live spectrum analyzer: this EQGraph instance (and its
        # SpectrumController's LoopbackCapture daemon thread + WASAPI
        # stream) is being discarded for good. Without this, the old
        # capture thread survives into the new window's lifetime (daemon
        # threads are only reaped at real process exit) and the freshly-
        # built EQGraph opens a SECOND loopback stream against the same
        # device if spectrum_enabled is persisted True. The VU/Peak meter
        # widget owns no background thread of its own (just a `LevelMeter`,
        # pure numpy state fed by this same capture), so it needs no
        # equivalent teardown call here.
        self.editor_panel.graph.stop_spectrum()
        self._closing_to_quit = True
        self.tray_icon.hide()
        self.close()
        app.setProperty("rumble_rescale", True)
        app.quit()

    def restore_unsaved_edit(self, profile_id: str, config: ProfileConfig) -> None:
        """Reload an unsaved edit carried across an in-process UI rebuild."""
        meta = self.manager.get_profile(profile_id)
        if meta is None:
            return
        active = self.manager.get_active_profile()
        self.profile_panel.select_profile(profile_id)
        self.editor_panel.load_profile(
            meta, config,
            is_active=active is not None and active.id == profile_id,
            is_bypassed=self.manager.is_bypassed(),
            is_readonly=meta.protected)
        self.editor_panel.refresh_listener_overlay()
        self._loaded_profile_id = profile_id
        self._dirty = True
        self._preview_diverged = True
        self._schedule_live_preview()
        self._update_status_bar()

    def _apply_autostart_setting(self) -> None:
        from rumble.core import autostart
        try:
            autostart.set_autostart(bool(self.manager.settings.get("start_with_windows")))
        except Exception:
            pass

    # ── status bar ───────────────────────────────────────────────────

    def _update_status_bar(self) -> None:
        active = self.manager.get_active_profile()
        if self.manager.is_bypassed():
            left = "Bypassed — EQ disabled"
        elif active is not None:
            left = f"Active: {active.name}"
        else:
            left = "No active profile"
        if self._dirty:
            left += ("  •  Unsaved changes (you're hearing them)" if self.manager.is_previewing
                     else "  •  Unsaved changes")
        self.status_left.setText(left)
        self._auto_switch_adapter.set_editor_dirty(self._dirty)
        self._refresh_auto_switch_ui()
        self._refresh_bass_management_action()

    # ── live device format ───────────────────────────────────────────

    def _on_device_format_changed(self) -> None:
        """Slot for DeviceFormatAdapter.format_changed. Runs on the Qt main
        thread (the adapter guarantees the marshaling — see
        device_format_adapter.py). Re-queries current state and refreshes the
        graph/delay/headroom re-render, the status-bar indicator and the tray
        tooltip. Task 6's convolution-IR mismatch check rides the same call:
        EditorPanel.set_sample_rate() re-derives the mismatch label via
        _refresh_ir_result() whenever the rate actually changes, so no extra
        hook is needed here."""
        # Refresh the device-format cache FIRST (this is also the sole place
        # that warms ProfileManager's detected-sample-rate cache), THEN read
        # effective_sample_rate() so it's a cache hit — avoids a second live
        # COM query for the same notification.
        self._refresh_device_format()
        rate = self.manager.effective_sample_rate()
        # A PK band is a biquad and warps with the sample rate; the GraphicEQ
        # residual that completes a curve-fitted profile does not. Re-solve the
        # residual against the new rate so imported/measured profiles stay exact
        # instead of drifting (up to ~1 dB, 96k vs 44.1k) when Windows switches
        # the device format under us.
        self._refresh_fitted_profiles(rate)
        self.editor_panel.set_sample_rate(rate)
        self._check_apo_coverage(toast=True)

    def _refresh_fitted_profiles(self, rate: int) -> None:
        """Re-solve curve-fitted profiles for `rate`, and reopen the one the
        editor is showing if it was among them: its bands are unchanged but its
        residual layer is not, and a stale editor would write the old residual
        back on the next save. Never clobbers unsaved edits — a dirty editor
        keeps what the user is working on, and the file on disk is already
        correct for the new rate either way.
        """
        changed = self.manager.refresh_fits_for_sample_rate(rate)
        if changed and self._loaded_profile_id in changed and not self._dirty:
            self._open_profile(self._loaded_profile_id)

    def _refresh_device_format(self) -> None:
        """Re-query the current default-device format and keep
        ProfileManager's detected-sample-rate cache (used by
        effective_sample_rate() in "auto" mode) in sync with the real
        device. Not surfaced in the UI: the live WASAPI mix format's bit
        depth commonly differs from the device's configured bit depth (the
        shared audio engine mixes internally at float32 regardless of the
        Windows Sound Settings "Default Format" — see device_format.py),
        which reads as a wrong reading rather than a distinct engine-level
        number. Only the sample rate, which is unambiguous, is used."""
        from rumble.core import device_format as _df
        fmt = _df.get_default_device_format()
        self.manager.set_detected_sample_rate(fmt.sample_rate if fmt is not None else None)

    # ── A1: APO-coverage health check ──────────────────────────────────

    def _check_apo_coverage(self, toast: bool = False) -> None:
        """Show/hide the coverage banner for the CURRENT default playback
        device, reporting whichever of the two silent failures is in play:

          * the device isn't hooked by APO at all, or
          * it IS hooked, but Windows "Audio enhancements" is off for it — so
            Windows never runs the effects chain APO lives in, and RUMBLE's EQ
            is inert no matter what it writes (profiles, built-ins and even
            Bypass all appear to do nothing).

        Both are undeterminable-safe: `default_device_hooked()` /
        `enhancements_disabled()` returning None means "we couldn't read it",
        which hides the banner. Never nag on a guess.

        When *toast* is set (the DeviceFormatAdapter.format_changed re-check —
        the default device just changed) and the banner newly needs showing,
        also pop a tray toast, mirroring TrayIcon.announce_switch's shape.
        """
        hooked = audio_devices.default_device_hooked()
        guid = audio_devices.default_render_guid()

        reason = None
        if hooked is False:
            reason = REASON_NOT_HOOKED
        elif hooked and guid and audio_devices.enhancements_disabled(guid) is True:
            # Hooked, but Windows will skip the whole effects chain.
            reason = REASON_ENHANCEMENTS_OFF

        needs_warning = (reason is not None
                         and not self.manager.is_apo_coverage_dismissed(guid))
        if needs_warning:
            self.apo_banner.set_reason(reason)
        self.apo_banner.setVisible(needs_warning)
        if needs_warning and toast:
            self.tray_icon.announce_coverage_warning()

    def _on_sound_settings_requested(self) -> None:
        """"Open Sound Settings" on the enhancements warning: Windows' own
        Sound page, where the "Audio enhancements" switch lives. os.startfile
        handles the ms-settings: URI; if that fails (stripped/older shell) fall
        back to the classic playback-devices control panel."""
        try:
            os.startfile("ms-settings:sound")
        except OSError:
            try:
                os.startfile("mmsys.cpl")
            except OSError:
                QMessageBox.information(
                    self, "Open Sound settings",
                    "Could not open Windows Sound settings automatically.\n\n"
                    "Open Settings > System > Sound, pick your playback device, "
                    "and turn \"Audio enhancements\" back on.")

    def _on_apo_configurator_requested(self) -> None:
        """"Open APO Configurator" button: launch the device-hooking tool
        (apo_path.get_configurator_path()) and re-check immediately after.
        os.startfile() returns as soon as the process is launched, not when
        it closes, so this immediate re-check only catches an
        already-current hook; the format-change watcher re-checks live once
        the user actually adds the device and Windows reports the change."""
        path = apo_path.get_configurator_path()
        if path is None:
            QMessageBox.information(
                self, "APO Configurator not found",
                "Could not locate Equalizer APO's device-hooking tool. "
                "Make sure Equalizer APO is installed.")
            return
        os.startfile(str(path))
        self._check_apo_coverage()

    def _on_manage_devices_requested(self) -> None:
        """"Manage Devices…" on the coverage banner: open Settings straight
        to the Devices page (D1b)."""
        self._open_settings(initial_category="Devices")

    def _on_apo_coverage_dismissed(self) -> None:
        """Persist the dismissal for the current default endpoint (if its
        GUID could be determined) and hide the banner immediately."""
        guid = audio_devices.default_render_guid()
        if guid is not None:
            self.manager.dismiss_apo_coverage_warning(guid)
        self.apo_banner.hide()

    # ── safety warning ───────────────────────────────────────────────

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        # First time the window actually appears (also covers tray launches,
        # where it fires when the user opens RUMBLE from the tray).
        if (not self._safety_warning_shown
                and self.manager.settings.get("show_safety_warning", True)):
            self._safety_warning_shown = True
            QTimer.singleShot(0, self._show_safety_warning)

    def _show_safety_warning(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Protect Your Hearing & Gear")
        box.setText("RUMBLE changes your system audio in real time — every "
                    "adjustment is applied the moment you make it.")
        box.setInformativeText(
            "Extreme settings can produce sudden, very loud output:\n\n"
            "  •  Large gain or shelf boosts can cause permanent hearing damage, "
            "especially with headphones or IEMs.\n"
            "  •  Sustained boosts and clipping can damage speakers, amplifiers "
            "and headphones.\n"
            "  •  Disabling pre-amp without compensation will cause digital clipping "
            "and distortion that can permanently damage hearing and gear.\n\n"
            "Stay safe while tuning:\n\n"
            "  •  Turn your playback volume down before making big changes, "
            "then raise it gradually.\n"
            "  •  Use Auto pre-amp to compensate boosts and prevent clipping.\n"
            "  •  Keep pre-amp enabled unless you specifically understand the risks.\n"
            "  •  Use Bypass to mute the EQ while you set up extreme curves.\n\n"
            "Misuse of this app is at your own risk.")
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        checkbox = QCheckBox("Don't show this warning again")
        checkbox.setToolTip(wrap_tooltip(
            "Suppress this hearing/gear safety warning on future launches."))
        box.setCheckBox(checkbox)
        box.exec()
        if checkbox.isChecked():
            self.manager.settings["show_safety_warning"] = False
            self.manager.save()

    # ── window lifecycle / tray behaviour ────────────────────────────

    def _show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._closing_to_quit:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.tray_icon.showMessage("RUMBLE", "RUMBLE is still running in the background.")

    def _quit_application(self) -> None:
        # Unsaved live-tuning changes die with the session — put the saved
        # active profile back so APO is not left playing the scratch preview.
        self._preview_timer.stop()
        self._reference_sound_poll_timer.stop()
        if self._reference_sound_engine is not None:
            self._reference_sound_engine.stop()
        self._release_reference_sound_bypass()
        self._format_adapter.stop()
        self._loudness_volume_adapter.stop()
        self._auto_switch_adapter.close()
        # Hygiene alongside the two stops just above: let the loopback
        # capture thread + WASAPI stream close cleanly before process exit
        # rather than relying solely on the daemon thread being killed
        # under the process — mirrors the same call added to
        # _apply_ui_size_change() for the in-process rebuild path.
        self.editor_panel.graph.stop_spectrum()
        self.manager.end_preview()
        self._closing_to_quit = True
        self.tray_icon.hide()
        self.close()
        QApplication.instance().quit()
