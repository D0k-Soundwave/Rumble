"""Rules editor for Phase 3 automatic profile switching: an ordered list of
`Rule`s (priority = list order, first match wins), each pairing a trigger
(app playing / default device / time of day) with a target profile.
"""
from __future__ import annotations

from PySide6.QtCore import QTime
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QStackedWidget,
    QTimeEdit, QVBoxLayout, QWidget,
)

from rumble.core.audio_devices import list_apo_devices
from rumble.core.auto_switch import Rule, generate_native_conditional, rules_from_settings
from rumble.ui.a11y import describe
from rumble.ui.density import dp
from rumble.ui.frameless import CHROME_STRIP_HEIGHT, FramelessDialog

#: (trigger key, display label), in the order the trigger combo/match-editor
#: stack presents them — index into one is an index into the other.
TRIGGERS: list[tuple[str, str]] = [
    ("app", "App playing"),
    ("device", "Default device"),
    ("time", "Time of day"),
]


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "heading")
    return label


def _rule_summary(rule: Rule, profile_name: str) -> str:
    if rule.trigger == "app":
        cond = f"App '{rule.match}' playing"
    elif rule.trigger == "device":
        cond = f"Default device matches '{rule.match}'"
    else:
        cond = f"Time {rule.match}"
    suffix = "" if rule.enabled else "  (disabled)"
    return f"{cond}  →  {profile_name}{suffix}"


