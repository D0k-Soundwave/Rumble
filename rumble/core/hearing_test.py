"""Hearing test suite -- Test A (per-ear frequency staircase), Phase 7.

Qt-free. See rumble/core/digit_screening.py for Test B (noise screening),
which reuses Staircase from this module.
"""
from __future__ import annotations

import json
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from rumble.core.audio import tones as _tones

_VOLUME_REFERENCE_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "audio" / "volume_reference.wav")


class Staircase:
    """Adaptive up/down threshold search with a configurable step-size
    ratio. NOT the clinical "2-down-1-up" procedure (which converges to
    ~70.7% correct) -- this is a simpler weighted up/down rule: any
    direction change counts as a reversal, and asymmetric step_down/step_up
    values (Test A uses step_down=8.0, step_up=4.0, a 2:1 ratio) shift the
    convergence point away from 50% correct. Test B (digit_screening.py)
    reuses this class with EQUAL step_down/step_up (symmetric), which does
    converge to the 50%-correct point -- required because Test B's SRT
    (speech reception threshold) is specifically DEFINED in the reference
    literature as the 50%-correct SNR point (see digit_screening.py's
    REFERRAL_SNR_DB derivation). Neutral naming (`_level`, not `_dbfs`) --
    this class tracks any bounded scalar that converges via "heard/not
    heard" style feedback; Test A uses it for tone level in dBFS.
    """

    def __init__(
        self,
        start_level: float = -30.0,
        step_down: float = 8.0,
        step_up: float = 4.0,
        reversals_needed: int = 5,
        floor_level: float = -90.0,
        ceil_level: float = -10.0,
        ceiling_give_up: int | None = None,
    ) -> None:
        self.level = start_level
        self._step_down = step_down
        self._step_up = step_up
        self._reversals_needed = reversals_needed
        self._floor = floor_level
        self._ceil = ceil_level
        # When set, this many CONSECUTIVE "not heard" responses at the
        # ceiling level ends the search immediately with `gave_up=True` and
        # `threshold == ceil_level` ("at or beyond the testable maximum") --
        # the listener plainly can't hear this stimulus at all, so bouncing
        # off the ceiling until reversals_needed accumulates is pure wasted
        # clicks. Opt-in (None = off) because Test B's symmetric SNR
        # staircase has different ceiling semantics and keeps the old
        # behavior.
        self._ceiling_give_up = ceiling_give_up
        self._ceiling_misses = 0
        self.gave_up = False

        self._last_direction: int | None = None  # +1 = level rose, -1 = fell
        self._reversal_levels: list[float] = []
        self._halved = False

    @property
    def done(self) -> bool:
        return self.gave_up or len(self._reversal_levels) >= self._reversals_needed

    @property
    def reversals(self) -> int:
        """How many reversals have accumulated so far -- UI convergence
        feedback ("narrowing: 3/5"), not part of the threshold math."""
        return len(self._reversal_levels)

    @property
    def reversals_needed(self) -> int:
        return self._reversals_needed

    @property
    def threshold(self) -> float | None:
        if self.gave_up:
            return self._ceil  # lower bound: "at or beyond the testable maximum"
        if not self.done:
            return None
        tail = self._reversal_levels[-(self._reversals_needed - 1):]
        return sum(tail) / len(tail)

    def respond(self, heard: bool) -> None:
        if self.done:
            return

        if self._ceiling_give_up is not None:
            if not heard and self.level >= self._ceil:
                self._ceiling_misses += 1
                if self._ceiling_misses >= self._ceiling_give_up:
                    self.gave_up = True
                    return
            elif heard:
                self._ceiling_misses = 0

        step_down = self._step_down / 2 if self._halved else self._step_down
        step_up = self._step_up / 2 if self._halved else self._step_up

        if heard:
            new_level = self.level - step_down
            direction = -1
        else:
            new_level = self.level + step_up
            direction = 1

        new_level = max(self._floor, min(self._ceil, new_level))

        # Pinned-at-boundary counts as a reversal too, not just a direction
        # flip: if the level is already sitting on the floor/ceiling and the
        # response keeps pushing further the same way, `new_level` clamps
        # right back to `self.level` and a literal direction change can
        # never happen. Left as a bare direction check, a listener whose
        # true threshold sits beyond the testable range (very plausible at
        # the -90 dBFS floor on a comfortable volume) could answer forever
        # without ever producing a reversal -- the staircase would never
        # reach `reversals_needed` and the UI would look permanently dead on
        # that (ear, freq) point. Treating repeated pinning as a reversal
        # bounces the staircase off the wall like a normal reversal would,
        # so it still converges (to the boundary value, i.e. "at or beyond
        # this limit") instead of hanging.
        pinned = (
            new_level == self.level
            and self.level in (self._floor, self._ceil)
            and self._last_direction == direction
        )

        if self._last_direction is not None and (direction != self._last_direction or pinned):
            self._reversal_levels.append(self.level)
            if not self._halved:
                self._halved = True

        self.level = new_level
        self._last_direction = direction


