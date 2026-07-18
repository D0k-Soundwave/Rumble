"""Self-test for convolution impulse-response analysis (core only, no Qt).

Run:  python tests/selftest_ir_analysis.py
Generates WAV files (PCM 16/24-bit via stdlib wave, float32 via a hand-rolled
RIFF writer) and checks the FFT-derived response: a unit impulse is flat at
0 dB, scaling shifts the whole curve by the exact dB amount, stereo channels
average, delay does not change magnitude, unreadable files yield None, and
results are cached per (path, mtime). Exits non-zero on failure.
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from rumble.core import eq_math, ir_analysis

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def write_pcm_wav(path: Path, channels: list[np.ndarray], fs: int = 48000,
                  sampwidth: int = 2) -> None:
    """Write float [-1, 1] channel arrays as a PCM WAV via stdlib wave."""
    n = len(channels[0])
    full_scale = float(2 ** (8 * sampwidth - 1) - 1)
    interleaved = np.empty(n * len(channels), dtype=np.int64)
    for i, ch in enumerate(channels):
        interleaved[i::len(channels)] = np.round(ch * full_scale).astype(np.int64)
    if sampwidth == 2:
        payload = interleaved.astype("<i2").tobytes()
    elif sampwidth == 3:
        as32 = interleaved.astype("<i4").tobytes()
        payload = b"".join(as32[i * 4:i * 4 + 3] for i in range(len(interleaved)))
    else:
        payload = interleaved.astype("<i4").tobytes()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(len(channels))
        wf.setsampwidth(sampwidth)
        wf.setframerate(fs)
        wf.writeframes(payload)


def write_float32_wav(path: Path, samples: np.ndarray, fs: int = 48000) -> None:
    """Hand-rolled RIFF writer for an IEEE-float (format 3) mono WAV, which
    the stdlib wave module cannot produce — exercises the fmt-3 read path."""
    data = samples.astype("<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, fs, fs * 4, 4, 32)
    body = (b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def impulse(gain: float = 1.0, n: int = 4096, at: int = 0) -> np.ndarray:
    buf = np.zeros(n)
    buf[at] = gain
    return buf


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_ir_"))
    freqs = eq_math.frequency_array()

    print("[1] PCM16 unit impulse -> flat 0 dB")
    p1 = tmp / "unit.wav"
    write_pcm_wav(p1, [impulse()])
    r1 = ir_analysis.get_ir_response(p1)
    check(r1 is not None, "unit impulse parses")
    check(r1.ir_sample_rate == 48000, "sample rate read from the file")
    check(len(r1.response_db) == len(freqs), "response sampled on the graph grid")
    check(float(np.max(np.abs(r1.response_db))) < 0.01,
          f"unit impulse is flat 0 dB (max dev {np.max(np.abs(r1.response_db)):.4f})")
    check(abs(r1.peak_db - float(np.max(r1.response_db))) < 1e-9,
          "peak_db matches the response maximum")

    print("[2] gain scaling is exact in dB")
    p2 = tmp / "half.wav"
    write_pcm_wav(p2, [impulse(0.5)])
    r2 = ir_analysis.get_ir_response(p2)
    check(r2 is not None and float(np.max(np.abs(r2.response_db + 6.0206))) < 0.02,
          "0.5-scaled impulse sits at -6.02 dB")

    print("[3] stereo channels average")
    p3 = tmp / "stereo.wav"
    write_pcm_wav(p3, [impulse(1.0), impulse(0.5)])
    r3 = ir_analysis.get_ir_response(p3)
    expected = 20 * np.log10(0.75)
    check(r3 is not None and float(np.max(np.abs(r3.response_db - expected))) < 0.02,
          "stereo averages to the mean channel gain")

    print("[4] delay does not change magnitude")
    p4 = tmp / "delayed.wav"
    write_pcm_wav(p4, [impulse(1.0, at=333)])
    r4 = ir_analysis.get_ir_response(p4)
    check(r4 is not None and float(np.max(np.abs(r4.response_db))) < 0.01,
          "delayed impulse is still flat 0 dB")

    print("[5] float32 (IEEE format 3) WAVs read")
    p5 = tmp / "float32.wav"
    write_float32_wav(p5, impulse(0.5))
    r5 = ir_analysis.get_ir_response(p5)
    check(r5 is not None and float(np.max(np.abs(r5.response_db + 6.0206))) < 0.02,
          "float32 impulse reads with exact gain")

    print("[6] 24-bit PCM reads")
    p6 = tmp / "pcm24.wav"
    write_pcm_wav(p6, [impulse(0.25)], sampwidth=3)
    r6 = ir_analysis.get_ir_response(p6)
    check(r6 is not None and float(np.max(np.abs(r6.response_db + 12.0412))) < 0.02,
          "24-bit impulse reads with exact gain")

    print("[7] failures are graceful")
    bad = tmp / "broken.wav"
    bad.write_bytes(b"RIFF-this-is-not-a-wav")
    check(ir_analysis.get_ir_response(bad) is None, "garbage file -> None")
    check(ir_analysis.get_ir_response(tmp / "missing.wav") is None,
          "missing file -> None")

    print("[8] cache: hit on same mtime, miss on change")
    hit = ir_analysis.get_ir_response(p1)
    check(hit is r1, "same path+mtime returns the cached result object")
    write_pcm_wav(p1, [impulse(0.5)])
    os.utime(p1, ns=(os.stat(p1).st_mtime_ns + 1_000_000,
                     os.stat(p1).st_mtime_ns + 1_000_000))
    fresh = ir_analysis.get_ir_response(p1)
    check(fresh is not None and fresh is not r1
          and float(np.max(np.abs(fresh.response_db + 6.0206))) < 0.02,
          "changed file re-analyses")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
