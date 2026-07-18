import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import sounddevice as sd
from rumble.core.audio import metering
from rumble.core.audio import mic_capture

FS = 48000


class FakeInputStream:
    """Stand-in for sounddevice.InputStream.

    Mirrors selftest_audio_engine.py's FakeStream: a test drives the
    callback by hand (`.feed()`), simulating PortAudio calling back on its
    own thread with canned blocks until the callback raises
    sd.CallbackStop, at which point the finished_callback fires (matching
    real sounddevice behavior).
    """

    def __init__(self, samplerate, channels, device, callback, finished_callback):
        self.samplerate = samplerate
        self.channels = channels
        self.device = device
        self.callback = callback
        self.finished_callback = finished_callback
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self, ignore_errors=True):
        self.stopped = True

    def close(self):
        self.closed = True

    def feed(self, blocks):
        """Drive the callback with a sequence of (frames, channels) blocks,
        like PortAudio delivering real input. Stops early if the callback
        raises CallbackStop (recording drained), firing finished_callback.
        """
        for block in blocks:
            try:
                self.callback(block, block.shape[0], None, 0)
            except sd.CallbackStop:
                if self.finished_callback:
                    self.finished_callback()
                return


def _mono_block(n_frames, amplitude=0.1, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal((n_frames, 1)) * amplitude).astype(np.float32)


def test_list_input_devices_shapes_from_query_devices(monkeypatch):
    fake_devices = [
        {"name": "Line In", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "USB Mic", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "Webcam Mic", "max_input_channels": 2, "max_output_channels": 0},
    ]
    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd.default, "device", [2, 0])

    devices = mic_capture.list_input_devices()

    assert [d.index for d in devices] == [1, 2]
    assert [d.name for d in devices] == ["USB Mic", "Webcam Mic"]
    assert devices[0].is_default is False
    assert devices[1].is_default is True


def test_list_input_devices_no_default_marks_none_default(monkeypatch):
    fake_devices = [{"name": "USB Mic", "max_input_channels": 1, "max_output_channels": 0}]
    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)

    class _RaisingDefault:
        @property
        def device(self):
            raise RuntimeError("no default device configured")

    monkeypatch.setattr(sd, "default", _RaisingDefault())

    devices = mic_capture.list_input_devices()
    assert devices[0].is_default is False


