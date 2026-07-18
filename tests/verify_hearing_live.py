"""Manual, human-in-the-loop verification of Phase 7's hearing test suite
(``rumble/core/hearing_test.py`` Test A + ``rumble/core/digit_screening.py``
Test B) against real Windows audio hardware. Follows
``verify_audio_live.py``'s house style.

What this script does:
  1. Plays a 1 s, -20 dBFS, 440 Hz reference tone left-channel-only, then
     right-channel-only (the same reference-tone check as
     ``verify_audio_live.py``), so channel routing is confirmed before the
     hearing-test-specific material.
  2. Runs one real Test A (``FrequencyTest``) staircase step: plays the
     standard set's first (ear, freq) pair three times at the staircase's
     starting level via ``present_tone()`` -- exactly like
     ``hearing_dialog.py``'s ``_on_play_tone`` -- then feeds a "heard"
     response back into the staircase and prints the resulting level, so
     both the audio and the staircase update can be confirmed.
  3. Runs one real Test B (``DigitScreeningTest``) trial: builds one triplet
     (seeded, so the printed ground-truth digits are reproducible across
     runs), mixes it with pink noise via ``mix_triplet_with_noise()`` at the
     staircase's starting SNR, plays it, and prints the ground-truth triplet
     plus its ``classify_srt()`` band so it can be compared against what was
     actually heard.

IMPORTANT -- what this script can and cannot confirm on its own (mirrors
``verify_audio_live.py``'s own framing): this is a *numeric sanity* + audio
check only. It can confirm the process doesn't crash or hang and that
playback reports finishing. It CANNOT confirm the tone/triplet is audibly
coming out of the correct physical channel, at a sane level, or that the
digits are actually intelligible in the noise -- that requires a human with
working speakers/headphones actually listening.

This script deliberately does NOT engage ``BypassGuard`` / a real
``ProfileManager`` bypass (unlike ``hearing_dialog.py``'s Test A flow) -- it
plays directly through ``ToneEngine``, the same as ``verify_audio_live.py``,
without touching a real Equalizer APO config directory. The bypass-engage/
restore behavior itself is already covered by
``tests/selftest_hearing_dialog.py``'s fake-engine tests; this script's job
is purely to confirm the actual generated audio is audible and correct by
ear.

Not part of the automated gate (tests/run_all_tests.py) -- it plays audible
sound and requires real playback hardware, so it is listed in that script's
EXCLUDE set.

Run directly:  python tests/verify_hearing_live.py
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core.audio import tones
from rumble.core.audio.engine import AudioError, ToneEngine
from rumble.core.digit_screening import (
    DigitScreeningTest, classify_srt, mix_triplet_with_noise,
)
from rumble.core.hearing_test import FrequencyTest, present_tone

SAMPLE_RATE = 48000
TONE_FREQ_HZ = 440.0
TONE_LEVEL_DBFS = -20.0
# Fixed so the printed Test B ground-truth triplet is reproducible run to run.
SCREENING_SEED = 42


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

    deadline = time.monotonic() + 5.0
    while not done["finished"] and time.monotonic() < deadline:
        time.sleep(0.05)
    if not done["finished"]:
        print("  WARNING: playback did not report finished within 5s, stopping it now.")
        engine.stop()
    else:
        print(f"  {label} finished.")


def main() -> int:
    print("Phase 7 hearing test suite live verification")
    print("=" * 60)
    print(f"Sample rate: {SAMPLE_RATE} Hz")
    print()

    engine = ToneEngine()

    # ── Part 1: reference tones (channel-routing sanity, as in verify_audio_live.py)
    left = tones.generate_sine(TONE_FREQ_HZ, 1.0, TONE_LEVEL_DBFS, SAMPLE_RATE, channel="left")
    right = tones.generate_sine(TONE_FREQ_HZ, 1.0, TONE_LEVEL_DBFS, SAMPLE_RATE, channel="right")
    play_and_wait(engine, left, "LEFT channel reference tone")
    play_and_wait(engine, right, "RIGHT channel reference tone")
    print()

    # ── Part 2: one real Test A staircase frequency point
    ft = FrequencyTest()
    current = ft.current
    if current is None or ft.staircase is None:
        print("FrequencyTest produced no (ear, freq) pair to test -- STANDARD_FREQS empty?")
        return 1
    ear, freq = current
    level = ft.staircase.level
    print(f"Test A: {ear} ear, {freq} Hz, starting level {level:.1f} dBFS, 3 presentations")
    for i in range(3):
        present_tone(engine, freq, level, ear, SAMPLE_RATE, duration_s=0.2)
        if i < 2:
            time.sleep(0.15)
    ft.respond(True)  # simulate a "heard" response so the staircase visibly updates
    next_level = ft.staircase.level if ft.staircase is not None else None
    print(f"  responded 'heard' -> next staircase level: {next_level}")
    print()

    # ── Part 3: one real Test B triplet-in-noise trial
    dst = DigitScreeningTest(rng=random.Random(SCREENING_SEED))
    triplet = dst.current_triplet
    snr = dst.staircase.level
    print(f"Test B: triplet {triplet[0]}-{triplet[1]}-{triplet[2]}, SNR {snr:.1f} dB, playing...")
    buffer = mix_triplet_with_noise(triplet, snr, SAMPLE_RATE)
    try:
        engine.play(buffer, SAMPLE_RATE, device=None)
    except AudioError as exc:
        print(f"  FAILED to start Test B playback ({exc.kind}): {exc}")
        return 1
    while engine.is_playing:
        time.sleep(0.01)
    print(f"  ground-truth triplet was {triplet[0]}-{triplet[1]}-{triplet[2]}")
    print(f"  (classify_srt({snr:.1f}) -> {classify_srt(snr)!r})")

    print()
    print("Done. Review:")
    print("  - Part 1: LEFT tone audible only in the left speaker/ear, RIGHT only right")
    print(f"  - Part 2: three short {freq} Hz beeps, audible only in the {ear} ear")
    print("  - Part 3: three spoken digits audible in noise -- do they match the")
    print("    printed ground-truth triplet above?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
