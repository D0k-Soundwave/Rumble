"""Hardware spike for the S22 BLOCKING follow-up: does Equalizer APO
actually honor `Stage: capture` combined with `Device: <pattern>` in one
dispatch block, the shape D2's capture-device assignments render
(ProfileManager._render_dispatch())?

KB SS20's live spike (S18) only exercised `Device:` alone, on Render
endpoints; no Capture endpoint was APO-hooked at S22 time, so D2 shipped
render-side and unit-tested only. This script answers KB SS22.3's two open
questions on real hardware:

  (a) Does a `Stage: capture` + `Device: <mic pattern>` block scope to
      JUST that endpoint's capture stream (not silently ignore the Stage:
      line, not leak to the Render stream)?
  (b) Does a Device:-scoped block with NO Stage: line reach the capture
      stream by default? The APO Configuration reference says the initial
      stages are "post-mix and capture", which would mean the existing
      unscoped `Device: all` fallback block already processes every hooked
      capture endpoint with the global playback profile -- a pre-existing
      behavior D2 documented but never confirmed live.

Methodology (mirrors S18's spike staging, but measured, not listened-to):
unlike the playback spikes, a capture-side effect can be VERIFIED
PROGRAMMATICALLY -- play a fixed two-tone probe (250 Hz + 4000 Hz) through
a speaker, record it through the APO-hooked microphone, and measure the
4 kHz : 250 Hz band-level ratio of the recording. A `LP 500 Hz` marker
filter on the capture stream crushes the 4 kHz probe (2nd-order low-pass,
3 octaves above corner, roughly -36 dB) while leaving 250 Hz alone, so
"did the block apply to the mic?" becomes a >=12 dB shift in one measured
number instead of a human listening judgment. The ratio (not the raw 4 kHz
level) is compared across phases so overall level differences between
recordings cancel out.

Phases (each: swap config via the same rumble_active.txt Include:
indirection ProfileManager itself uses, wait for APO's hot-reload, play
probe, record, measure):

  0. Flat reference        -> baseline ratio + acoustic-path sanity check
                              (both probe tones must clear the local noise
                              floor, else abort: mic too far / volume low).
  1. Stage: capture + Device: <mic>  LP 500 (+ flat Device: all fallback,
     D2's exact dispatch shape)
                           -> ratio DROP expected: combined scoping works
                              and reaches the capture stream (question a).
  2. Stage: post-mix + Device: <mic> LP 500 (+ flat fallback)
                           -> NO drop expected: post-mix does not exist
                              for capture devices, so an honored Stage:
                              line makes the block inert here. A drop in
                              THIS phase means APO ignores Stage: when
                              combined with Device: -- exactly the failure
                              KB SS22.3(a) worries about.
  3. Device: <mic> LP 500, no Stage: line (+ flat fallback)
                           -> answers (b): a drop means a stage-unscoped
                              Device: block reaches capture streams by
                              default (the documented "post-mix and
                              capture" initial stages), so the real
                              `Device: all` fallback does too. This uses
                              the mic's own pattern rather than `all` to
                              avoid the confound where `all` would ALSO
                              low-pass the playback device carrying the
                              probe (post-mix), killing 4 kHz in the room
                              itself before the mic could see it.

If a second active APO-hooked capture endpoint exists, phase 1 also
records from it as a bonus check that the block scoped to only its own
endpoint (no drop expected on mic 2).

The Render stream side of question (a) -- the same block must NOT muffle
playback -- is covered by phase 2 vs phase 1 logically (Stage: honored
means capture-only), and audibly: the probe is played through the room on
every phase, so a listener present during the run would immediately hear
any phase where playback lost its 4 kHz tone. The script prints a note to
that effect but does not require the listener.

Preconditions: Equalizer APO installed; at least one ACTIVE APO-hooked
Capture endpoint (hook one first: run Equalizer APO's Configurator.exe as
admin, tick the microphone on the Capture devices tab, reboot/restart the
audio service when prompted); the mic physically able to hear the default
playback device; a reasonably quiet room for the ~30 s this takes.

The real rumble_active.txt bytes are read before the first swap and
restored verbatim in a finally block, and all scratch files are deleted on
the way out -- the same restore guarantee verify_device_dispatch_live.py
gives.

Not part of the automated gate (tests/run_all_tests.py EXCLUDE set): plays
audible sound and needs real, physically-coupled speaker+mic hardware.

Run directly:  python tests/verify_capture_dispatch_live.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, apo_path, audio_devices
from rumble.core.audio import tones
from rumble.core.audio.engine import AudioError, ToneEngine
from rumble.core.crossover import BUTTERWORTH_Q
from rumble.core.profile_manager import (
    ACTIVE_POINTER_FILENAME,
    RUMBLE_SUBDIR,
    ProfileManager,
)

PROBE_LOW_HZ = 250.0
PROBE_HIGH_HZ = 4000.0
PROBE_LEVEL_DBFS = -16.0   # two tones sum to ~0.32 peak, comfortable headroom
PROBE_DURATION_S = 3.5
RECORD_LEAD_IN_S = 0.5
RECORD_TRAIL_S = 0.5

# LP marker corner: 3 octaves below the high probe (2nd-order Butterworth
# low-pass is -12 dB/octave, so ~-36 dB at 4 kHz) and 1 octave above the low
# probe (leaves 250 Hz essentially untouched).
MARKER_LP_HZ = 500.0

# APO's hot-reload watcher needs a moment after a config write before the
# new chain is live; S18's spike observed reloads landing well inside 2 s.
RELOAD_SETTLE_S = 2.0

# Verdict thresholds on the change in (4 kHz - 250 Hz) band ratio vs the
# flat baseline. The marker predicts ~-36 dB; anything past -12 dB cannot be
# room variance, and a genuinely inert block stays within a few dB.
APPLIED_DROP_DB = 12.0
INERT_MAX_DROP_DB = 6.0

# Baseline sanity: each probe band must clear its local noise floor by this
# much, or the acoustic path is too weak to support any verdict.
MIN_PROBE_SNR_DB = 10.0

SCRATCH_NAME = "rumble_verify_capture_dispatch.txt"


def build_block(*, stage: str | None, device: str,
                lp: bool) -> apo_config.ProfileConfig:
    filters = [apo_config.Filter(type="LP", fc=MARKER_LP_HZ, q=BUTTERWORTH_Q)] if lp else []
    section = apo_config.ChannelSection(channel="all", filters=filters)
    return apo_config.ProfileConfig(stage=stage, device=device,
                                    channel_sections=[section])


def render_phase_config(mic_pattern: str, *, stage: str | None,
                        scoped_lp: bool) -> str:
    """One mic-scoped block (optionally Stage:-prefixed, optionally carrying
    the LP marker) followed by a flat `Device: all` fallback -- the exact
    two-block shape ProfileManager._render_dispatch() writes for a single
    capture assignment, assignment block first (KB SS20.3 first-claim
    order)."""
    blocks = []
    if scoped_lp:
        blocks.append(apo_config.render_config_body(
            build_block(stage=stage, device=mic_pattern, lp=True)))
    blocks.append(apo_config.render_config_body(
        build_block(stage=None, device="all", lp=False)))
    return "\n".join(blocks)


def find_input_index(device: audio_devices.ApoDevice) -> tuple[int | None, str]:
    """Resolve an APO capture endpoint to a sounddevice input index, the
    input-side mirror of verify_device_dispatch_live.resolve_playback_index:
    full deviceName+connectionName word match first, then deviceName alone."""
    import sounddevice as sd

    def match(fragment: str) -> int | None:
        wanted = fragment.lower().split()
        for idx, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) <= 0:
                continue
            name = str(info.get("name", "")).lower()
            if all(word in name for word in wanted):
                return idx
        return None

    idx = match(device.pattern)
    if idx is not None:
        return idx, f"full pattern {device.pattern!r}"
    if device.device_name:
        idx = match(device.device_name)
        if idx is not None:
            return idx, f"device name only {device.device_name!r}"
    return None, "default input (no sounddevice name match found)"


def record_blocking(duration_s: float, sample_rate: int,
                    device: int | None) -> np.ndarray:
    """Blocking mono capture -- same plain-sounddevice shape as
    time_alignment._default_record (a real input endpoint, none of
    capture.py's WASAPI-loopback device resolution)."""
    import sounddevice as sd

    frames = int(round(duration_s * sample_rate))
    rec = sd.rec(frames, samplerate=sample_rate, channels=1,
                 dtype="float32", device=device)
    sd.wait()
    return rec[:, 0]


def band_level_db(recording: np.ndarray, sample_rate: int,
                  freq_hz: float, width_hz: float = 40.0) -> float:
    """Level (dB, arbitrary reference) of the +-width_hz band around
    freq_hz, from a Hann-windowed rFFT of the steady middle of the
    recording (edges hold the fades plus reload/scheduling slop)."""
    n = len(recording)
    mid = recording[n // 4: 3 * n // 4].astype(np.float64)
    windowed = mid * np.hanning(len(mid))
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(len(mid), 1.0 / sample_rate)
    mask = (freqs >= freq_hz - width_hz) & (freqs <= freq_hz + width_hz)
    energy = float(np.sum(spectrum[mask] ** 2))
    return 10.0 * np.log10(max(energy, 1e-30))


def noise_floor_db(recording: np.ndarray, sample_rate: int,
                   freq_hz: float) -> float:
    """Local noise reference: the higher of the two sidebands one probe
    bandwidth away on each side (higher = conservative SNR estimate)."""
    return max(
        band_level_db(recording, sample_rate, freq_hz - 120.0),
        band_level_db(recording, sample_rate, freq_hz + 120.0),
    )


def write_pointer_with_retry(pointer_path: Path, data: bytes | str, *,
                             retries: int = 5) -> None:
    """Same transient-WinError-5 retry verify_device_dispatch_live.py added
    after S18 hit APO's hot-reload watcher holding a brief read lock."""
    mode = "w" if isinstance(data, str) else "wb"
    kwargs = {"encoding": "utf-8"} if isinstance(data, str) else {}
    for attempt in range(retries):
        try:
            with open(pointer_path, mode, **kwargs) as f:
                f.write(data)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.3)


def run_probe_phase(engine: ToneEngine, probe: np.ndarray, sample_rate: int,
                    input_index: int | None) -> np.ndarray:
    """Play the probe through the default output while recording the mic --
    time_alignment.run_measurement's own record-thread-first sequencing."""
    record_seconds = RECORD_LEAD_IN_S + PROBE_DURATION_S + RECORD_TRAIL_S
    result: dict[str, np.ndarray] = {}
    error: dict[str, BaseException] = {}

    def _do_record() -> None:
        try:
            result["captured"] = record_blocking(record_seconds, sample_rate,
                                                 input_index)
        except Exception as exc:  # pragma: no cover - real hardware path
            error["exc"] = exc

    rec_thread = threading.Thread(target=_do_record, daemon=True)
    rec_thread.start()
    time.sleep(RECORD_LEAD_IN_S)
    engine.play(probe, sample_rate)
    while engine.is_playing:
        time.sleep(0.01)
    rec_thread.join(timeout=record_seconds + 5.0)
    if "captured" not in result:
        detail = f": {error['exc']}" if "exc" in error else " (timed out)"
        raise RuntimeError(f"Mic recording failed{detail}")
    return result["captured"]


def main() -> int:
    print("RUMBLE capture-dispatch hardware spike (S22 BLOCKING follow-up)")
    print("=" * 68)

    if not apo_path.is_apo_installed():
        print("Equalizer APO does not appear to be installed. Aborting.")
        return 1

    capture_devices = [d for d in audio_devices.list_apo_devices()
                       if d.flow == "Capture" and d.is_active]
    if not capture_devices:
        print("No ACTIVE APO-hooked Capture endpoint found.")
        print("Hook a microphone first: run Equalizer APO's Configurator.exe")
        print("(as admin), tick the mic on the Capture devices tab, restart")
        print("audio when prompted, then re-run this script. Aborting.")
        return 1

    mic = capture_devices[0]
    print(f"Mic under test: {mic.label}")
    print(f"  pattern: {mic.pattern!r}")
    input_index, how = find_input_index(mic)
    print(f"  sounddevice input resolution: index={input_index!r} ({how})")
    if input_index is None:
        print("  WARNING: recording will use the system default input, which")
        print("  may not be the APO-hooked endpoint -- results unreliable")
        print("  unless the default input IS the mic under test.")
    second_mic = capture_devices[1] if len(capture_devices) > 1 else None

    root_dir = Path(__file__).resolve().parents[1]
    manager = ProfileManager(data_dir=root_dir / "data")
    sample_rate = manager.effective_sample_rate()
    print(f"Equalizer APO config path: {manager.config_path}")
    print(f"Sample rate: {sample_rate} Hz")
    print()
    print("Playing the two-tone probe through the DEFAULT playback device on")
    print("every phase. Keep the room quiet; if you are listening, note that")
    print("the played sound itself should stay identical across all phases --")
    print("if any phase audibly loses its high tone in the ROOM, a capture-")
    print("scoped block leaked into the playback chain (report that).")
    print()

    probe = (tones.generate_sine(PROBE_LOW_HZ, PROBE_DURATION_S,
                                 PROBE_LEVEL_DBFS, sample_rate)
             + tones.generate_sine(PROBE_HIGH_HZ, PROBE_DURATION_S,
                                   PROBE_LEVEL_DBFS, sample_rate))

    pointer_path = manager.config_path / ACTIVE_POINTER_FILENAME
    scratch_path = manager.rumble_dir / SCRATCH_NAME
    original_pointer_bytes = (pointer_path.read_bytes()
                              if pointer_path.exists() else None)

    phases = [
        ("PHASE 0 -- flat reference (baseline)",
         render_phase_config(mic.pattern, stage=None, scoped_lp=False)),
        ("PHASE 1 -- Stage: capture + Device: <mic>  (D2's dispatch shape)",
         render_phase_config(mic.pattern, stage="capture", scoped_lp=True)),
        ("PHASE 2 -- Stage: post-mix + Device: <mic>  (must be inert on a mic)",
         render_phase_config(mic.pattern, stage="post-mix", scoped_lp=True)),
        ("PHASE 3 -- Device: <mic>, no Stage: line  (default-stages probe)",
         render_phase_config(mic.pattern, stage=None, scoped_lp=True)),
    ]

    engine = ToneEngine()
    ratios: list[float] = []
    second_mic_ratio: float | None = None
    second_mic_baseline: float | None = None
    try:
        manager.rumble_dir.mkdir(parents=True, exist_ok=True)
        for name, config_text in phases:
            print(name)
            scratch_path.write_text(
                "# RUMBLE capture-dispatch spike scratch -- temporary, safe to delete\n\n"
                + config_text, encoding="utf-8")
            write_pointer_with_retry(
                pointer_path,
                apo_config.render_include(f"{RUMBLE_SUBDIR}/{SCRATCH_NAME}"))
            time.sleep(RELOAD_SETTLE_S)

            try:
                captured = run_probe_phase(engine, probe, sample_rate, input_index)
            except (AudioError, RuntimeError) as exc:
                print(f"  FAILED: {exc}")
                return 1

            low_db = band_level_db(captured, sample_rate, PROBE_LOW_HZ)
            high_db = band_level_db(captured, sample_rate, PROBE_HIGH_HZ)
            ratio = high_db - low_db
            ratios.append(ratio)
            print(f"  250 Hz band: {low_db:7.1f} dB   4 kHz band: {high_db:7.1f} dB"
                  f"   ratio (4k-250): {ratio:6.1f} dB")

            if name.startswith("PHASE 0"):
                low_snr = low_db - noise_floor_db(captured, sample_rate, PROBE_LOW_HZ)
                high_snr = high_db - noise_floor_db(captured, sample_rate, PROBE_HIGH_HZ)
                print(f"  probe SNR vs local floor: 250 Hz {low_snr:.1f} dB, "
                      f"4 kHz {high_snr:.1f} dB (need >= {MIN_PROBE_SNR_DB:g})")
                if low_snr < MIN_PROBE_SNR_DB or high_snr < MIN_PROBE_SNR_DB:
                    print("  ABORT: probe tones don't clear the noise floor --")
                    print("  move the mic closer to the speaker / raise playback")
                    print("  volume / quiet the room, then re-run.")
                    return 1

            if name.startswith("PHASE 1") and second_mic is not None:
                idx2, how2 = find_input_index(second_mic)
                print(f"  bonus: second hooked mic {second_mic.label}"
                      f" (index={idx2!r}, {how2})")
                try:
                    cap2 = run_probe_phase(engine, probe, sample_rate, idx2)
                    second_mic_ratio = (band_level_db(cap2, sample_rate, PROBE_HIGH_HZ)
                                        - band_level_db(cap2, sample_rate, PROBE_LOW_HZ))
                    print(f"  second-mic ratio under phase 1: {second_mic_ratio:6.1f} dB")
                except (AudioError, RuntimeError) as exc:
                    print(f"  second-mic check skipped: {exc}")
            print()
    finally:
        if original_pointer_bytes is not None:
            write_pointer_with_retry(pointer_path, original_pointer_bytes)
        elif pointer_path.exists():
            pointer_path.unlink()
        if scratch_path.exists():
            scratch_path.unlink()
        engine.stop()

    print("Restored the real active profile.")
    print()
    print("VERDICTS (ratio change vs PHASE 0 baseline; the LP marker predicts")
    print(f"roughly -36 dB when applied; APPLIED <= -{APPLIED_DROP_DB:g} dB,"
          f" INERT >= -{INERT_MAX_DROP_DB:g} dB):")
    baseline = ratios[0]
    labels_and_expectations = [
        ("PHASE 1  Stage: capture + Device:", "APPLIED",
         "combined Stage:+Device: scoping reaches the mic's capture stream"
         " -- KB SS22.3(a) confirmed"),
        ("PHASE 2  Stage: post-mix + Device:", "INERT",
         "the Stage: line is honored when combined with Device: (post-mix"
         " cannot match a capture stream); a drop here means Stage: was"
         " IGNORED -- the SS22.3(a) failure mode"),
        ("PHASE 3  Device: only, no Stage:", "APPLIED",
         "a stage-unscoped Device: block reaches capture by default, so the"
         " real `Device: all` fallback processes hooked mics with the global"
         " playback profile -- KB SS22.3(b) answered"),
    ]
    for (label, expected, meaning), ratio in zip(labels_and_expectations, ratios[1:]):
        drop = baseline - ratio
        if drop >= APPLIED_DROP_DB:
            observed = "APPLIED"
        elif drop <= INERT_MAX_DROP_DB:
            observed = "INERT"
        else:
            observed = "AMBIGUOUS"
        flag = "as expected" if observed == expected else "UNEXPECTED"
        print(f"  {label}: 4k suppressed by {drop:5.1f} dB -> {observed}"
              f" (expected {expected}, {flag})")
        print(f"      meaning if as expected: {meaning}")
    if second_mic_ratio is not None:
        drop2 = baseline - second_mic_ratio
        print(f"  BONUS second mic under PHASE 1: suppressed by {drop2:5.1f} dB"
              " (expected INERT -- the block must scope to only its own"
              " endpoint). NOTE: uses phase 0's baseline from the FIRST mic;"
              " treat a borderline number with caution, different mics have"
              " different responses.")
    print()
    print("Record the outcome in docs/RUMBLE_APP_SPECIFIC_KB.md SS22.3 and the")
    print("progress file's Follow-ups (S22 [HW] entry).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
