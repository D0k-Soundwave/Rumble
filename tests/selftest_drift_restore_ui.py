# tests/selftest_drift_restore_ui.py
"""Selftest: config.txt drift-snapshot listing (Rumble 4.1) — several
`config.txt.rumble_drift-<ts>` files on disk are listed newest-first with a
human-readable timestamp, and a chosen snapshot can be restored verbatim
back into config.txt (and RUMBLE stops re-managing it, so it isn't
immediately re-captured as a fresh drift event on next launch)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core.profile_manager import CONFIG_DRIFT_SUFFIX, CONFIG_FILENAME, ProfileManager


def _write_drift(apo_dir: Path, ts: str, text: str) -> Path:
    apo_dir.mkdir(parents=True, exist_ok=True)
    path = apo_dir / f"{CONFIG_FILENAME}{CONFIG_DRIFT_SUFFIX}-{ts}"
    path.write_text(text, encoding="utf-8")
    return path


def test_list_drift_snapshots_sorted_newest_first():
    with tempfile.TemporaryDirectory() as tmp:
        apo_dir = Path(tmp) / "apo"
        mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
        _write_drift(apo_dir, "20260101-090000", "Preamp: -3 dB\n")
        _write_drift(apo_dir, "20260301-120000", "Preamp: -6 dB\n")
        _write_drift(apo_dir, "20260201-153000", "Preamp: -1 dB\n")

        snapshots = mgr.list_drift_snapshots()
        assert [s.timestamp.isoformat() for s in snapshots] == sorted(
            (s.timestamp.isoformat() for s in snapshots), reverse=True)
        assert snapshots[0].timestamp.month == 3
        assert snapshots[-1].timestamp.month == 1
        # Human-readable label, not just the raw filename.
        assert all(s.label and s.label != s.path.name for s in snapshots)


def test_list_drift_snapshots_empty_when_none():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(Path(tmp) / "data", config_path=Path(tmp) / "apo")
        assert mgr.list_drift_snapshots() == []


def test_restore_drift_snapshot_writes_config_and_releases_management():
    with tempfile.TemporaryDirectory() as tmp:
        apo_dir = Path(tmp) / "apo"
        mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
        snap_path = _write_drift(apo_dir, "20260301-120000", "Preamp: -6 dB\n")

        assert mgr.restore_drift_snapshot(snap_path) is True
        assert (apo_dir / CONFIG_FILENAME).read_text(encoding="utf-8") == "Preamp: -6 dB\n"
        assert mgr.is_apo_managed() is False


def test_restore_drift_snapshot_missing_file_fails_cleanly():
    with tempfile.TemporaryDirectory() as tmp:
        apo_dir = Path(tmp) / "apo"
        mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
        assert mgr.restore_drift_snapshot(apo_dir / "config.txt.rumble_drift-99999999-000000") is False


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
