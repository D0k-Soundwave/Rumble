"""Modal dialog collecting every option needed to create a complete EQ
profile. Two ways to start, as tabs: (1) "Blank / Preset" — name, description,
tags, starting curve (flat or a built-in genre/situation preset), channel
layout, number of starting filter bands, and device scope, with the chosen
curve cloned across all channels; or (2) "Measurement Database" — an embedded
search/import/merge/gear-simulation workspace (reusing
rumble.ui.autoeq_dialog.MeasurementWorkspace) so starting a profile from a real
headphone or speaker measurement is built into New Profile directly, rather
than a separate dialog reached by a shortcut link."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QFormLayout, QInputDialog, QLabel,
    QLineEdit, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from rumble.ui.a11y import describe, wrap_tooltip
from rumble.ui.autoeq_dialog import DIALOG_SIZE, MeasurementWorkspace, center_over_parent
from rumble.ui.editor_panel import (
    CHANNEL_PRESETS, CUSTOM_LAYOUT_LABEL, MAX_BANDS,
    NON_STANDARD_CHANNEL_LAYOUT_WARNING, NON_STANDARD_CHANNEL_LAYOUTS,
)
from rumble.ui.frameless import FramelessDialog

DEFAULT_LAYOUT = "Stereo / Headphones"

FLAT_HINT = (
    "The profile starts flat (0 dB Peak bands). Channels are linked in "
    "the editor by default — edit one channel and all follow; turn off "
    "“Link Channels” there for independent per-channel values."
)


class NewProfileDialog(FramelessDialog):
    """Gathers all creation options; read the result via values().

    When ``manager`` is supplied, a second "Measurement Database" tab embeds
    the full import/merge/gear-simulation workspace — profiles created there
    are tracked in ``imported_ids`` instead of going through ``values()``.
    ``manager`` is optional (default ``None``) so callers that only need the
    blank/preset flow (previews, tests) don't need a real ``ProfileManager``.

    ``profile_created`` forwards the embedded workspace's own signal (once the
    workspace is lazily built) so a host with a live profile list connects to
    it before ``exec()`` and gets updates immediately, regardless of when the
    user actually switches to the Measurement Database tab.
    """

    profile_created = Signal(str)

    def __init__(self, theme: dict, default_bands: int = 10,
                 parent: QWidget | None = None, *, manager=None):
        super().__init__(parent)
        self.theme = theme
        self.manager = manager
        self.setWindowTitle("New Profile")
        self.setModal(True)
        self.setFixedSize(*DIALOG_SIZE)
        self._centered = False

        self._workspace: MeasurementWorkspace | None = None
        self._measurement_page: QWidget | None = None

        root = QVBoxLayout(self.body)
        root.setSpacing(12)

        self.mode_tabs = QTabWidget()
        self.mode_tabs.addTab(self._build_standard_page(default_bands), "Blank / Preset")
        self.mode_tabs.setTabToolTip(
            0, "Start from a blank or preset EQ curve, choosing name, layout "
               "and band count.")
        if manager is not None:
            self._measurement_page = QWidget()
            self.mode_tabs.addTab(self._measurement_page, "Measurement Database")
            self.mode_tabs.setTabToolTip(
                1, "Import a ready-made tuning, merge several measurements, or "
                   "simulate other gear — creates the profile directly.")
        root.addWidget(self.mode_tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setProperty("class", "primary")
        describe(buttons.button(QDialogButtonBox.StandardButton.Cancel), "Cancel",
                 "Close without creating a profile.")
        root.addWidget(buttons)

        self.mode_tabs.currentChanged.connect(self._on_mode_changed)
        self._update_ok_button()
        self.name_edit.setFocus()

    # ── "Blank / Preset" page ─────────────────────────────────────────
    def _build_standard_page(self, default_bands: int) -> QWidget:
        page = QWidget()
        page_root = QVBoxLayout(page)
        page_root.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. HD650 Music")
        describe(self.name_edit, "Profile name",
                 "The name of the new profile, shown in the profile list. Required.")
        self.name_edit.textChanged.connect(lambda _=None: self._update_ok_button())
        form.addRow("Name:", self.name_edit)

        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("Optional description")
        describe(self.desc_edit, "Profile description",
                 "An optional note about what this profile is for.")
        form.addRow("Description:", self.desc_edit)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("Comma-separated, e.g. Music, Headphones")
        describe(self.tags_edit, "Tags",
                 "Optional comma-separated labels used to filter the profile "
                 "list, e.g. 'Music, Headphones'.")
        form.addRow("Tags:", self.tags_edit)

        self.layout_combo = QComboBox()
        self.layout_combo.addItems(list(CHANNEL_PRESETS.keys()) + [CUSTOM_LAYOUT_LABEL])
        self.layout_combo.setCurrentText(DEFAULT_LAYOUT)
        # Channel names entered via Custom…, used while that entry is selected.
        self._custom_channels: list[str] = []
        self._last_preset_layout = DEFAULT_LAYOUT
        self.layout_combo.currentTextChanged.connect(self._on_layout_changed)
        describe(self.layout_combo, "Channel layout",
                 "The speaker arrangement this profile EQs — each layout "
                 "creates a filter section per channel. Custom… accepts any "
                 "channel names your device exposes.")
        form.addRow("Channel layout:", self.layout_combo)

        self.bands_spin = QSpinBox()
        self.bands_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bands_spin.setRange(1, MAX_BANDS)
        self.bands_spin.setValue(max(1, min(int(default_bands), MAX_BANDS)))
        describe(self.bands_spin, "Starting filter bands",
                 "How many flat (0 dB) filter bands the profile starts with — "
                 "you can add or remove bands later, up to 32.")
        form.addRow("Filter bands:", self.bands_spin)

        self.device_combo = QComboBox()
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        try:
            from rumble.core.audio_devices import list_apo_devices
            devices = list_apo_devices()
        except Exception:
            devices = []
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
        describe(self.device_combo, "Device",
                 "Which audio output device the profile applies to — one of the "
                 "endpoints Equalizer APO is installed on, or 'All' for every device.")
        form.addRow("Device:", self.device_combo)

        page_root.addLayout(form)

        self._hint = QLabel(FLAT_HINT)
        self._hint.setWordWrap(True)
        self._hint.setProperty("class", "muted")
        page_root.addWidget(self._hint)
        page_root.addStretch()
        return page

    # ── "Measurement Database" tab ────────────────────────────────────
    def _on_mode_changed(self, index: int) -> None:
        # The dialog is a fixed size (DIALOG_SIZE) set once in __init__ and
        # never changes — only the tab content and OK button react here.
        if index == 1 and self._workspace is None and self._measurement_page is not None:
            self._workspace = MeasurementWorkspace(self.manager, self.theme)
            self._workspace.profile_created.connect(self.profile_created.emit)
            page_layout = QVBoxLayout(self._measurement_page)
            page_layout.setContentsMargins(14, 14, 14, 14)
            page_layout.addWidget(self._workspace)
        self._update_ok_button()

    def _update_ok_button(self) -> None:
        if self.mode_tabs.currentIndex() == 1:
            self._ok_button.setText("Done")
            self._ok_button.setEnabled(True)
            describe(self._ok_button, "Done",
                     "Close this dialog. Anything imported, merged, or "
                     "simulated in the Measurement Database tab is already "
                     "created and saved.")
        else:
            self._ok_button.setText("Create")
            self._ok_button.setEnabled(bool(self.name_edit.text().strip()))
            describe(self._ok_button, "Create profile",
                     "Create the profile with these options and open it in the "
                     "editor. Enabled once a name is entered.")

    @property
    def imported_ids(self) -> list[str]:
        """Profile ids created via the embedded Measurement Database tab, if
        any — checked by the caller before falling back to ``values()``."""
        return self._workspace.imported_ids if self._workspace is not None else []

    def _on_layout_changed(self, layout_name: str) -> None:
        if layout_name != CUSTOM_LAYOUT_LABEL:
            self._last_preset_layout = layout_name
            self._update_hint(layout_name)
            return
        text, ok = QInputDialog.getText(
            self, "Custom Channel Layout",
            "Space-separated channel names (e.g. L R C LFE, or channel "
            "numbers):", text=" ".join(self._custom_channels))
        tokens = list(dict.fromkeys(text.split())) if ok else []
        if tokens:
            self._custom_channels = tokens
        else:
            # Cancelled/empty — fall back to the previously chosen preset.
            self.layout_combo.blockSignals(True)
            self.layout_combo.setCurrentText(self._last_preset_layout)
            self.layout_combo.blockSignals(False)
        self._update_hint(self.layout_combo.currentText())

    def _update_hint(self, layout_name: str) -> None:
        if layout_name in NON_STANDARD_CHANNEL_LAYOUTS:
            self._hint.setText(
                FLAT_HINT + "\n\n"
                + NON_STANDARD_CHANNEL_LAYOUT_WARNING.format(name=layout_name))
        else:
            self._hint.setText(FLAT_HINT)

    def channels(self) -> list[str]:
        if (self.layout_combo.currentText() == CUSTOM_LAYOUT_LABEL
                and self._custom_channels):
            return list(self._custom_channels)
        return list(CHANNEL_PRESETS.get(self.layout_combo.currentText(), ["L", "R"]))

    def values(self) -> dict:
        from rumble.core.audio_devices import first_active_device_pattern

        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        # Ensure we never explicitly set device to "all" — use first active device
        device = str(self.device_combo.currentData() or "")
        if device.lower() == "all" or not device:
            device = first_active_device_pattern()

        return {
            "name": self.name_edit.text().strip(),
            "description": self.desc_edit.text().strip(),
            "tags": tags,
            "channels": self.channels(),
            "bands": int(self.bands_spin.value()),
            "device": device,
        }

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._centered:
            center_over_parent(self)
            self._centered = True

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._workspace is not None:
            self._workspace.shutdown()
        super().closeEvent(event)
