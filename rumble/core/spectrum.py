"""Spectrum display shaping (Phase 6: Live Spectrum Analyzer, Task 1).

Pure numpy signal processing for real-time spectrum display: interpolation,
pink-noise tilt compensation, exponential smoothing, and peak-hold decay.
No Qt, no sounddevice — this module takes spectrum arrays in and returns
shaped/transformed spectrum arrays out.

Public API:
    SpectrumShaper       -- stateful class for smoothed spectrum display
    subtract_response    -- "Source (estimated)" transform (element-wise subtract)
    broadband_offset_db  -- subtract_response reduced to one scalar dB offset,
                            for the VU/Peak/Reference level meter
"""
from __future__ import annotations

import math

import numpy as np

# Floor for spectrum magnitudes (matches metering.FLOOR_DBFS).
FLOOR_DBFS = -120.0

# ── attack/release defaults, derived from named ballistics standards ────────
#
# IEC 60268-10 / ANSI S1.4-style real-time-analyzer detector ballistics define
# two named integration time constants: "Fast" (tau ~= 125 ms) and "Slow"
# (tau ~= 1000 ms). This module drives its exponential smoothing from those
# standard time constants rather than a hand-picked coefficient, converted to
# a per-tick coefficient via the standard EWMA formula
#
#     alpha = 1 - exp(-dt / tau)
#
# using this project's own already-fixed UI update tick (Task 2/3's 33 ms
# QTimer -- rumble/ui/spectrum_overlay.py) as `dt`. If that tick interval ever
# changes, recompute these two constants from the same formula rather than
# hand-adjusting them.
_TICK_INTERVAL_S = 0.033        # Task 2/3's QTimer drain interval
_ATTACK_TIME_CONSTANT_S = 0.125  # IEC "Fast" ballistics
_RELEASE_TIME_CONSTANT_S = 1.0   # IEC "Slow" ballistics
DEFAULT_ATTACK = 1.0 - math.exp(-_TICK_INTERVAL_S / _ATTACK_TIME_CONSTANT_S)
DEFAULT_RELEASE = 1.0 - math.exp(-_TICK_INTERVAL_S / _RELEASE_TIME_CONSTANT_S)

# Peak-hold trace decay: same IEC 60268-10 ~20 dB/s linear (dB-domain, not
# exponential) release rate rumble.core.level_meter.PEAK_DECAY_DB_PER_TICK
# derives for the sibling VU/Peak meter, converted to this module's own tick
# interval rather than duplicated as a separately-tuned number.
PEAK_HOLD_DECAY_DB_PER_TICK = 20.0 * _TICK_INTERVAL_S

# Pink noise has PSD(f) ~ 1/f, so its expected FFT bin magnitude ~ f^-0.5 (for
# fixed bin bandwidth). metering.spectrum_mags reports magnitude in 20*log10
# dB, so the expected falloff is 20*log10(2**-0.5) = -10*log10(2) ~= -3.01 dB
# per octave -- this is where the "pink noise is -3 dB/octave" convention
# actually comes from for an amplitude-dB (not power-dB) display. +3.0 dB/oct
# is therefore the physically correct compensation for this exact convention,
# not a tuned constant.
DEFAULT_TILT_DB_PER_OCT = 3.0