class AutoSwitchDialog(FramelessDialog):
    """Ordered rule-list editor. `manager.settings["auto_switch_rules"]` is
    read on open and written (as a list of `Rule.to_dict()` dicts) on OK —
    the caller (main_window) is responsible for calling the adapter's
    `reload_rules()` afterward so the running controller picks up the edit."""

    def __init__(self, manager, theme: dict, playing_exes: set[str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Auto-Switch Rules")
        # Original resize() target plus FramelessDialog's chrome strip on
        # top, same pattern as settings_dialog.py's SettingsDialog. The
        # rule list (self.list_widget, built below) already scrolls
        # internally, so growing this fixed rather than clipping a long
        # rule list.
        self.setFixedSize(dp(600, 500), dp(560, 460) + CHROME_STRIP_HEIGHT)
        self.manager = manager
        self._theme = theme
        self._profiles = manager.list_profiles()
        self._rules = rules_from_settings(manager)

        root = QVBoxLayout(self.body)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)
        root.addWidget(_section_title("AUTO-SWITCH RULES"))

        hint = QLabel(
            "Rules are evaluated top to bottom — the first matching rule wins. "
            "When no rule matches, the profile you last chose manually stays active.")
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        body = QHBoxLayout()
        self.list_widget = QListWidget()
        describe(self.list_widget, "Rules",
                 "The ordered list of auto-switch rules. The first rule that "
                 "matches the current app/device/time wins.")
        body.addWidget(self.list_widget, 1)

        reorder_col = QVBoxLayout()
        self.btn_up = QPushButton("Up")
        self.btn_up.setProperty("class", "ghost")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        describe(self.btn_up, "Move rule up", "Give this rule higher priority.")
        self.btn_down = QPushButton("Down")
        self.btn_down.setProperty("class", "ghost")
        self.btn_down.clicked.connect(lambda: self._move(1))
        describe(self.btn_down, "Move rule down", "Give this rule lower priority.")
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setProperty("class", "ghost")
        self.btn_remove.clicked.connect(self._remove_selected)
        describe(self.btn_remove, "Remove rule", "Delete the selected rule.")
        for btn in (self.btn_up, self.btn_down, self.btn_remove):
            reorder_col.addWidget(btn)
        reorder_col.addStretch(1)
        body.addLayout(reorder_col)
        root.addLayout(body, 1)

        root.addWidget(_section_title("ADD RULE"))
        form = QHBoxLayout()

        self.cmb_trigger = QComboBox()
        for key, label in TRIGGERS:
            self.cmb_trigger.addItem(label, key)
        self.cmb_trigger.currentIndexChanged.connect(self._on_trigger_changed)
        describe(self.cmb_trigger, "Trigger type",
                 "What this rule watches: which app is playing audio, the "
                 "current default playback device, or the time of day.")
        form.addWidget(self.cmb_trigger)

        self.match_stack = QStackedWidget()

        self.cmb_app_match = QComboBox()
        self.cmb_app_match.setEditable(True)
        self.cmb_app_match.addItems(sorted(playing_exes or []))
        describe(self.cmb_app_match, "App executable",
                 "The process name to match (e.g. 'spotify.exe'), pre-filled "
                 "with apps currently playing audio — or type any exe name.")
        self.match_stack.addWidget(self.cmb_app_match)

        self.cmb_device_match = QComboBox()
        self.cmb_device_match.setEditable(True)
        for device in list_apo_devices():
            self.cmb_device_match.addItem(device.pattern)
        describe(self.cmb_device_match, "Device",
                 "The playback device this rule matches against the current "
                 "Windows default device.")
        self.match_stack.addWidget(self.cmb_device_match)

        time_widget = QWidget()
        time_row = QHBoxLayout(time_widget)
        time_row.setContentsMargins(0, 0, 0, 0)
        self.time_start = QTimeEdit(QTime(22, 0))
        self.time_end = QTimeEdit(QTime(7, 0))
        describe(self.time_start, "Start time", "When this time window begins.")
        describe(self.time_end, "End time",
                 "When this time window ends — may be earlier than the start "
                 "time, meaning the window crosses midnight.")
        time_row.addWidget(self.time_start)
        time_row.addWidget(QLabel("to"))
        time_row.addWidget(self.time_end)
        self.match_stack.addWidget(time_widget)

        form.addWidget(self.match_stack, 1)

        self.cmb_target = QComboBox()
        for meta in self._profiles:
            self.cmb_target.addItem(meta.name, meta.id)
        describe(self.cmb_target, "Target profile",
                 "The profile to activate when this rule matches.")
        form.addWidget(self.cmb_target)

        self.btn_enabled = QPushButton("Enabled")
        self.btn_enabled.setCheckable(True)
        self.btn_enabled.setChecked(True)
        self.btn_enabled.setProperty("class", "ghost")
        describe(self.btn_enabled, "Rule enabled", "Untick to keep the rule without deleting it.")
        form.addWidget(self.btn_enabled)

        root.addLayout(form)

        add_row = QHBoxLayout()
        self.btn_add = QPushButton(" Add Rule")
        self.btn_add.setProperty("class", "ghost")
        self.btn_add.clicked.connect(self._add_rule)
        describe(self.btn_add, "Add rule", "Append a new rule built from the fields above.")
        add_row.addWidget(self.btn_add)
        add_row.addStretch(1)
        root.addLayout(add_row)

        export_row = QHBoxLayout()
        self.btn_export_native = QPushButton(" Export as Native Conditional…")
        self.btn_export_native.setProperty("class", "ghost")
        self.btn_export_native.clicked.connect(self._export_native_conditional)
        describe(self.btn_export_native, "Export as native conditional",
                 "Generate an Equalizer APO If/ElseIf/Else block from the "
                 "device rules above, for pasting into a config by hand — a "
                 "self-contained alternative to RUMBLE's own rule engine. App "
                 "and time-of-day rules can't be expressed this way and are "
                 "listed separately.")
        export_row.addWidget(self.btn_export_native)
        export_row.addStretch(1)
        root.addLayout(export_row)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton(" OK")
        self.btn_ok.setProperty("class", "primary")
        self.btn_ok.clicked.connect(self._on_ok)
        button_row.addWidget(self.btn_cancel)
        button_row.addWidget(self.btn_ok)
        root.addLayout(button_row)

        self._refresh_list()

    def _on_trigger_changed(self, index: int) -> None:
        self.match_stack.setCurrentIndex(index)

    def _profile_name(self, profile_id: str) -> str:
        for meta in self._profiles:
            if meta.id == profile_id:
                return meta.name
        return profile_id

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for rule in self._rules:
            self.list_widget.addItem(
                QListWidgetItem(_rule_summary(rule, self._profile_name(rule.profile_id))))

    def _current_match(self) -> str:
        trigger = self.cmb_trigger.currentData()
        if trigger == "app":
            return self.cmb_app_match.currentText().strip()
        if trigger == "device":
            return self.cmb_device_match.currentText().strip()
        return f"{self.time_start.time().toString('HH:mm')}-{self.time_end.time().toString('HH:mm')}"

    def _add_rule(self) -> None:
        trigger = self.cmb_trigger.currentData()
        match = self._current_match()
        if not match:
            QMessageBox.warning(self, "Auto-Switch Rules", "Enter a match value for this rule.")
            return
        target = self.cmb_target.currentData()
        if target is None:
            QMessageBox.warning(self, "Auto-Switch Rules", "Create a profile first.")
            return
        self._rules.append(Rule(trigger, match, target, enabled=self.btn_enabled.isChecked()))
        self._refresh_list()
        self.list_widget.setCurrentRow(len(self._rules) - 1)

    def _remove_selected(self) -> None:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._rules):
            del self._rules[row]
            self._refresh_list()

    def _move(self, delta: int) -> None:
        row = self.list_widget.currentRow()
        new_row = row + delta
        if row < 0 or not (0 <= new_row < len(self._rules)):
            return
        self._rules[row], self._rules[new_row] = self._rules[new_row], self._rules[row]
        self._refresh_list()
        self.list_widget.setCurrentRow(new_row)

    def _on_ok(self) -> None:
        self.manager.settings["auto_switch_rules"] = [r.to_dict() for r in self._rules]
        self.manager.save()
        self.accept()

    def rules(self) -> list[Rule]:
        return list(self._rules)

    def _export_native_conditional(self) -> None:
        result = generate_native_conditional(
            self._rules, list_apo_devices(), self.manager.load_profile_config,
            baseline_profile_id=self.manager.settings.get("last_active_profile"))
        if not result.translated:
            QMessageBox.information(
                self, "Export as Native Conditional",
                "No device rules could be translated. Only rules whose "
                "trigger is 'Default device' (matching a currently known "
                "endpoint) can be exported this way — app and time-of-day "
                "rules have no Equalizer APO runtime equivalent.")
            return
        NativeConditionalPreviewDialog(result, self).exec()


