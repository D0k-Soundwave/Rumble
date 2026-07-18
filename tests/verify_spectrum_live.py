"""Manual, human-in-the-loop verification of Phase 6's live spectrum
analyzer pipeline (`SpectrumController` + `SpectrumShaper`) against real
Windows audio hardware: a real WASAPI loopback capture (`LoopbackCapture`,
Phase 4) feeding the real display-shaping smoothing (`SpectrumShaper`,
Task 1) exactly as the EQ graph's Live overlay does -- no fake capture
factory, no fake shaping.

What this script does:
  1. Starts a real `SpectrumController` (default `capture_factory`, i.e. a
     real `LoopbackCapture` following the current system default output
     device -- same as `EQGraph`'s own default construction).
  2. Waits `DURATION_S` seconds while you play real audio (music, pink
     noise, a tone generator -- anything with real spectral content)
     through that device.
  3. Once a second, prints the single loudest frequency/level pair from the
     latest `spectrum_ready` reading, so you can watch it track live.
  4. At the end, reads the controller's own decaying peak-hold trace
     (`SpectrumController.peak` -- the same data Task 3's "Peak hold"
     overlay curve draws) and prints the top 3 non-adjacent spectral peaks
     observed across the whole run, so a human can visually/audibly confirm
     those peaks correspond to what was actually played.

IMPORTANT -- what this script can and cannot confirm on its own:
  This is a *numeric sanity* check only, matching `verify_audio_live.py`'s
  own framing: it can confirm the process doesn't crash or hang, that
  capture starts against a real device, and that the printed peaks are
  plausible for whatever was playing. It CANNOT confirm on its own that the
  frequencies/levels are *correct* in an absolute sense -- a human must
  actually play something with known spectral content (e.g. a steady 1 kHz
  tone) and eyeball whether the printed peak locations make sense.

Not part of the automated gate (tests/run_all_tests.py) -- it captures real
audio and needs a human actually playing something while it runs, so it is
listed in that script's EXCLUDE set.

Run directly:  python tests/verify_spectrum_live.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QCoreApplication

from rumble.core import eq_math
from rumble.ui.spectrum_overlay import SpectrumController

DURATION_S = 8.0
FREQ_POINTS = eq_math.frequency_array()


def _top_peaks(freqs: np.ndarray, levels: np.ndarray, n: int = 3,
               min_separation_octaves: float = 1.0) -> list[tuple[float, float]]:
    """Greedily pick the `n` loudest points that are each at least
    `min_separation_octaves` away (in log2 frequency) from every point
    already picked.

    Picking the raw top-3 by value alone tends to return 3 neighbouring
    bins of the exact same physical peak -- `FREQ_POINTS` packs many
    log-spaced points across `eq_math`'s graph frequency range, so several
    of them sit within a few percent of each other near any single peak.
    """
    order = np.argsort(levels)[::-1]
    picked: list[int] = []
    for idx in order:
        f = freqs[idx]
        if all(abs(np.log2(f / freqs[p])) >= min_separation_octaves for p in picked):
            picked.append(idx)
        if len(picked) == n:
            break
    return [(freqs[p], levels[p]) for p in picked]


def main() -> int:
    print("Phase 6 live spectrum analyzer verification")
    print("=" * 60)
    print(f"Sample rate fallback: 48000 Hz, {len(FREQ_POINTS)} log-spaced display points")
    print()

    # QCoreApplication (not QApplication) is enough -- SpectrumController's
    # QTimer/signals only need QtCore's event loop, no GUI platform plugin.
    app = QCoreApplication.instance() or QCoreApplication([])

    latest = {"spectrum": None, "status": "stopped"}

    def on_spectrum(spec) -> None:
        latest["spectrum"] = spec

    def on_status(status: str) -> None:
        latest["status"] = status
        print(f"  [status] {status}")

    ctrl = SpectrumController(FREQ_POINTS, sample_rate=48000)
    ctrl.spectrum_ready.connect(on_spectrum)
    ctrl.status_changed.connect(on_status)

    ctrl.start(None)  # follow the current system default output device; status_changed
                      # fires synchronously (direct connection, same thread) during this
                      # call if the capture transitioned away from its initial "stopped"

    if latest["status"] == "unavailable":
        print("WARNING: loopback capture could not open the default output device.")
        print("(This can be a genuine negative result -- no loopback-capable")
        print("default device -- but there is nothing more this script can verify.)")
        ctrl.stop()
        return 1

    print(f"Capturing for {DURATION_S:.0f}s. Play some real audio now")
    print("(music, pink noise, a tone generator) through your default output device.")
    print()

    # Pump the Qt event loop by hand rather than QApplication.exec() -- this
    # is a bounded, self-terminating loop (deadline below), not a blocking
    # exec() that would need something else to quit() it.
    deadline = time.monotonic() + DURATION_S
    last_print = 0.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
        now = time.monotonic()
        if now - last_print >= 1.0 and latest["spectrum"] is not None:
            last_print = now
            spec = latest["spectrum"]
            idx = int(np.argmax(spec))
            remaining = deadline - now
            print(f"  t-{remaining:4.1f}s: loudest bin now "
                  f"{FREQ_POINTS[idx]:7.1f} Hz @ {spec[idx]:6.1f} dB")

    peak_trace = ctrl.peak  # snapshot before stop() (stop() doesn't touch it, but be explicit)
    ctrl.stop()

    print()
    print("Done capturing. Top 3 spectral peaks observed (decaying peak-hold trace):")
    if latest["spectrum"] is None:
        print("  (spectrum_ready never fired -- was anything actually playing?)")
        return 1

    for rank, (freq, level) in enumerate(_top_peaks(FREQ_POINTS, peak_trace), start=1):
        print(f"  #{rank}: {freq:8.1f} Hz  @  {level:6.1f} dB")

    print()
    print("Review: do these 3 peaks look like what you played? If you played a")
    print("steady known tone (e.g. 1 kHz), peak #1 should land close to that")
    print("frequency, and the level should track roughly with how loud it was.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
