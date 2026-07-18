import inspect
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rumble.core.hearing_test import (
    Staircase,
    STANDARD_FREQS,
    COARSE_FREQS,
    REFERENCE_SOUNDS,
    FrequencyTest,
    baseline_complete,
    normalise,
    frequency_flags,
    load_reference_sound,
    load_volume_reference,
    progressive_order,
    summarise,
    save_results,
    load_all_results,
    export_text,
    volume_drift_note,
)

def run_sim(true_threshold, start=-30.0):
    """Simulated listener: hears anything at/above their threshold."""
    s = Staircase(start_level=start)
    for _ in range(200):
        if s.done:
            break
        s.respond(s.level >= true_threshold)
    return s

def test_converges_on_threshold():
    for t in (-70.0, -55.0, -42.0):
        s = run_sim(t)
        assert s.done and abs(s.threshold - t) <= 4.0   # within one fine step

def test_never_escapes_bounds():
    s = Staircase()
    for _ in range(50):
        if s.done:
            break
        assert -90.0 <= s.level <= -10.0
        s.respond(False)                                 # keeps pushing up

def test_threshold_none_until_done():
    s = Staircase()
    assert s.threshold is None

def test_reusable_for_snr_tracking_with_symmetric_steps():
    # Task 5 (Test B) reuses this class for SNR tracking with SYMMETRIC step
    # sizes (step_down == step_up), unlike Test A's asymmetric tone-threshold
    # steps -- symmetric steps converge to the 50%-correct point, which is
    # what a speech-reception-threshold (SRT) is defined as in the published
    # research Test B's referral criterion comes from (see digit_screening.py).
    # Prove the class itself has no dBFS-specific behavior baked in and that
    # symmetric steps work correctly (convergence is still exact, just at a
    # different statistical point than Test A's asymmetric configuration).
    s = Staircase(start_level=0.0, step_down=2.0, step_up=2.0,
                   floor_level=-15.0, ceil_level=10.0, reversals_needed=8)
    for _ in range(300):
        if s.done:
            break
        s.respond(s.level >= -5.0)   # simulated "true SRT" of -5.0 dB SNR
    assert s.done and abs(s.threshold - (-5.0)) <= 2.0

def test_staircase_gives_up_after_two_ceiling_misses():
    # A listener who hears nothing: level climbs to the -10 ceiling, then two
    # consecutive misses AT the ceiling end the point immediately with the
    # ceiling as an "at or beyond" threshold -- no bouncing until 5 reversals.
    s = Staircase(ceiling_give_up=2)
    clicks = 0
    while not s.done:
        s.respond(False)
        clicks += 1
        assert clicks < 50
    assert s.gave_up
    assert s.threshold == -10.0
    # -30 -> ceiling is 5 climbs at step_up=4, then exactly 2 ceiling misses.
    assert clicks == 7

def test_staircase_ceiling_miss_counter_resets_on_heard():
    s = Staircase(start_level=-10.0, ceiling_give_up=2)
    s.respond(False)          # miss #1 at ceiling
    s.respond(True)           # heard -> counter resets
    assert not s.gave_up
    for _ in range(100):
        if s.done:
            break
        s.respond(s.level >= -20.0)   # a real (loud) threshold, converges
    assert s.done and not s.gave_up

def test_staircase_without_give_up_keeps_old_ceiling_behavior():
    s = Staircase()           # give-up off (Test B relies on this default)
    for _ in range(100):
        if s.done:
            break
        s.respond(False)
    assert s.done and not s.gave_up
    assert s.threshold == -10.0       # pinned reversals still converge there

def test_frequency_test_progressive_order():
    # Baseline subset first (its own outward sweep from the 1 kHz anchor),
    # then the detail frequencies -- the single progressive test's
    # informative-points-first ordering.
    ft = FrequencyTest(frequencies=[125, 250, 500, 1000, 2000, 4000])
    order = []
    for _, freq in ft._pairs:
        if freq not in order:
            order.append(freq)
    assert order == [1000, 4000, 250, 500, 2000, 125]
    assert ft.current == ("left", 1000)   # anchor first, easiest tone


