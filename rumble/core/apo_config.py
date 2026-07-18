"""Equalizer APO config file parsing and writing.

Structurally modelled commands: Preamp (global and per-channel — APO sums
preamps in dB per selected channel), Filter (all types, including raw IIR
coefficients), GraphicEQ (multiple lines per section stack and are combined
exactly at the union of breakpoints), Convolution, Delay (ms or samples, the
samples form preserved verbatim), Copy, Device, Channel, VSTPlugin,
LoudnessCorrection (profile-level, generic ordered token-pair list so
unknown/future keys round-trip verbatim), the *first* Stage line, all Eval
lines, and If/ElseIf/Else/EndIf conditional blocks (verbatim).

Everything else — later Stage lines, Include, unknown commands — is preserved
verbatim in extra_lines and re-emitted at the end of the body, which can
relocate order-sensitive directives; see PROFILE_FILE_CONSTRUCTION.md §3.8.
Eval lines are hoisted to just after Device (definitions precede use; a
profile reassigning a variable mid-file sees only the last value).

Per-channel support: multiple Channel: ... blocks are parsed into ordered
ChannelSection objects, each containing its own filters, optional GraphicEQ,
optional Delay and optional per-channel Preamp. Consecutive sections with the
same channel name are merged at parse time so that the common single-channel
case remains a single section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Equalizer APO itself imposes no technical limit on filter count (its own
# site: "add as many audio filters as you need without any restrictions").
# MAX_BANDS is therefore a deliberately arbitrary RUMBLE-side ceiling, not a
# derived or standard value — chosen as generous headroom above realistic use
# (most curated corrections in this app use ~10-20 bands; see
# docs/RUMBLE_APP_SPECIFIC_KB.md §3.4) while keeping the per-band editor list
# and live-graph recompute bounded for responsiveness. It lives here rather
# than in the editor because the curve->band decomposition in the core
# (autoeq_merge.build_curve_config) has to respect the same ceiling.
MAX_BANDS = 32

# ─────────────────────────────────────────────────────────────────────────
# Filter type metadata
# ─────────────────────────────────────────────────────────────────────────

FILTER_CATEGORIES: dict[str, list[str]] = {
    "Peak":        ["PK", "PEQ", "Modal"],
    "Pass":        ["LP", "LPQ", "HP", "HPQ", "BP"],
    "Shelf":       ["LS", "LSC", "LS 6dB", "LS 12dB", "HS", "HSC", "HS 6dB", "HS 12dB"],
    "Special":     ["NO", "AP"],
    "Advanced":    ["IIR"],
}

# Rumble 1.2: no widget anywhere reads/writes iir_order/iir_coefficients
# (PLANNED_WORK.md #16), so letting a user select "Custom IIR" today
# produces a filter with no usable fields. Hidden from new-authoring UI
# until that coefficient editor exists; existing IIR filters (parsed from an
# imported/round-tripped profile) are unaffected — parsing, FILTER_TYPE_LABELS
# and ALL_FILTER_TYPES all still recognise "IIR" verbatim, only the
# type-dropdown's *offered* choices for a non-IIR band are filtered by this.
IIR_EDITOR_AVAILABLE = False

FILTER_TYPE_LABELS: dict[str, str] = {
    "PK": "Peak (PK)",
    "PEQ": "Peak Bandwidth (PEQ)",
    "Modal": "Modal",
    "LP": "Low Pass (LP)",
    "LPQ": "Low Pass Q (LPQ)",
    "HP": "High Pass (HP)",
    "HPQ": "High Pass Q (HPQ)",
    "BP": "Band Pass (BP)",
    "LS": "Low Shelf (LS)",
    "LSC": "Low Shelf Centre (LSC)",
    "LS 6dB": "Low Shelf 6 dB/oct",
    "LS 12dB": "Low Shelf 12 dB/oct",
    "HS": "High Shelf (HS)",
    "HSC": "High Shelf Centre (HSC)",
    "HS 6dB": "High Shelf 6 dB/oct",
    "HS 12dB": "High Shelf 12 dB/oct",
    "NO": "Notch (NO)",
    "AP": "All Pass (AP)",
    "IIR": "Custom IIR",
}

TYPES_WITH_GAIN = {
    "PK", "PEQ", "Modal", "LS", "LSC", "LS 6dB", "LS 12dB", "HS", "HSC", "HS 6dB", "HS 12dB",
}
TYPES_WITH_Q = {
    "PK", "PEQ", "Modal", "LP", "LPQ", "HP", "HPQ", "BP", "LS", "LSC", "HS", "HSC", "NO", "AP",
}
TYPES_WITH_BW_VARIANT = {"PK", "PEQ", "Modal", "LP", "LPQ", "HP", "HPQ", "BP", "LS", "LSC", "HS", "HSC", "NO"}

ALL_FILTER_TYPES: list[str] = [t for cats in FILTER_CATEGORIES.values() for t in cats]

DEFAULT_SAMPLE_RATE = 48000


# ─────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Filter:
    """A single APO filter band."""

    enabled: bool = True
    type: str = "PK"
    fc: float = 1000.0
    gain: float = 0.0
    q: float = 1.0
    use_bw: bool = False
    bw_oct: float = 1.0
    iir_order: int = 2
    iir_coefficients: str = ""

    def supports_gain(self) -> bool:
        return self.type in TYPES_WITH_GAIN

    def supports_q(self) -> bool:
        return self.type in TYPES_WITH_Q

    def supports_bw(self) -> bool:
        return self.type in TYPES_WITH_BW_VARIANT

    def to_line(self, index: int) -> str:
        state = "ON" if self.enabled else "OFF"

        if self.type == "IIR":
            return (
                f"Filter {index}: {state} IIR Order {self.iir_order} "
                f"Coefficients {self.iir_coefficients}"
            )

        parts = [f"Filter {index}: {state} {self.type}", f"Fc {self.fc:.1f} Hz"]

        if self.supports_gain():
            parts.append(f"Gain {self.gain:.1f} dB")

        if self.supports_q():
            if self.use_bw and self.supports_bw():
                parts.append(f"BW Oct {self.bw_oct:.2f}")
            else:
                parts.append(f"Q {self.q:.2f}")

        return " ".join(parts)


@dataclass
class ChannelSection:
    """One Channel: ... block — its filters, optional GraphicEQ, optional
    Delay and optional per-channel Preamp ("trim")."""
    channel: str = "all"
    filters: list[Filter] = field(default_factory=list)
    graphic_eq: str = ""
    delay_ms: float | None = None
    # Keyword-ish trailing additions (never construct sections positionally
    # past delay_ms — use clone() when copying so new fields are never lost):
    preamp_db: float | None = None       # sum of Preamp lines inside the section
    delay_samples: float | None = None   # set when the file said "N samples"; wins at render

    def has_content(self) -> bool:
        return bool(self.filters or self.graphic_eq
                    or self.delay_ms is not None or self.preamp_db is not None)

    def clone(self) -> "ChannelSection":
        """Independent copy: filters are new instances, all fields carried."""
        from dataclasses import replace
        return ChannelSection(
            channel=self.channel,
            filters=[replace(f) for f in self.filters],
            graphic_eq=self.graphic_eq,
            delay_ms=self.delay_ms,
            preamp_db=self.preamp_db,
            delay_samples=self.delay_samples,
        )


def conditional_convolution_paths(profile: "ProfileConfig") -> list[str]:
    """Every IR path convolved from inside *profile*'s conditional blocks,
    in order.

    A profile that selects its IR per sample rate keeps the `Convolution:`
    lines INSIDE an `If:/ElseIf:/Else:` block (see
    ProfileManager.create_profile_with_convolution), where the parser stores
    them as opaque block text rather than in the `convolution` field -- that
    field only ever holds a BARE, unconditional `Convolution:` line. So the
    bookkeeping question "which IR belongs to this profile?" has to look in
    both places, which is what this exists for.
    """
    paths: list[str] = []
    for block in profile.conditional_blocks:
        for line in block.splitlines():
            m = _CONVOLUTION_RE.match(line.strip())
            if m:
                paths.append(m.group(1).strip())
    return paths


def fallback_convolution_path(profile: "ProfileConfig") -> str:
    """The IR a sample-rate conditional falls back to — the one its `Else:`
    branch convolves — or "" when there is no such branch.

    This is the profile's *declared default* IR (the `default_rate` file), so
    it is the honest single answer to "which IR is this profile" for anything
    rate-agnostic: IR analysis, headroom, the export bundle's bare line.
    Taking the first branch instead would answer with whichever rate happened
    to sort first (44.1 kHz), which is not the default."""
    for block in profile.conditional_blocks:
        in_else = False
        for line in block.splitlines():
            stripped = line.strip()
            if _CONDITIONAL_ELSE_RE.match(stripped):
                in_else = True
                continue
            if _CONDITIONAL_BRANCH_RE.match(stripped):
                in_else = False
                continue
            if in_else:
                m = _CONVOLUTION_RE.match(stripped)
                if m:
                    return m.group(1).strip()
    return ""


#: The canonical per-ear channel identifiers, in canonical order.
STEREO_CHANNELS: tuple[str, ...] = ("L", "R")


def stereo_section_ears(channel: str) -> list[str] | None:
    """Which ear channels a `Channel:` string addresses, or None when the
    section isn't a plain-stereo one.

    ``"L"`` -> ``["L"]``, ``"R"`` -> ``["R"]``, a COMBINED stereo section
    ``"L R"`` -> ``["L", "R"]``, and the linked ``"all"`` -> ``["L", "R"]``
    (the whole stream, which on a stereo layout is exactly both ears).
    Anything naming a non-stereo channel (``"C"``, ``"LFE"``, ``"L R C"``,
    ...) or naming nothing at all -> None. Case-insensitive; the returned
    tokens are always canonical upper-case, de-duplicated, in L-then-R
    order.

    THE one definition of "is this section plain stereo" for the whole
    codebase (crossfeed.is_stereo_only, listener_correction.
    compose_channel_sections). It lives here, beside ChannelSection itself,
    because both of those modules previously carried their OWN copy of the
    rule as a literal ``("L", "R", "all")`` tuple -- which silently excluded
    the combined ``"L R"`` section that builtin_profiles_data._body() (and
    RUMBLE's own AI prompts) actually emit, i.e. the single most common
    stereo layout there is. That made headphone crossfeed permanently
    unavailable and per-ear listener correction a silent no-op on every
    built-in playback profile. Fixed 2026-07-13; keep this the only copy.
    """
    tokens = channel.split()
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0].lower() == "all":
        return list(STEREO_CHANNELS)
    ears: list[str] = []
    for token in tokens:
        upper = token.upper()
        if upper not in STEREO_CHANNELS:
            return None
        if upper not in ears:
            ears.append(upper)
    return sorted(ears, key=STEREO_CHANNELS.index)


def is_stereo_section(channel: str) -> bool:
    """True when *channel* is a plain-stereo `Channel:` string -- see
    stereo_section_ears() for exactly what qualifies."""
    return stereo_section_ears(channel) is not None


@dataclass
class VSTPlugin:
    """A VSTPlugin: ... entry, with optional VSTPlugin Chunks: data."""
    path: str = ""
    chunks: str = ""


@dataclass
class LoudnessCorrection:
    """APO LoudnessCorrection command as an ordered token-pair list, so unknown
    parameters round-trip verbatim. Known keys get typed accessors."""
    tokens: list[tuple[str, str]] = field(default_factory=list)

    def get(self, key: str, default: str | None = None) -> str | None:
        for k, v in self.tokens:
            if k.lower() == key.lower():
                return v
        return default

    def set(self, key: str, value: str) -> None:
        for i, (k, _) in enumerate(self.tokens):
            if k.lower() == key.lower():
                self.tokens[i] = (k, value)
                return
        self.tokens.append((key, value))

    @property
    def enabled(self) -> bool:
        return (self.get("State") or "0").lower() in ("1", "on", "true")

    @enabled.setter
    def enabled(self, on: bool) -> None:
        self.set("State", "1" if on else "0")

    def to_line(self) -> str:
        return "LoudnessCorrection: " + " ".join(f"{k} {v}" for k, v in self.tokens)


@dataclass
class ProfileConfig:
    """Parsed/structured representation of an APO profile config file."""

    device: str = "all"
    preamp: float | None = None

    # First Stage: line (lower-cased, e.g. "pre-mix" or "post-mix capture");
    # None means APO's default (post-mix + capture). Rendered as body line 1.
    stage: str | None = None

    # All Eval: expressions, in file order, rendered together after Device.
    eval_lines: list[str] = field(default_factory=list)

    # Ordered channel sections — most profiles have exactly one ("all").
    channel_sections: list[ChannelSection] = field(default_factory=list)

    # VST plugins (path-order preserved from file).
    vst_plugins: list[VSTPlugin] = field(default_factory=list)

    # Global processing (applied before channel sections in rendered output).
    convolution: str = ""
    copy_lines: list[str] = field(default_factory=list)

    # All If/ElseIf/Else/EndIf blocks found, preserved in order.
    conditional_blocks: list[str] = field(default_factory=list)

    # The profile-level LoudnessCorrection command, if present (None means
    # the profile has no loudness correction block at all).
    loudness_correction: "LoudnessCorrection | None" = None

    # Unrecognised lines, preserved verbatim for round-trip safety.
    extra_lines: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────

_FILTER_RE = re.compile(r"^Filter\s*(?:\d+)?\s*:\s*(ON|OFF)\s+(.+)$", re.IGNORECASE)
_IIR_RE = re.compile(r"^IIR\s+Order\s+(\d+)\s+Coefficients\s+(.+)$", re.IGNORECASE)
_FC_RE = re.compile(r"Fc\s+([\-+]?[\d.]+)\s*Hz", re.IGNORECASE)
_GAIN_RE = re.compile(r"Gain\s+([\-+]?[\d.]+)\s*(?:dB)?", re.IGNORECASE)
_Q_RE = re.compile(r"\bQ\s+([\-+]?[\d.]+)", re.IGNORECASE)
_BW_RE = re.compile(r"BW\s+Oct\s+([\-+]?[\d.]+)", re.IGNORECASE)
_PREAMP_RE = re.compile(r"^Preamp\s*:\s*([\-+]?[\d.]+)\s*dB", re.IGNORECASE)
_DEVICE_RE = re.compile(r"^Device\s*:\s*(.+)$", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"^Channel\s*:\s*(.+)$", re.IGNORECASE)
_GRAPHICEQ_RE = re.compile(r"^GraphicEQ\s*:\s*(.+)$", re.IGNORECASE)
_CONVOLUTION_RE = re.compile(r"^Convolution\s*:\s*(.+)$", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^Include\s*:\s*(.+)$", re.IGNORECASE)
_DELAY_RE = re.compile(r"^Delay\s*:\s*([\-+]?[\d.]+)\s*(ms|samples)", re.IGNORECASE)
_COPY_RE = re.compile(r"^Copy\s*:\s*(.+)$", re.IGNORECASE)
_VST_RE = re.compile(r"^VSTPlugin\s*:\s*(.+)$", re.IGNORECASE)
_VST_CHUNKS_RE = re.compile(r"^VSTPlugin\s+Chunks\s*:\s*(.+)$", re.IGNORECASE)
_CONDITIONAL_START_RE = re.compile(r"^If\s*:", re.IGNORECASE)
_CONDITIONAL_END_RE = re.compile(r"^EndIf\s*:?", re.IGNORECASE)
_CONDITIONAL_ELSE_RE = re.compile(r"^Else\s*:", re.IGNORECASE)
_CONDITIONAL_BRANCH_RE = re.compile(r"^(?:If|ElseIf|EndIf)\s*:?", re.IGNORECASE)
_STAGE_RE = re.compile(r"^Stage\s*:\s*(.+)$", re.IGNORECASE)
_EVAL_RE = re.compile(r"^Eval\s*:\s*(.+)$", re.IGNORECASE)
_LOUDNESS_RE = re.compile(r"^LoudnessCorrection\s*:\s*(.+)$", re.IGNORECASE)


def _stack_graphic_eq(existing: str, new: str) -> str:
    """Combine two stacked GraphicEQ value strings into one equivalent string.

    APO instantiates one filter per GraphicEQ line, so the responses add in
    dB. Summing at the union of breakpoints reproduces that exactly (the
    curves are piecewise linear in dB over log-frequency).
    """
    from rumble.core import autoeq_merge
    combined = autoeq_merge.sum_curves([
        autoeq_merge.parse_graphiceq(existing),
        autoeq_merge.parse_graphiceq(new),
    ])
    return autoeq_merge.format_graphiceq(combined)


def _parse_filter_body(state: str, body: str) -> Filter:
    iir_match = _IIR_RE.match(body.strip())
    if iir_match:
        return Filter(
            enabled=(state.upper() == "ON"),
            type="IIR",
            iir_order=int(iir_match.group(1)),
            iir_coefficients=iir_match.group(2).strip(),
        )

    type_match = re.match(r"^(.*?)\s+Fc\s", body, re.IGNORECASE)
    ftype = type_match.group(1).strip() if type_match else body.split()[0]
    ftype_upper = ftype.upper()
    normalised = next((t for t in ALL_FILTER_TYPES if t.upper() == ftype_upper), ftype)

    fc_match = _FC_RE.search(body)
    gain_match = _GAIN_RE.search(body)
    bw_match = _BW_RE.search(body)
    q_match = None if bw_match else _Q_RE.search(body)

    return Filter(
        enabled=(state.upper() == "ON"),
        type=normalised,
        fc=float(fc_match.group(1)) if fc_match else 1000.0,
        gain=float(gain_match.group(1)) if gain_match else 0.0,
        q=float(q_match.group(1)) if q_match else 1.0,
        use_bw=bool(bw_match),
        bw_oct=float(bw_match.group(1)) if bw_match else 1.0,
    )


def _parse_loudness(params: str) -> LoudnessCorrection | None:
    parts = params.split()
    if len(parts) < 2 or len(parts) % 2 != 0:
        return None                      # malformed → caller keeps it in extra_lines
    pairs = [(parts[i], parts[i + 1]) for i in range(0, len(parts), 2)]
    return LoudnessCorrection(tokens=pairs)


def _merge_same_channel_sections(sections: list[ChannelSection]) -> list[ChannelSection]:
    """Merge consecutive sections with the same channel name.

    Filters concatenate, per-channel preamps sum (APO semantics) and stacked
    GraphicEQ lines are combined exactly.
    """
    if not sections:
        return sections
    merged: list[ChannelSection] = [sections[0]]
    for sec in sections[1:]:
        prev = merged[-1]
        if sec.channel.lower() == prev.channel.lower():
            prev.filters.extend(sec.filters)
            if sec.graphic_eq:
                prev.graphic_eq = (_stack_graphic_eq(prev.graphic_eq, sec.graphic_eq)
                                   if prev.graphic_eq else sec.graphic_eq)
            if sec.preamp_db is not None:
                prev.preamp_db = (prev.preamp_db or 0.0) + sec.preamp_db
        else:
            merged.append(sec)
    return merged


def parse_config(text: str, sample_rate: int = DEFAULT_SAMPLE_RATE) -> ProfileConfig:
    """Parse APO config text into a structured ProfileConfig.

    *sample_rate* is used only to convert ``Delay: <n> samples`` into the ms
    value the UI displays; the samples figure itself is kept verbatim.
    Unknown/unsupported lines are preserved verbatim in extra_lines so that
    round-tripping a profile never silently drops content.
    """
    profile = ProfileConfig(device="", preamp=None)
    lines = text.splitlines()

    # Current channel section accumulator — None means no Channel: seen yet.
    current_section: ChannelSection | None = None
    completed_sections: list[ChannelSection] = []

    # For conditional block accumulation
    in_conditional = False
    conditional_lines: list[str] = []

    # Track last VST so we can attach Chunks to it
    last_vst: VSTPlugin | None = None

    def _flush_section() -> None:
        nonlocal current_section
        if current_section is not None and current_section.has_content():
            completed_sections.append(current_section)
        current_section = None

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.strip()
        i += 1

        if not line or line.startswith("#"):
            continue

        # ── conditional blocks ───────────────────────────────────────
        if in_conditional:
            conditional_lines.append(raw_line)
            if _CONDITIONAL_END_RE.match(line):
                in_conditional = False
                profile.conditional_blocks.append("\n".join(conditional_lines))
                conditional_lines = []
            continue

        if _CONDITIONAL_START_RE.match(line):
            in_conditional = True
            conditional_lines = [raw_line]
            continue

        # ── channel directive — starts a new section ──────────────────
        m = _CHANNEL_RE.match(line)
        if m:
            _flush_section()
            current_section = ChannelSection(channel=m.group(1).strip())
            continue

        # ── filter ───────────────────────────────────────────────────
        m = _FILTER_RE.match(line)
        if m:
            if current_section is None:
                current_section = ChannelSection("all")
            current_section.filters.append(_parse_filter_body(m.group(1), m.group(2)))
            continue

        # ── graphic EQ (stacked lines combine exactly) ───────────────
        m = _GRAPHICEQ_RE.match(line)
        if m:
            if current_section is None:
                current_section = ChannelSection("all")
            value = m.group(1).strip()
            current_section.graphic_eq = (
                _stack_graphic_eq(current_section.graphic_eq, value)
                if current_section.graphic_eq else value)
            continue

        # ── preamp (per selected channel; multiple lines sum in dB) ──
        m = _PREAMP_RE.match(line)
        if m:
            value = float(m.group(1))
            if current_section is None:
                profile.preamp = (profile.preamp or 0.0) + value
            else:
                current_section.preamp_db = (current_section.preamp_db or 0.0) + value
            continue

        # ── device ───────────────────────────────────────────────────
        m = _DEVICE_RE.match(line)
        if m:
            profile.device = m.group(1).strip()
            continue

        # ── convolution ──────────────────────────────────────────────
        m = _CONVOLUTION_RE.match(line)
        if m:
            profile.convolution = m.group(1).strip()
            continue

        # ── delay (per-channel — assigned to the active section) ─────
        m = _DELAY_RE.match(line)
        if m:
            if current_section is None:
                current_section = ChannelSection("all")
            value, unit = float(m.group(1)), m.group(2).lower()
            if unit == "ms":
                current_section.delay_ms = value
                current_section.delay_samples = None
            else:
                # Keep the samples figure verbatim; the ms value is only the
                # UI's display conversion at the configured sample rate.
                current_section.delay_samples = value
                current_section.delay_ms = (value / sample_rate) * 1000.0
            continue

        # ── stage (first line structured; later ones stay verbatim) ──
        m = _STAGE_RE.match(line)
        if m:
            if profile.stage is None:
                profile.stage = " ".join(m.group(1).lower().split())
            else:
                profile.extra_lines.append(raw_line)
            continue

        # ── eval expressions (collected wherever they appear) ────────
        m = _EVAL_RE.match(line)
        if m:
            profile.eval_lines.append(m.group(1).strip())
            continue

        # ── copy ─────────────────────────────────────────────────────
        m = _COPY_RE.match(line)
        if m:
            profile.copy_lines.append(m.group(1).strip())
            continue

        # ── VST chunks (must be checked before VSTPlugin: to avoid partial match) ─
        m = _VST_CHUNKS_RE.match(line)
        if m:
            if last_vst is not None:
                last_vst.chunks = m.group(1).strip()
            else:
                profile.extra_lines.append(raw_line)
            continue

        # ── VST plugin ───────────────────────────────────────────────
        m = _VST_RE.match(line)
        if m:
            last_vst = VSTPlugin(path=m.group(1).strip())
            profile.vst_plugins.append(last_vst)
            continue

        # ── loudness correction ─────────────────────────────────────
        m = _LOUDNESS_RE.match(line)
        if m:
            lc = _parse_loudness(m.group(1).strip())
            if lc is not None and profile.loudness_correction is None:
                profile.loudness_correction = lc
                continue
            # malformed token stream, or a second LoudnessCorrection: line
            # after one has already been parsed → fall through to verbatim
            # preservation (never silently overwrite/drop a prior block).

        # ── preserve everything else ─────────────────────────────────
        profile.extra_lines.append(raw_line)

    # Flush any unterminated conditional block
    if conditional_lines:
        profile.conditional_blocks.append("\n".join(conditional_lines))

    # Flush the last section
    _flush_section()

    profile.channel_sections = _merge_same_channel_sections(completed_sections)
    return profile


# RUMBLE's own pointer files / managed subtree — never inlined when flattening an
# existing APO config (we want the *user's* content, not our own indirection).
_RUMBLE_OWNED_INCLUDE = ("rumble_active.txt", "rumble_preview.txt")


def include_target(line: str) -> str | None:
    """Return the (slash-normalised) target of an ``Include:`` line, else None."""
    m = _INCLUDE_RE.match(line.strip())
    return m.group(1).strip().replace("\\", "/") if m else None


def resolve_includes(text: str, base_dir, skip_stems=frozenset(),
                     _seen: set[str] | None = None) -> str:
    """Flatten an APO config by inlining every ``Include:`` target.

    APO resolves an include against the directory of the file containing the
    command, so each target is read relative to *base_dir* and spliced in place,
    recursively. RUMBLE's own pointer files and the whole ``rumble/`` managed
    subtree are dropped rather than inlined; targets whose filename stem is in
    *skip_stems* (e.g. APO's bundled sample configs) are likewise dropped, so the
    result is the *user's* effective config. Missing files and include cycles are
    skipped safely.
    """
    from pathlib import Path

    base_dir = Path(base_dir)
    if _seen is None:
        _seen = set()

    out: list[str] = []
    for raw in text.splitlines():
        m = _INCLUDE_RE.match(raw.strip())
        if not m:
            out.append(raw)
            continue
        target = m.group(1).strip().replace("\\", "/")
        low = target.lower()
        if low in _RUMBLE_OWNED_INCLUDE or low.startswith("rumble/"):
            continue  # drop RUMBLE's own indirection / managed tree
        try:
            inc_path = (base_dir / target).resolve()
            key = str(inc_path).lower()
        except OSError:
            continue
        if inc_path.stem in skip_stems:
            continue  # drop APO's bundled sample includes
        if key in _seen:
            continue  # cycle guard
        _seen.add(key)
        try:
            inc_text = inc_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # missing / unreadable include → skip
        out.append(resolve_includes(inc_text, inc_path.parent, skip_stems, _seen))
    return "\n".join(out)


@dataclass
class ResolvedLine:
    """One line of a fully-flattened APO config, tagged with the file it came
    from — for display/traceability (the config-health viewer), not for
    import filtering (see ``resolve_includes()`` above, which deliberately
    drops RUMBLE's own indirection for that different purpose)."""
    text: str
    source: str


def resolve_includes_traced(text: str, base_dir, source_name: str = "config.txt",
                            _seen: set[str] | None = None) -> list[ResolvedLine]:
    """Flatten every ``Include:`` target exactly like ``resolve_includes()``,
    but keep *every* line — including RUMBLE's own managed pointer files —
    tagged with the (base_dir-relative) file it came from, and note missing
    includes/cycles inline instead of silently skipping them. Used by the
    config-health viewer to show exactly what's applying and where each part
    comes from.
    """
    from pathlib import Path

    base_dir = Path(base_dir)
    if _seen is None:
        _seen = set()

    out: list[ResolvedLine] = []
    for raw in text.splitlines():
        m = _INCLUDE_RE.match(raw.strip())
        if not m:
            out.append(ResolvedLine(raw, source_name))
            continue
        target = m.group(1).strip().replace("\\", "/")
        try:
            inc_path = (base_dir / target).resolve()
            key = str(inc_path).lower()
        except OSError:
            out.append(ResolvedLine(f"# (unreadable include: {target})", source_name))
            continue
        if key in _seen:
            out.append(ResolvedLine(f"# (include cycle skipped: {target})", source_name))
            continue
        _seen.add(key)
        try:
            inc_text = inc_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out.append(ResolvedLine(f"# (missing include: {target})", source_name))
            continue
        out.extend(resolve_includes_traced(inc_text, inc_path.parent, target, _seen))
    return out


# ─────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────

def render_config_body(profile: ProfileConfig) -> str:
    """Render the APO command body of a ProfileConfig (no comment header).

    Canonical order (PROFILE_FILE_CONSTRUCTION.md §3): Stage, Device, Eval
    lines, global Preamp, Convolution/Copy, VST, LoudnessCorrection, channel
    sections (Channel → per-channel Preamp → Delay → filters → GraphicEQ),
    conditional blocks, then verbatim extra lines.
    """
    lines: list[str] = []

    # Stage first: it scopes every command after it.
    if profile.stage:
        lines.append(f"Stage: {profile.stage}")
        lines.append("")

    if profile.device and profile.device.lower() != "all":
        lines.append(f"Device: {profile.device}")
        lines.append("")
    elif profile.device.lower() == "all":
        lines.append("Device: all")
        lines.append("")

    # Eval lines directly after Device so definitions precede use.
    if profile.eval_lines:
        for expr in profile.eval_lines:
            lines.append(f"Eval: {expr}")
        lines.append("")

    if profile.preamp is not None:
        lines.append(f"Preamp: {profile.preamp:.1f} dB")
        lines.append("")

    # Global processing
    if profile.convolution:
        lines.append(f"Convolution: {profile.convolution}")

    if profile.copy_lines:
        for copy_expr in profile.copy_lines:
            lines.append(f"Copy: {copy_expr}")

    if profile.convolution or profile.copy_lines:
        lines.append("")

    # VST plugins
    for vst in profile.vst_plugins:
        if vst.path:
            lines.append(f"VSTPlugin: {vst.path}")
            if vst.chunks:
                lines.append(f"VSTPlugin Chunks: {vst.chunks}")
    if profile.vst_plugins:
        lines.append("")

    if profile.loudness_correction is not None:
        lines.append(profile.loudness_correction.to_line())
        lines.append("")

    # Channel sections (delay + filters + GraphicEQ per section)
    for section in profile.channel_sections:
        if not section.has_content():
            continue
        lines.append(f"Channel: {section.channel or 'all'}")
        if section.preamp_db is not None:
            lines.append(f"Preamp: {section.preamp_db:.1f} dB")
        if section.delay_samples is not None:
            lines.append(f"Delay: {section.delay_samples:g} samples")
        elif section.delay_ms is not None:
            lines.append(f"Delay: {section.delay_ms:.1f} ms")
        for idx, filt in enumerate(section.filters, start=1):
            lines.append(filt.to_line(idx))
        if section.graphic_eq:
            lines.append(f"GraphicEQ: {section.graphic_eq}")
        lines.append("")

    # Conditional blocks
    for block in profile.conditional_blocks:
        if block:
            lines.append(block)
            lines.append("")

    # Verbatim preserved lines
    for extra in profile.extra_lines:
        lines.append(extra)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"
    return text


def render_include(target_relative_path: str) -> str:
    return f"# RUMBLE active profile pointer — do not edit by hand\nInclude: {target_relative_path}\n"


def render_bypass() -> str:
    return (
        "# RUMBLE bypass — EQ temporarily disabled\n"
        "# This file intentionally contains no active commands.\n"
    )
