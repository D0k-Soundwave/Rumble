"""Stereo VU/Peak/Reference level meter ballistics.

Pure numpy/math, no Qt -- takes `metering.StereoLevels` readings in (one per
GUI-thread tick, see `rumble/ui/spectrum_overlay.py`) and returns smoothed
per-channel meter positions out. Structured like `rumble/core/spectrum.py`'s
`SpectrumShaper`: every ballistics constant here is derived from a named
standard and a formula, never a hand-picked number.

Public API:
    LevelMeter        -- stateful VU/Peak/Reference meter, one per stereo widget
    K_REFERENCE_DBFS   -- Bob Katz's K-System reference levels ("k12"/"k14"/"k20")
"""
from __future__ import annotations

import math

from rumble.core.audio import metering

# ── tick interval ─────────────────────────────────────────────────────────
# Duplicated from `rumble/ui/spectrum_overlay.py`'s `_TICK_MS` (core/ can't
# import ui/) -- same cross-module duplication-with-comment convention
# already used between spectrum.py and spectrum_overlay.py. If that tick
# interval ever changes, recompute every constant below from the same
# formulas rather than hand-adjusting them.
_TICK_INTERVAL_S = 0.033

# ── VU ballistics: ANSI C16.5-1942 / IEC 60268-17 ────────────────────────
# The classic VU-meter standard specifies symmetric ballistics: the meter
# reaches 99% of a suddenly-applied step in 300ms +/-10ms, for BOTH a rising
# and a falling step (unlike an RTA's asymmetric fast-attack/slow-release).
# A single-pole exponential reaches a fraction `f` of a step after
# `t = -tau * ln(1 - f)` seconds, so for f=0.99, t=0.300s:
#     tau = 0.300 / -ln(1 - 0.99) = 0.300 / ln(100)
_VU_TIME_CONSTANT_S = 0.300 / math.log(100)  # ~= 65.1 ms
VU_ATTACK = 1.0 - math.exp(-_TICK_INTERVAL_S / _VU_TIME_CONSTANT_S)
VU_RELEASE = VU_ATTACK  # symmetric, per the standard

# ── Peak ballistics: IEC 60268-10 peak programme meter ───────────────────
# IEC 60268-10's integration time (~5ms) is far below this app's 33ms tick,
# so at this tick rate attack is effectively instantaneous -- the bar simply
# shows each tick's measured peak directly, no smoothing.
PEAK_ATTACK = 1.0
# Decay follows the same standard's ~20 dB/s linear (dB-domain, not
# exponential) release rate -- a per-tick decrement of:
PEAK_DECAY_DB_PER_TICK = 20.0 * _TICK_INTERVAL_S
# The peak-hold cap decays at the same rate as the bar it sits over -- no
# standard reason for the cap to fall differently than the meter itself.
HOLD_DECAY_DB_PER_TICK = PEAK_DECAY_DB_PER_TICK

# ── display floor / scale ─────────────────────────────────────────────────
# -60 dBFS is a UX/display-range choice (matches common software level-meter
# conventions), not a physical constant -- `metering.FLOOR_DBFS` (-120) stays
# the true measurement floor underneath it. This is also the bottom of the
# widget's fixed dB scale (see rumble/ui/vu_meter.py): ticks at 0, -10, -20,
# -30, -40, -50, -60 dBFS.
FLOOR_DBFS = -60.0

# ── Reference mode: Bob Katz's K-System metering ──────────────────────────
# An analog VU meter's "0 VU" is a calibrated reference level with real
# headroom above it (well-aligned analog gear doesn't actually distort until
# well past 0 VU) -- there's no equivalent on a digital scale, where 0 dBFS
# IS the hard ceiling with no headroom beyond it. Bob Katz's K-System (Mix
# magazine, 1998; also documented in his book "Mastering Audio") is the
# named, standard answer: pick a dBFS point to stand in for "0" the way an
# analog meter's needle rests at 0 VU, with the stated number of dB of
# headroom reserved above it before the real 0 dBFS ceiling. K-12/K-14/K-20
# name how much headroom (in dB) that reference point reserves -- K-20 for
# wide-dynamic-range/critical-listening material, K-14 as Katz's general-
# purpose recommendation, K-12 for already-loud/heavily-limited material.
K_REFERENCE_DBFS = {"k12": -12.0, "k14": -14.0, "k20": -20.0}

# ── clip threshold ─────────────────────────────────────────────────────────
# Matches `rumble/ui/spectrum_overlay.py`'s own `_CLIP_THRESHOLD_DBFS`: -0.1
# dBFS, a small guard margin below true 0 dBFS full-scale (see that module's
# comment for the full justification). Duplicated here rather than imported
# because that constant lives in the ui/ layer's Qt-adjacent module and this
# one is deliberately Qt-free.
_CLIP_THRESHOLD_DBFS = -0.1

