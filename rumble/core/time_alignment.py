"""Mic-sweep multi-channel time-alignment measurement (Rumble 4.9).

Measures the relative arrival time of each output channel at a fixed
microphone position -- a physically-separated speaker (e.g. a subwoofer
sitting closer to the listening position than the mains) arrives earlier
than its acoustic distance alone would suggest once processing/driver
latency is folded in, so a calculated distance-based delay is only ever an
approximation. Playing a calibrated sweep through one channel at a time and
timing its arrival via a real microphone measures the actual end-to-end
offset directly, the same technique room-correction tools (REW, Dirac,
Audyssey) use for speaker-distance/delay calibration.

Two layers, deliberately kept separate:
  - The correlation/offset math below (`detect_offset_samples`,
    `measure_channel_delays`, `build_channel_sweep`) is pure numpy, fully
    testable with synthetic signals -- no microphone required. This is
    where the actual measurement logic lives.
  - `run_measurement` is a thin real-hardware orchestration shell (play a
    sweep out of each channel in turn via an injected `ToneEngine`, record
    via an injected `record_factory`) that calls into the tested core for
    every actual computation. It is not exercised by the automated test
    gate -- there is no microphone in CI -- mirroring how
    rumble/core/audio/capture.py's real WASAPI backend isn't unit-tested
    either, only the logic around it.

Public API:
    detect_offset_samples -- GCC-PHAT arrival-time offset of a reference
                              signal within a (longer) captured recording
    measure_channel_delays -- turn per-channel captures into relative
                              delay-ms values, rebased to the latest
                              arrival (APO's Delay: line can only add
                              delay, never subtract it)
    build_channel_sweep   -- place a mono sweep into one channel of an
                              otherwise-silent N-channel playback buffer
    STANDARD_CHANNEL_ORDER -- canonical physical-channel index order
    MeasurementError        -- raised by run_measurement on a hardware
                              failure
    run_measurement          -- live play-and-record orchestration
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import numpy as np

from rumble.core.audio import tones

# The channel-index order sounddevice/PortAudio's WASAPI host API exposes a
# Windows multichannel output stream in: SPEAKER_FRONT_LEFT,
# SPEAKER_FRONT_RIGHT, SPEAKER_FRONT_CENTER, SPEAKER_LOW_FREQUENCY,
# SPEAKER_BACK_LEFT, SPEAKER_BACK_RIGHT, SPEAKER_SIDE_LEFT,
# SPEAKER_SIDE_RIGHT -- the standard KSAUDIO_SPEAKER_7POINT1_SURROUND
# channel-mask bit order (ksmedia.h), not an arbitrary choice. This matches
# rumble/ui/editor_panel.py's existing CHANNEL_PRESETS["7.1 Surround"] list
# order (kept as a separate constant here rather than importing from ui/,
# since core/ must not depend on ui/ -- Session 28's channel-preset/APO
# reconciliation research is the place to unify these if they ever drift).
STANDARD_CHANNEL_ORDER: list[str] = ["L", "R", "C", "LFE", "RL", "RR", "SL", "SR"]


def _gcc_phat(sig: np.ndarray, ref: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Generalized Cross-Correlation with Phase Transform (Knapp & Carter,
    "The Generalized Correlation Method for Estimation of Time Delay", IEEE
    Trans. ASSP, 1976) between `sig` and `ref`.

    Whitening each frequency bin's magnitude to 1 before the inverse
    transform is what makes GCC-PHAT far more robust than a raw
    cross-correlation against multipath/room-reflection false peaks --
    exactly the failure mode a naive correlation would hit in a live room.

    Returns `(cc, lags)`: the correlation value and the corresponding
    integer lag (in samples of `sig` relative to `ref`) for every index.
    """
    sig = np.asarray(sig, dtype=np.float64)
    ref = np.asarray(ref, dtype=np.float64)
    n = len(sig) + len(ref)
    n_fft = 1 << (n - 1).bit_length()

    SIG = np.fft.rfft(sig, n=n_fft)
    REF = np.fft.rfft(ref, n=n_fft)
    cross = SIG * np.conj(REF)
    mag = np.abs(cross)
    mag = np.where(mag < 1e-15, 1e-15, mag)
    cross = cross / mag
    cc = np.fft.irfft(cross, n=n_fft)

    half = n_fft // 2
    lags = np.concatenate([np.arange(0, half + 1), np.arange(-(n_fft - half - 1), 0)])
    return cc, lags


def detect_offset_samples(
    reference: np.ndarray,
    captured: np.ndarray,
    max_offset_samples: Optional[int] = None,
) -> int:
    """Find the sample index into `captured` at which `reference` best
    cross-correlates, via GCC-PHAT -- i.e. the arrival time of `reference`
    within the (typically much longer, silence-padded) `captured`
    recording.

    Only non-negative lags are considered: `captured` is a recording that
    starts at or before the reference signal was played, so the reference
    can never "arrive" before the recording started. `max_offset_samples`,
    if given, additionally caps how far into `captured` to search.
    """
    cc, lags = _gcc_phat(captured, reference)
    mask = lags >= 0
    if max_offset_samples is not None:
        mask &= lags <= max_offset_samples
    cc = np.where(mask, cc, -np.inf)
    best_idx = int(np.argmax(cc))
    return int(lags[best_idx])


