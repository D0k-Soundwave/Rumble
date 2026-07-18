"""Selftest: LoudnessVolumeAdapter — the GUI-thread volume poller that keeps
the ISO 226 loudness layer tracking the Windows volume knob between the
normal render choke points.

Drives the tick logic directly (no timer waits) through the adapter's
volume_reader test seam and a fake manager, per the same seam convention as
tests/selftest_hearing_dialog.py's fake tone engine.

Run:  python tests/selftest_loudness_volume_adapter.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from rumble.ui.loudness_volume_adapter import LoudnessVolumeAdapter, REFRESH_THRESHOLD_DB


class _FakeManager:
    def __init__(self, enabled=True, reference=-20.0):
        self.settings = {
            "iso226_loudness_enabled": enabled,
            "iso226_reference_volume_db": reference,
        }
        self.refresh_calls = 0
        self.refresh_result = True

    def refresh_render_layers(self) -> bool:
        self.refresh_calls += 1
        return self.refresh_result


class _Volume:
    """Mutable volume source counting reads, for the volume_reader seam."""

    def __init__(self, db):
        self.db = db
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.db


def test_disabled_layer_never_reads_or_refreshes():
    mgr = _FakeManager(enabled=False)
    vol = _Volume(-20.0)
    adapter = LoudnessVolumeAdapter(mgr, volume_reader=vol)
    adapter._tick()
    assert vol.reads == 0 and mgr.refresh_calls == 0


def test_uncalibrated_layer_never_reads_or_refreshes():
    mgr = _FakeManager(reference=None)
    vol = _Volume(-20.0)
    adapter = LoudnessVolumeAdapter(mgr, volume_reader=vol)
    adapter._tick()
    assert vol.reads == 0 and mgr.refresh_calls == 0


def test_first_tick_renders_and_records_volume():
    mgr = _FakeManager()
    vol = _Volume(-20.0)
    adapter = LoudnessVolumeAdapter(mgr, volume_reader=vol)
    adapter._tick()
    assert mgr.refresh_calls == 1


def test_sub_threshold_wiggle_does_not_rerender():
    mgr = _FakeManager()
    vol = _Volume(-20.0)
    adapter = LoudnessVolumeAdapter(mgr, volume_reader=vol)
    adapter._tick()
    vol.db = -20.0 + (REFRESH_THRESHOLD_DB * 0.5)
    adapter._tick()
    assert mgr.refresh_calls == 1   # unchanged


def test_change_at_threshold_rerenders():
    mgr = _FakeManager()
    vol = _Volume(-20.0)
    adapter = LoudnessVolumeAdapter(mgr, volume_reader=vol)
    adapter._tick()
    vol.db = -20.0 - REFRESH_THRESHOLD_DB
    adapter._tick()
    assert mgr.refresh_calls == 2


def test_refused_refresh_is_retried_next_tick():
    # refresh_render_layers() returns False while previewing/bypassed --
    # the adapter must NOT record the volume then, so the refresh happens
    # once the manager allows it again, even if the volume stops moving.
    mgr = _FakeManager()
    vol = _Volume(-20.0)
    adapter = LoudnessVolumeAdapter(mgr, volume_reader=vol)
    mgr.refresh_result = False
    adapter._tick()
    assert mgr.refresh_calls == 1
    mgr.refresh_result = True
    adapter._tick()                 # same volume, but last attempt failed
    assert mgr.refresh_calls == 2


def test_unreadable_volume_is_skipped():
    mgr = _FakeManager()
    vol = _Volume(None)
    adapter = LoudnessVolumeAdapter(mgr, volume_reader=vol)
    adapter._tick()
    assert mgr.refresh_calls == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