def test_recorder_records_right_length_and_shape():
    duration_s = 0.1
    n_samples = int(round(duration_s * FS))
    blocks = [_mono_block(256, amplitude=0.1, seed=i) for i in range((n_samples // 256) + 2)]

    fakes = []

    def factory(samplerate, channels, device, callback, finished_callback):
        assert channels == 1
        fake = FakeInputStream(samplerate, channels, device, callback, finished_callback)
        fakes.append(fake)
        return fake

    rec = mic_capture.MicRecorder(stream_factory=factory)
    done = []
    rec.record(duration_s, FS, device=None, on_done=lambda: done.append(True))

    assert len(fakes) == 1
    assert fakes[0].started
    assert rec.is_recording is True

    fakes[0].feed(blocks)

    assert done == [True]
    assert rec.is_recording is False
    buf = rec.buffer
    assert buf is not None
    assert buf.dtype == np.float32
    assert buf.shape == (n_samples,)


def test_recorder_clip_detection_triggers_at_full_scale_not_minus_1_dbfs():
    n_samples = 512
    duration_s = n_samples / FS

    # A block that touches exactly full scale (1.0) must trigger clipping.
    clipping_block = np.zeros((n_samples, 1), dtype=np.float32)
    clipping_block[10, 0] = 1.0

    fakes = []

    def factory(samplerate, channels, device, callback, finished_callback):
        fake = FakeInputStream(samplerate, channels, device, callback, finished_callback)
        fakes.append(fake)
        return fake

    rec = mic_capture.MicRecorder(stream_factory=factory)
    rec.record(duration_s, FS, device=None)
    fakes[0].feed([clipping_block])
    assert rec.clipped is True

    # A block peaking at -1 dBFS (amplitude ~0.891) must NOT trigger clipping.
    minus_1_dbfs_amplitude = 10 ** (-1.0 / 20.0)
    quiet_block = np.zeros((n_samples, 1), dtype=np.float32)
    quiet_block[10, 0] = minus_1_dbfs_amplitude

    rec2 = mic_capture.MicRecorder(stream_factory=factory)
    rec2.record(duration_s, FS, device=None)
    fakes[1].feed([quiet_block])
    assert rec2.clipped is False


def test_recorder_rms_matches_metering():
    n_samples = 4096
    duration_s = n_samples / FS
    block = _mono_block(n_samples, amplitude=0.2, seed=7)

    fakes = []

    def factory(samplerate, channels, device, callback, finished_callback):
        fake = FakeInputStream(samplerate, channels, device, callback, finished_callback)
        fakes.append(fake)
        return fake

    rec = mic_capture.MicRecorder(stream_factory=factory)
    rec.record(duration_s, FS, device=None)
    fakes[0].feed([block])

    expected = metering.rms_dbfs(block[:, 0])
    assert abs(rec.rms_dbfs() - expected) < 1e-6


def test_recorder_rms_before_any_recording_is_floor():
    rec = mic_capture.MicRecorder(stream_factory=lambda *a, **k: FakeInputStream(*a, **k))
    assert rec.buffer is None
    assert rec.rms_dbfs() == metering.FLOOR_DBFS


def test_recorder_second_record_stops_first():
    duration_s = 1.0
    fakes = []

    def factory(samplerate, channels, device, callback, finished_callback):
        fake = FakeInputStream(samplerate, channels, device, callback, finished_callback)
        fakes.append(fake)
        return fake

    rec = mic_capture.MicRecorder(stream_factory=factory)
    rec.record(duration_s, FS)
    first = fakes[0]
    assert first.started and not first.stopped

    rec.record(duration_s, FS)
    assert len(fakes) == 2
    second = fakes[1]

    assert first.stopped is True
    assert first.closed is True
    assert second.started is True
    assert second.stopped is False
    assert rec.is_recording is True


def test_recorder_stop_is_clean_and_idempotent():
    fakes = []

    def factory(samplerate, channels, device, callback, finished_callback):
        fake = FakeInputStream(samplerate, channels, device, callback, finished_callback)
        fakes.append(fake)
        return fake

    rec = mic_capture.MicRecorder(stream_factory=factory)
    rec.record(1.0, FS)
    rec.stop()
    assert rec.is_recording is False
    assert fakes[0].stopped is True
    assert fakes[0].closed is True

    rec.stop()  # idempotent, no exception
    assert rec.is_recording is False


def test_recorder_raises_audio_error_on_exclusive_device():
    def factory(samplerate, channels, device, callback, finished_callback):
        raise sd.PortAudioError("Error opening InputStream: Device unavailable", -9996)

    rec = mic_capture.MicRecorder(stream_factory=factory)

    raised = None
    try:
        rec.record(1.0, FS)
    except mic_capture.AudioError as exc:
        raised = exc

    assert raised is not None
    assert raised.kind == "exclusive"
    assert rec.is_recording is False


def test_recorder_raises_audio_error_no_device():
    def factory(samplerate, channels, device, callback, finished_callback):
        raise sd.PortAudioError("Error opening InputStream: Invalid device", -9997)

    rec = mic_capture.MicRecorder(stream_factory=factory)

    raised = None
    try:
        rec.record(1.0, FS, device=999)
    except mic_capture.AudioError as exc:
        raised = exc

    assert raised is not None
    assert raised.kind == "no-device"


if __name__ == "__main__":
    import inspect

    class _Monkeypatch:
        """Minimal pytest-style monkeypatch stand-in for setattr with
        automatic restore -- this test file is run standalone by
        run_all_tests.py, not under pytest, mirroring every other
        selftest_*.py's `if __name__ == "__main__"` runner shape.
        """

        def __init__(self):
            self._restores = []

        def setattr(self, obj, name, value):
            self._restores.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._restores):
                setattr(obj, name, old)

    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            params = inspect.signature(f).parameters
            if "monkeypatch" in params:
                mp = _Monkeypatch()
                try:
                    f(mp)
                finally:
                    mp.undo()
            else:
                f()
            print("PASS", n)
