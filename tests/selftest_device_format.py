"""Self-test: rumble.core.device_format — pure WAVEFORMATEX parsing/labeling,
plus a smoke test that the COM-touching query functions never raise.

Fully offline for the pure parts (SimpleNamespace stand-ins for WAVEFORMATEX,
no COM). The COM smoke tests exercise the real pycaw/comtypes path on this
machine but only assert "did not raise" — the actual return value (a
DeviceFormat or None) depends on the machine's audio stack, so it is not
asserted precisely, matching the offscreen/CI-safe pattern used elsewhere in
this test suite.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from types import SimpleNamespace

from rumble.core import device_format as df
from rumble.core.audio_devices import pattern_matches_text
from rumble.core.profile_manager import ProfileManager


def test_parse_waveformat():
    wfx = SimpleNamespace(nSamplesPerSec=48000, wBitsPerSample=24, nChannels=2)
    fmt = df.parse_waveformat(wfx)
    assert (fmt.sample_rate, fmt.bit_depth, fmt.channels) == (48000, 24, 2)
    assert fmt.label == "48 kHz · 24-bit"


def test_label_441():
    fmt = df.DeviceFormat(44100, 16, 2)
    assert fmt.label == "44.1 kHz · 16-bit"


def test_label_96k():
    fmt = df.DeviceFormat(96000, 24, 2)
    assert fmt.label == "96 kHz · 24-bit"


def test_label_192k():
    fmt = df.DeviceFormat(192000, 32, 2)
    assert fmt.label == "192 kHz · 32-bit"


def test_label_882k_not_round():
    # 88.2 kHz is a common "x2 of 44.1" rate and must not round to 88 or 88.20
    fmt = df.DeviceFormat(88200, 24, 2)
    assert fmt.label == "88.2 kHz · 24-bit"


def test_label_odd_bit_depth():
    fmt = df.DeviceFormat(48000, 20, 2)
    assert fmt.label == "48 kHz · 20-bit"


def test_label_float_mix_format():
    # WASAPI shared-mode mix formats are frequently 32-bit IEEE float
    fmt = df.DeviceFormat(96000, 32, 2)
    assert fmt.label == "96 kHz · 32-bit"


def test_label_multichannel_does_not_affect_label():
    fmt = df.DeviceFormat(48000, 24, 6)
    assert fmt.channels == 6
    assert fmt.label == "48 kHz · 24-bit"


def test_parse_waveformat_coerces_to_int():
    # ctypes struct fields come back as plain ints already, but guard against
    # any wrapper type (e.g. a ctypes.c_ulong) that supports int().
    wfx = SimpleNamespace(nSamplesPerSec=44100.0, wBitsPerSample=16, nChannels=2)
    fmt = df.parse_waveformat(wfx)
    assert fmt.sample_rate == 44100
    assert isinstance(fmt.sample_rate, int)


def test_device_format_is_frozen():
    fmt = df.DeviceFormat(48000, 24, 2)
    try:
        fmt.sample_rate = 44100
        raised = False
    except Exception:
        raised = True
    assert raised, "DeviceFormat must be immutable (frozen dataclass)"


def test_fragment_matches_word_wise():
    # device_format.py reuses audio_devices.pattern_matches_text for identity
    # matching — exercised directly here, and via get_device_format below.
    assert pattern_matches_text("Speakers FIIO", "Speakers (FIIO K13 R2R)")
    assert pattern_matches_text("fiio", "Speakers (FIIO K13 R2R)")
    assert not pattern_matches_text("Headphones", "Speakers (FIIO K13 R2R)")
    assert not pattern_matches_text("", "Speakers (FIIO K13 R2R)")


def test_fragment_matches_semicolon_alternatives():
    # ';' separates alternatives; any one matching is enough (APO semantics).
    assert pattern_matches_text("Realtek; FIIO", "Speakers (FIIO K13 R2R)")
    assert pattern_matches_text("Realtek; Headphones USB", "Speakers (FIIO K13 R2R) [Headphones USB]")
    assert not pattern_matches_text("Realtek; Headphones", "Speakers (FIIO K13 R2R)")


def test_fragment_matches_all_keyword():
    # The literal keyword "all" matches every device — this is what
    # ApoDevice.pattern / first_active_device_pattern() return when no
    # device is active, so get_device_format must honor it too.
    assert pattern_matches_text("all", "Speakers (FIIO K13 R2R)")
    assert pattern_matches_text("all", "")
    assert pattern_matches_text("all", "anything at all")


def test_query_never_raises():
    df.get_default_device_format()   # may be None offscreen/CI — must not raise


def test_get_default_device_name_never_raises():
    name = df.get_default_device_name()  # may be "" offscreen/CI — must not raise
    assert isinstance(name, str)


def test_get_device_format_never_raises_on_garbage():
    df.get_device_format("this device definitely does not exist xyz123")
    df.get_device_format("")
    # "all" is what ApoDevice.pattern / first_active_device_pattern() return
    # when no device is active — must be accepted, not just "not raise".
    df.get_device_format("all")


def test_sink_dispatch():
    # _NotificationSink fans every IMMNotificationClient handler out to one
    # plain zero-arg callback — exercised here with direct method calls, no
    # live COM registration or CoInitialize required.
    calls = []
    sink = df._NotificationSink(lambda: calls.append(1))
    sink.OnPropertyValueChanged("id", None)
    sink.OnDefaultDeviceChanged(0, 0, "id")
    sink.OnDeviceAdded("id"); sink.OnDeviceRemoved("id")
    sink.OnDeviceStateChanged("id", 1)
    assert len(calls) == 5


def test_sink_dispatch_swallows_callback_exceptions():
    # A raise inside a COM callback thread must never propagate.
    def boom():
        raise RuntimeError("boom")
    sink = df._NotificationSink(boom)
    sink.OnDeviceAdded("id")  # must not raise
    sink.OnPropertyValueChanged("id", None)  # must not raise


def test_watcher_start_stop_idempotent():
    w = df.DeviceFormatWatcher(lambda: None)
    w.start(); w.start(); w.stop(); w.stop()   # must never raise, even without COM registration succeeding


def test_watcher_debounce_coalesces_bursts():
    # A burst of rapid-fire notifications must collapse into exactly one
    # on_change() call, not one per notification.
    calls = []
    w = df.DeviceFormatWatcher(lambda: calls.append(1))
    for _ in range(5):
        w._on_notification()
    time.sleep(w._DEBOUNCE_SECONDS + 0.3)
    assert len(calls) == 1, f"expected exactly 1 coalesced call, got {len(calls)}"
    w.stop()


def test_watcher_stop_before_start_never_raises():
    w = df.DeviceFormatWatcher(lambda: None)
    w.stop()  # never started — must not raise


def test_watcher_start_retries_after_failed_registration():
    # A transient registration failure (e.g. a one-off COM/audio-service
    # error at startup) must not permanently disable the watcher: a second
    # .start() call must retry, not silently no-op forever. Simulate the
    # failure by monkeypatching the module-level `_NotificationSink` factory
    # — the same seam `start()` calls unqualified after a real (successful,
    # on this machine) `AudioUtilities.GetDeviceEnumerator()` call — to raise
    # once, then succeed.
    calls = {"n": 0}
    real_sink_factory = df._NotificationSink

    def flaky_sink(callback):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient registration failure")
        return real_sink_factory(callback)

    df._NotificationSink = flaky_sink
    try:
        w = df.DeviceFormatWatcher(lambda: None)
        w.start()  # first attempt fails (simulated) — must not raise
        assert not w._registered, "registration must not be marked successful after a failure"
        w.start()  # retry — must attempt registration again, not no-op
        assert calls["n"] == 2, f"expected _NotificationSink to be called twice (retry), got {calls['n']}"
        w.stop()
    finally:
        df._NotificationSink = real_sink_factory


def test_watcher_live_registration_smoke():
    # Real pycaw/comtypes path on this machine — only asserts "did not
    # raise", matching the COM smoke-test pattern used elsewhere in this
    # file; registration success/failure both count as pass.
    calls = []
    w = df.DeviceFormatWatcher(lambda: calls.append(1))
    w.start()
    w.stop()


# ---------------------------------------------------------------------------
# ProfileManager.effective_sample_rate() — Task 4
# ---------------------------------------------------------------------------
#
# Every ProfileManager here is constructed with BOTH data_dir and config_path
# pointing at a temp dir (see tests/test_device_integration.py's harness
# comment) — passing only data_dir would leave config_path falling back to
# the real Equalizer APO config dir, which these tests must never touch.


def _temp_manager() -> ProfileManager:
    tmpdir = Path(tempfile.mkdtemp())
    return ProfileManager(tmpdir / "data", config_path=tmpdir / "config")


def test_effective_sample_rate_manual_mode():
    manager = _temp_manager()
    manager.settings["sample_rate_mode"] = "manual"
    manager.settings["sample_rate"] = 44100
    assert manager.effective_sample_rate() == 44100


def test_effective_sample_rate_manual_mode_never_calls_device_format():
    # "manual" must not touch device_format/COM at all, not even to ignore
    # the result — monkeypatch the query to raise if it's ever called.
    manager = _temp_manager()
    manager.settings["sample_rate_mode"] = "manual"
    manager.settings["sample_rate"] = 44100
    real_query = df.get_default_device_format

    def boom():
        raise AssertionError("manual mode must not call get_default_device_format")

    df.get_default_device_format = boom
    try:
        assert manager.effective_sample_rate() == 44100
    finally:
        df.get_default_device_format = real_query


def test_effective_sample_rate_auto_mode_uses_detected_rate():
    manager = _temp_manager()
    manager.settings["sample_rate_mode"] = "auto"
    manager.settings["sample_rate"] = 44100  # manual fallback — must be ignored here
    real_query = df.get_default_device_format
    df.get_default_device_format = lambda: df.DeviceFormat(96000, 24, 2)
    try:
        assert manager.effective_sample_rate() == 96000
    finally:
        df.get_default_device_format = real_query


def test_effective_sample_rate_auto_mode_falls_back_when_detection_fails():
    manager = _temp_manager()
    manager.settings["sample_rate_mode"] = "auto"
    manager.settings["sample_rate"] = 44100
    real_query = df.get_default_device_format
    df.get_default_device_format = lambda: None
    try:
        assert manager.effective_sample_rate() == 44100
    finally:
        df.get_default_device_format = real_query


def test_effective_sample_rate_default_mode_is_auto():
    # DEFAULT_SETTINGS["sample_rate_mode"] must default to "auto" so existing
    # users (and fresh installs) get device-matched detection without opting in.
    manager = _temp_manager()
    assert manager.settings.get("sample_rate_mode") == "auto"


# ---------------------------------------------------------------------------
# ProfileManager.effective_sample_rate() in-memory cache — Task 7 fix
# ---------------------------------------------------------------------------
#
# effective_sample_rate() must NOT make a live COM round-trip on every call in
# "auto" mode. Once a rate has been detected (either by a prior live query, or
# by set_detected_sample_rate() from MainWindow._refresh_device_format()),
# subsequent calls on the same manager instance must be cache hits.


def test_effective_sample_rate_auto_mode_caches_after_first_live_query():
    # First call with no cache warmed: falls back to a live query and must
    # warm the cache from it. A second call, even after the live query is
    # repointed at a different rate, must still return the FIRST cached
    # value — proving no second COM call happened.
    manager = _temp_manager()
    manager.settings["sample_rate_mode"] = "auto"
    manager.settings["sample_rate"] = 44100
    real_query = df.get_default_device_format
    df.get_default_device_format = lambda: df.DeviceFormat(96000, 24, 2)
    try:
        assert manager.effective_sample_rate() == 96000  # cache miss -> live query, warms cache

        def boom():
            raise AssertionError("second call must be a cache hit, not a live query")

        df.get_default_device_format = boom
        assert manager.effective_sample_rate() == 96000  # cache hit -> still the first value
    finally:
        df.get_default_device_format = real_query


def test_set_detected_sample_rate_avoids_live_query_entirely():
    # When the cache is pre-warmed via set_detected_sample_rate() (as
    # MainWindow._refresh_device_format() does), effective_sample_rate() must
    # never call device_format.get_default_device_format() at all.
    manager = _temp_manager()
    manager.settings["sample_rate_mode"] = "auto"
    manager.settings["sample_rate"] = 44100
    manager.set_detected_sample_rate(88200)
    real_query = df.get_default_device_format

    def boom():
        raise AssertionError("cache was pre-warmed; get_default_device_format must not be called")

    df.get_default_device_format = boom
    try:
        assert manager.effective_sample_rate() == 88200
    finally:
        df.get_default_device_format = real_query


def test_effective_sample_rate_auto_mode_does_not_cache_failed_detection():
    # A failed live detection (None) must NOT be cached as a permanent
    # failure — the next call should retry live and pick up a real rate.
    manager = _temp_manager()
    manager.settings["sample_rate_mode"] = "auto"
    manager.settings["sample_rate"] = 44100
    real_query = df.get_default_device_format
    df.get_default_device_format = lambda: None
    try:
        assert manager.effective_sample_rate() == 44100  # falls back to manual, not cached

        df.get_default_device_format = lambda: df.DeviceFormat(96000, 24, 2)
        assert manager.effective_sample_rate() == 96000  # retried live, picked up the new rate
    finally:
        df.get_default_device_format = real_query


# ---------------------------------------------------------------------------
# DeviceFormatAdapter — Qt main-thread marshaling (Task 5)
# ---------------------------------------------------------------------------
#
# These tests need a QApplication and Qt's offscreen platform, matching the
# convention in tests/selftest_settings_dialog_eq_defaults.py. The QApplication
# is created lazily (only when an adapter test runs) so the pure/COM tests
# above stay Qt-free and this file remains runnable headless in CI.

import os
import threading


def _ensure_qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_adapter_has_format_changed_signal():
    _ensure_qapp()
    from rumble.ui.device_format_adapter import DeviceFormatAdapter
    adapter = DeviceFormatAdapter()
    # A bound Signal exposes .connect/.emit; assert the public surface exists.
    assert hasattr(adapter, "format_changed")
    assert hasattr(adapter, "start") and hasattr(adapter, "stop")
    adapter.stop()  # safe before start()


def test_adapter_start_stop_never_raises():
    _ensure_qapp()
    from rumble.ui.device_format_adapter import DeviceFormatAdapter
    adapter = DeviceFormatAdapter()
    adapter.start(); adapter.start(); adapter.stop(); adapter.stop()


def test_adapter_marshals_to_main_thread():
    # THE core Task-5 guarantee: the watcher callback fires on a foreign
    # (COM/Timer) thread, but the connected Qt slot MUST run on the Qt main
    # thread. We call the adapter's watcher-callback seam
    # (_on_change_from_com_thread) from a background threading.Thread, then
    # spin the event loop and assert the slot's captured thread identity
    # equals the main thread's — proving Qt's queued cross-thread delivery.
    app = _ensure_qapp()
    from PySide6.QtCore import QCoreApplication, QThread
    from rumble.ui.device_format_adapter import DeviceFormatAdapter

    main_thread = QThread.currentThread()
    captured = {}
    fired = []

    def on_format_changed():
        captured["thread"] = QThread.currentThread()
        fired.append(1)

    adapter = DeviceFormatAdapter()
    adapter.format_changed.connect(on_format_changed)

    # Fire the callback from a genuinely different thread, and confirm the
    # slot did NOT run inline on that thread (queued, not direct).
    bg_thread_ran_slot = {"inline": False}

    def bg():
        adapter._on_change_from_com_thread()
        # If the connection were (wrongly) Direct, the slot would have run by
        # now on THIS thread and `captured` would already be populated.
        bg_thread_ran_slot["inline"] = "thread" in captured

    t = threading.Thread(target=bg)
    t.start()
    t.join()

    assert not bg_thread_ran_slot["inline"], (
        "slot ran inline on the background thread — connection was Direct, not "
        "Queued; the emit did not cross the thread boundary as required")

    # Nothing has spun the event loop yet, so the queued slot must not have run.
    assert not fired, "queued slot ran before the event loop was spun"

    # Spin the main-thread event loop to deliver the queued invocation.
    for _ in range(10):
        QCoreApplication.processEvents()
        if fired:
            break

    assert fired, "format_changed slot never delivered after processEvents()"
    assert captured["thread"] is main_thread, (
        "format_changed slot ran on the wrong thread — expected the Qt main "
        f"thread {main_thread}, got {captured['thread']}")


def test_adapter_debounce_then_marshal_end_to_end():
    # End-to-end through the real watcher debounce: a burst of _on_notification
    # calls collapses to ONE on_change on a Timer thread, which the adapter
    # marshals to the main thread as one format_changed.
    app = _ensure_qapp()
    from PySide6.QtCore import QCoreApplication, QThread
    from rumble.ui.device_format_adapter import DeviceFormatAdapter

    main_thread = QThread.currentThread()
    seen = {"count": 0, "thread": None}

    def on_changed():
        seen["count"] += 1
        seen["thread"] = QThread.currentThread()

    adapter = DeviceFormatAdapter()
    adapter.format_changed.connect(on_changed)
    # Drive the watcher's debounce directly (no live COM registration needed).
    for _ in range(5):
        adapter._watcher._on_notification()
    # Wait past the debounce for the Timer thread to fire on_change → emit().
    time.sleep(adapter._watcher._DEBOUNCE_SECONDS + 0.4)
    for _ in range(10):
        QCoreApplication.processEvents()
        if seen["count"]:
            break
    adapter.stop()
    assert seen["count"] == 1, f"expected exactly 1 coalesced emit, got {seen['count']}"
    assert seen["thread"] is main_thread, "coalesced emit not marshaled to main thread"


# ---------------------------------------------------------------------------
# ir_analysis.check_rate_mismatch() — Task 6
# ---------------------------------------------------------------------------

from rumble.core import ir_analysis


def test_rate_mismatch_matched_rates_is_none():
    assert ir_analysis.check_rate_mismatch(44100, 44100) is None


def test_rate_mismatch_detects_mismatch_with_both_numbers():
    warning = ir_analysis.check_rate_mismatch(44100, 48000)
    assert warning is not None
    assert "44.1" in warning
    assert "48" in warning


def test_rate_mismatch_unknown_ir_rate_zero_is_none():
    # No false alarms when the IR's own rate could not be determined.
    assert ir_analysis.check_rate_mismatch(0, 48000) is None


def test_rate_mismatch_unknown_ir_rate_none_is_none():
    assert ir_analysis.check_rate_mismatch(None, 48000) is None


def test_rate_mismatch_unknown_device_rate_is_none():
    # Symmetric guard: an unset/zero device rate must not false-alarm either.
    assert ir_analysis.check_rate_mismatch(44100, 0) is None
    assert ir_analysis.check_rate_mismatch(44100, None) is None


def test_rate_mismatch_never_raises_on_string_like_ints():
    # check_rate_mismatch does int(ir_rate)/int(device_rate) internally, so
    # string-like inputs (e.g. from a loosely-typed caller) must coerce
    # cleanly rather than raising, AND must compare by numeric value after
    # coercion (not fall back to some string-equality shortcut).
    assert ir_analysis.check_rate_mismatch("44100", "48000") is not None
    assert ir_analysis.check_rate_mismatch("44100", "44100") is None
    assert ir_analysis.check_rate_mismatch("44100", 44100) is None


def test_rate_mismatch_matches_at_high_rates():
    warning = ir_analysis.check_rate_mismatch(96000, 192000)
    assert warning is not None
    assert "96" in warning and "192" in warning


# ---------------------------------------------------------------------------
# ConvolutionEditor.set_file_status() — Task 6 UI surfacing
# ---------------------------------------------------------------------------


def test_convolution_editor_set_file_status_shows_mismatch_text():
    _ensure_qapp()
    from rumble.ui.advanced_panel import ConvolutionEditor
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    editor = ConvolutionEditor(get_theme(DEFAULT_THEME_NAME))
    editor.set_file_status(True, "IR is 44.1 kHz but the device runs 48 kHz — mismatch")
    assert "44.1" in editor.warning.text()
    assert "48" in editor.warning.text()


def test_convolution_editor_set_file_status_missing_file_wins_over_mismatch():
    # A missing/unreadable file must show the missing-file warning even if a
    # mismatch string is (incorrectly) passed alongside ok=False.
    _ensure_qapp()
    from rumble.ui.advanced_panel import ConvolutionEditor
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    editor = ConvolutionEditor(get_theme(DEFAULT_THEME_NAME))
    editor.set_file_status(False, "some mismatch text")
    assert editor.warning.text() == editor._warning_missing


def test_convolution_editor_set_file_status_no_mismatch_shows_default():
    _ensure_qapp()
    from rumble.ui.advanced_panel import ConvolutionEditor
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    editor = ConvolutionEditor(get_theme(DEFAULT_THEME_NAME))
    editor.set_file_status(True, None)
    assert editor.warning.text() == editor._warning_default


# ---------------------------------------------------------------------------
# EditorPanel end-to-end: IR mismatch surfaces on load AND on a live rate
# change (simulating DeviceFormatAdapter.format_changed), without a full
# profile reload — Task 6
# ---------------------------------------------------------------------------


def _write_minimal_wav(path: Path, sample_rate: int, num_samples: int = 256) -> None:
    """Write a minimal real PCM16 mono RIFF/WAVE file at *sample_rate* —
    enough for ir_analysis.read_wav() to parse (fmt + data chunks only)."""
    import struct as _struct

    samples = bytes(2 * num_samples)  # silence is fine; only the header rate matters
    fmt_chunk = _struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    data_chunk = samples
    riff_body = (
        b"WAVE"
        + b"fmt " + _struct.pack("<I", len(fmt_chunk)) + fmt_chunk
        + b"data" + _struct.pack("<I", len(data_chunk)) + data_chunk
    )
    path.write_bytes(b"RIFF" + _struct.pack("<I", len(riff_body)) + riff_body)


def test_editor_panel_shows_mismatch_on_ir_load():
    _ensure_qapp()
    from rumble.ui.editor_panel import EditorPanel
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    tmpdir = Path(tempfile.mkdtemp(prefix="rumble_ir_selftest_"))
    wav_path = tmpdir / "ir_44100.wav"
    _write_minimal_wav(wav_path, 44100)

    panel = EditorPanel(get_theme(DEFAULT_THEME_NAME))
    panel.set_ir_resolver(lambda value: Path(value) if Path(value).is_file() else None)
    panel._sample_rate = 48000  # device runs 48k; the IR is 44.1k
    panel.advanced_panel.convolution.set_value(str(wav_path))
    panel._refresh_ir_result()

    warning_text = panel.advanced_panel.convolution.warning.text()
    assert "44.1" in warning_text and "48" in warning_text, (
        f"expected a mismatch warning naming both rates, got: {warning_text!r}")


def test_editor_panel_mismatch_updates_on_live_rate_change():
    # THE core Task-6 guarantee: changing the device's effective sample rate
    # (as DeviceFormatAdapter.format_changed -> MainWindow._on_device_format_
    # changed -> EditorPanel.set_sample_rate does) must update the displayed
    # mismatch warning WITHOUT a full profile reload.
    _ensure_qapp()
    from rumble.ui.editor_panel import EditorPanel
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    tmpdir = Path(tempfile.mkdtemp(prefix="rumble_ir_selftest_"))
    wav_path = tmpdir / "ir_44100.wav"
    _write_minimal_wav(wav_path, 44100)

    panel = EditorPanel(get_theme(DEFAULT_THEME_NAME))
    panel.set_ir_resolver(lambda value: Path(value) if Path(value).is_file() else None)
    panel._sample_rate = 44100  # starts matched
    panel.advanced_panel.convolution.set_value(str(wav_path))
    panel._refresh_ir_result()

    matched_text = panel.advanced_panel.convolution.warning.text()
    assert "44.1" not in matched_text or "48" not in matched_text, (
        "matched rates must not show a mismatch warning naming a mismatched pair")

    # Simulate a live device-format change to 48 kHz, exactly as
    # MainWindow._on_device_format_changed calls it.
    panel.set_sample_rate(48000)

    mismatched_text = panel.advanced_panel.convolution.warning.text()
    assert "44.1" in mismatched_text and "48" in mismatched_text, (
        f"set_sample_rate() must re-derive the mismatch label live, got: {mismatched_text!r}")


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
