# tests/selftest_listener_backup.py
"""Selftest: the whole-data-dir backup zip automatically covers
data_dir/listeners/ with no special-casing (design spec §3.1's claim,
verified rather than just asserted)."""
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import listener_slots
from rumble.core.profile_manager import ProfileManager


def test_backup_zip_includes_listener_data():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(Path(tmp) / "data", config_path=Path(tmp) / "apo")
        listener_slots.save_slot_result(
            mgr.data_dir, 2, {"left": {}, "right": {}, "imbalance": {1000: 3.0}},
            {"left": {}, "right": {}})
        listener_slots.save_slot_names(mgr.data_dir, {2: "Sam"})

        zip_path = Path(tmp) / "backup.zip"
        assert mgr.backup_library(zip_path) is True
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert any("listeners/2/hearing/frequency" in n for n in names)
        assert any(n.endswith("listeners/names.json") for n in names)


def test_restore_brings_back_listener_data():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(Path(tmp) / "data", config_path=Path(tmp) / "apo")
        listener_slots.save_slot_result(
            mgr.data_dir, 2, {"left": {}, "right": {}, "imbalance": {1000: 3.0}},
            {"left": {}, "right": {}})
        zip_path = Path(tmp) / "backup.zip"
        assert mgr.backup_library(zip_path) is True

        mgr2 = ProfileManager(Path(tmp) / "data2", config_path=Path(tmp) / "apo2")
        assert mgr2.restore_library(zip_path) is True
        runs = listener_slots.load_slot_runs(mgr2.data_dir, 2)
        assert len(runs) == 1 and runs[0]["norm"]["imbalance"][1000] == 3.0


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