# ── colour bands, fixed regardless of app theme ───────────────────────────
# Aligned exactly to two of the printed scale's own tick marks (-10, -3) so
# the coloured zones and the labelled scale agree with each other.
BAND_GREEN_MAX_DBFS = -10.0   # green: FLOOR_DBFS .. -10
BAND_YELLOW_MAX_DBFS = -3.0   # yellow: -10 .. -3, red: -3 .. 0

_VALID_MODES = ("vu", "peak", "ref")
# "ref" (Reference/K-System) reads the same underlying quantity with the same
# symmetric ballistics as "vu" -- Katz's K-System meters are explicitly VU-
# style detectors, just read against a relabelled reference point rather than
# 0 dBFS. The two modes share all ballistics/hold behaviour below; only the
# *display* (which dB point the widget marks as the analog-style reference,
# see rumble/ui/vu_meter.py) differs, which is a rendering concern this
# Qt-free module has no reason to know about.
_VU_STYLE_MODES = ("vu", "ref")


class LevelMeter:
    """Stateful per-channel VU/Peak/Reference meter position + peak-hold +
    clip flag.

    One instance drives one stereo (L/R) meter widget. `update()` is called
    once per GUI-thread tick with the tick's `metering.StereoLevels`;
    `bar_l`/`bar_r` report the resulting smoothed meter position in dBFS.
    """

    def __init__(self, mode: str = "vu") -> None:
        self._mode = "vu"
        self._bar_l = FLOOR_DBFS
        self._bar_r = FLOOR_DBFS
        self._hold_l: float | None = None
        self._hold_r: float | None = None
        self._clip_l = False
        self._clip_r = False
        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        """Switch ballistics mode. Resets meter state -- Peak reads a
        different underlying quantity (instantaneous peak vs. VU/Reference's
        RMS) and has a hold cap the other two don't, so a stale reading from
        another mode would be meaningless."""
        if mode not in _VALID_MODES:
            raise ValueError(f"LevelMeter mode must be one of {_VALID_MODES}, got {mode!r}")
        self._mode = mode
        self.reset()

    @property
    def mode(self) -> str:
        return self._mode

    def reset(self) -> None:
        """Back to the display floor, hold/clip cleared -- used both on a
        mode switch and when the owning widget goes idle (Analyser off)."""
        self._bar_l = FLOOR_DBFS
        self._bar_r = FLOOR_DBFS
        self._hold_l = None if self._mode in _VU_STYLE_MODES else FLOOR_DBFS
        self._hold_r = None if self._mode in _VU_STYLE_MODES else FLOOR_DBFS
        self._clip_l = False
        self._clip_r = False

    def update(self, levels: metering.StereoLevels) -> None:
        if self._mode in _VU_STYLE_MODES:
            self._bar_l = self._ewma(self._bar_l, levels.rms_l, VU_ATTACK, VU_RELEASE)
            self._bar_r = self._ewma(self._bar_r, levels.rms_r, VU_ATTACK, VU_RELEASE)
            # VU-style meters have no peak-hold segment -- hold_l/hold_r stay None.
        else:
            self._bar_l = max(levels.peak_l, self._bar_l - PEAK_DECAY_DB_PER_TICK)
            self._bar_r = max(levels.peak_r, self._bar_r - PEAK_DECAY_DB_PER_TICK)
            assert self._hold_l is not None and self._hold_r is not None
            self._hold_l = max(levels.peak_l, self._hold_l - HOLD_DECAY_DB_PER_TICK)
            self._hold_r = max(levels.peak_r, self._hold_r - HOLD_DECAY_DB_PER_TICK)

        self._clip_l = self._is_clipping(levels.peak_l, self._hold_l)
        self._clip_r = self._is_clipping(levels.peak_r, self._hold_r)

    @staticmethod
    def _is_clipping(peak_dbfs_value: float, hold_dbfs_value: float | None) -> bool:
        if peak_dbfs_value >= _CLIP_THRESHOLD_DBFS:
            return True
        # Peak mode only (hold_dbfs_value is None in VU mode): the hold cap
        # gives the clip flag a brief natural persistence past the
        # instantaneous flash, matching the cap's own decay.
        return hold_dbfs_value is not None and hold_dbfs_value >= _CLIP_THRESHOLD_DBFS

    @staticmethod
    def _ewma(prev: float, new: float, attack: float, release: float) -> float:
        coef = attack if new > prev else release
        return prev + coef * (new - prev)

    @property
    def bar_l(self) -> float:
        return self._bar_l

    @property
    def bar_r(self) -> float:
        return self._bar_r

    @property
    def hold_l(self) -> float | None:
        return self._hold_l

    @property
    def hold_r(self) -> float | None:
        return self._hold_r

    @property
    def clipping(self) -> tuple[bool, bool]:
        return (self._clip_l, self._clip_r)
