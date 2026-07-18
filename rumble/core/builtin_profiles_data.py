"""Built-in profile definitions. Generated at runtime to rumble/defaults/.

These are seven **hardware-agnostic** playback preference profiles plus three
**hardware-agnostic** microphone profiles (D2): none carry device correction
(no transducer is known), only a clamped taste/tone layer that works on any
headphones/speakers (playback) or any microphone (capture). Each is authored
in the exact shape the RUMBLE AI prompts emit (§8 of
RUMBLE_Universal_AI_Prompt.md) — canonical header keys (name/description/tags)
plus an Equalizer APO body of parametric filters, a flat GraphicEQ, and an
anti-clipping Preamp computed from the combined response (§4.4). They
round-trip through the same parse/render pipeline as imported user profiles,
so a built-in is indistinguishable from a user-created one apart from its
system-protected flag.

Preamp values are computed with core.preamp_calc.calculate_auto_preamp, which
matches the prompt's §4.4 algorithm (same RBJ biquad math, same combined
filters+GEQ peak, same 0.5/1.0 dB margin rule) to within APO's 0.1 dB step.

ProfileManager.ensure_builtin_profiles() writes/refreshes one .txt per entry in
rumble/defaults/ and removes any stale .txt no longer listed here.
"""

_FLAT_GEQ = "20 0; 20000 0"


def _body(preamp: float, filters: list[str]) -> str:
    """Assemble a canonical APO body: Preamp, a stereo Channel section with the
    given Filter lines, and a flat GraphicEQ."""
    lines = [f"Preamp: {preamp:.1f} dB", "", "Channel: L R"]
    lines += filters
    lines.append(f"GraphicEQ: {_FLAT_GEQ}")
    return "\n".join(lines) + "\n"


def _mic_body(preamp: float, filters: list[str]) -> str:
    """Same shape as _body(), but scoped to capture and `Channel: all`
    instead of `Channel: L R` -- a microphone capture endpoint is commonly
    mono (unlike the stereo playback devices the other built-ins target),
    and `all` is the universal, channel-count-agnostic scope apo_config.py
    already treats as the default (ChannelSection.channel's own default
    value).

    The leading `Stage: capture` is what MAKES these mic profiles: it is
    the sole thing ProfileMeta.capture_only reads, so without it the
    library's "Microphone profiles" toggle cannot tell them apart from
    playback profiles and they are neither hideable nor disableable. Stage
    goes first, per render_config_body()'s canonical order -- it scopes
    every command after it.
    """
    lines = ["Stage: capture", "", f"Preamp: {preamp:.1f} dB", "", "Channel: all"]
    lines += filters
    lines.append(f"GraphicEQ: {_FLAT_GEQ}")
    return "\n".join(lines) + "\n"


