import sys
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import sounddevice as sd
from rumble.core.audio import tones
from rumble.core.audio import metering
from rumble.core.audio import engine
from rumble.core.audio import capture

FS = 48000

def test_sine_level_accuracy():
    buf = tones.generate_sine(1000, 1.0, -20.0, FS, channel="both", fade_ms=0)
    rms = np.sqrt((buf[:, 0] ** 2).mean())
    assert abs(20 * np.log10(rms) - (-20.0 - 3.0103)) < 0.1   # sine RMS = peak − 3.01 dB

def test_sine_channel_routing():
    left = tones.generate_sine(1000, 0.1, -20, FS, channel="left")
    assert left[:, 1].max() == 0.0 and abs(left[:, 0]).max() > 0
    right = tones.generate_sine(1000, 0.1, -20, FS, channel="right")
    assert right[:, 0].max() == 0.0 and abs(right[:, 1]).max() > 0

def test_fade_removes_clicks():
    buf = tones.generate_sine(1000, 0.5, -20, FS, fade_ms=10)
    n_fade = int(0.010 * FS)
    assert abs(buf[0, 0]) < 1e-4 and abs(buf[-1, 0]) < 1e-4
    assert abs(buf[:n_fade, 0]).max() < abs(buf[n_fade:2*n_fade, 0]).max()

def test_pink_noise_level_and_shape():
    buf = tones.generate_pink_noise(2.0, -20.0, FS, seed=42)
    rms_db = 20 * np.log10(np.sqrt((buf[:, 0] ** 2).mean()))
    assert abs(rms_db - (-20.0)) < 0.5
    assert buf.dtype == np.float32 and buf.shape == (2 * FS, 2)

def test_log_sweep_level_shape_and_fades():
    buf = tones.generate_log_sweep(100.0, 8000.0, 0.5, -20.0, FS, fade_ms=10.0)
    assert buf.dtype == np.float32
    assert buf.shape == (int(0.5 * FS),)
    peak = float(np.abs(buf).max())
    assert abs(20 * np.log10(peak) - (-20.0)) < 0.2   # calibrated peak level
    n_fade = int(0.010 * FS)
    assert abs(buf[0]) < 1e-4 and abs(buf[-1]) < 1e-4
    assert abs(buf[:n_fade]).max() < abs(buf[n_fade:2 * n_fade]).max()


def test_log_sweep_frequency_rises_from_start_to_end():
    # Zero-crossing rate near the start should be far lower than near the
    # end for an exponential sweep from 100 Hz to 8000 Hz.
    buf = tones.generate_log_sweep(100.0, 8000.0, 1.0, -12.0, FS, fade_ms=0)
    window = FS // 20  # 50 ms windows

    def zero_crossings(segment):
        return int(np.sum(np.diff(np.sign(segment)) != 0))

    start_crossings = zero_crossings(buf[:window])
    end_crossings = zero_crossings(buf[-window:])
    assert end_crossings > start_crossings * 5


def test_log_sweep_rejects_invalid_range():
    raised = None
    try:
        tones.generate_log_sweep(1000.0, 500.0, 1.0, -12.0, FS)
    except ValueError as exc:
        raised = exc
    assert raised is not None


def test_metering_sine_rms_and_peak():
    # -20 dBFS full-scale sine: peak amplitude calibrated to 10**(-20/20).
    buf = tones.generate_sine(1000, 1.0, -20.0, FS, channel="left", fade_ms=0)
    mono = buf[:, 0]
    rms = metering.rms_dbfs(mono)
    peak = metering.peak_dbfs(mono)
    assert abs(rms - (-23.0103)) < 0.1
    assert abs(peak - (-20.0)) < 0.1


def test_metering_silence_floors_without_error():
    silence = np.zeros(4096, dtype=np.float32)
    rms = metering.rms_dbfs(silence)
    peak = metering.peak_dbfs(silence)
    assert rms <= -120.0 and not np.isnan(rms) and np.isfinite(rms)
    assert peak <= -120.0 and not np.isnan(peak) and np.isfinite(peak)

    # Stereo all-zero block too.
    silence_stereo = np.zeros((4096, 2), dtype=np.float32)
    assert metering.rms_dbfs(silence_stereo) <= -120.0
    assert metering.peak_dbfs(silence_stereo) <= -120.0