def test_progressive_order_full_standard_set():
    order = progressive_order()
    # The five baseline frequencies (the old Quick set) come first...
    assert set(order[:len(COARSE_FREQS)]) == set(COARSE_FREQS)
    assert order[0] == 1000               # anchored
    # ...then every detail frequency, nothing lost or duplicated.
    assert sorted(order) == sorted(STANDARD_FREQS)
    assert len(order) == len(set(order))


def test_baseline_complete_counts_skips_as_resolved():
    raw = {"left": {f: -50.0 for f in COARSE_FREQS},
           "right": {f: -50.0 for f in COARSE_FREQS}}
    raw["left"][16000] = None             # skipped, still resolved
    assert baseline_complete(raw)
    del raw["right"][250]
    assert not baseline_complete(raw)


def test_frequency_test_explicit_pairs_mode():
    # Dashboard mode: an explicit queue (single-point re-test) overrides
    # the frequency sweep entirely.
    ft = FrequencyTest(pairs=[("right", 8000)])
    assert ft.current == ("right", 8000)
    while not ft.done:
        ft.respond(ft.staircase.level >= -60.0)
    assert abs(ft.results["right"][8000] - (-60.0)) <= 4.0
    assert ft.results["left"] == {}


def test_frequency_test_seeds_from_prior_state():
    # Cross-session seeding: a re-test starts near the persisted answer
    # (one coarse step above the nearest/bracketing known thresholds),
    # not from the cold -30 dBFS default.
    seed = {"left": {1000: -62.0, 4000: -58.0}}
    ft = FrequencyTest(pairs=[("left", 2000)], seed_thresholds=seed)
    # Bracketed between 1000 and 4000: interpolation + 8 dB margin.
    assert -60.0 < ft.staircase.level < -48.0


def test_frequency_test_retest_does_not_seed_from_itself():
    # Re-testing a point must not anchor to the very value being questioned.
    seed = {"left": {2000: -20.0}}
    ft = FrequencyTest(pairs=[("left", 2000)], seed_thresholds=seed)
    assert ft.staircase.level == -30.0    # cold default, no self-seed


def test_give_up_boundary_does_not_demote_closer_detail_points():
    # Progressive order visits the 8 kHz baseline extreme BEFORE the 2 kHz
    # detail point; giving up at 8 kHz turns only points beyond 8 kHz into
    # ceiling probes -- the hearable 2 kHz still queued behind it gets a
    # normal seeded staircase (proven by it converging on a threshold well
    # below the -10 dBFS probe start).
    ft = FrequencyTest(frequencies=[1000, 2000, 8000, 16000])
    def hears(ear, freq, level):
        return freq <= 4000 and level >= -55.0
    steps = 0
    while not ft.done and steps < 2000:
        ear, freq = ft.current
        ft.respond(hears(ear, freq, ft.staircase.level))
        steps += 1
    assert ft.done
    for ear in ("left", "right"):
        assert ft.results[ear][8000] == -10.0     # gave up at the ceiling
        assert ft.results[ear][16000] == -10.0    # beyond the boundary: probed
        assert abs(ft.results[ear][2000] - (-55.0)) <= 4.0  # closer in: tested

def test_frequency_test_seeds_next_point_from_neighbour():
    ft = FrequencyTest(frequencies=[1000, 2000])
    # Complete both ears' 1000 Hz points against a -60 dBFS listener.
    while not ft.done and ft.current[1] == 1000:
        ft.respond(ft.staircase.level >= -60.0)
    assert ft.current == ("left", 2000)
    # Seeded one coarse step above the ~-60 neighbour threshold, not -30.
    assert -70.0 < ft.staircase.level < -45.0

