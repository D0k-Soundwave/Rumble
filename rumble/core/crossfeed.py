"""Headphone crossfeed as APO-native config text (no DSP of our own) --
Copy:/Channel:/Filter:/Delay:/Copy: structure validated live on real
hardware in Session S6 (docs/RUMBLE_APP_SPECIFIC_KB.md §17). Roadmap B2.

The generated block is appended as raw text after the rest of a composed
render (ProfileManager._render_crossfeed_block()), never round-tripped
through ProfileConfig.copy_lines/channel_sections -- KB §17.4 established
that apo_config.render_config_body() always renders every copy_lines entry
before every channel_sections entry, which cannot express this block's
second Copy: line, which must render AFTER the Channel: section it reads
XL/XR from.

Every mode here is a gain/filter/delay recipe expressed as APO config text --
there is no HRTF/impulse-response-based "true binaural" crossfeed (that would
mean sourcing/licensing a bundleable HRIR dataset and new per-channel
Convolution: render plumbing, a separate initiative, not a mode of this
module). If that ever gets built it belongs alongside render_block() as
another dispatched mode, not a replacement for the ones here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from rumble.core.apo_config import (
    STEREO_CHANNELS, ChannelSection, Filter, stereo_section_ears,
)
from rumble.core.crossover import BUTTERWORTH_Q


@dataclass(frozen=True)
class CrossfeedPreset:
    label: str
    gain_db: float     # cross-path attenuation at strength=100 (negative dB)
    freq_hz: float | None  # cross-path low-pass corner, None = no filter (full-band)
    delay_us: float      # cross-path delay (interaural time difference), 0 = none


# Chu Moy's classic passive crossfeed circuit ("An Acoustic Simulator for
# Headphone Amplifiers", HeadWize --
# https://headwizememorial.wordpress.com/2018/03/09/an-acoustic-simulator-for-headphone-amplifiers/,
# mirrored at
# https://astersart.net/amp/headphones/acoussim/acoustic_simulator_for_headphone.htm):
# a single-pole RC low-pass on the cross path at ~700 Hz, cross-channel level
# roughly -6 dB relative to the direct path. No explicit delay stage -- the
# RC filter's own phase response is the only timing cue.
CHU_MOY = CrossfeedPreset(label="Chu Moy", gain_db=-6.0, freq_hz=700.0, delay_us=0.0)

# Jan Meier's "crossfeed" circuit, shipped in Meier Audio headphone amplifiers
# (published circuit description, same passive RC topology as Chu Moy's but a
# stronger, slightly narrower cross path): ~-9.5 dB cross-channel attenuation,
# ~650 Hz low-pass corner.
MEIER = CrossfeedPreset(label="Jan Meier", gain_db=-9.5, freq_hz=650.0, delay_us=0.0)

# Siegfried Linkwitz's crossfeed design ("Improved Headphone Listening",
# Audio magazine, December 1971; widely reproduced as the "Modified Linkwitz
# Crossfeed", https://tangentsoft.com/audio/mlxfeed.html, and documented
# alongside Chu Moy's circuit at
# https://headwizememorial.wordpress.com/2018/03/09/an-acoustic-simulator-for-headphone-amplifiers/):
# the same ~700 Hz low-pass corner as Chu Moy's circuit, but with an explicit
# ~250-300 microsecond delay on the cross path modelling interaural time
# difference directly -- the delay stage is Linkwitz's distinguishing
# contribution over the plain RC designs, and is what Session S6's spike
# validated live (KB §17.1 used exactly 300 us). Linkwitz's passive network
# was never published as a single headline attenuation figure the way the
# other two were, so the -4.5 dB feed level is taken from bs2b (Boris
# Mikhaylov's "Bauer stereophonic-to-binaural" DSP, bs2b.sourceforge.net) --
# the same catalogue the Chu Moy (700 Hz/6.0 dB) and Meier (650 Hz/9.5 dB)
# figures above appear in as its "cmoy"/"jmeier" presets. bs2b's default
# 700 Hz/4.5 dB preset models Benjamin Bauer's 30-degree/3-metre virtual
# speaker placement (Bauer, JAES 1961) -- the very design Linkwitz's 1971
# circuit was derived from -- making it the natural feed level for the
# delay-based Bauer/Linkwitz lineage preset, where the explicit ITD stage
# carries more of the spatial cue than the level difference does.
LINKWITZ = CrossfeedPreset(label="Linkwitz-style", gain_db=-4.5, freq_hz=700.0, delay_us=300.0)

# bs2b's own default preset (bs2b.sourceforge.net) -- 700 Hz/-4.5 dB, modelling
# Benjamin Bauer's 30-degree/3-metre virtual speaker placement (JAES 1961).
# This is the same figure LINKWITZ above borrows for its gain (see its own
# comment), but exposed here undelayed, as bs2b actually ships it -- the
# level-only "Bauer" preset, next to LINKWITZ's delay-augmented take on the
# same lineage.
BAUER = CrossfeedPreset(label="Bauer (bs2b Default)", gain_db=-4.5, freq_hz=700.0, delay_us=0.0)

# A plain full-band gain blend with no low-pass corner and no delay -- not a
# published circuit, just the simple "mix some of the opposite channel in,
# unfiltered" fallback some players ship as a crude alternative to a true
# crossfeed circuit. Reuses Chu Moy's already-cited -6 dB figure rather than
# inventing a new attenuation number.
DIRECT = CrossfeedPreset(label="Direct Blend", gain_db=-6.0, freq_hz=None, delay_us=0.0)

PRESETS: dict[str, CrossfeedPreset] = {
    "chumoy": CHU_MOY,
    "meier": MEIER,
    "linkwitz": LINKWITZ,
    "bauer": BAUER,
    "direct": DIRECT,
}

DEFAULT_PRESET = "meier"

def can_crossfeed(channel_sections: list[ChannelSection]) -> bool:
    """True when crossfeed can apply to a profile with this channel layout.

    The ONLY real requirement is that the stream carries an L and an R: the
    block this module emits mixes L into R and back and touches NOTHING
    else, and it is appended as raw text AFTER the profile body, so how that
    body groups its own `Channel:` sections is irrelevant to it. Therefore:

      - NO `Channel:` sections at all -> True. A profile with no `Channel:`
        line applies to every channel (APO's own default), which includes
        L and R. This is the shape of nearly every real headphone profile:
        AutoEQ/convolution exports are Preamp + Filters/Convolution with no
        `Channel:` line whatsoever.
      - An `all` section -> True (every channel, so L and R).
      - Any layout naming BOTH L and R -> True. That includes the combined
        `"L R"`, split `L` + `R`, and multichannel layouts like
        `L R C LFE RL RR` -- crossfeed leaves C/LFE/surrounds untouched, so
        there is no reason to withhold it there.
      - Only a layout with no L/R at all (e.g. a lone `C`, or `LFE`) -> False,
        because there is genuinely nothing to crossfeed.

    History (2026-07-13): this was `is_stereo_only()`, which demanded that
    EVERY section be exactly "L"/"R"/"all" and returned False for an empty
    list. That rejected the combined `"L R"` section every built-in playback
    profile uses AND every `Channel:`-less headphone profile -- i.e. it was
    False for essentially every profile a user would actually want crossfeed
    on, both greying out the UI and silently rendering nothing. Widened to
    the true precondition above.
    """
    if not channel_sections:
        return True
    ears: set[str] = set()
    for section in channel_sections:
        section_ears = stereo_section_ears(section.channel)
        if section_ears:
            ears.update(section_ears)
        else:
            # A non-stereo section (e.g. "C", "LFE", or a multichannel
            # "L R C") still tells us which ears the layout exposes.
            ears.update(token.upper() for token in section.channel.split()
                        if token.upper() in STEREO_CHANNELS)
    return set(STEREO_CHANNELS) <= ears


def _scaled_gain_db(preset_gain_db: float, strength_pct: float) -> float:
    """Strength (0-100) scales the cross-path gain in the LINEAR (voltage)
    domain, not the dB value directly -- scaling a dB number linearly isn't
    physically meaningful (it doesn't converge to "no attenuation" at 0).
    Converting to linear, interpolating toward unity (1.0 = 0 dB = no
    crossfeed) by strength/100, then converting back to dB gives a mapping
    where strength=100 reproduces the preset's own published figure exactly
    and strength=0 is unity gain (the "no effect" endpoint that render_
    crossfeed_block() short-circuits to an empty block, so this branch is
    never actually asked to render 0 dB in practice)."""
    linear_full = 10.0 ** (preset_gain_db / 20.0)
    linear_at_strength = 1.0 + (linear_full - 1.0) * (strength_pct / 100.0)
    return 20.0 * math.log10(linear_at_strength)


def _render_gain_filter_delay_block(spec: CrossfeedPreset, strength: float) -> str:
    """The S6-validated Copy:/Channel:/[Delay:]/[Filter:]/Copy: structure for
    an arbitrary *spec* (a named PRESETS entry or a user-built Custom one) at
    *strength* percent (0-100, clamped). Returns "" (no block at all) at
    strength <= 0 -- the "flat" case, so a caller can unconditionally append
    the result without a separate enabled-check for the zero-strength edge.
    Shared by render_crossfeed_block() and render_block()'s "custom" path."""
    strength = max(0.0, min(100.0, strength))
    if strength <= 0.0:
        return ""
    gain_db = _scaled_gain_db(spec.gain_db, strength)
    lines = [
        "Copy: XL=R",
        "Copy: XR=L",
        "",
        "Channel: XL XR",
        f"Preamp: {gain_db:.1f} dB",
    ]
    if spec.delay_us > 0.0:
        lines.append(f"Delay: {spec.delay_us / 1000.0:.1f} ms")
    if spec.freq_hz is not None:
        lp_filter = Filter(type="LP", fc=spec.freq_hz, q=BUTTERWORTH_Q)
        lines.append(lp_filter.to_line(1))
    lines.append("")
    lines.append("Copy: L=L+XL R=R+XR")
    lines.append("")
    return "\n".join(lines)


def render_crossfeed_block(preset: str, strength: float) -> str:
    """render_gain_filter_delay_block() for a named PRESETS entry -- see that
    function's docstring for the block shape and the zero-strength case."""
    return _render_gain_filter_delay_block(PRESETS[preset], strength)


def render_block(mode: str, strength: float, settings: Mapping[str, object]) -> str:
    """Single dispatch entry point for both of ProfileManager's render call
    sites. "custom" is a settings-driven mode with no PRESETS entry of its
    own; any other *mode* is looked up in PRESETS and rendered exactly as
    render_crossfeed_block() always has (settings is ignored on that path,
    same behaviour as before this function existed)."""
    if mode == "custom":
        spec = CrossfeedPreset(
            label="Custom",
            gain_db=float(settings.get("crossfeed_custom_gain_db", -6.0)),
            freq_hz=float(settings.get("crossfeed_custom_freq_hz", 700.0)),
            delay_us=float(settings.get("crossfeed_custom_delay_us", 300.0)))
        # Custom is the user's own exact figures -- there is no separate
        # published-circuit "full strength" to scale toward, so it always
        # renders at its own values (strength=100) rather than exposing a
        # second scalar on top that would just be confusing.
        return _render_gain_filter_delay_block(spec, 100.0)
    return render_crossfeed_block(mode, strength)
