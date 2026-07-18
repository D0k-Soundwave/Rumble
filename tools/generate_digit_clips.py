"""Development-time script: synthesize the 10 digit-clip WAV assets used by
Test B (digit-triplet-in-noise screening). Run this ONCE; commit its output
(rumble/assets/audio/digits/*.wav) to the repo. Not part of the shipped app --
end users never run this, they only get the resulting fixed WAV files.

Usage: .venv\\Scripts\\python.exe tools\\generate_digit_clips.py
"""
from __future__ import annotations

import wave
from collections import deque
from pathlib import Path

import numpy as np
import comtypes.client

OUT_DIR = Path(__file__).resolve().parent.parent / "rumble" / "assets" / "audio" / "digits"
SAMPLE_RATE = 48000
DIGIT_WORDS = {0: "zero", 1: "one", 2: "two", 3: "three", 4: "four",
               5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
TARGET_RMS_DBFS = -20.0

# Peak-limiter parameters (see normalize_rms / peak_limit below). Attack is
# fast enough to catch a transient before it reaches full scale (a handful
# of milliseconds); release is slow enough that gain reduction doesn't pump
# audibly, but still short enough that it stays localized to the transient
# rather than smearing across the whole ~1.2-1.5s clip.
LIMITER_ATTACK_MS = 2.0
LIMITER_RELEASE_MS = 40.0
# Ceiling the limiter targets, in int16 counts. Left below the true 32767
# rail because .astype(np.int16) truncates toward zero rather than rounding,
# so a value scaled to exactly the rail truncates to a value indistinguishable
# from a clipped sample -- a few counts of margin avoids that ambiguity.
# (peak_limit's gain chain is a proven exact bound, not an approximation --
# see its docstring -- so this margin is purely about int16 truncation, not
# about absorbing limiter error.) 32760 leaves a ~0.0019 dB margin below full
# scale -- inaudible, and verified empirically (see task report) to be enough
# that the defensive np.clip() below never actually engages.
PEAK_CEILING = 32760.0


def _dbfs_to_amplitude(dbfs: float) -> float:
    return 10 ** (dbfs / 20.0)


def _sliding_min_forward(x: np.ndarray, window: int) -> np.ndarray:
    """out[n] = min(x[n : n + window]) (window looks forward from n, clipped
    at the array end), computed in O(len(x)) via a monotonic deque.

    This is the "lookahead" primitive: because it includes index n itself
    in the window, out[n] <= x[n] always -- a mathematical guarantee, not
    an approximation, which is what lets `peak_limit` below prove no sample
    can clip (see its docstring).
    """
    n = len(x)
    out = np.empty(n, dtype=np.float64)
    dq: deque = deque()  # indices, with x[dq[i]] strictly increasing
    for i in range(n - 1, -1, -1):
        while dq and x[dq[-1]] >= x[i]:
            dq.pop()
        dq.append(i)
        if dq[0] >= i + window:
            dq.popleft()
        out[i] = x[dq[0]]
    return out


def peak_limit(samples_f: np.ndarray, sample_rate: int, ceiling: float,
               attack_ms: float, release_ms: float) -> np.ndarray:
    """Localized downward peak limiter with lookahead.

    Unlike a global scale-factor backoff (which quiets the ENTIRE clip to
    accommodate its single loudest transient -- the approach this replaces),
    this only reduces gain in the vicinity of samples that would exceed
    `ceiling`, leaving the rest of the clip -- and therefore its overall
    RMS -- essentially untouched.

    Design note / lesson learned during this fix: a first version of this
    function tracked a one-pole IIR envelope of |signal| (classic
    fast-attack/slow-release envelope follower) and derived gain from that.
    It measurably FAILED on digit 6 ("six"): its sibilant "-ks" ending has
    genuine signal energy oscillating at roughly 1 kHz, and a millisecond-
    scale attack time constant low-pass-filters exactly that range, so the
    tracked envelope undershot the true per-sample peaks and let a sample
    clip at the negative int16 rail (-32768) -- verified empirically, not
    hypothetical. An IIR envelope is fundamentally an *approximation* of
    the peak; it does not bound it.

    This version instead computes the gain analytically and lookahead is a
    true sliding-window minimum, not a smoothing filter:

    1. `g_inst[n] = min(1.0, ceiling / |x[n]|)` -- the exact gain that, if
       applied at sample n, guarantees |x[n] * g_inst[n]| <= ceiling. This
       is per-sample and exact, not smoothed, so no fast transient can
       sneak past it.
    2. `hold_gain[n] = min(g_inst[n : n + lookahead])` -- a genuine sliding
       window minimum (`_sliding_min_forward`), not an IIR approximation.
       Because the window always includes n itself, `hold_gain[n] <=
       g_inst[n]` is guaranteed by construction, and because it looks
       `lookahead` samples ahead, gain reduction for an upcoming transient
       is already fully engaged `lookahead` samples before that transient
       arrives (the actual "lookahead" behavior) rather than reacting to it
       a few milliseconds late.
    3. A release-only one-pole smoothing pass turns the blocky `hold_gain`
       curve into a gradual recovery back toward unity gain after a
       transient passes (avoiding audible "pumping"/gain-stepping), while
       never smoothing PAST `hold_gain` on the way down -- so the
       mathematical guarantee from steps 1-2 is preserved: the final
       applied gain at every sample n is always <= g_inst[n], therefore
       |x[n] * gain[n]| <= ceiling for every single sample. This is a proof,
       not an empirical hope -- verified against the actual generated
       assets in the task report regardless.
    """
    n = len(samples_f)
    if n == 0:
        return samples_f

    abs_x = np.abs(samples_f)
    g_inst = np.ones(n, dtype=np.float64)
    over = abs_x > ceiling
    g_inst[over] = ceiling / abs_x[over]

    lookahead = max(1, int(round(attack_ms / 1000.0 * sample_rate)))
    hold_gain = _sliding_min_forward(g_inst, lookahead)

    release_coeff = np.exp(-1.0 / (release_ms / 1000.0 * sample_rate))
    gain = np.empty(n, dtype=np.float64)
    prev = 1.0
    for i in range(n):
        target = hold_gain[i]
        if target < prev:
            cur = target  # instant attack -- lookahead already anticipated it
        else:
            cur = release_coeff * prev + (1.0 - release_coeff) * target
            if cur > target:
                cur = target  # never smooth past the safety bound
        gain[i] = cur
        prev = cur

    return samples_f * gain


def synthesize_digit_wav_bytes(word: str) -> bytes:
    """Use SAPI (via comtypes) to synthesize `word` to a WAV byte stream at
    SAMPLE_RATE, 16-bit PCM, mono.

    The SpeechAudioFormatType enum's integer values are NOT documented
    stably enough to hardcode (a hardcoded `fmt.Type = 40` was tried during
    planning and empirically verified WRONG on a real machine -- 40 is
    SAFTTrueSpeech_8kHz1BitMono, a compressed 8kHz format, not 48kHz/16-bit/
    mono; the correct value on that machine was 38, but do not hardcode 38
    either). Instead, look up the named constant symbolically from the
    live COM type library, which is guaranteed correct regardless of which
    exact SAPI version/registration is on the machine running this script.
    """
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
    type_info = voice.GetTypeInfo(0)
    type_lib, _ = type_info.GetContainingTypeLib()
    sapi_mod = comtypes.client.GetModule(type_lib)
    fmt.Type = sapi_mod.SAFT48kHz16BitMono
    tmp_path = OUT_DIR / f"_tmp_{word}.wav"
    stream.Format = fmt
    stream.Open(str(tmp_path), 3)  # SSFMCreateForWrite = 3
    voice.AudioOutputStream = stream
    voice.Speak(word)
    stream.Close()
    data = tmp_path.read_bytes()
    tmp_path.unlink()
    return data


def normalize_rms(samples: np.ndarray, target_dbfs: float) -> np.ndarray:
    """Scale `samples` to hit `target_dbfs` RMS, then apply a localized
    peak limiter so no sample can exceed full scale (+-32767 for int16)
    without dragging the whole clip's level down.

    History: a pure RMS-target scale factor can clip on high-crest-factor
    speech transients (measured: digits 4/5/6 railed 14/126/15 samples at
    exactly +-32767 when scaled purely to -20 dBFS RMS). A first fix used
    `scale = min(rms_based_scale, peak_safe_scale)` -- a single scale factor
    applied to the WHOLE clip, chosen small enough that even the loudest
    instant never clips. That eliminated clipping but, for digit 5 ("five"),
    exposed a genuine ~10ms mid-word consonant transient whose crest factor
    forced the entire 1.4s clip down by ~3.17 dB to accommodate it --
    leaving "five" measurably quieter than the other 9 digits, which is a
    real problem for a hearing *screening* test where per-digit level
    consistency is what keeps one word from being spuriously easier or
    harder to hear (see the -5.55 dB SNR referral cutoff in the Phase 7
    spec, which assumes level-matched stimuli).

    This version instead scales to the exact RMS target first (as the
    original pre-clipping-fix code did), then runs `peak_limit()` on that
    RMS-scaled signal: an exact per-sample gain with true sliding-window
    lookahead (not an envelope-follower approximation -- an earlier attempt
    at that was tried and discarded, see peak_limit's docstring for why)
    that only pulls down gain in the immediate vicinity of a threatened
    transient, leaving the rest of the clip -- and therefore its overall
    RMS -- essentially untouched. The trade-off is honest, not hidden: a
    limiter reducing a transient's energy necessarily costs that clip a
    small amount of RMS relative to a hypothetical unclipped/unlimited
    scale, so this does NOT produce bit-identical RMS across all 10 digits
    -- but it should land digits with no threatened transient (0,1,2,3,7,8,9
    per prior measurement) unchanged, and bring digit 5 back within a small
    fraction of a dB of the rest, rather than the ~3.17 dB the global-backoff
    approach cost it. See tests/selftest_digit_clips.py and the task report
    for the actual measured spread.

    np.clip below is kept only as a defensive final clamp against float
    rounding / limiter-approximation error; it should not actually change a
    value once PEAK_CEILING and the limiter's attack/lookahead are
    parameterized correctly -- verified empirically, see task report.
    """
    samples_f = samples.astype(np.float64)
    rms = np.sqrt(np.mean(samples_f ** 2))
    if rms == 0:
        return samples

    target_amplitude = _dbfs_to_amplitude(target_dbfs) * 32768.0
    rms_scale = target_amplitude / rms
    rms_scaled = samples_f * rms_scale

    limited = peak_limit(rms_scaled, SAMPLE_RATE, PEAK_CEILING,
                          LIMITER_ATTACK_MS, LIMITER_RELEASE_MS)

    return np.clip(limited, -32768, 32767).astype(np.int16)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for digit, word in DIGIT_WORDS.items():
        raw_wav_bytes = synthesize_digit_wav_bytes(word)
        tmp_path = OUT_DIR / f"_raw_{digit}.wav"
        tmp_path.write_bytes(raw_wav_bytes)
        with wave.open(str(tmp_path), "rb") as wf:
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            samples = np.frombuffer(raw, dtype=np.int16)
        tmp_path.unlink()

        normalized = normalize_rms(samples, TARGET_RMS_DBFS)
        out_path = OUT_DIR / f"{digit}.wav"
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(normalized.tobytes())
        print(f"Wrote {out_path} ({len(normalized)} samples, "
              f"{len(normalized) / SAMPLE_RATE * 1000:.0f} ms)")


if __name__ == "__main__":
    main()
