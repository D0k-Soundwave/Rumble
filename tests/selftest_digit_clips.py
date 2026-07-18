import sys
import wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np

DIGITS_DIR = Path(__file__).resolve().parents[1] / "rumble" / "assets" / "audio" / "digits"

def test_all_ten_digit_clips_exist():
    for digit in range(10):
        assert (DIGITS_DIR / f"{digit}.wav").exists(), f"missing digit clip {digit}"

def test_clips_are_correct_format_and_nonsilent():
    for digit in range(10):
        with wave.open(str(DIGITS_DIR / f"{digit}.wav"), "rb") as wf:
            assert wf.getframerate() == 48000
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            n_frames = wf.getnframes()
            assert 0 < n_frames / 48000 < 2.0   # each clip under 2 seconds
            samples = np.frombuffer(wf.readframes(n_frames), dtype=np.int16)
            assert np.abs(samples).max() > 100   # not silence

def test_clips_are_rms_normalized_within_tolerance():
    levels = []
    for digit in range(10):
        with wave.open(str(DIGITS_DIR / f"{digit}.wav"), "rb") as wf:
            n_frames = wf.getnframes()
            samples = np.frombuffer(wf.readframes(n_frames), dtype=np.int16).astype(np.float64)
            rms = np.sqrt(np.mean(samples ** 2))
            levels.append(20 * np.log10(rms / 32768.0))
    # All 10 clips should be normalized to within a couple dB of each other --
    # this is what prevents one digit being systematically easier to hear.
    assert max(levels) - min(levels) < 2.0, f"digit RMS spread too wide: {levels}"

def test_clips_have_no_clipping():
    # Regression guard for a task-review finding: normalize_rms() previously
    # scaled purely to hit -20 dBFS RMS, which railed samples on
    # high-crest-factor transients (digits 4/5/6 measured at exactly
    # +-32767). A peak-safe-normalized natural speech clip should never
    # land exactly on the int16 rail -- if it does, something (re-)clipped.
    for digit in range(10):
        with wave.open(str(DIGITS_DIR / f"{digit}.wav"), "rb") as wf:
            n_frames = wf.getnframes()
            samples = np.frombuffer(wf.readframes(n_frames), dtype=np.int16)
            peak = np.abs(samples).max()
            assert peak < 32767, f"digit {digit} clipped: peak={peak}"

if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
