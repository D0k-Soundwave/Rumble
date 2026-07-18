"""Blind A/B/X listening test support — Qt-free core.

Level matching: before a blind comparison, the candidate configs must be
gain-matched so a loudness difference does not masquerade as a quality
difference (a louder signal is reliably judged "better" by naive listeners
regardless of its actual EQ shape).

`level_match()` estimates each config's broadband gain from its combined
frequency response (reusing `rumble.core.eq_math.combined_response` — the same
function the live response graph renders with, never reimplemented here) plus
its `Preamp` value, then returns clones with every louder config's preamp
reduced so all candidates land at the quietest one's estimated level (only
ever reducing, never boosting, so matching can't introduce clipping).

The broadband-gain estimate averages the response in the power (linear)
domain — converting each dB sample to a linear power ratio, averaging those,
then converting back to dB — across `eq_math.frequency_array()`'s log-spaced
frequency points. Because those points are log-spaced, an equal-weight
average across them is equivalent to an equal-weight-per-octave average,
i.e. the same RMS level change pink noise (equal energy per octave, the
standard proxy for real music's long-term spectral density) would show
passing through the response. This is the physically correct quantity for
level-matching real program material: a narrow EQ peak genuinely does not
raise broadband loudness much, and a power-domain average reflects that
where a plain dB average would not.
"""
from __future__ import annotations

import copy
import math
import secrets

import numpy as np

from rumble.core import eq_math
from rumble.core.apo_config import ProfileConfig

# Audible range for the broadband-gain estimate (narrower than eq_math's own
# graph-rendering default of 10 Hz-24 kHz, per the level-matching spec).
_FMIN = 20.0
_FMAX = 20000.0

# Below this, a config is considered already level-matched (returned
# offset is exactly 0.0 rather than a near-zero float). 0.1 dB matches APO's
# own output precision elsewhere in this project (preamp_calc.py quantises
# to APO's 0.1 dB grid; LoudnessCorrection/Preamp lines render with 1 decimal
# place) -- a "match" finer than that isn't representable in the rendered
# config anyway, so there's nothing to gain from a tighter tolerance.
_MATCH_TOLERANCE_DB = 0.1


def estimated_gain_db(config: ProfileConfig, sample_rate: int) -> float:
    """Estimate a config's overall broadband gain in dB.

    Averages the response in the power (linear) domain across the log-spaced
    frequency array — equal weight per octave, i.e. the same RMS level change
    pink noise (the standard proxy for real music's long-term spectral
    density) would show passing through this response. This is the
    physically correct quantity for level-matching real program material: a
    narrow EQ peak genuinely does not raise broadband loudness much, and this
    estimate reflects that. It also stays consistent with the optional
    real-loopback-RMS refinement (both are power-domain RMS quantities, so
    they should agree rather than diverge by construction).

    Public (promoted from the formerly-private ``_estimated_gain_db``, A2 in
    the adaptive front-end roadmap) so callers outside this module — the
    profile-switch loudness-matching render layer in profile_manager.py —
    can reuse the exact same estimate ``level_match`` uses, rather than
    reimplementing it.
    """
    freqs = eq_math.frequency_array(fmin=_FMIN, fmax=_FMAX)
    power_samples = []
    for section in config.channel_sections:
        response = eq_math.combined_response(section.filters, freqs, sample_rate)
        if section.graphic_eq:
            response = response + eq_math.graphic_eq_response(section.graphic_eq, freqs)
        if section.preamp_db:
            response = response + section.preamp_db
        power_samples.append(10 ** (response / 10.0))

    if not power_samples:
        return config.preamp or 0.0

    mean_power = float(np.mean(np.concatenate(power_samples)))
    power_gain_db = 10 * np.log10(max(mean_power, 1e-12))
    return power_gain_db + (config.preamp or 0.0)


def level_match(configs: list[ProfileConfig],
                 sample_rate: int) -> tuple[list[ProfileConfig], list[float]]:
    """Gain-match any number of configs ahead of a blind A/B/X comparison.

    Returns ``(clones, offsets_db)``: independent clones of every input
    (originals are never mutated) with each louder clone's ``preamp`` reduced
    so all candidates have the same estimated broadband gain as the quietest
    one (reduction only — matching can never introduce clipping).
    ``offsets_db[i]`` is the (non-negative) reduction applied to clone ``i`` —
    ``0.0`` when that config already matched the quietest within tolerance.
    """
    gains = [estimated_gain_db(config, sample_rate) for config in configs]
    clones = [copy.deepcopy(config) for config in configs]
    if not clones:
        return [], []

    floor = min(gains)
    offsets = []
    for clone, gain in zip(clones, gains):
        diff = gain - floor
        if diff < _MATCH_TOLERANCE_DB:
            offsets.append(0.0)
        else:
            clone.preamp = (clone.preamp or 0.0) - diff
            offsets.append(diff)
    return clones, offsets


def roll_index(rng, n: int) -> int:
    """Uniform draw over ``0..n-1`` from an injectable rng exposing
    ``.random()``. ``int(random()*n)`` is uniform over the range; the ``% n``
    guards the ``random()==1.0`` edge some injected test doubles can produce.
    Shared by ABXSession's per-trial X roll and the dialog's candidate draw.
    """
    return int(rng.random() * n) % n