class SpectrumShaper:
    """Interpolate, tilt-compensate, and smooth a spectrum magnitude curve.

    Consumes raw FFT magnitude spectra and produces a display spectrum that:
    1. Interpolates from arbitrary frequency points onto a fixed log-spaced axis.
    2. Applies pink-noise tilt compensation so 1/f noise reads flat.
    3. Per-frequency exponential smoothing with asymmetric attack/release.
    4. Decaying peak-hold trace.

    This is the core of the live spectrum analyzer's front-end processing.
    """

    def __init__(
        self,
        freq_points: np.ndarray,
        sample_rate: int,
        attack: float = DEFAULT_ATTACK,
        release: float = DEFAULT_RELEASE,
        tilt_db_per_oct: float = DEFAULT_TILT_DB_PER_OCT,
    ) -> None:
        """Initialize the spectrum shaper.

        Args:
            freq_points: Log-spaced frequency axis (e.g. from eq_math.frequency_array()).
                         The shaper interpolates all incoming spectra onto this grid.
            sample_rate: Sample rate of the incoming audio (used for context).
            attack: Smoothing coefficient for rising magnitudes (0 = no smoothing, 1 = no update).
                    Defaults to the IEC "Fast" ballistics constant (see DEFAULT_ATTACK above).
            release: Smoothing coefficient for falling magnitudes (0 = no smoothing, 1 = no update).
                    Defaults to the IEC "Slow" ballistics constant (see DEFAULT_RELEASE above).
            tilt_db_per_oct: Pink-noise tilt compensation in dB/octave. Positive value
                           raises low frequencies relative to high to flatten pink noise.
        """
        self.freq_points = np.asarray(freq_points, dtype=np.float64)
        self.sample_rate = int(sample_rate)
        self.attack = float(attack)
        self.release = float(release)
        self.tilt_db_per_oct = float(tilt_db_per_oct)

        # Running state: the current smoothed spectrum. Starts uninitialized
        # (rather than pre-seeded with an arbitrary floor value) so the very
        # first update() reports its tilted reading directly instead of being
        # dragged toward -120 dB by one smoothing step from a floor that was
        # never a real measurement.
        self._running_state: np.ndarray | None = None

        # Peak-hold: decaying peak trace (initialized to floor).
        self._peak = np.full(len(self.freq_points), FLOOR_DBFS, dtype=np.float64)

    def update(self, freqs_hz: np.ndarray, mags_db: np.ndarray) -> np.ndarray:
        """Consume a new spectrum and return the shaped output.

        Args:
            freqs_hz: Frequency array from the FFT (arbitrary length/spacing).
            mags_db: Magnitude array in dB (same length as freqs_hz).

        Returns:
            Shaped magnitude spectrum, same length as freq_points.
        """
        freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
        mags_db = np.asarray(mags_db, dtype=np.float64)

        # Step 1: Interpolate onto freq_points using log-frequency space.
        # Filter out DC (f <= 0) to avoid log10(0) = -inf, but keep all positive freqs.
        mask = freqs_hz > 0
        freqs_hz_valid = freqs_hz[mask]
        mags_db_valid = mags_db[mask]

        # Use log10 as the interpolation domain since freq_points is log-spaced.
        log_freqs_hz = np.log10(freqs_hz_valid)
        log_freq_points = np.log10(self.freq_points)
        interpolated = np.interp(log_freq_points, log_freqs_hz, mags_db_valid, left=FLOOR_DBFS, right=FLOOR_DBFS)

        # Step 2: Apply pink-noise tilt compensation BEFORE smoothing.
        # See DEFAULT_TILT_DB_PER_OCT above for the derivation: -3.01 dB/octave
        # falloff in THIS module's 20*log10 magnitude convention (not a
        # separate "power vs magnitude" distinction -- 20*log10(amplitude
        # ratio) and 10*log10(power ratio) are the same number). We add the
        # tilt to compensate: + tilt_db_per_oct * log2(f/1000).
        log2_freq_norm = np.log2(self.freq_points / 1000.0)
        tilted = interpolated + self.tilt_db_per_oct * log2_freq_norm

        # Step 3: Per-frequency exponential smoothing.
        # out[i] = prev[i] + coef * (new[i] - prev[i])
        # where coef = attack if new > prev (rising), else release (falling).
        # The very first call has no real "previous" reading to smooth from --
        # only an arbitrary floor -- so it reports the tilted value directly.
        if self._running_state is None:
            smoothed = tilted.copy()
        else:
            rising = tilted > self._running_state
            coef = np.where(rising, self.attack, self.release)
            smoothed = self._running_state + coef * (tilted - self._running_state)

        # Update the running state in place for the next call.
        self._running_state = smoothed

        # Step 4: Decaying peak-hold.
        # peak[i] = max(peak[i] - PEAK_HOLD_DECAY_DB_PER_TICK, smoothed[i])
        self._peak = np.maximum(self._peak - PEAK_HOLD_DECAY_DB_PER_TICK, smoothed)

        return smoothed

    @property
    def peak(self) -> np.ndarray:
        """Decaying peak-hold trace (same length as freq_points)."""
        return self._peak.copy()

    def reset(self) -> None:
        """Reset the running state to uninitialized (next update() reports
        directly, unsmoothed) and the peak-hold trace to the floor."""
        self._running_state = None
        self._peak.fill(FLOOR_DBFS)


