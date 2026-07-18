"""Capture-to-GUI spectrum pipeline (Phase 6: Live Spectrum Analyzer, Task 2).

Bridges Phase 4's Qt-free `LoopbackCapture` (background-thread WASAPI
loopback) and Task 1's Qt-free `SpectrumShaper` (pure-numpy display shaping)
to the Qt GUI thread, for a live spectrum analyzer overlay.

Threading contract (deliberately different from `auto_switch_adapter.py` /
`device_format_adapter.py`'s "foreign thread calls `.emit()`, Qt's
AutoConnection queues it to the GUI thread" pattern): here the audio
callback (`_on_capture_block`) NEVER touches Qt at all -- not even a signal
emit. Per design spec Sec4 point 2 and Phase 4's own contract, it ONLY
appends the raw block to a plain `collections.deque`, relying solely on
that deque append/pop being atomic under the GIL -- no `threading.Lock`
shared with the GUI thread. All FFT/shaping/emitting work happens later, on
the GUI thread, inside the 33 ms `QTimer` slot (`_on_tick`), which is cheap
enough at this size to run per-tick without any marshaling.

Public API:
    SpectrumController -- QObject owning capture + shaper + the ring buffer
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from rumble.core.audio import metering
from rumble.core.audio.capture import LoopbackCapture
from rumble.core.spectrum import SpectrumShaper, drift_correlation, drift_expected

# ── GUI drain tick ───────────────────────────────────────────────────────
# Matches spectrum.py's own `_TICK_INTERVAL_S` comment: the SpectrumShaper's
# attack/release constants are derived assuming THIS 33 ms tick as `dt`. If
# this ever changes, spectrum.py's DEFAULT_ATTACK/DEFAULT_RELEASE must be
# recomputed from the same formula.
_TICK_MS = 33

# ── FFT size: 8192, not spectrum_mags's own 2048 default ────────────────
# At 48kHz, spectrum_mags's default n_fft=2048 is only 2048/48000 ~= 43ms of
# audio -- less than one full cycle of a 20Hz tone (a 20Hz cycle is 50ms).
# The EQ graph's own 10-20Hz floor would collapse into a single FFT bin at
# that window length, giving no real resolution down there. This module
# explicitly passes n_fft=8192 (8192/48000 ~= 171ms) to every
# `spectrum_mags` call -- several full cycles at 20Hz, giving real
# resolution at the graph's own floor -- and 8192 is exactly 8x
# `LoopbackCapture`'s own already-fixed 1024-sample chunk size, which is
# why the ring buffer below is a `deque(maxlen=_RING_BUFFER_CHUNKS)` with
# `_RING_BUFFER_CHUNKS = 8`.
#
# Note: the Phase 6 design spec's overview section describes "~2048-point
# FFT" -- that phrase is a stale/imprecise echo of spectrum_mags's generic
# default parameter, not a deliberate decision made for this feature. This
# comment exists so a future reader isn't confused by the discrepancy; the
# spec doc itself is not being edited (out of scope for this task).
_N_FFT = 8192
_RING_BUFFER_CHUNKS = 8  # 8 * capture's 1024-sample chunk = 8192 samples

# ── "silent" detection threshold ─────────────────────────────────────────
# -70 dBFS sustained for >= 2 seconds is a deliberate product-UX choice for
# THIS feature, not derived from a single external standard -- real-world
# silence-detection conventions vary a lot by application (broadcast
# "dead-air" detectors commonly trigger around -40 to -60 dBFS held for many
# seconds). We deliberately chose a threshold quieter/stricter than those
# broadcast conventions specifically so a genuinely quiet passage in real
# music (which can dip well below -40/-60 dBFS transiently without being
# "silence") does not false-trigger the "no audio" hint, while -70 dBFS
# held for a full 2 seconds is still low/long enough to reliably catch true
# silence (e.g. an exclusive-mode/ASIO stream bypassing Equalizer APO
# entirely) promptly enough to be a useful UI hint.
_SILENCE_THRESHOLD_DBFS = -70.0
_SILENCE_WINDOW_S = 2.0

# ── "clipping" detection thresholds ─────────────────────────────────────
# -0.1 dBFS sustained for >= 3 consecutive `_on_tick()` emissions (~100 ms
# at the default 33 ms tick) is, like the silence threshold above, a
# deliberate product-UX choice for THIS feature rather than a single
# universal external standard -- but it has a real justification: -0.1
# dBFS is a small guard margin below true 0 dBFS full-scale, consistent
# with common clip-indicator/true-peak conventions (e.g. EBU R128-style
# true-peak limiting commonly uses a margin somewhere in the -0.1 to
# -1 dBFS range) rather than requiring an exact 0.0 dBFS reading, which
# risks missing genuinely near-clip cases to floating-point/measurement
# jitter. Requiring 3 consecutive emissions (rather than lighting the
# indicator on the very first one) avoids a single-sample transient
# falsely triggering it. The 5 second clear window mirrors
# `_SILENCE_WINDOW_S`'s own reasoning: it prevents the indicator
# flickering on/off during an intermittently loud passage (e.g. a track
# with scattered hot peaks), only clearing once output has genuinely
# stayed clear of the threshold for a while.
_CLIP_THRESHOLD_DBFS = -0.1
_CLIP_SUSTAIN_TICKS = 3
_CLIP_CLEAR_WINDOW_S = 5.0

# ── "drift" detection windows (Rumble 3.7) ───────────────────────────────
# Generalizes the clipping check above to the broader "measured spectrum
# doesn't match the calculated EQ curve" case (competing DSP, muted/bypassed
# EQ, wrong device) -- see rumble.core.spectrum's module comment for the
# correlation/significance math (spectrum.drift_correlation /
# drift_expected). That per-tick test is already statistically decisive on
# its own (large `n` = number of frequency bins), but real program content
# can transiently look uncorrelated for benign reasons the pure math doesn't
# model (a quiet passage, narrow-band content) -- exactly the same practical
# reason the clipping check above needs a sustain/clear window rather than
# firing on tick one. Rather than inventing new magnitudes, this reuses two
# windows this module has already derived and justified for the identical
# "transient vs. real state" problem: `_SILENCE_WINDOW_S` for how long an
# unexpected reading must persist before it's treated as real, and
# `_CLIP_CLEAR_WINDOW_S` for how long a clean reading must persist before a
# flagged state clears (same flicker-avoidance reasoning as clipping's own
# clear window).
_DRIFT_SUSTAIN_S = _SILENCE_WINDOW_S
_DRIFT_CLEAR_S = _CLIP_CLEAR_WINDOW_S


class SpectrumController(QObject):
    """Owns a `LoopbackCapture` + `SpectrumShaper` + a bounded ring buffer
    of raw capture blocks, and drains/processes/emits on a GUI-thread
    `QTimer`.

    Signals:
        spectrum_ready(object) -- shaped display spectrum, an `np.ndarray`
                                  the same length as `freq_points`, always
                                  emitted from the GUI thread.
        level_ready(float)     -- peak dBFS of the most recently drained
                                  capture window (via `metering.peak_dbfs`).
        stereo_level_ready(object) -- `metering.StereoLevels` (per-channel
                                  peak/RMS) of the same drained window,
                                  reduced to L/R via `_to_stereo_pair` instead
                                  of `level_ready`'s channel-blind combine --
                                  feeds the VU/Peak side meter.
        status_changed(str)    -- mirrors `LoopbackCapture.status`
                                  ("stopped"/"running"/"unavailable"), plus
                                  a synthesized "silent" status layered on
                                  top of "running" (see module docstring).
        clipping_changed(bool) -- emitted only on flips of the sustained
                                  "measured clipping" flag (see the
                                  `_CLIP_*` module-level comment above for
                                  the threshold/sustain/clear reasoning).
        drift_changed(bool)     -- emitted only on flips of the sustained
                                  "measured doesn't track calculated" flag
                                  (Rumble 3.7 -- see the `_DRIFT_*`
                                  module-level comment and
                                  `rumble.core.spectrum.drift_correlation`/
                                  `drift_expected` for the correlation/
                                  significance math). Requires
                                  `set_calculated_response()` to have been
                                  called with a non-`None` curve; never
                                  fires otherwise.

    `capture_factory` is the constructor injection seam for tests: a
    callable `(on_block) -> capture-like object` exposing `.start(
    device_name_fragment)`, `.stop()`, and a polled `.status` property --
    exactly `LoopbackCapture`'s shape. Defaults to a real `LoopbackCapture`.
    """

    spectrum_ready = Signal(object)  # np.ndarray
    level_ready = Signal(float)
    stereo_level_ready = Signal(object)  # metering.StereoLevels
    status_changed = Signal(str)
    clipping_changed = Signal(bool)
    drift_changed = Signal(bool)

    def __init__(
        self,
        freq_points: np.ndarray,
        sample_rate: int = 48000,
        capture_factory: Optional[Callable[[Callable[[np.ndarray, int], None]], object]] = None,
        tick_ms: int = _TICK_MS,
        silence_window_s: float = _SILENCE_WINDOW_S,
        silence_threshold_dbfs: float = _SILENCE_THRESHOLD_DBFS,
        clip_clear_window_s: float = _CLIP_CLEAR_WINDOW_S,
        drift_sustain_s: float = _DRIFT_SUSTAIN_S,
        drift_clear_s: float = _DRIFT_CLEAR_S,
        parent: Optional[QObject] = None,
    ) -> None:
        """Initialize the controller. Does not start capture -- call
        `.start()` explicitly.

        Args:
            freq_points: Log-spaced display frequency axis, passed through
                         to `SpectrumShaper`.
            sample_rate: Fallback sample rate used for the very first FFT
                         (before any real block has arrived to report the
                         capture device's actual rate) and for constructing
                         the `SpectrumShaper`.
            capture_factory: Injection seam -- see class docstring. `None`
                         uses a real `LoopbackCapture`.
            tick_ms: GUI-thread drain interval. Defaults to 33 ms (see
                         `_TICK_MS` above) -- only change this alongside
                         `spectrum.py`'s attack/release constants.
            silence_window_s: How long (seconds) the peak must stay below
                         `silence_threshold_dbfs` before `status_changed`
                         reports "silent". Shrinkable here so tests don't
                         need to wait out a real 2 seconds.
            silence_threshold_dbfs: See the module-level comment above.
            clip_clear_window_s: How long (seconds) the peak must stay below
                         `_CLIP_THRESHOLD_DBFS` before a sustained
                         `clipping_changed(True)` clears back to `False`.
                         Shrinkable here so tests don't need to wait out a
                         real 5 seconds -- mirrors `silence_window_s` above.
            drift_sustain_s: How long (seconds) the measured spectrum must
                         keep failing to correlate with the calculated
                         response before `drift_changed(True)` fires.
                         Shrinkable here for tests, like the windows above.
            drift_clear_s: How long (seconds) it must correlate again before
                         a sustained `drift_changed(True)` clears.
        """
        super().__init__(parent)

        self._shaper = SpectrumShaper(freq_points, sample_rate)
        self._last_sample_rate = int(sample_rate)

        self._blocks: "deque[np.ndarray]" = deque(maxlen=_RING_BUFFER_CHUNKS)

        make_capture = capture_factory or (lambda on_block: LoopbackCapture(on_block))
        self._capture = make_capture(self._on_capture_block)
        self._device_fragment: Optional[str] = None

        self._tick_interval_s = tick_ms / 1000.0
        self._silence_window_s = float(silence_window_s)
        self._silence_threshold_dbfs = float(silence_threshold_dbfs)
        self._silent_accum_s = 0.0

        self._clip_clear_window_s = float(clip_clear_window_s)
        self._clipping = False
        self._clip_sustain_count = 0
        self._clip_clear_accum_s = 0.0

        self._calculated_response_db: Optional[np.ndarray] = None
        self._drift_sustain_s = float(drift_sustain_s)
        self._drift_clear_s = float(drift_clear_s)
        self._drift = False
        self._drift_sustain_accum_s = 0.0
        self._drift_clear_accum_s = 0.0

        # Tracks the capture's own raw status separately from the
        # synthesized "silent" overlay, so a real status transition (e.g.
        # to "unavailable") always wins and clears any stale "silent".
        self._raw_status = getattr(self._capture, "status", "stopped")
        self._effective_status = self._raw_status

        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self._on_tick)

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self, device_fragment: Optional[str] = None) -> None:
        """Start capture and the GUI-thread drain timer.

        `device_fragment` is passed straight through to
        `LoopbackCapture.start()` (`None` = follow the current system
        default output device). Remembered so a later `set_visible(True)`
        can resume against the same target.
        """
        self._device_fragment = device_fragment
        self._blocks.clear()
        self._shaper.reset()
        self._silent_accum_s = 0.0
        # Reset (not emit) -- a fresh capture session starts unclipped by
        # definition. `stop()` is the one responsible for emitting the
        # False transition if `_clipping` was left `True`; see its
        # docstring for why that can't be deferred to this reset instead.
        self._clipping = False
        self._clip_sustain_count = 0
        self._clip_clear_accum_s = 0.0
        # Same reset-not-emit contract as `_clipping` above (see stop()'s
        # docstring for why the emit-on-clear responsibility lives there).
        self._drift = False
        self._drift_sustain_accum_s = 0.0
        self._drift_clear_accum_s = 0.0
        self._capture.start(device_fragment)
        self._sync_raw_status()
        self._timer.start()

    def stop(self) -> None:
        """Stop capture and the drain timer. Safe to call when not
        running, and safe to call twice.

        Also clears any sustained "measured clipping" state, emitting
        `clipping_changed(False)` if `_clipping` was `True` -- this must
        happen HERE, not only via `start()`'s own reset, or the flag goes
        stale: `start()` resets `_clipping` to `False` silently (no signal,
        since the accumulators are meant to start fresh for a new capture
        session), so a caller that stops while clipping, then restarts and
        feeds only quiet audio, would never see a `True -> False` edge to
        emit -- the UI (editor_panel.py's headroom label) would stay stuck
        at its clipping-active state indefinitely with no user-facing way
        to clear it short of triggering a brand new clip event. The
        sustained "measured drift" flag (Rumble 3.7) has the identical
        problem and is cleared the same way here.
        """
        self._timer.stop()
        self._capture.stop()
        self._sync_raw_status()
        if self._clipping:
            self._clipping = False
            self.clipping_changed.emit(False)
        self._clip_sustain_count = 0
        self._clip_clear_accum_s = 0.0
        if self._drift:
            self._drift = False
            self.drift_changed.emit(False)
        self._drift_sustain_accum_s = 0.0
        self._drift_clear_accum_s = 0.0

    def set_calculated_response(self, response_db: Optional[np.ndarray]) -> None:
        """Set the current calculated EQ response (dB, on the same
        `freq_points` grid as this controller's shaped spectrum output) that
        `drift_changed` compares the measured spectrum against. `None`
        (default, and whenever the caller has nothing to offer) disables
        drift checking entirely rather than judging against a stale curve --
        `_update_drift()` treats a `None` response the same as
        `spectrum.drift_correlation()` returning `None` (skip, don't reset
        any in-progress sustain/clear accumulation)."""
        self._calculated_response_db = (
            None if response_db is None else np.asarray(response_db, dtype=np.float64))

    @property
    def peak(self) -> np.ndarray:
        """Per-frequency decaying peak-hold trace from the underlying
        `SpectrumShaper` (see `SpectrumShaper.peak`) -- Task 3's "Peak hold"
        overlay curve reads this after every `spectrum_ready` tick."""
        return self._shaper.peak

    def set_visible(self, visible: bool) -> None:
        """Pause (`False`) or resume (`True`) without discarding
        configuration -- used to auto-pause capture + FFT work while the
        spectrum graph itself isn't visible in the UI.

        Unlike `stop()`/`start()`, this does not reset the shaper's
        smoothing state, and resumes against the same `device_fragment`
        last passed to `start()`.
        """
        if visible:
            if self._raw_status != "running":
                self._capture.start(self._device_fragment)
                self._sync_raw_status()
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._capture.stop()
            self._sync_raw_status()

    # ── audio thread -- Qt-free ──────────────────────────────────────────

    def _on_capture_block(self, block: np.ndarray, sample_rate: int) -> None:
        """`LoopbackCapture`'s `on_block` callback. Runs on the capture's
        background thread -- per Phase 4's Qt-free contract, this method
        must NEVER touch Qt (no signals, no widgets, no QTimer calls) and
        holds no lock beyond `deque.append()`'s own GIL-protected atomicity.
        All FFT/shaping/emitting happens later, on the GUI thread, in
        `_on_tick`.
        """
        self._last_sample_rate = sample_rate
        self._blocks.append(block)

    # ── GUI thread ───────────────────────────────────────────────────────

    @staticmethod
    def _to_mono(block: np.ndarray) -> np.ndarray:
        """Average an (n_frames, channels) capture block down to mono.
        Already-mono (1-D) blocks pass through unchanged."""
        arr = np.asarray(block)
        if arr.ndim == 1:
            return arr
        return arr.mean(axis=1)

    @staticmethod
    def _to_stereo_pair(block: np.ndarray) -> np.ndarray:
        """Reduce an (n_frames, channels) capture block to exactly 2 channels
        (L, R) for the stereo level meter. Mono (1-D, or a single-column 2-D
        block) is duplicated to both channels; >=2 channels take the first
        two columns (WASAPI's standard channel mask puts front-left/
        front-right first). Must run per-block, before concatenation --
        same reasoning as `_to_mono` above: a device/format change mid-
        session can leave differently-shaped blocks sitting in the same ring
        buffer window, and concatenating raw blocks first would raise on
        that shape mismatch."""
        arr = np.asarray(block)
        if arr.ndim == 1:
            return np.stack([arr, arr], axis=1)
        if arr.shape[1] == 1:
            return np.concatenate([arr, arr], axis=1)
        return arr[:, :2]

    def _on_tick(self) -> None:
        """QTimer slot -- runs on the GUI thread. Polls capture status,
        then (if any blocks have arrived) snapshots the ring buffer,
        computes the shaped spectrum + peak level, and emits both."""
        self._sync_raw_status()

        if not self._blocks:
            return

        # Snapshot without popping: `deque(maxlen=_RING_BUFFER_CHUNKS)`
        # already maintains a rolling window on its own -- as the audio
        # thread appends newer chunks it evicts the oldest -- so simply
        # reading its current contents each tick gives the newest
        # ~_RING_BUFFER_CHUNKS * chunk_size samples (~8192 at the capture
        # module's fixed 1024-frame chunk size) with no extra bookkeeping,
        # and no need to coordinate draining with the audio thread.
        blocks = list(self._blocks)

        # Mono-ize each block individually BEFORE concatenating -- not the
        # reverse. `LoopbackCapture` follows the system default device
        # (`device_fragment=None`); if the default device changes mid-
        # session, `LoopbackCapture._reconnect()` (Phase 4) reopens against
        # the new default and starts appending differently-shaped blocks
        # (e.g. a new mono block) while up to `_RING_BUFFER_CHUNKS - 1`
        # older-shaped blocks (e.g. stereo) may still be sitting in this
        # ring buffer. Concatenating the raw blocks first (the old order)
        # then mono-izing the result raises `ValueError` on that shape
        # mismatch -- an uncaught exception inside this Qt timer slot on a
        # continuously-running live feature. Mono-izing first sidesteps the
        # mismatch entirely: every resulting array is 1-D, and 1-D arrays
        # concatenate fine regardless of how many channels each source
        # block originally had.
        mono = np.concatenate([self._to_mono(b) for b in blocks], axis=0)
        if mono.shape[0] > _N_FFT:
            mono = mono[-_N_FFT:]

        freqs_hz, mags_db = metering.spectrum_mags(mono, self._last_sample_rate, n_fft=_N_FFT)
        shaped = self._shaper.update(freqs_hz, mags_db)
        # Peak is computed per RAW block (before mono-izing) and maxed
        # across the window, rather than on the mono-ized/concatenated
        # array above, for two reasons: (1) `metering.peak_dbfs` is just
        # `max(abs(x))` over the whole array regardless of shape, so
        # per-block-max-then-max-of-maxes is mathematically identical to a
        # single whole-window max -- no accuracy change from the pre-fix
        # behavior of calling it once on the old raw `combined` array; (2)
        # averaging channels down to mono (as the FFT path above needs to)
        # can understate true peak -- e.g. a signal loud in only one
        # channel reads roughly 6 dB quieter once averaged -- which would
        # under-report `level_ready` and under-trigger Task 4's
        # measured-clipping detector. Computing peak on the raw blocks
        # also means it never needs to concatenate blocks of differing
        # channel counts at all, so it can't hit the same mismatch.
        peak = max(metering.peak_dbfs(b) for b in blocks)

        # Second, independent reduction of the SAME `blocks` snapshot above
        # for the stereo VU/Peak meter -- no new ring buffer, no extra
        # capture-thread involvement. Per-block stereo-pairing (not
        # concatenate-then-reduce) for the identical differently-shaped-
        # blocks-mid-device-change reason as the mono/peak paths above.
        stereo = np.concatenate([self._to_stereo_pair(b) for b in blocks], axis=0)
        levels = metering.stereo_levels(stereo)

        self.spectrum_ready.emit(shaped)
        self.level_ready.emit(peak)
        self.stereo_level_ready.emit(levels)
        self._update_silence(peak)
        self._update_clipping(peak)
        self._update_drift(shaped)

    def _sync_raw_status(self) -> None:
        """Read `capture.status` and emit `status_changed` on any
        transition. Resets the silence accumulator on every real status
        transition so a fresh "running" period starts unsilenced."""
        raw = self._capture.status
        if raw != self._raw_status:
            self._raw_status = raw
            self._effective_status = raw
            self._silent_accum_s = 0.0
            self.status_changed.emit(raw)

    def _update_silence(self, peak_dbfs_value: float) -> None:
        """Track how long the recent peak has stayed below
        `_silence_threshold_dbfs` and synthesize a "silent" status on top
        of "running" once that holds for `_silence_window_s` -- see the
        module-level comment on `_SILENCE_THRESHOLD_DBFS` for the reasoning
        behind the specific threshold/window. Reverts to "running" as soon
        as a block exceeds the threshold again.
        """
        if self._raw_status != "running":
            self._silent_accum_s = 0.0
            return

        if peak_dbfs_value < self._silence_threshold_dbfs:
            self._silent_accum_s += self._tick_interval_s
            if self._silent_accum_s >= self._silence_window_s and self._effective_status != "silent":
                self._effective_status = "silent"
                self.status_changed.emit("silent")
        else:
            self._silent_accum_s = 0.0
            if self._effective_status == "silent":
                self._effective_status = "running"
                self.status_changed.emit("running")

    def _update_clipping(self, peak_dbfs_value: float) -> None:
        """Track sustained "measured clipping" -- see the module-level
        `_CLIP_*` comment for the reasoning behind the specific threshold/
        sustain-count/clear-window. Emits `clipping_changed` only on a flip
        (True after `_CLIP_SUSTAIN_TICKS` consecutive over-threshold ticks;
        False after `_clip_clear_window_s` continuously back under it).
        """
        if peak_dbfs_value >= _CLIP_THRESHOLD_DBFS:
            self._clip_clear_accum_s = 0.0
            self._clip_sustain_count += 1
            if not self._clipping and self._clip_sustain_count >= _CLIP_SUSTAIN_TICKS:
                self._clipping = True
                self.clipping_changed.emit(True)
        else:
            self._clip_sustain_count = 0
            if self._clipping:
                self._clip_clear_accum_s += self._tick_interval_s
                if self._clip_clear_accum_s >= self._clip_clear_window_s:
                    self._clipping = False
                    self._clip_clear_accum_s = 0.0
                    self.clipping_changed.emit(False)

    def _update_drift(self, measured_db: np.ndarray) -> None:
        """Track sustained "measured doesn't match calculated" (Rumble 3.7)
        -- see the module-level `_DRIFT_*` comment and
        `rumble.core.spectrum.drift_correlation`/`drift_expected` for the
        correlation/significance math. Emits `drift_changed` only on a flip
        (True after `_drift_sustain_s` seconds of consecutive
        not-significantly-positive-correlation ticks; False after
        `_drift_clear_s` seconds continuously back to tracking as expected).

        A tick with nothing to judge (no calculated response set yet, or one
        too close to flat -- `drift_correlation()` returns `None`) neither
        advances nor resets either accumulator: it's not evidence of drift,
        but it's also not evidence the EQ is working, so it shouldn't erase
        progress already made toward either state.
        """
        if self._calculated_response_db is None:
            return
        r = drift_correlation(measured_db, self._calculated_response_db)
        if r is None:
            return
        expected = drift_expected(r, len(self._calculated_response_db))
        if expected is None:
            return
        if expected:
            self._drift_sustain_accum_s = 0.0
            if self._drift:
                self._drift_clear_accum_s += self._tick_interval_s
                if self._drift_clear_accum_s >= self._drift_clear_s:
                    self._drift = False
                    self._drift_clear_accum_s = 0.0
                    self.drift_changed.emit(False)
        else:
            self._drift_clear_accum_s = 0.0
            self._drift_sustain_accum_s += self._tick_interval_s
            if not self._drift and self._drift_sustain_accum_s >= self._drift_sustain_s:
                self._drift = True
                self.drift_changed.emit(True)
