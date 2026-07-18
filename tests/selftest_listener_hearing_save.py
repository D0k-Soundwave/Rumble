"""Selftest: Test A results save/load against the active Listener Slot."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from rumble.core import listener_slots
from rumble.core.hearing_test import FrequencyTest
from rumble.core.profile_manager import ProfileManager
from rumble.ui.hearing_dialog import HearingSuiteDialog
from rumble.ui.theme.tokens import THEMES, DEFAULT_THEME_NAME

_app = QApplication.instance() or QApplication([])


def _manager(tmp) -> ProfileManager:
    return ProfileManager(Path(tmp) / "data", config_path=Path(tmp) / "apo")


def test_finish_frequency_test_saves_against_active_slot():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.set_active_listener_slot(3)
        dlg = HearingSuiteDialog(mgr, THEMES[DEFAULT_THEME_NAME])
        dlg._ft = FrequencyTest(frequencies=[1000])
        dlg._ft.results = {"left": {1000: -30.0}, "right": {1000: -34.0}}
        dlg._finish_frequency_test()

        runs = listener_slots.load_slot_runs(mgr.data_dir, 3)
        assert len(runs) == 1
        assert runs[0]["raw"]["left"][1000] == -30.0
        # slot 1 (not active) must be untouched
        assert listener_slots.load_slot_runs(mgr.data_dir, 1) == []


def test_finish_frequency_test_invalidates_correction_cache_when_active_slot():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        assert mgr.get_active_correction().is_flat  # primes the cache empty
        dlg = HearingSuiteDialog(mgr, THEMES[DEFAULT_THEME_NAME])
        dlg._ft = FrequencyTest(frequencies=[1000])
        dlg._ft.results = {"left": {1000: -20.0}, "right": {1000: -34.0}}
        dlg._finish_frequency_test()
        assert not mgr.get_active_correction().is_flat


def test_start_with_checkbox_anchors_loudness_reference_from_conversation():
    # The intro page's "also use this volume as my loudness baseline"
    # checkbox (default on): Start captures the reference-clip-matched
    # volume and anchors the ISO 226 reference (default 80 phon) 5 dB
    # above it (80 - REFERENCE_CLIP_TARGET_PHON's 75).
    from rumble.core import master_volume
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        dlg = HearingSuiteDialog(mgr, THEMES[DEFAULT_THEME_NAME],
                                  tone_engine_factory=_FakeEngine)
        assert dlg.chk_set_loudness_ref.isChecked()   # default on
        real_db = master_volume.get_master_volume_db
        real_pct = master_volume.get_master_volume_percent
        master_volume.get_master_volume_db = lambda: -40.0
        master_volume.get_master_volume_percent = lambda: 30.0
        try:
            dlg._on_start_frequency_test()
        finally:
            master_volume.get_master_volume_db = real_db
            master_volume.get_master_volume_percent = real_pct
        assert mgr.settings["iso226_reference_volume_db"] == -35.0
        dlg._end_frequency_test()


def test_start_with_checkbox_off_leaves_loudness_reference_alone():
    from rumble.core import master_volume
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        dlg = HearingSuiteDialog(mgr, THEMES[DEFAULT_THEME_NAME],
                                  tone_engine_factory=_FakeEngine)
        dlg.chk_set_loudness_ref.setChecked(False)
        real_db = master_volume.get_master_volume_db
        real_pct = master_volume.get_master_volume_percent
        master_volume.get_master_volume_db = lambda: -40.0
        master_volume.get_master_volume_percent = lambda: 30.0
        try:
            dlg._on_start_frequency_test()
        finally:
            master_volume.get_master_volume_db = real_db
            master_volume.get_master_volume_percent = real_pct
        assert mgr.settings["iso226_reference_volume_db"] is None
        dlg._end_frequency_test()


class _FakeEngine:
    """Minimal ToneEngine stand-in (same shape as selftest_hearing_dialog's):
    never blocks, never touches real audio -- needed because
    _on_start_frequency_test constructs an engine and the new auto-play may
    present a tone through it."""

    def play(self, buffer, sample_rate, device=None, on_done=None):
        pass

    def stop(self):
        pass

    @property
    def is_playing(self):
        return False


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