def subtract_response(spectrum_db: np.ndarray, response_db: np.ndarray) -> np.ndarray:
    """Compute the "Source (estimated)" spectrum by subtracting the response.

    This is the front-end transform: if we observe (spectrum) and measure
    (response), the estimated source is spectrum - response.

    Args:
        spectrum_db: Observed spectrum in dB.
        response_db: Measured system response in dB (same length).

    Returns:
        Estimated source spectrum (spectrum_db - response_db).
    """
    return np.asarray(spectrum_db, dtype=np.float64) - np.asarray(response_db, dtype=np.float64)


def broadband_offset_db(measured_db: np.ndarray, response_db: np.ndarray) -> float:
    """Single broadband dB offset between the estimated pre-EQ source and
    the measured post-EQ signal, for feeding a scalar level meter (the VU/
    Peak/Reference panel) rather than a per-frequency curve.

    `subtract_response()` above answers this per frequency bin, which is
    what the graph's own Input(estimated) trace needs -- a level meter needs
    one number. Rather than a flat mean of the per-bin dB difference (which
    would weight a silent bin the same as a loud one), this integrates
    actual power: convert both the measured and the estimated-source spectra
    to linear power (10**(dB/10)), sum across bins (Parseval-style total
    energy), and take the dB ratio of the two sums. That means the offset
    reflects where THIS TICK'S actual signal energy sits, not an arbitrary
    equal-weight average across frequencies the program material may not
    even be using.

    Returns 0.0 (no correction) if the measured spectrum carries no power at
    all, rather than dividing by zero.
    """
    measured_db = np.asarray(measured_db, dtype=np.float64)
    estimated_db = subtract_response(measured_db, response_db)
    measured_power = np.sum(10.0 ** (measured_db / 10.0))
    estimated_power = np.sum(10.0 ** (estimated_db / 10.0))
    if measured_power <= 0.0:
        return 0.0
    return float(10.0 * np.log10(estimated_power / measured_power))


# ── VU-bars display: fold the fine spectrum into 1/3-octave bands ───────────
#
# The waveform display draws the full-resolution spectrum as a continuous
# trace; the "Bars (VU)" display folds it into standard 1/3-octave bands (ISO
# 266 preferred centre frequencies via the base-2 relation fc = 1000 * 2**(n/3),
# the same spacing a hardware real-time analyzer uses). Thirty-one bands span
# 20 Hz-20 kHz, which is the familiar graphic-EQ / RTA look and coarse enough to
# read as discrete bars at a glance. Nothing here is a tuned constant: the band
# grid is the ISO standard, and each bar takes the MAX magnitude of the fine
# bins inside it — peak, not mean, so a narrow tone still lights its band fully,
# matching how spectrum-analyzer bars conventionally behave.
_VU_REFERENCE_HZ = 1000.0
_VU_BANDS_PER_OCTAVE = 3


