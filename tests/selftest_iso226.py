"""Selftest: rumble.core.iso226 -- ISO 226:2003 equal-loudness-contour SPL
computation and loudness-compensation curve derivation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import iso226


def test_1khz_spl_equals_phon_by_definition():
    # ISO 226:2003 defines the phon scale itself at 1kHz, so the contour's
    # SPL at 1000 Hz must equal the requested phon level for any phon.
    idx_1khz = iso226.FREQUENCIES.index(1000)
    for phon in (20.0, 40.0, 60.0, 80.0, 90.0):
        spl = iso226.equal_loudness_spl(idx_1khz, phon)
        assert abs(spl - phon) < 0.1


def test_phon_zero_uses_threshold_of_hearing():
    idx = iso226.FREQUENCIES.index(1000)
    assert iso226.equal_loudness_spl(idx, 0.0) == iso226.T_F[idx]


def test_bass_needs_more_spl_than_1khz_at_any_phon():
    # At any real listening level, a 100 Hz tone needs more SPL than a 1kHz
    # tone to sound equally loud (bass is perceptually attenuated).
    idx_100hz = iso226.FREQUENCIES.index(100)
    for phon in (20.0, 40.0, 60.0, 80.0):
        spl = iso226.equal_loudness_spl(idx_100hz, phon)
        assert spl > phon


def test_compensation_curve_is_flat_at_1khz():
    # The standard's fitted formula only nearly satisfies SPL(1kHz,phon)==phon
    # (see test_1khz_spl_equals_phon_by_definition's own 0.1 dB tolerance), so
    # the 1kHz compensation is negligible rather than exactly zero.
    curve = iso226.loudness_compensation_curve(reference_phon=80.0, current_phon=40.0)
    as_dict = dict(curve)
    assert abs(as_dict[1000.0]) < 0.05


def test_compensation_curve_boosts_bass_when_quieter_than_reference():
    # Turning down from 80 phon (reference) to 40 phon (current) should
    # need a bass boost at 100 Hz to keep the same perceived balance.
    curve = iso226.loudness_compensation_curve(reference_phon=80.0, current_phon=40.0)
    as_dict = dict(curve)
    assert as_dict[100.0] > 0.0


def test_compensation_curve_is_zero_when_current_equals_reference():
    curve = iso226.loudness_compensation_curve(reference_phon=70.0, current_phon=70.0)
    assert all(abs(g) < 1e-9 for _, g in curve)


def test_compensation_curve_reverses_sign_when_louder_than_reference():
    quieter = dict(iso226.loudness_compensation_curve(reference_phon=80.0, current_phon=40.0))
    louder = dict(iso226.loudness_compensation_curve(reference_phon=40.0, current_phon=80.0))
    # Symmetric: going from A->B is the exact negation of B->A.
    for f in iso226.FREQUENCIES:
        assert abs(quieter[float(f)] + louder[float(f)]) < 1e-6


def test_phon_is_clamped_to_documented_valid_range():
    idx_100hz = iso226.FREQUENCIES.index(100)
    below = iso226.equal_loudness_spl(idx_100hz, -20.0)
    at_zero = iso226.equal_loudness_spl(idx_100hz, 0.0)
    assert below == at_zero
    above = iso226.equal_loudness_spl(idx_100hz, 200.0)
    at_max = iso226.equal_loudness_spl(idx_100hz, iso226.MAX_PHON)
    assert above == at_max


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
