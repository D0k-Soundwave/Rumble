"""Qt adapter bridging the Qt-free `DeviceFormatWatcher` (rumble.core) to the
Qt main thread.

WHY THIS CLASS EXISTS
---------------------
`rumble.core.device_format.DeviceFormatWatcher` is deliberately Qt-free: it
fires its zero-arg `on_change` callback on whatever thread Windows/COM hands
the notification to, and — after its 1.5 s debounce — on a
`threading.Timer` thread. That thread is **never** the Qt main (GUI) thread,
so the callback must never touch a widget directly. This adapter is the one
place that marshals that foreign-thread callback onto the Qt main thread and
re-broadcasts it as a plain Qt signal (`format_changed`) that UI code can
connect to safely.

THREADING CONTRACT (read this — Phase 3 copies this pattern)
------------------------------------------------------------
`DeviceFormatAdapter` is a `QObject` created on the Qt main thread, so its
*thread affinity* is the main thread. When `format_changed.emit()` is called
from a **different** thread (the watcher's `threading.Timer` thread), Qt's
default `Qt.AutoConnection` resolves — per-slot, at emit time — to
`Qt.QueuedConnection` for every slot whose receiver also lives on the main
thread. Queued means Qt packages the invocation and posts it to the
receiver-thread's event loop; the slot then runs on the main thread the next
time that event loop spins (`app.exec()` in production;
`QCoreApplication.processEvents()` in tests). Same-thread connections resolve
to `Qt.DirectConnection` and run inline — which is exactly why we must go
*through* `emit()` and never call a slot from the callback ourselves: calling
a slot directly from the Timer thread would run UI code on the Timer thread.

So the marshaling mechanism is: **plain `Signal.emit()` from the foreign
thread.** No `QMetaObject.invokeMethod`, no manual `QThread` juggling — the
signal/slot machinery does the cross-thread hop for us, *provided* this
object was created on the GUI thread and the connected slots' receivers live
on the GUI thread too (they do: they're the main window and its children).

This is proven, not assumed: `selftest_device_format.py`'s
`test_adapter_marshals_to_main_thread` calls `_on_change_from_com_thread()`
from a background `threading.Thread`, captures `QThread.currentThread()`
inside the connected slot, and asserts it equals the main thread captured
before the background thread started. See that test for the RED/GREEN
evidence.

The adapter itself renders nothing — it only says "something changed, go
re-query." Subscribers re-read `manager.effective_sample_rate()` /
`device_format.get_default_device_format()` themselves, mirroring the
watcher's own no-args callback convention.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from rumble.core.device_format import DeviceFormatWatcher


class DeviceFormatAdapter(QObject):
    """Owns a `DeviceFormatWatcher` and re-emits its foreign-thread callback
    as the Qt-main-thread `format_changed` signal.

    Usage::

        adapter = DeviceFormatAdapter()          # created on the GUI thread
        adapter.format_changed.connect(on_change)  # slot runs on GUI thread
        adapter.start()
        ...
        adapter.stop()                            # on teardown

    `format_changed` carries no payload: subscribers re-query current state.
    """

    #: Emitted on the Qt main thread whenever the live device format may have
    #: changed. No payload by design — subscribers re-query.
    format_changed = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        # The watcher fires `_on_change_from_com_thread` on a COM/Timer thread
        # (never the GUI thread). We own it so its lifetime matches ours.
        self._watcher = DeviceFormatWatcher(self._on_change_from_com_thread)

    def start(self) -> None:
        """Begin watching for device-format changes. Never raises — the
        underlying watcher swallows and logs registration failures and stays
        inert (retrying on a later `start()`)."""
        self._watcher.start()

    def stop(self) -> None:
        """Stop watching and cancel any pending debounce. Safe before
        `start()`, after a failed `start()`, or called twice."""
        self._watcher.stop()

    def _on_change_from_com_thread(self) -> None:
        """Watcher callback — invoked on a COM/`threading.Timer` thread, NOT
        the Qt main thread.

        The single job here is to hand off to the Qt main thread. We do that
        by emitting `format_changed`: because this QObject lives on the GUI
        thread and the emit happens on a different thread, Qt queues the
        connected slots onto the GUI thread's event loop (Qt.AutoConnection →
        Qt.QueuedConnection cross-thread). We must NOT call any slot directly
        here, or that UI code would run on this foreign thread. `emit()` is
        the only safe action.
        """
        self.format_changed.emit()