def third_octave_bands(fmin: float = 20.0, fmax: float = 20000.0
                       ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ISO 1/3-octave band centres and edges covering [fmin, fmax].

    Returns ``(centres, lo_edges, hi_edges)`` in Hz. Band ``n`` is centred at
    ``1000 * 2**(n/3)`` with edges a sixth of an octave to each side, so
    adjacent bands meet exactly at their shared edge.
    """
    half = 2.0 ** (1.0 / (2.0 * _VU_BANDS_PER_OCTAVE))  # sixth-octave, as a ratio
    # Round to the NEAREST band index at each end, so the nominal endpoints round
    # in: fmin=20 -> n=-17 (19.7 Hz, the ISO "20 Hz" band) and fmax=20000 -> n=13
    # (20159 Hz, the "20 kHz" band), giving the familiar 31-band graphic-EQ
    # ladder rather than clipping the extremes off.
    n_lo = round(_VU_BANDS_PER_OCTAVE * math.log2(fmin / _VU_REFERENCE_HZ))
    n_hi = round(_VU_BANDS_PER_OCTAVE * math.log2(fmax / _VU_REFERENCE_HZ))
    n = np.arange(n_lo, n_hi + 1)
    centres = _VU_REFERENCE_HZ * 2.0 ** (n / _VU_BANDS_PER_OCTAVE)
    return centres, centres / half, centres * half


def bands_from_centres(centres_hz, fmin: float = 20.0, fmax: float = 20000.0
                       ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Band centres and edges for an ARBITRARY set of centre frequencies — the
    EQ's own filter centres, so the VU shows one bar per band the user is
    actually editing rather than a fixed ISO ladder.

    Each band owns the region out to the log-frequency midpoint between it and
    each neighbour; the outermost band mirrors its inner half-width outward, then
    the whole span is clamped to [fmin, fmax]. Centres are sorted and de-duped.
    Returns ``(centres, lo_edges, hi_edges)``; an empty input yields three empty
    arrays (caller decides the fallback).
    """
    centres = np.unique(np.asarray([c for c in centres_hz if c > 0], dtype=np.float64))
    if len(centres) == 0:
        return centres, centres.copy(), centres.copy()
    if len(centres) == 1:
        return centres, np.array([fmin]), np.array([fmax])

    mids = np.sqrt(centres[:-1] * centres[1:])          # log midpoints between neighbours
    lo = np.empty_like(centres)
    hi = np.empty_like(centres)
    lo[1:] = mids
    hi[:-1] = mids
    lo[0] = centres[0] ** 2 / mids[0]                   # mirror the inner half-width outward
    hi[-1] = centres[-1] ** 2 / mids[-1]
    return centres, np.clip(lo, fmin, fmax), np.clip(hi, fmin, fmax)


def band_levels(freqs: np.ndarray, spectrum_db: np.ndarray,
                lo_edges: np.ndarray, hi_edges: np.ndarray,
                floor_db: float = FLOOR_DBFS) -> np.ndarray:
    """Peak magnitude of `spectrum_db` within each [lo, hi) band.

    A band containing no fine-spectrum bin (possible only if the display grid
    were ever coarser than the band grid, which it is not) reads `floor_db`, so
    the returned array always has one finite level per band for the bars to draw.
    """
    freqs = np.asarray(freqs, dtype=np.float64)
    spectrum_db = np.asarray(spectrum_db, dtype=np.float64)
    out = np.full(len(lo_edges), floor_db, dtype=np.float64)
    for i, (lo, hi) in enumerate(zip(lo_edges, hi_edges)):
        mask = (freqs >= lo) & (freqs < hi)
        if mask.any():
            out[i] = float(np.max(spectrum_db[mask]))
    return out


# ── Rumble 3.7: measured-vs-calculated drift detection ──────────────────────
#
# The live spectrum analyzer's existing "measured vs. calculated" check
# (SpectrumController's clipping detector, spectrum_overlay.py) compares one
# MEASURED scalar (peak dBFS) against one fixed reference (0 dBFS) -- it
# never actually looks at whether the measured spectrum's SHAPE matches the
# calculated EQ response's shape. This extends that idea to the general case
# the guide describes: a competing driver-level DSP, a muted/bypassed EQ, or
# the wrong output device selected would all cause the measured spectrum to
# stop tracking the calculated correction, in a way clipping detection can't
# catch (none of those necessarily push the signal anywhere near 0 dBFS).
#
# Comparing raw measured level against the calculated curve directly doesn't
# work: the measured spectrum reflects UNKNOWN, constantly-changing program
# material riding on top of the EQ, not the EQ in isolation -- exactly why
# subtract_response() above exists (`spectrum = source + response`, and
# `source` is never known by itself). What IS testable without knowing the
# source is *correlation*: if Equalizer APO is genuinely applying the
# computed correction, frequencies the correction cuts should measure
# systematically lower (relative to the spectrum's own mean) than frequencies
# it boosts -- a real pattern riding on top of unrelated source variance, not
# a coincidence. If that pattern is absent or inverted, something between
# RUMBLE's calculation and the listener's ears doesn't match.
#
# "Significant" is decided by Fisher's r-to-z transformation -- a named,
# standard statistical test for whether a Pearson correlation coefficient
# could plausibly have arisen from uncorrelated (zero-mean) noise, not a
# hand-picked correlation cutoff:
#
#     z = atanh(r) * sqrt(n - 3)
#
# compared against the two-tailed 99% normal-distribution critical value
# (z = 2.576). 99%, not the more common 95%, because a false "drift
# detected" alert is a meaningfully disruptive UX event for a live
# diagnostic hint -- worth a stricter bar against false positives than a
# discarded research hypothesis test would need. A single instantaneous FFT
# frame is judged the same way clipping is: not on one frame alone (real
# program content changes as fast as the frame rate) but on a SUSTAINED
# pattern across many ticks -- see spectrum_overlay.py's drift tracking,
# which pools several ticks' (r, n) pairs before applying this test.
DRIFT_MIN_CALCULATED_RANGE_DB = 3.0
# Below this the calculated curve is nearly flat -- there is no real
# correction shape to check the measured spectrum against, so
# drift_correlation() returns None (skip judging entirely) rather than a
# meaningless near-zero correlation. Roughly the smallest broadband EQ move
# generally agreed to be a *deliberate* correction rather than incidental
# ripple -- the same "is this actually a correction, not noise" bar
# RUMBLE_ACTION_GUIDE.md #1.4's clamp notice already applies to a single
# point at a coarser 0.5 dB granularity, scaled up here to a whole curve's
# peak-to-peak span.
DRIFT_Z_CRITICAL = 2.576


def drift_correlation(measured_db: np.ndarray, calculated_db: np.ndarray) -> float | None:
    """Pearson correlation between *measured_db* and *calculated_db* across
    frequency (same length, same frequency grid).

    Returns ``None`` when there's nothing meaningful to compare: too few
    points, a *calculated_db* too close to flat (see
    :data:`DRIFT_MIN_CALCULATED_RANGE_DB`), or a degenerate (zero-variance)
    *measured_db*. Callers should treat ``None`` as "can't judge this frame"
    and skip it, not as evidence of drift either way.
    """
    m = np.asarray(measured_db, dtype=np.float64)
    c = np.asarray(calculated_db, dtype=np.float64)
    if m.shape != c.shape or m.size < 3:
        return None
    if float(np.ptp(c)) < DRIFT_MIN_CALCULATED_RANGE_DB:
        return None
    m_dev = m - m.mean()
    c_dev = c - c.mean()
    denom = float(np.sqrt(np.sum(m_dev ** 2) * np.sum(c_dev ** 2)))
    if denom <= 0:
        return None
    r = float(np.sum(m_dev * c_dev) / denom)
    return max(-1.0, min(1.0, r))


def drift_expected(r: float | None, n: int) -> bool | None:
    """Whether correlation *r* (from :func:`drift_correlation`, computed over
    *n* frequency points) is a statistically significant POSITIVE
    correlation -- i.e. the measured spectrum tracks the calculated curve as
    expected. ``False`` covers both "no relationship" (roughly zero
    correlation -- e.g. a muted/bypassed EQ or the wrong device) and "an
    inverted relationship" (a significantly negative correlation -- e.g. a
    competing DSP undoing the correction). ``None`` passes through *r*'s own
    ``None`` (nothing to judge)."""
    if r is None or n < 4:
        return None
    z = math.atanh(max(-0.999999, min(0.999999, r))) * math.sqrt(n - 3)
    return z >= DRIFT_Z_CRITICAL