def test_frequency_test_give_up_high_probes_higher_freqs_cheaply():
    ft = FrequencyTest(frequencies=[1000, 4000, 8000, 16000])
    # Both ears: normal hearing to 4000 Hz, stone deaf above.
    def hears(ear, freq, level):
        return freq <= 4000 and level >= -55.0
    steps = 0
    probe_clicks = {("left", 16000): 0, ("right", 16000): 0}
    while not ft.done and steps < 2000:
        ear, freq = ft.current
        if (ear, freq) in probe_clicks:
            # The beyond-boundary point must start AT the ceiling (a probe,
            # not a cold -30 start or a seeded level).
            if probe_clicks[(ear, freq)] == 0:
                assert ft.staircase.level == -10.0
            probe_clicks[(ear, freq)] += 1
        ft.respond(hears(ear, freq, ft.staircase.level))
        steps += 1
    assert ft.done
    for ear in ("left", "right"):
        assert ft.results[ear][8000] == -10.0     # gave up at the ceiling
        assert ft.results[ear][16000] == -10.0    # probed: same "at or beyond"
        assert abs(ft.results[ear][4000] - (-55.0)) <= 4.0  # normal points intact
    # A truly inaudible probe costs exactly two clicks, never a full search.
    assert probe_clicks[("left", 16000)] == 2
    assert probe_clicks[("right", 16000)] == 2
    # The gave-up points are recorded AS SUCH, not left to be re-guessed from
    # their threshold value: a ceiling reading alone cannot distinguish
    # "couldn't hear it at all" from "only just heard it at the maximum",
    # and the dashboard renders those two differently.
    assert ft.gave_up_pairs == {("left", 8000), ("right", 8000),
                                ("left", 16000), ("right", 16000)}
    assert ("left", 4000) not in ft.gave_up_pairs


def test_gave_up_pairs_empty_for_a_normal_listener():
    ft = FrequencyTest(frequencies=[1000, 4000])
    while not ft.done:
        ft.respond(ft.staircase.level >= -55.0)
    assert ft.gave_up_pairs == set()
    assert all(v is not None and v < -10.0
               for ear in ("left", "right") for v in ft.results[ear].values())

def test_frequency_test_give_up_low_probes_lower_freqs_cheaply():
    ft = FrequencyTest(frequencies=[125, 250, 500, 1000])
    def hears(ear, freq, level):
        return freq >= 500 and level >= -55.0     # deaf below 500 Hz
    steps = 0
    while not ft.done and steps < 2000:
        ear, freq = ft.current
        ft.respond(hears(ear, freq, ft.staircase.level))
        steps += 1
    assert ft.done
    for ear in ("left", "right"):
        assert ft.results[ear][250] == -10.0
        assert ft.results[ear][125] == -10.0      # probed, confirmed inaudible
        assert abs(ft.results[ear][500] - (-55.0)) <= 4.0

def test_probe_catches_hearable_point_beyond_a_hardware_null():
    # The case the old auto-skip got wrong: a narrow hardware null at
    # 8 kHz (give-up there), but 12 kHz is genuinely hearable -- the probe
    # must fall into a normal descending search and measure it.
    ft = FrequencyTest(frequencies=[1000, 8000, 12000])
    def hears(ear, freq, level):
        if freq == 8000:
            return False                          # dead null at 8 kHz
        return level >= -45.0
    steps = 0
    while not ft.done and steps < 2000:
        ear, freq = ft.current
        ft.respond(hears(ear, freq, ft.staircase.level))
        steps += 1
    assert ft.done
    for ear in ("left", "right"):
        assert ft.results[ear][8000] == -10.0     # the null: at or beyond
        assert ft.results[ear][12000] is not None
        assert abs(ft.results[ear][12000] - (-45.0)) <= 4.0  # measured, not skipped

def test_frequency_test_give_up_on_anchor_skips_whole_ear():
    ft = FrequencyTest(frequencies=[500, 1000, 4000])
    # Left ear hears nothing at all; right ear is normal.
    def hears(ear, freq, level):
        return ear == "right" and level >= -55.0
    steps = 0
    while not ft.done and steps < 2000:
        ear, freq = ft.current
        ft.respond(hears(ear, freq, ft.staircase.level))
        steps += 1
    assert ft.done
    assert ft.results["left"][1000] == -10.0      # anchor: gave up at ceiling
    assert ft.results["left"][4000] is None       # rest of the ear skipped
    assert ft.results["left"][500] is None
    assert abs(ft.results["right"][1000] - (-55.0)) <= 4.0  # other ear unaffected

