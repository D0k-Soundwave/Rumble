"""Hearing test suite -- Test B (digit-triplet-in-noise screening), Phase 7.

Qt-free. Reuses Staircase from rumble/core/hearing_test.py (Test A) with
SYMMETRIC step sizes -- see that class's docstring and the module-level note
above REFERRAL_SNR_DB below for why symmetry matters here specifically.

Orchestrates: pick a random 3-digit "triplet" (no two adjacent digits equal),
mix the concatenated spoken-digit clips (Task 4's committed WAV assets) with
calibrated pink noise at a staircase-controlled SNR, score a listener's
reported triplet against the presented one (exact match required, the
standard digit-triplet-test convention), and adapt the SNR until an SRT
(speech reception threshold -- the SNR yielding 50% correct) is reached.
"""
from __future__ import annotations

import json
import random
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from rumble.core.audio import tones as _tones
from rumble.core.hearing_test import Staircase

_DIGITS_DIR = Path(__file__).resolve().parent.parent / "assets" / "audio" / "digits"

# Digit clips (Task 4) are RMS-normalized to this level at asset-generation
# time (see tools/generate_digit_clips.py); mix_triplet_with_noise treats it
# as the fixed reference the noise level is expressed relative to -- SNR
# varies by moving the NOISE level, never the triplet's own level, matching
# how a real digit-triplet test is administered (fixed speech level, varying
# masker level).
_TRIPLET_LEVEL_DBFS = -20.0


