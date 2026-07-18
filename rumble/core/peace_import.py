"""Import Peace ("Peace, interface Equalizer APO") `.peace` save files as
RUMBLE profiles.

Peace does NOT save raw Equalizer APO `Filter:`/`Copy:` text — it has its own
INI-style format: per-speaker `[Frequencies]`/`[Gains]`/`[Qualities]`/
`[Filters]` sections (unsuffixed for speaker index 0 = "all", `N` suffix for
indices 1-8), a `[Speakers]` section mapping each index to an APO channel
token (`SpeakerTargets{n}`, e.g. "L"/"SUB"), and a `[General]` section holding
`Description=`, an optional `Routing=` (an Equalizer APO `Copy:` line's
parameter string verbatim) and optional `PreAmp{n}=` per-channel trims.

Verified 2026-07-09 against two real presets downloaded from Peace's own
SourceForge "Configurations (presets)" library:
- `Bass Boost.peace` — speaker 0 ("all") only, no `[Filters]` section at all
  (every band implicitly Peace's default type, index 0).
- `3 Way Crossover.peace` — per-channel `PreAmp{n}`, a `Routing=` line, and
  explicit `[FiltersN]` sections using non-default type indices (4, 5).
The filter-type ordinal → name array below is quoted from Peace's own
published AutoIt source (`Peace16911.au3`, SourceForge "Source code"); the
index-4/5 = LS/HS mapping was cross-checked against the crossover preset's
own internally-consistent PreAmp+shelf design (each driver's shelf boosts
exactly cancel that channel's PreAmp cut on its pass side), not assumed.

Because there is no literal APO text here to hand to `apo_config.parse_config`,
this module builds `apo_config`'s own dataclasses (`ProfileConfig`,
`ChannelSection`, `Filter`) directly from the parsed INI data — reusing the
existing data model rather than reusing the text parser, since there is no
APO-syntax text in a `.peace` file to reparse.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field

from rumble.core.apo_config import ChannelSection, Filter, ProfileConfig

# Peace's internal filter-type ordinal, in order (index = value stored in a
# "FilterN=" key). Quoted from Peace16911.au3.
PEACE_FILTER_TYPES: list[str] = [
    "PK", "LPQ", "HPQ", "BP", "LS", "HS", "NO", "AP",
    "LSC", "HSC", "BWLP", "BWHP", "LRLP", "LRHP", "LSCQ", "HSCQ", "LSQ", "HSQ",
]

# Peace-internal filter designs (Butterworth/Linkwitz-Riley multi-order
# pass filters, adjustable-Q shelf variants) that Peace itself expands into a
# multi-stage cascade when it writes an actual Equalizer APO config — there is
# no literal `Filter:` type token for these, so a band using one can't be
# faithfully imported from the saved ordinal alone.
UNSUPPORTED_PEACE_TYPES = frozenset(PEACE_FILTER_TYPES[10:])


@dataclass
class PeaceImportResult:
    """The result of parsing a `.peace` file: a ready-to-save `ProfileConfig`,
    Peace's own description text, and human-readable notes for any band that
    had to be skipped (never silently dropped)."""
    config: ProfileConfig
    description: str = ""
    skipped: list[str] = field(default_factory=list)


def parse_peace(text: str) -> PeaceImportResult:
    """Parse `.peace` INI text into a `PeaceImportResult`."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # preserve Peace's exact key casing (FrequencyN, GainN, ...)
    cp.read_string(text)

    general = cp["General"] if cp.has_section("General") else {}
    speakers = cp["Speakers"] if cp.has_section("Speakers") else {}

    description = general.get("Description", "") if general else ""

    indices: list[int] = []
    n = 0
    while speakers and f"SpeakerTargets{n}" in speakers:
        indices.append(n)
        n += 1
    if not indices:
        indices = [0]  # no [Speakers] section at all: a bare speaker-0 ("all") save

    sections: list[ChannelSection] = []
    skipped: list[str] = []

    for n in indices:
        channel = speakers.get(f"SpeakerTargets{n}", "all") if speakers else "all"
        suffix = "" if n == 0 else str(n)

        freqs = cp[f"Frequencies{suffix}"] if cp.has_section(f"Frequencies{suffix}") else {}
        gains = cp[f"Gains{suffix}"] if cp.has_section(f"Gains{suffix}") else {}
        qualities = cp[f"Qualities{suffix}"] if cp.has_section(f"Qualities{suffix}") else {}
        filter_types = cp[f"Filters{suffix}"] if cp.has_section(f"Filters{suffix}") else {}

        preamp_key = f"PreAmp{n}"
        preamp_db = float(general[preamp_key]) if (general and preamp_key in general) else None

        filters: list[Filter] = []
        band_indices = sorted(
            int(k[len("Gain"):]) for k in gains
            if k.startswith("Gain") and k[len("Gain"):].isdigit())
        for k in band_indices:
            fc = float(freqs.get(f"Frequency{k}", "1000")) if freqs else 1000.0
            gain = float(gains[f"Gain{k}"])
            quality = float(qualities.get(f"Quality{k}", "1.0")) if qualities else 1.0
            type_idx = int(filter_types.get(f"Filter{k}", "0")) if filter_types else 0
            ftype = (PEACE_FILTER_TYPES[type_idx]
                     if 0 <= type_idx < len(PEACE_FILTER_TYPES) else "PK")
            if ftype in UNSUPPORTED_PEACE_TYPES:
                skipped.append(
                    f"'{channel}' band at {fc:g} Hz uses Peace's {ftype} filter design, "
                    "which has no direct Equalizer APO filter type — skipped")
                continue
            filters.append(Filter(enabled=True, type=ftype, fc=fc, gain=gain, q=quality))

        if not filters and preamp_db is None:
            continue  # nothing audible on this speaker chain

        sections.append(ChannelSection(channel=channel, filters=filters, preamp_db=preamp_db))

    config = ProfileConfig(device="all", channel_sections=sections)

    routing = general.get("Routing", "") if general else ""
    if routing:
        config.copy_lines = [routing]

    return PeaceImportResult(config=config, description=description, skipped=skipped)