def test_frequency_test_full_run_and_normalise():
    ft = FrequencyTest(frequencies=[500, 1000, 4000])
    # Simulated listener: right ear is 6 dB above left at 4000 Hz, matched elsewhere.
    true_thresholds = {
        ("left", 500): -60.0, ("right", 500): -60.0,
        ("left", 1000): -55.0, ("right", 1000): -55.0,
        ("left", 4000): -50.0, ("right", 4000): -56.0,
    }
    steps = 0
    while not ft.done and steps < 5000:
        ear, freq = ft.current
        ft.respond(ft.staircase.level >= true_thresholds[(ear, freq)])
        steps += 1
    assert ft.done
    norm = normalise(ft.results)
    assert abs(norm["imbalance"][4000] - 6.0) <= 3.0
    assert abs(norm["imbalance"][500]) < 3.0

def test_normalise_anchors_each_ear_to_its_own_1k():
    results = {"left": {1000: -55.0, 4000: -45.0}, "right": {1000: -55.0, 4000: -55.0}}
    norm = normalise(results)
    assert norm["left"][1000] == 0.0 and norm["right"][1000] == 0.0
    assert abs(norm["left"][4000] - 10.0) < 1e-6      # -45 - (-55) = +10
    assert abs(norm["right"][4000] - 0.0) < 1e-6

def test_imbalance_uses_raw_values_not_normalised_curves():
    # The two ears have DIFFERENT raw 1kHz readings (-50 vs -55, a real 5 dB
    # whole-ear offset) -- this is the disambiguating case. If imbalance were
    # (wrongly) computed from the anchor-normalised curves, BOTH ears' 1kHz
    # would read 0.0 by construction and the real 5 dB offset would vanish
    # entirely, including at every other frequency that shares the same
    # constant offset. Computed correctly from raw values, the offset must
    # show up at 1000 Hz itself, not just elsewhere.
    results = {"left": {1000: -50.0, 4000: -50.0}, "right": {1000: -55.0, 4000: -55.0}}
    norm = normalise(results)
    assert norm["left"][1000] == 0.0 and norm["right"][1000] == 0.0   # per-ear curves: still self-anchored
    assert abs(norm["imbalance"][1000] - 5.0) < 1e-6    # -50 - (-55) = +5, NOT 0.0
    assert abs(norm["imbalance"][4000] - 5.0) < 1e-6    # same constant offset carries through

def test_normalise_tolerates_skipped_frequency():
    results = {"left": {1000: -55.0, 16000: None}, "right": {1000: -55.0, 16000: -40.0}}
    norm = normalise(results)
    assert 16000 not in norm["imbalance"]              # can't compare, one side skipped

def test_normalise_handles_missing_1k_anchor_without_raising():
    # Should not normally happen (only 16 kHz is UI-skippable), but guard it:
    # a missing 1 kHz anchor must not raise, and that ear's normalised curve
    # comes back empty rather than producing garbage relative values.
    results = {"left": {1000: None, 4000: -50.0}, "right": {1000: -55.0, 4000: -55.0}}
    norm = normalise(results)
    assert norm["left"] == {}
    assert 4000 not in norm["imbalance"] and 1000 not in norm["imbalance"]

def test_frequency_flags_flags_outliers_relative_to_own_test():
    results = {"left": {1000: -55.0, 4000: -55.0, 8000: -30.0}, "right": {1000: -55.0, 4000: -55.0, 8000: -54.0}}
    norm = normalise(results)
    flags = frequency_flags(norm, threshold_db=6.0)
    assert 8000 in flags and 4000 not in flags and 1000 not in flags

def test_summarise_mentions_imbalanced_ear_and_frequency():
    norm = {"left": {1000: 0.0, 4000: -6.0}, "right": {1000: 0.0, 4000: 0.0},
            "imbalance": {1000: 0.0, 4000: -6.0}}
    text = summarise(norm)
    assert "left" in text.lower() and "4" in text

def test_summarise_reports_closely_matched():
    norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {1000: 1.0}}
    assert "closely matched" in summarise(norm).lower()

def test_export_text_contains_disclaimer_table_and_ai_instruction():
    norm = {"left": {1000: 0.0, 4000: -3.0}, "right": {1000: 0.0, 4000: 0.0},
            "imbalance": {1000: 0.0, 4000: -3.0}}
    raw = {"left": {1000: -55.0, 4000: -58.0}, "right": {1000: -55.0, 4000: -55.0}}
    text = export_text(norm, raw, device_label="Test Speakers")
    assert "not a medical evaluation" in text
    assert "Test Speakers" in text
    assert "1000" in text and "4000" in text
    assert "+6 dB" in text