# ---------------------------------------------------------------------------
# Test A: per-ear frequency threshold staircase
# ---------------------------------------------------------------------------

# Full 11-point sweep (spec §12a). Roughly one-third-octave-ish spacing,
# widened at the extremes where consumer speakers/headphones are least
# reliable (hence 16 kHz being the UI-skippable escape hatch).
STANDARD_FREQS = [125, 250, 500, 1000, 2000, 3000, 4000, 6000, 8000, 12000, 16000]

# The baseline subset of STANDARD_FREQS: the five most informative points,
# anchored at 1000 Hz and touching the low/high extremes. Historically this
# was a separate selectable "Quick" test (hence the COARSE name, kept for
# compatibility); in the single progressive test it is instead the FIRST
# HALF of every calibration -- once these points are measured for both ears,
# the calibration is usable ("baseline complete") and everything after adds
# detail. See progressive_order() / baseline_complete().
COARSE_FREQS = [250, 1000, 4000, 8000, 16000]
BASELINE_FREQS = COARSE_FREQS


#: How many consecutive "not heard" answers at the ceiling end a point --
#: user-specified rule ("can't hear it twice in a row at the loudest level").
_CEILING_GIVE_UP = 2

#: Seed margin for a new point's start level: one full coarse step
#: (Staircase's default step_down) above the same ear's threshold at the
#: nearest already-measured frequency. Adjacent-frequency thresholds
#: correlate to within a few dB, so starting one coarse step above the
#: neighbour makes the first presentation almost always heard and drops the
#: staircase straight into the fine search near the target, instead of
#: walking down from -30 dBFS from cold every time.
_SEED_MARGIN_DB = 8.0

#: Test A's level bounds, mirroring Staircase's own defaults -- used to
#: clamp seeded start levels into the presentable range.
_LEVEL_FLOOR_DBFS = -90.0
_LEVEL_CEIL_DBFS = -10.0


def _outward_sweep(frequencies: list[int]) -> list[int]:
    """Order a frequency set for testing: the 1 kHz anchor first (the tone
    normalise() anchors each ear's curve to, and the easiest one to learn
    the task on), then the highs ascending, then the lows descending. Each
    direction thus moves strictly outward from the anchor, which is what
    makes both threshold seeding (seed from the adjacent-inward point) and
    the directional give-up abort (can't hear this extreme -> won't hear a
    more extreme one) possible. Falls back to the log-nearest frequency to
    1000 Hz as the anchor if 1000 itself isn't in the set."""
    if not frequencies:
        return []
    anchor = min(frequencies, key=lambda f: abs(np.log2(f / 1000.0)))
    highs = sorted(f for f in frequencies if f > anchor)
    lows = sorted((f for f in frequencies if f < anchor), reverse=True)
    return [anchor] + highs + lows


def progressive_order(frequencies: list[int] = STANDARD_FREQS,
                       baseline: list[int] = BASELINE_FREQS) -> list[int]:
    """Order a frequency set for the single progressive calibration: the
    baseline subset first (each an outward sweep, so anchoring/seeding/
    give-up semantics hold within each half), then the remaining detail
    frequencies. Front-loads the informative points -- stopping after the
    baseline half leaves a usable calibration, and every detail point after
    it is bracketed by two already-measured baseline neighbours, so its
    staircase seeds from an interpolation instead of a cold start."""
    baseline_set = set(baseline)
    base = [f for f in frequencies if f in baseline_set]
    detail = sorted(f for f in frequencies if f not in baseline_set)
    return _outward_sweep(base) + _outward_sweep(detail)


