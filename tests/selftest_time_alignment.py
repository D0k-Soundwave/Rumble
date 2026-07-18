import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from rumble.core import time_alignment
from rumble.core.audio import tones

SR = 48000


def _sweep():
    return tones.generate_log_sweep(100.0, 8000.0, 0.2, -12.0, SR, fade_ms=5.0)


def _padded_capture(sweep, lead_in, trail=2000):
    total = lead_in + len(sweep) + trail
    buf = np.zeros(total, dtype=np.float64)
    buf[lead_in:lead_in + len(sweep)] = sweep
    return buf


def test_detect_offset_recovers_known_delay():
    sweep = _sweep()
    lead_in = 4000
    captured = _padded_capture(sweep, lead_in)
    offset = time_alignment.detect_offset_samples(sweep, captured)
    assert offset == lead_in


def test_detect_offset_recovers_known_delay_at_44100():
    sr = 44100
    sweep = tones.generate_log_sweep(100.0, 8000.0, 0.2, -12.0, sr, fade_ms=5.0)
    lead_in = 9123
    captured = _padded_capture(sweep, lead_in)
    offset = time_alignment.detect_offset_samples(sweep, captured)
    assert offset == lead_in


def test_detect_offset_robust_to_noise():
    sweep = _sweep()
    lead_in = 7777
    total = lead_in + len(sweep) + 3000
    rng = np.random.default_rng(1)
    captured = 0.02 * rng.standard_normal(total)
    captured[lead_in:lead_in + len(sweep)] += sweep
    offset = time_alignment.detect_offset_samples(sweep, captured)
    assert offset == lead_in


def test_detect_offset_respects_max_offset_cap():
    sweep = _sweep()
    lead_in = 30000
    captured = _padded_capture(sweep, lead_in)
    # Cap the search well short of the true offset -- the true peak must be
    # excluded, so the result must differ from the uncapped answer.
    offset = time_alignment.detect_offset_samples(sweep, captured, max_offset_samples=1000)
    assert offset != lead_in
    assert 0 <= offset <= 1000


def test_measure_channel_delays_rebases_to_latest():
    # APO's Delay: line can only ADD delay, so the latest-arriving channel
    # is the 0 ms reference and earlier channels are pushed back to meet
    # it: applying these values must converge all arrivals, not widen them.
    sweep = _sweep()
    captures = {
        "L": _padded_capture(sweep, 3000),
        "R": _padded_capture(sweep, 3000 + 480),   # +10ms @ 48kHz
        "LFE": _padded_capture(sweep, 3000 + 960),  # +20ms @ 48kHz
    }
    delays = time_alignment.measure_channel_delays(captures, sweep, SR)
    assert delays["LFE"] == 0.0
    assert abs(delays["R"] - 10.0) < 0.2
    assert abs(delays["L"] - 20.0) < 0.2


def test_measure_channel_delays_applying_result_aligns_arrivals():
    # End-to-end invariant of the whole feature: measured arrival offset
    # plus the returned Delay: value must be the same for every channel.
    sweep = _sweep()
    arrivals = {"L": 5000, "R": 1000, "LFE": 2500}
    captures = {ch: _padded_capture(sweep, n) for ch, n in arrivals.items()}
    delays = time_alignment.measure_channel_delays(captures, sweep, SR)
    corrected = {
        ch: arrivals[ch] / SR * 1000.0 + delays[ch]
        for ch in arrivals
    }
    values = list(corrected.values())
    assert max(values) - min(values) < 0.2


def test_measure_channel_delays_never_negative():
    sweep = _sweep()
    captures = {
        "L": _padded_capture(sweep, 5000),
        "R": _padded_capture(sweep, 1000),  # R arrives earliest
    }
    delays = time_alignment.measure_channel_delays(captures, sweep, SR)
    assert all(v >= 0.0 for v in delays.values())
    assert delays["L"] == 0.0
    assert delays["R"] > 0.0


def test_build_channel_sweep_places_energy_in_target_channel_only():
    sweep = _sweep()
    buf = time_alignment.build_channel_sweep(sweep, 2, 4)
    assert buf.shape == (len(sweep), 4)
    assert buf.dtype == np.float32
    for ch in (0, 1, 3):
        assert np.allclose(buf[:, ch], 0.0)
    assert np.allclose(buf[:, 2], sweep.astype(np.float32))


def test_build_channel_sweep_rejects_out_of_range_index():
    sweep = _sweep()
    raised = None
    try:
        time_alignment.build_channel_sweep(sweep, 4, 4)
    except ValueError as exc:
        raised = exc
    assert raised is not None


def test_standard_channel_order_matches_known_layouts():
    # Sanity-anchor against editor_panel.py's own 7.1 preset list, which is
    # this same canonical physical-channel order under a different name.
    assert time_alignment.STANDARD_CHANNEL_ORDER == ["L", "R", "C", "LFE", "RL", "RR", "SL", "SR"]


class _FakeToneEngine:
    def __init__(self):
        self.is_playing = False
        self.played = []

    def play(self, buffer, sample_rate, device=None, on_done=None):
        self.played.append((buffer.copy(), sample_rate, device))
        self.is_playing = False


def test_run_measurement_routes_each_channel_and_recovers_delays():
    sr = 8000  # small rate keeps the synthetic test fast
    names = ["L", "R"]
    true_offsets_samples = {"L": 200, "R": 200 + 80}  # R 10ms behind L
    call_count = {"n": 0}

    def fake_record(duration_s, sample_rate, device):
        # The record thread is started before play() is called (it must
        # already be "listening" when playback begins), so wait for the
        # matching play() call to land before reading which channel it
        # routed energy into.
        idx = call_count["n"]
        call_count["n"] += 1
        while len(engine.played) <= idx:
            time.sleep(0.001)
        buf, played_sr, played_device = engine.played[idx]
        channel_index = int(np.argmax(np.abs(buf).sum(axis=0)))
        name = names[channel_index]
        n = int(round(duration_s * sample_rate))
        out = np.zeros(n, dtype=np.float64)
        sweep_col = buf[:, channel_index].astype(np.float64)
        start = true_offsets_samples[name]
        end = min(n, start + len(sweep_col))
        out[start:end] = sweep_col[: end - start]
        return out

    engine = _FakeToneEngine()
    delays = time_alignment.run_measurement(
        names, engine, sr,
        duration_s=0.1, lead_in_s=0.01, trail_s=0.01,
        record_factory=fake_record,
    )

    assert len(engine.played) == 2
    for i, (buf, played_sr, device) in enumerate(engine.played):
        assert buf.shape[1] == 2
        assert played_sr == sr
        nonzero_cols = [c for c in range(2) if np.any(buf[:, c] != 0)]
        assert nonzero_cols == [i]

    # R arrives 10ms later, so R is the latest-arrival reference (0 ms)
    # and L must be delayed 10ms to meet it.
    assert delays["R"] == 0.0
    assert abs(delays["L"] - 10.0) < 1.0


def test_run_measurement_raises_on_recording_failure():
    def failing_record(duration_s, sample_rate, device):
        raise RuntimeError("no input device")

    engine = _FakeToneEngine()
    raised = None
    try:
        time_alignment.run_measurement(
            ["L"], engine, 8000, duration_s=0.05, lead_in_s=0.01, trail_s=0.01,
            record_factory=failing_record,
        )
    except time_alignment.MeasurementError as exc:
        raised = exc
    assert raised is not None
    assert "no input device" in str(raised)


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
