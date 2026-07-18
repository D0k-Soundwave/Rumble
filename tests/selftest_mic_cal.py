"""Selftest: C1b microphone calibration import + confidence tiers
(rumble/core/mic_cal.py).

Run:  python tests/selftest_mic_cal.py

Covers: miniDSP/UMIK text parsing (with/without a "Sens Factor" header line,
tolerating comment lines, reporting malformed lines rather than raising);
save/list/load round-trip of a calibration + its JSON sidecar; cal
subtraction on mismatched frequency grids against hand-computed log-linear
interpolation values; and the four-way correction_band() tier model (no
cal, file, transfer, estimated with/without a stored band).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import mic_cal

_TOL = 1e-9

# ── fixtures ─────────────────────────────────────────────────────────────

WITH_SENS_TEXT = """\
"Sens Factor =-8.760dB, SERNO: 7145562"
* miniDSP UMIK-1 calibration file
20.00\t-1.2
500.00\t0.0
20000.00\t1.0
"""

# One line here (just "500.0" with no gain column) is deliberately malformed
# so parsing a real file's worth of good data is also checked to survive one
# bad row rather than discarding everything.
WITH_SENS_AND_BAD_LINE_TEXT = WITH_SENS_TEXT + "500.0\n"

WITHOUT_SENS_TEXT = """\
* Generic calibration file, no sensitivity header
20.00 0.5
200.00 0.1
20000.00 1.0
"""

MALFORMED_TEXT = """\
this is not a calibration file
just some random descriptive text
no numeric data rows at all
"""


# ── parse_cal_text() ─────────────────────────────────────────────────────

def test_parse_with_sens_line():
    result = mic_cal.parse_cal_text(WITH_SENS_TEXT)
    assert result.sens_factor_db == -8.760
    assert result.curve == [(20.0, -1.2), (500.0, 0.0), (20000.0, 1.0)]
    assert result.warnings == []


def test_parse_without_sens_line():
    result = mic_cal.parse_cal_text(WITHOUT_SENS_TEXT)
    assert result.sens_factor_db is None
    assert result.curve == [(20.0, 0.5), (200.0, 0.1), (20000.0, 1.0)]
    assert result.warnings == []


def test_parse_tolerates_one_bad_line_without_losing_the_rest():
    result = mic_cal.parse_cal_text(WITH_SENS_AND_BAD_LINE_TEXT)
    assert result.curve == [(20.0, -1.2), (500.0, 0.0), (20000.0, 1.0)]
    assert len(result.warnings) == 1
    assert "500.0" in result.warnings[0]


def test_parse_malformed_file_is_rejected_with_a_reason():
    result = mic_cal.parse_cal_text(MALFORMED_TEXT)
    assert result.curve == []
    assert result.warnings
    assert "no usable" in result.warnings[-1].lower()


# ── save_cal() / list_cals() / load_cal() round-trip ────────────────────

def test_save_list_load_round_trip_file_source():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        curve = [(20.0, -1.2), (100.0, 0.3), (1000.0, 0.0), (10000.0, -0.9), (20000.0, -2.1)]

        cal_id = mic_cal.save_cal(data_dir, "UMIK 7145562", curve, source="file")

        entries = mic_cal.list_cals(data_dir)
        assert len(entries) == 1
        assert entries[0].id == cal_id
        assert entries[0].name == "UMIK 7145562"
        assert entries[0].source == "file"
        assert entries[0].band is None

        loaded = mic_cal.load_cal(data_dir, cal_id)
        assert loaded is not None
        entry, loaded_curve = loaded
        assert entry == entries[0]
        assert loaded_curve == curve

        # The sidecar really is a small, separate JSON file (not the curve
        # data itself) -- the curve rides in the GraphicEQ-format .txt.
        sidecar_path = data_dir / mic_cal.CAL_SUBDIR / f"{cal_id}.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar == {"name": "UMIK 7145562", "source": "file", "band": None}
        txt_path = data_dir / mic_cal.CAL_SUBDIR / f"{cal_id}.txt"
        assert txt_path.exists()
        assert "GraphicEQ" not in txt_path.read_text(encoding="utf-8")


def test_save_estimated_source_stores_and_returns_its_band():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        curve = [(300.0, 0.4), (5000.0, -0.6)]
        cal_id = mic_cal.save_cal(
            data_dir, "Estimated from KEF LS50", curve,
            source="estimated", band=(250.0, 8000.0))

        entry, loaded_curve = mic_cal.load_cal(data_dir, cal_id)
        assert entry.source == "estimated"
        assert entry.band == (250.0, 8000.0)
        assert loaded_curve == curve


def test_duplicate_names_get_distinct_ids():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        first = mic_cal.save_cal(data_dir, "My Mic", [(1000.0, 0.0)], source="file")
        second = mic_cal.save_cal(data_dir, "My Mic", [(1000.0, 0.0)], source="file")
        assert first != second
        assert len(mic_cal.list_cals(data_dir)) == 2


def test_list_cals_empty_when_no_directory():
    with tempfile.TemporaryDirectory() as tmp:
        assert mic_cal.list_cals(Path(tmp)) == []


def test_load_cal_missing_id_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        assert mic_cal.load_cal(Path(tmp), "does_not_exist") is None


# ── subtract_cal() on mismatched grids ───────────────────────────────────

def test_subtract_cal_on_mismatched_grids_matches_hand_computed_points():
    # measurement: linear in dB over log10(f) from (10 Hz, 0 dB) to
    # (10000 Hz, 8 dB) -- slope 8/3 dB per decade between x=log10(f)=1..4.
    measurement = [(10.0, 0.0), (10000.0, 8.0)]
    # cal: linear from (100 Hz, 1 dB) to (1000 Hz, 3 dB) -- slope 2 dB/decade
    # over x=2..3, clamped to its endpoint gain outside that range.
    cal = [(100.0, 1.0), (1000.0, 3.0)]

    result = mic_cal.subtract_cal(measurement, cal)

    expected = {
        10.0: 0.0 - 1.0,                    # cal clamps below its range to 1.0
        100.0: (8.0 / 3.0) - 1.0,            # measurement interpolated, cal exact
        1000.0: (16.0 / 3.0) - 3.0,          # measurement interpolated, cal exact
        10000.0: 8.0 - 3.0,                  # cal clamps above its range to 3.0
    }
    assert [f for f, _ in result] == sorted(expected)
    for f, g in result:
        assert abs(g - expected[f]) < 1e-9, (f, g, expected[f])


def test_subtract_cal_empty_inputs_are_safe():
    assert mic_cal.subtract_cal([], [(100.0, 1.0)]) == []
    assert mic_cal.subtract_cal([(100.0, 1.0)], []) == []


# ── correction_band() tier model (all four source cases) ────────────────

def test_correction_band_no_cal_uses_default_schroeder_cap():
    assert mic_cal.correction_band(None) == (20.0, 300.0)


def test_correction_band_no_cal_honors_custom_cap_within_bounds():
    assert mic_cal.correction_band(None, uncalibrated_cap_hz=400.0) == (20.0, 400.0)


def test_correction_band_no_cal_clamps_custom_cap_to_documented_range():
    assert mic_cal.correction_band(None, uncalibrated_cap_hz=50.0) == (20.0, 200.0)
    assert mic_cal.correction_band(None, uncalibrated_cap_hz=1000.0) == (20.0, 500.0)


def test_correction_band_file_source_is_full_range():
    entry = mic_cal.MicCalEntry(id="a", name="A", source="file")
    assert mic_cal.correction_band(entry) == mic_cal.FULL_RANGE_HZ == (20.0, 20000.0)


def test_correction_band_transfer_source_is_full_range():
    entry = mic_cal.MicCalEntry(id="b", name="B", source="transfer")
    assert mic_cal.correction_band(entry) == mic_cal.FULL_RANGE_HZ


def test_correction_band_estimated_source_uses_its_own_stored_band():
    entry = mic_cal.MicCalEntry(id="c", name="C", source="estimated", band=(250.0, 8000.0))
    assert mic_cal.correction_band(entry) == (250.0, 8000.0)


def test_correction_band_estimated_source_without_band_falls_back_to_uncalibrated():
    entry = mic_cal.MicCalEntry(id="d", name="D", source="estimated", band=None)
    assert mic_cal.correction_band(entry) == (20.0, 300.0)


# ── derive_transfer_cal() (S16, C4a) ─────────────────────────────────────

# Frequencies spaced a decade apart -- well outside octave_smooth_adaptive's
# widest window (2 octaves, above 8 kHz per eq_target.ADAPTIVE_SMOOTH_
# TREBLE_WINDOW_OCT), so each point's own smoothing window never reaches its
# neighbors and the recovered curve should match the invented "true" curve
# almost exactly, not just approximately.
_XFER_FREQS = [20.0, 200.0, 2000.0, 20000.0]


def _sum_curve(a, b):
    return [(f, ga + gb) for (f, ga), (_, gb) in zip(a, b)]


def test_derive_transfer_cal_recovers_unknown_mic_curve():
    # A synthetic "room" (the true, mic-independent speaker+room+position
    # response), the reference mic's own known error, and the "true"
    # unknown-mic error derive_transfer_cal should recover.
    room = [(f, g) for f, g in zip(_XFER_FREQS, [-1.0, 0.5, 0.0, 1.0])]
    reference_cal = [(f, g) for f, g in zip(_XFER_FREQS, [0.2, -0.1, 0.0, -0.2])]
    unknown_true = [(f, g) for f, g in zip(_XFER_FREQS, [1.0, -0.5, 0.3, -1.0])]

    # Each mic's raw measurement is the true room response plus its own
    # capsule error -- the same additive model subtract_cal() assumes
    # everywhere else in this module.
    reference_curve = _sum_curve(room, reference_cal)
    unknown_curve = _sum_curve(room, unknown_true)

    result = mic_cal.derive_transfer_cal(reference_curve, reference_cal, unknown_curve)
    result_map = dict(result)
    for f, expected in unknown_true:
        assert abs(result_map[f] - expected) < 0.05, (f, result_map[f], expected)


def test_derive_transfer_cal_saves_with_transfer_source_and_full_range_band():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        room = [(f, g) for f, g in zip(_XFER_FREQS, [-1.0, 0.5, 0.0, 1.0])]
        reference_cal = [(f, g) for f, g in zip(_XFER_FREQS, [0.2, -0.1, 0.0, -0.2])]
        unknown_true = [(f, g) for f, g in zip(_XFER_FREQS, [1.0, -0.5, 0.3, -1.0])]
        reference_curve = _sum_curve(room, reference_cal)
        unknown_curve = _sum_curve(room, unknown_true)

        result = mic_cal.derive_transfer_cal(reference_curve, reference_cal, unknown_curve)
        cal_id = mic_cal.save_cal(data_dir, "Transfer Test", result, source="transfer")

        entry, loaded_curve = mic_cal.load_cal(data_dir, cal_id)
        assert entry.source == "transfer"
        # band is only ever stored for source="estimated" -- a transfer cal
        # is trusted full-range unconditionally, not from a stored band.
        assert entry.band is None
        assert mic_cal.correction_band(entry) == mic_cal.FULL_RANGE_HZ == (20.0, 20000.0)
        assert loaded_curve == result


# ── derive_estimated_cal() / estimated_band() (S17, C4b) ────────────────

# Same decade-spacing convention as _XFER_FREQS above -- well outside
# octave_smooth_adaptive's widest window (2.0 octaves, the setting
# derive_estimated_cal uses uniformly), so each point's own smoothing
# window never reaches its neighbors and the recovered curve should match
# the invented "true" mic curve almost exactly.
_EST_FREQS = [20.0, 200.0, 2000.0, 20000.0]


def test_estimated_gate_f_lo_hz_is_inverse_of_gate_window():
    assert abs(mic_cal.estimated_gate_f_lo_hz(5.0) - 200.0) < 1e-9
    assert abs(mic_cal.estimated_gate_f_lo_hz(2.0) - 500.0) < 1e-9


def test_estimated_band_uses_gate_f_lo_and_fixed_ceiling():
    assert mic_cal.estimated_band(5.0) == (200.0, mic_cal.ESTIMATED_BAND_HIGH_HZ)
    assert mic_cal.estimated_band(4.0) == (250.0, 10000.0)


def test_estimated_band_never_inverts_for_an_implausibly_short_gate():
    f_lo, f_hi = mic_cal.estimated_band(0.05)  # implies f_lo = 20000 Hz
    assert f_lo <= f_hi
    assert f_hi == mic_cal.ESTIMATED_BAND_HIGH_HZ


def test_estimated_tier_max_boost_db_is_half_the_standard_cap():
    assert mic_cal.ESTIMATED_TIER_MAX_BOOST_DB == 3.0


def test_derive_estimated_cal_recovers_mic_curve_within_band():
    # A synthetic reference speaker's published anechoic on-axis curve, and
    # the "true" mic response derive_estimated_cal should recover from a
    # gated measurement of that speaker.
    speaker_on_axis = [(f, g) for f, g in zip(_EST_FREQS, [-1.0, 0.0, 0.5, -2.0])]
    mic_true = [(f, g) for f, g in zip(_EST_FREQS, [0.8, -0.3, 0.2, 1.0])]
    # A gated measurement through the mic reports the speaker's own on-axis
    # response plus the mic's own error -- the same additive model
    # subtract_cal() assumes everywhere else in this module.
    gated_measurement = _sum_curve(speaker_on_axis, mic_true)

    result = mic_cal.derive_estimated_cal(gated_measurement, speaker_on_axis)
    result_map = dict(result)
    for f, expected in mic_true:
        assert abs(result_map[f] - expected) < 0.05, (f, result_map[f], expected)


def test_derive_estimated_cal_saves_with_estimated_source_and_gate_band():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        speaker_on_axis = [(f, g) for f, g in zip(_EST_FREQS, [-1.0, 0.0, 0.5, -2.0])]
        mic_true = [(f, g) for f, g in zip(_EST_FREQS, [0.8, -0.3, 0.2, 1.0])]
        gated_measurement = _sum_curve(speaker_on_axis, mic_true)

        result = mic_cal.derive_estimated_cal(gated_measurement, speaker_on_axis)
        gate_ms = 4.0
        band = mic_cal.estimated_band(gate_ms)
        cal_id = mic_cal.save_cal(data_dir, "Estimated from Test Speaker", result,
                                  source="estimated", band=band)

        entry, loaded_curve = mic_cal.load_cal(data_dir, cal_id)
        assert entry.source == "estimated"
        assert entry.band == band == (250.0, 10000.0)
        assert mic_cal.correction_band(entry) == band
        # format_graphiceq() rounds gains to 1 decimal place -- compare
        # within that rounding tolerance rather than requiring bit-identical
        # floats (result itself carries ordinary float-arithmetic noise from
        # the subtract/smooth chain above).
        assert [f for f, _ in loaded_curve] == [f for f, _ in result]
        for (_, loaded_g), (_, result_g) in zip(loaded_curve, result):
            assert abs(loaded_g - result_g) < 0.05, (loaded_g, result_g)


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
