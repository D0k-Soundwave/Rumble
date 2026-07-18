"""Full settings window.

Eleven pages in four labelled nav groups (see NAV_GROUPS). Each page owns one
subject and says so in a one-line caption under its title; within a page,
related controls sit in a titled _Card rather than a flat stack of checkboxes,
so the page's shape carries the grouping instead of the reader having to infer
it from the order things happen to appear in.

A setting lives on the page that owns the thing it changes -- Ear Calibration
gets its own page (rather than being folded into Listeners) because it is a
distinct action with its own flow, not just a listener-identity field; Blind
A/B/X sits with the profile library it compares, the sample rate with the APO
engine it describes.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSlider,
    QSpinBox, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

from rumble.core import (
    apo_path, audio_devices, crossfeed, hearing_test, iso226, listener_slots, settings_reset,
)
from rumble.core.custom_themes import save_custom_theme, validate_theme
from rumble.ui.a11y import describe
from rumble.ui.density import dp
from rumble.ui.frameless import (
    CHROME_STRIP_HEIGHT, STANDARD_DIALOG_HEIGHT, STANDARD_DIALOG_WIDTH, FramelessDialog,
    scrollable_page,
)
from rumble.ui.theme.tokens import (
    DEFAULT_THEME_NAME, THEMES, FALLBACK_THEME, build_system_theme,
    ordered_theme_names, qss_color,
)

# The nav rail, in order: (group label, page names). A group answers "why is
# this page next to that one" -- the four groups are the four things settings
# here actually act on: your ears, your profile library, what plays where, and
# the application itself.
NAV_GROUPS: list[tuple[str, list[str]]] = [
    ("Listening", ["Listeners", "Ear Calibration", "Enhancements"]),
    ("Profiles", ["Library", "Corrections"]),
    ("Playback", ["Devices", "Auto-Switch", "Equalizer APO"]),
    ("Application", ["Appearance", "Theme Creator", "Startup", "Backup & Restore"]),
]

# Page names in stack order. Callers address pages by name
# (CATEGORIES.index("Devices")), so this stays the public contract; NAV_GROUPS
# only decides how the rail is drawn.
CATEGORIES: list[str] = [name for _group, names in NAV_GROUPS for name in names]

# How long after the last control change settings are applied and written.
# There is no Save button -- every control applies itself -- so a continuous
# control (the crossfeed strength slider, a spin box being typed into) would
# otherwise fire one full settings write + APO re-render per pixel/keystroke.
# 250 ms is short enough that a released slider still feels instant and long
# enough that a drag coalesces into a single apply.
APPLY_DEBOUNCE_MS = 250

# Grouped token labels for the editor (token_key, display_label)
_EDITOR_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Backgrounds", [
        ("bg_deep",    "Deep (darkest)"),
        ("bg_base",    "Base"),
        ("bg_surface", "Surface"),
        ("bg_raised",  "Raised"),
        ("bg_sunken",  "Sunken (inputs)"),
    ]),
    ("Borders", [
        ("border_subtle",  "Subtle"),
        ("border_default", "Default"),
        ("border_strong",  "Strong / Focus"),
    ]),
    ("Accent", [
        ("accent",        "Primary"),
        ("accent_hover",  "Hover"),
        ("accent_active", "Pressed"),
    ]),
    ("Text", [
        ("text_primary",   "Primary"),
        ("text_secondary", "Secondary"),
        ("text_muted",     "Muted"),
        ("text_on_accent", "On Accent"),
    ]),
    ("Semantic", [
        ("success", "Success"),
        ("warning", "Warning"),
        ("error",   "Error"),
    ]),
    ("Graph", [
        ("graph_bg",       "Background"),
        ("graph_grid",     "Grid lines"),
        ("graph_zero",     "Zero line"),
        ("graph_curve",    "Curve"),
        ("graph_band_sel", "Selected band"),
    ]),
]


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "heading")
    return label


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "muted")
    label.setWordWrap(True)
    return label


def _page(title: str, caption: str) -> tuple[QWidget, QVBoxLayout]:
    """A page shell: title, one line saying what the page is for, then cards.

    The caption is not decoration -- it is the page's claim to its own
    contents. If a control cannot be justified by the caption of the page it
    is on, it is on the wrong page.
    """
    page = QWidget()
    layout = QVBoxLayout(page)
    # Bottom margin matches the inter-card spacing below, not a wider
    # "footer" gap -- the last card's border is the page's real bottom edge.
    layout.setContentsMargins(dp(24, 20), dp(22, 18), dp(24, 20), dp(12, 10))
    layout.setSpacing(dp(12, 10))
    layout.addWidget(_section_title(title.upper()))
    layout.addWidget(_caption(caption))
    layout.addSpacing(dp(4, 2))
    return page, layout


class _Card(QFrame):
    """One titled group of related settings inside a page.

    Uses the shared "card" QSS class, so the glass/rim treatment matches every
    other card in the app rather than being invented here.
    """

    def __init__(self, title: str, caption: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("class", "card")
        self._body = QVBoxLayout(self)
        self._body.setContentsMargins(dp(16, 14), dp(14, 12), dp(16, 14), dp(16, 14))
        self._body.setSpacing(dp(10, 8))
        head = QLabel(title)
        head.setProperty("class", "subheading")
        self._body.addWidget(head)
        if caption:
            self._body.addWidget(_caption(caption))

    def add(self, widget: QWidget) -> QWidget:
        self._body.addWidget(widget)
        return widget

    def add_layout(self, layout) -> object:
        self._body.addLayout(layout)
        return layout

    def add_row(self, label_text: str, *widgets: QWidget) -> QHBoxLayout:
        """A labelled control row: label, control(s), then stretch."""
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        for widget in widgets:
            row.addWidget(widget)
        row.addStretch(1)
        self._body.addLayout(row)
        return row


# Shared with every other pop-out dialog fixed at the standard size (see
# frameless.py) -- this dialog is where the pattern originated.
_scrollable = scrollable_page


def _info_banner(text: str, theme: dict) -> QFrame:
    frame = QFrame()
    frame.setProperty("class", "info-banner")
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(16, 12, 16, 12)
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme['text_primary']};")
    layout.addWidget(label)
    return frame


class ThemeSwatch(QFrame):
    """Clickable 120×80 card previewing a theme's background and accent colours."""

    clicked = Signal(str)

    def __init__(self, theme_name: str, tokens: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme_name = theme_name
        self.setFixedSize(120, 80)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tokens = tokens
        describe(self, f"{theme_name} theme",
                 f"Switch the whole application to the '{theme_name}' colour "
                 "theme. Applies and is kept immediately.")
        self._build()

    def _build(self) -> None:
        t = self._tokens
        self.setStyleSheet(
            f"QFrame {{ background-color: {t['bg_base']}; border: 2px solid {t['border_subtle']}; "
            f"border-radius: 6px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(4)
        for color in (t["bg_surface"], t["accent"], t["graph_curve"]):
            chip = QFrame()
            chip.setFixedSize(20, 20)
            chip.setStyleSheet(f"background-color: {color}; border-radius: 4px; border: 1px solid {t['border_default']};")
            swatch_row.addWidget(chip)
        swatch_row.addStretch(1)
        layout.addLayout(swatch_row)
        layout.addStretch(1)

        name_label = QLabel(self.theme_name)
        # border: none is load-bearing. QLabel IS a QFrame, so the "QFrame"
        # rule this widget sets on itself above cascades onto this label and
        # draws the swatch's own 2px rounded border around the name.
        name_label.setStyleSheet(
            f"color: {t['text_primary']}; font-weight: 600; font-size: 12px; "
            "background: transparent; border: none;")
        layout.addWidget(name_label)

    def set_selected(self, selected: bool) -> None:
        t = self._tokens
        border_color = t["accent"] if selected else t["border_subtle"]
        width = 2
        self.setStyleSheet(
            f"QFrame {{ background-color: {t['bg_base']}; border: {width}px solid {border_color}; "
            f"border-radius: 6px; }}"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().mousePressEvent(event)
        self.clicked.emit(self.theme_name)


class ResetSettingsDialog(FramelessDialog):
    """Pick which groups of settings go back to their defaults.

    A checklist rather than one all-or-nothing button: the groups differ a lot
    in cost to undo (re-picking a theme is nothing; re-writing your auto-switch
    rules is not), so the user chooses. Nothing is checked at open and the
    Reset button stays disabled until something is -- this dialog IS the
    confirmation step, so it must not be dismissable into a destructive default.
    """

    def __init__(self, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Reset Settings")
        self.setMinimumWidth(dp(520, 440))

        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        layout.addWidget(_section_title("RESET SETTINGS TO DEFAULTS"))
        intro = QLabel("Choose what to put back to its default. Anything you "
                       "leave unticked is untouched.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(_info_banner(
            "Your profiles, custom themes, IR files and Ear Calibration results "
            "are never touched by a reset. Neither is which profile is active, "
            "nor whether RUMBLE or Equalizer APO owns config.txt.", theme))

        self._checks: dict[str, QCheckBox] = {}
        for group in settings_reset.RESET_GROUPS:
            # Escape the label: Qt reads a lone '&' as a mnemonic marker, so
            # "Startup & warnings" would render as "Startup _warnings". The
            # group labels are plain prose owned by the (Qt-free) core, so the
            # escaping belongs here rather than in them.
            check = QCheckBox(group.label.replace("&", "&&"))
            check.toggled.connect(self._update_reset_button)
            describe(check, f"Reset {group.label.lower()}", group.description)
            layout.addWidget(check)

            detail = QLabel(group.description)
            detail.setProperty("class", "muted")
            detail.setWordWrap(True)
            # Indent under the checkbox's label, past the indicator.
            detail.setContentsMargins(dp(24, 20), 0, 0, dp(4, 2))
            layout.addWidget(detail)

            self._checks[group.key] = check

        layout.addStretch(1)

        row = QHBoxLayout()
        self.btn_select_all = QPushButton(" Select All")
        self.btn_select_all.setProperty("class", "ghost")
        self.btn_select_all.clicked.connect(self._on_select_all)
        describe(self.btn_select_all, "Select all",
                 "Tick every group, so a reset restores every setting that can "
                 "be reset.")
        row.addWidget(self.btn_select_all)
        row.addStretch(1)
        btn_cancel = QPushButton(" Cancel")
        btn_cancel.clicked.connect(self.reject)
        describe(btn_cancel, "Cancel", "Close without resetting anything.")
        row.addWidget(btn_cancel)
        self.btn_reset = QPushButton(" Reset Selected")
        self.btn_reset.setProperty("class", "danger")
        self.btn_reset.clicked.connect(self.accept)
        describe(self.btn_reset, "Reset selected",
                 "Put the ticked groups back to their default values. This "
                 "cannot be undone.")
        row.addWidget(self.btn_reset)
        layout.addLayout(row)

        self._update_reset_button()

    def _on_select_all(self) -> None:
        for check in self._checks.values():
            check.setChecked(True)

    def _update_reset_button(self) -> None:
        self.btn_reset.setEnabled(bool(self.selected_groups()))

    def selected_groups(self) -> list[str]:
        """The ResetGroup.key values the user ticked, in RESET_GROUPS order."""
        return [g.key for g in settings_reset.RESET_GROUPS
                if self._checks[g.key].isChecked()]


# Fixed dialog size -- height matches the main window's own fixed compact
# height exactly (window_fixed_size() in main_window.py), so Settings never
# dwarfs the app it belongs to. Not computed from any page's sizeHint():
# that was tried and reverted, since sizeHint() on a widget that has never
# been laid out at its real on-screen width is unreliable for word-wrapped
# captions -- real (proportional) fonts wrap very differently at slightly
# different widths than this project's offscreen test harness (no real
# fonts) can reproduce. Every page scrolls inside this fixed frame instead
# (see _scrollable and Theme Creator's own internal scroll area) -- none of
# them resize the dialog, ever.
_FIXED_WIDTH = STANDARD_DIALOG_WIDTH
_FIXED_HEIGHT = STANDARD_DIALOG_HEIGHT


class SettingsDialog(FramelessDialog):
    """Grouped-nav + stacked-pages settings window (resizable).

    Every control applies and persists itself the moment it changes: there is
    no Save button and no Cancel/revert baseline. Changing a control snapshots
    the whole page set into the working dict and emits settings_saved (via a
    short debounce, see APPLY_DEBOUNCE_MS), which the main window applies to
    the live render chain -- so a toggle is audible/visible straight away."""

    theme_preview_requested = Signal(str)   # applied live as the theme is picked
    settings_saved = Signal(dict)           # emitted on every change, not just at close
    custom_theme_saved = Signal(str)        # emitted when a new theme is saved from the creator
    backup_requested = Signal()             # Backup Library button — handled by the main window
    restore_requested = Signal()            # Restore From Backup button — handled by the main window
    drift_restore_requested = Signal()      # Restore Drift Snapshot button — handled by the main window
    profile_trash_requested = Signal()      # Deleted Profiles button — handled by the main window
    calibrate_loudness_requested = Signal()  # ISO 226 "Calibrate at Current Volume" — main window
    import_apo_requested = Signal()         # Import APO configs button — handled by the main window
    release_apo_requested = Signal()        # Hand back to APO button — handled by the main window
    auto_switch_toggled = Signal(bool)      # Auto-Switch on/off — starts/stops the watcher adapter
    auto_switch_rules_requested = Signal()  # Edit Rules… button — handled by the main window
    abx_requested = Signal()                # Launch Blind A/B/X — handled by the main window
    hearing_test_requested = Signal()       # Launch Ear Calibration — handled by the main window
    play_reference_sound_requested = Signal(str)  # Ear Calibration "check your volume" sample buttons — main window plays hearing_test.REFERENCE_SOUNDS[key] (or "" for the bracketed voice clip)
    insert_crossfeed_requested = Signal()   # "Insert into profile…" — handled by the main window

    # Custom exec() result: an interface-size button was clicked, so the whole
    # UI (this dialog included) must be rebuilt at the new size right now. The
    # caller applies the size keys and reopens the dialog from carry_state().
    APPLY_SIZE_NOW = 2

    # Custom exec() result: the user reset settings, so this dialog closed
    # itself. It must NOT stay open across a reset -- every page seeded its
    # controls from the pre-reset values, and the next control touched would
    # _collect() that whole stale snapshot straight back over the reset. The
    # caller reads reset_groups(), applies the reset, and reopens a fresh
    # dialog.
    RESET_NOW = 3

    def __init__(self, settings: dict, theme: dict, data_dir: Path, parent: QWidget | None = None,
                 restore: dict | None = None, active_profile_is_stereo: bool = True,
                 apo_render_devices: list[audio_devices.ApoDevice] | None = None,
                 apo_capture_devices: list[audio_devices.ApoDevice] | None = None,
                 device_profile_options: list[tuple[str, str]] | None = None,
                 initial_category: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("RUMBLE Settings")
        # Fixed, not resizable -- every page scrolls inside it (see
        # _scrollable) rather than growing or shrinking the window to fit.
        # Height grows by the chrome strip FramelessDialog adds above
        # self.body, so page content keeps its original vertical budget.
        self.setFixedSize(_FIXED_WIDTH, _FIXED_HEIGHT + CHROME_STRIP_HEIGHT)

        self._theme = theme
        self._data_dir = data_dir
        # Whether the active profile's channel layout exposes an L and an R
        # for crossfeed to work on -- crossfeed.can_crossfeed(), computed by
        # the caller against the manager's active profile since this dialog
        # otherwise has no profile-config access. Gates the crossfeed card on
        # the Enhancements page; recomputed fresh every time the dialog is
        # (re)opened. True for essentially every playback profile (including
        # Channel:-less AutoEQ/convolution ones and multichannel layouts) --
        # only a layout with no L/R at all disables the card.
        self._active_profile_is_stereo = active_profile_is_stereo
        # D1b: APO-hooked RENDER endpoints (name/connection/active-state) and
        # (profile_id, name) choices for the Devices page's per-endpoint
        # combos -- both computed by the caller (main_window has the
        # ProfileManager this dialog otherwise has no access to), same
        # pattern as active_profile_is_stereo above. D2 adds the CAPTURE
        # (microphone) counterpart, same computed-by-caller pattern.
        self._apo_render_devices = list(apo_render_devices or [])
        self._apo_capture_devices = list(apo_capture_devices or [])
        self._device_profile_options = list(device_profile_options or [])
        # `restore` carries a previous dialog's full state across the
        # in-process UI rebuild that an instant size change triggers, so the
        # reopened dialog looks untouched.
        state = restore or {}
        self._working = dict(state.get("working", settings))
        # Set only when closing with RESET_NOW; read by the caller afterwards.
        self._reset_groups: list[str] = []
        # Set while the pages below are being built: seeding a control with
        # setChecked/setValue fires the same change signal a user click does,
        # and applying settings mid-construction would read controls that do
        # not exist yet. Reset in the try/finally around the page build.
        self._loading = True
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(APPLY_DEBOUNCE_MS)
        self._apply_timer.timeout.connect(self._apply_now)

        root = QHBoxLayout(self.body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_nav())

        # No Save button: the chrome strip's close button is the only close
        # control (there is nothing to Save -- see done()'s debounce flush,
        # which fires on that path too), so the stack gets the whole right side.
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)

        right_container = QWidget()
        right_container.setLayout(right)
        root.addWidget(right_container, 1)

        try:
            # Order must match CATEGORIES, i.e. NAV_GROUPS read top to bottom.
            self.stack.addWidget(_scrollable(self._build_listeners_page()))
            self.stack.addWidget(_scrollable(self._build_ear_calibration_page()))
            self.stack.addWidget(_scrollable(self._build_enhancements_page()))
            self.stack.addWidget(_scrollable(self._build_library_page()))
            self.stack.addWidget(_scrollable(self._build_corrections_page()))
            self._devices_page_index = self.stack.count()
            self.stack.addWidget(_scrollable(self._build_devices_page()))
            self.stack.addWidget(_scrollable(self._build_auto_switch_page()))
            self.stack.addWidget(_scrollable(self._build_apo_page()))
            self.stack.addWidget(_scrollable(self._build_appearance_page()))
            # Theme Creator scrolls its own colour-token editor and keeps its
            # name row and buttons pinned -- wrapping it too would nest two
            # scrollbars.
            self.stack.addWidget(self._build_theme_creator_page())
            self.stack.addWidget(_scrollable(self._build_startup_page()))
            self.stack.addWidget(_scrollable(self._build_backup_page()))

            tc_state = state.get("theme_creator")
            if tc_state:
                self._tc_restore(tc_state)
        finally:
            self._loading = False
        if initial_category is not None and "category" not in state:
            self._select_page(CATEGORIES.index(initial_category))
        else:
            self._select_page(state.get("category", 0))

    # ── Nav rail ─────────────────────────────────────────────────────

    def _build_nav(self) -> QWidget:
        """The category rail: four group headers, each over its own pages.

        Headers are real rows rather than a separate widget stack, so one
        QListWidget still owns keyboard navigation. They carry NoItemFlags,
        which makes them unselectable by click and skipped by the arrow keys.
        """
        t = self._theme
        self.category_list = QListWidget()
        self.category_list.setFixedWidth(dp(186, 154))
        # accent_muted is a #RRGGBBAA token and Qt's stylesheet parser reads
        # 8-digit hex as #AARRGGBB -- straight in, the selection fill comes out
        # a different colour entirely (green, in the default theme). Every
        # 8-digit token in inline QSS goes through qss_color().
        self.category_list.setStyleSheet(
            f"QListWidget {{ background-color: {t['bg_deep']}; border: none; "
            f"border-right: 1px solid {t['border_subtle']}; padding: {dp(8, 6)}px 0; }}"
            f"QListWidget::item {{ padding: {dp(6, 4)}px {dp(10, 8)}px; "
            f"margin: 1px {dp(8, 6)}px; border-radius: {dp(4, 3)}px; "
            f"border-left: 2px solid transparent; color: {t['text_primary']}; }}"
            # Group headers are the only disabled rows (NoItemFlags), and a
            # label must not outrank the things it labels.
            f"QListWidget::item:disabled {{ color: {t['text_muted']}; "
            f"background: transparent; }}"
            f"QListWidget::item:hover {{ background-color: {t['bg_raised']}; "
            f"color: {t['text_primary']}; }}"
            f"QListWidget::item:selected {{ "
            f"background-color: {qss_color(t['accent_muted'])}; "
            f"border-left: 2px solid {t['accent']}; color: {t['text_primary']}; }}"
        )
        describe(self.category_list, "Settings categories",
                 "The settings sections, grouped by what they change — select "
                 "one to show its options.")

        # nav row -> stack index, and back. Group headers occupy rows too, so
        # a row is not a page index and neither map can be dropped.
        self._nav_row_to_page: dict[int, int] = {}
        self._page_to_nav_row: dict[int, int] = {}

        page_index = 0
        for group_index, (group_label, names) in enumerate(NAV_GROUPS):
            header = QListWidgetItem(group_label.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = header.font()
            font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
            font.setBold(True)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
            header.setFont(font)
            # Extra height on every group but the first buys the air that
            # separates one group from the one above it.
            header.setSizeHint(QSize(0, dp(34 if group_index else 22, 28 if group_index else 18)))
            self.category_list.addItem(header)

            for name in names:
                item = QListWidgetItem(name)
                item.setSizeHint(QSize(0, dp(28, 24)))
                self.category_list.addItem(item)
                row = self.category_list.row(item)
                self._nav_row_to_page[row] = page_index
                self._page_to_nav_row[page_index] = row
                page_index += 1

        self.category_list.currentRowChanged.connect(self._on_nav_row_changed)
        return self.category_list

    def _select_page(self, page_index: int) -> None:
        """Show a page by its CATEGORIES/stack index, and select its nav row."""
        if page_index not in self._page_to_nav_row:
            page_index = 0
        self.category_list.setCurrentRow(self._page_to_nav_row[page_index])

    def _on_nav_row_changed(self, row: int) -> None:
        page_index = self._nav_row_to_page.get(row)
        if page_index is not None:
            self.stack.setCurrentIndex(page_index)

    # ── Listeners ────────────────────────────────────────────────────

    def _build_listeners_page(self) -> QWidget:
        page, layout = _page(
            "Listeners",
            "Who is listening. Each slot keeps its own name and its own Ear "
            "Calibration data, set up on the Ear Calibration page.")

        slots = _Card("Listener slots",
                      "Up to 4 people can each keep their own Ear Calibration "
                      "data. Whoever is selected gets a gentle, automatic "
                      "correction on top of whatever profile is playing — the "
                      "profile itself is never changed.")
        self._listener_name_edits: dict[int, QLineEdit] = {}
        names = listener_slots.load_slot_names(self._data_dir)
        for slot_id in listener_slots.SLOT_IDS:
            row = QHBoxLayout()
            label = QLabel(f"Slot {slot_id}:")
            label.setFixedWidth(dp(60, 50))
            edit = QLineEdit(names.get(slot_id, ""))
            edit.setPlaceholderText(f"Listener {slot_id}")
            edit.textChanged.connect(
                lambda text, sid=slot_id: self._on_listener_name_edited(sid, text))
            describe(edit, f"Listener {slot_id} name",
                     "A display name for this listener slot; leave blank to "
                     "use the default label.")
            self._listener_name_edits[slot_id] = edit
            row.addWidget(label)
            row.addWidget(edit)
            slots.add_layout(row)

        self.combo_default_listener = QComboBox()
        for slot_id in listener_slots.SLOT_IDS:
            self.combo_default_listener.addItem(names.get(slot_id) or f"Listener {slot_id}", slot_id)
        current_default = int(self._working.get("default_listener_slot", 1))
        idx = self.combo_default_listener.findData(current_default)
        self.combo_default_listener.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_default_listener.currentIndexChanged.connect(self._schedule_apply)
        describe(self.combo_default_listener, "Default Listener Slot",
                 "Which listener slot RUMBLE starts with on every "
                 "launch, regardless of what was last selected.")
        slots.add_row("Selected on every launch:", self.combo_default_listener)
        layout.addWidget(slots)

        layout.addStretch(1)
        return page

    def _on_listener_name_edited(self, slot_id: int, text: str) -> None:
        idx = self.combo_default_listener.findData(slot_id)
        if idx >= 0:
            # setItemText does not emit currentIndexChanged, so this cannot
            # re-enter through the combo's own _schedule_apply connection.
            self.combo_default_listener.setItemText(idx, text.strip() or f"Listener {slot_id}")
        self._schedule_apply()

    # ── Ear Calibration ─────────────────────────────────────────────────

    def _build_ear_calibration_page(self) -> QWidget:
        page, layout = _page(
            "Ear Calibration",
            "An informal per-ear frequency check on your current hardware "
            "— not a medical evaluation. Results are saved per Listener slot.")

        correction = _Card("Ear correction",
                           "Measure each ear, then let RUMBLE lift only the "
                           "weaker one back into balance.")
        self.chk_listener_correction = QCheckBox("Apply the active listener's ear correction")
        self.chk_listener_correction.setChecked(
            bool(self._working.get("listener_correction_enabled", True)))
        self.chk_listener_correction.toggled.connect(self._schedule_apply)
        describe(self.chk_listener_correction, "Apply ear correction",
                 "When on, the active listener's saved Ear Calibration "
                 "results add a gentle weak-ear correction (capped at +6 dB) "
                 "on top of whatever profile is playing. Turn off to hear "
                 "profiles uncorrected -- the saved results are kept, and "
                 "nothing is measured again when you turn it back on.")
        correction.add(self.chk_listener_correction)

        self.btn_hearing_test_launch = QPushButton(" Run Ear Calibration…")
        self.btn_hearing_test_launch.setProperty("class", "primary")
        self.btn_hearing_test_launch.clicked.connect(self.hearing_test_requested)
        describe(self.btn_hearing_test_launch, "Run Ear Calibration",
                 "Run an informal per-ear frequency calibration and get a "
                 "downloadable summary — not a medical evaluation.")
        run_row = QHBoxLayout()
        run_row.addWidget(self.btn_hearing_test_launch)
        run_row.addStretch(1)
        correction.add_layout(run_row)
        layout.addWidget(correction)

        # A1 (adaptive-frontend follow-up): the same reference sounds the
        # Ear Calibration volume-setting step uses, playable on demand from
        # here -- so a listener who changed their volume since last
        # calibrating can re-check it in one click, without opening the
        # full test dialog/staircase.
        volume_check = _Card(
            "Check your volume",
            "The same reference sounds Ear Calibration uses to help you set "
            "a good volume — play any of these any time to re-check, "
            "without starting the test.")
        self.btn_play_reference_clip = QPushButton("Play Full Reference Voice")
        self.btn_play_reference_clip.clicked.connect(
            lambda: self.play_reference_sound_requested.emit(""))
        describe(self.btn_play_reference_clip, "Play full reference voice",
                 "Play the bracketed voice clip Ear Calibration uses: "
                 "quieter, at the target level, then louder.")
        volume_check.add(self.btn_play_reference_clip)

        sounds_grid = QGridLayout()
        sounds_grid.setSpacing(dp(8, 6))
        self._btn_reference_sounds: dict[str, QPushButton] = {}
        for i, (key, label) in enumerate(hearing_test.REFERENCE_SOUNDS):
            btn = QPushButton(label)
            btn.setProperty("class", "ghost")
            btn.clicked.connect(
                lambda _checked=False, k=key: self.play_reference_sound_requested.emit(k))
            describe(btn, f"Play {label.lower()}",
                     f"Play a short {label.lower()} sample, level-matched to "
                     "the reference voice's target segment — should sound "
                     "roughly as loud as it once your volume matches.")
            self._btn_reference_sounds[key] = btn
            sounds_grid.addWidget(btn, i // 2, i % 2)
        volume_check.add_layout(sounds_grid)
        layout.addWidget(volume_check)

        layout.addStretch(1)
        return page

    # ── Enhancements ─────────────────────────────────────────────────

    def _build_enhancements_page(self) -> QWidget:
        """Every render layer that sits on top of the active profile.

        These three used to be scattered down the old Listeners page. They
        belong together and nowhere else: each is a machine/session setting
        that changes what you hear without changing the profile, so none of
        them travels with a profile you export.
        """
        page, layout = _page(
            "Enhancements",
            "Extra processing layered on top of whatever profile is playing. "
            "These are machine settings — none of them is saved into a "
            "profile, and none changes one.")

        # ISO 226
        loudness = _Card(
            "Loudness compensation (ISO 226)",
            "A real ISO 226:2003 equal-loudness-contour correction, tied to "
            "your current volume — independent of any profile or listener. "
            "Calibrate once at your normal listening volume; turning the "
            "volume down from there gently restores bass/treble balance, the "
            "way your ears would need it to sound the same as at that "
            "reference level.")
        self.chk_iso226_enabled = QCheckBox("Enable loudness compensation")
        self.chk_iso226_enabled.setChecked(bool(self._working.get("iso226_loudness_enabled", False)))
        self.chk_iso226_enabled.toggled.connect(self._schedule_apply)
        describe(self.chk_iso226_enabled, "Enable loudness compensation",
                 "Apply a volume-linked ISO 226 equal-loudness compensation "
                 "curve on top of whatever profile is playing.")
        loudness.add(self.chk_iso226_enabled)

        # The stored number is always phon (that's what iso226.py's equal-
        # loudness maths takes) -- but ISO 226:2003 defines phon as
        # identical to dB SPL at 1 kHz, so this app already treats the two
        # as the same number everywhere (see loudness_layer.
        # REFERENCE_CLIP_TARGET_PHON's own comment). The unit combo below
        # only changes which of those two equivalent, equally-correct
        # labels is shown -- it never converts or rescales the value. "dB"
        # on its own is deliberately NOT offered here: that would be
        # confusable with the DIFFERENT quantity shown next to the
        # Calibrate button below (iso226_reference_volume_db, a raw WASAPI
        # volume-knob position, not a loudness level).
        self.spin_iso226_phon = QDoubleSpinBox()
        self.spin_iso226_phon.setRange(iso226.MIN_PHON, iso226.MAX_PHON)
        self.spin_iso226_phon.setSingleStep(1.0)
        self.spin_iso226_phon.setDecimals(0)
        self.spin_iso226_phon.setSuffix(" phon")
        self.spin_iso226_phon.setValue(float(self._working.get("iso226_reference_phon", 80.0)))
        self.spin_iso226_phon.valueChanged.connect(self._schedule_apply)

        self.combo_iso226_unit = QComboBox()
        self.combo_iso226_unit.addItem("phon", " phon")
        self.combo_iso226_unit.addItem("dB SPL", " dB SPL")
        self.combo_iso226_unit.currentIndexChanged.connect(self._refresh_iso226_unit_label)
        describe(self.combo_iso226_unit, "Reference level unit",
                 "Purely a display choice -- phon and dB SPL are the same "
                 "number here (ISO 226:2003 defines phon as equal to dB "
                 "SPL at 1 kHz), so switching this never changes the "
                 "stored value, only which term is shown.")
        self._refresh_iso226_unit_label()
        loudness.add_row("Reference listening level:", self.spin_iso226_phon, self.combo_iso226_unit)

        calib_row = QHBoxLayout()
        self.btn_iso226_calibrate = QPushButton("Use Current Volume as Reference")
        self.btn_iso226_calibrate.clicked.connect(self.calibrate_loudness_requested)
        describe(self.btn_iso226_calibrate, "Use current volume as reference",
                 "Reads your system's master (WASAPI) volume right now and "
                 "stores it as the anchor point: the compensation layer "
                 "will treat THIS volume as corresponding to the Reference "
                 "listening level above (e.g. 80 phon / 80 dB SPL). Set "
                 "your volume to wherever you actually listen first, then "
                 "click this -- turning the volume down from here is what "
                 "triggers bass/treble compensation; turning it up doesn't "
                 "need to, since louder already sounds more tonally flat "
                 "on its own. Re-run this any time your normal listening "
                 "volume changes. This reads your live system volume "
                 "directly; it does not use the Ear Calibration reference "
                 "clip (Settings → Ear Calibration has its own, separate "
                 "way to set this from that clip instead).")
        self.lbl_iso226_reference = QLabel()
        self._refresh_iso226_reference_label()
        calib_row.addWidget(self.btn_iso226_calibrate)
        calib_row.addWidget(self.lbl_iso226_reference)
        calib_row.addStretch(1)
        loudness.add_layout(calib_row)
        layout.addWidget(loudness)

        # Crossfeed
        cf = _Card(
            "Crossfeed (headphones)",
            "Attenuated, delayed, low-pass cross-mix so hard-panned headphone "
            "content stops sounding like it's inside your skull.")
        self.chk_crossfeed_enabled = QCheckBox("Enable crossfeed")
        self.chk_crossfeed_enabled.setChecked(bool(self._working.get("crossfeed_enabled", False)))
        self.chk_crossfeed_enabled.toggled.connect(self._schedule_apply)
        describe(self.chk_crossfeed_enabled, "Enable crossfeed",
                 "Mix a low-passed, attenuated, delayed copy of each channel "
                 "into the other so hard-panned headphone content sounds "
                 "more natural. Only takes effect on a plain stereo profile.")
        cf.add(self.chk_crossfeed_enabled)

        def _hideable_row(label_text: str, *widgets: QWidget) -> list[QWidget]:
            """A labelled control row, like _Card.add_row(), but keeping
            handles to every widget in it (including the label) so a mode
            switch can show/hide the whole row at once."""
            row = QHBoxLayout()
            label = QLabel(label_text)
            row.addWidget(label)
            for widget in widgets:
                row.addWidget(widget)
            row.addStretch(1)
            cf.add_layout(row)
            return [label, *widgets]

        self.combo_crossfeed_preset = QComboBox()
        for key, preset in crossfeed.PRESETS.items():
            self.combo_crossfeed_preset.addItem(preset.label, key)
        self.combo_crossfeed_preset.addItem("Custom", "custom")
        self.combo_crossfeed_preset.addItem("Soundstage Blend", "soundstage")
        current_crossfeed_preset = str(
            self._working.get("crossfeed_preset", crossfeed.DEFAULT_PRESET))
        preset_idx = self.combo_crossfeed_preset.findData(current_crossfeed_preset)
        self.combo_crossfeed_preset.setCurrentIndex(preset_idx if preset_idx >= 0 else 0)
        self.combo_crossfeed_preset.currentIndexChanged.connect(self._schedule_apply)
        self.combo_crossfeed_preset.currentIndexChanged.connect(
            self._update_crossfeed_mode_visibility)
        describe(self.combo_crossfeed_preset, "Crossfeed preset",
                 "Which crossfeed to model: Chu Moy, Jan Meier, Bauer (bs2b's "
                 "own default), a Linkwitz-style design with an explicit "
                 "interaural delay, an unfiltered Direct Blend, your own "
                 "Custom gain/cutoff/delay, or a Soundstage Blend that widens "
                 "or narrows the stereo image directly.")
        cf.add_row("Preset:", self.combo_crossfeed_preset)

        strength_row = QHBoxLayout()
        self.lbl_crossfeed_strength_caption = QLabel("Strength:")
        strength_row.addWidget(self.lbl_crossfeed_strength_caption)
        self.slider_crossfeed_strength = QSlider(Qt.Orientation.Horizontal)
        self.slider_crossfeed_strength.setRange(0, 100)
        self.slider_crossfeed_strength.setValue(
            int(self._working.get("crossfeed_strength", 100)))
        self.slider_crossfeed_strength.valueChanged.connect(
            self._update_crossfeed_strength_label)
        self.slider_crossfeed_strength.valueChanged.connect(self._schedule_apply)
        describe(self.slider_crossfeed_strength, "Crossfeed strength",
                 "Scales the preset's cross-channel attenuation from 0% (no "
                 "effect) up to 100% (the preset's full published figure), "
                 "computed in the linear/voltage domain.")
        strength_row.addWidget(self.slider_crossfeed_strength, 1)
        self.lbl_crossfeed_strength = QLabel()
        self.lbl_crossfeed_strength.setFixedWidth(48)
        self.lbl_crossfeed_strength.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_crossfeed_strength_label(self.slider_crossfeed_strength.value())
        strength_row.addWidget(self.lbl_crossfeed_strength)
        cf.add_layout(strength_row)
        self._crossfeed_strength_widgets = (
            self.lbl_crossfeed_strength_caption, self.slider_crossfeed_strength,
            self.lbl_crossfeed_strength,
        )

        # Custom mode: exact gain/cutoff/delay instead of a named circuit.
        self.spin_crossfeed_custom_gain = QDoubleSpinBox()
        self.spin_crossfeed_custom_gain.setRange(-20.0, 0.0)
        self.spin_crossfeed_custom_gain.setSingleStep(0.5)
        self.spin_crossfeed_custom_gain.setDecimals(1)
        self.spin_crossfeed_custom_gain.setSuffix(" dB")
        self.spin_crossfeed_custom_gain.setValue(
            float(self._working.get("crossfeed_custom_gain_db", -6.0)))
        self.spin_crossfeed_custom_gain.valueChanged.connect(self._schedule_apply)
        describe(self.spin_crossfeed_custom_gain, "Custom crossfeed gain",
                 "Cross-channel attenuation for the Custom preset.")
        custom_widgets = list(_hideable_row("Gain:", self.spin_crossfeed_custom_gain))

        self.spin_crossfeed_custom_freq = QDoubleSpinBox()
        self.spin_crossfeed_custom_freq.setRange(200.0, 2000.0)
        self.spin_crossfeed_custom_freq.setSingleStep(10.0)
        self.spin_crossfeed_custom_freq.setDecimals(0)
        self.spin_crossfeed_custom_freq.setSuffix(" Hz")
        self.spin_crossfeed_custom_freq.setValue(
            float(self._working.get("crossfeed_custom_freq_hz", 700.0)))
        self.spin_crossfeed_custom_freq.valueChanged.connect(self._schedule_apply)
        describe(self.spin_crossfeed_custom_freq, "Custom crossfeed cutoff",
                 "Low-pass corner for the Custom preset's cross-path.")
        custom_widgets += _hideable_row("Cutoff:", self.spin_crossfeed_custom_freq)

        self.spin_crossfeed_custom_delay = QDoubleSpinBox()
        self.spin_crossfeed_custom_delay.setRange(0.0, 690.0)
        self.spin_crossfeed_custom_delay.setSingleStep(10.0)
        self.spin_crossfeed_custom_delay.setDecimals(0)
        self.spin_crossfeed_custom_delay.setSuffix(" us")
        self.spin_crossfeed_custom_delay.setValue(
            float(self._working.get("crossfeed_custom_delay_us", 300.0)))
        self.spin_crossfeed_custom_delay.valueChanged.connect(self._schedule_apply)
        describe(self.spin_crossfeed_custom_delay, "Custom crossfeed delay",
                 "Interaural delay for the Custom preset's cross-path. "
                 "Capped at 690 us, the Woodworth-formula interaural "
                 "time-difference ceiling for an average adult head.")
        custom_widgets += _hideable_row("Delay:", self.spin_crossfeed_custom_delay)
        self._crossfeed_custom_widgets = tuple(custom_widgets)

        # Soundstage Blend mode: two independent axes instead of Strength.
        self.slider_crossfeed_soundstage_blend = QSlider(Qt.Orientation.Horizontal)
        self.slider_crossfeed_soundstage_blend.setRange(0, 100)
        self.slider_crossfeed_soundstage_blend.setValue(
            int(self._working.get("crossfeed_soundstage_blend", 20)))
        self.slider_crossfeed_soundstage_blend.valueChanged.connect(
            self._update_crossfeed_soundstage_blend_label)
        self.slider_crossfeed_soundstage_blend.valueChanged.connect(self._schedule_apply)
        describe(self.slider_crossfeed_soundstage_blend, "Soundstage Blend amount",
                 "How strongly the mid-side low-shelf blend narrows the "
                 "stereo image, from 0% (no effect) to 100%.")
        self.lbl_crossfeed_soundstage_blend = QLabel()
        self.lbl_crossfeed_soundstage_blend.setFixedWidth(48)
        self.lbl_crossfeed_soundstage_blend.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_crossfeed_soundstage_blend_label(
            self.slider_crossfeed_soundstage_blend.value())
        soundstage_widgets = list(_hideable_row(
            "Blend:", self.slider_crossfeed_soundstage_blend,
            self.lbl_crossfeed_soundstage_blend))

        self.slider_crossfeed_soundstage_width = QSlider(Qt.Orientation.Horizontal)
        self.slider_crossfeed_soundstage_width.setRange(0, 100)
        self.slider_crossfeed_soundstage_width.setValue(
            int(self._working.get("crossfeed_soundstage_width", 50)))
        self.slider_crossfeed_soundstage_width.valueChanged.connect(
            self._update_crossfeed_soundstage_width_label)
        self.slider_crossfeed_soundstage_width.valueChanged.connect(self._schedule_apply)
        describe(self.slider_crossfeed_soundstage_width, "Soundstage Blend width",
                 "Soundstage wideness for the Blend mode -- higher values "
                 "push the low-shelf cutoff lower, widening the perceived "
                 "stage.")
        self.lbl_crossfeed_soundstage_width = QLabel()
        self.lbl_crossfeed_soundstage_width.setFixedWidth(48)
        self.lbl_crossfeed_soundstage_width.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_crossfeed_soundstage_width_label(
            self.slider_crossfeed_soundstage_width.value())
        soundstage_widgets += _hideable_row(
            "Width:", self.slider_crossfeed_soundstage_width,
            self.lbl_crossfeed_soundstage_width)
        self._crossfeed_soundstage_widgets = tuple(soundstage_widgets)

        self.btn_insert_crossfeed = QPushButton("Insert into profile…")
        self.btn_insert_crossfeed.clicked.connect(self.insert_crossfeed_requested)
        describe(self.btn_insert_crossfeed, "Insert crossfeed into profile",
                 "Write the current crossfeed preset and strength into the "
                 "active profile as real, portable config text, then turn "
                 "off this machine-level setting so it isn't applied twice. "
                 "Refused while there are unsaved editor changes.")
        insert_row = QHBoxLayout()
        insert_row.addWidget(self.btn_insert_crossfeed)
        insert_row.addStretch(1)
        cf.add_layout(insert_row)

        self._crossfeed_controls = (
            self.chk_crossfeed_enabled, self.combo_crossfeed_preset,
            self.slider_crossfeed_strength, self.btn_insert_crossfeed,
            self.spin_crossfeed_custom_gain, self.spin_crossfeed_custom_freq,
            self.spin_crossfeed_custom_delay,
            self.slider_crossfeed_soundstage_blend,
            self.slider_crossfeed_soundstage_width,
        )
        self.lbl_crossfeed_nonstereo = QLabel(
            "The active profile has no left/right channels for crossfeed to "
            "work on, so it has no effect until you switch to one that does "
            "-- the setting is kept, not lost.")
        self.lbl_crossfeed_nonstereo.setProperty("class", "muted")
        self.lbl_crossfeed_nonstereo.setWordWrap(True)
        cf.add(self.lbl_crossfeed_nonstereo)
        self._apply_crossfeed_stereo_gate(self._active_profile_is_stereo)
        self._update_crossfeed_mode_visibility()
        layout.addWidget(cf)

        # Loudness matching
        matching = _Card(
            "Profile-switch loudness matching",
            "Switching to a louder profile (or turning bypass off) can make it "
            "sound better just because it's louder, not because the EQ is "
            "actually better. When on, RUMBLE quietly pulls hot profiles down "
            "to match how loud your content sounds with EQ off — never boosts "
            "quiet ones.")
        self.chk_profile_loudness_matching = QCheckBox("Match loudness across profiles")
        self.chk_profile_loudness_matching.setChecked(
            bool(self._working.get("profile_loudness_matching", False)))
        self.chk_profile_loudness_matching.toggled.connect(self._schedule_apply)
        describe(self.chk_profile_loudness_matching, "Match loudness across profiles",
                 "When on, an extra render layer reduces (never boosts) each "
                 "profile's estimated loudness to match a flat, EQ-off "
                 "config, so switching profiles or toggling bypass never "
                 "jumps in volume.")
        matching.add(self.chk_profile_loudness_matching)
        layout.addWidget(matching)

        layout.addStretch(1)
        return page

    def _update_crossfeed_strength_label(self, value: int) -> None:
        self.lbl_crossfeed_strength.setText(f"{value}%")

    def _update_crossfeed_soundstage_blend_label(self, value: int) -> None:
        self.lbl_crossfeed_soundstage_blend.setText(f"{value}%")

    def _update_crossfeed_soundstage_width_label(self, value: int) -> None:
        self.lbl_crossfeed_soundstage_width.setText(f"{value}%")

    def _update_crossfeed_mode_visibility(self, *_args) -> None:
        """Strength applies to every named PRESETS entry; Custom and
        Soundstage Blend are settings-driven with their own controls
        instead (crossfeed.render_block()'s dispatch) -- show only the row
        group that matches whichever preset is currently selected."""
        mode = str(self.combo_crossfeed_preset.currentData() or crossfeed.DEFAULT_PRESET)
        for widget in self._crossfeed_strength_widgets:
            widget.setVisible(mode not in ("custom", "soundstage"))
        for widget in self._crossfeed_custom_widgets:
            widget.setVisible(mode == "custom")
        for widget in self._crossfeed_soundstage_widgets:
            widget.setVisible(mode == "soundstage")

    def _apply_crossfeed_stereo_gate(self, is_stereo: bool) -> None:
        """Soft-disable the crossfeed controls (values kept, not reset) when
        the active profile's channel layout isn't plain stereo -- roadmap B2
        Decisions: crossfeed is out of scope on non-stereo layouts, same
        stance as listener correction."""
        for control in self._crossfeed_controls:
            control.setEnabled(is_stereo)
        self.lbl_crossfeed_nonstereo.setVisible(not is_stereo)

    def note_crossfeed_inserted(self) -> None:
        """Called by the main window after insert_crossfeed_requested is
        handled: the machine-level toggle is turned off (the block now
        lives in the profile's own text instead, avoiding a double-apply at
        render time), so reflect that here without needing the dialog
        reopened -- same pattern as note_loudness_reference_calibrated()."""
        self._working["crossfeed_enabled"] = False
        self.chk_crossfeed_enabled.setChecked(False)

    def _refresh_iso226_unit_label(self) -> None:
        """Swap the Reference listening level spin box's suffix (and its
        tooltip's unit wording) between "phon" and "dB SPL" -- a display
        choice only, see combo_iso226_unit's own describe() text; the
        stored iso226_reference_phon value never changes."""
        suffix = self.combo_iso226_unit.currentData()
        unit_label = self.combo_iso226_unit.currentText()
        self.spin_iso226_phon.setSuffix(suffix)
        describe(self.spin_iso226_phon, "Reference listening level",
                 f"The loudness level ({unit_label}) your content is "
                 "assumed to sound tonally flat at. 80 (phon or dB SPL -- "
                 "ISO 226:2003 defines them as the same number at 1 kHz) "
                 "is one of ISO 226:2003's own commonly-illustrated "
                 "reference contours.")

    def _refresh_iso226_reference_label(self) -> None:
        ref = self._working.get("iso226_reference_volume_db")
        self.lbl_iso226_reference.setText(
            f"Reference volume: {ref:.1f} dB" if ref is not None
            else "Not yet calibrated")

    def note_loudness_reference_calibrated(self, reference_volume_db: float) -> None:
        """Called by the main window after calibrate_loudness_requested is
        handled, so the dialog reflects the freshly-captured reference
        without needing to be reopened."""
        self._working["iso226_reference_volume_db"] = reference_volume_db
        self._refresh_iso226_reference_label()

    # ── Library ──────────────────────────────────────────────────────

    def _build_library_page(self) -> QWidget:
        """The profile library: what you see in it, what a new one starts as,
        and the two tools that act on saved profiles (compare, undelete)."""
        page, layout = _page(
            "Library",
            "Which profiles you see, what a new one starts as, and the tools "
            "that act on the ones you have saved.")
        # A touch tighter than _page()'s default: this page carries four
        # cards, more than most others, and the goal is fitting the fixed
        # dialog height without scrolling.
        layout.setSpacing(dp(8, 6))

        visible = _Card("Show in the library",
                        "Hiding a profile never deletes it — it stays on disk "
                        "and keeps working.")
        self.chk_show_builtin = QCheckBox("Built-in profiles")
        self.chk_show_builtin.setChecked(bool(self._working.get("show_builtin_profiles", True)))
        self.chk_show_builtin.toggled.connect(self._schedule_apply)
        describe(self.chk_show_builtin, "Show built-in profiles",
                 "Include RUMBLE's curated built-in profiles in the library. "
                 "Turn off to keep only your own profiles.")
        visible.add(self.chk_show_builtin)

        self.chk_show_microphone = QCheckBox("Microphone profiles")
        self.chk_show_microphone.setChecked(bool(self._working.get("show_microphone_profiles", False)))
        self.chk_show_microphone.toggled.connect(self._schedule_apply)
        self.chk_show_microphone.toggled.connect(self._on_show_microphone_toggled)
        describe(self.chk_show_microphone, "Show microphone profiles",
                 "Include capture-only (microphone) profiles in the library, "
                 "and let a microphone be assigned one on the Devices page. "
                 "Turn off to put microphone EQ away entirely: the profiles "
                 "stay on disk and any assignment is remembered, but no EQ is "
                 "applied to a microphone until you turn this back on. "
                 "Measuring with a calibration microphone is unaffected "
                 "either way.")
        visible.add(self.chk_show_microphone)
        layout.addWidget(visible)

        new_profiles = _Card("New profiles & editing",
                             "What a profile looks like the moment you create it, "
                             "and how RUMBLE keeps you from losing edits.")
        self.spin_default_bands = QSpinBox()
        self.spin_default_bands.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spin_default_bands.setRange(1, 32)
        self.spin_default_bands.setValue(int(self._working.get("default_bands", 10)))
        self.spin_default_bands.setFixedWidth(80)
        self.spin_default_bands.valueChanged.connect(self._schedule_apply)
        describe(self.spin_default_bands, "Default band count",
                 "How many flat filter bands a brand-new profile starts with (1–32).")
        new_profiles.add_row("Number of bands:", self.spin_default_bands)

        self.chk_auto_preamp = QCheckBox("Automatically calculate pre-amp on save")
        self.chk_auto_preamp.setChecked(bool(self._working.get("auto_preamp", True)))
        self.chk_auto_preamp.toggled.connect(self._schedule_apply)
        describe(self.chk_auto_preamp, "Auto pre-amp on save",
                 "Recalculate the anti-clipping pre-amp automatically every time "
                 "a profile is saved.")
        new_profiles.add(self.chk_auto_preamp)

        self.chk_warn_unsaved = QCheckBox("Warn about unsaved profile changes")
        self.chk_warn_unsaved.setChecked(bool(self._working.get("warn_unsaved_changes", True)))
        self.chk_warn_unsaved.toggled.connect(self._schedule_apply)
        describe(self.chk_warn_unsaved, "Warn about unsaved profile changes",
                 "Ask for confirmation before discarding unsaved editor "
                 "changes when switching or activating a different profile.")
        new_profiles.add(self.chk_warn_unsaved)
        layout.addWidget(new_profiles)

        abx = _Card("Compare two profiles",
                    "Run a blind listening test between two profiles to check "
                    "whether you can reliably tell them apart.")
        self.btn_abx_launch = QPushButton(" Launch Blind A/B/X Test…")
        self.btn_abx_launch.clicked.connect(self.abx_requested)
        describe(self.btn_abx_launch, "Launch Blind A/B/X test",
                 "Refused while there are unsaved editor changes or an active "
                 "live preview.")
        abx_row = QHBoxLayout()
        abx_row.addWidget(self.btn_abx_launch)
        abx_row.addStretch(1)
        abx.add_layout(abx_row)
        layout.addWidget(abx)

        trash = _Card("Deleted profiles",
                      "Deleting a profile moves it to a recycle bin instead of "
                      "erasing it — restore it or purge it for good here.")
        self.btn_profile_trash = QPushButton(" Deleted Profiles…")
        self.btn_profile_trash.clicked.connect(self.profile_trash_requested)
        describe(self.btn_profile_trash, "Deleted profiles",
                 "Browse deleted profiles and restore or permanently purge "
                 "them.")
        trash_row = QHBoxLayout()
        trash_row.addWidget(self.btn_profile_trash)
        trash_row.addStretch(1)
        trash.add_layout(trash_row)
        layout.addWidget(trash)

        layout.addStretch(1)
        return page

    # ── Corrections ──────────────────────────────────────────────────

    def _build_corrections_page(self) -> QWidget:
        """Defaults for generated corrections only. Every control here answers
        "how should RUMBLE turn a measurement into EQ bands" -- which is why
        none of them touch manual GEQ/PEQ editing."""
        page, layout = _page(
            "Corrections",
            "Defaults used when RUMBLE turns a headphone or speaker "
            "measurement into EQ bands. Bands you edit by hand are never "
            "affected.")

        smoothing = _Card("Smoothing",
                          "How closely a correction is allowed to chase every "
                          "wrinkle in a measurement.")
        self.spin_smoothing_oct = QDoubleSpinBox()
        self.spin_smoothing_oct.setRange(0.10, 1.00)
        self.spin_smoothing_oct.setSingleStep(0.05)
        self.spin_smoothing_oct.setDecimals(2)
        self.spin_smoothing_oct.setSuffix(" oct")
        self.spin_smoothing_oct.setValue(
            float(self._working.get("spinorama_smoothing_oct", 1 / 3)))
        self.spin_smoothing_oct.valueChanged.connect(self._schedule_apply)
        describe(self.spin_smoothing_oct, "Correction smoothing",
                 "How wide (in octaves) a smoothing window is applied to "
                 "speaker/headphone correction curves before EQ bands are "
                 "generated. Wider values produce gentler, less peaky "
                 "corrections.")
        smoothing.add_row("Window width:", self.spin_smoothing_oct)

        self.chk_adaptive_smoothing = QCheckBox("Adaptive high-frequency smoothing")
        self.chk_adaptive_smoothing.setChecked(
            bool(self._working.get("adaptive_smoothing", True)))
        self.chk_adaptive_smoothing.toggled.connect(self._schedule_apply)
        describe(self.chk_adaptive_smoothing, "Adaptive high-frequency smoothing",
                 "For auto-generated speaker corrections, widen smoothing and "
                 "taper boost above ~8 kHz toward zero -- AutoEQ's own "
                 "convention, since measurement-rig and room variance "
                 "dominates real device differences up there. Manual GEQ/PEQ "
                 "editing is never affected.")
        smoothing.add(self.chk_adaptive_smoothing)
        smoothing.add(_caption(
            "Above roughly 8 kHz, measurement rigs disagree more than the "
            "devices do, so adaptive smoothing widens the window and tapers "
            "boost toward zero up there."))
        layout.addWidget(smoothing)

        speakers = _Card("Speakers",
                         "Applied when you import a speaker measurement.")
        self.spin_default_tilt = QDoubleSpinBox()
        self.spin_default_tilt.setRange(-3.0, 1.0)
        self.spin_default_tilt.setSingleStep(0.1)
        self.spin_default_tilt.setDecimals(2)
        self.spin_default_tilt.setSuffix(" dB/oct")
        self.spin_default_tilt.setValue(
            float(self._working.get("spinorama_default_tilt", -0.9)))
        self.spin_default_tilt.valueChanged.connect(self._schedule_apply)
        describe(self.spin_default_tilt, "Default speaker target tilt",
                 "The room-preference tilt offered as the speaker correction "
                 "default when importing a measurement, in dB per octave. "
                 "Negative values roll off the top end for a warmer, typical "
                 "in-room preference.")
        speakers.add_row("Target tilt:", self.spin_default_tilt)
        speakers.add(_caption(
            "Negative tilt rolls the top end off for the warmer balance most "
            "rooms prefer."))

        self.chk_speaker_crossover = QCheckBox("Add subsonic high-pass on import")
        self.chk_speaker_crossover.setChecked(bool(self._working.get("spinorama_add_crossover", False)))
        self.chk_speaker_crossover.toggled.connect(self._schedule_apply)
        describe(self.chk_speaker_crossover, "Speaker crossover suggestion",
                 "When importing a single speaker measurement, add a high-pass filter "
                 "near its published low-frequency extension so correction isn't "
                 "applied below where the speaker can't reproduce it.")
        speakers.add(self.chk_speaker_crossover)

        self.chk_directivity_limiting = QCheckBox("Directivity-aware boost limiting")
        self.chk_directivity_limiting.setChecked(
            bool(self._working.get("spinorama_directivity_limiting", True)))
        self.chk_directivity_limiting.toggled.connect(self._schedule_apply)
        describe(self.chk_directivity_limiting, "Directivity-aware limiting",
                 "Damp correction boost for speakers whose published directivity "
                 "estimates show poor, narrow off-axis control — a speaker with "
                 "ragged off-axis behavior shouldn't get the same aggressive boost "
                 "as one with wide, consistent directivity.")
        speakers.add(self.chk_directivity_limiting)
        layout.addWidget(speakers)

        layout.addStretch(1)
        return page

    # ── D1b: Devices ──────────────────────────────────────────────────

    _FOLLOW_ACTIVE_LABEL = "Follow active profile"

    def _build_devices_page(self) -> QWidget:
        show_mic = bool(self._working.get("show_microphone_profiles", False))
        page, outer = _page(
            "Devices",
            ("Each Equalizer APO-hooked playback device or microphone can "
             "carry its own profile at once. " if show_mic else
             "Each Equalizer APO-hooked playback device can carry its own "
             "profile at once. ") +
            "Unassigned devices follow whatever profile is globally active "
            "— nothing changes here until you assign one.")

        self._device_assignment_combos: dict[str, QComboBox] = {}
        assignments = dict(self._working.get("device_profile_assignments") or {})
        # Capture GUIDs stay "known" even when the Capture tab is hidden
        # below: a hidden microphone is disabled, not unavailable, and
        # listing it under "Unavailable devices" would both misdescribe it
        # and offer a Clear button that throws the assignment away -- the
        # one thing hiding is meant NOT to do.
        known_guids = ({d.guid for d in self._apo_render_devices}
                       | {d.guid for d in self._apo_capture_devices})

        per_device = _Card(
            "Per-device profile",
            "An explicit assignment always wins over the global active profile "
            "on that device. Auto-Switch rules keep driving the GLOBAL active "
            "profile only — they never change a per-device assignment.")
        tabs = QTabWidget()
        tabs.addTab(
            self._build_device_flow_tab(
                self._apo_render_devices, assignments,
                "No playback devices are currently hooked by Equalizer APO."),
            "Playback")
        # Microphone EQ is off entirely while microphone profiles are
        # hidden (ProfileManager._valid_device_assignments() drops capture
        # branches from the dispatch to match), so the tab that assigns it
        # would be a control with no effect. Hidden rather than disabled:
        # the great majority of users never EQ a mic, and the Library
        # setting that governs this says so in its own description.
        if show_mic:
            tabs.addTab(
                self._build_capture_devices_tab(assignments), "Capture")
            tabs.setTabBarAutoHide(False)
        else:
            # One remaining tab: the bar is pure noise, so drop it.
            tabs.setTabBarAutoHide(True)
        per_device.add(tabs)
        outer.addWidget(per_device)

        stale_guids = [g for g in assignments if g not in known_guids]
        self._device_stale_rows: dict[str, QWidget] = {}
        if stale_guids:
            stale = _Card(
                "Unavailable devices",
                "These devices are no longer hooked by Equalizer APO (or "
                "aren't currently detected), but their assignment is kept — it "
                "applies again if the device reappears — until you clear it.")
            for guid in stale_guids:
                row_widget = QWidget()
                row = QHBoxLayout(row_widget)
                row.setContentsMargins(0, 0, 0, 0)
                label = QLabel(f"{guid} (unavailable)")
                label.setProperty("class", "muted")
                row.addWidget(label, 1)
                btn_clear = QPushButton("Clear assignment")
                btn_clear.setProperty("class", "ghost")
                describe(btn_clear, f"Clear assignment for {guid}",
                         "Remove this unavailable device's profile "
                         "assignment. It never applies again unless the "
                         "device is reassigned.")
                btn_clear.clicked.connect(
                    lambda _checked=False, g=guid: self._on_clear_stale_device(g))
                row.addWidget(btn_clear)
                stale.add(row_widget)
                self._device_stale_rows[guid] = row_widget
            outer.addWidget(stale)

        outer.addStretch(1)
        return page

    def _on_show_microphone_toggled(self, checked: bool) -> None:
        """Rebuild the Devices page so its Capture tab appears/disappears with
        the Library toggle that governs it, rather than only on the next
        open -- every other control in this dialog takes effect the moment it
        is touched, and a Capture tab that outlives the setting turning it off
        is a tab whose combos no longer reach APO.

        Safe to throw the page's widgets away mid-session because
        _collect_device_assignments() reads the working dict FIRST and only
        overwrites the GUIDs it still has a combo for -- the capture
        assignments the rebuilt page no longer shows are carried, not
        dropped. _schedule_apply() is already connected separately, so this
        only touches the widgets."""
        if self._loading:
            return
        # _schedule_apply() is DEBOUNCED, so _working still holds the old
        # value right now and _build_devices_page() (which reads it) would
        # rebuild the page exactly as it was. Write it through first; the
        # debounced _collect() lands on the same value shortly after.
        self._working["show_microphone_profiles"] = bool(checked)
        # The toggle lives on the Library page, so the combos being rebuilt
        # are never the widget currently being interacted with.
        self._collect_device_assignments()
        index = self._devices_page_index
        old = self.stack.widget(index)
        was_current = self.stack.currentIndex() == index
        self.stack.insertWidget(index, _scrollable(self._build_devices_page()))
        self.stack.removeWidget(old)
        old.deleteLater()
        if was_current:
            self.stack.setCurrentIndex(index)

    def _build_device_flow_tab(self, devices: list[audio_devices.ApoDevice],
                                assignments: dict[str, str], empty_text: str) -> QWidget:
        """One tab's worth of per-endpoint assignment rows (Playback or
        Capture) -- the same row shape D1b built, factored out so D2's
        Capture tab reuses it verbatim rather than duplicating the combo
        logic. Populates self._device_assignment_combos (shared across BOTH
        tabs, keyed by GUID) so _collect_device_assignments() needs no
        changes."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        if not devices:
            layout.addWidget(QLabel(empty_text))
        else:
            for device in devices:
                row = QHBoxLayout()
                label = QLabel(device.label)
                label.setMinimumWidth(dp(240, 200))
                row.addWidget(label)
                combo = QComboBox()
                combo.addItem(self._FOLLOW_ACTIVE_LABEL, None)
                known_profile_ids = set()
                for profile_id, name in self._device_profile_options:
                    combo.addItem(name, profile_id)
                    known_profile_ids.add(profile_id)
                current = assignments.get(device.guid)
                if current and current not in known_profile_ids:
                    # D1c: the assigned profile was deleted. Without this, a
                    # dead id doesn't match any combo entry (findData() ==
                    # -1 below), so the combo silently lands on "Follow
                    # active profile" with no visible trace anything had
                    # ever been assigned -- same silent-drop the render
                    # side's _valid_device_assignments() already does, but
                    # THIS page's whole job is showing the user what's
                    # assigned, so it must say so instead of hiding it.
                    # Kept selected (not auto-cleared) until the user
                    # explicitly picks something else, mirroring the
                    # Unavailable devices card's own "kept until cleared"
                    # convention.
                    combo.addItem("(deleted profile -- was assigned)", current)
                idx = combo.findData(current) if current else 0
                combo.setCurrentIndex(idx if idx >= 0 else 0)
                describe(combo, f"Profile for {device.label}",
                         "Which profile this device uses: 'Follow active "
                         "profile' (default) tracks whatever profile is "
                         "globally active, or pick a specific saved profile "
                         "to pin to this device regardless of what else is "
                         "active.")
                combo.currentIndexChanged.connect(self._schedule_apply)
                self._device_assignment_combos[device.guid] = combo
                row.addWidget(combo, 1)
                layout.addLayout(row)

        layout.addStretch(1)
        return tab

    def _build_capture_devices_tab(self, assignments: dict[str, str]) -> QWidget:
        """D2: the Capture (microphone) counterpart of the Playback tab --
        same per-endpoint assignment combo, plus an A1-style note (inside
        this page only, no new banner per the D2 spec) listing capture
        endpoints Windows reports that aren't APO-hooked yet."""
        tab = self._build_device_flow_tab(
            self._apo_capture_devices, assignments,
            "No microphones are currently hooked by Equalizer APO.")
        hooked_guids = {d.guid for d in self._apo_capture_devices}
        unhooked = [d for d in audio_devices.list_all_capture_endpoints()
                    if d.is_active and d.guid not in hooked_guids]
        if unhooked:
            layout = tab.layout()
            note = QLabel(
                "Not hooked by Equalizer APO yet, so no profile can be "
                "assigned: " + ", ".join(d.label for d in unhooked))
            note.setWordWrap(True)
            note.setProperty("class", "muted")
            describe(note, "Un-hooked microphones",
                     "These microphones are available on this PC but "
                     "Equalizer APO isn't installed on them, so RUMBLE "
                     "can't apply a profile to them until they're hooked.")
            # Insert before the trailing stretch this tab's layout already ends with.
            layout.insertWidget(layout.count() - 1, note)
        return tab

    def _on_clear_stale_device(self, guid: str) -> None:
        """Drop a stale device_profile_assignments entry. The row is gone from
        the page once cleared, so unlike every other control here there is no
        widget for _collect_device_assignments() to read it back from -- the
        working dict is the only record, hence the edit here rather than a
        plain _schedule_apply()."""
        assignments = dict(self._working.get("device_profile_assignments") or {})
        assignments.pop(guid, None)
        self._working["device_profile_assignments"] = assignments
        row_widget = self._device_stale_rows.pop(guid, None)
        if row_widget is not None:
            row_widget.setParent(None)
            row_widget.deleteLater()
        self._schedule_apply()

    def _collect_device_assignments(self) -> None:
        assignments = dict(self._working.get("device_profile_assignments") or {})
        for guid, combo in self._device_assignment_combos.items():
            profile_id = combo.currentData()
            if profile_id:
                assignments[guid] = profile_id
            else:
                assignments.pop(guid, None)
        self._working["device_profile_assignments"] = assignments

    # ── Auto-Switch ──────────────────────────────────────────────────

    def _build_auto_switch_page(self) -> QWidget:
        page, layout = _page(
            "Auto-Switch",
            "Activate profiles by rule instead of by hand — which app is "
            "playing, the default device, or the time of day.")

        # auto_switch_toggled is handled by the main window exactly like the
        # quick toggle in the profile panel: it starts/stops the watcher
        # adapter, which the generic settings apply below does not do. See
        # _collect() for why the key is still collected as well.
        rules = _Card("Rules",
                      "Auto-Switch drives the globally active profile. It is "
                      "suspended while there are unsaved editor changes, and "
                      "never overrides a per-device assignment.")
        self.chk_auto_switch = QCheckBox("Automatically activate profiles by rule")
        self.chk_auto_switch.setChecked(bool(self._working.get("auto_switch_enabled", False)))
        self.chk_auto_switch.toggled.connect(self.auto_switch_toggled.emit)
        describe(self.chk_auto_switch, "Auto-Switch",
                 "Automatically activate profiles by rule — which app is "
                 "playing, the default device, or time of day. Suspended "
                 "while there are unsaved editor changes.")
        rules.add(self.chk_auto_switch)

        self.btn_auto_switch_rules = QPushButton(" Edit Rules…")
        self.btn_auto_switch_rules.clicked.connect(self.auto_switch_rules_requested)
        describe(self.btn_auto_switch_rules, "Edit Auto-Switch rules",
                 "Choose which apps, output devices or times of day activate "
                 "which profiles.")
        rules_row = QHBoxLayout()
        rules_row.addWidget(self.btn_auto_switch_rules)
        rules_row.addStretch(1)
        rules.add_layout(rules_row)
        layout.addWidget(rules)

        layout.addStretch(1)
        return page

    # ── Equalizer APO ────────────────────────────────────────────────

    def _build_apo_page(self) -> QWidget:
        page, layout = _page(
            "Equalizer APO",
            "Where RUMBLE writes its config, and what it assumes about the "
            "device Equalizer APO is processing.")

        config = _Card("Config folder")
        detected = apo_path.get_config_path()
        config.add(QLabel(f"Detected: {detected}"))
        status = "Installed" if apo_path.is_apo_installed() else "Not detected"
        config.add(QLabel(f"Equalizer APO: {status}"))

        row = QHBoxLayout()
        row.addWidget(QLabel("Override:"))
        self.edit_config_override = QLineEdit(self._working.get("config_path_override") or "")
        self.edit_config_override.setPlaceholderText("Leave blank to use the detected path")
        # editingFinished (focus-out / Enter), not textChanged: every keystroke
        # of a path being typed is a half-written path, and applying those
        # would repoint the whole APO config directory at nonexistent folders
        # on the way to the real one.
        self.edit_config_override.editingFinished.connect(self._schedule_apply)
        describe(self.edit_config_override, "Config path override",
                 "Use a different Equalizer APO config folder instead of the "
                 "automatically detected one. Leave blank for the detected path.")
        btn_browse = QPushButton(" Browse…")
        btn_browse.clicked.connect(self._browse_config_path)
        describe(btn_browse, "Browse for config folder",
                 "Choose the Equalizer APO config folder on disk.")
        row.addWidget(self.edit_config_override, 1)
        row.addWidget(btn_browse)
        config.add_layout(row)
        layout.addWidget(config)

        managed = _Card("Managed config")
        managed.add(_info_banner(
            "RUMBLE owns config.txt so its profiles fully override Equalizer "
            "APO. Your original config is saved as config.txt.rumble_backup and "
            "restored if you hand control back or uninstall.", self._theme))

        manage_row = QHBoxLayout()
        self.btn_import_apo = QPushButton(" Import APO configs…")
        describe(self.btn_import_apo, "Import APO configs",
                 "Scan the Equalizer APO config folder for existing configs and "
                 "import them as RUMBLE profiles.")
        self.btn_import_apo.clicked.connect(self.import_apo_requested)
        is_managed = bool(self._working.get("apo_managed", True))
        self.btn_release_apo = QPushButton(
            " Hand back to APO" if is_managed else " Resume managing APO")
        describe(self.btn_release_apo, "Hand back to APO",
                 "Restore your original config.txt and stop RUMBLE managing "
                 "Equalizer APO (it keeps your profiles on disk). Use again to "
                 "resume managing.")
        self.btn_release_apo.clicked.connect(self.release_apo_requested)
        manage_row.addWidget(self.btn_import_apo)
        manage_row.addWidget(self.btn_release_apo)
        manage_row.addStretch(1)
        managed.add_layout(manage_row)

        managed.add(_caption(
            "Loudness correction is configured in Equalizer APO's own Editor; "
            "RUMBLE preserves its config lines untouched."))
        layout.addWidget(managed)

        rate = _Card(
            "Processing sample rate",
            "The rate your audio device runs at. Used for the response graph "
            "and auto pre-amp maths, and to show sample-denominated delays in "
            "milliseconds.")
        rate_row = QHBoxLayout()
        # Auto/manual ghost toggle. When ON (auto), the app follows the live
        # Windows device format and the manual combo below is inert; when OFF
        # (manual), the combo's fixed rate is used. Mirrors the "session control
        # vs manual override" pairs elsewhere in this codebase.
        self.btn_sample_rate_auto = QPushButton("Match Windows device format")
        self.btn_sample_rate_auto.setCheckable(True)
        self.btn_sample_rate_auto.setProperty("class", "ghost")
        self.btn_sample_rate_auto.setChecked(
            str(self._working.get("sample_rate_mode", "auto")) == "auto")
        self.btn_sample_rate_auto.toggled.connect(self._on_sample_rate_mode_toggled)
        describe(self.btn_sample_rate_auto, "Match Windows device format",
                 "When on, RUMBLE follows the live-detected Windows "
                 "playback device sample rate automatically and the manual "
                 "override below is ignored. When off, the fixed rate you pick "
                 "is used.")
        rate_row.addWidget(self.btn_sample_rate_auto)

        self.cmb_sample_rate = QComboBox()
        for hz in (44100, 48000, 88200, 96000, 176400, 192000):
            self.cmb_sample_rate.addItem(f"{hz / 1000:g} kHz", hz)
        current_rate = int(self._working.get("sample_rate", 48000) or 48000)
        rate_idx = self.cmb_sample_rate.findData(current_rate)
        self.cmb_sample_rate.setCurrentIndex(rate_idx if rate_idx >= 0 else 1)
        self.cmb_sample_rate.currentIndexChanged.connect(self._schedule_apply)
        describe(self.cmb_sample_rate, "Processing sample rate",
                 "The sample rate your audio device runs at. Used for the "
                 "response graph and auto pre-amp maths, and to display "
                 "sample-denominated delays in milliseconds. Windows shows "
                 "the device rate under Sound settings.")
        rate_row.addWidget(self.cmb_sample_rate)
        rate_row.addStretch(1)
        rate.add_layout(rate_row)
        # Apply the initial enabled/greyed state of the manual combo.
        self._on_sample_rate_mode_toggled(self.btn_sample_rate_auto.isChecked())
        layout.addWidget(rate)

        layout.addStretch(1)
        return page

    def _browse_config_path(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select Equalizer APO Config Directory")
        if directory:
            # setText does not emit editingFinished, so apply the browsed path
            # explicitly -- picking a folder in the browser is the whole edit.
            self.edit_config_override.setText(directory)
            self._schedule_apply()

    # ── Appearance ───────────────────────────────────────────────────

    def _build_appearance_page(self) -> QWidget:
        page, layout = _page(
            "Appearance",
            "Colour, size and scale of the interface. Nothing here changes "
            "what you hear.")

        theme_card = _Card("Theme")
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setContentsMargins(0, 0, 0, 0)
        self._swatches: dict[str, ThemeSwatch] = {}
        current_theme = self._working.get("theme", DEFAULT_THEME_NAME)
        _COLS = 4

        # Darkest → brightest, System last; custom/imported themes (already
        # merged into THEMES at load time) fall into the ramp automatically.
        all_themes: list[tuple[str, dict]] = [
            (name, THEMES[name]) for name in ordered_theme_names(include_system=False)
        ]
        try:
            all_themes.append(("System", build_system_theme()))
        except Exception:
            pass

        for i, (name, tokens) in enumerate(all_themes):
            swatch = ThemeSwatch(name, tokens)
            swatch.clicked.connect(self._on_theme_swatch_clicked)
            swatch.set_selected(name == current_theme)
            self._swatches[name] = swatch
            grid.addWidget(swatch, i // _COLS, i % _COLS)
        theme_card.add_layout(grid)
        theme_card.add(_caption("Build your own in Theme Creator, below."))
        layout.addWidget(theme_card)

        # Interface size: segmented Compact / Double toggle. Compact (the
        # default) is the app's one base layout (1000×580 window); Double
        # renders that same layout at exactly twice its size for
        # high-resolution screens. Applied instantly on click via an
        # in-process UI rebuild — the dialog closes with APPLY_SIZE_NOW and
        # is reopened from carry_state() once the UI exists at the new size.
        size_card = _Card(
            "Interface size",
            "Double shows the same compact layout at twice the size, for "
            "high-resolution screens (2000×1280 and up). Applies instantly.")
        size_row = QHBoxLayout()
        size_row.setSpacing(0)
        self._ui_size_mode = "double" if self._working.get("ui_double_size") else "compact"
        self.btn_size_compact = QPushButton("Compact")
        self.btn_size_double = QPushButton("Double (2×)")
        self._size_buttons = (
            (self.btn_size_compact, "compact"),
            (self.btn_size_double, "double"),
        )
        for btn, mode in self._size_buttons:
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, m=mode: self._on_size_clicked(m))
        describe(self.btn_size_compact, "Compact interface size",
                 "Show the interface at its normal size — a compact window "
                 "that leaves room for other applications. Applies instantly.")
        describe(self.btn_size_double, "Double interface size",
                 "Show every element of the interface at twice its size, so "
                 "RUMBLE does not look tiny on high-resolution screens. "
                 "Needs a 2000×1280 or larger display; applies instantly.")
        size_row.addWidget(self.btn_size_compact)
        size_row.addWidget(self.btn_size_double)
        size_row.addStretch(1)
        size_card.add_layout(size_row)
        self._set_ui_size_mode(self._ui_size_mode)
        layout.addWidget(size_card)

        # Graph height: segmented Normal / Double Height toggle. The dB axis
        # always stays ±30 dB (the full gain range); this instead grows the
        # window so the extra height goes entirely to the frequency graph
        # (see window_fixed_size()) — roughly doubling its vertical space for
        # finer control when placing band handles and reading the live
        # spectrum analyzer. Applied instantly, no rebuild needed (unlike
        # Interface size, this is a plain resize, not a Qt scale factor).
        scale_card = _Card(
            "Graph height",
            "Double gives the frequency graph roughly twice the vertical "
            "space, for finer control placing band handles and reading the "
            "live spectrum analyser. Applies instantly.")
        scale_row = QHBoxLayout()
        scale_row.setSpacing(0)
        self._graph_height_mode = (
            "double" if self._working.get("graph_double_height") else "normal")
        self.btn_graph_height_normal = QPushButton("Normal")
        self.btn_graph_height_double = QPushButton("Double Height")
        self._graph_height_buttons = (
            (self.btn_graph_height_normal, "normal"),
            (self.btn_graph_height_double, "double"),
        )
        for btn, mode in self._graph_height_buttons:
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, m=mode: self._set_graph_height_mode(m))
        describe(self.btn_graph_height_normal, "Normal graph height",
                 "Show the frequency graph at its normal height.")
        describe(self.btn_graph_height_double, "Double graph height",
                 "Grow the window so the frequency graph gets roughly twice "
                 "its normal height, for finer control placing band handles "
                 "and reading the live spectrum analyser. Needs a taller "
                 "display. Applies instantly.")
        scale_row.addWidget(self.btn_graph_height_normal)
        scale_row.addWidget(self.btn_graph_height_double)
        scale_row.addStretch(1)
        scale_card.add_layout(scale_row)
        self._set_graph_height_mode(self._graph_height_mode)
        layout.addWidget(scale_card)

        layout.addStretch(1)
        return page

    def _on_theme_swatch_clicked(self, theme_name: str) -> None:
        self._working["theme"] = theme_name
        for name, swatch in self._swatches.items():
            swatch.set_selected(name == theme_name)
        self.theme_preview_requested.emit(theme_name)

    def _on_size_clicked(self, mode: str) -> None:
        """Apply an interface-size choice instantly: snapshot every control
        into the working dict and hand control back to the main window with
        APPLY_SIZE_NOW, which rebuilds the whole UI at the new size and
        reopens this dialog from carry_state(). Unlike every other control,
        the size key cannot ride the normal apply path -- Qt only reads the
        scale factor while a QApplication is being built."""
        changed = mode != self._ui_size_mode
        self._set_ui_size_mode(mode)
        if changed:
            self._collect()
            self.done(self.APPLY_SIZE_NOW)

    def _set_ui_size_mode(self, mode: str) -> None:
        self._ui_size_mode = mode
        self._style_segmented(self._size_buttons, mode)

    def _set_graph_height_mode(self, mode: str) -> None:
        self._graph_height_mode = mode
        self._style_segmented(self._graph_height_buttons, mode)
        # Also called once during the page build to paint the initial state;
        # _schedule_apply is inert then (self._loading).
        self._schedule_apply()

    def _style_segmented(self, buttons: tuple, mode: str) -> None:
        """Style a segmented (btn, mode) group, checking the selected mode."""
        t = self._theme
        active = (
            f"background: {t['accent']}; color: {t['text_on_accent']}; "
            f"border: 1px solid {t['accent_active']}; font-weight: 600;"
        )
        inactive = (
            f"background: transparent; color: {t['text_muted']}; "
            f"border: 1px solid {t['border_subtle']};"
        )
        last = len(buttons) - 1
        for i, (btn, btn_mode) in enumerate(buttons):
            selected = btn_mode == mode
            btn.setChecked(selected)
            # Round only the outer corners of the segmented group; interior
            # buttons drop their right border so dividers stay 1px.
            left_r = 3 if i == 0 else 0
            right_r = 3 if i == last else 0
            right_border = "" if i == last else "border-right: none;"
            btn.setStyleSheet(
                f"QPushButton {{ {active if selected else inactive} {right_border} "
                f"border-top-left-radius: {left_r}px; border-bottom-left-radius: {left_r}px; "
                f"border-top-right-radius: {right_r}px; border-bottom-right-radius: {right_r}px; "
                f"padding: 0 14px; font-size: 11px; }}"
                f"QPushButton:hover {{ color: {t['text_on_accent'] if selected else t['text_primary']}; }}"
            )

    # ── Startup ──────────────────────────────────────────────────────

    def _build_startup_page(self) -> QWidget:
        page, layout = _page(
            "Startup",
            "What happens when you sign in to Windows, and when you close the "
            "window.")

        signin = _Card("On sign-in")
        self.chk_start_with_windows = QCheckBox("Start with Windows")
        self.chk_start_with_windows.setChecked(bool(self._working.get("start_with_windows", True)))
        self.chk_start_with_windows.toggled.connect(self._schedule_apply)
        describe(self.chk_start_with_windows, "Start with Windows",
                 "Launch RUMBLE automatically when you sign in to Windows.")
        signin.add(self.chk_start_with_windows)

        self.chk_start_minimised = QCheckBox("Start minimised to tray")
        self.chk_start_minimised.setChecked(bool(self._working.get("start_minimised", True)))
        self.chk_start_minimised.toggled.connect(self._schedule_apply)
        describe(self.chk_start_minimised, "Start minimised",
                 "When starting with Windows, open straight to the system tray "
                 "instead of showing the main window.")
        signin.add(self.chk_start_minimised)
        layout.addWidget(signin)

        closing = _Card("On closing the window")
        closing.add(_info_banner(
            "RUMBLE always minimises to the system tray when the window is closed. "
            "To fully exit, use Quit in the tray menu.", self._theme))
        layout.addWidget(closing)

        layout.addStretch(1)
        return page

    # ── Backup & Restore ─────────────────────────────────────────────

    def _build_backup_page(self) -> QWidget:
        page, layout = _page(
            "Backup & Restore",
            "Snapshots of everything RUMBLE keeps, and the way back to "
            "defaults.")

        full = _Card(
            "Full backup",
            "Save a complete copy of RUMBLE to a single zip file — every "
            "profile (built-in and your own), all settings, custom themes and "
            "impulse-response files. Restore puts everything back on this or "
            "another PC.")
        full.add(_info_banner(
            "A backup is a full snapshot: profiles, settings, custom themes and "
            "convolution (IR) files. Restoring replaces your current library with "
            "the snapshot.", self._theme))
        row = QHBoxLayout()
        self.btn_backup_now = QPushButton(" Back Up Everything…")
        self.btn_backup_now.setProperty("class", "primary")
        self.btn_backup_now.clicked.connect(self.backup_requested)
        describe(self.btn_backup_now, "Back up everything",
                 "Save every profile, all settings, custom themes and impulse-"
                 "response files into a single zip archive you can keep or move "
                 "to another PC.")
        self.btn_restore_now = QPushButton(" Restore From Backup…")
        self.btn_restore_now.clicked.connect(self.restore_requested)
        describe(self.btn_restore_now, "Restore from backup",
                 "Replace everything — profiles, settings, themes and IR files — "
                 "with the full contents of a backup zip archive.")
        row.addWidget(self.btn_backup_now)
        row.addWidget(self.btn_restore_now)
        row.addStretch(1)
        full.add_layout(row)
        layout.addWidget(full)

        drift = _Card(
            "config.txt drift snapshots",
            "If another tool or an APO reinstall overwrites config.txt, RUMBLE "
            "preserves the displaced content as a timestamped snapshot before "
            "re-taking ownership. Browse and restore those snapshots here.")
        self.btn_restore_drift = QPushButton(" Restore Drift Snapshot…")
        self.btn_restore_drift.clicked.connect(self.drift_restore_requested)
        describe(self.btn_restore_drift, "Restore drift snapshot",
                 "Browse preserved earlier versions of config.txt and restore "
                 "one, replacing the current config.txt.")
        drift_row = QHBoxLayout()
        drift_row.addWidget(self.btn_restore_drift)
        drift_row.addStretch(1)
        drift.add_layout(drift_row)
        layout.addWidget(drift)

        reset = _Card(
            "Reset settings",
            "Put settings back to their defaults — you choose which groups, so "
            "a reset can be as small as the theme or as broad as every setting. "
            "Profiles, custom themes, IR files and Ear Calibration results are "
            "never affected.")
        self.btn_reset_settings = QPushButton(" Reset Settings…")
        self.btn_reset_settings.clicked.connect(self._on_reset_settings)
        describe(self.btn_reset_settings, "Reset settings",
                 "Choose which groups of settings go back to their default "
                 "values. Profiles and calibration results are not affected.")
        reset_row = QHBoxLayout()
        reset_row.addWidget(self.btn_reset_settings)
        reset_row.addStretch(1)
        reset.add_layout(reset_row)
        layout.addWidget(reset)

        layout.addStretch(1)
        return page

    def _on_reset_settings(self) -> None:
        dialog = ResetSettingsDialog(self._theme, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        groups = dialog.selected_groups()
        if not groups:
            return
        self._reset_groups = groups
        # done() flushes any pending debounce first, so an edit made moments
        # before the reset is written and then reset, rather than landing on
        # top of it afterwards.
        self.done(self.RESET_NOW)

    def reset_groups(self) -> list[str]:
        """The groups to reset, valid after exec() returns RESET_NOW."""
        return list(self._reset_groups)

    # ── Theme Creator ─────────────────────────────────────────────────

    def _build_theme_creator_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(dp(24, 20), dp(22, 18), dp(24, 20), dp(24, 20))
        outer.setSpacing(dp(12, 10))
        outer.addWidget(_section_title("THEME CREATOR"))
        outer.addWidget(_caption(
            "Start from a built-in theme, change any colour, and save it — it "
            "joins the grid on the Appearance page."))

        # Name row
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Theme name:"))
        self._tc_name = QLineEdit()
        self._tc_name.setPlaceholderText("My Custom Theme")
        self._tc_name.setFixedWidth(240)
        describe(self._tc_name, "Theme name",
                 "The name your custom colour theme is saved and listed under.")
        name_row.addWidget(self._tc_name)
        name_row.addStretch(1)
        outer.addLayout(name_row)

        # Base-on row
        base_row = QHBoxLayout()
        base_row.addWidget(QLabel("Start from:"))
        self._tc_base_combo = QComboBox()
        self._tc_base_combo.addItems(list(THEMES.keys()))
        self._tc_base_combo.setFixedWidth(160)
        self._tc_base_combo.currentTextChanged.connect(self._tc_load_base_theme)
        describe(self._tc_base_combo, "Base theme",
                 "The built-in theme used as the starting point — its colours "
                 "fill the editor below for you to adjust.")
        base_row.addWidget(self._tc_base_combo)
        base_row.addStretch(1)
        outer.addLayout(base_row)

        # Scrollable colour token editor
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(dp(12, 10))
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # One card per token group, one colour-swatch row per token
        self._tc_swatches: dict[str, QPushButton] = {}
        base_tokens = THEMES.get(self._tc_base_combo.currentText(), FALLBACK_THEME)
        self._tc_working: dict[str, str] = dict(base_tokens)

        for group_label, tokens in _EDITOR_GROUPS:
            card = _Card(group_label)
            grid = QGridLayout()
            grid.setSpacing(8)
            grid.setContentsMargins(0, 0, 0, 0)
            for row_idx, (key, label) in enumerate(tokens):
                lbl = QLabel(label)
                lbl.setFixedWidth(dp(160, 132))

                btn = QPushButton()
                btn.setFixedSize(36, 22)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                current = self._tc_working.get(key, "#000000")
                btn.setStyleSheet(f"background-color: {current}; border: 1px solid #444; border-radius: 3px;")
                btn.setProperty("tc_key", key)
                btn.clicked.connect(lambda _checked=False, k=key: self._tc_pick_colour(k))
                describe(btn, f"{label} colour",
                         f"Pick the colour used for '{label}' in your custom theme.")
                self._tc_swatches[key] = btn

                hex_lbl = QLabel(current.upper())
                hex_lbl.setFixedWidth(90)
                hex_lbl.setProperty("tc_hex_key", key)

                grid.addWidget(lbl, row_idx, 0)
                grid.addWidget(btn, row_idx, 1)
                grid.addWidget(hex_lbl, row_idx, 2)
                grid.setColumnStretch(3, 1)

            card.add_layout(grid)
            inner_layout.addWidget(card)

        inner_layout.addStretch(1)

        # Save button
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        btn_save = QPushButton(" Save Theme")
        btn_save.setProperty("class", "primary")
        btn_save.clicked.connect(self._tc_save_theme)
        describe(btn_save, "Save theme",
                 "Save this custom theme and make it available in the "
                 "Appearance section.")
        save_row.addWidget(btn_save)
        outer.addLayout(save_row)

        return page

    def _tc_load_base_theme(self, name: str) -> None:
        base = THEMES.get(name, FALLBACK_THEME)
        self._tc_working = dict(base)
        for key, btn in self._tc_swatches.items():
            colour = self._tc_working.get(key, "#000000")
            btn.setStyleSheet(f"background-color: {colour}; border: 1px solid #444; border-radius: 3px;")
        # Update hex labels
        page = self.stack.widget(CATEGORIES.index("Theme Creator"))
        for lbl in page.findChildren(QLabel):
            key = lbl.property("tc_hex_key")
            if key:
                lbl.setText(self._tc_working.get(key, "#000000").upper())

    def _tc_pick_colour(self, key: str) -> None:
        current = self._tc_working.get(key, "#000000")
        # Strip alpha suffix for QColorDialog (it only handles 6-digit hex)
        color_hex = current[:7] if len(current) >= 7 else current
        initial = QColor(color_hex)
        colour = QColorDialog.getColor(initial, self, f"Pick colour for {key}")
        if not colour.isValid():
            return
        hex_val = colour.name().upper()
        self._tc_working[key] = hex_val
        btn = self._tc_swatches[key]
        btn.setStyleSheet(f"background-color: {hex_val}; border: 1px solid #444; border-radius: 3px;")
        # Update hex label
        page = self.stack.widget(CATEGORIES.index("Theme Creator"))
        for lbl in page.findChildren(QLabel):
            if lbl.property("tc_hex_key") == key:
                lbl.setText(hex_val)
                break
        # Live preview: build a temporary theme dict and emit
        preview = dict(self._tc_working)
        preview.setdefault("name", self._tc_name.text().strip() or "Preview")
        preview.setdefault("accent_name", "Custom")
        self.theme_preview_requested.emit(preview.get("name", "Preview"))

    def _tc_save_theme(self) -> None:
        name = self._tc_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Theme Creator", "Please enter a theme name.")
            return
        theme = dict(self._tc_working)
        theme["name"] = name
        theme.setdefault("accent_name", "Custom")
        errors = validate_theme(theme)
        if errors:
            QMessageBox.warning(self, "Theme Creator",
                                "Theme is incomplete:\n" + "\n".join(f"• {e}" for e in errors))
            return
        save_custom_theme(self._data_dir, theme)
        self.custom_theme_saved.emit(name)
        QMessageBox.information(self, "Theme Creator",
                                f"Theme \"{name}\" saved successfully.\n"
                                "It is now available in the Appearance section "
                                "and the Theme menu.")

    # ── plumbing ─────────────────────────────────────────────────────

    def _on_sample_rate_mode_toggled(self, auto: bool) -> None:
        """Grey out the manual sample-rate combo when auto (match Windows) is
        on; enable it when off. Pure UI enable-state — no reentrancy guard
        needed (it reads no control it also writes)."""
        self.cmb_sample_rate.setEnabled(not auto)
        self._schedule_apply()

    def _schedule_apply(self, *_args) -> None:
        """Queue an apply for APPLY_DEBOUNCE_MS from now. Every control's
        change signal lands here; the debounce collapses a slider drag or a
        typed-in number into one settings write and one APO re-render.

        Takes *_args so it can be connected straight to toggled(bool),
        valueChanged(int/float) and currentIndexChanged(int) alike."""
        if self._loading:
            return
        self._apply_timer.start()

    def _apply_now(self) -> None:
        """Collect, persist and broadcast the current state of every control.
        Called by the debounce timer, and directly wherever a pending apply
        must not be lost (closing, an interface-size rebuild)."""
        self._apply_timer.stop()
        self._collect()
        names = {slot_id: edit.text() for slot_id, edit in self._listener_name_edits.items()}
        listener_slots.save_slot_names(self._data_dir, names)
        self.settings_saved.emit(dict(self._working))

    def done(self, result: int) -> None:  # noqa: N802 - Qt override
        """Flush a pending debounce before the dialog goes away -- otherwise a
        change made in the last quarter-second before Close (or Esc, or the
        interface-size rebuild) would be dropped on the floor."""
        if self._apply_timer.isActive():
            self._apply_now()
        super().done(result)

    def _collect(self) -> None:
        """Snapshot every control's current value into the working dict."""
        self._working["start_with_windows"] = self.chk_start_with_windows.isChecked()
        self._working["start_minimised"] = self.chk_start_minimised.isChecked()
        self._working["ui_double_size"] = self._ui_size_mode == "double"
        self._working["default_bands"] = self.spin_default_bands.value()
        self._working["auto_preamp"] = self.chk_auto_preamp.isChecked()
        self._working["show_builtin_profiles"] = self.chk_show_builtin.isChecked()
        self._working["show_microphone_profiles"] = self.chk_show_microphone.isChecked()
        self._working["spinorama_add_crossover"] = self.chk_speaker_crossover.isChecked()
        self._working["spinorama_directivity_limiting"] = self.chk_directivity_limiting.isChecked()
        self._working["spinorama_default_tilt"] = self.spin_default_tilt.value()
        self._working["spinorama_smoothing_oct"] = self.spin_smoothing_oct.value()
        self._working["adaptive_smoothing"] = self.chk_adaptive_smoothing.isChecked()
        self._working["graph_double_height"] = self._graph_height_mode == "double"
        self._working["sample_rate"] = int(self.cmb_sample_rate.currentData() or 48000)
        self._working["sample_rate_mode"] = (
            "auto" if self.btn_sample_rate_auto.isChecked() else "manual")
        override = self.edit_config_override.text().strip()
        self._working["config_path_override"] = override or None
        # Collected even though auto_switch_toggled already applied it live:
        # every apply hands the whole working dict to the main window, which
        # writes it over manager.settings wholesale. Left out, the value
        # snapshotted when the dialog opened would be written back on the next
        # unrelated change, silently undoing the toggle.
        self._working["auto_switch_enabled"] = self.chk_auto_switch.isChecked()
        self._working["default_listener_slot"] = int(self.combo_default_listener.currentData() or 1)
        self._working["listener_correction_enabled"] = self.chk_listener_correction.isChecked()
        self._working["profile_loudness_matching"] = self.chk_profile_loudness_matching.isChecked()
        self._working["warn_unsaved_changes"] = self.chk_warn_unsaved.isChecked()
        self._working["iso226_loudness_enabled"] = self.chk_iso226_enabled.isChecked()
        self._working["iso226_reference_phon"] = self.spin_iso226_phon.value()
        self._working["crossfeed_enabled"] = self.chk_crossfeed_enabled.isChecked()
        self._working["crossfeed_preset"] = str(
            self.combo_crossfeed_preset.currentData() or crossfeed.DEFAULT_PRESET)
        self._working["crossfeed_strength"] = self.slider_crossfeed_strength.value()
        self._working["crossfeed_custom_gain_db"] = self.spin_crossfeed_custom_gain.value()
        self._working["crossfeed_custom_freq_hz"] = self.spin_crossfeed_custom_freq.value()
        self._working["crossfeed_custom_delay_us"] = self.spin_crossfeed_custom_delay.value()
        self._working["crossfeed_soundstage_blend"] = self.slider_crossfeed_soundstage_blend.value()
        self._working["crossfeed_soundstage_width"] = self.slider_crossfeed_soundstage_width.value()
        self._collect_device_assignments()

    def result_settings(self) -> dict:
        return dict(self._working)

    def carry_state(self) -> dict:
        """Everything needed to reopen this dialog as it looks right now,
        after the in-process UI rebuild an instant size change triggers.

        "category" is a CATEGORIES/stack index, not a nav row -- the rail has
        group-header rows that are not pages, so the two do not match.
        """
        return {
            "category": self.stack.currentIndex(),
            "working": dict(self._working),
            "theme_creator": {
                "name": self._tc_name.text(),
                "base": self._tc_base_combo.currentText(),
                "tokens": dict(self._tc_working),
            },
        }

    def _tc_restore(self, tc: dict) -> None:
        """Reload carried Theme Creator state (name, base, edited colours)."""
        self._tc_name.setText(tc.get("name", ""))
        self._tc_base_combo.blockSignals(True)
        self._tc_base_combo.setCurrentText(tc.get("base", self._tc_base_combo.currentText()))
        self._tc_base_combo.blockSignals(False)
        self._tc_working = dict(tc.get("tokens") or self._tc_working)
        for key, btn in self._tc_swatches.items():
            colour = self._tc_working.get(key, "#000000")
            btn.setStyleSheet(f"background-color: {colour}; border: 1px solid #444; border-radius: 3px;")
        page = self.stack.widget(CATEGORIES.index("Theme Creator"))
        for lbl in page.findChildren(QLabel):
            key = lbl.property("tc_hex_key")
            if key:
                lbl.setText(self._tc_working.get(key, "#000000").upper())
