"""Bass-management profile-content generator (Rumble Campaign D, D3a).

Builds canonical ProfileConfig-ready content -- a list[ChannelSection] plus
Copy: lines plus a computed safety preamp -- for the 2.1/4.1/5.1-style
layouts named in CHANNEL_PRESETS (rumble/ui/editor_panel.py): a high-pass
crossover on the full-range ("main") channels, a low-pass crossover plus a
summed feed on the sub channel, per-channel level trims, and distance-
derived per-channel delay. This is profile CONTENT, not a render layer --
the caller (the S24 wizard) merges the returned fields into a ProfileConfig
through the normal editor/dirty flow, exactly like autoeq_merge.py's or
listener_correction.py's own composed output is merged elsewhere.

Deviation (protocol 1.2, reconcile-first): the roadmap/S23 spec prose uses
"SUB" as its example channel name (``Copy: SUB=SUB+0.5*L+0.5*R``). Reality
disagrees -- CHANNEL_PRESETS (rumble/ui/editor_panel.py), STANDARD_CHANNEL_
ORDER (time_alignment.py) and the LFE-only-bass-filter warning
(main_window.py) all use the literal APO channel identifier "LFE" for this
channel, never "SUB". SUB_CHANNEL below is "LFE", not "SUB" -- read as
"SUB" being the spec's generic English word for the channel, not a literal
APO identifier to emit.

Linkwitz-Riley crossover alignments
------------------------------------
A Linkwitz-Riley crossover of order 2n is defined (Linkwitz, S. H., "Active
Crossover Networks for Noncoincident Drivers," J. Audio Eng. Soc., 1976) as
the SQUARE of an n-th-order Butterworth filter's transfer function -- both
the low-pass and high-pass sides use the same alignment, so their magnitude
responses sum to exactly 0 dB (unity) at every frequency, with -6 dB (not
Butterworth's own -3 dB) at the crossover frequency itself:

  LR2 (12 dB/octave): squares a 1st-order Butterworth response (a single
  real pole -- 1st order has no Q term at all). Two identical 1st-order
  real poles at the same cutoff form a single critically-damped 2nd-order
  section, which the Audio EQ Cookbook / RBJ biquad model this project's
  eq_math.py already implements expresses as ONE 2nd-order HP/LP filter at
  Q = 0.5 -- the "cascaded 1st-order == Q 0.5 equivalent" the roadmap's D3
  text names. Rendered as a single Filter line per section.

  LR4 (24 dB/octave): squares a 2nd-order Butterworth filter, whose own
  defining Q is 1/sqrt(2) ~= 0.70711 (the standard "maximally flat
  magnitude" Butterworth alignment). LR4 is therefore two IDENTICAL
  Q=0.70711 2nd-order sections cascaded at the same cutoff. Rendered as TWO
  Filter lines (same Fc, same Q, same pass type) in the same Channel: block
  -- APO applies multiple Filter lines within one section in series, which
  is exactly the cascade the definition requires.

Verified numerically against this project's own eq_math.py biquad model
(complex frequency response, not magnitudes -- an earlier version of this
docstring claimed voltage-domain flatness from a magnitude-only sum, which
is blind to phase and misses the LR2 polarity requirement below): at Q=0.5
(one section) and at Q=0.70711 (two cascaded sections), both HP and LP sit
at exactly -6.021 dB at the crossover frequency, and the MAGNITUDES are
exactly power/voltage complementary (|HP|+|LP| = 1 to floating-point
precision across 20-2000 Hz at an 80 Hz crossover).

LR2 polarity inversion (fixed 2026-07-13)
------------------------------------------
The COMPLEX (acoustic) sum -- what actually happens in the room when the
sub's and mains' outputs combine -- behaves differently per alignment, a
classical Linkwitz-Riley result (Linkwitz, JAES 1976):

  LR4: HP + LP is allpass (flat magnitude) with both sides IN phase -- no
  polarity handling needed. Verified: |HP+LP| deviates 0.00 dB across
  20-2000 Hz at an 80 Hz crossover.

  LR2: HP and LP are 180 degrees out of phase at every frequency, so the
  in-phase sum has a deep null at the crossover (measured -65 dB at 80 Hz
  against eq_math.py's biquads) -- every real LR2 implementation inverts
  one driver's polarity, after which |HP-LP| is exactly allpass-flat.

This module therefore inverts the SUB side for LR2 -- the whole summed
feed, native LFE content included, matching how an active LR2 crossover
inverts the entire driver connection (and an AVR's "phase 180" switch
flips the whole sub output): keeping native LFE and redirected bass
mutually coherent inside the one physical sub. Expressed as negative
`Copy:` factors (``LFE=-1*LFE+-0.5*L+-0.5*R``), Equalizer APO's documented
syntax (Configuration reference example ``Copy: L=R+-6dB*C``); negative
factors have not yet had a project hardware spike -- worth a one-minute
listen at the crossover frequency the first time LR2 is used live. LR4's
feed keeps plain positive weights.

Sub/LFE summed feed
--------------------
The roadmap's own D3 example, ``Copy: SUB=SUB+0.5*L+0.5*R``, weights each
of the two main-channel sources at 0.5 -- not a strict equal-POWER (RMS,
incoherent-source) combination, which for two sources would weight each at
1/sqrt(2) (~0.707). 0.5+0.5 is instead the standard bass-redirection
convention consumer AVR/THX-style bass management uses: it bounds the WORST
CASE (every redirected source simultaneously at full scale and in phase --
the common case for correlated program bass) to exactly unity gain
(0.5*1.0 + 0.5*1.0 = 1.0), so the summed feed never itself clips no matter
how many main channels are redirected into it. This module generalises the
weighting directly: for N main (non-sub) channels in a layout, each is
weighted 1/N, which preserves the same never-exceeds-unity property for N
simultaneous full-scale correlated sources summing into one mono feed.

Design trade-off, noted so it isn't mistaken for a bug: this 1/N weighting
means the crossover's own HP+LP=unity flatness (proven above) does not by
itself guarantee a flat *per-original-channel* reconstruction once the sub
is shared across multiple mains -- e.g. content present only in L reaches
the listener as HP(L) [from the L speaker] plus (1/N)*LP(L) [its share of
the shared mono sub], not the full LP(L) a single-source 2-way crossover
would deliver. This is not specific to the 0.5 weighting choice -- ANY
bass-management design that redirects multiple channels' bass into one
shared mono subwoofer has this same inherent limitation (a mono sub cannot
reconstruct per-channel stereo bass separately); it is the accepted,
universal trade-off bass management makes in exchange for using one
physical subwoofer, not an error introduced here. The crossover tests in
tests/selftest_bass_management.py verify the HP+LP filter-pair flatness
(the well-defined, textbook LR property) rather than a full weighted
multi-channel acoustic reconstruction, for exactly this reason.

Distance -> Delay
------------------
rumble/core/time_alignment.py (read per this session's ADAPT FIRST
instruction) has no distance-to-delay conversion at all -- its
measure_channel_delays() derives delay purely from a microphone
measurement's cross-correlation offset; there is no physical-distance input
anywhere in that module. This module's ``distances`` parameter is direct
manual entry in centimetres, so the "existing math" available to reuse is
the physical conversion itself, using a standard reference speed of sound
(SPEED_OF_SOUND_M_PER_S = 343.0 m/s, the ISO 9613-1:1993 reference
atmospheric condition, the same figure room-correction tools such as REW
use for their own distance<->delay calculators).

Rebasing follows time_alignment.py's own documented invariant -- APO's
Delay: line can only ADD delay, never subtract it, so exactly one channel
must be the zero-delay reference and every other channel gets a positive
top-up -- and the reference channel here is chosen by the physics of
distance, the same convention time_alignment.py's microphone-measured
delays now use: the FARTHEST channel (longest acoustic path, so it already
arrives latest and cannot be sped up) gets 0 ms; every closer channel is
delayed by the difference in transit time, so all channels arrive at the
listening position together.

(The S23 [REVIEW] flag that used to live here -- measure_channel_delays()
rebasing to the EARLIEST arrival, so applying its output as Delay: lines
widened the channel-arrival gap instead of closing it -- was confirmed as
a sign bug and fixed 2026-07-13: it now rebases to the LATEST arrival,
matching this module's farthest-gets-0 convention, so both delay sources
are directly comparable if "use measured distances" is ever wired up.)

Clipping safety
----------------
Summing N main channels into the sub feed raises that channel's peak level
above what its own crossover filter alone would produce. build_bass_
management() therefore evaluates preamp_calc.calculate_auto_preamp() across
EVERY composed section (each main's HP response + its own trim, and the sub
section's LP response + its own trim + the summed-feed's own headroom cost)
and takes the MINIMUM (most negative, i.e. most conservative) result as the
single profile-level preamp -- the same "recompute the global preamp as the
min across composed sections" convention tone_trim.py/loudness_layer.py
already establish for this codebase's other composed-content generators.

The summed-feed headroom cost is modelled as a flat +20*log10(2) ~= +6.02 dB
section_offset_db added to the sub section's peak-response calculation: two
independently full-scale, in-phase time-domain sources summing (the sub
channel's own native content, worst case, plus the redirected main-channel
sum -- which the 1/N weighting above already bounds to no more than one
further full-scale equivalent, regardless of channel count N) doubles peak
amplitude, i.e. +20*log10(2) dB, independent of N.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from rumble.core import preamp_calc
from rumble.core.apo_config import DEFAULT_SAMPLE_RATE, ChannelSection, Filter

# Named alignment -> (# cascaded biquad sections per side, each section's Q).
# See module docstring "Linkwitz-Riley crossover alignments" for the full
# derivation and the numeric verification against eq_math.py.
_ALIGNMENTS: dict[str, tuple[int, float]] = {
    "LR2": (1, 0.5),                   # single critically-damped 2nd-order section
    "LR4": (2, 1.0 / math.sqrt(2.0)),  # two cascaded Butterworth (Q=0.70711) sections
}

#: Standard reference speed of sound in air at 20 degrees C (ISO 9613-1:1993
#: reference atmospheric condition) -- see module docstring "Distance ->
#: Delay".
SPEED_OF_SOUND_M_PER_S = 343.0
_CM_PER_MS = SPEED_OF_SOUND_M_PER_S * 100.0 / 1000.0  # = 34.3 cm/ms

#: Literal APO channel identifier this project uses for the subwoofer/LFE
#: channel everywhere else in the codebase -- see the module docstring's
#: Deviation note for why this is "LFE", not the roadmap prose's "SUB".
SUB_CHANNEL = "LFE"

#: See module docstring "Clipping safety".
_SUMMED_FEED_HEADROOM_DB = 20.0 * math.log10(2.0)


@dataclass
class BassManagementResult:
    """Profile CONTENT only -- not a render layer. The caller merges these
    fields into a ProfileConfig (channel_sections, copy_lines, preamp)
    through the normal editor/dirty flow."""
    channel_sections: list[ChannelSection] = field(default_factory=list)
    copy_lines: list[str] = field(default_factory=list)
    preamp_db: float = 0.0


def _crossover_filters(alignment: str, xover_hz: float, pass_type: str) -> list[Filter]:
    """*pass_type* is "HP" (mains) or "LP" (sub). Returns the Filter cascade
    implementing the named Linkwitz-Riley *alignment* at *xover_hz*."""
    if alignment not in _ALIGNMENTS:
        raise ValueError(f"Unknown alignment {alignment!r}; expected one of {sorted(_ALIGNMENTS)}")
    n_sections, q = _ALIGNMENTS[alignment]
    return [Filter(type=pass_type, fc=xover_hz, q=q, enabled=True) for _ in range(n_sections)]


def _distance_delays_ms(distances: dict[str, float]) -> dict[str, float]:
    """Physical distance (centimetres) -> per-channel Delay: value (ms),
    rebased so the FARTHEST channel gets 0 ms and every closer channel gets
    a positive top-up. See module docstring "Distance -> Delay"."""
    if not distances:
        return {}
    transit_ms = {ch: cm / _CM_PER_MS for ch, cm in distances.items()}
    farthest_ms = max(transit_ms.values())
    return {ch: farthest_ms - ms for ch, ms in transit_ms.items()}


def build_bass_management(
    layout: list[str],
    xover_hz: float,
    alignment: str,
    sub_trim_db: float,
    distances: dict[str, float] | None = None,
    main_trims_db: dict[str, float] | None = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> BassManagementResult:
    """Build canonical bass-management profile content for a 2.1/4.1/5.1-
    style *layout* (a CHANNEL_PRESETS-shaped list of channel names, e.g.
    ``["L", "R", "LFE"]``) -- see the module docstring for the crossover,
    summed-feed, distance and safety-preamp derivations.

    *xover_hz*: shared crossover frequency for both the mains' high-pass and
    the sub's low-pass (Linkwitz-Riley alignments are only flat when both
    sides share one crossover point).
    *alignment*: "LR2" or "LR4".
    *sub_trim_db*: the sub channel's own level trim (per-channel Preamp:).
    *distances*: optional {channel: distance_cm} for delay-based alignment;
    channels omitted from this dict get no Delay: line. May include
    SUB_CHANNEL itself.
    *main_trims_db*: optional {channel: trim_db} per main channel (mains
    omitted from this dict default to 0.0, i.e. no Preamp: line).

    Raises ValueError if *layout* has no sub channel or no main channels.
    """
    if SUB_CHANNEL not in layout:
        raise ValueError(f"layout {layout!r} has no {SUB_CHANNEL!r} channel -- "
                          "bass management requires a sub/LFE channel")
    mains = [ch for ch in layout if ch != SUB_CHANNEL]
    if not mains:
        raise ValueError(f"layout {layout!r} has no main (non-{SUB_CHANNEL}) channels")

    main_trims_db = main_trims_db or {}
    distances = distances or {}
    delays_ms = _distance_delays_ms(distances)

    # Standard N-source bass-redirection weighting -- see module docstring
    # "Sub/LFE summed feed". LR2's whole sub feed is polarity-inverted
    # (negative factors) or its acoustic sum with the mains' HP output nulls
    # at the crossover -- see "LR2 polarity inversion"; LR4 sums in phase.
    weight = 1.0 / len(mains)
    if alignment == "LR2":
        copy_expr = f"{SUB_CHANNEL}=-1*{SUB_CHANNEL}" + "".join(
            f"+-{weight:.6g}*{ch}" for ch in mains
        )
    else:
        copy_expr = SUB_CHANNEL + "=" + SUB_CHANNEL + "".join(
            f"+{weight:.6g}*{ch}" for ch in mains
        )
    copy_lines = [copy_expr]

    sections: list[ChannelSection] = []
    for ch in mains:
        trim = main_trims_db.get(ch, 0.0)
        sections.append(ChannelSection(
            channel=ch,
            filters=_crossover_filters(alignment, xover_hz, "HP"),
            preamp_db=trim if trim else None,
            delay_ms=delays_ms.get(ch),
        ))

    sections.append(ChannelSection(
        channel=SUB_CHANNEL,
        filters=_crossover_filters(alignment, xover_hz, "LP"),
        preamp_db=sub_trim_db if sub_trim_db else None,
        delay_ms=delays_ms.get(SUB_CHANNEL),
    ))

    required_preamps = []
    for section in sections:
        offset = section.preamp_db or 0.0
        if section.channel == SUB_CHANNEL:
            offset += _SUMMED_FEED_HEADROOM_DB
        required_preamps.append(preamp_calc.calculate_auto_preamp(
            section.filters, sample_rate=sample_rate, section_offset_db=offset))
    preamp_db = min(required_preamps) if required_preamps else 0.0

    return BassManagementResult(channel_sections=sections, copy_lines=copy_lines, preamp_db=preamp_db)