def measure_channel_delays(
    channel_captures: dict[str, np.ndarray],
    reference: np.ndarray,
    sample_rate: int,
    max_offset_s: Optional[float] = None,
) -> dict[str, float]:
    """Given a mic capture of each channel's played reference sweep, return
    each channel's delay in milliseconds needed to align it with the
    other channels.

    Each channel's arrival offset is measured against `reference`
    independently via `detect_offset_samples`, then re-based so the
    *latest*-arriving channel gets 0 ms and every earlier channel gets a
    positive delay equal to how far ahead of it that channel arrived:
    APO's per-channel `Delay:` line can only add delay, it cannot make a
    channel play earlier, so alignment can only ever push the early
    channels back to match the latest one, never the reverse. (Same
    rebase convention as bass_management.py's distance-derived delays,
    where the farthest -- latest-arriving -- channel is the 0 ms
    reference.)
    """
    max_offset_samples = (
        int(round(max_offset_s * sample_rate)) if max_offset_s is not None else None
    )
    raw_offsets = {
        channel: detect_offset_samples(reference, captured, max_offset_samples)
        for channel, captured in channel_captures.items()
    }
    latest = max(raw_offsets.values())
    return {
        channel: (latest - offset) / sample_rate * 1000.0
        for channel, offset in raw_offsets.items()
    }


def build_channel_sweep(sweep: np.ndarray, channel_index: int, n_channels: int) -> np.ndarray:
    """Place a mono `sweep` into `channel_index` of an otherwise-silent
    `n_channels`-wide buffer, ready for `ToneEngine.play()`.
    """
    if not (0 <= channel_index < n_channels):
        raise ValueError(f"channel_index {channel_index} out of range for {n_channels} channels")
    buf = np.zeros((len(sweep), n_channels), dtype=np.float32)
    buf[:, channel_index] = sweep
    return buf


class MeasurementError(Exception):
    """Raised by `run_measurement` when a channel's recording could not be
    captured (no input device, a hardware error, or the recording thread
    never produced a result)."""


def _default_record(duration_s: float, sample_rate: int, device: Optional[int]) -> np.ndarray:
    """Default `record_factory`: a blocking mono capture via sounddevice.

    Deliberately plain `sounddevice` (not the pyaudiowpatch loopback path
    in rumble/core/audio/capture.py) -- a measurement microphone is a normal
    input device, not a loopback endpoint, so it needs none of that
    module's WASAPI-loopback-specific device resolution.
    """
    import sounddevice as sd

    frames = int(round(duration_s * sample_rate))
    rec = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32", device=device)
    sd.wait()
    return rec[:, 0]


def run_measurement(
    channel_names: list[str],
    tone_engine,
    sample_rate: int,
    output_device: Optional[int] = None,
    input_device: Optional[int] = None,
    f_start_hz: float = 20.0,
    f_end_hz: Optional[float] = None,
    duration_s: float = 1.0,
    lead_in_s: float = 0.3,
    trail_s: float = 0.3,
    level_dbfs: float = -12.0,
    record_factory: Optional[Callable[[float, int, Optional[int]], np.ndarray]] = None,
) -> dict[str, float]:
    """Play a sweep through each of `channel_names` in turn (one physical
    output channel at a time, all others silent) while recording via a
    measurement microphone, and return each channel's delay in ms needed
    to align it with the latest-arriving channel.

    `channel_names[i]` is played out of output-buffer column `i` -- callers
    are responsible for ordering `channel_names` to match
    STANDARD_CHANNEL_ORDER's physical-channel index convention.

    Real hardware only -- not covered by the automated test gate (no mic in
    CI). All the actual math is delegated to `measure_channel_delays` /
    `detect_offset_samples`, which are; this function is deliberately kept
    a thin sequencing shell over that tested core, per this project's
    stream_factory/record_factory dependency-injection convention (mirrors
    rumble/core/audio/capture.py's `stream_factory` seam).
    """
    n_channels = len(channel_names)
    f_end = f_end_hz if f_end_hz is not None else min(20000.0, 0.45 * sample_rate)
    sweep = tones.generate_log_sweep(f_start_hz, f_end, duration_s, level_dbfs, sample_rate)
    record_seconds = lead_in_s + duration_s + trail_s
    record = record_factory or _default_record

    captures: dict[str, np.ndarray] = {}
    for idx, name in enumerate(channel_names):
        buf = build_channel_sweep(sweep, idx, n_channels)
        result: dict[str, np.ndarray] = {}
        error: dict[str, BaseException] = {}

        def _do_record() -> None:
            try:
                result["captured"] = record(record_seconds, sample_rate, input_device)
            except Exception as exc:  # pragma: no cover - real hardware path
                error["exc"] = exc

        rec_thread = threading.Thread(target=_do_record, daemon=True)
        rec_thread.start()
        time.sleep(lead_in_s)
        tone_engine.play(buf, sample_rate, device=output_device)
        while tone_engine.is_playing:
            time.sleep(0.01)
        rec_thread.join(timeout=record_seconds + 5.0)

        if "captured" not in result:
            detail = f": {error['exc']}" if "exc" in error else " (timed out)"
            raise MeasurementError(f"Recording channel {name!r} failed{detail}")
        captures[name] = result["captured"]

    return measure_channel_delays(captures, sweep, sample_rate)
