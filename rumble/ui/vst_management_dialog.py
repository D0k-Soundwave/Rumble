"""VST Management -- a library-wide audit of every VST plugin referenced
across all profiles: which profiles use each plugin, whether the path still
resolves on disk, a manual relink for a broken path, a bulk remove across
every profile that references a plugin, and a best-effort folder scan to
help add a discovered plugin to a profile.

Not a VST host: preset "chunk" data is never decoded, generated or edited
here (it only ever comes from Equalizer APO Configurator and is carried
through untouched), and this never changes where the VST block sits in a
profile's rendered config. Shown from Tools -> VST Management…
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from rumble.core.apo_config import VSTPlugin
from rumble.core.profile_manager import ProfileManager
from rumble.core.vst_registry import DiscoveredPlugin, VstUsage, scan_common_vst_folders
from rumble.ui.a11y import describe
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH,
    FramelessDialog, scrollable_page,
)

_VST_FILE_FILTER = "VST Plugin (*.dll);;All Files (*)"


class _FnThread(QThread):
    """Run a no-arg callable off the GUI thread, emitting its result or error."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # surfaced to the user as a status message
            self.failed.emit(str(exc))


def _hline() -> QFrame:
    frame = QFrame()
    frame.setProperty("class", "hline")
    return frame


def _usage_item_text(usage: VstUsage) -> str:
    count = len(usage.profile_ids)
    state = "OK" if usage.exists else "MISSING"
    return (f"{Path(usage.path).name or usage.path}  —  "
            f"used by {count} profile{'s' if count != 1 else ''}  —  {state}")