def test_metering_rms_peak_stereo_shape():
    buf = tones.generate_sine(1000, 1.0, -20.0, FS, channel="both", fade_ms=0)
    # Stereo (n, 2) input must not raise and should agree with the mono
    # column (both channels carry the identical tone here).
    rms_stereo = metering.rms_dbfs(buf)
    rms_mono = metering.rms_dbfs(buf[:, 0])
    assert abs(rms_stereo - rms_mono) < 0.01


def test_spectrum_mags_bin_and_level():
    # Sample rate/n_fft chosen so 1 kHz lands exactly on an FFT bin
    # (bin width = 51200 / 2048 = 25 Hz; 1000 Hz = bin 40 exactly), removing
    # spectral leakage/scalloping as a confound for the tolerance check.
    sr = 51200
    n_fft = 2048
    level_dbfs = -6.0
    amplitude = tones.dbfs_to_amplitude(level_dbfs)
    t = np.arange(n_fft, dtype=np.float64) / sr
    mono = (amplitude * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)

    freqs, mags_db = metering.spectrum_mags(mono, sr, n_fft=n_fft)

    bin_width = sr / n_fft
    peak_bin = int(np.argmax(mags_db))
    assert abs(freqs[peak_bin] - 1000.0) <= bin_width
    assert abs(mags_db[peak_bin] - level_dbfs) < 0.5