class NativeConditionalPreviewDialog(FramelessDialog):
    """Read-only preview of a generated native If/ElseIf/Else/EndIf block,
    with a one-click copy — this is an export for manual pasting, not
    something RUMBLE writes into a profile itself."""

    def __init__(self, result, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Native Conditional Export")
        # Original content budget (420, 340) plus FramelessDialog's chrome
        # strip on top, same pattern as autoeq_dialog.py's DIALOG_SIZE.
        self.setFixedSize(dp(560, 460), dp(420, 340) + CHROME_STRIP_HEIGHT)

        root = QVBoxLayout(self.body)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)
        root.addWidget(_section_title("NATIVE CONDITIONAL EXPORT"))

        hint = QLabel(
            f"{len(result.translated)} device rule(s) translated below. "
            "Paste this block into a config.txt (or a Conditional block in "
            "the profile editor) to route by device with no RUMBLE process "
            "running.")
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.text_edit = QPlainTextEdit(result.text)
        self.text_edit.setReadOnly(True)
        self.text_edit.setProperty("class", "raw-text-edit")
        describe(self.text_edit, "Generated conditional block",
                 "The generated If/ElseIf/Else/EndIf text, read-only.")
        root.addWidget(self.text_edit, 1)

        if result.untranslatable:
            kinds = ", ".join(sorted({r.trigger for r in result.untranslatable}))
            warn = QLabel(
                f"{len(result.untranslatable)} rule(s) skipped ({kinds} "
                "trigger(s) have no native Equalizer APO runtime equivalent, "
                "or their device is no longer known).")
            warn.setProperty("class", "value-warning")
            warn.setWordWrap(True)
            root.addWidget(warn)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.btn_copy = QPushButton("Copy to Clipboard")
        self.btn_copy.setProperty("class", "ghost")
        self.btn_copy.clicked.connect(self._copy)
        self.btn_close = QPushButton(" Close")
        self.btn_close.setProperty("class", "primary")
        self.btn_close.clicked.connect(self.accept)
        button_row.addWidget(self.btn_copy)
        button_row.addWidget(self.btn_close)
        root.addLayout(button_row)

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.text_edit.toPlainText())
