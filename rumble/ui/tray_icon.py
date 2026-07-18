"""System tray icon with toast-style profile menu."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon, QCursor
from PySide6.QtWidgets import QSystemTrayIcon, QWidget, QApplication

_SWITCH_TOAST_MS = 4000

from rumble.core.profile_manager import ProfileMeta
from rumble.ui.a11y import wrap_tooltip
from rumble.ui.toast_menu import ToastMenu


class TrayIcon(QSystemTrayIcon):
    """System tray icon with toast popup for profiles."""

    open_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()
    profile_activate_requested = Signal(str)
    bypass_toggled = Signal(bool)
    auto_switch_toggled = Signal(bool)
    auto_switch_rules_requested = Signal()
    abx_requested = Signal()
    hearing_test_requested = Signal()
    listener_slot_selected = Signal(int)
    tone_trim_changed = Signal(float, float, float)
    tone_trim_bake_requested = Signal()
    crossfeed_toggled = Signal(bool)

    _BASE_TOOLTIP = "RUMBLE — EQ Profile Manager"

    def __init__(self, icon: QIcon, theme: dict[str, str], parent: QWidget | None = None):
        super().__init__(icon, parent)
        self.setToolTip(wrap_tooltip(self._BASE_TOOLTIP))

        self._toast = ToastMenu(theme, parent)
        self._toast.profile_selected.connect(self.profile_activate_requested.emit)
        self._toast.bypass_toggled.connect(self.bypass_toggled.emit)
        self._toast.auto_switch_toggled.connect(self.auto_switch_toggled.emit)
        self._toast.auto_switch_rules_requested.connect(self.auto_switch_rules_requested.emit)
        self._toast.abx_requested.connect(self.abx_requested.emit)
        self._toast.hearing_test_requested.connect(self.hearing_test_requested.emit)
        self._toast.listener_slot_selected.connect(self.listener_slot_selected.emit)
        self._toast.tone_trim_changed.connect(self.tone_trim_changed.emit)
        self._toast.tone_trim_bake_requested.connect(self.tone_trim_bake_requested.emit)
        self._toast.crossfeed_toggled.connect(self.crossfeed_toggled.emit)
        self._toast.open_requested.connect(self.open_requested.emit)
        self._toast.settings_requested.connect(self.settings_requested.emit)
        self._toast.quit_requested.connect(self.quit_requested.emit)
        self._active_profile_id: str | None = None

        self.activated.connect(self._on_activated)

    def set_profiles(self, profiles: list[ProfileMeta], active_id: str | None = None) -> None:
        """Update toast with profiles."""
        self._active_profile_id = active_id
        self._toast.set_profiles(profiles, active_id)

    def set_bypassed(self, bypassed: bool) -> None:
        """Update bypass state in the toast."""
        self._toast.set_bypass_state(bypassed)

    def set_auto_switch_state(self, enabled: bool, suspended: bool) -> None:
        """Update Auto-Switch toggle state in the toast."""
        self._toast.set_auto_switch_state(enabled, suspended)

    def set_listeners(self, names: dict[int, str], active_slot: int) -> None:
        """Update Listener Slot buttons in the toast."""
        self._toast.set_listeners(names, active_slot)

    def set_tone_trim(self, bass_db: float, treble_db: float, tilt_db_per_oct: float) -> None:
        """Sync the toast's tone-trim sliders to the current session_trim."""
        self._toast.set_tone_trim(bass_db, treble_db, tilt_db_per_oct)

    def set_crossfeed_state(self, enabled: bool) -> None:
        """Sync the toast's Crossfeed quick toggle to the current setting."""
        self._toast.set_crossfeed_state(enabled)

    def announce_switch(self, profile_name: str, reason: str) -> None:
        """Toast a switch made by AutoSwitchController — `reason` is "rule"
        (a rule just took hold) or "baseline" (restored after release)."""
        label = f"Auto: → {profile_name}" if reason == "rule" else f"Auto: restored → {profile_name}"
        self.showMessage("Auto-Switch", label,
                          QSystemTrayIcon.MessageIcon.Information, _SWITCH_TOAST_MS)

    def announce_coverage_warning(self) -> None:
        """Toast for A1: the default playback device just changed to one
        Equalizer APO isn't installed on (MainWindow._check_apo_coverage on
        DeviceFormatAdapter.format_changed)."""
        self.showMessage(
            "RUMBLE",
            "EQ is not reaching your new default playback device — it isn't "
            "hooked into Equalizer APO yet.",
            QSystemTrayIcon.MessageIcon.Warning, _SWITCH_TOAST_MS)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Handle tray icon activation."""
        if reason == QSystemTrayIcon.ActivationReason.Context:
            # Right-click: show toast at tray icon position
            tray_geom = self.geometry()
            if tray_geom.isValid() and tray_geom.width() > 0:
                x = tray_geom.x()
                y = tray_geom.y() - self._toast.height() - 5  # Above the icon
            else:
                # Fallback: position at bottom-right near cursor
                cursor = QCursor.pos()
                screen = QApplication.screenAt(cursor)
                if screen:
                    sg = screen.availableGeometry()
                    x = sg.right() - self._toast.width() - 10
                    y = sg.bottom() - self._toast.height() - 10
                else:
                    x, y = cursor.x(), cursor.y()
            self._toast.show_at_position(x, y)
        elif reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            # Left-click or double-click: open main window
            self.open_requested.emit()
