"""Browse, search, import, merge and simulate EQ profiles from online databases.

A single **unified** browser over the measurement-data layer
(:mod:`rumble.core.measurements`). Every enabled source — AutoEQ (headphones/IEMs)
and Spinorama (speakers) — is federated into one searchable catalogue
(:class:`~rumble.core.measurements.federated.FederatedCatalog`): one search box,
one results list, with the transducer exposed as a device-type **facet**
("Over-ear", "In-ear", "Earbud", "Speaker") rather than a database picker.

Workflows:

* **Import** — pick any result and import it. Headphone tunings import as-is
  (parametric, unchanged); a speaker's anechoic Listening Window is turned into a
  correction toward a target tilt (the speaker-only control, enabled when a
  speaker is highlighted) — flat ("anechoic starting point") or gently tilted
  (room-preference), usable even without a room measurement.
* **Merge** — average several measurements into one ``[Merged]`` profile.
* **Gear Simulation** — build a ``target - source`` profile.

All network calls run on background QThreads; profiles are created on the GUI
thread through ``ProfileManager`` so the library/tag-tree refresh path is
identical to a normal import.

The workspace (:class:`MeasurementWorkspace`) is a plain ``QWidget`` — usable
standalone via :class:`MeasurementBrowserDialog` (Tools menu) or embedded
directly in :class:`~rumble.ui.new_profile_dialog.NewProfileDialog`'s
"Measurement Database" tab, so creating a profile from a real measurement is
a first-class way to start a new profile, not a separate detour.
"""
from __future__ import annotations

import unicodedata
from collections import Counter

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from rumble.core import autoeq_merge, target_curves
from rumble.core.eq_target import ClampSummary, clamp_gain
from rumble.core.measurements import (
    CorrectionOptions, FederatedCatalog, enabled_providers,
)
from rumble.ui.a11y import describe, wrap_tooltip
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH, FramelessDialog,
    scrollable_page,
)

# Cap the number of rows rendered at once — searching thousands of entries is
# instant but building thousands of list items on every keystroke is not.
_RESULT_LIMIT = 400

#: Fixed size shared by MeasurementBrowserDialog and NewProfileDialog so the
#: Measurement Database workspace always renders identically in either host,
#: and neither dialog resizes itself after first showing -- the app's
#: standard pop-out dialog size (matches SettingsDialog).
DIALOG_SIZE = (STANDARD_DIALOG_WIDTH, STANDARD_DIALOG_HEIGHT + CHROME_STRIP_HEIGHT)


def center_over_parent(widget: QWidget) -> None:
    """Center a top-level dialog over its parent window (or its screen, if
    none/not yet visible). Qt's own placement is unreliable once a dialog's
    size can differ from what the window manager assumed at construction —
    calling this from showEvent keeps launches consistent every time."""
    parent = widget.parentWidget()
    target = None
    if parent is not None and parent.isVisible():
        target = parent.frameGeometry().center()
    else:
        screen = widget.screen()
        if screen is not None:
            target = screen.availableGeometry().center()
    if target is None:
        return
    frame = widget.frameGeometry()
    frame.moveCenter(target)
    widget.move(frame.topLeft())


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


# ── metadata helpers ─────────────────────────────────────────────────────

