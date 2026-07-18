"""Modal offering the "replace or blend with community data" choice for a
REW frequency-response measurement import (Rumble 3.8) — a user's own real
measurement either stands alone, or blends with an existing profile's
already-imported community curve (AutoEQ/Spinorama), weighted toward the
REW measurement per rew_import.REW_MEASUREMENT_WEIGHT. Only shown for
measurement imports; a Filter Settings import has no curve to blend and
skips this dialog entirely (see main_window.py's _on_import_rew)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialogButtonBox, QLabel, QVBoxLayout, QWidget,
)

from rumble.core import autoeq_merge
from rumble.ui.a11y import describe
from rumble.ui.frameless import FramelessDialog


class RewMeasurementImportDialog(FramelessDialog):
    """Pick whether a REW measurement import blends with an existing
    profile's curve. Read the result via `blend_curve()` after `exec()`
    returns `Accepted` — `None` means "use the REW measurement alone"."""

    def __init__(self, manager, file_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._manager = manager
        self._curves_by_id: dict[str, list] = {}

        self.setWindowTitle("Import from REW")
        self.setModal(True)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self.body)
        root.setSpacing(12)

        intro = QLabel(
            f"Import the frequency-response measurement in “{file_name}” as a "
            "new profile. Your own real measurement is the most trustworthy "
            "data source RUMBLE can use for this device.")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        root.addWidget(intro)

        self.chk_blend = QCheckBox("Blend with an existing profile's curve")
        describe(self.chk_blend, "Blend with an existing profile",
                 "Average this measurement with a profile you already imported from "
                 "AutoEQ or Spinorama, weighted toward your own measurement. Leave "
                 "unticked to use only the REW measurement.")
        self.chk_blend.toggled.connect(self._on_blend_toggled)
        root.addWidget(self.chk_blend)

        self.combo_profile = QComboBox()
        for meta in manager.list_profiles():
            config = manager.load_profile_config(meta.id)
            if config is None or not config.channel_sections:
                continue
            # The section's TOTAL shape (bands + GraphicEQ layer), not its
            # GraphicEQ line alone: an imported profile now stores its curve as
            # bands plus a small residual, so reading graphic_eq by itself would
            # blend against the residual wiggle instead of the correction.
            curve = autoeq_merge.section_curve(
                config.channel_sections[0], manager.effective_sample_rate())
            if not curve:
                continue
            self._curves_by_id[meta.id] = curve
            self.combo_profile.addItem(meta.name, meta.id)
        self.combo_profile.setEnabled(False)
        describe(self.combo_profile, "Profile to blend with",
                 "The existing profile whose curve this measurement blends with.")
        root.addWidget(self.combo_profile)

        if self.combo_profile.count() == 0:
            self.chk_blend.setEnabled(False)
            self.chk_blend.setToolTip(
                "No existing profile with an importable curve was found to blend with.")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Import")
        ok.setProperty("class", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_blend_toggled(self, checked: bool) -> None:
        self.combo_profile.setEnabled(checked)

    def blend_curve(self) -> list | None:
        """The existing profile's curve to blend with, or `None` when the
        user left "blend" unticked (import the REW measurement alone)."""
        if not self.chk_blend.isChecked() or self.combo_profile.count() == 0:
            return None
        profile_id = self.combo_profile.currentData()
        return self._curves_by_id.get(profile_id)
