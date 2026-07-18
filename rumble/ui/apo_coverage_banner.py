"""A1 (docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md):
dismissable warning banner shown when the current default playback device is
not one Equalizer APO is installed on -- the single most common silent
failure of any APO front end (EQ looks applied in RUMBLE but nothing reaches
the speakers). Styling follows the existing "warning-banner" QFrame class
(see theme_engine.py, already used by autoeq_dialog.py's clamp_notice)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from rumble.ui.a11y import describe

# The banner covers the two ways RUMBLE's EQ can silently fail to reach a
# device. They share one banner (and one dismissal) because to the user they are
# the same problem — "the EQ isn't reaching my speakers" — but each needs its own
# explanation and its own one-click fix.
REASON_NOT_HOOKED = "not_hooked"
REASON_ENHANCEMENTS_OFF = "enhancements_off"

_MESSAGE_NOT_HOOKED = (
    "Your current default playback device does not have Equalizer APO "
    "installed on it, so RUMBLE's EQ is not reaching your speakers/"
    "headphones right now.")

# Windows runs Equalizer APO as a system effect. With "Audio enhancements" off,
# Windows skips the whole effects chain, so the EQ is inert even though APO is
# correctly installed and RUMBLE is writing its config perfectly — profiles,
# built-ins and even Bypass all appear to do nothing. Worth saying explicitly,
# because every symptom points at RUMBLE being broken when it isn't.
_MESSAGE_ENHANCEMENTS_OFF = (
    "Windows \"Audio enhancements\" is turned OFF for your current playback "
    "device. Equalizer APO runs as an audio enhancement, so RUMBLE's EQ is "
    "being ignored entirely — switching profiles and even Bypass will have no "
    "effect until you turn enhancements back on.")


class ApoCoverageBanner(QFrame):
    """Emits `configurator_requested` when the fix button is clicked,
    `sound_settings_requested` when the enhancements fix button is clicked,
    `manage_devices_requested` when the per-device assignments link is
    clicked (D1b: Settings -> Devices), and `dismissed` when the user
    dismisses it for the current endpoint.

    `set_reason()` switches which failure is being reported."""

    configurator_requested = Signal()
    sound_settings_requested = Signal()
    manage_devices_requested = Signal()
    dismissed = Signal()

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.setProperty("class", "warning-banner")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 12, 10)
        layout.setSpacing(10)

        self.label = QLabel(_MESSAGE_NOT_HOOKED)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(f"color: {theme['text_primary']};")
        layout.addWidget(self.label, 1)

        self.configurator_button = QPushButton("Open APO Configurator")
        describe(self.configurator_button, "Open APO Configurator",
                 "Launch Equalizer APO's device-hooking tool so you can add "
                 "your current playback device to it.")
        self.configurator_button.clicked.connect(self.configurator_requested.emit)
        layout.addWidget(self.configurator_button)

        self.sound_settings_button = QPushButton("Open Sound Settings")
        describe(self.sound_settings_button, "Open Sound Settings",
                 "Open Windows Sound settings for this device, where "
                 "\"Audio enhancements\" can be turned back on so Equalizer "
                 "APO — and therefore RUMBLE's EQ — runs again.")
        self.sound_settings_button.clicked.connect(self.sound_settings_requested.emit)
        self.sound_settings_button.hide()  # shown only for the enhancements reason
        layout.addWidget(self.sound_settings_button)

        self.manage_devices_button = QPushButton("Manage Devices…")
        self.manage_devices_button.setProperty("class", "ghost")
        describe(self.manage_devices_button, "Manage devices",
                 "Open Settings -> Devices, where each Equalizer APO-hooked "
                 "device can be assigned its own profile instead of always "
                 "following the global active profile.")
        self.manage_devices_button.clicked.connect(self.manage_devices_requested.emit)
        layout.addWidget(self.manage_devices_button)

        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setProperty("class", "ghost")
        describe(self.dismiss_button, "Dismiss",
                 "Stop showing this warning for the current playback device. "
                 "It reappears if you switch to a different un-hooked device.")
        self.dismiss_button.clicked.connect(self.dismissed.emit)
        layout.addWidget(self.dismiss_button)

    def set_reason(self, reason: str) -> None:
        """Report either REASON_NOT_HOOKED or REASON_ENHANCEMENTS_OFF: swap the
        explanation and show only the fix button that actually fixes THAT cause.
        Sending the user to the APO Configurator when the real problem is a
        Windows toggle (or vice versa) would be worse than saying nothing."""
        enhancements = reason == REASON_ENHANCEMENTS_OFF
        self.label.setText(
            _MESSAGE_ENHANCEMENTS_OFF if enhancements else _MESSAGE_NOT_HOOKED)
        self.sound_settings_button.setVisible(enhancements)
        self.configurator_button.setVisible(not enhancements)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.label.setStyleSheet(f"color: {theme['text_primary']};")