class ABXSession:
    """Runs an open-ended sequence of blind A/B/X trials over ``n_candidates``
    candidate configs.

    Each trial secretly (crypto-randomly, unless a deterministic ``rng`` is
    injected for testing) decides which candidate the hidden reference "X"
    actually is — exposed only via ``x_index``, which the UI layer must never
    reveal to the listener before they answer. The listener guesses which
    candidate X is; ``answer()`` records whether that guess matched the
    truth (returning it, so the UI can give immediate per-trial feedback),
    advances to the next trial, and re-randomizes ``x_index`` for it.

    There is no fixed trial count: every answer is one trial, and the
    listener stops whenever they choose — ``trial`` and ``correct`` reflect
    however many answers were actually recorded, so
    ``verdict(session.correct, session.trial, session.n_candidates)`` is
    meaningful at any point.
    """

    def __init__(self, n_candidates: int = 2, rng: object | None = None) -> None:
        if n_candidates < 2:
            raise ValueError(f"ABXSession needs at least 2 candidates, got {n_candidates}")
        self.n_candidates = n_candidates
        self.rng = rng if rng is not None else secrets.SystemRandom()
        self.trial = 0
        self.correct = 0
        self.x_index = self._roll()

    def _roll(self) -> int:
        return roll_index(self.rng, self.n_candidates)

    def answer(self, guess_index: int) -> bool:
        """Record a listener's guess for the current trial and advance.

        Returns whether the guess was correct, so the UI can tell the
        listener immediately.
        """
        was_correct = guess_index == self.x_index
        if was_correct:
            self.correct += 1
        self.trial += 1
        self.x_index = self._roll()
        return was_correct


def binomial_p(correct: int, total: int, chance: float = 0.5) -> float:
    """One-sided P(X >= correct | p=chance) for a binomial(total, chance).

    This is the null-hypothesis probability of scoring at least this many
    correct guesses by pure chance (blind guessing at a per-trial success
    rate of ``chance`` — 1/2 for a classic two-candidate test, 1/N when X is
    drawn from N candidates). Each term is computed in log space (lgamma)
    rather than via ``math.comb``: sessions are open-ended, so ``total`` is
    unbounded, and a big-int ``math.comb`` value beyond ~1e308 would raise
    OverflowError the moment it met a float. Log space never overflows and
    is accurate to double precision — far finer than the p<0.01/0.05
    verdict thresholds can distinguish.
    """
    if total <= 0 or correct <= 0:
        return 1.0
    log_chance = math.log(chance)
    log_miss = math.log1p(-chance)  # log(1 - chance), exact for small chance
    prob = 0.0
    for k in range(correct, total + 1):
        log_comb = (math.lgamma(total + 1) - math.lgamma(k + 1)
                    - math.lgamma(total - k + 1))
        prob += math.exp(log_comb + k * log_chance + (total - k) * log_miss)
    # Float rounding can nudge a full-tail sum a hair past 1.0; clamp it.
    return min(prob, 1.0)


def verdict(correct: int, total: int, n_candidates: int = 2) -> str:
    """Plain-language judgment of an ABX result, with the exact p-value.

    ``n_candidates`` sets the per-trial chance rate (1/N). Thresholds follow
    the conventional significance levels: p < 0.01 is considered strong
    evidence the listener reliably hears a difference; p < 0.05 is weaker
    but still suggestive; anything looser is statistically indistinguishable
    from guessing.
    """
    p = binomial_p(correct, total, chance=1.0 / n_candidates)
    score = f"{correct}/{total}"
    if p < 0.01:
        verdict_text = "you reliably hear a difference"
    elif p < 0.05:
        verdict_text = "you probably hear a difference"
    else:
        verdict_text = "consistent with guessing"
    return f"{score} correct (p={p:.4g}): {verdict_text}"


class ABXRunner:
    """Switches Equalizer APO's live config between the candidate configs
    during a blind A/B/X trial, guaranteeing the listener's true active
    profile is restored on every exit path — including an exception raised
    mid-trial.

    Each `.present(index)` / `.present_x(session)` call previews the requested
    candidate via `manager.preview_config()` (the same scratch-file +
    active-include-pointer mechanism the live editor preview uses — nothing
    canonical is touched). "X" is resolved via `session.x_index`, never
    revealed beyond that selection.

    `.finish()` always calls `manager.end_preview()`, which points Equalizer
    APO's active include back at whatever profile is *actually* the saved
    active one — the correct "put things back" call, deliberately not
    `activate_profile()` (which would re-save settings, persist
    `last_active_profile`, and fire `on_profile_activated` callbacks, all
    inappropriate side effects for merely cancelling a preview).

    Use as a context manager so the restore happens even if trial code raises:

        with ABXRunner(manager, configs, sample_rate) as runner:
            runner.present(0)
            ...

    `__exit__` calls `.finish()` unconditionally and returns False, so any
    exception from inside the `with` block still propagates after the
    restore runs.
    """

    def __init__(self, manager, configs: list[ProfileConfig],
                 sample_rate: int) -> None:
        self.manager = manager
        self.configs = list(configs)
        self.sample_rate = sample_rate

    def present(self, index: int) -> bool:
        """Preview candidate ``index``. Returns whatever
        `manager.preview_config()` returns (False if the manager is currently
        bypassed and declined to preview)."""
        return self.manager.preview_config(self.configs[index])

    def present_x(self, session: "ABXSession") -> bool:
        """Preview the hidden reference X (resolved via ``session.x_index``)."""
        return self.present(session.x_index)

    def finish(self) -> None:
        """Always restore the true active profile. Safe to call more than
        once (`end_preview()` is a no-op once already restored)."""
        self.manager.end_preview()

    def __enter__(self) -> "ABXRunner":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        # Unconditional restore — runs even when an exception is propagating
        # out of the `with` block. Returning False (never swallow) lets that
        # exception continue on its way after the restore has happened.
        self.finish()
        return False
