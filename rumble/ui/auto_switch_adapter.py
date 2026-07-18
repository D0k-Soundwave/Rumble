"""Qt adapter bridging Phase 3's Qt-free auto-switch core (`RuleEngine`,
`SessionWatcher`, `AutoSwitchController` — `rumble/core/auto_switch.py`) to
the Qt main thread.

WHY THIS CLASS EXISTS / THREADING CONTRACT
-------------------------------------------
Copies `rumble/ui/device_format_adapter.py`'s proven pattern verbatim: read
that module's docstring for the full argument. In short, `SessionWatcher`
fires its `on_state` callback on its own polling thread, never the Qt main
thread. This `QObject` is created on the GUI thread, so emitting its
`playing_changed` signal from the watcher thread lets Qt's `Qt.AutoConnection`
resolve to a queued connection for the GUI-thread slot — the slot then runs
safely on the main thread the next time its event loop spins. We must only
ever call `.emit()` from the foreign thread, never a slot directly.

`AutoSwitchController.tick()` additionally needs a periodic heartbeat so
time-of-day and device rules re-evaluate even when the playing-app set never
changes — driven by a `QTimer` created here, which is already main-thread
native (no marshaling needed for its `timeout` signal).
"""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from rumble.core.auto_switch import AutoSwitchController, RuleEngine, SessionWatcher, rules_from_settings
from rumble.core.device_format import get_default_device_name

TICK_MS = 1000


class AutoSwitchAdapter(QObject):
    """Owns a `SessionWatcher` + `AutoSwitchController` for one running
    `ProfileManager`, and drives `tick()` on a main-thread `QTimer`.

    `playing_changed` and `switched` are plain Qt signals UI code connects
    to; `switched` carries `(profile_id, reason)` for tray/status toasts.
    """

    #: Re-broadcasts SessionWatcher.on_state, marshaled to the main thread.
    playing_changed = Signal(object)  # set[str]
    #: profile_id, reason ("rule" | "baseline") — always emitted from tick(),
    #: which itself runs on the main thread (QTimer.timeout), so this signal
    #: needs no cross-thread marshaling; re-emitted for a uniform surface.
    switched = Signal(str, str)

    def __init__(self, manager, parent: QObject | None = None):
        super().__init__(parent)
        self.manager = manager
        engine = RuleEngine(rules_from_settings(manager))
        self.controller = AutoSwitchController(manager, engine, on_switched=self._on_switched)
        self.controller.set_enabled(manager.settings.get("auto_switch_enabled", False))
        self._watcher = SessionWatcher(self._on_state_from_watcher_thread)
        self._playing: set[str] = set()
        self.playing_changed.connect(self._on_playing_changed)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        """Begin watching sessions and ticking. Never raises — the
        underlying watcher swallows and logs its own failures."""
        self._watcher.start()
        self._timer.start()

    def stop(self) -> None:
        """Stop watching and ticking. Safe before `start()` or called
        twice. Use this for a temporary pause (e.g. the header/tray toggle
        turning the feature off) — the controller stays subscribed to
        `manager.on_profile_activated` so its baseline still tracks manual
        picks made while paused, ready to resume correctly if re-enabled."""
        self._timer.stop()
        self._watcher.stop()

    def close(self) -> None:
        """Stop watching/ticking AND detach the controller from
        `manager.on_profile_activated` for good. Use this — not `stop()` —
        whenever this adapter's owner is being discarded permanently against
        a `ProfileManager` that outlives it (e.g. main_window's in-process
        interface-size rebuild, which recreates MainWindow against the same
        long-lived manager); see `AutoSwitchController.close()`."""
        self.stop()
        self.controller.close()

    def reload_rules(self) -> None:
        """Rebuild the RuleEngine from `manager.settings["auto_switch_rules"]`
        — call after the rules dialog saves changes."""
        self.controller.engine = RuleEngine(rules_from_settings(self.manager))

    def set_enabled(self, enabled: bool) -> None:
        self.controller.set_enabled(enabled)

    def set_editor_dirty(self, dirty: bool) -> None:
        self.controller.set_editor_dirty(dirty)

    def state(self) -> str:
        return self.controller.state()

    def is_holding(self) -> bool:
        return self.controller.is_holding()

    @property
    def playing(self) -> set[str]:
        """The most recently observed set of actively-playing exe names —
        used to pre-fill the rules dialog's app-match combo."""
        return self._playing

    def _on_state_from_watcher_thread(self, exes: set[str]) -> None:
        """SessionWatcher callback — runs on the polling thread, NOT the Qt
        main thread. The only safe action here is `.emit()`."""
        self.playing_changed.emit(exes)

    def _on_playing_changed(self, exes) -> None:
        self._playing = exes

    def _on_switched(self, profile_id: str, reason: str) -> None:
        self.switched.emit(profile_id, reason)

    def _tick(self) -> None:
        now = time.monotonic()
        clock_hhmm = datetime.now().strftime("%H:%M")
        default_device = get_default_device_name()
        self.controller.tick(self._playing, default_device, now, clock_hhmm)