def test_save_and_load_results_round_trip(tmp_path):
    norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {1000: 0.0}}
    raw = {"left": {1000: -55.0}, "right": {1000: -55.0}}
    save_results(norm, raw, tmp_path)
    save_results(norm, raw, tmp_path)
    all_results = load_all_results(tmp_path)
    assert len(all_results) == 2
    assert all_results[0]["norm"]["left"][1000] == 0.0

def test_save_and_load_results_round_trip_with_volume_reference(tmp_path):
    norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {1000: 0.0}}
    raw = {"left": {1000: -55.0}, "right": {1000: -55.0}}
    ref = {"windows_volume_percent": 42.0, "windows_volume_db": -12.5,
           "gear_note": "amp at 2 o'clock", "captured_at": "2026-07-10"}
    save_results(norm, raw, tmp_path, volume_reference=ref)
    all_results = load_all_results(tmp_path)
    assert all_results[0]["volume_reference"] == ref

def test_export_text_includes_volume_reference_and_scope_caveat_when_given():
    norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {1000: 0.0}}
    raw = {"left": {1000: -55.0}, "right": {1000: -55.0}}
    ref = {"windows_volume_percent": 42.0, "windows_volume_db": -12.5,
           "gear_note": "amp at 2 o'clock", "captured_at": "2026-07-10"}
    text = export_text(norm, raw, "Test Speakers", volume_reference=ref)
    assert "42%" in text
    assert "amp at 2 o'clock" in text
    assert "does not transfer to a different device" in text

def test_export_text_reports_no_response_points_the_math_dropped():
    """The dead band is the most important thing on the page. It is excluded
    from the tables and the imbalance (it is a lower bound, not a threshold),
    so the export has to say so explicitly rather than let it vanish -- or,
    worse, label it "skipped" as though the listener had declined to test it."""
    # Left never heard 8 kHz (censored -> None); right heard it at -50 dB. Both
    # ears anchor at 1 kHz, so only 1 kHz has a computable imbalance.
    raw = {"left": {1000: -55.0, 8000: None}, "right": {1000: -55.0, 8000: -50.0}}
    norm = normalise(raw)
    assert 8000 not in norm["imbalance"]  # the fixture is the real shape
    text = export_text(norm, raw, "Test Speakers",
                       no_response={"left": [8000], "right": []})
    assert "No response" in text
    assert "left | 8000" in text
    assert "above the test ceiling, not at it" in text
    # ...and the per-ear table must not call it "skipped" -- the listener DID
    # test it and heard nothing. That would contradict the section above.
    assert "8000 | no response" in text
    assert "8000 | skipped" not in text

def test_export_text_omits_no_response_section_when_every_point_was_heard():
    norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {1000: 0.0}}
    raw = {"left": {1000: -55.0}, "right": {1000: -55.0}}
    assert "No response" not in export_text(norm, raw, "Test Speakers")
    assert "No response" not in export_text(norm, raw, "Test Speakers",
                                            no_response={"left": [], "right": []})

def test_export_text_omits_volume_reference_section_when_not_given():
    norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {1000: 0.0}}
    raw = {"left": {1000: -55.0}, "right": {1000: -55.0}}
    text = export_text(norm, raw, "Test Speakers")
    assert "Volume reference" not in text

def test_volume_drift_note_blank_with_no_prior_run():
    current = {"windows_volume_db": -20.0, "windows_volume_percent": 50.0}
    assert volume_drift_note(current, None) == ""

def test_volume_drift_note_blank_when_close():
    current = {"windows_volume_db": -20.0, "windows_volume_percent": 50.0}
    prior = {"windows_volume_db": -22.0, "windows_volume_percent": 48.0}
    assert volume_drift_note(current, prior) == ""

def test_volume_drift_note_blank_when_reading_unavailable():
    current = {"windows_volume_db": None, "windows_volume_percent": None}
    prior = {"windows_volume_db": -20.0, "windows_volume_percent": 50.0}
    assert volume_drift_note(current, prior) == ""