def baseline_complete(raw: dict, baseline: list[int] = BASELINE_FREQS) -> bool:
    """True when every baseline (ear, freq) point has been RESOLVED --
    measured or explicitly skipped (present in the dict, even as None; a
    listener who skipped 16 kHz shouldn't be told forever that their
    baseline is incomplete). The single test's "you can stop here" milestone."""
    return all(freq in raw.get(ear, {})
               for ear in ("left", "right") for freq in baseline)


class FrequencyTest:
    """Test A orchestrator: runs an independent Staircase for every
    (ear, frequency) pair, interleaving ears within each frequency (left,
    right, left, right, ...) so a listener never does a whole ear's sweep
    before switching -- this keeps fatigue/attention drift from biasing one
    ear more than the other.

    Frequencies are visited in progressive_order() (the baseline subset's
    outward sweep first, then the detail frequencies -- see that function),
    each new point's staircase starts one coarse step above the same ear's
    nearest already-measured threshold (interpolated when two measured
    neighbours bracket the new point), and a point the listener can't hear
    at all (two consecutive "not heard" at the ceiling -> Staircase.gave_up)
    records the ceiling as its "at or beyond" threshold AND turns that
    ear's remaining points MORE EXTREME than the give-up frequency into
    CEILING PROBES: presented with their staircase started at the ceiling
    instead of being auto-skipped. A truly inaudible probe resolves in
    exactly two "not heard" clicks (the same ceiling give-up rule), so a
    listener who genuinely can't hear anything past 8 kHz spends almost
    nothing on 12/16 kHz -- but a listener whose HARDWARE has a narrow
    null at the give-up frequency (common in the 8-12 kHz region for
    headphones) gets their genuinely-hearable 12 kHz measured normally
    instead of silently dropped as the old auto-skip did. Points closer in
    than the boundary (e.g. a later-queued 3 kHz detail point) are
    unaffected either way. Giving up on the 1 kHz anchor itself still
    skips the whole ear (None, never probed): without an anchor reading,
    normalise() can't build that ear's curve, so there is nothing a
    measurement could feed.

    Dashboard mode (the single progressive test's per-point control):
    *pairs*, when given, is an explicit (ear, freq) queue -- a single
    point's re-test, "remaining points only", "flagged points only" -- and
    overrides *frequencies* entirely. *seed_thresholds*, when given, is a
    {"left": {freq: dbfs}, "right": {...}} dict of already-known thresholds
    (this listener's persistent calibration state, possibly from earlier
    sessions) used as additional seeding neighbours -- this-run results
    still take precedence where both exist.
    """

    def __init__(self, frequencies: list[int] = STANDARD_FREQS, *,
                 pairs: list[tuple[str, int]] | None = None,
                 seed_thresholds: dict | None = None) -> None:
        if pairs is not None:
            self._pairs = [(ear, int(freq)) for ear, freq in pairs]
            # Directional give-up stays anchored at the canonical 1 kHz
            # reference regardless of what's in the queue, so "can't hear
            # 8 kHz -> skip queued 12/16 kHz" still works on partial queues.
            self._anchor: int | None = 1000
        else:
            ordered = progressive_order(list(frequencies))
            self._anchor = ordered[0] if ordered else None
            self._pairs = [
                (ear, freq) for freq in ordered for ear in ("left", "right")
            ]
        self._seed_thresholds: dict[str, dict[int, float]] = {
            ear: {int(f): float(v)
                  for f, v in (seed_thresholds or {}).get(ear, {}).items()
                  if v is not None}
            for ear in ("left", "right")
        }
        self._index = 0
        self.results: dict[str, dict[int, float | None]] = {"left": {}, "right": {}}
        #: (ear, freq) pairs the listener could not hear at the loudest
        #: presentable level -- see respond(). Their `results` entry is the
        #: ceiling level, i.e. "at or beyond the testable maximum", not a
        #: real threshold; callers that present readings to a user should
        #: show these differently.
        self.gave_up_pairs: set[tuple[str, int]] = set()
        # Per ear: the frequency at which each direction gave up ("couldn't
        # hear it at the loudest testable level"). Points MORE extreme than
        # that boundary become ceiling probes (see the class docstring) --
        # never points closer in: with the progressive order (baseline
        # extremes before detail infill), a whole-direction rule would
        # wrongly demote hearable detail points inside the boundary.
        self._give_up_freq: dict[str, dict[str, int]] = {"left": {}, "right": {}}
        # Ears whose 1 kHz anchor itself gave up -- the one case that still
        # hard-skips (records None without presenting): an ear with no
        # anchor reading has no usable curve for any measurement to join.
        self._anchor_gave_up: set[str] = set()
        self.staircase: Staircase | None = self._new_staircase() if self._pairs else None

    @property
    def current(self) -> tuple[str, int] | None:
        if self._index >= len(self._pairs):
            return None
        return self._pairs[self._index]

    @property
    def done(self) -> bool:
        return self._index >= len(self._pairs)

    def _new_staircase(self) -> Staircase:
        ear, freq = self._pairs[self._index]
        if self._beyond_give_up(ear, freq):
            # Ceiling probe: start at the loudest presentable level, so the
            # existing ceiling give-up rule resolves a truly inaudible
            # point in exactly two responses -- and a hearable one (the
            # hardware-null case the old auto-skip got wrong) falls
            # straight into a normal descending search.
            return Staircase(start_level=_LEVEL_CEIL_DBFS,
                             ceiling_give_up=_CEILING_GIVE_UP)
        # Persistent-state seeds first, this run's own results on top --
        # a threshold measured minutes ago beats one from a prior session.
        measured = dict(self._seed_thresholds[ear])
        measured.update(
            {f: v for f, v in self.results[ear].items() if v is not None})
        measured.pop(freq, None)  # re-testing a point: don't seed from itself
        if measured:
            lows = [f for f in measured if f < freq]
            highs = [f for f in measured if f > freq]
            if lows and highs:
                # Bracketed (every detail point after the baseline half, and
                # most re-tests): log-frequency interpolation between the
                # nearest measured neighbours on each side -- thresholds
                # vary smoothly enough across frequency that this starts the
                # staircase tighter than either neighbour alone.
                f_lo, f_hi = max(lows), min(highs)
                t = float(np.log2(freq / f_lo) / np.log2(f_hi / f_lo))
                estimate = measured[f_lo] * (1.0 - t) + measured[f_hi] * t
            else:
                nearest = min(measured, key=lambda f: abs(np.log2(f / freq)))
                estimate = measured[nearest]
            seed = estimate + _SEED_MARGIN_DB
            seed = max(_LEVEL_FLOOR_DBFS, min(_LEVEL_CEIL_DBFS, seed))
            return Staircase(start_level=seed, ceiling_give_up=_CEILING_GIVE_UP)
        return Staircase(ceiling_give_up=_CEILING_GIVE_UP)

    def _direction(self, freq: int) -> str | None:
        if self._anchor is None or freq == self._anchor:
            return None
        return "high" if freq > self._anchor else "low"

    def _should_skip(self, ear: str, freq: int) -> bool:
        # Only an anchor give-up hard-skips (whole ear unusable) -- beyond-
        # boundary points are ceiling-probed instead, see _beyond_give_up.
        return ear in self._anchor_gave_up

    def _beyond_give_up(self, ear: str, freq: int) -> bool:
        """True when *freq* lies beyond the frequency at which this ear
        already gave up in that direction -- presented as a ceiling probe
        (staircase started at the ceiling: two clicks to confirm "still
        can't hear it", a normal descending search if the listener can)."""
        direction = self._direction(freq)
        if direction is None:
            return False
        boundary = self._give_up_freq[ear].get(direction)
        if boundary is None:
            return False
        return freq > boundary if direction == "high" else freq < boundary

    def _advance(self) -> None:
        self._index += 1
        # Hard-skip only an anchor-gave-up ear's remaining pairs -- recorded
        # as None (unmeasured), same as a manual skip(). Beyond-boundary
        # pairs are NOT skipped here; they get a ceiling-probe staircase.
        while self._index < len(self._pairs):
            ear, freq = self._pairs[self._index]
            if not self._should_skip(ear, freq):
                break
            self.results[ear][freq] = None
            self._index += 1
        self.staircase = self._new_staircase() if self._index < len(self._pairs) else None

    def respond(self, heard: bool) -> None:
        if self.done:
            return
        self.staircase.respond(heard)
        if self.staircase.done:
            ear, freq = self._pairs[self._index]
            self.results[ear][freq] = self.staircase.threshold
            if self.staircase.gave_up:
                # Record WHICH points hit the ceiling. A gave-up point's
                # threshold is the ceiling level, but so is a point whose
                # search legitimately converges there, so the threshold value
                # alone cannot distinguish "couldn't hear this at all" from
                # "only just heard it at the maximum" -- and the dashboard
                # shows those differently ("limit" vs a real reading). The
                # staircase knows; this is the only place that knowledge
                # survives, so it is captured rather than re-guessed later.
                self.gave_up_pairs.add((ear, freq))
                direction = self._direction(freq)
                boundaries = self._give_up_freq[ear]
                if direction == "high":
                    boundaries["high"] = min(boundaries.get("high", freq), freq)
                elif direction == "low":
                    boundaries["low"] = max(boundaries.get("low", freq), freq)
                else:
                    # Couldn't hear the anchor itself at the maximum level:
                    # hard-skip this ear's whole remaining sweep -- with no
                    # anchor, normalise() can't use any of its readings.
                    self._anchor_gave_up.add(ear)
            self._advance()

    def skip(self) -> None:
        """Mark the current (ear, freq) pair as unmeasured and move on --
        the 16 kHz escape hatch for hardware/listeners that can't reproduce
        it at all.
        """
        if self.done:
            return
        ear, freq = self._pairs[self._index]
        self.results[ear][freq] = None
        self._advance()


