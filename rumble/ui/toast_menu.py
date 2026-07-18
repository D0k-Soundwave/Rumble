"""Toast-style popup menu for profile selection from system tray."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QScrollArea
)

from rumble.core.profile_manager import ProfileMeta
from rumble.core.tag_hierarchy import TagHierarchyBuilder, TagNode
from rumble.ui.a11y import describe
from rumble.ui.tone_trim_widget import ToneTrimWidget

#: Auto-hide the toast this long after the pointer leaves it (or after it is
#: shown and never hovered), so it doesn't linger on screen if abandoned.
_AUTO_DISMISS_MS = 4000


class ToastMenu(QWidget):
    """Toast popup showing profiles and app actions."""

    profile_selected = Signal(str)  # Emitted when user selects a profile
    bypass_toggled = Signal(bool)   # Emitted when bypass is toggled
    auto_switch_toggled = Signal(bool)     # Emitted when Auto-Switch master toggle flips
    auto_switch_rules_requested = Signal() # Emitted when "Auto-Switch Rules…" is clicked
    abx_requested = Signal()        # Emitted when "Blind A/B/X Test…" is clicked
    hearing_test_requested = Signal() # Emitted when "Ear Calibration…" is clicked
    listener_slot_selected = Signal(int)  # Emitted when a Listener Slot button is clicked
    tone_trim_changed = Signal(float, float, float)  # bass_db, treble_db, tilt_db_per_oct
    tone_trim_bake_requested = Signal()  # Emitted when "Bake into profile…" is clicked
    crossfeed_toggled = Signal(bool)  # Emitted when the Crossfeed quick toggle flips
    open_requested = Signal()       # Emitted when Open is clicked
    settings_requested = Signal()   # Emitted when Settings is clicked
    quit_requested = Signal()       # Emitted when Quit is clicked

    def __init__(self, theme: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        # Apply theme colors
        bg = theme['bg_surface']
        border = theme['border_subtle']
        text = theme['text_primary']
        hover = theme['bg_raised']
        accent = theme['accent']

        self.setStyleSheet(f"""
            ToastMenu {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 4px;
            }}
            QLabel {{
                color: {text};
                font-weight: bold;
                padding: 8px;
            }}
            QPushButton {{
                background-color: {bg};
                color: {text};
                border: none;
                padding: 8px;
                text-align: left;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {hover};
            }}
            QPushButton:checked {{
                background-color: {accent};
            }}
        """)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # Header
        self._header = QLabel("Profiles")
        self._layout.addWidget(self._header)

        # Scrollable area for profiles
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(300)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        self._profile_container = QWidget()
        self._profile_layout = QVBoxLayout(self._profile_container)
        self._profile_layout.setContentsMargins(0, 0, 0, 0)
        self._profile_layout.setSpacing(0)

        scroll.setWidget(self._profile_container)
        self._layout.addWidget(scroll)

        # ── listener slot ────────────────────────────────────────────
        self._listener_header = QLabel("Listener")
        self._layout.addWidget(self._listener_header)
        self._listener_buttons: dict[int, QPushButton] = {}
        for slot_id in (1, 2, 3, 4):
            btn = QPushButton(f"Listener {slot_id}")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, sid=slot_id: self._on_listener_clicked(sid))
            describe(btn, f"Listener {slot_id}",
                     "Switch the active listener slot -- applies a gentle, "
                     "automatic per-ear correction on top of whatever "
                     "profile is playing, without changing the profile.")
            self._layout.addWidget(btn)
            self._listener_buttons[slot_id] = btn
        self._layout.addSpacing(4)

        # ── tone trim (B1) ───────────────────────────────────────────
        self._tone_trim_header = QLabel("Tone Trim")
        self._layout.addWidget(self._tone_trim_header)
        self._tone_trim = ToneTrimWidget(theme, self)
        self._tone_trim.trim_changed.connect(self.tone_trim_changed.emit)
        self._tone_trim.bake_requested.connect(self.tone_trim_bake_requested.emit)
        self._layout.addWidget(self._tone_trim)
        self._layout.addSpacing(4)

        # ── crossfeed (B2) ────────────────────────────────────────────
        self.action_crossfeed = QPushButton("Crossfeed")
        self.action_crossfeed.setCheckable(True)
        self.action_crossfeed.toggled.connect(self.crossfeed_toggled.emit)
        describe(self.action_crossfeed, "Crossfeed",
                 "Quickly turn headphone crossfeed on or off, using its "
                 "current preset and strength from Settings -> Listeners.")
        self._layout.addWidget(self.action_crossfeed)
        self._layout.addSpacing(4)

        # Separator and action buttons
        self._layout.addSpacing(4)

        self.action_bypass = QPushButton("Bypass EQ")
        self.action_bypass.setCheckable(True)
        self.action_bypass.setStyleSheet("""
            QPushButton:checked {
                background-color: #d32f2f;
            }
        """)
        self.action_bypass.toggled.connect(self.bypass_toggled.emit)
        describe(self.action_bypass, "Bypass",
                 "Temporarily switch all EQ processing off to hear the unprocessed "
                 "sound. Click again to switch the EQ back on.")
        self._layout.addWidget(self.action_bypass)

        self.action_auto_switch = QPushButton("Auto-Switch")
        self.action_auto_switch.setCheckable(True)
        self.action_auto_switch.toggled.connect(self.auto_switch_toggled.emit)
        describe(self.action_auto_switch, "Auto-Switch",
                 "Automatically activate profiles by rule — which app is "
                 "playing, the default device, or time of day.")
        self._layout.addWidget(self.action_auto_switch)

        self.action_auto_switch_rules = QPushButton("Auto-Switch Rules…")
        self.action_auto_switch_rules.clicked.connect(self.auto_switch_rules_requested.emit)
        describe(self.action_auto_switch_rules, "Auto-Switch Rules",
                 "Open the rules editor for automatic profile switching.")
        self._layout.addWidget(self.action_auto_switch_rules)

        self.action_abx = QPushButton("Blind A/B/X Test…")
        self.action_abx.clicked.connect(self.abx_requested.emit)
        describe(self.action_abx, "Blind A/B/X Test",
                 "Run a blind listening test between two profiles to check "
                 "whether you can reliably tell them apart.")
        self._layout.addWidget(self.action_abx)

        self.action_hearing_test = QPushButton("Ear Calibration…")
        self.action_hearing_test.clicked.connect(self.hearing_test_requested.emit)
        describe(self.action_hearing_test, "Ear Calibration",
                 "Run an informal per-ear frequency calibration to learn more "
                 "about your ears' response on your current hardware — not a "
                 "medical evaluation.")
        self._layout.addWidget(self.action_hearing_test)

        self.action_open = QPushButton("Open RUMBLE")
        self.action_open.clicked.connect(self.open_requested.emit)
        describe(self.action_open, "Open RUMBLE",
                 "Open the RUMBLE main window.")
        self._layout.addWidget(self.action_open)

        self.action_settings = QPushButton("Settings…")
        self.action_settings.clicked.connect(self.settings_requested.emit)
        describe(self.action_settings, "Settings",
                 "Open RUMBLE settings: appearance, EQ defaults and APO integration.")
        self._layout.addWidget(self.action_settings)

        self.action_quit = QPushButton("Quit RUMBLE")
        self.action_quit.clicked.connect(self.quit_requested.emit)
        describe(self.action_quit, "Quit",
                 "Exit RUMBLE completely (EQ processing keeps running in "
                 "Equalizer APO).")
        self._layout.addWidget(self.action_quit)

        self._profile_buttons: dict[str, QPushButton] = {}
        self._tag_buttons: dict[str, QPushButton] = {}
        self._expanded_tags: set[str] = set()
        self._active_profile_id: str | None = None
        self._auto_dismiss_timer = QTimer(self)
        self._auto_dismiss_timer.setSingleShot(True)
        self._auto_dismiss_timer.setInterval(_AUTO_DISMISS_MS)
        self._auto_dismiss_timer.timeout.connect(self.hide)
        self._hierarchy_builder = TagHierarchyBuilder()
        self._profiles: dict[str, ProfileMeta] = {}

    def set_profiles(self, profiles: list[ProfileMeta], active_id: str | None = None) -> None:
        """Update the profile list with tag hierarchy."""
        # Clear old buttons
        for btn in self._profile_buttons.values():
            btn.deleteLater()
        for btn in self._tag_buttons.values():
            btn.deleteLater()
        self._profile_buttons.clear()
        self._tag_buttons.clear()
        self._expanded_tags.clear()

        # Store profile metadata for later lookup
        self._profiles = {p.id: p for p in profiles}
        self._active_profile_id = active_id

        # Build hierarchy from profiles
        self._hierarchy_builder.build_from_profiles(profiles)
        root = self._hierarchy_builder.root

        # Get all profiles in the hierarchy
        profiles_in_hierarchy = set(self._hierarchy_builder.find_profiles(root))

        # Display hierarchy
        if root.children:
            self._build_hierarchy_view(root, indent=0)

        # Add untagged profiles to "Untagged" section
        untagged_profiles = [p for p in profiles if p.id not in profiles_in_hierarchy]
        if untagged_profiles:
            self._add_untagged_section(untagged_profiles)

        self.adjustSize()

    def _add_untagged_section(self, profiles: list[ProfileMeta]) -> None:
        """Add an 'Untagged' section for profiles without tags."""
        if not profiles:
            return

        # Create Untagged header button
        tag_id = "Untagged"
        expanded = tag_id in self._expanded_tags
        btn = self._make_tag_button("Untagged", expanded, 0, [p.id for p in profiles])
        btn.clicked.connect(
            lambda checked=False, nid=tag_id: self._toggle_tag_expansion(nid)
        )
        self._profile_layout.addWidget(btn)
        self._tag_buttons[tag_id] = btn

        # Add profiles if expanded
        if expanded:
            for profile in sorted(profiles, key=lambda p: p.name):
                btn = self._make_profile_button(
                    profile.name, profile.id, self._active_profile_id, 1
                )
                self._profile_layout.addWidget(btn)
                self._profile_buttons[profile.id] = btn

    def _build_hierarchy_view(self, node: TagNode, indent: int = 0) -> None:
        """Recursively build the hierarchical view from TagNode tree."""
        for child in node.sorted_children():
            # Get profiles at this node (only direct, not descendants)
            profiles_at_node = list(child.profile_ids)

            # Create button for this node (both intermediate and leaf nodes)
            tag_id = self._get_node_id(child)
            expanded = tag_id in self._expanded_tags
            btn = self._make_tag_button(child.name, expanded, indent, profiles_at_node)
            btn.clicked.connect(
                lambda checked=False, nid=tag_id: self._toggle_tag_expansion(nid)
            )
            self._profile_layout.addWidget(btn)
            self._tag_buttons[tag_id] = btn

            # If expanded, show children and/or profiles
            if expanded:
                # Add direct profiles for this node
                for profile_id in sorted(profiles_at_node):
                    if profile_id in self._profiles:
                        profile = self._profiles[profile_id]
                        btn = self._make_profile_button(
                            profile.name, profile_id, self._active_profile_id, indent + 1
                        )
                        self._profile_layout.addWidget(btn)
                        self._profile_buttons[profile_id] = btn

                # Recursively add children if this node has any
                if child.children:
                    self._build_hierarchy_view(child, indent + 1)

    def _make_tag_button(self, tag_name: str, expanded: bool, indent: int,
                         profile_count: list[str]) -> QPushButton:
        """Create a button for a tag node with expansion arrow."""
        arrow = "▼" if expanded else "▶"
        count_text = f" ({len(profile_count)})" if profile_count else ""
        btn = QPushButton(f"{arrow} {tag_name}{count_text}")
        btn.setStyleSheet(f"""
            QPushButton {{
                text-align: left;
                padding-left: {16 + indent * 16}px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {self.theme['bg_raised']};
            }}
        """)
        describe(btn, tag_name, "Expand or collapse this tag group.")
        return btn

    def _make_profile_button(self, profile_name: str, profile_id: str,
                             active_id: str | None, indent: int) -> QPushButton:
        """Create a button for a profile."""
        btn = QPushButton(profile_name)
        btn.setCheckable(True)
        btn.setChecked(profile_id == active_id)
        btn.setStyleSheet(f"""
            QPushButton {{
                text-align: left;
                padding-left: {16 + indent * 16}px;
            }}
            QPushButton:checked {{
                background-color: {self.theme['accent']};
            }}
        """)
        btn.clicked.connect(
            lambda checked=False, pid=profile_id: self._on_profile_selected(pid)
        )
        describe(btn, profile_name, "Make this profile the active EQ.")
        return btn

    def _get_node_id(self, node: TagNode) -> str:
        """Generate a unique ID for a tag node."""
        return node.name

    def _toggle_tag_expansion(self, tag_id: str) -> None:
        """Toggle expansion state of a tag and rebuild view."""
        if tag_id in self._expanded_tags:
            self._expanded_tags.discard(tag_id)
        else:
            self._expanded_tags.add(tag_id)

        # Rebuild the view with new expansion state
        for btn in self._profile_buttons.values():
            btn.deleteLater()
        for btn in self._tag_buttons.values():
            btn.deleteLater()
        self._profile_buttons.clear()
        self._tag_buttons.clear()

        root = self._hierarchy_builder.root
        profiles_in_hierarchy = set(self._hierarchy_builder.find_profiles(root))

        if root.children:
            self._build_hierarchy_view(root, indent=0)

        # Add untagged profiles to "Untagged" section
        untagged_profiles = [p for p in self._profiles.values() if p.id not in profiles_in_hierarchy]
        if untagged_profiles:
            self._add_untagged_section(untagged_profiles)

        self.adjustSize()

    def set_bypass_state(self, bypassed: bool) -> None:
        """Update the bypass button state."""
        self.action_bypass.blockSignals(True)
        self.action_bypass.setChecked(bypassed)
        self.action_bypass.blockSignals(False)

    def set_auto_switch_state(self, enabled: bool, suspended: bool) -> None:
        """Update the Auto-Switch toggle's checked state and label. *suspended*
        (True while the editor has unsaved changes) shows as a paused label
        without unchecking the master toggle — the feature stays "on", it is
        just not acting right now."""
        self.action_auto_switch.blockSignals(True)
        self.action_auto_switch.setChecked(enabled)
        self.action_auto_switch.blockSignals(False)
        if enabled and suspended:
            self.action_auto_switch.setText("Auto-Switch (paused — unsaved edits)")
        else:
            self.action_auto_switch.setText("Auto-Switch")

    def set_listeners(self, names: dict[int, str], active_slot: int) -> None:
        """Update the listener buttons' labels and checked state."""
        for slot_id, btn in self._listener_buttons.items():
            label = names.get(slot_id) or f"Listener {slot_id}"
            btn.setText(label)
            btn.blockSignals(True)
            btn.setChecked(slot_id == active_slot)
            btn.blockSignals(False)

    def set_tone_trim(self, bass_db: float, treble_db: float, tilt_db_per_oct: float) -> None:
        """Sync the tone-trim sliders to the manager's current session_trim
        state (same pull-based refresh pattern as set_listeners())."""
        self._tone_trim.set_trim(bass_db, treble_db, tilt_db_per_oct)

    def set_crossfeed_state(self, enabled: bool) -> None:
        """Update the Crossfeed quick-toggle's checked state (same
        pull-based sync pattern as set_bypass_state/set_auto_switch_state)."""
        self.action_crossfeed.blockSignals(True)
        self.action_crossfeed.setChecked(enabled)
        self.action_crossfeed.blockSignals(False)

    def _on_listener_clicked(self, slot_id: int) -> None:
        for sid, btn in self._listener_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(sid == slot_id)
            btn.blockSignals(False)
        self.listener_slot_selected.emit(slot_id)

    def _on_profile_selected(self, profile_id: str) -> None:
        """Emit signal and hide."""
        self.profile_selected.emit(profile_id)
        self.hide()

    def show_at_position(self, x: int, y: int) -> None:
        """Show toast at the given position."""
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        # Start counting down to auto-hide in case the user never interacts;
        # enterEvent cancels it as soon as the pointer moves over the toast.
        self._auto_dismiss_timer.start()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Pointer is over the toast: the user is interacting, so keep it open.
        self._auto_dismiss_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Pointer left the toast: restart the countdown to auto-hide.
        self._auto_dismiss_timer.start()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._auto_dismiss_timer.stop()
        super().hideEvent(event)

