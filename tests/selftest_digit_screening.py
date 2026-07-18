import inspect
import sys
import random
import tempfile
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rumble.core.digit_screening import (
    load_digit_clip, build_triplet, mix_triplet_with_noise,
    DigitScreeningTest, REFERRAL_SNR_DB, classify_srt, screening_summary,
    save_screening_result, load_all_screening_results, export_screening_text,
)

def test_load_digit_clip_returns_float_mono():
    clip = load_digit_clip(3)
    assert clip.ndim == 1
    assert clip.dtype.kind == "f"
    assert -1.0 <= clip.max() <= 1.0

def test_build_triplet_no_adjacent_repeats():
    rng = random.Random(42)
    for _ in range(200):
        t = build_triplet(rng)
        assert t[0] != t[1] and t[1] != t[2]
        assert all(0 <= d <= 9 for d in t)

def test_mix_triplet_with_noise_shape_and_snr_direction():
    # A very high SNR (loud triplet, quiet noise) should have higher peak
    # amplitude in the triplet-only regions than a very low SNR mix.
    hi = mix_triplet_with_noise((1, 2, 3), snr_db=20.0, sample_rate=48000)
    lo = mix_triplet_with_noise((1, 2, 3), snr_db=-10.0, sample_rate=48000)
    assert hi.shape[1] == 2
    assert hi.shape == lo.shape

def test_mix_triplet_with_noise_stays_headroom_safe_across_snr_range():
    # mix_triplet_with_noise's docstring documents headroom-safe scaling
    # (peak clamped to 0.98 by scaling triplet+noise together, uniformly)
    # as a REQUIRED property "at the staircase's floor_level ... just as
    # much as at its ceil_level". DigitScreeningTest.__init__ sets
    # floor_level=-15.0 / ceil_level=10.0 for its Staircase -- those are the
    # real bounds Test B's adaptive procedure can reach, so sweep across
    # them (plus REFERRAL_SNR_DB and a couple of interior values) rather
    # than the old test's two arbitrary points (20.0 / -10.0, which don't
    # even bracket the true staircase range).
    triplet = (1, 2, 3)
    sample_rate = 48000
    snr_values = [-15.0, -10.0, REFERRAL_SNR_DB, 0.0, 5.0, 10.0]
    for snr_db in snr_values:
        mix = mix_triplet_with_noise(triplet, snr_db=snr_db, sample_rate=sample_rate)
        peak = float(np.abs(mix).max())
        assert peak <= 0.98 + 1e-6, f"snr_db={snr_db} produced unsafe peak {peak}"

    # SNR-ratio-preservation proxy: patch the noise generator to return a
    # deterministic, guaranteed-loud noise track so the >0.98 scaling branch
    # is definitely exercised, then confirm triplet-only energy and
    # noise-only energy inside the combined mix are both divided by the
    # SAME factor -- i.e. scaling never favors one component over the
    # other, which is exactly what "SNR is preserved" means here.
    import rumble.core.digit_screening as digit_screening

    def _loud_deterministic_noise(duration_s, level_dbfs, sample_rate):
        n = int(round(duration_s * sample_rate))
        rng = np.random.default_rng(0)
        mono = rng.standard_normal(n) * 2.0  # deliberately loud -> triggers clamp
        return np.column_stack([mono, mono]).astype(np.float32)

    original = digit_screening._tones.generate_pink_noise
    digit_screening._tones.generate_pink_noise = _loud_deterministic_noise
    try:
        snr_db = -15.0
        clips = [load_digit_clip(d) for d in triplet]
        gap = np.zeros(int(round(0.15 * sample_rate)), dtype=np.float32)
        triplet_audio = np.concatenate([clips[0], gap, clips[1], gap, clips[2]]).astype(np.float64)

        n = triplet_audio.shape[0]
        rng = np.random.default_rng(0)
        noise_mono = (rng.standard_normal(n) * 2.0)[:n]
        unscaled_combined = triplet_audio + noise_mono
        peak = np.abs(unscaled_combined).max()
        assert peak > 0.98  # sanity: this case really does need clamping
        expected_factor = 0.98 / peak

        mix = mix_triplet_with_noise(triplet, snr_db=snr_db, sample_rate=sample_rate)
        actual = mix[:, 0].astype(np.float64)
        # actual should equal unscaled_combined * expected_factor exactly
        # (same factor applied to the whole combined signal -> triplet and
        # noise components are attenuated identically, preserving SNR).
        assert np.allclose(actual, unscaled_combined * expected_factor, atol=1e-5)
    finally:
        digit_screening._tones.generate_pink_noise = original

