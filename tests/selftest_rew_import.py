"""Self-test: REW (Room EQ Wizard) import path (Rumble 3.8).

Fixture text mirrors REW's own documented export formats (confirmed against
REW's official help docs — see rew_import.py's module docstring for
citations), not invented syntax. Covers both the pure parser
(rumble.core.rew_import) and the ProfileManager wiring
(import_rew_measurement / import_rew_filter_settings)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rumble.core import rew_import
from rumble.core.profile_manager import ProfileManager

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


# ── parse_measurement_text(): frequency-response export ──────────────────
MEASUREMENT_TEXT = """\
* REW SPL Measurement Export
* Freq(Hz)  SPL(dB)  Phase(degrees)
* Note: this is a synthetic fixture, not a real capture
20.0, 60.10, 12.0
21.0, 60.77, 11.4
50.0, 65.20, 5.0
100.0, 70.00, 2.0
1000.0, 72.50, -1.0
5000.0, 68.00, -20.0
20000.0, 58.00, -80.0
"""

result = rew_import.parse_measurement_text(MEASUREMENT_TEXT)
check(len(result.curve) == 7, "all 7 data lines are parsed")
check(result.curve[0] == (20.0, 60.10), "frequency and SPL (not phase) are extracted, in order")
check(result.curve[-1] == (20000.0, 58.00), "the last data point round-trips exactly")
check(not result.warnings, "a clean, strictly-increasing measurement produces no warnings")

check(rew_import.looks_like_filter_settings(MEASUREMENT_TEXT) is False,
      "a frequency-response export is never mistaken for a filter-settings export")

# Comment lines other than REW's own "*" marker (e.g. a stray column-header
# row) must also be ignored, per REW's own "only lines beginning with a
# number are imported" rule -- not just lines starting with "*".
HEADER_VARIANTS = """\
Frequency(Hz)  SPL(dB)
20.0  60.0
100.0  65.0
1000.0  70.0
5000.0  68.0
20000.0  60.0
"""
result2 = rew_import.parse_measurement_text(HEADER_VARIANTS)
check(len(result2.curve) == 5, "a non-numeric header line without a '*' marker is still skipped")

# Tab and semicolon delimiters (REW's other supported delimiters).
TAB_DELIMITED = "20.0\t60.0\n100.0\t65.0\n1000.0\t70.0\n5000.0\t68.0\n20000.0\t60.0\n"
SEMICOLON_DELIMITED = "20.0;60.0\n100.0;65.0\n1000.0;70.0\n5000.0;68.0\n20000.0;60.0\n"
check(rew_import.parse_measurement_text(TAB_DELIMITED).curve
      == rew_import.parse_measurement_text(SEMICOLON_DELIMITED).curve
      == rew_import.parse_measurement_text(HEADER_VARIANTS).curve,
      "comma, tab and semicolon delimiters all parse to the identical curve")

# Out-of-order frequencies are skipped and reported, not silently reordered.
OUT_OF_ORDER = "20.0, 60.0\n100.0, 65.0\n50.0, 63.0\n1000.0, 70.0\n5000.0, 68.0\n20000.0, 60.0\n"
result3 = rew_import.parse_measurement_text(OUT_OF_ORDER)
check(len(result3.curve) == 5 and len(result3.warnings) == 1,
      "an out-of-order point is dropped and reported rather than corrupting the curve")

check(rew_import.parse_measurement_text("").curve == [],
      "empty input produces an empty curve, not an error")
check(rew_import.parse_measurement_text("not a number, still not\n").curve == [],
      "a file with no numeric-leading lines at all produces an empty curve")

FEW_POINTS = "20.0, 60.0\n100.0, 65.0\n"
result4 = rew_import.parse_measurement_text(FEW_POINTS)
check(len(result4.curve) == 2 and result4.warnings,
      "fewer than REW's own 5-point minimum is still parsed but flagged as likely truncated")


# ── parse_filter_settings_text(): Filter Settings export ─────────────────
FILTER_SETTINGS_TEXT = """\
Filter Settings file

Room EQ V5.20
Dated: 07/10/26 12:00:00 PM

Notes:

Equaliser: Generic