def test_spectrum_mags_silence_floors_without_error():
    silence = np.zeros(2048, dtype=np.float32)
    freqs, mags_db = metering.spectrum_mags(silence, FS, n_fft=2048)
    assert np.all(mags_db <= -120.0)
    assert np.all(np.isfinite(mags_db))
    assert freqs.shape == mags_db.shape == (2048 // 2 + 1,)


class FakeStream:
    """Stand-in for sounddevice.OutputStream.

    Records every block the ToneEngine callback writes and lets a test drive
    the callback by hand (`.feed()`), simulating PortAudio calling back on
    its own thread until the callback raises sd.CallbackStop -- at which
    point, like real sounddevice, the stream's finished_callback fires.
    """

    def __init__(self, samplerate, channels, device, callback, finished_callback,
                 blocksize=512):
        self.samplerate = samplerate
        self.channels = channels
        self.device = device
        self.callback = callback
        self.finished_callback = finished_callback
        self.blocksize = blocksize
        self.started = False
        self.stopped = False
        self.closed = False
        self.written = []

    def start(self):
        self.started = True

    def stop(self, ignore_errors=True):
        self.stopped = True

    def close(self):
        self.closed = True

    def feed(self, n_calls=None):
        """Drive the callback like PortAudio would.

        Calls the callback repeatedly (each time with a fresh zeroed block),
        recording what it writes, until either `n_calls` calls have happened
        or the callback raises CallbackStop (drained) -- at which point the
        finished_callback is invoked, exactly like a real stream reaching
        its natural end.
        """
        calls = 0
        while n_calls is None or calls < n_calls:
            outdata = np.zeros((self.blocksize, self.channels), dtype=np.float32)
            try:
                self.callback(outdata, self.blocksize, None, 0)
            except sd.CallbackStop:
                self.written.append(outdata.copy())
                if self.finished_callback:
                    self.finished_callback()
                return
            self.written.append(outdata.copy())
            calls += 1


def test_engine_play_feeds_buffer_and_calls_on_done():
    buf = tones.generate_sine(1000, 0.05, -20.0, FS, channel="both", fade_ms=0)
    fakes = []

    def factory(samplerate, channels, device, callback, finished_callback):
        fake = FakeStream(samplerate, channels, device, callback, finished_callback)
        fakes.append(fake)
        return fake

    eng = engine.ToneEngine(stream_factory=factory)
    done_calls = []
    eng.play(buf, FS, device=None, on_done=lambda: done_calls.append(True))

    assert len(fakes) == 1
    fake = fakes[0]
    assert fake.started
    assert eng.is_playing is True

    fake.feed()  # drive to natural completion

    written = np.concatenate(fake.written, axis=0)
    n = len(buf)
    assert np.allclose(written[:n], buf)
    assert np.all(written[n:] == 0.0)   # tail beyond the buffer is zero-padded
    assert done_calls == [True]
    assert eng.is_playing is False


def test_engine_stop_mid_play_is_clean():
    buf = tones.generate_sine(1000, 1.0, -20.0, FS, channel="both", fade_ms=0)
    fakes = []

    def factory(samplerate, channels, device, callback, finished_callback):
        fake = FakeStream(samplerate, channels, device, callback, finished_callback)
        fakes.append(fake)
        return fake

    eng = engine.ToneEngine(stream_factory=factory)
    eng.play(buf, FS)
    fake = fakes[0]

    fake.feed(n_calls=3)   # a few blocks in, buffer still far from drained
    assert eng.is_playing is True

    eng.stop()   # must not raise
    assert eng.is_playing is False
    assert fake.stopped is True
    assert fake.closed is True

    # Calling stop() again (already stopped) must also be a clean no-op.
    eng.stop()
    assert eng.is_playing is False


def test_engine_second_play_stops_first():
    buf1 = tones.generate_sine(500, 1.0, -20.0, FS, fade_ms=0)
    buf2 = tones.generate_sine(2000, 0.05, -20.0, FS, fade_ms=0)
    fakes = []

    def factory(samplerate, channels, device, callback, finished_callback):
        fake = FakeStream(samplerate, channels, device, callback, finished_callback)
        fakes.append(fake)
        return fake

    eng = engine.ToneEngine(stream_factory=factory)
    eng.play(buf1, FS)
    first = fakes[0]
    assert first.started and not first.stopped

    eng.play(buf2, FS)
    assert len(fakes) == 2
    second = fakes[1]

    # First stream must be stopped/closed before the second ever starts.
    assert first.stopped is True
    assert first.closed is True
    assert second.started is True
    assert second.stopped is False
    assert eng.is_playing is True


def test_engine_play_raises_audio_error_on_exclusive_device():
    def factory(samplerate, channels, device, callback, finished_callback):
        raise sd.PortAudioError("Error opening OutputStream: Device unavailable", -9996)

    eng = engine.ToneEngine(stream_factory=factory)
    buf = tones.generate_sine(1000, 0.05, -20.0, FS, fade_ms=0)

    raised = None
    try:
        eng.play(buf, FS)
    except engine.AudioError as exc:
        raised = exc

    assert raised is not None
    assert raised.kind == "exclusive"
    assert eng.is_playing is False


def test_find_output_device_none_means_default():
    assert engine.find_output_device(None) is None


def test_find_output_device_matches_by_fragment():
    devices = sd.query_devices()
    output_devices = [(i, d) for i, d in enumerate(devices) if d["max_output_channels"] > 0]
    assert output_devices, "test host must have at least one output device"
    index, info = output_devices[0]
    fragment = info["name"].split()[0]   # first word is enough to match
    found = engine.find_output_device(fragment)
    assert found is not None
    assert devices[found]["name"] == info["name"] or fragment.lower() in devices[found]["name"].lower()


def test_find_output_device_no_match_falls_back_to_default():
    assert engine.find_output_device("no-such-device-xyz-zzz") is None


class FakeCaptureStream:
    """Stand-in for a LoopbackCapture stream_factory's returned object.

    Yields canned (n, channels) float32 blocks one at a time from `.read()`;
    once exhausted, `.read()` raises to simulate the device dying / the
    stream being closed out from under the capture thread.
    """

    def __init__(self, blocks, sample_rate=48000, channels=2):
        self._blocks = list(blocks)
        self._idx = 0
        self.sample_rate = sample_rate
        self.channels = channels
        self.closed = False

    def read(self, n_frames):
        if self._idx >= len(self._blocks):
            raise RuntimeError("simulated device death: stream exhausted")
        block = self._blocks[self._idx]
        self._idx += 1
        return block

    def close(self):
        self.closed = True


def _make_blocks(n_blocks, n_frames=256, channels=2, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.standard_normal((n_frames, channels)).astype(np.float32) for _ in range(n_blocks)]


def test_loopback_capture_delivers_blocks_with_sample_rate():
    blocks = _make_blocks(3)
    received = []
    got_all = threading.Event()

    def on_block(block, sample_rate):
        received.append((block, sample_rate))
        if len(received) >= len(blocks):
            got_all.set()

    def factory(device_name_fragment):
        return FakeCaptureStream(blocks, sample_rate=44100, channels=2)

    cap = capture.LoopbackCapture(on_block, stream_factory=factory, retry_interval=0.01)
    cap.start()
    assert cap.status == "running"
    assert got_all.wait(timeout=2.0), "on_block never received the expected blocks"
    cap.stop()

    assert cap.status == "stopped"
    for block, sample_rate in received[:len(blocks)]:
        assert sample_rate == 44100
    for (block, _), expected in zip(received[:len(blocks)], blocks):
        assert np.array_equal(block, expected)


def test_loopback_capture_unavailable_device_does_not_raise_or_spin():
    def factory(device_name_fragment):
        raise RuntimeError("device is exclusively locked by another application")

    cap = capture.LoopbackCapture(lambda block, sr: None, stream_factory=factory, retry_interval=0.01)
    cap.start()  # must not raise

    assert cap.status == "unavailable"
    # No background thread should have been spawned to retry forever.
    assert cap._thread is None

    cap.stop()  # still safe/no-op
    assert cap.status == "stopped"


def test_loopback_capture_reconnects_after_stream_dies():
    blocks = _make_blocks(3, seed=1)
    received = []
    recovered = threading.Event()
    open_attempts = {"count": 0}
    FAILURES_BEFORE_SUCCESS = 3

    def on_block(block, sample_rate):
        received.append((block, sample_rate))
        if len(received) >= len(blocks) + 1:  # at least one block after recovery
            recovered.set()

    def factory(device_name_fragment):
        open_attempts["count"] += 1
        if open_attempts["count"] == 1:
            # First open succeeds; this stream dies after `blocks` are read.
            return FakeCaptureStream(blocks, sample_rate=48000, channels=2)
        if open_attempts["count"] <= 1 + FAILURES_BEFORE_SUCCESS:
            # Simulate the device staying unavailable for a few retries.
            raise RuntimeError("device still unavailable")
        # Recovery: a fresh stream that will deliver more blocks.
        return FakeCaptureStream(_make_blocks(2, seed=2), sample_rate=48000, channels=2)

    cap = capture.LoopbackCapture(on_block, stream_factory=factory, retry_interval=0.01)
    cap.start()
    assert cap.status == "running"

    assert recovered.wait(timeout=5.0), "capture never recovered blocks after reconnect"
    # Status must have stayed meaningful (never "unavailable") across a
    # reconnect from an already-running stream.
    assert cap.status == "running"
    assert open_attempts["count"] >= 2 + FAILURES_BEFORE_SUCCESS

    cap.stop()
    assert cap.status == "stopped"


def test_loopback_capture_stop_is_clean_and_idempotent():
    def factory(device_name_fragment):
        # A stream that always has more (trivial) blocks -- keeps the
        # capture thread alive/busy until .stop() is called.
        return FakeCaptureStream(_make_blocks(10_000, n_frames=8, seed=3), sample_rate=48000, channels=2)

    cap = capture.LoopbackCapture(lambda block, sr: None, stream_factory=factory, retry_interval=0.01)
    cap.start()
    assert cap.status == "running"
    thread = cap._thread
    assert thread is not None and thread.is_alive()

    cap.stop()  # must not raise
    assert cap.status == "stopped"
    assert not thread.is_alive()

    cap.stop()  # calling twice must also be a clean no-op
    assert cap.status == "stopped"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"): f(); print("PASS", n)
