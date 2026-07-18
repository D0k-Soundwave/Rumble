"""Manual, human-in-the-loop verification of the Phase 4 audio engine
against real Windows audio hardware: playback (tones.py + engine.py,
sounddevice/PortAudio) and loopback capture (capture.py, PyAudioWPatch).

What this script does:
  1. Plays a 1 s, -20 dBFS, 440 Hz tone through the default output device:
     left channel only, then right channel only, then both.
  2. Plays ~4 s of -20 dBFS pink noise through the default output device
     while simultaneously starting LoopbackCapture on the default device,
     printing the measured rms_dbfs of each second's worth of captured
     audio.

IMPORTANT -- what this script can and cannot confirm on its own:
  This is a *numeric sanity* check only. It can confirm the process doesn't
  crash or hang, that playback reports finishing, and that the loopback
  capture's measured RMS is in a plausible range (roughly -20 dBFS during
  the tone phases if you have a mic/loopback path measuring it, and clearly
  above the -120 dBFS silence floor during the pink-noise loopback phase).
  It CANNOT confirm the tone is audibly coming out of the correct physical
  channel (left speaker vs right speaker) -- that requires a human with
  working speakers/headphones actually listening. A human must run this
  script once on their own machine, listening on stereo headphones or
  speakers, and visually confirm the printed numbers look sane, before this
  is considered verified end-to-end.

Not part of the automated gate (tests/run_all_tests.py) -- it plays audible
sound and requires real playback + loopback hardware, so it is listed in
that script's EXCLUDE set.

Run directly:  python tests/verify_audio_live.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core.audio import tones
from rumble.core.audio.capture import LoopbackCapture
from rumble.core.audio.engine import AudioError, ToneEngine
from rumble.core.audio.metering import rms_dbfs

SAMPLE_RATE = 48000
TONE_FREQ_HZ = 440.0
TONE_LEVEL_DBFS = -20.0
NOISE_LEVEL_DBFS = -20.0


def play_and_wait(engine: ToneEngine, buffer, label: str) -> None:
    print(f"Playing {label}...")
    done = {"finished": False}

    def on_done():
        done["finished"] = True

    try:
        engine.play(buffer, SAMPLE_RATE, device=None, on_done=on_done)
    except AudioError as exc:
        print(f"  FAILED to start playback ({exc.kind}): {exc}")
        return

    # Poll rather than block forever: the callback thread sets done via
    # on_done, but we still want a hard ceiling in case something wedges.
    deadline = time.monotonic() + 5.0
    while not done["finished"] and time.monotonic() < deadline:
        time.sleep(0.05)
    if not done["finished"]:
        print("  WARNING: playback did not report finished within 5s, stopping it now.")
        engine.stop()
    else:
        print(f"  {label} finished.")


def main() -> int:
    print("Phase 4 audio engine live verification")
    print("=" * 60)
    print(f"Sample rate: {SAMPLE_RATE} Hz, tone {TONE_FREQ_HZ} Hz @ {TONE_LEVEL_DBFS} dBFS")
    print()

    engine = ToneEngine()

    left = tones.generate_sine(TONE_FREQ_HZ, 1.0, TONE_LEVEL_DBFS, SAMPLE_RATE, channel="left")
    right = tones.generate_sine(TONE_FREQ_HZ, 1.0, TONE_LEVEL_DBFS, SAMPLE_RATE, channel="right")
    both = tones.generate_sine(TONE_FREQ_HZ, 1.0, TONE_LEVEL_DBFS, SAMPLE_RATE, channel="both")

    play_and_wait(engine, left, "LEFT channel")
    play_and_wait(engine, right, "RIGHT channel")
    play_and_wait(engine, both, "BOTH channels")

    print()
    print("Capturing loopback while playing pink noise...")

    noise_duration_s = 4.0
    noise = tones.generate_pink_noise(noise_duration_s, NOISE_LEVEL_DBFS, SAMPLE_RATE)

    capture_seconds = [0.0]  # mutable box: seconds' worth of samples buffered
    blocks_this_second: list = []
    second_counter = {"n": 0}

    import numpy as np

    def on_block(block: "np.ndarray", sample_rate: int) -> None:
        blocks_this_second.append(block)
        capture_seconds[0] += len(block) / sample_rate
        if capture_seconds[0] >= 1.0:
            combined = np.concatenate(blocks_this_second, axis=0)
            blocks_this_second.clear()
            capture_seconds[0] = 0.0
            second_counter["n"] += 1
            level = rms_dbfs(combined)
            print(f"  Capturing loopback while playing pink noise, second {second_counter['n']}: rms={level:.1f} dBFS")

    capture = LoopbackCapture(on_block)

    try:
        engine.play(noise, SAMPLE_RATE, device=None)
    except AudioError as exc:
        print(f"  FAILED to start pink noise playback ({exc.kind}): {exc}")
        return 1

    capture.start(None)
    if capture.status == "unavailable":
        print("  WARNING: loopback capture could not open the default device (status='unavailable').")
    else:
        print(f"  Loopback capture status: {capture.status}")

    time.sleep(noise_duration_s + 0.5)

    capture.stop()
    engine.stop()

    print()
    print("Done. Review the printed levels above:")
    print("  - the three tone phases should have been audible in the correct")
    print("    channel(s) if you were listening on stereo headphones/speakers")
    print("    (this script cannot confirm that part itself -- a human must)")
    print("  - the loopback capture rms values should be clearly above the")
    print("    -120 dBFS silence floor while pink noise was playing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