Filter  1: ON  PK       Fc    7515 Hz  Gain  -9.7 dB  Q  1.00
Filter  2: ON  PK       Fc     100 Hz  Gain   4.0 dB  Q  0.71
Filter  3: OFF PK       Fc    2000 Hz  Gain  -3.0 dB  Q  2.00
Filter  4: ON  LS       Fc      80 Hz  Gain   3.0 dB  Q  0.71
Filter  5: ON  None
Filter  6: ON  PA       Fc   129.1Hz (  125 +2 )   Gain -18.5dB  BW/60  4.0
"""

check(rew_import.looks_like_filter_settings(FILTER_SETTINGS_TEXT) is True,
      "a Filter Settings export is correctly detected despite several header lines first")
check(rew_import.looks_like_filter_settings(MEASUREMENT_TEXT) is False,
      "a measurement export is never mistaken for a Filter Settings export")

fresult = rew_import.parse_filter_settings_text(FILTER_SETTINGS_TEXT)
check(len(fresult.filters) == 4, "4 real filters parsed (1 empty 'None' slot, 1 BW/60 filter excluded)")
f1 = fresult.filters[0]
check(f1.type == "PK" and f1.fc == 7515.0 and f1.gain == -9.7 and f1.q == 1.00 and f1.enabled,
      "an ON PK filter's type/Fc/Gain/Q all parse correctly")
f3 = fresult.filters[2]
check(f3.enabled is False, "an OFF filter's enabled state parses correctly")
f4 = fresult.filters[3]
check(f4.type == "LS" and f4.fc == 80.0,
      "a shelf filter type (LS) parses via the same reused apo_config logic as PK")
check(len(fresult.skipped) == 1 and "BW/60" in fresult.skipped[0],
      "the BW/60-defined filter is reported as skipped, not silently misparsed as Q=1.0")

check(rew_import.parse_filter_settings_text("").filters == [],
      "empty input produces no filters, not an error")


# ── blend_with_community(): weighted merge reusing autoeq_merge ──────────
rew_curve = [(100.0, 0.0), (1000.0, 0.0)]
community_curve = [(100.0, 4.0), (1000.0, -4.0)]
blended = dict(rew_import.blend_with_community(rew_curve, community_curve))
check(abs(blended[100.0]) < 2.0,
      "the blend pulls strongly toward the REW measurement, not a flat 50/50 average "
      "(a flat average would be 2.0)")
check(rew_import.REW_MEASUREMENT_WEIGHT > 3.0,
      "a user's own measurement is weighted above even Spinorama's best 'high' "
      "community-quality tier (3.0)")


# ── ProfileManager wiring ─────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp(prefix="rumble_rew_test_"))
manager = ProfileManager(data_dir=tmp, config_path=tmp)

meas_path = tmp / "measurement.txt"
meas_path.write_text(MEASUREMENT_TEXT, encoding="utf-8")
meta, clamp, warnings = manager.import_rew_measurement(meas_path)
check(meta is not None, "import_rew_measurement() creates a profile from a valid measurement")
check(meta is not None and "REW" in meta.tags, "the imported profile is tagged REW")
config = manager.load_profile_config(meta.id) if meta else None
check(config is not None and config.channel_sections
      and config.channel_sections[0].graphic_eq,
      "the imported profile carries a real GraphicEQ correction curve")

filters_path = tmp / "filters.txt"
filters_path.write_text(FILTER_SETTINGS_TEXT, encoding="utf-8")
fmeta, fskipped = manager.import_rew_filter_settings(filters_path)
check(fmeta is not None, "import_rew_filter_settings() creates a profile from valid filter settings")
fconfig = manager.load_profile_config(fmeta.id) if fmeta else None
check(fconfig is not None and len(fconfig.channel_sections[0].filters) == 4,
      "the imported profile carries all 4 real (non-empty, non-BW/60) filters")
check(len(fskipped) == 1, "the BW/60 skip note is threaded back through ProfileManager")

# Blending against an already-imported profile's curve.
community_meta_curve = [(100.0, 3.0), (1000.0, -3.0), (10000.0, 6.0)]
bmeta, bclamp, bwarnings = manager.import_rew_measurement(
    meas_path, name="Blended Import", blend_curve=community_meta_curve)
check(bmeta is not None and "blended" in bmeta.description.lower(),
      "a blended import's description notes that it was blended")

empty_path = tmp / "empty.txt"
empty_path.write_text("not usable data\n", encoding="utf-8")
empty_meta, empty_clamp, empty_warnings = manager.import_rew_measurement(empty_path)
check(empty_meta is None, "an unusable measurement file returns no profile rather than a broken one")

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