def normalise(results: dict) -> dict:
    """Return {"left": {...}, "right": {...}, "imbalance": {...}}.

    "left"/"right" are each ear's raw thresholds expressed relative to that
    SAME ear's own 1000 Hz reading (so each ear's own curve reads 0.0 at
    1000 Hz by construction) -- this is the shape of one ear relative to
    itself, used for the per-ear chart curves.

    "imbalance" is deliberately computed from the RAW results, not from the
    two curves above: both ears' own-1kHz-anchored curves are forced to 0.0
    at 1000 Hz, so an imbalance computed from them would hide any real,
    constant left/right offset (spec §12a: both ears share the same
    uncalibrated output stage, so a raw difference is meaningful regardless
    of calibration).
    """
    raw = results
    normalised: dict[str, dict[int, float]] = {}
    anchors: dict[str, float | None] = {}

    for ear in ("left", "right"):
        ear_raw = raw.get(ear, {})
        anchor = ear_raw.get(1000)
        anchors[ear] = anchor
        if anchor is None:
            normalised[ear] = {}
        else:
            normalised[ear] = {
                freq: value - anchor
                for freq, value in ear_raw.items()
                if value is not None
            }

    imbalance: dict[int, float] = {}
    if anchors["left"] is not None and anchors["right"] is not None:
        left_raw = raw.get("left", {})
        right_raw = raw.get("right", {})
        for freq in sorted(set(left_raw) & set(right_raw)):
            left_val = left_raw[freq]
            right_val = right_raw[freq]
            if left_val is not None and right_val is not None:
                imbalance[freq] = left_val - right_val

    normalised["imbalance"] = imbalance
    return normalised