BUILTIN_PROFILES: dict[str, dict] = {
    "rumble_warm_smooth": {
        "name": "Warm & Smooth",
        "description": "Adds gentle low-end warmth and softens the highs for a relaxed, easy-going sound on any gear.",
        "tags": ["Universal", "Character / Warm", "Situation / Casual"],
        "body": _body(-4.5, [
            "Filter 1: ON LSC Fc 110.0 Hz Gain 4.0 dB Q 0.70",
            "Filter 2: ON PK Fc 3500.0 Hz Gain -1.5 dB Q 1.40",
            "Filter 3: ON HSC Fc 9000.0 Hz Gain -2.5 dB Q 0.70",
        ]),
    },
    "rumble_bright_clear": {
        "name": "Bright & Clear",
        "description": "Lifts detail and treble air for a crisper, more open sound on any headphones or speakers.",
        "tags": ["Universal", "Character / Bright", "Situation / Detail"],
        "body": _body(-3.6, [
            "Filter 1: ON LSC Fc 100.0 Hz Gain -1.5 dB Q 0.70",
            "Filter 2: ON PK Fc 3000.0 Hz Gain 2.0 dB Q 1.20",
            "Filter 3: ON HSC Fc 8000.0 Hz Gain 3.0 dB Q 0.70",
        ]),
    },
    "rumble_bass_boost": {
        "name": "Bass Boost",
        "description": "A solid low-end lift for extra weight and punch, useful on bass-light gear.",
        "tags": ["Universal", "Character / Bass", "Music / Electronic"],
        "body": _body(-8.7, [
            "Filter 1: ON PK Fc 45.0 Hz Gain 2.0 dB Q 0.80",
            "Filter 2: ON LSC Fc 90.0 Hz Gain 6.0 dB Q 0.70",
        ]),
    },
    "rumble_v_shaped": {
        "name": "V-Shaped",
        "description": "Boosted bass and treble with a scooped midrange — a fun, energetic sound for casual listening.",
        "tags": ["Universal", "Character / V-Shaped", "Music / Pop"],
        "body": _body(-5.5, [
            "Filter 1: ON LSC Fc 100.0 Hz Gain 5.0 dB Q 0.70",
            "Filter 2: ON PK Fc 600.0 Hz Gain -2.5 dB Q 0.80",
            "Filter 3: ON HSC Fc 9000.0 Hz Gain 4.0 dB Q 0.70",
        ]),
    },
    "rumble_vocal_clarity": {
        "name": "Vocal Clarity",
        "description": "Brings voices and dialogue forward and trims rumble and hiss — great for podcasts, audiobooks and films.",
        "tags": ["Universal", "Situation / Speech", "Music / Vocal"],
        "body": _body(-4.1, [
            "Filter 1: ON LSC Fc 120.0 Hz Gain -2.0 dB Q 0.70",
            "Filter 2: ON PK Fc 2500.0 Hz Gain 3.0 dB Q 1.00",
            "Filter 3: ON PK Fc 4200.0 Hz Gain 1.5 dB Q 1.30",
            "Filter 4: ON HSC Fc 10000.0 Hz Gain -1.5 dB Q 0.70",
        ]),
    },
    "rumble_smooth_treble": {
        "name": "Smooth Treble",
        "description": "Eases sharp, sibilant highs to reduce harshness and listening fatigue.",
        "tags": ["Universal", "Character / Smooth", "Situation / Fatigue"],
        "body": _body(0.0, [
            "Filter 1: ON PK Fc 3500.0 Hz Gain -2.0 dB Q 1.40",
            "Filter 2: ON PK Fc 6500.0 Hz Gain -3.0 dB Q 2.50",
        ]),
    },
    "rumble_loudness": {
        "name": "Loudness",
        "description": "Compensates for quiet listening by lifting bass and treble, so music stays full at low volume.",
        "tags": ["Universal", "Situation / Low Volume", "Character / Loudness"],
        "body": _body(-6.5, [
            "Filter 1: ON LSC Fc 80.0 Hz Gain 6.0 dB Q 0.70",
            "Filter 2: ON PK Fc 2800.0 Hz Gain 1.5 dB Q 1.00",
            "Filter 3: ON HSC Fc 10000.0 Hz Gain 4.0 dB Q 0.70",
        ]),
    },
    # D2 (docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md):
    # microphone built-ins -- hardware-agnostic like the seven above, but for
    # a capture device (Devices page's Capture tab) rather than playback.
    # `Channel: all` via _mic_body(), not `Channel: L R` (mono capture).
    "rumble_voice_clean_hpf": {
        "name": "Voice - Clean HPF",
        "description": "A subsonic high-pass to strip rumble, handling noise and plosives from a microphone signal, leaving voice untouched.",
        "tags": ["Microphone", "Situation / Voice", "Situation / Podcast"],
        # 80-100 Hz is the standard broadcast/podcast mic HPF corner (removes
        # room rumble, mic-stand handling noise and breath plosives without
        # touching the voice fundamental, which sits well above it for
        # nearly all speakers); Q 0.70 (Butterworth, maximally flat) matches
        # this file's own shelf-filter convention above.
        "body": _mic_body(0.0, [
            "Filter 1: ON HP Fc 90.0 Hz Q 0.70",
        ]),
    },
    "rumble_voice_presence": {
        "name": "Voice - Presence",
        "description": "A gentle presence lift so spoken voice sits forward and stays intelligible, without the harsh edge of a sharper boost.",
        "tags": ["Microphone", "Situation / Voice", "Situation / Podcast"],
        "body": _mic_body(-3.0, [
            "Filter 1: ON HSC Fc 3500.0 Hz Gain 2.5 dB Q 0.70",
        ]),
    },
    "rumble_voice_broadcast": {
        "name": "Voice - Broadcast Mix",
        "description": "Combines the subsonic high-pass with a presence lift and a light treble tame, a typical podcast/streaming voice chain.",
        "tags": ["Microphone", "Situation / Voice", "Situation / Podcast"],
        "body": _mic_body(-2.5, [
            "Filter 1: ON HP Fc 90.0 Hz Q 0.70",
            "Filter 2: ON PK Fc 3000.0 Hz Gain 2.0 dB Q 1.00",
            "Filter 3: ON HSC Fc 8000.0 Hz Gain -2.0 dB Q 0.70",
        ]),
    },
}