def test_volume_drift_note_fires_when_meaningfully_louder():
    current = {"windows_volume_db": -10.0, "windows_volume_percent": 70.0}
    prior = {"windows_volume_db": -25.0, "windows_volume_percent": 40.0}
    note = volume_drift_note(current, prior)
    assert "louder" in note
    assert "40%" in note and "70%" in note

def test_volume_drift_note_fires_when_meaningfully_quieter():
    current = {"windows_volume_db": -30.0, "windows_volume_percent": 20.0}
    prior = {"windows_volume_db": -10.0, "windows_volume_percent": 70.0}
    note = volume_drift_note(current, prior)
    assert "quieter" in note

# Task 3 tests: BypassGuard + present_tone

from rumble.core.hearing_test import BypassGuard, present_tone


class _FakeManager:
    def __init__(self, initially_bypassed=False):
        self._bypassed = initially_bypassed
        self.calls = []

    def is_bypassed(self):
        return self._bypassed

    def set_bypass(self, value):
        self.calls.append(value)
        self._bypassed = value


def test_bypass_guard_bypasses_then_restores():
    mgr = _FakeManager(initially_bypassed=False)
    with BypassGuard(mgr):
        assert mgr.is_bypassed() is True
    assert mgr.is_bypassed() is False
    assert mgr.calls == [True, False]

def test_bypass_guard_restores_already_bypassed_state():
    mgr = _FakeManager(initially_bypassed=True)
    with BypassGuard(mgr):
        assert mgr.is_bypassed() is True
    assert mgr.is_bypassed() is True   # restored to True, not force-cleared

def test_bypass_guard_restores_on_exception():
    mgr = _FakeManager(initially_bypassed=False)
    try:
        with BypassGuard(mgr):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert mgr.is_bypassed() is False

def test_present_tone_builds_and_plays_correct_channel():
    played = {}
    class _FakeEngine:
        def play(self, buffer, sample_rate, device=None, on_done=None):
            played["buffer"] = buffer
            played["sample_rate"] = sample_rate
        @property
        def is_playing(self):
            return False
    present_tone(_FakeEngine(), freq=1000, level_dbfs=-30.0, ear="left", sample_rate=48000, duration_s=0.2)
    buf = played["buffer"]
    assert buf.shape[1] == 2
    assert (buf[:, 1] == 0).all()          # right channel silent
    assert buf[:, 0].max() > 0             # left channel carries the tone


def test_load_volume_reference_returns_stereo_buffer():
    buf, sample_rate = load_volume_reference()
    assert buf.shape[1] == 2
    assert sample_rate > 0
    assert buf.shape[0] > 0
    # Duplicated to both channels, per the function's own contract.
    assert (buf[:, 0] == buf[:, 1]).all()


def test_reference_sounds_every_key_loads_as_stereo_buffer():
    assert len(REFERENCE_SOUNDS) > 0
    for key, label in REFERENCE_SOUNDS:
        assert label  # every entry has a real display label
        buf, sample_rate = load_reference_sound(key)
        assert buf.shape[1] == 2
        assert sample_rate > 0
        assert buf.shape[0] > 0
        assert (buf[:, 0] == buf[:, 1]).all()


def test_load_reference_sound_unknown_key_raises():
    try:
        load_reference_sound("not_a_real_key")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError for an unknown reference-sound key")


def test_export_text_includes_measured_spl_when_present():
    norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {}}
    raw = {"left": {1000: -40.0}, "right": {1000: -40.0}}
    text = export_text(norm, raw, "Test Device",
                       volume_reference={"windows_volume_percent": 30.0,
                                         "windows_volume_db": -40.0,
                                         "gear_note": "",
                                         "measured_spl_db": 74.3,
                                         "captured_at": "2026-07-16"})
    assert "74.3 dB SPL" in text


def test_export_text_omits_measured_spl_line_when_not_given():
    norm = {"left": {1000: 0.0}, "right": {1000: 0.0}, "imbalance": {}}
    raw = {"left": {1000: -40.0}, "right": {1000: -40.0}}
    text = export_text(norm, raw, "Test Device",
                       volume_reference={"windows_volume_percent": 30.0,
                                         "windows_volume_db": -40.0,
                                         "gear_note": "",
                                         "measured_spl_db": None,
                                         "captured_at": "2026-07-16"})
    assert "dB SPL" not in text


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