def load_digit_clip(digit: int) -> np.ndarray:
    """Load rumble/assets/audio/digits/{digit}.wav (Task 4's committed
    48kHz/mono/16-bit-PCM asset) as float32 mono samples normalized to
    [-1, 1]. No further level adjustment is applied here -- the clip's
    already-baked-in RMS normalization IS the fixed triplet reference level
    (see _TRIPLET_LEVEL_DBFS above).
    """
    if not 0 <= digit <= 9:
        raise ValueError(f"digit must be 0-9, got {digit!r}")
    path = _DIGITS_DIR / f"{digit}.wav"
    with wave.open(str(path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return samples / 32768.0


def build_triplet(rng: random.Random) -> tuple[int, int, int]:
    """Pick 3 digits (0-9) with no two ADJACENT digits equal --
    triplet[0] != triplet[1] and triplet[1] != triplet[2]; triplet[0] ==
    triplet[2] is allowed (e.g. (3, 7, 3) is a valid triplet). This mirrors
    the standard digit-triplet-test stimulus-construction rule (adjacent
    repeats like "3-3-7" are avoidable ambiguity, not because 3 and 7 can't
    both appear).
    """
    d0 = rng.randint(0, 9)
    d1 = rng.randint(0, 9)
    while d1 == d0:
        d1 = rng.randint(0, 9)
    d2 = rng.randint(0, 9)
    while d2 == d1:
        d2 = rng.randint(0, 9)
    return (d0, d1, d2)


def mix_triplet_with_noise(
    triplet: tuple[int, int, int],
    snr_db: float,
    sample_rate: int,
    gap_s: float = 0.15,
) -> np.ndarray:
    """Concatenate the 3 digit clips (with gap_s silence between each pair)
    into one mono "triplet_audio" track, generate pink noise spanning the
    same duration at a level snr_db BELOW the fixed triplet reference level
    (_TRIPLET_LEVEL_DBFS), and sum them over the full duration (noise plays
    continuously, including through the inter-digit gaps -- that's what
    makes them meaningfully "in noise" and not just isolated quiet gaps).

    generate_pink_noise returns stereo (n, 2) with BOTH CHANNELS IDENTICAL
    (see tones.py's own docstring) -- only noise[:, 0] is used here, since
    summing both channels would double the noise amplitude relative to the
    intended calibrated level, corrupting the SNR.

    Headroom-safe scaling: if the combined signal's peak exceeds 0.98, the
    ENTIRE combined signal (triplet + noise together) is scaled down by the
    same factor. Because triplet and noise are summed BEFORE this scaling is
    computed and applied, both components are attenuated by the identical
    factor -- their relative levels, and therefore the SNR, are preserved
    exactly. This has to hold at the staircase's floor_level (very loud
    noise relative to the triplet, the highest clipping risk) just as much
    as at its ceil_level (quiet noise, low risk) -- see the module test that
    sweeps the real DigitScreeningTest staircase range (floor_level=-15.0 to
    ceil_level=10.0, plus REFERRAL_SNR_DB and interior points).

    Returns float32 (n, 2) -- the scaled mono mix duplicated to both
    channels (Test B measures overall SRT, not ear asymmetry, so both
    channels always carry the identical final mix).
    """
    clips = [load_digit_clip(d) for d in triplet]
    gap = np.zeros(int(round(gap_s * sample_rate)), dtype=np.float32)
    triplet_audio = np.concatenate(
        [clips[0], gap, clips[1], gap, clips[2]]
    ).astype(np.float64)

    n = triplet_audio.shape[0]
    duration_s = n / sample_rate
    noise_level_dbfs = _TRIPLET_LEVEL_DBFS - snr_db
    noise = _tones.generate_pink_noise(duration_s, noise_level_dbfs, sample_rate)
    noise_mono = noise[:, 0].astype(np.float64)
    # generate_pink_noise rounds duration_s*sample_rate to an int internally;
    # defensively reconcile any off-by-one rather than assume exact length.
    if noise_mono.shape[0] < n:
        noise_mono = np.pad(noise_mono, (0, n - noise_mono.shape[0]))
    else:
        noise_mono = noise_mono[:n]

    combined = triplet_audio + noise_mono

    peak = np.abs(combined).max()
    if peak > 0.98:
        combined = combined * (0.98 / peak)

    return np.column_stack([combined, combined]).astype(np.float32)


class DigitScreeningTest:
    """Test B orchestrator: presents randomized digit triplets in pink
    noise, adapts the SNR via a symmetric-step Staircase until an SRT
    (50%-correct SNR) is reached.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng if rng is not None else random.Random()
        # Symmetric step_down/step_up (required for 50%-correct convergence
        # -- see Staircase's docstring and REFERRAL_SNR_DB's below). Bounds
        # comfortably bracket the real normative SRT range (~-10.5 to -3.80
        # dB SNR, see REFERRAL_SNR_DB's derivation): start_level=0.0 dB SNR
        # is audible for essentially everyone; floor_level=-15.0 sits below
        # even excellent-performer SRTs; ceil_level=10.0 sits comfortably
        # above the "poor" boundary. reversals_needed=8 (vs. Test A's 5) for
        # a more reliable SRT estimate, matching typical published DTT
        # adaptive-procedure reversal counts.
        self.staircase = Staircase(
            start_level=0.0,
            step_down=2.0,
            step_up=2.0,
            floor_level=-15.0,
            ceil_level=10.0,
            reversals_needed=8,
        )
        self.current_triplet: tuple[int, int, int] = build_triplet(self._rng)

    @property
    def done(self) -> bool:
        return self.staircase.done

    @property
    def srt_db(self) -> float | None:
        return self.staircase.threshold

    def respond(self, reported: tuple[int, int, int]) -> None:
        """Score `reported` against `self.current_triplet`: correct only if
        ALL 3 digits match, in order (the standard digit-triplet-test
        scoring convention -- no partial credit for 2 out of 3). Advances
        the staircase, then regenerates current_triplet unless the test is
        now done.
        """
        if self.staircase.done:
            return
        correct = reported == self.current_triplet
        self.staircase.respond(correct)
        if not self.staircase.done:
            self.current_triplet = build_triplet(self._rng)


# ---------------------------------------------------------------------------
# REFERRAL_SNR_DB -- the "normal"/"insufficient" boundary for Test B's SRT
# (the SNR, in dB, yielding 50% correct triplet identification -- see
# Staircase's symmetric-step requirement above, and the class docstring in
# hearing_test.py, for why this specific definition requires symmetric
# step_down/step_up).
#
# Research trail: -5.55 dB SNR was adopted during this plan's original
# research pass (see .superpowers/sdd/task-5-brief.md) from a three-band
# classification recurring across multiple independent sources and commonly
# attributed to Smits & Houtgast (the researchers behind the Dutch national
# telephone hearing-test methodology), corroborated via the UK Biobank's
# published DTT normative-values reference (Dawes et al., Nov 2013).
#
# This implementation did ONE MORE search pass, as required, specifically to
# try to pin the exact primary citation. Findings:
#
# - The clearest primary-source statement of the numeric good/insufficient/
#   poor cutoffs found in this pass is:
#     Smits, C., Merkus, P., & Houtgast, T. (2006). "How we do it: the Dutch
#     functional hearing-screening tests by telephone and internet."
#     Clinical Otolaryngology, 31(5), 436-440.
#   reported (via secondary quotation, see LIMITATION 1 below) as: good
#   SRTn < -5.5 dB SNR; insufficient -5.5 <= SRTn <= -2.8 dB SNR; poor
#   SRTn > -2.8 dB SNR.
# - A closely related, 2-author paper -- matching how this criterion is
#   usually informally cited ("Smits & Houtgast") -- analyzed population-
#   level results (n=39,968 callers) from the same national test and is a
#   plausible antecedent/companion source for where these category
#   boundaries were derived from real caller data:
#     Smits, C., & Houtgast, T. (2005). "Results from the Dutch speech-in-
#     noise screening test by telephone." Ear and Hearing, 26(1), 89-95.
#     (PMID 15692307, DOI 10.1097/00003446-200502000-00008)
# - Independent secondary corroboration in the same neighborhood: the UK
#   Biobank's published DTT normative-values reference (Dawes et al., Nov
#   2013) and a related Dawes et al. report citing a similar-but-not-
#   identical "-5.5 to -3.5 dB SNR" insufficient band. Secondary sources
#   disagree with each other by a few tenths of a dB on the UPPER ("poor")
#   boundary (-2.8 vs. -3.5 vs. this codebase's -3.80), but cluster tightly
#   (-5.5 to -5.6 dB) on the LOWER boundary that REFERRAL_SNR_DB actually
#   uses.
#
# LIMITATION 1 (documented, not resolved): both primary-source candidates
# above are paywalled -- this pass could only confirm their existence, exact
# author list, year, journal, volume, and page numbers (via PubMed/abstract
# access), not read either paper's own printed table to verify -5.55 is
# their literal stated value rather than a slightly more precise figure
# derived (by this project's earlier planning pass, or by an intermediate
# secondary source such as the UK Biobank PDF) from the ~-5.5 dB figure that
# recurs everywhere. -5.55 is kept as-is -- it is a settled project value,
# not something to invent a replacement for -- and is corroborated in
# direction and magnitude by every source above, but not verified
# byte-for-byte against either primary paper's own printed text.
#
# LIMITATION 2 (documented, not resolved): every criterion above was
# validated using a single fixed RECORDED HUMAN VOICE speaking digits (Dutch,
# in the primary sources) over a real telephone/internet channel. Test B
# instead uses AI/SAPI-synthesized English digit words (Task 4) played back
# on the listener's own uncalibrated speakers/headphones -- a different
# voice, language, and playback chain than any of these criteria were
# validated on. This is a real, inherent source of extra uncertainty on top
# of ordinary test-retest variability (~0.7 dB, per the DIN/DTT literature),
# consistent with -- and part of why -- Test B's disclaimer frames this as
# an informal *estimate*, never a clinical-grade application of the
# published criterion. Do not remove or soften this caveat.
REFERRAL_SNR_DB: float = -5.55


def classify_srt(srt_db: float) -> str:
    """"typical" if srt_db <= REFERRAL_SNR_DB, else "consider_assessment".
    Lower SRT = better performance (less noise needed to defeat the
    listener) -- normal performers have the most negative/lowest SRTs, so
    "typical" is correctly the *lower* band. RUMBLE collapses the source
    literature's "insufficient" and "poor" bands into one
    "consider_assessment" band.
    """
    return "typical" if srt_db <= REFERRAL_SNR_DB else "consider_assessment"


def _summary_text_for_band(band: str) -> str:
    """Plain-language, non-clinical sentence for a given band
    ("typical" or "consider_assessment"). Never says "normal", "loss",
    "diagnosis", or "audiogram".

    This is the single source of truth for band -> sentence, shared by
    screening_summary (which derives band from srt_db via classify_srt) and
    export_screening_text (which takes its caller's band argument directly,
    so a caller-supplied band is always authoritative and export text can
    never silently diverge from it by re-deriving a different band from
    srt_db internally).
    """
    if band == "typical":
        return "Your result was within the typical range for this screening."
    return (
        "This result suggests it may be worth having your hearing checked "
        "by a professional."
    )


def screening_summary(srt_db: float) -> str:
    """Plain-language, non-clinical sentence describing srt_db's band.
    Never says "normal", "loss", "diagnosis", or "audiogram".
    """
    return _summary_text_for_band(classify_srt(srt_db))


def save_screening_result(srt_db: float, band: str, data_dir: Path) -> Path:
    """Persist one Test B run as timestamped JSON under
    data_dir/hearing/screening/. Same pattern as Test A's save_results
    (hearing_test.py): second-resolution UTC timestamp filenames, with a
    numeric suffix to disambiguate two saves landing in the same second
    rather than silently overwriting.
    """
    out_dir = Path(data_dir) / "hearing" / "screening"
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{base_name}.json"
    suffix = 1
    while path.exists():
        path = out_dir / f"{base_name}_{suffix}.json"
        suffix += 1

    payload = {"srt_db": srt_db, "band": band}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_all_screening_results(data_dir: Path) -> list[dict]:
    """Load every saved Test B run under data_dir/hearing/screening/,
    oldest to newest.
    """
    out_dir = Path(data_dir) / "hearing" / "screening"
    if not out_dir.exists():
        return []

    files = sorted(out_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    return [json.loads(file_path.read_text(encoding="utf-8")) for file_path in files]


# Global Constraints (progress.md), verbatim -- must appear FIRST in the
# exported file, and in two other UI locations (intro page, confirmation
# modal) not implemented in this core module.
_DISCLAIMER = (
    "This is an informal screening estimate for your own information — not "
    "a medical device, not a diagnosis, and not a substitute for a "
    "professional hearing assessment. RUMBLE and its developer are not "
    "responsible for any use made of this result. Consider a professional "
    "assessment, or disregard this entirely."
)


def export_screening_text(srt_db: float, band: str, device_label: str) -> str:
    """Plain-text export: the disclaimer (verbatim, FIRST) + date + device
    label + the SRT value + a summary sentence for `band`. No frequency
    table (Test B doesn't produce one) and no mention of EQ profiles or AI
    authorship -- unlike Test A's export, this file is for the listener's
    own information, not an AI-authoring instruction set.

    `band` (not srt_db) is authoritative for the summary sentence: this
    deliberately does NOT re-derive a band from srt_db via classify_srt and
    ignore the caller's band -- srt_db is used only for the displayed "SRT:
    ... dB SNR" line. Callers are expected to pass a `band` that already
    agrees with classify_srt(srt_db) (e.g. DigitScreeningTest.srt_db paired
    with classify_srt of that same value); this function trusts that
    pairing rather than second-guessing it.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        _DISCLAIMER,
        "",
        "RUMBLE Hearing Test -- Test B: Noise Screening",
        f"Date: {date_str}",
        f"Device: {device_label}",
        f"SRT: {srt_db:.2f} dB SNR",
        "",
        _summary_text_for_band(band),
    ]
    return "\n".join(lines)