class VSTManagementDialog(FramelessDialog):
    """Library-wide view of every VST plugin referenced across all
    profiles, with missing-path detection, manual relink, and a discovery
    scan that can add a found plugin to a chosen profile."""

    #: profile_ids whose config.txt was rewritten by a relink/add/remove, so
    #: MainWindow can resync a currently-open editor.
    profiles_changed = Signal(list)

    def __init__(self, manager: ProfileManager, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager
        self.theme = theme
        self._threads: list[_FnThread] = []

        self.setWindowTitle("VST Management")
        self.setModal(True)
        self.setFixedSize(STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT + CHROME_STRIP_HEIGHT)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        root.addWidget(self._build_hero())

        intro = QLabel(
            "Every VST 2.x plugin path referenced across your profile library. "
            "Preset data (chunks) is carried through automatically and is not "
            "shown or edited here — only the plugin path itself.")
        intro.setWordWrap(True)
        intro.setProperty("class", "secondary")
        root.addWidget(intro)

        self.list = QListWidget()
        self.list.setMaximumHeight(150)
        self.list.itemSelectionChanged.connect(self._on_selection_changed)
        describe(self.list, "VST plugins in use",
                 "Every distinct VST plugin path referenced by any profile in "
                 "your library, with how many profiles use it and whether the "
                 "path still exists on disk.")
        root.addWidget(self.list)

        self.detail_label = QLabel("Select a plugin above to see which profiles use it.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setProperty("class", "secondary")
        root.addWidget(self.detail_label)

        relink_row = QHBoxLayout()
        self.relink_btn = QPushButton("Relink…")
        self.relink_btn.setEnabled(False)
        describe(self.relink_btn, "Relink VST plugin",
                 "Choose a replacement DLL for the selected plugin path — every "
                 "profile referencing the old path is updated to the new one. "
                 "Only available for a path that no longer exists.")
        self.relink_btn.clicked.connect(self._on_relink_clicked)
        relink_row.addWidget(self.relink_btn)
        self.remove_btn = QPushButton("Remove…")
        self.remove_btn.setEnabled(False)
        describe(self.remove_btn, "Remove VST plugin",
                 "Remove the selected plugin from every profile that references "
                 "it. This does not touch the plugin's DLL on disk, only the "
                 "reference to it.")
        self.remove_btn.clicked.connect(self._on_remove_clicked)
        relink_row.addWidget(self.remove_btn)
        relink_row.addStretch()
        root.addLayout(relink_row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setProperty("class", "muted")
        root.addWidget(self.status)

        root.addWidget(_hline())

        disc_heading = QLabel("ADD FROM DISCOVERED PLUGINS")
        disc_heading.setProperty("class", "subheading")
        root.addWidget(disc_heading)

        disc_hint = QLabel(
            "A best-effort scan of common VST install folders. Nothing found "
            "here doesn't mean nothing's installed — browse manually from a "
            "profile's Advanced tab if your plugin isn't listed.")
        disc_hint.setWordWrap(True)
        disc_hint.setProperty("class", "muted")
        root.addWidget(disc_hint)

        self.discovered_list = QListWidget()
        self.discovered_list.setMaximumHeight(80)
        self.discovered_list.itemSelectionChanged.connect(self._on_discovered_selection_changed)
        describe(self.discovered_list, "Discovered VST plugins",
                 "Plugin DLLs found in common VST install folders on this machine.")
        root.addWidget(self.discovered_list)

        add_row = QHBoxLayout()
        self.profile_combo = QComboBox()
        describe(self.profile_combo, "Target profile",
                 "The profile the selected discovered plugin will be added to.")
        add_row.addWidget(self.profile_combo, 1)
        self.add_btn = QPushButton("Add to Profile")
        self.add_btn.setEnabled(False)
        describe(self.add_btn, "Add plugin to profile",
                 "Append the selected discovered plugin to the chosen profile's "
                 "VST plugin list.")
        self.add_btn.clicked.connect(self._on_add_clicked)
        add_row.addWidget(self.add_btn)
        self.rescan_btn = QPushButton("Rescan")
        describe(self.rescan_btn, "Rescan for VST plugins",
                 "Scan common VST install folders again.")
        self.rescan_btn.clicked.connect(self._rescan_discovered)
        add_row.addWidget(self.rescan_btn)
        root.addLayout(add_row)

        root.addStretch(1)

        outer = QVBoxLayout(self.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scrollable_page(content))

        self._refresh_report()
        self._refresh_profile_combo()
        self._rescan_discovered()

    # ── hero ─────────────────────────────────────────────────────────

    def _build_hero(self) -> QWidget:
        hero = QFrame()
        hero.setProperty("class", "hero")
        lay = QVBoxLayout(hero)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(4)

        title = QLabel("VST Management")
        title.setProperty("class", "subheading")
        lay.addWidget(title)

        self.summary_chip = QLabel("")
        self.summary_chip.setProperty("class", "chip-muted")
        row = QHBoxLayout()
        row.addWidget(self.summary_chip)
        row.addStretch()
        lay.addLayout(row)
        return hero

    # ── aggregate list ───────────────────────────────────────────────

    def _refresh_report(self) -> None:
        report = self.manager.vst_report()
        self._report = report
        self.list.clear()
        for usage in report.entries:
            item = QListWidgetItem(_usage_item_text(usage))
            item.setData(Qt.ItemDataRole.UserRole, usage)
            item.setToolTip(usage.path)
            if not usage.exists:
                item.setForeground(QBrush(QColor(self.theme["warning"])))
            self.list.addItem(item)
        n_plugins = len(report.entries)
        n_profiles = report.profile_count
        self.summary_chip.setText(
            f"{n_plugins} plugin{'s' if n_plugins != 1 else ''} across "
            f"{n_profiles} profile{'s' if n_profiles != 1 else ''}")
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        items = self.list.selectedItems()
        if not items:
            self.detail_label.setText("Select a plugin above to see which profiles use it.")
            self.relink_btn.setEnabled(False)
            self.remove_btn.setEnabled(False)
            return
        usage: VstUsage = items[0].data(Qt.ItemDataRole.UserRole)
        names = ", ".join(usage.profile_names)
        state = "This path exists on disk." if usage.exists else \
            "This path no longer exists on disk — relink it to fix every profile below."
        self.detail_label.setText(f"Used by: {names}\n{state}")
        self.relink_btn.setEnabled(not usage.exists)
        self.remove_btn.setEnabled(True)

    def _on_relink_clicked(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        usage: VstUsage = items[0].data(Qt.ItemDataRole.UserRole)
        new_path, _ = QFileDialog.getOpenFileName(
            self, "Select Replacement VST Plugin", "", _VST_FILE_FILTER)
        if not new_path:
            return
        result = self.manager.relink_vst_plugin(usage.path, new_path)
        self._refresh_report()
        if result.skipped_protected_ids:
            n = len(result.skipped_protected_ids)
            self.status.setText(
                f"Relinked {len(result.updated_profile_ids)} profile"
                f"{'s' if len(result.updated_profile_ids) != 1 else ''}. "
                f"{n} profile{'s' if n != 1 else ''} locked and not updated — "
                "unlock it first.")
        else:
            n = len(result.updated_profile_ids)
            self.status.setText(f"Relinked {n} profile{'s' if n != 1 else ''}.")
        if result.updated_profile_ids:
            self.profiles_changed.emit(result.updated_profile_ids)

    def _on_remove_clicked(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        usage: VstUsage = items[0].data(Qt.ItemDataRole.UserRole)
        name = Path(usage.path).name or usage.path
        count = len(usage.profile_ids)
        reply = QMessageBox.question(
            self, "Remove VST Plugin",
            f"Remove '{name}' from {count} profile{'s' if count != 1 else ''}? "
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = self.manager.remove_vst_plugin(usage.path)
        self._refresh_report()
        if result.skipped_protected_ids:
            n = len(result.skipped_protected_ids)
            self.status.setText(
                f"Removed from {len(result.updated_profile_ids)} profile"
                f"{'s' if len(result.updated_profile_ids) != 1 else ''}. "
                f"{n} profile{'s' if n != 1 else ''} locked and not updated — "
                "unlock it first.")
        else:
            n = len(result.updated_profile_ids)
            self.status.setText(f"Removed from {n} profile{'s' if n != 1 else ''}.")
        if result.updated_profile_ids:
            self.profiles_changed.emit(result.updated_profile_ids)

    # ── discovery ─────────────────────────────────────────────────────

    def _refresh_profile_combo(self) -> None:
        self.profile_combo.clear()
        for meta in self.manager.list_profiles():
            if not meta.protected:
                self.profile_combo.addItem(meta.name, meta.id)

    def _rescan_discovered(self) -> None:
        self.rescan_btn.setEnabled(False)
        thread = _FnThread(scan_common_vst_folders, self)
        thread.done.connect(self._on_discovered_ready)
        thread.failed.connect(self._on_discovered_failed)
        thread.finished.connect(lambda: self._reap_thread(thread))
        self._threads.append(thread)
        thread.start()

    def _reap_thread(self, thread: _FnThread) -> None:
        if thread in self._threads:
            self._threads.remove(thread)
        self.rescan_btn.setEnabled(True)

    def _on_discovered_ready(self, plugins: list) -> None:
        self.discovered_list.clear()
        for plugin in plugins:
            item = QListWidgetItem(plugin.filename)
            item.setData(Qt.ItemDataRole.UserRole, plugin)
            item.setToolTip(plugin.path)
            self.discovered_list.addItem(item)
        if not plugins:
            self.discovered_list.addItem("No plugins found in common install folders.")

    def _on_discovered_failed(self, message: str) -> None:
        self.status.setText(f"Plugin scan failed: {message}")

    def _on_discovered_selection_changed(self) -> None:
        items = self.discovered_list.selectedItems()
        has_plugin = bool(items) and items[0].data(Qt.ItemDataRole.UserRole) is not None
        self.add_btn.setEnabled(has_plugin and self.profile_combo.count() > 0)

    def _on_add_clicked(self) -> None:
        items = self.discovered_list.selectedItems()
        if not items:
            return
        plugin: DiscoveredPlugin | None = items[0].data(Qt.ItemDataRole.UserRole)
        profile_id = self.profile_combo.currentData()
        if plugin is None or profile_id is None:
            return
        config = self.manager.load_profile_config(profile_id)
        if config is None:
            self.status.setText("Could not load that profile.")
            return
        config.vst_plugins.append(VSTPlugin(path=plugin.path, chunks=""))
        if self.manager.save_profile_config(profile_id, config):
            self.status.setText(
                f"Added {plugin.filename} to {self.profile_combo.currentText()}.")
            self._refresh_report()
            self.profiles_changed.emit([profile_id])
        else:
            self.status.setText("Could not save that profile — is it locked?")

    # ── lifecycle ─────────────────────────────────────────────────────

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self._refresh_report()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        for thread in list(self._threads):
            thread.wait(2000)
        super().closeEvent(event)
