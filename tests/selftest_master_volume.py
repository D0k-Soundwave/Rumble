"""Selftest: rumble.core.master_volume -- WASAPI master-volume reading must
never raise, even with no working audio/COM stack (offscreen/CI)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import master_volume


def test_get_master_volume_db_never_raises():
    result = master_volume.get_master_volume_db()
    assert result is None or isinstance(result, float)


def test_get_master_volume_percent_never_raises():
    result = master_volume.get_master_volume_percent()
    assert result is None or isinstance(result, float)


def test_get_master_volume_percent_in_range_when_available():
    result = master_volume.get_master_volume_percent()
    if result is not None:
        assert 0.0 <= result <= 100.0


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
