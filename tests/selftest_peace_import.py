"""Self-test: Peace `.peace` config importer (Rumble 4.5).

Fixture text is a trimmed-but-verbatim excerpt of two REAL presets
downloaded 2026-07-09 from Peace's own SourceForge "Configurations (presets)"
library (Bass Boost.peace, 3 Way Crossover.peace) -- not invented data. See
peace_import.py's module docstring for the full provenance/citations."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core.peace_import import parse_peace

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── "Bass Boost.peace": speaker 0 ("all") only, no [Filters] section at all
# → every band is Peace's implicit default type (PK) ───────────────────────
BASS_BOOST = """\
[Frequencies]
Frequency1=10
Frequency2=21
Frequency3=42
[Gains]
Gain1=9.5
Gain2=7.5
Gain3=6
[Qualities]
Quality1=1
Quality2=1
Quality3=1
[General]
Description=Bass boost using peak filters
[Speakers]
SpeakerId0=0
SpeakerTargets0=all
SpeakerName0=All
"""

result = parse_peace(BASS_BOOST)
check(result.description == "Bass boost using peak filters",
      "Description= is captured verbatim")
check(len(result.config.channel_sections) == 1,
      "a single-speaker (\"all\") save produces exactly one ChannelSection")
sec = result.config.channel_sections[0]
check(sec.channel == "all", "the section's channel token is Peace's SpeakerTargets0 (\"all\")")
check(len(sec.filters) == 3, "all 3 bands with a Gain entry are imported")
check(all(f.type == "PK" for f in sec.filters),
      "bands default to PK when no [Filters] section exists at all")
check(abs(sec.filters[0].fc - 10.0) < 1e-6 and abs(sec.filters[0].gain - 9.5) < 1e-6,
      "frequency and gain values round-trip exactly for the first band")
check(sec.preamp_db is None, "no PreAmp0 key means no per-channel preamp override")
check(not result.skipped, "no bands were skipped (all are the supported default PK type)")

# ── "3 Way Crossover.peace": per-channel PreAmp, Routing, and explicit
# shelf filter types (index 4 = LS, index 5 = HS, quoted from Peace16911.au3)
# ───────────────────────────────────────────────────────────────────────────
CROSSOVER = """\
[General]
Description=3 way crossover by using shelf filters and pre amps
Routing=R=1.000*L C=1.000*L SUB=1.000*R RL=1.000*R RR=1.000*R
PreAmp1=-10
PreAmp2=-20
[Speakers]
SpeakerId0=0
SpeakerTargets0=all
SpeakerName0=All
SpeakerId1=1
SpeakerTargets1=L
SpeakerName1=High Left (Left)
SpeakerId2=2
SpeakerTargets2=R
SpeakerName2=Mid Left (Right)
[Frequencies1]
Frequency10=4000
[Qualities1]
Quality10=1.41
[Gains1]
Gain10=10
[Filters1]
Filter10=5
[Frequencies2]
Frequency5=166
Frequency10=4000
[Qualities2]
Quality5=1.41
Quality10=1.41
[Gains2]
Gain5=10
Gain10=10
[Filters2]
Filter5=5
Filter10=4
"""

result = parse_peace(CROSSOVER)
check(result.config.copy_lines == ["R=1.000*L C=1.000*L SUB=1.000*R RL=1.000*R RR=1.000*R"],
      "Routing= becomes a Copy: line's parameter string verbatim")
by_channel = {s.channel: s for s in result.config.channel_sections}
check("L" in by_channel and "R" in by_channel,
      "SpeakerTargets{n} tokens (L, R) become the imported sections' channel names")
check(abs(by_channel["L"].preamp_db - (-10.0)) < 1e-6,
      "PreAmp1 becomes the L section's per-channel preamp_db")
check(abs(by_channel["R"].preamp_db - (-20.0)) < 1e-6,
      "PreAmp2 becomes the R section's per-channel preamp_db")
l_filter = by_channel["L"].filters[0]
check(l_filter.type == "HS" and abs(l_filter.fc - 4000.0) < 1e-6 and abs(l_filter.gain - 10.0) < 1e-6,
      "Filter10=5 on the L channel maps to Peace ordinal 5 = HS (High Shelf) at the right freq/gain")
r_types = {round(f.fc): f.type for f in by_channel["R"].filters}
check(r_types == {166: "HS", 4000: "LS"},
      "the R channel's two shelf bands map to HS@166Hz and LS@4000Hz per ordinals 5 and 4")
check(not result.skipped, "no unsupported filter types were used in this preset")

# ── a band using one of Peace's cascade-only types (no literal APO Filter:
# token) is skipped, not silently imported with the wrong shape ────────────
UNSUPPORTED_TYPE = """\
[Frequencies]
Frequency1=80
[Qualities]
Quality1=0.707
[Gains]
Gain1=0
[Filters]
Filter1=10
[General]
Description=Butterworth low-pass test
"""
result = parse_peace(UNSUPPORTED_TYPE)
check(not result.config.channel_sections,
      "a preset whose only band is an unsupported Peace-cascade type imports no filters")
check(len(result.skipped) == 1 and "BWLP" in result.skipped[0],
      "the skipped band is reported back by name, not silently dropped")

# ── no [Speakers] section at all: a bare speaker-0 save ────────────────────
BARE = """\
[Frequencies]
Frequency1=1000
[Gains]
Gain1=3
[Qualities]
Quality1=1
"""
result = parse_peace(BARE)
check(len(result.config.channel_sections) == 1 and result.config.channel_sections[0].channel == "all",
      "a file with no [Speakers] section at all still imports as a single \"all\" channel")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