def _ascii(text: str) -> str:
    """Fold to ASCII-only (transliterate accents, drop the rest) for metadata.

    Used only for the generated [Merged]/[Sim] names, which embed free-form gear
    names; a stray accent is normalised defensively.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.split())


def _split_brand_model(name: str) -> tuple[str, str]:
    """Best-effort (brand, model) split — first token is the brand."""
    parts = name.split()
    if len(parts) <= 1:
        return name, ""
    return parts[0], " ".join(parts[1:])


def _gear_tag(prefix: str, name: str) -> str:
    """A hierarchical ``Prefix / Brand / Model`` tag for the tag tree."""
    brand, model = _split_brand_model(name)
    return f"{prefix} / {brand} / {model}" if model else f"{prefix} / {brand}"


def _representative_name(entries: list) -> str:
    """The most common device name among the selected measurements."""
    names = [e.name for e in entries]
    return Counter(names).most_common(1)[0][0] if names else ""


def _available_import_kinds(catalog, entry) -> list[tuple[str, str]]:
    """(label, kind) options for the 'Import as' combo. Pure/testable:
    driven only by static provider capability flags + the entry's
    transducer, never by probing the network (fetch calls only happen
    after the user picks and clicks Import)."""
    kinds = [("Tuning (parametric/graphic EQ)", "standard")]
    is_headphone = entry is not None and getattr(entry, "transducer", "") == "headphone"
    if is_headphone and getattr(catalog, "supports_fixed_band", False):
        kinds.append(("Fixed-Band EQ (device-native band count)", "fixed_band"))
    if is_headphone and getattr(catalog, "supports_convolution", False):
        kinds.append(("Convolution (impulse response)", "convolution"))
    return kinds


# ── reusable measurement search + merge panel ────────────────────────────

class _MeasurementPanel(QWidget):
    """Search a catalogue, tick measurements, and merge them into one curve.

    Catalogue-agnostic: all source/search/fetch calls go through the injected
    ``catalogue`` (a provider or a :class:`FederatedCatalog`), and network
    fetches run through the ``run_async`` callable so every call stays on a
    background thread. The device-type facet is shown only when the catalogue
    exposes types.
    """

    merged = Signal()                  # a fresh averaged curve is now available
    import_requested = Signal(object)  # a measurement was double-clicked
    selection_changed = Signal()       # the ticked/highlighted set changed

    def __init__(self, catalogue, run_async, *, checkable: bool = True,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._provider = catalogue
        self._run_async = run_async
        self._checkable = checkable  # False = plain single-select import list
        self._entries: list = []
        self._merge_thread: _FnThread | None = None
        self._loading = False

        # Latest merge result.
        self.merged_curve: list = []
        self.merged_sources: list[str] = []
        self.merged_name: str = ""
        self.merged_clamp: ClampSummary = ClampSummary()
        self._last_merge_entries: list = []

        self._build()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self._refresh_results)

    # ── construction ─────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.setContentsMargins(0, 0, 0, 0)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search by model or brand…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(lambda _=None: self._timer.start())
        describe(self.search_box, "Search measurements",
                 "Type a model or brand to filter the catalogue below.")
        lay.addWidget(self.search_box)

        facets = QHBoxLayout()
        self.type_combo: QComboBox | None = None
        device_types = self._provider.device_types()
        if device_types:
            self.type_combo = QComboBox()
            self.type_combo.addItem("All types", None)
            for t in device_types:
                self.type_combo.addItem(t, t)
            self.type_combo.currentIndexChanged.connect(self._refresh_results)
            describe(self.type_combo, "Device type filter",
                     "Limit the list to one kind of device — headphone types or "
                     "speakers — or show all types.")
            facets.addWidget(self.type_combo, 1)

        self.source_combo = QComboBox()
        self.source_combo.addItem("All sources", None)
        self.source_combo.currentIndexChanged.connect(self._refresh_results)
        describe(self.source_combo, "Measurement source filter",
                 "Limit the list to measurements from one source (reviewer or "
                 "measurement rig), or show every source.")
        facets.addWidget(self.source_combo, 1)
        lay.addLayout(facets)

        self.results = QListWidget()
        self.results.itemChanged.connect(lambda _=None: self._on_items_changed())
        self.results.itemDoubleClicked.connect(self._on_double_click)
        self.results.itemSelectionChanged.connect(lambda: self.selection_changed.emit())
        if self._checkable:
            describe(self.results, "Measurement list",
                     "Matching measurements. Tick the ones to combine, then use "
                     "Merge Selected to average them into a single curve.")
        else:
            describe(self.results, "Measurement list",
                     "Matching measurements. Double-click one to import it, or "
                     "highlight it and click Import Selected.")
        lay.addWidget(self.results, 1)

        # Select-All + Merge controls exist only for the merge/simulation panels;
        # the plain import panel is a single-select list with no checkboxes.
        self.select_all_btn: QPushButton | None = None
        self.merge_btn: QPushButton | None = None
        if self._checkable:
            row = QHBoxLayout()
            self.select_all_btn = QPushButton("Select All")
            self.select_all_btn.setProperty("class", "ghost")
            self.select_all_btn.setCheckable(True)
            self.select_all_btn.toggled.connect(self._on_select_all)
            describe(self.select_all_btn, "Select all measurements",
                     "Tick every measurement currently listed; click again to "
                     "clear them all.")
            row.addWidget(self.select_all_btn)
            row.addStretch()
            self.merge_btn = QPushButton("Merge Selected")
            describe(self.merge_btn, "Merge selected measurements",
                     "Average the ticked measurements into one combined curve.")
            self.merge_btn.clicked.connect(self.merge_selected)
            row.addWidget(self.merge_btn)
            lay.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setProperty("class", "muted")
        lay.addWidget(self.status)

        self._update_merge_enabled()

    # ── entries / search ─────────────────────────────────────────────
    def set_entries(self, entries: list) -> None:
        self._entries = entries
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("All sources", None)
        for src in self._provider.sources(entries):
            self.source_combo.addItem(src, src)
        self.source_combo.blockSignals(False)
        self._refresh_results()

    def _refresh_results(self) -> None:
        if not self._entries:
            return
        device_type = self.type_combo.currentData() if self.type_combo else None
        results = self._provider.search(
            self._entries,
            self.search_box.text(),
            source=self.source_combo.currentData(),
            device_type=device_type,
        )
        total = len(results)
        shown = results[:_RESULT_LIMIT]

        self._loading = True
        try:
            self.results.setUpdatesEnabled(False)
            self.results.clear()
            for entry in shown:
                item = QListWidgetItem(entry.display)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                item.setToolTip(wrap_tooltip(self._provider.badge(entry)))
                if self._checkable:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                self.results.addItem(item)
            self.results.setUpdatesEnabled(True)
        finally:
            self._loading = False

        if self._checkable:
            self._sync_select_all()
        if total == 0:
            self.status.setText("No matching devices. Try a different search.")
        elif total > _RESULT_LIMIT:
            self.status.setText(
                f"Showing first {_RESULT_LIMIT} of {total} matches — refine your search.")
        elif self._checkable:
            self.status.setText(
                f"{total} match{'es' if total != 1 else ''}. Tick measurements to merge.")
        else:
            self.status.setText(
                f"{total} match{'es' if total != 1 else ''}. Double-click a measurement "
                "to import it, or highlight one and click Import Selected.")
        self._update_merge_enabled()
        self.selection_changed.emit()

    # ── checkbox / selection plumbing ────────────────────────────────
    def _checked_items(self) -> list[QListWidgetItem]:
        return [self.results.item(i) for i in range(self.results.count())
                if self.results.item(i).checkState() == Qt.CheckState.Checked]

    def selected_entries(self) -> list:
        return [it.data(Qt.ItemDataRole.UserRole) for it in self._checked_items()]

    def current_entry(self):
        item = self.results.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_items_changed(self) -> None:
        if self._loading:
            return
        if self._checkable:
            self._sync_select_all()
        self._update_merge_enabled()
        self.selection_changed.emit()

    def _on_select_all(self, checked: bool) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for i in range(self.results.count()):
                self.results.item(i).setCheckState(state)
        finally:
            self._loading = False
        self.select_all_btn.setText("Clear All" if checked else "Select All")
        self._update_merge_enabled()
        self.selection_changed.emit()

    def _sync_select_all(self) -> None:
        n = self.results.count()
        all_checked = n > 0 and len(self._checked_items()) == n
        self.select_all_btn.blockSignals(True)
        self.select_all_btn.setChecked(all_checked)
        self.select_all_btn.setText("Clear All" if all_checked else "Select All")
        self.select_all_btn.blockSignals(False)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if entry is not None:
            self.import_requested.emit(entry)

    # ── merge ────────────────────────────────────────────────────────
    def has_merge(self) -> bool:
        return bool(self.merged_curve)

    def _merge_busy(self) -> bool:
        return self._merge_thread is not None and self._merge_thread.isRunning()

    def merge_selected(self) -> None:
        if self._merge_busy():
            return
        entries = self.selected_entries()
        self._last_merge_entries = entries
        if not entries:
            self.status.setText("Tick one or more measurements first.")
            return
        self.set_busy(True, f"Fetching {len(entries)} measurement"
                            f"{'s' if len(entries) != 1 else ''}…")
        provider = self._provider

        def work():
            curves, sources = [], []
            clamped_boost = clamped_cut = 0
            max_delta = 0.0
            for entry in entries:
                points, clamp = provider.fetch_curve(entry)
                if points:
                    curves.append(points)
                    sources.append(entry.source or entry.location)
                    clamped_boost += clamp.points_clamped_boost
                    clamped_cut += clamp.points_clamped_cut
                    max_delta = max(max_delta, clamp.max_clamped_delta_db)
            if not curves:
                raise ValueError("No curve data was returned for the selected measurements.")
            summary = ClampSummary(clamped_boost, clamped_cut, max_delta)
            return (autoeq_merge.average_curves(curves), sources,
                    _representative_name(entries), summary)

        self._merge_thread = self._run_async(work, self._on_merged, self._on_merge_failed)

    def _on_merged(self, result) -> None:
        self.merged_curve, self.merged_sources, self.merged_name, self.merged_clamp = result
        self.set_busy(False)
        n = len(self.merged_sources)
        message = f"Merged {n} measurement{'s' if n != 1 else ''} of “{self.merged_name}”."
        entries = self._last_merge_entries
        if entries and all(getattr(e, "transducer", "") == "speaker" for e in entries):
            if len(autoeq_merge.distinct_entry_names(entries)) > 1:
                message += (" Note: these are different speakers — averaging "
                            "assumes their responses are comparable.")
        self.status.setText(message)
        self.merged.emit()

    def _on_merge_failed(self, message: str) -> None:
        self.set_busy(False)
        self.status.setText(f"Merge failed — check your connection.\n({message})")

    # ── busy state ───────────────────────────────────────────────────
    def set_busy(self, busy: bool, message: str = "") -> None:
        widgets = [self.search_box, self.source_combo, self.results]
        if self.type_combo is not None:
            widgets.append(self.type_combo)
        if self.select_all_btn is not None:
            widgets.append(self.select_all_btn)
        for widget in widgets:
            widget.setEnabled(not busy)
        if message:
            self.status.setText(message)
        self._update_merge_enabled()

    def _update_merge_enabled(self) -> None:
        if self.merge_btn is not None:
            self.merge_btn.setEnabled(bool(self.selected_entries()) and not self._merge_busy())


# ── embeddable workspace ───────────────────────────────────────────────────

class MeasurementWorkspace(QWidget):
    """The searchable Import/Merge/Gear-Simulation workspace over the federated
    measurement catalogue — everything ``MeasurementBrowserDialog`` used to own
    directly, extracted so it can be embedded elsewhere (the New Profile
    dialog's "Measurement Database" tab) as well as shown standalone.

    Every profile this workspace creates goes straight through
    ``ProfileManager`` (unchanged from before); ``imported_ids`` accumulates
    their ids so a host dialog can report/select/open them after the fact.
    ``profile_created`` fires the moment each one is made — a host with a live
    profile list (the main window) connects to it so the list updates
    instantly, even while this workspace's dialog is still open (import,
    merge, and gear-sim can all happen several times in one session).
    """

    profile_created = Signal(str)

    def __init__(self, manager, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager
        self.theme = theme

        providers = enabled_providers(manager.data_dir, getattr(manager, "settings", {}))
        self.catalog = FederatedCatalog(providers)

        self.imported_ids: list[str] = []
        self._entries: list = []
        self._threads: list[_FnThread] = []
        self._index_thread: _FnThread | None = None
        self._import_thread: _FnThread | None = None

        self.import_panel: _MeasurementPanel | None = None
        self.merge_panel: _MeasurementPanel | None = None
        self.source_panel: _MeasurementPanel | None = None
        self.target_panel: _MeasurementPanel | None = None
        self.tilt_label: QLabel | None = None
        self.tilt_combo: QComboBox | None = None
        self.tilt_spin: QDoubleSpinBox | None = None
        self.pivot_spin: QDoubleSpinBox | None = None
        self.basis_label: QLabel | None = None
        self.basis_combo: QComboBox | None = None
        self.boost_cap_spin: QDoubleSpinBox | None = None
        self.import_kind_combo: QComboBox | None = None
        self.target_curve_label: QLabel | None = None
        self.target_curve_combo: QComboBox | None = None

        self._build_ui()
        self._load_index()

    # ── threading helper ─────────────────────────────────────────────
    def _run_async(self, fn, on_done, on_fail) -> _FnThread:
        thread = _FnThread(fn, self)
        thread.done.connect(on_done)
        thread.failed.connect(on_fail)
        thread.finished.connect(lambda t=thread: self._reap(t))
        self._threads.append(thread)
        thread.start()
        return thread

    def _reap(self, thread: _FnThread) -> None:
        if thread in self._threads:
            self._threads.remove(thread)

    # ── construction ─────────────────────────────────────────────────
    def _intro_text(self) -> str:
        return (
            "<b>Measurement Database</b> — one search across ready-to-use EQ for "
            "headphones &amp; IEMs (<a href='https://github.com/jaakkopasanen/AutoEq'>"
            "AutoEQ</a>) and speakers (<a href='https://www.spinorama.org'>Spinorama"
            "</a>). Filter by type, then import a tuning, merge several, or simulate "
            "gear. Headphone tunings import as-is; speaker corrections are an "
            "anechoic starting point (choose a target tilt) — refine with a room "
            "measurement where you can.")

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # A populated results list (real catalogue data, real fonts) can
        # want more height than the host dialog's fixed standard size has
        # to give -- scrollable_page keeps this widget's own minimum size
        # small regardless, so a full catalogue never forces the dialog
        # taller than STANDARD_DIALOG_HEIGHT (see frameless.scrollable_page
        # and measure_dialog.py's per-page wizard fix for the same issue).
        content = QWidget()
        outer.addWidget(scrollable_page(content))

        root = QVBoxLayout(content)
        root.setSpacing(12)
        root.setContentsMargins(0, 0, 0, 0)

        self.intro = QLabel(self._intro_text())
        self.intro.setWordWrap(True)
        self.intro.setOpenExternalLinks(True)
        root.addWidget(self.intro)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_import_tab(), "Import")
        self.tabs.setTabToolTip(0, "Search every database and import a tuning.")
        if self.catalog.supports_merge:
            self.tabs.addTab(self._build_merge_tab(), "Merge")
            self.tabs.addTab(self._build_sim_tab(), "Gear Simulation")
            self.tabs.setTabToolTip(1, "Average several measurements of one model "
                                       "into a single [Merged] profile.")
            self.tabs.setTabToolTip(2, "Build a profile that makes your source gear "
                                       "sound like a target set of gear.")
        root.addWidget(self.tabs, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.clamp_notice = QFrame()
        self.clamp_notice.setProperty("class", "warning-banner")
        clamp_layout = QHBoxLayout(self.clamp_notice)
        clamp_layout.setContentsMargins(12, 8, 12, 8)
        self.clamp_notice_label = QLabel("")
        self.clamp_notice_label.setWordWrap(True)
        clamp_layout.addWidget(self.clamp_notice_label)
        self.clamp_notice.setVisible(False)
        root.addWidget(self.clamp_notice)

        bar = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh Index")
        describe(self.refresh_btn, "Refresh catalogue",
                 "Re-download the latest catalogues from every database.")
        self.refresh_btn.clicked.connect(lambda: self._load_index(force=True))
        bar.addWidget(self.refresh_btn)
        bar.addStretch()
        root.addLayout(bar)

        if not self.catalog.providers:
            self.intro.setText("No measurement databases are available.")
            self.tabs.setEnabled(False)
            self.refresh_btn.setEnabled(False)

    def _build_import_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        intro = QLabel(
            "Search every database, then import a tuning into your library. Use the "
            "type filter to narrow to headphones, IEMs or speakers.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.import_panel = _MeasurementPanel(self.catalog, self._run_async,
                                              checkable=False)
        self.import_panel.import_requested.connect(self._import_entry)
        self.import_panel.selection_changed.connect(self._on_import_selection)
        lay.addWidget(self.import_panel, 1)

        if self.catalog.supports_tilt:
            lay.addLayout(self._build_tilt_row())
            lay.addLayout(self._build_basis_row())
            lay.addLayout(self._build_target_curve_row())

        row = QHBoxLayout()
        row.addWidget(QLabel("Import as:"))
        self.import_kind_combo = QComboBox()
        describe(self.import_kind_combo, "Import as",
                 "Choose which form of this measurement to import: the standard "
                 "tuning, a fixed-band variant, or (headphones only) a "
                 "convolution impulse response.")
        row.addWidget(self.import_kind_combo)
        row.addStretch()
        self.import_btn = QPushButton("Import Selected")
        describe(self.import_btn, "Import selected measurement",
                 "Import the highlighted measurement into your library as a "
                 "single profile.")
        self.import_btn.clicked.connect(self._on_import_clicked)
        row.addWidget(self.import_btn)
        lay.addLayout(row)

        self._update_import_kind_options()
        self._update_import_btn()
        return page

    def _build_tilt_row(self) -> QHBoxLayout:
        """Speaker target-tilt control — enabled only when a speaker is selected."""
        default_tilt = float(self.manager.settings.get("spinorama_default_tilt", -0.9))
        row = QHBoxLayout()
        self.tilt_label = QLabel("Speaker target:")
        row.addWidget(self.tilt_label)
        self.tilt_combo = QComboBox()
        self.tilt_combo.addItem("Anechoic flat (0 dB/oct)", 0.0)
        self.tilt_combo.addItem(f"Room tilt ({default_tilt:+.1f} dB/oct)", default_tilt)
        self.tilt_combo.addItem("Custom…", None)
        self.tilt_combo.setCurrentIndex(1)  # room tilt is the no-measurement default
        self.tilt_combo.currentIndexChanged.connect(self._on_tilt_changed)
        describe(self.tilt_combo, "Speaker target tilt",
                 "Applies only to speakers. Flat is the neutral anechoic starting "
                 "point; a downward room tilt approximates a typical, preferred "
                 "in-room response without needing a measurement.")
        row.addWidget(self.tilt_combo, 1)

        self.tilt_spin = QDoubleSpinBox()
        self.tilt_spin.setRange(-3.0, 1.0)
        self.tilt_spin.setSingleStep(0.1)
        self.tilt_spin.setDecimals(2)
        self.tilt_spin.setSuffix(" dB/oct")
        self.tilt_spin.setValue(default_tilt)
        self.tilt_spin.setEnabled(False)
        describe(self.tilt_spin, "Custom tilt slope",
                 "Set a custom speaker target slope in decibels per octave.")
        row.addWidget(self.tilt_spin)

        self.pivot_spin = QDoubleSpinBox()
        self.pivot_spin.setRange(20.0, 20000.0)
        self.pivot_spin.setDecimals(0)
        self.pivot_spin.setSingleStep(50.0)
        self.pivot_spin.setSuffix(" Hz")
        self.pivot_spin.setValue(1000.0)
        describe(self.pivot_spin, "Tilt pivot frequency",
                 "The frequency the target tilt pivots around — gain is "
                 "unchanged here; the slope rotates the target up or down on "
                 "either side of it.")
        row.addWidget(self.pivot_spin)
        return row

    def _build_basis_row(self) -> QHBoxLayout:
        """Speaker curve-basis control — enabled only when a speaker is selected."""
        row = QHBoxLayout()
        self.basis_label = QLabel("Curve basis:")
        row.addWidget(self.basis_label)
        self.basis_combo = QComboBox()
        self.basis_combo.addItem("Listening Window (default)", "Listening Window")
        self.basis_combo.addItem("On-Axis", "On Axis")
        self.basis_combo.addItem("Estimated In-Room", "Estimated In-Room Response")
        self.basis_combo.addItem("Early Reflections", "Early Reflections")
        self.basis_combo.addItem("Sound Power", "Sound Power")
        describe(self.basis_combo, "Speaker curve basis",
                 "Which measured curve the correction targets: Listening Window "
                 "(default, smoother), On-Axis (raw forward response), "
                 "Estimated In-Room (predicted room response), Early "
                 "Reflections (first-reflection energy), or Sound Power "
                 "(total radiated energy).")
        row.addWidget(self.basis_combo, 1)

        self.boost_cap_spin = QDoubleSpinBox()
        self.boost_cap_spin.setRange(0.0, 24.0)
        self.boost_cap_spin.setDecimals(1)
        self.boost_cap_spin.setSingleStep(0.5)
        self.boost_cap_spin.setSuffix(" dB")
        self.boost_cap_spin.setValue(6.0)
        describe(self.boost_cap_spin, "Correction boost cap",
                 "The maximum boost, in dB, the generated correction is "
                 "allowed to apply at any frequency — protects against "
                 "over-driving a dip a speaker can't really fill.")
        row.addWidget(self.boost_cap_spin)
        return row

    def _build_target_curve_row(self) -> QHBoxLayout:
        """Named target-curve selector (Rumble 3.4) — an alternative to the
        tilt row's linear tilt, defaulted by the highlighted entry's device
        type. Applies to headphones and speakers alike; disables the tilt
        controls above while a named curve is active (make_correction()
        ignores slope_db_per_oct when target_curve is set, so leaving the
        tilt row interactively enabled would be misleading)."""
        row = QHBoxLayout()
        self.target_curve_label = QLabel("Target curve:")
        row.addWidget(self.target_curve_label)
        self.target_curve_combo = QComboBox()
        self.target_curve_combo.currentIndexChanged.connect(self._update_tilt_enabled)
        describe(self.target_curve_combo, "Named target curve",
                 "Target a real, published response curve — Harman preference "
                 "research for headphones, or a diffuse-field rig curve — "
                 "instead of the plain tilt above. Choose 'Default' to go back "
                 "to the tilt-based target.")
        row.addWidget(self.target_curve_combo, 1)
        return row

    def _update_target_curve_options(self) -> None:
        combo = self.target_curve_combo
        if combo is None:
            return
        transducer = self._current_transducer()
        entry = self.import_panel.current_entry() if self.import_panel else None
        device_type = ""
        if entry is not None:
            device_type = getattr(entry, "device_type", "") or (
                "Speaker" if transducer == "speaker" else "")
        prior = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        default_label = ("Default (AutoEQ's own target)" if transducer == "headphone"
                         else "Default (linear tilt)")
        combo.addItem(default_label, None)
        for opt_label, name in target_curves.options_for_device_type(device_type):
            combo.addItem(opt_label, name)
        idx = combo.findData(prior) if prior is not None else -1
        if idx < 0:
            default_name = target_curves.default_for_device_type(device_type)
            idx = combo.findData(default_name) if default_name else -1
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        enabled = transducer in ("headphone", "speaker")
        self.target_curve_label.setEnabled(enabled)
        combo.setEnabled(enabled)

    def _on_tilt_changed(self, _index: int) -> None:
        self._update_tilt_enabled()

    def _on_import_selection(self) -> None:
        self._update_import_btn()
        self._update_target_curve_options()
        self._update_tilt_enabled()
        self._update_import_kind_options()

    def _update_import_kind_options(self) -> None:
        combo = self.import_kind_combo
        if combo is None:
            return
        entry = self.import_panel.current_entry() if self.import_panel else None
        prior = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for label, kind in _available_import_kinds(self.catalog, entry):
            combo.addItem(label, kind)
        idx = combo.findData(prior)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _current_transducer(self) -> str:
        entry = self.import_panel.current_entry() if self.import_panel else None
        return getattr(entry, "transducer", "") if entry is not None else ""

    def _update_tilt_enabled(self) -> None:
        if self.tilt_combo is None:
            return
        transducer = self._current_transducer()
        tilt_enabled = transducer in ("speaker", "headphone")
        named_active = bool(self.target_curve_combo is not None
                            and self.target_curve_combo.currentData())
        self.tilt_label.setEnabled(tilt_enabled)
        self.tilt_combo.setEnabled(tilt_enabled and not named_active)
        self.tilt_spin.setEnabled(
            tilt_enabled and not named_active and self.tilt_combo.currentData() is None)
        self.pivot_spin.setEnabled(tilt_enabled and not named_active)
        self._retext_tilt_row(transducer)
        if self.basis_combo is not None:
            speaker = transducer == "speaker"
            self.basis_label.setEnabled(speaker)
            self.basis_combo.setEnabled(speaker)
            self.boost_cap_spin.setEnabled(speaker)

    def _retext_tilt_row(self, transducer: str) -> None:
        """Swap the tilt row's copy/range per transducer, without rebuilding
        its combo items (so currentData()/the custom slope value survive a
        selection change between transducers)."""
        if transducer == "headphone":
            self.tilt_label.setText("Preference tilt:")
            self.tilt_combo.setItemText(0, "Harman target, unmodified")
            self.tilt_spin.setRange(-1.5, 1.5)
            describe(self.tilt_combo, "Headphone preference tilt",
                     "Applies only to headphones/IEMs. Adds an extra tilt on top "
                     "of AutoEQ's own Harman-based target — negative warms/adds "
                     "bass, positive brightens.")
        elif transducer == "speaker":
            self.tilt_label.setText("Speaker target:")
            self.tilt_combo.setItemText(0, "Anechoic flat (0 dB/oct)")
            self.tilt_spin.setRange(-3.0, 1.0)
            describe(self.tilt_combo, "Speaker target tilt",
                     "Applies only to speakers. Flat is the neutral anechoic "
                     "starting point; a downward room tilt approximates a "
                     "typical, preferred in-room response without needing a "
                     "measurement.")

    def _build_merge_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        intro = QLabel(
            "Found several measurements of one model (different rigs or sources)? "
            "Tick the ones to combine and <b>Merge Selected</b> averages them into a "
            "single <i>[Merged]</i> profile.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.merge_panel = _MeasurementPanel(self.catalog, self._run_async,
                                             checkable=True)
        self.merge_panel.merged.connect(self._on_merge_completed)
        lay.addWidget(self.merge_panel, 1)
        return page

    def _build_sim_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        intro = QLabel(
            "Build an averaged curve for your <b>source</b> gear (what you own) and "
            "your <b>target</b> gear (what you want it to sound like). The simulation "
            "applies the difference so the source mimics the target. "
            "<i>Approximate — verify by ear.</i>")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        panels = QHBoxLayout()
        panels.setSpacing(12)
        self.source_panel = _MeasurementPanel(self.catalog, self._run_async)
        self.target_panel = _MeasurementPanel(self.catalog, self._run_async)
        for title, panel in (("Source gear", self.source_panel),
                             ("Target gear", self.target_panel)):
            card = QFrame()
            card.setProperty("class", "card")
            cl = QVBoxLayout(card)
            cl.setSpacing(8)
            head = QLabel(title)
            head.setProperty("class", "heading")
            cl.addWidget(head)
            cl.addWidget(panel)
            panels.addWidget(card, 1)
            panel.merged.connect(self._update_sim_btn)
        lay.addLayout(panels, 1)

        row = QHBoxLayout()
        row.addStretch()
        self.sim_btn = QPushButton("Create Simulation Profile")
        self.sim_btn.setProperty("class", "primary")
        describe(self.sim_btn, "Create simulation profile",
                 "Create a profile from the difference between the merged target "
                 "and source curves, so the source gear mimics the target.")
        self.sim_btn.clicked.connect(self._create_sim_profile)
        row.addWidget(self.sim_btn)
        lay.addLayout(row)

        self._update_sim_btn()
        return page

    # ── clamp notice (Rumble 1.4 / 5.3) ─────────────────────────────────
    def _show_clamp_notice(self, summary: ClampSummary | None) -> None:
        """Surface a correction's boost/cut clamping instead of applying it
        silently — RUMBLE_ACTION_GUIDE.md #1.4's own "meaningful amount"
        (> 0.5 dB) significance bar, via ClampSummary.notable()."""
        if summary is not None and summary.notable():
            n = summary.points_clamped_boost + summary.points_clamped_cut
            self.clamp_notice_label.setText(
                f"{n} point{'s' if n != 1 else ''} of this correction were "
                f"limited by the boost/cut cap (by up to "
                f"{summary.max_clamped_delta_db:.1f} dB).")
            self.clamp_notice.setVisible(True)
        else:
            self.clamp_notice.setVisible(False)

    # ── index loading ────────────────────────────────────────────────
    def _panels(self) -> list[_MeasurementPanel]:
        return [p for p in (self.import_panel, self.merge_panel,
                            self.source_panel, self.target_panel) if p is not None]

    def _load_index(self, force: bool = False) -> None:
        if not self.catalog.providers:
            return
        self.refresh_btn.setEnabled(False)
        self.tabs.setEnabled(False)
        self.status.setText("Loading measurement catalogues…")
        self._index_thread = self._run_async(
            lambda: self.catalog.load_index(force_refresh=force),
            self._on_index_loaded, self._on_index_failed)

    def _on_index_loaded(self, entries: list) -> None:
        self._entries = entries
        for panel in self._panels():
            panel.set_entries(entries)
        self.refresh_btn.setEnabled(True)
        self.tabs.setEnabled(True)
        n_db = len(self.catalog.providers)
        self.status.setText(
            f"{len(entries)} measurements across {n_db} database"
            f"{'s' if n_db != 1 else ''}. Search for your gear to begin.")
        self._update_import_btn()
        self._update_target_curve_options()
        self._update_tilt_enabled()
        self._update_import_kind_options()

    def _on_index_failed(self, message: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.tabs.setEnabled(True)
        self.status.setText(
            "Could not load the catalogues. Check your internet connection "
            f"and try Refresh Index.\n({message})")

    # ── correction options (speaker tilt) ────────────────────────────
    def _current_options(self) -> CorrectionOptions | None:
        transducer = self._current_transducer()
        if self.tilt_combo is None or transducer not in ("speaker", "headphone"):
            return None
        data = self.tilt_combo.currentData()
        slope = self.tilt_spin.value() if data is None else float(data)
        default_tilt = float(self.manager.settings.get("spinorama_default_tilt", -0.9))
        named_curve = (self.target_curve_combo.currentData()
                      if self.target_curve_combo is not None else None)
        if named_curve:
            label = target_curves.label(named_curve)
        elif slope == 0:
            label = "unmodified" if transducer == "headphone" else "anechoic"
        elif transducer == "speaker" and abs(slope - default_tilt) < 1e-9:
            label = "room tilt"
        else:
            label = f"tilt {slope:+.1f}"
        smooth = float(self.manager.settings.get("spinorama_smoothing_oct", 1 / 3))
        basis = self.basis_combo.currentData() if self.basis_combo else "Listening Window"
        directivity_limiting = bool(self.manager.settings.get("spinorama_directivity_limiting", True))
        adaptive_smoothing = bool(self.manager.settings.get("adaptive_smoothing", True))
        return CorrectionOptions(slope_db_per_oct=slope, pivot_hz=self.pivot_spin.value(),
                                 target_curve_name=named_curve,
                                 smooth_oct=smooth, max_boost_db=self.boost_cap_spin.value(),
                                 label=label, curve_basis=basis,
                                 directivity_limiting=directivity_limiting,
                                 adaptive_smoothing=adaptive_smoothing)

    # ── single import ────────────────────────────────────────────────
    def _import_busy(self) -> bool:
        return self._import_thread is not None and self._import_thread.isRunning()

    def _on_import_clicked(self) -> None:
        self._import_entry(self.import_panel.current_entry())

    def _import_entry(self, entry) -> None:
        if entry is None or self._import_busy():
            return
        kind = self.import_kind_combo.currentData() if self.import_kind_combo else "standard"
        catalog = self.catalog
        options = self._current_options()

        # Rumble 3.1/3.2: a "standard" import (headphone or speaker) defaults
        # to a consensus curve across every measurement of the same device
        # (other sources/rigs) rather than just whichever one the user
        # happened to highlight — quality-weighted for Spinorama speakers via
        # FederatedCatalog.fetch_consensus_curve()'s entry_weight() dispatch.
        # The Merge tab still exists for hand-picking a subset.
        consensus_entries = None
        if kind == "standard" and getattr(entry, "transducer", "") in ("headphone", "speaker"):
            siblings = catalog.same_device_entries(self._entries, entry)
            if len(siblings) > 1:
                consensus_entries = siblings

        if consensus_entries is not None:
            self.status.setText(
                f"Importing “{entry.name}” — averaging {len(consensus_entries)} "
                "measurements…")
        else:
            self.status.setText(f"Importing “{entry.name}”…")
        self.clamp_notice.setVisible(False)

        def work():
            if consensus_entries is not None:
                return ("consensus", catalog.fetch_consensus_curve(
                    consensus_entries, options=options))
            if kind == "fixed_band":
                text = catalog.fetch_fixed_band(entry)
                if text is None:
                    raise ValueError("No fixed-band EQ is published for this device.")
                return ("parametric", text)
            if kind == "convolution":
                irs = catalog.fetch_convolution(entry)
                if not irs:
                    raise ValueError("No impulse response is published for this device.")
                return ("convolution", irs)
            text = catalog.fetch_parametric(entry, options=options)
            if text is not None:
                return ("parametric", text)
            return ("curve", catalog.fetch_curve(entry, options=options))

        self._import_thread = self._run_async(
            work,
            lambda res, e=entry, o=options: self._on_import_result(e, o, res),
            self._on_fetch_failed)
        self._update_import_btn()

    def _on_import_result(self, entry, options, result) -> None:
        catalog = self.catalog
        kind, payload = result
        clamp: ClampSummary | None = None
        spread_note = ""
        if kind == "curve":
            payload, clamp = payload
        elif kind == "consensus":
            payload, spread, sources, clamp = payload
            n = len(sources)
            spread_note = (f" Averaged {n} measurements ({', '.join(sources)}); "
                           f"spread up to ±{autoeq_merge.max_spread_db(spread):.1f} dB.")
        name = catalog.import_name(entry, options)
        tags = catalog.import_tags(entry, options)
        description = catalog.import_description(entry, options) + spread_note
        if kind == "parametric":
            meta = self.manager.create_from_apo_text(
                name, payload, description=description, tags=tags)
        elif kind == "convolution":
            meta = self.manager.create_profile_with_convolution(
                name, payload, description=description, tags=tags)
        else:
            if not payload:
                self.status.setText(f"“{entry.name}” returned no usable measurement data.")
                self._update_import_btn()
                return
            extra_filters = None
            if getattr(entry, "transducer", "") == "speaker" \
                    and self.manager.settings.get("spinorama_add_crossover", False):
                extra_filters = catalog.suggested_filters(entry)
            meta = self._save_profile(name, payload, description, tags, extra_filters=extra_filters)
        if meta is None:
            self.status.setText(f"“{entry.name}” could not be imported (no valid filters).")
            self._update_import_btn()
            return
        if kind in ("parametric", "convolution"):
            self._record_created(meta)
        self._show_clamp_notice(clamp)
        self.status.setText(
            f"Imported “{meta.name}”.{spread_note} Select more to import, or Close to view it.")
        self._update_import_btn()

    def _on_fetch_failed(self, message: str) -> None:
        self.status.setText(f"Download failed — check your connection.\n({message})")
        self._update_import_btn()

    def _update_import_btn(self) -> None:
        if self.import_panel is None:
            return
        self.import_btn.setEnabled(
            self.import_panel.current_entry() is not None and not self._import_busy())

    # ── merge → [Merged] profile ─────────────────────────────────────
    def _on_merge_completed(self) -> None:
        panel = self.merge_panel
        if not panel.has_merge():
            return
        n = len(panel.merged_sources)
        name = _ascii(f"[Merged] {panel.merged_name}")
        description = _ascii(
            f"Averaged from {n} measurements via online measurement databases. "
            f"Sources: {', '.join(panel.merged_sources)}.")
        tags = [_ascii(_gear_tag("Source", panel.merged_name)), "Character / Merged"]
        meta = self._save_profile(name, panel.merged_curve, description, tags)
        if meta is not None:
            self._show_clamp_notice(panel.merged_clamp)
            self.status.setText(
                f"Created merged profile “{meta.name}” from {n} measurements.")

    # ── simulation → [Sim] profile ───────────────────────────────────
    def _update_sim_btn(self) -> None:
        ready = self.source_panel.has_merge() and self.target_panel.has_merge()
        self.sim_btn.setEnabled(ready)
        self.sim_btn.setToolTip(wrap_tooltip(
            "Create a profile from the difference between the merged target and "
            "source curves, so the source gear mimics the target."
            if ready else "Merge a source curve and a target curve first."))

    def _create_sim_profile(self) -> None:
        src, tgt = self.source_panel, self.target_panel
        if not (src.has_merge() and tgt.has_merge()):
            return
        curve = autoeq_merge.delta_curve(tgt.merged_curve, src.merged_curve)
        if not curve:
            self.status.setText("Could not build the simulation — re-merge both sides.")
            return
        # Rumble 2.6: a delta of two arbitrary measured curves has no inherent
        # gain bound (unlike make_correction()'s speaker corrections) — apply
        # the same soft boost/cut cap and surface it via the existing notice.
        curve, sim_clamp = clamp_gain(curve)
        s_name, t_name = src.merged_name, tgt.merged_name
        n, m = len(src.merged_sources), len(tgt.merged_sources)
        name = _ascii(f"[Sim] {s_name} -> {t_name}")
        # "Provisional" status lives in the description — which is the profile
        # row's hover text — rather than as a tag.
        description = _ascii(
            f"Provisional simulation profile: applies the response difference "
            f"between {t_name} and {s_name} to make the source gear sound like the "
            f"target. Merged from {n} source measurements and {m} target "
            "measurements via online measurement databases. Approximate - verify by ear.")
        tags = [_ascii(_gear_tag("Source", s_name)),
                _ascii(_gear_tag("Target", t_name)),
                "Character / Simulation"]
        meta = self._save_profile(name, curve, description, tags)
        if meta is not None:
            self._show_clamp_notice(sim_clamp)
            self.status.setText(f"Created simulation profile “{meta.name}”.")

    # ── shared save ──────────────────────────────────────────────────
    def _save_profile(self, name: str, curve: list, description: str,
                      tags: list[str], extra_filters: list | None = None):
        """Persist a measured curve as a stereo (L R) user profile.

        The curve is decomposed into editable PK bands plus a residual GraphicEQ
        layer (autoeq_merge.build_curve_config) rather than stored as a bare
        curve, so the imported profile actually shows bands in the editor.

        Goes through ProfileManager.create_profile so the file is written to
        rumble/user/ with the canonical header and the library is updated exactly
        like a normal import. The new id is recorded and profile_created is
        emitted so a listening host (the main window) can refresh its profile
        list/tag tree immediately, not just when the dialog eventually closes.
        """
        sample_rate = self.manager.effective_sample_rate()
        config = autoeq_merge.build_curve_config(
            curve, sample_rate=sample_rate, extra_filters=extra_filters)
        meta = self.manager.create_profile(
            name, description=description, tags=tags, config=config,
            fit_sample_rate=sample_rate)
        if meta is not None:
            self._record_created(meta)
        return meta

    def _record_created(self, meta) -> None:
        """Track a newly created profile and notify any live listener
        immediately (not just when the host dialog eventually closes)."""
        self.imported_ids.append(meta.id)
        self.profile_created.emit(meta.id)

    # ── teardown ─────────────────────────────────────────────────────
    def shutdown(self) -> None:
        """Block briefly for any in-flight background thread. A plain QWidget's
        closeEvent never fires when it's embedded as a child (only true
        top-level windows get one), so hosts call this explicitly from their
        own closeEvent instead."""
        for thread in list(self._threads):
            if thread.isRunning():
                thread.wait(3000)


# ── standalone dialog ──────────────────────────────────────────────────────

class MeasurementBrowserDialog(FramelessDialog):
    """Unified measurement browser (Tools menu): a MeasurementWorkspace sized
    for standalone modal use -- dismissed via the chrome strip's close
    button, same as every other pop-out dialog."""

    def __init__(self, manager, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager
        self.theme = theme
        self.setModal(True)
        self.setFixedSize(*DIALOG_SIZE)
        self.setWindowTitle("Browse Measurement Database")
        self._centered = False

        self.workspace = MeasurementWorkspace(manager, theme, self)

        root = QVBoxLayout(self.body)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)
        root.addWidget(self.workspace, 1)

    @property
    def imported_ids(self) -> list[str]:
        return self.workspace.imported_ids

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._centered:
            center_over_parent(self)
            self._centered = True

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.workspace.shutdown()
        super().closeEvent(event)


#: Backwards-compatible alias — the dialog was AutoEQ-only before the
#: measurement-provider layer federated it.
AutoEqBrowserDialog = MeasurementBrowserDialog
