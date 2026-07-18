"""Self-test: SpectrumController's stereo reduction (_to_stereo_pair) and the
end-to-end stereo_level_ready plumbing, using the existing capture_factory
injection seam so no real hardware is needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QCoreApplication

from rumble.core import eq_math
from rumble.ui.spectrum_overlay import SpectrumController

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── _to_stereo_pair: shape handling ───────────────────────────────────────

mono = np.array([0.1, 0.2, 0.3])
pair = SpectrumController._to_stereo_pair(mono)
check(pair.shape == (3, 2), "mono block becomes (n, 2)")
check(np.array_equal(pair[:, 0], pair[:, 1]),
      "mono block is duplicated identically to both channels")

single_col = np.array([[0.1], [0.2]])
pair2 = SpectrumController._to_stereo_pair(single_col)
check(pair2.shape == (2, 2) and np.array_equal(pair2[:, 0], pair2[:, 1]),
      "single-column 2-D block is treated the same as mono")

multi = np.arange(24).reshape(4, 6).astype(float)
pair3 = SpectrumController._to_stereo_pair(multi)
check(pair3.shape == (4, 2) and np.array_equal(pair3, multi[:, :2]),
      ">2-channel block takes the first two columns as L/R")

# A sequence of differently-shaped blocks (the documented device-change
# scenario) must concatenate without raising.
try:
    blocks = [mono, np.array([[0.1, 0.2], [0.3, 0.4]])]
    combined = np.concatenate([SpectrumController._to_stereo_pair(b) for b in blocks], axis=0)
    check(combined.shape == (5, 2),
          "mono then stereo blocks concatenate fine after per-block reduction")
except ValueError:
    check(False, "mono then stereo blocks concatenate fine after per-block reduction")

# ── end-to-end: fake capture feeds stereo_level_ready ─────────────────────

app = QCoreApplication.instance() or QCoreApplication([])


class _FakeCapture:
    """Minimal `LoopbackCapture`-shaped stand-in: on `.start()`, immediately
    hands the controller one stereo block with a known L/R difference."""

    def __init__(self, on_block):
        self._on_block = on_block
        self.status = "stopped"

    def start(self, device_fragment=None):
        self.status = "running"
        n = 2048
        block = np.zeros((n, 2), dtype=np.float64)
        block[:, 0] = 0.5   # L: louder
        block[:, 1] = 0.05  # R: quieter
        self._on_block(block, 48000)

    def stop(self):
        self.status = "stopped"


received = []
ctrl = SpectrumController(eq_math.frequency_array(), sample_rate=48000,
                           capture_factory=_FakeCapture)
ctrl.stereo_level_ready.connect(lambda levels: received.append(levels))
ctrl.start(None)
ctrl._on_tick()  # drive the GUI-thread tick directly, no need for a real QTimer wait

check(len(received) == 1, "stereo_level_ready fired once after one tick")
if received:
    levels = received[0]
    check(levels.peak_l > levels.peak_r,
          f"louder L channel reports a higher peak_l than peak_r ({levels.peak_l} vs {levels.peak_r})")
    check(levels.rms_l > levels.rms_r,
          f"louder L channel reports a higher rms_l than rms_r ({levels.rms_l} vs {levels.rms_r})")

ctrl.stop()

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("All stereo-level checks passed.")
