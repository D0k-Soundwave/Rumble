"""Left sidebar: searchable profile tree organised by tag hierarchy.

The panel mirrors the system-tray toast: profiles live in a collapsible tag
tree (the same `TagHierarchyBuilder` model the tray uses), split into top-level
**User** and **Built-in** branches with their tags nested beneath. A search box
filters the whole tree and auto-expands matches — the one thing the compact
tray view can't do. There is deliberately no filter dropdown; the tree *is* the
filter.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from rumble.core.profile_manager import ProfileMeta
from rumble.core.tag_hierarchy import TagHierarchyBuilder
from rumble.ui import icons
from rumble.ui.a11y import describe
from rumble.ui.density import dp
from rumble.ui.marquee_label import MarqueeLabel

# Item data roles. UserRole carries the profile id on profile rows (None on
# tag rows); the next two carry a tag row's stable key and its base label.
_ROLE_ID = Qt.ItemDataRole.UserRole
_ROLE_KEY = Qt.ItemDataRole.UserRole + 1
_ROLE_BASE = Qt.ItemDataRole.UserRole + 2

# Stable keys for the two top-level branches and the per-branch untagged bucket.
_USER_ROOT = "root:user"
_BUILTIN_ROOT = "root:builtin"
_UNTAGGED = "__untagged__"

# Match the tray's glyphs (U+25BC / U+25B6) — Inter renders these; the smaller
# ▾/▸ variants fall back to box glyphs.
_EXPAND_ARROW = "▼"    # open
_COLLAPSE_ARROW = "▶"  # closed

_LISTENER_SLOT_IDS = (1, 2, 3, 4)  # mirrors rumble.core.listener_slots.SLOT_IDS


def panel_width() -> int:
    return dp(240, 200)


def _row_height() -> int:
    return dp(52, 40)


def _tag_row_height() -> int:
    return dp(30, 24)


class ProfileListItemWidget(QFrame):
    """Custom row widget shown for a profile inside the tree."""

    # profile id, entered — emitted as the pointer enters/leaves the whole row.
    row_hover = Signal(str, bool)

    def __init__(self, meta: ProfileMeta, theme: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.profile_id = meta.id
        self.setFixedHeight(_row_height())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, dp(8, 4), 12, dp(8, 4))
        layout.setSpacing(8)

        # Active indicator: a filled check in the accent colour marks the live
        # EQ, distinct from the row's selection highlight (which means "editing").
        self.lbl_active = QLabel()
        self.lbl_active.setFixedWidth(dp(18, 16))
        self.lbl_active.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_active)

        text_col = QVBoxLayout()
        text_col.setSpacing(dp(2, 1))
        # Rows scroll their overflowing text only while highlighted or selected;
        # the panel drives this via set_scrolling (no per-label pointer hover).
        self.lbl_name = MarqueeLabel(meta.name, hover_scroll=False)
        self.lbl_name.setProperty("class", "body-medium")
        self.lbl_name.setStyleSheet("font-weight: 500;")

        self.lbl_subtitle = MarqueeLabel(self._subtitle(meta), hover_scroll=False)
        self.lbl_subtitle.setProperty("class", "muted")

        text_col.addWidget(self.lbl_name)
        text_col.addWidget(self.lbl_subtitle)
        layout.addLayout(text_col, 1)

        self.lbl_lock = QLabel()
        layout.addWidget(self.lbl_lock)

        # Transparent so the tree's selection/hover highlight shows through.
        self.setStyleSheet("QFrame { border: none; background: transparent; }")

        # Let mouse events fall through the child labels to this frame so its
        # enter/leave fire for the *whole* row (not just over the text), and so
        # clicks still propagate to the tree for selection.
        for child in (self.lbl_active, self.lbl_name, self.lbl_subtitle, self.lbl_lock):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._set_active(meta)
        self._set_badge(meta)
        self._describe_row(meta)

    def set_scrolling(self, active: bool) -> None:
        """Start/stop marquee scrolling for this row's name and subtitle."""
        self.lbl_name.set_scroll_active(active)
        self.lbl_subtitle.set_scroll_active(active)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.row_hover.emit(self.profile_id, True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.row_hover.emit(self.profile_id, False)
        super().leaveEvent(event)

    @staticmethod
    def _subtitle(meta: ProfileMeta) -> str:
        return ", ".join(meta.tags) if meta.tags else (meta.modified[:10] if meta.modified else "")

    def _set_active(self, meta: ProfileMeta) -> None:
        # Colour is set via the marquee's override (not an inline stylesheet) so
        # the custom-painted scrolling text picks it up too.
        if meta.active:
            self.lbl_active.setPixmap(icons.icon_pixmap("check_circle", self.theme["accent"], 14))
            self.lbl_name.setStyleSheet("font-weight: 600;")
            self.lbl_name.set_active_color(QColor(self.theme["accent"]))
        else:
            self.lbl_active.clear()
            self.lbl_name.setStyleSheet("font-weight: 500;")
            self.lbl_name.set_active_color(None)

    def _describe_row(self, meta: ProfileMeta) -> None:
        parts = [meta.description] if meta.description else []
        if meta.active:
            parts.append("Currently active.")
        if meta.builtin:
            parts.append("Built-in profile, system protected — duplicate it to customise.")
        elif meta.readonly:
            parts.append("Locked against changes.")
        parts.append("Click to make it the active EQ and edit it, right-click for more actions.")
        describe(self, f"Profile {meta.name}", " ".join(parts))

    def _set_badge(self, meta: ProfileMeta) -> None:
        if meta.builtin:
            self.lbl_lock.setPixmap(icons.icon_pixmap("shield", self.theme["text_muted"], 13))
            describe(self.lbl_lock, "Built-in",
                     "This is a built-in profile, system protected: it cannot be "
                     "overwritten, renamed or deleted. Duplicate it to customise.")
        elif meta.readonly:
            self.lbl_lock.setPixmap(icons.icon_pixmap("lock", self.theme["text_muted"], 13))
            describe(self.lbl_lock, "Locked",
                     "This profile is locked: it cannot be overwritten, renamed or "
                     "deleted until you unlock it (right-click the profile).")
        else:
            self.lbl_lock.clear()
        self.lbl_lock.setVisible(meta.builtin or meta.readonly)


def _token_color(token: str) -> QColor:
    """Parse a theme colour token into a QColor honouring the theme's intent.

    The theme writes translucent tints as "#RRGGBBAA" (e.g. accent_muted =
    "#D4845A28" is the accent at ~16% alpha). Both QColor and Qt's stylesheet
    parser instead read 8-digit hex as "#AARRGGBB", which scrambles the channels
    (turning the blue Void accent into green, etc.) — that is exactly why
    qss_color() rewrites these tokens to rgba() for stylesheets. We split the
    bytes the same way here so the row fill is always the *current theme's*
    accent, not a mis-decoded colour.
    """
    s = token.strip()
    if len(s) == 9 and s.startswith("#"):
        r, g, b, a = (int(s[i:i + 2], 16) for i in (1, 3, 5, 7))
        return QColor(r, g, b, a)
    return QColor(token)


class _FullRowTree(QTreeWidget):
    """Tree that paints the selection/hover highlight across the FULL row width.

    Qt's default item highlight only covers an item's *visual rect*, which starts
    after the indentation — so a deeply nested row shows a highlight that begins
    partway across and slides further right as branches expand. Filling the whole
    row inside `drawRow` keeps the highlight edge-to-edge at every depth.
    """

    # Windows-style keyboard shortcuts on the selection: Delete removes the
    # selected profiles, F2 renames the single selected one. The panel wires
    # these to the same actions as the context menu.
    delete_pressed = Signal()
    rename_pressed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._sel_color = QColor(0, 0, 0, 0)
        self._hover_color = QColor(0, 0, 0, 0)
        self._hover_row = -1  # top-level row index of the hovered row, -1 if none
        # Track the pointer so the hovered row can be repainted full-width. Hover
        # moving between rows only invalidates each item's (indented) rect, so we
        # repaint the whole viewport to keep the full-row fill in step.
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.entered.connect(self._on_entered)
        self.viewportEntered.connect(self._clear_hover)

    # accent_muted is intentionally a very light 16% tint; that reads as too faint
    # for "this row is selected", so the selection fill uses the same theme accent
    # at a stronger alpha while hover keeps the subtle tint.
    _SEL_ALPHA = 0x70  # ≈44%

    def set_row_colors(self, selected: str, hover: str) -> None:
        sel = _token_color(selected)
        sel.setAlpha(self._SEL_ALPHA)
        self._sel_color = sel
        self._hover_color = _token_color(hover)

    def _on_entered(self, index) -> None:
        row = self.visualItemRect(self.itemFromIndex(index)).top()
        if row != self._hover_row:
            self._hover_row = row
            self.viewport().update()

    def _clear_hover(self) -> None:
        if self._hover_row != -1:
            self._hover_row = -1
            self.viewport().update()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Delete:
            self.delete_pressed.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_F2:
            self.rename_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._clear_hover()  # drop the hover fill when the pointer exits

    def drawRow(self, painter, option, index) -> None:  # noqa: N802 - Qt override
        # State_Selected / State_MouseOver are applied by QTreeView per column
        # *inside* its own loop, so they are not reliably set on the row-level
        # `option` handed to us here. Determine both from the view directly, then
        # fill the ENTIRE row (x=0 to the right edge) so the highlight ignores the
        # indentation gutter and always spans the full width.
        full = QRect(0, option.rect.y(), self.viewport().width(), option.rect.height())
        sm = self.selectionModel()
        if sm is not None and sm.isSelected(index):
            painter.fillRect(full, self._sel_color)
        elif option.rect.top() == self._hover_row and self._hover_color.alpha():
            painter.fillRect(full, self._hover_color)
        super().drawRow(painter, option, index)


class ProfilePanel(QWidget):
    """Fixed-width left sidebar: searchable tag tree of profiles."""

    new_profile_requested = Signal()
    import_requested = Signal()
    export_requested = Signal()
    auto_switch_toggled = Signal(bool)
    abx_requested = Signal()
    listener_slot_changed = Signal(int)     # slot id (1-4)
    profile_selected = Signal(str)          # profile id (single click / selection)
    profile_activated = Signal(str)         # profile id (double click / activation)
    profile_rename_requested = Signal(str, str)   # id, new_name
    profile_duplicate_requested = Signal(str)
    profile_delete_requested = Signal(str)
    profile_lock_toggle_requested = Signal(str)
    # Batch actions on a multi-selection (Ctrl/Shift-selected rows).
    profiles_duplicate_requested = Signal(list)        # list[str] ids
    profiles_delete_requested = Signal(list)           # list[str] ids
    profiles_lock_requested = Signal(list, bool)       # ids, lock=True / unlock=False

    def __init__(self, theme: dict, manager=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.manager = manager
        self.setFixedWidth(panel_width())
        self.setProperty("class", "surface")

        self._profiles: list[ProfileMeta] = []
        self._item_by_id: dict[str, QTreeWidgetItem] = {}
        self._row_widgets: dict[str, ProfileListItemWidget] = {}
        self._tag_items: dict[str, QTreeWidgetItem] = {}
        # Remembered expansion across rebuilds. User branch open by default so a
        # user's own profiles are front and centre; the 21 built-ins stay tucked
        # under a single collapsed line.
        self._expanded_keys: set[str] = {_USER_ROOT}
        self._search: str = ""
        self._hovered_id: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── header ───────────────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(dp(48, 34))
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(dp(16, 12), 0, dp(12, 8), 0)
        title = QLabel("PROFILES")
        title.setProperty("class", "heading")
        self.btn_new = QPushButton(" New")
        self.btn_new.setProperty("class", "ghost")
        self.btn_new.setIcon(icons.icon("add", theme["text_secondary"], 16))
        describe(self.btn_new, "New profile",
                 "Create a new EQ profile, choosing its name, speaker layout "
                 "and starting number of bands.")
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(self.btn_new)
        root.addWidget(header)

        # ── quick actions: Auto-Switch on/off + Blind A/B/X launcher ───
        # Same "ghost" button convention used everywhere else in the app
        # (checkable toggle for on/off, plain click for a launched action) —
        # relocated here from the main-window status bar so they sit with
        # the profile list they act on. Settings is still the authoritative
        # home for the full flows (Auto-Switch rule editing on its own page,
        # Ear Calibration on its own page); these are quick-access
        # duplicates, same pattern as the tray menu.
        actions = QWidget()
        actions.setFixedHeight(dp(36, 28))
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(dp(8, 6), dp(4, 3), dp(8, 6), dp(4, 3))
        actions_layout.setSpacing(4)

        self.btn_auto_switch = QPushButton("Auto-Switch")
        self.btn_auto_switch.setCheckable(True)
        self.btn_auto_switch.setProperty("class", "ghost")
        self.btn_auto_switch.toggled.connect(self.auto_switch_toggled.emit)
        describe(self.btn_auto_switch, "Auto-Switch",
                 "Automatically activate profiles by rule — which app is "
                 "playing, the default device, or time of day. Suspended "
                 "while there are unsaved editor changes.")

        self.btn_abx = QPushButton("Blind A/B/X")
        self.btn_abx.setProperty("class", "ghost")
        self.btn_abx.clicked.connect(self.abx_requested.emit)
        describe(self.btn_abx, "Blind A/B/X Test",
                 "Run a blind listening test between two profiles to check "
                 "whether you can reliably tell them apart. Refused while "
                 "there are unsaved editor changes or an active live preview.")

        for btn in (self.btn_auto_switch, self.btn_abx):
            btn.setMinimumWidth(0)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            actions_layout.addWidget(btn)
        root.addWidget(actions)

        # ── listener slot ────────────────────────────────────────────
        # Session-level control (same category as the Auto-Switch/Blind
        # A/B/X row above), following the same "moved out of the status bar
        # into the profile panel" pattern the comment on that row documents.
        listener_row = QWidget()
        listener_row.setFixedHeight(dp(36, 28))
        listener_layout = QHBoxLayout(listener_row)
        listener_layout.setContentsMargins(dp(8, 6), 0, dp(8, 6), dp(4, 3))
        listener_layout.setSpacing(4)
        listener_label = QLabel("Listener:")
        listener_label.setProperty("class", "secondary")
        self.combo_listener = QComboBox()
        self.combo_listener.currentIndexChanged.connect(self._on_listener_combo_changed)
        describe(self.combo_listener, "Active Listener Slot",
                 "Apply this listener's own Ear Calibration correction on top "
                 "of whatever profile is playing. Never changes the profile "
                 "itself -- switching listeners resets to your Settings "
                 "default on every launch.")
        listener_layout.addWidget(listener_label)
        listener_layout.addWidget(self.combo_listener, 1)
        root.addWidget(listener_row)

        # ── search ───────────────────────────────────────────────────
        search_widget = QWidget()
        search_widget.setFixedHeight(dp(44, 32))
        search_layout = QHBoxLayout(search_widget)
        search_layout.setContentsMargins(0, dp(6, 3), 0, dp(6, 3))
        self.search_box = QLineEdit()
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setPlaceholderText("Search profiles…")
        self.search_box.textChanged.connect(lambda _=None: self._search_timer.start())
        describe(self.search_box, "Search profiles",
                 "Type to filter the tree by profile name or tag; matching "
                 "branches expand automatically. Clear it to browse the full tree.")
        search_layout.addWidget(self.search_box)
        root.addWidget(search_widget)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_search)

        # ── profile tree ─────────────────────────────────────────────
        self.tree = _FullRowTree()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setRootIsDecorated(False)   # arrows are drawn in the tag label
        self.tree.setIndentation(dp(14, 12))
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.setUniformRowHeights(False)
        self.tree.setExpandsOnDoubleClick(False)
        # ExtendedSelection gives the standard Windows behaviour: click selects
        # one, Ctrl+click toggles individual rows, Shift+click selects a range.
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setAutoScroll(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemCollapsed.connect(self._on_item_collapsed)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.delete_pressed.connect(self._emit_delete_selection)
        self.tree.rename_pressed.connect(self._emit_rename_selection)
        describe(self.tree, "Profile tree",
                 "Your EQ profiles, grouped by tag under User and Built-in. "
                 "Click a group to expand it, click a profile to make it the "
                 "active EQ and edit it, or right-click a profile for more actions. "
                 "Ctrl+click and Shift+click select several profiles at once for "
                 "batch duplicate, delete or lock; press Delete to remove them.")
        root.addWidget(self.tree, 1)
        self._apply_tree_style()

        # ── bottom action buttons ────────────────────────────────────
        # Borderless "ghost" buttons that float directly on the panel surface —
        # no enclosing footer box or divider line.
        bottom = QFrame()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(dp(8, 6), dp(8, 5), dp(8, 6), dp(8, 5))
        bottom_layout.setSpacing(4)

        self.btn_import = QPushButton("Import")
        self.btn_import.setProperty("class", "ghost")
        describe(self.btn_import, "Import profile",
                 "Add a profile from an Equalizer APO configuration text file, "
                 "such as one exported from RUMBLE or another EQ tool.")
        self.btn_export = QPushButton("Export")
        self.btn_export.setProperty("class", "ghost")
        describe(self.btn_export, "Export profile",
                 "Save the selected profile as a portable text file you can "
                 "share or use on another computer.")
        # Backup / Restore now live in Settings → Backup & Restore.
        for btn in (self.btn_import, self.btn_export):
            btn.setMinimumWidth(0)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            bottom_layout.addWidget(btn)
        root.addWidget(bottom)

        # ── wiring ───────────────────────────────────────────────────
        self.btn_new.clicked.connect(self.new_profile_requested.emit)
        self.btn_import.clicked.connect(self.import_requested.emit)
        self.btn_export.clicked.connect(self.export_requested.emit)

    # ── data binding ─────────────────────────────────────────────────

    def set_profiles(self, profiles: list[ProfileMeta]) -> None:
        self._profiles = list(profiles)
        self._rebuild_tree()

    def set_auto_switch_checked(self, enabled: bool) -> None:
        self.btn_auto_switch.blockSignals(True)
        self.btn_auto_switch.setChecked(enabled)
        self.btn_auto_switch.blockSignals(False)

    def set_listener_slots(self, names: dict[int, str], active_slot: int) -> None:
        """Populate/refresh the listener combo. *names* maps slot id -> a
        custom display name; a slot absent from *names* shows "Listener N"."""
        self.combo_listener.blockSignals(True)
        self.combo_listener.clear()
        for slot_id in _LISTENER_SLOT_IDS:
            label = names.get(slot_id) or f"Listener {slot_id}"
            self.combo_listener.addItem(label, slot_id)
        idx = self.combo_listener.findData(active_slot)
        self.combo_listener.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_listener.blockSignals(False)

    def _on_listener_combo_changed(self, _index: int) -> None:
        slot_id = self.combo_listener.currentData()
        if slot_id is not None:
            self.listener_slot_changed.emit(int(slot_id))

    def selected_profile_id(self) -> str | None:
        item = self.tree.currentItem()
        return item.data(0, _ROLE_ID) if item is not None else None

    def selected_profile_ids(self) -> list[str]:
        """Ids of every selected profile row (tag rows aren't selectable)."""
        return [pid for it in self.tree.selectedItems()
                if (pid := it.data(0, _ROLE_ID))]

    def _emit_delete_selection(self) -> None:
        ids = self.selected_profile_ids()
        if ids:
            self.profiles_delete_requested.emit(ids)

    def _emit_rename_selection(self) -> None:
        # Rename is single-only; F2 with several selected does nothing.
        ids = self.selected_profile_ids()
        if len(ids) == 1:
            self.profile_rename_requested.emit(ids[0], "")

    def select_profile(self, profile_id: str | None) -> None:
        item = self._item_by_id.get(profile_id) if profile_id else None
        if item is None:
            return
        self.tree.blockSignals(True)
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            self._sync_arrow(parent)
            parent = parent.parent()
        self.tree.setCurrentItem(item)
        self.tree.blockSignals(False)
        self._refresh_scrolling()  # signals were blocked; start the new row's scroll

    # ── search ───────────────────────────────────────────────────────

    def _apply_search(self) -> None:
        self._search = self.search_box.text().strip().lower()
        self._rebuild_tree()

    def _visible_ids(self) -> set[str] | None:
        """Ids passing the current search, or None when no search is active."""
        if not self._search:
            return None
        tokens = self._search.split()
        out: set[str] = set()
        for p in self._profiles:
            hay = f"{p.name} {' '.join(p.tags)}".lower()
            if all(tok in hay for tok in tokens):
                out.add(p.id)
        return out

    # ── tree construction ────────────────────────────────────────────

    def _rebuild_tree(self) -> None:
        previously_selected = self.selected_profile_id()
        scroll_pos = self.tree.verticalScrollBar().value()
        self.tree.blockSignals(True)
        self.tree.clear()
        self._item_by_id.clear()
        self._row_widgets.clear()
        self._tag_items.clear()

        visible = self._visible_ids()
        user = [p for p in self._profiles if not p.builtin]
        builtin = [p for p in self._profiles if p.builtin]
        self._add_branch("User", _USER_ROOT, user, visible)
        self._add_branch("Built-in", _BUILTIN_ROOT, builtin, visible)

        if previously_selected and previously_selected in self._item_by_id:
            self.tree.setCurrentItem(self._item_by_id[previously_selected])
        self.tree.blockSignals(False)
        self.tree.verticalScrollBar().setValue(scroll_pos)
        self._refresh_scrolling()  # fresh row widgets need their scroll state set

    def _add_branch(self, label: str, key: str, profiles: list[ProfileMeta],
                    visible: set[str] | None) -> None:
        if not profiles:
            return
        by_id = {p.id: p for p in profiles}
        matching = [p for p in profiles if visible is None or p.id in visible]
        if not matching:
            return

        root_item = self._make_tag_item(None, label, len(matching), key)

        builder = TagHierarchyBuilder()
        builder.build_from_profiles(profiles)
        in_hierarchy = set(builder.find_profiles(builder.root))
        self._render_children(builder, builder.root, root_item, key, by_id, visible)

        untagged = [p for p in profiles if p.id not in in_hierarchy]
        self._add_untagged(root_item, key, untagged, visible)

        self._set_expanded(root_item, key, visible)

    def _render_children(self, builder: TagHierarchyBuilder, node, parent_item,
                         parent_key: str, by_id: dict, visible: set[str] | None) -> None:
        for child in node.sorted_children():
            child_key = f"{parent_key}>{child.name}"
            under = builder.find_profiles(child)
            if visible is not None:
                under = [i for i in under if i in visible]
                if not under:
                    continue
            tag_item = self._make_tag_item(parent_item, child.name, len(under), child_key)

            for pid in sorted(child.profile_ids):
                if visible is not None and pid not in visible:
                    continue
                meta = by_id.get(pid)
                if meta is not None:
                    self._add_profile_row(tag_item, meta)

            self._render_children(builder, child, tag_item, child_key, by_id, visible)
            self._set_expanded(tag_item, child_key, visible)

    def _add_untagged(self, parent_item, parent_key: str,
                      profiles: list[ProfileMeta], visible: set[str] | None) -> None:
        matching = [p for p in profiles if visible is None or p.id in visible]
        if not matching:
            return
        key = f"{parent_key}>{_UNTAGGED}"
        tag_item = self._make_tag_item(parent_item, "Untagged", len(matching), key)
        for meta in sorted(matching, key=lambda p: p.name.lower()):
            self._add_profile_row(tag_item, meta)
        self._set_expanded(tag_item, key, visible)

    def _make_tag_item(self, parent, name: str, count: int, key: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent) if parent is not None else QTreeWidgetItem(self.tree)
        item.setData(0, _ROLE_ID, None)
        item.setData(0, _ROLE_KEY, key)
        item.setData(0, _ROLE_BASE, f"{name}  ({count})")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)  # not selectable; click toggles
        item.setSizeHint(0, QSize(0, _tag_row_height()))
        item.setForeground(0, QColor(self.theme["text_secondary"]))
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        self._tag_items[key] = item
        self._sync_arrow(item)
        return item

    def _add_profile_row(self, parent_item, meta: ProfileMeta) -> None:
        item = QTreeWidgetItem(parent_item)
        item.setData(0, _ROLE_ID, meta.id)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        item.setSizeHint(0, QSize(0, _row_height()))
        widget = ProfileListItemWidget(meta, self.theme)
        widget.row_hover.connect(self._on_row_hover)
        self.tree.setItemWidget(item, 0, widget)
        self._item_by_id[meta.id] = item
        self._row_widgets[meta.id] = widget

    def _set_expanded(self, item: QTreeWidgetItem, key: str, visible: set[str] | None) -> None:
        # While searching, force every matching branch open; otherwise honour the
        # remembered state.
        expanded = True if visible is not None else (key in self._expanded_keys)
        item.setExpanded(expanded)
        self._sync_arrow(item)

    def _sync_arrow(self, item: QTreeWidgetItem) -> None:
        base = item.data(0, _ROLE_BASE)
        if base is None:
            return
        arrow = _EXPAND_ARROW if item.isExpanded() else _COLLAPSE_ARROW
        item.setText(0, f"{arrow}  {base}")

    # ── selection / activation handling ──────────────────────────────

    def _on_selection_changed(self) -> None:
        self._refresh_scrolling()
        # Only drive the editor for a single selection; extending a multi-
        # selection must not reload/activate each newly-touched profile.
        ids = self.selected_profile_ids()
        if len(ids) == 1:
            self.profile_selected.emit(ids[0])

    # ── marquee scrolling (highlighted or selected rows only) ─────────

    def _on_row_hover(self, profile_id: str, entered: bool) -> None:
        if entered:
            self._set_hovered(profile_id)
        elif self._hovered_id == profile_id:
            self._set_hovered(None)

    def _set_hovered(self, profile_id: str | None) -> None:
        if profile_id == self._hovered_id:
            return
        self._hovered_id = profile_id
        self._refresh_scrolling()

    def _refresh_scrolling(self) -> None:
        # Only ONE marquee may scroll at a time: the hovered row wins, falling
        # back to the selected row. Every other row is forced off, so focus
        # moving away from a row always stops its scroll.
        target = self._hovered_id if self._hovered_id in self._row_widgets \
            else self.selected_profile_id()
        for pid, widget in self._row_widgets.items():
            widget.set_scrolling(pid == target)
        # Force a full-viewport repaint. On a selection/hover change Qt only
        # invalidates each item's *visual rect* — which starts after the
        # indentation — so the indent gutter on the left keeps its stale (un-
        # highlighted) colour, leaving a mismatched strip beside the row. A full
        # repaint redraws every row's gutter in lockstep with its highlight, so
        # the selection tint always spans the entire row width.
        self.tree.viewport().update()

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        profile_id = item.data(0, _ROLE_ID)
        if profile_id:
            # Don't reload the editor mid multi-select (Ctrl/Shift+click).
            if len(self.selected_profile_ids()) <= 1:
                self.profile_selected.emit(profile_id)
        elif item.data(0, _ROLE_KEY) is not None:
            item.setExpanded(not item.isExpanded())  # toggle tag group

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        profile_id = item.data(0, _ROLE_ID)
        if profile_id:
            self.profile_activated.emit(profile_id)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, _ROLE_KEY)
        if key is not None and not self._search:
            self._expanded_keys.add(key)
        self._sync_arrow(item)

    def _on_item_collapsed(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, _ROLE_KEY)
        if key is not None and not self._search:
            self._expanded_keys.discard(key)
        self._sync_arrow(item)

    def _on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        clicked_id = item.data(0, _ROLE_ID)
        if not clicked_id:
            return
        # Windows behaviour: right-clicking a row outside the current selection
        # collapses the selection to just that row; right-clicking inside the
        # selection keeps it, so the menu acts on the whole multi-selection.
        if clicked_id not in self.selected_profile_ids():
            self.tree.clearSelection()
            item.setSelected(True)
            self.tree.setCurrentItem(item)
        ids = self.selected_profile_ids()
        if len(ids) > 1:
            self._show_multi_menu(pos, ids)
        else:
            self._show_single_menu(pos, clicked_id)

    def _show_single_menu(self, pos, profile_id: str) -> None:
        from PySide6.QtWidgets import QMenu
        meta = next((p for p in self._profiles if p.id == profile_id), None)
        if meta is None:
            return
        menu = QMenu(self)
        t = self.theme
        act_rename = menu.addAction(icons.icon("edit", t["text_secondary"], 14), "Rename…")
        act_duplicate = menu.addAction(icons.icon("content_copy", t["text_secondary"], 14), "Duplicate…")
        act_delete = menu.addAction(icons.icon("delete", t["text_secondary"], 14), "Delete…")
        menu.addSeparator()
        if meta.builtin:
            act_lock = menu.addAction(icons.icon("shield", t["text_muted"], 14),
                                      "Built-In — System Protected")
            act_lock.setEnabled(False)
        elif meta.readonly:
            act_lock = menu.addAction(icons.icon("lock_open", t["text_secondary"], 14), "Unlock Profile")
        else:
            act_lock = menu.addAction(icons.icon("lock", t["text_secondary"], 14), "Lock Profile")
        act_rename.setEnabled(not meta.protected)
        act_delete.setEnabled(not meta.protected)

        chosen = menu.exec(self.tree.mapToGlobal(pos))
        if chosen is act_rename:
            self.profile_rename_requested.emit(profile_id, "")
        elif chosen is act_duplicate:
            self.profile_duplicate_requested.emit(profile_id)
        elif chosen is act_delete:
            self.profile_delete_requested.emit(profile_id)
        elif chosen is act_lock:
            self.profile_lock_toggle_requested.emit(profile_id)

    def _show_multi_menu(self, pos, ids: list[str]) -> None:
        from PySide6.QtWidgets import QMenu
        metas = [p for p in self._profiles if p.id in ids]
        t = self.theme
        n = len(ids)
        deletable = [m for m in metas if not m.protected]
        lockable = [m for m in metas if not m.builtin and not m.readonly]
        unlockable = [m for m in metas if not m.builtin and m.readonly]

        menu = QMenu(self)
        act_dup = menu.addAction(icons.icon("content_copy", t["text_secondary"], 14),
                                 f"Duplicate {n} profiles")
        act_del = menu.addAction(icons.icon("delete", t["text_secondary"], 14),
                                 f"Delete {len(deletable)} profiles…")
        act_del.setEnabled(bool(deletable))
        act_lock = act_unlock = None
        if lockable or unlockable:
            menu.addSeparator()
        if lockable:
            act_lock = menu.addAction(icons.icon("lock", t["text_secondary"], 14),
                                      f"Lock {len(lockable)} profiles")
        if unlockable:
            act_unlock = menu.addAction(icons.icon("lock_open", t["text_secondary"], 14),
                                        f"Unlock {len(unlockable)} profiles")

        chosen = menu.exec(self.tree.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_dup:
            self.profiles_duplicate_requested.emit(ids)
        elif chosen is act_del:
            self.profiles_delete_requested.emit(ids)
        elif chosen is act_lock:
            self.profiles_lock_requested.emit(ids, True)
        elif chosen is act_unlock:
            self.profiles_lock_requested.emit(ids, False)

    # ── theming ──────────────────────────────────────────────────────

    def _apply_tree_style(self) -> None:
        t = self.theme
        # The highlight backgrounds are painted full-row in _FullRowTree.drawRow,
        # NOT via ::item:selected/::item:hover (those only cover the indented item
        # rect, so the tint would start partway across deeper rows). The stylesheet
        # here only sets borders, the transparent branch gutter and selected text
        # colour; the row fill colours are handed to the tree below.
        self.tree.setStyleSheet(
            "QTreeWidget { background: transparent; border: none; outline: none; }"
            "QTreeWidget::item { border: none; }"
            "QTreeWidget::branch { background: transparent; }"
            f"QTreeWidget::item:selected {{ color: {t['text_primary']}; }}"
        )
        self.tree.set_row_colors(t["accent_muted"], t["bg_raised"])
        # Stop the style filling a selected item with the palette Highlight colour
        # (a stray tab the stylesheet can't reach); drawRow supplies the fill.
        pal = self.tree.palette()
        pal.setColor(QPalette.ColorRole.Highlight, QColor(0, 0, 0, 0))
        self.tree.setPalette(pal)

    def set_theme(self, theme: dict) -> None:
        self.theme = theme
        self.btn_new.setIcon(icons.icon("add", theme["text_secondary"], 16))
        self._apply_tree_style()
        for widget in self._row_widgets.values():
            widget.theme = theme
        self._rebuild_tree()