def frequency_flags(norm: dict, threshold_db: float = 6.0) -> list[int]:
    """Soft, speculative outlier note (spec §12a): frequencies where either
    ear's own-anchored normalised value exceeds threshold_db in magnitude --
    "this reads unusually relative to the rest of this same test." This is
    NOT a clinical or referral criterion (unlike Test B's REFERRAL_SNR_DB);
    threshold_db=6.0 is a starting point for planning discussion, not a
    researched value, and only drives a softly-worded UI note.
    """
    flags: list[int] = []
    freqs = set(norm.get("left", {})) | set(norm.get("right", {}))
    for freq in sorted(freqs):
        left_val = norm.get("left", {}).get(freq)
        right_val = norm.get("right", {}).get(freq)
        if (left_val is not None and abs(left_val) > threshold_db) or (
            right_val is not None and abs(right_val) > threshold_db
        ):
            flags.append(freq)
    return flags


def volume_drift_note(current: dict | None, prior: dict | None,
                       threshold_db: float = 6.0) -> str:
    """Soft, speculative note (same shape as frequency_flags above, and
    reusing its exact threshold_db=6.0 -- not a researched value, just
    reusing an already-established "starting point for discussion" from
    elsewhere in this module rather than inventing a new arbitrary number):
    if this run's captured volume-reference (`current`) and the PRIOR run's
    (`prior`) both have a readable Windows-volume dB, and they differ by
    more than threshold_db, this run's test conditions may not be
    comparable to the last one. Compares dB, not the displayed percent --
    percent is a linear scalar, not perceptually uniform, so a 10-point
    swing means very different things at 20% vs 80%; dB is the right unit
    for "is this a meaningfully different loudness."

    Returns "" when there's nothing to say (no prior run, either reading is
    unavailable, or the drift is small) -- never returns anything about
    absolute loudness, only about the two runs' relative Windows-volume
    difference.
    """
    if not current or not prior:
        return ""
    cur_db = current.get("windows_volume_db")
    prior_db = prior.get("windows_volume_db")
    if cur_db is None or prior_db is None:
        return ""
    diff = cur_db - prior_db
    if abs(diff) <= threshold_db:
        return ""
    direction = "louder" if diff > 0 else "quieter"
    cur_pct = current.get("windows_volume_percent")
    prior_pct = prior.get("windows_volume_percent")
    detail = ""
    if cur_pct is not None and prior_pct is not None:
        detail = f" (was {prior_pct:.0f}%, now {cur_pct:.0f}%)"
    return (
        f"Note: your Windows volume was noticeably {direction} for this "
        f"run than your last calibration{detail} -- results may be less "
        "directly comparable across the two runs. Not a problem on its "
        "own, just worth knowing if you're comparing runs over time.")