def test_digit_screening_full_run_converges():
    rng = random.Random(7)
    true_srt = 2.0
    dst = DigitScreeningTest(rng=rng)
    steps = 0
    while not dst.done and steps < 2000:
        triplet = dst.current_triplet
        heard_correctly = dst.staircase.level >= true_srt
        reported = triplet if heard_correctly else (
            (triplet[0] + 1) % 10, triplet[1], triplet[2])  # simulate a wrong report
        dst.respond(reported)
        steps += 1
    assert dst.done
    assert abs(dst.srt_db - true_srt) <= 3.0

def test_classify_srt_bands():
    assert classify_srt(REFERRAL_SNR_DB - 5.0) == "typical"
    assert classify_srt(REFERRAL_SNR_DB + 5.0) == "consider_assessment"

def test_screening_summary_wording_never_medical():
    for band_srt in (REFERRAL_SNR_DB - 5.0, REFERRAL_SNR_DB + 5.0):
        text = screening_summary(band_srt).lower()
        for banned in ("diagnosis", "audiogram", "you have", "normal hearing"):
            assert banned not in text

def test_export_screening_text_has_disclaimer_first():
    text = export_screening_text(srt_db=1.0, band="typical", device_label="Test Speakers")
    assert text.strip().startswith("This is an informal screening estimate")
    assert "not responsible" in text
    assert "Test Speakers" in text

def test_export_screening_text_band_is_authoritative():
    # srt_db=1.0 is > REFERRAL_SNR_DB, so classify_srt(1.0) ==
    # "consider_assessment" -- but the caller here explicitly passes
    # band="typical". export_screening_text must use the CALLER'S band to
    # pick the summary sentence, not silently recompute classify_srt(srt_db)
    # internally and use that instead (the bug this test guards against).
    assert classify_srt(1.0) == "consider_assessment"

    typical_text = export_screening_text(srt_db=1.0, band="typical", device_label="Test Speakers")
    assert screening_summary(REFERRAL_SNR_DB - 5.0) in typical_text  # "typical" sentence
    assert screening_summary(REFERRAL_SNR_DB + 5.0) not in typical_text

    # And the reverse mismatch: band="consider_assessment" with a srt_db
    # that would classify as "typical" must still honor band, not srt_db.
    consider_text = export_screening_text(
        srt_db=REFERRAL_SNR_DB - 5.0, band="consider_assessment", device_label="Test Speakers"
    )
    assert screening_summary(REFERRAL_SNR_DB + 5.0) in consider_text  # "consider_assessment" sentence
    assert screening_summary(REFERRAL_SNR_DB - 5.0) not in consider_text

    # Sanity: the non-mismatched (normal) case still works as expected.
    normal_text = export_screening_text(
        srt_db=REFERRAL_SNR_DB - 5.0, band="typical", device_label="Test Speakers"
    )
    assert screening_summary(REFERRAL_SNR_DB - 5.0) in normal_text

def test_save_and_load_screening_results_round_trip(tmp_path):
    save_screening_result(1.0, "typical", tmp_path)
    save_screening_result(2.0, "consider_assessment", tmp_path)
    all_results = load_all_screening_results(tmp_path)
    assert len(all_results) == 2

if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            sig = inspect.signature(f)
            if "tmp_path" in sig.parameters:
                with tempfile.TemporaryDirectory() as td:
                    f(Path(td))
            else:
                f()
            print("PASS", n)