def summarise(norm: dict) -> str:
    """Plain-language sentence about `imbalance` only -- frequency_flags is
    a separate, visually-softer UI element and is intentionally not
    mentioned here.
    """
    imbalance = norm.get("imbalance", {})
    if not imbalance or all(abs(value) < 3.0 for value in imbalance.values()):
        return "Ears closely matched."

    freq, value = max(imbalance.items(), key=lambda item: abs(item[1]))
    if value < 0:
        lower_ear, other_ear = "Left", "right"
    else:
        lower_ear, other_ear = "Right", "left"
    return f"{lower_ear} tracks ~{abs(value):.0f} dB below {other_ear} at {freq} Hz."


def export_text(norm: dict, raw: dict, device_label: str,
                 volume_reference: dict | None = None,
                 no_response: dict | None = None) -> str:
    """Plain-text export: header + disclaimer, per-ear relative-dB tables,
    an imbalance table, an optional volume-reference note, and a closing
    instruction paragraph aimed at an AI that will read this file to author
    a compensation profile.

    `volume_reference`, when given, is the dict captured by
    rumble/ui/hearing_dialog.py's intro-page volume-setting step (see that
    module) -- {"windows_volume_percent", "windows_volume_db", "gear_note",
    "measured_spl_db", "captured_at"}. It is deliberately scoped as a same-machine,
    same-hardware consistency anchor, not a portable calibration value --
    see the caveat line this function emits alongside it -- so an AI or a
    future you reading this file doesn't mistake it for an absolute SPL
    measurement it was never able to be.

    `no_response`, when given, is {"left": [freq, ...], "right": [...]}: the
    frequencies that were tested but never heard, even at the loudest testable
    level (listener_slots.no_response_freqs()). These are deliberately ABSENT
    from the tables above and from the imbalance -- they are censored
    observations, not measurements, and the correction excludes them (see
    listener_slots.calibration_raw) -- but they are the single most important
    thing on the page for whoever reads it, so they get their own section
    rather than being silently dropped or, worse, mislabelled "skipped" as
    though the listener had just declined to test them.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = [
        "RUMBLE Ear Calibration -- Audio Response Profile",
        f"Date: {date_str}",
        f"Device: {device_label}",
        "Relative measurement on your current hardware — not a medical "
        "evaluation.",
        "",
    ]

    for ear in ("left", "right"):
        lines.append(f"{ear.capitalize()} ear (relative dB from this ear's own 1000 Hz reading):")
        lines.append("freq_hz | relative_db")
        ear_raw = raw.get(ear, {})
        ear_norm = norm.get(ear, {})
        # A no-response point also arrives as None (it is censored out of the
        # math), but it is emphatically NOT "skipped" -- the listener tested it
        # and heard nothing. Distinguish the two or the table contradicts the
        # No-response section below.
        ear_no_response = set((no_response or {}).get(ear, ()))
        for freq in sorted(ear_raw):
            if freq in ear_no_response:
                lines.append(f"{freq} | no response")
                continue
            raw_val = ear_raw[freq]
            if raw_val is None:
                lines.append(f"{freq} | skipped")
                continue
            norm_val = ear_norm.get(freq)
            if norm_val is None:
                lines.append(f"{freq} | skipped")
            else:
                lines.append(f"{freq} | {norm_val:.1f}")
        lines.append("")

    lines.append("Imbalance (left minus right, raw dB, at frequencies both ears measured):")
    lines.append("freq_hz | imbalance_db")
    for freq in sorted(norm.get("imbalance", {})):
        lines.append(f"{freq} | {norm['imbalance'][freq]:.1f}")
    lines.append("")

    if no_response and any(no_response.get(ear) for ear in ("left", "right")):
        lines.append(
            "No response (tested, but the tone was never heard even at the "
            "loudest testable level):")
        lines.append("ear | freq_hz")
        for ear in ("left", "right"):
            for freq in no_response.get(ear, []):
                lines.append(f"{ear} | {freq}")
        lines.append(
            "  These frequencies have NO threshold: the true value is above "
            "the test ceiling, not at it. They are excluded from the tables "
            "and the imbalance above, and from any correction -- boosting a "
            "band the ear cannot hear at any testable level only steals "
            "headroom from the bands it can.")
        lines.append("")

    if volume_reference:
        lines.append("Volume reference (captured when this run started):")
        percent = volume_reference.get("windows_volume_percent")
        lines.append(
            f"  Windows volume: {percent:.0f}%" if percent is not None
            else "  Windows volume: not available")
        gear_note = (volume_reference.get("gear_note") or "").strip()
        if gear_note:
            lines.append(f"  External volume note (user-entered): {gear_note}")
        measured_spl = volume_reference.get("measured_spl_db")
        if measured_spl is not None:
            lines.append(f"  Measured level (user-entered, real SPL meter): {measured_spl:.1f} dB SPL")
        captured_at = volume_reference.get("captured_at")
        if captured_at:
            lines.append(f"  Captured: {captured_at}")
        lines.append(
            "  Scope: only meaningful on THIS machine with THIS exact audio "
            "hardware (same headphones/speakers/amp, same physical volume "
            "settings) -- it does not transfer to a different device or "
            "setup, and is not an absolute loudness measurement.")
        lines.append("")

    lines.append(
        "Instruction for an AI reading this file: Author an Equalizer APO "
        "GraphicEQ or parametric compensation profile targeting gentle "
        "correction of the measured imbalance; cap any single boost at "
        "+6 dB; do not attempt to correct for absolute hearing loss, since "
        "these thresholds are relative to this listener's own 1 kHz "
        "reading on uncalibrated hardware, not a clinical measurement."
    )

    return "\n".join(lines)


def save_results(norm: dict, raw: dict, data_dir: Path,
                  volume_reference: dict | None = None) -> Path:
    """Persist one Test A run as timestamped JSON under
    data_dir/hearing/frequency/. Filenames carry second-resolution UTC
    timestamps; if two runs land in the same second, a numeric suffix
    disambiguates so neither save silently overwrites the other.

    `volume_reference`, when given, is stored alongside norm/raw as-is (see
    export_text's docstring for its shape and scope) so a later run for the
    same Listener Slot can show "here's what you set last time" before the
    listener starts adjusting their volume again.
    """
    out_dir = Path(data_dir) / "hearing" / "frequency"
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{base_name}.json"
    suffix = 1
    while path.exists():
        path = out_dir / f"{base_name}_{suffix}.json"
        suffix += 1

    payload = {"norm": norm, "raw": raw}
    if volume_reference is not None:
        payload["volume_reference"] = volume_reference
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_all_results(data_dir: Path) -> list[dict]:
    """Load every saved Test A run under data_dir/hearing/frequency/,
    oldest to newest. JSON round-trips dict keys as strings, so frequency
    keys are restored to int for norm's/raw's "left"/"right"/"imbalance"
    sub-dicts.
    """
    out_dir = Path(data_dir) / "hearing" / "frequency"
    if not out_dir.exists():
        return []

    files = sorted(out_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))

    payloads: list[dict] = []
    for file_path in files:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        for section in ("norm", "raw"):
            section_data = payload.get(section, {})
            for key, sub in list(section_data.items()):
                section_data[key] = {int(freq): value for freq, value in sub.items()}
        payloads.append(payload)
    return payloads


# ---------------------------------------------------------------------------
# Test A playback context + audio glue
# ---------------------------------------------------------------------------


class BypassGuard:
    """Context manager: forces APO bypass for the duration of the block,
    then restores whatever bypass state was active before -- always, even
    on an exception. Mirrors Phase 5's ABXRunner restore guarantee."""

    def __init__(self, manager) -> None:
        self._manager = manager
        self._previous: bool | None = None

    def __enter__(self) -> "BypassGuard":
        self._previous = self._manager.is_bypassed()
        if not self._previous:
            self._manager.set_bypass(True)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._manager.set_bypass(self._previous)
        return False


def start_tone(engine, freq: float, level_dbfs: float, ear: str,
                sample_rate: int, duration_s: float = 1.0) -> None:
    """Start one calibrated test tone playing and return immediately --
    the non-blocking primitive under present_tone(). The UI drives its
    three-repeat presentation off this (sequenced by a QTimer polling
    engine.is_playing) so the event loop keeps running during playback:
    the "tone playing" light can actually light up, and pause/answer
    clicks land immediately instead of queuing behind a time.sleep."""
    buffer = _tones.generate_sine(freq, duration_s, level_dbfs, sample_rate,
                                   channel=ear, fade_ms=10.0)
    engine.play(buffer, sample_rate)


def present_tone(engine, freq: float, level_dbfs: float, ear: str,
                  sample_rate: int, duration_s: float = 1.0) -> None:
    """Blocking convenience wrapper over start_tone() -- plays and waits."""
    start_tone(engine, freq, level_dbfs, ear, sample_rate, duration_s)
    while engine.is_playing:
        time.sleep(0.01)


def load_volume_reference() -> tuple[np.ndarray, int]:
    """Load rumble/assets/audio/volume_reference.wav -- a committed,
    bracketed spoken reference clip (16-bit PCM mono) -- as a float32
    stereo buffer plus its sample rate, for the Frequency Calibration intro
    page's volume-setting step: play this, and have the listener adjust
    their real-world (OS AND any external amp/speaker/headphone) volume so
    the clip's TARGET segment (bracketed either side by a quieter and a
    louder segment, so the listener hears where "too quiet"/"too loud" sit
    relative to it) feels right -- roughly as loud as a hair dryer, a
    whistling kettle, a school bell, a pedestrian crossing signal, or a
    whistle blast (see loudness_layer.REFERENCE_CLIP_TARGET_PHON, ~75 dB
    SPL) -- before the actual staircase runs. Duplicated to both channels
    (rather than left as mono) so it plays identically in both ears
    regardless of how the output device maps a mono stream -- the level
    judgement this step exists for depends on that.
    """
    with wave.open(str(_VOLUME_REFERENCE_PATH), "rb") as wf:
        sample_rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    stereo = np.column_stack([samples, samples]).astype(np.float32)
    return stereo, sample_rate


#: (key, display label) for every standalone reference-sound sample under
#: rumble/assets/audio/reference_sounds/ -- each independently normalized to
#: the same target RMS as volume_reference.wav's own target segment (see
#: loudness_layer.REFERENCE_CLIP_TARGET_PHON), so any one of them played at
#: whatever real-world volume the listener matched to the main clip should
#: land at roughly the same ~75 dB SPL anchor. Exposed so the Settings ->
#: Ear Calibration page can offer on-demand "check your volume" playback
#: without opening the full Ear Calibration dialog/staircase.
REFERENCE_SOUNDS: tuple[tuple[str, str], ...] = (
    ("hair_dryer", "Hair dryer"),
    ("kettle_whistle", "Whistling kettle"),
    ("school_bell", "School bell"),
    ("crossing_beep", "Pedestrian crossing signal"),
    ("guard_whistle", "Whistle blast"),
)

_REFERENCE_SOUNDS_DIR = (
    Path(__file__).resolve().parent.parent / "assets" / "audio" / "reference_sounds")


def load_reference_sound(key: str) -> tuple[np.ndarray, int]:
    """Load one of REFERENCE_SOUNDS by key as a float32 stereo buffer plus
    its sample rate -- same shape as load_volume_reference(), so both play
    through the same ToneEngine.play() call. Raises FileNotFoundError for
    an unknown key rather than silently returning nothing to play."""
    path = _REFERENCE_SOUNDS_DIR / f"{key}.wav"
    with wave.open(str(path), "rb") as wf:
        sample_rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    stereo = np.column_stack([samples, samples]).astype(np.float32)
    return stereo, sample_rate
