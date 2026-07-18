"""Self-test: rumble.core.auto_switch — Phase 3 automatic profile switching.

Task 1 covers the prep refactor: ProfileManager.activate_profile persists
last_active_profile itself and notifies on_profile_activated observers, so
main_window (manual activation), the tray, and the auto-switch controller
(Task 4) all share one activation path. Later tasks append RuleEngine,
SessionWatcher, and AutoSwitchController tests to this same file.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import auto_switch as asw
from rumble.core.auto_switch import AutoSwitchController, Rule, RuleEngine, SessionWatcher
from rumble.core.profile_manager import ProfileManager


def _ensure_qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_manager():
    d = Path(tempfile.mkdtemp(prefix="rumble_autoswitch_data_"))
    c = Path(tempfile.mkdtemp(prefix="rumble_autoswitch_config_"))
    return ProfileManager(d, config_path=c)


def test_activation_observer_and_persistence():
    m = make_manager()
    pid = m.create_profile("Obs Test").id
    seen = []
    m.on_profile_activated.append(seen.append)
    assert m.activate_profile(pid)
    assert seen == [pid]
    assert m.settings["last_active_profile"] == pid


def test_failed_activation_no_callback():
    m = make_manager()
    m.on_profile_activated.append(lambda pid: (_ for _ in ()).throw(AssertionError))
    assert not m.activate_profile("no-such-id")


def test_observer_exception_does_not_break_activation():
    # A misbehaving observer must not prevent activation or other observers
    # from running (main_window, tray, and auto-switch all subscribe).
    m = make_manager()
    pid = m.create_profile("Resilient Test").id
    seen = []

    def bad_cb(_pid):
        raise RuntimeError("boom")

    m.on_profile_activated.append(bad_cb)
    m.on_profile_activated.append(seen.append)
    assert m.activate_profile(pid)
    assert seen == [pid]


# ── Task 2: RuleEngine (pure logic) ────────────────────────────────────

R_APP = Rule("app", "game.exe", "p-game")
R_DEV = Rule("device", "Headphones", "p-hp")
R_TIME = Rule("time", "22:00-07:00", "p-night")


def ev(engine, exes=frozenset(), dev="Speakers", now=0.0, hhmm="12:00"):
    return engine.evaluate(set(exes), dev, now, hhmm)


def test_priority_first_match_wins():
    e = RuleEngine([R_DEV, R_APP], trigger_secs=0, linger_secs=0)
    assert ev(e, exes={"game.exe"}, dev="Headphones") == "p-hp"


def test_app_debounce_then_fire():
    e = RuleEngine([R_APP], trigger_secs=2, linger_secs=10)
    assert ev(e, exes={"game.exe"}, now=0.0) is None      # seen, not yet held 2s
    assert ev(e, exes={"game.exe"}, now=2.5) == "p-game"  # held long enough


def test_linger_survives_gaps_then_releases():
    e = RuleEngine([R_APP], trigger_secs=0, linger_secs=10)
    assert ev(e, exes={"game.exe"}, now=0.0) == "p-game"
    assert ev(e, exes=set(), now=5.0) == "p-game"          # within linger
    assert ev(e, exes=set(), now=11.0) is None             # released


def test_time_window_crossing_midnight():
    e = RuleEngine([R_TIME], trigger_secs=0, linger_secs=0)
    assert ev(e, hhmm="23:30") == "p-night"
    assert ev(e, hhmm="06:59") == "p-night"
    assert ev(e, hhmm="12:00") is None


def test_disabled_rule_skipped():
    r = Rule("app", "game.exe", "p-game", enabled=False)
    assert ev(RuleEngine([r], trigger_secs=0, linger_secs=0), exes={"game.exe"}) is None


def test_roundtrip_dict():
    assert Rule.from_dict(R_TIME.to_dict()) == R_TIME


def test_app_rule_never_fires_below_trigger_without_holding():
    # A second brief appearance that never accumulates trigger_secs must
    # keep returning None, not spuriously latch "held".
    e = RuleEngine([R_APP], trigger_secs=5, linger_secs=0)
    assert ev(e, exes={"game.exe"}, now=0.0) is None
    assert ev(e, exes=set(), now=1.0) is None  # stopped before holding; releases
    assert ev(e, exes={"game.exe"}, now=1.0) is None  # first_seen resets to 1.0
    assert ev(e, exes={"game.exe"}, now=3.0) is None  # only 2s elapsed, not 5


# ── Task 3: SessionWatcher (pycaw polling thread) ──────────────────────

def _fake_session(name, state):
    process = SimpleNamespace(name=lambda: name) if name is not None else None
    return SimpleNamespace(Process=process, State=state)


def test_active_exes_from_sessions_filters_by_state_and_process():
    sessions = [
        _fake_session("Game.exe", 1),   # active -> included, lowercased
        _fake_session("Idle.exe", 0),   # inactive -> excluded
        _fake_session(None, 1),         # no process -> excluded
    ]
    assert asw._active_exes_from_sessions(sessions) == {"game.exe"}


def test_active_exes_from_sessions_skips_broken_session():
    class Broken:
        @property
        def State(self):
            raise RuntimeError("boom")

    sessions = [Broken(), _fake_session("Music.exe", 1)]
    assert asw._active_exes_from_sessions(sessions) == {"music.exe"}


def test_session_watcher_start_stop_idempotent():
    w = SessionWatcher(lambda exes: None, poll_secs=0.05)
    w.start(); w.start(); w.stop(); w.stop()  # must never raise


def test_session_watcher_callback_only_on_change():
    calls = []
    w = SessionWatcher(calls.append, poll_secs=0.05)
    real_get_sessions = asw._get_sessions
    results = iter([
        [_fake_session("game.exe", 1)],
        [_fake_session("game.exe", 1)],  # unchanged -> no callback
        [_fake_session("game.exe", 1)],  # unchanged -> no callback
        [],                              # changed -> callback
    ])
    asw._get_sessions = lambda: next(results, [])
    try:
        w.start()
        time.sleep(0.05 * 6)
        w.stop()
    finally:
        asw._get_sessions = real_get_sessions
    assert calls == [{"game.exe"}, set()], f"expected exactly the two change events, got {calls}"


def test_session_watcher_live_smoke():
    # Real pycaw path on this machine — only asserts "did not raise",
    # matching device_format.py's COM smoke-test convention.
    w = SessionWatcher(lambda exes: None, poll_secs=0.2)
    w.start()
    time.sleep(0.3)
    w.stop()


# ── Task 4: Settings + AutoSwitchController ────────────────────────────

def _spy_activate(manager, calls):
    real_activate = manager.activate_profile

    def spy(pid):
        calls.append(pid)
        return real_activate(pid)

    manager.activate_profile = spy


def test_controller_activates_once_then_restores_baseline_on_release():
    m = make_manager()
    p_base = m.create_profile("Baseline").id
    p_rule = m.create_profile("RuleTarget").id
    m.activate_profile(p_base)

    calls = []
    _spy_activate(m, calls)

    rule = Rule("app", "game.exe", p_rule)
    engine = RuleEngine([rule], trigger_secs=0, linger_secs=0)
    switches = []
    controller = AutoSwitchController(m, engine, on_switched=lambda pid, reason: switches.append((pid, reason)))

    controller.tick({"game.exe"}, "Speakers", 0.0, "12:00")
    controller.tick({"game.exe"}, "Speakers", 1.0, "12:00")  # same target -> no repeat activation
    assert calls == [p_rule], f"expected exactly one activation, got {calls}"
    assert m.settings["last_active_profile"] == p_rule
    assert switches == [(p_rule, "rule")]

    controller.tick(set(), "Speakers", 20.0, "12:00")  # rule releases -> baseline restored
    assert calls == [p_rule, p_base]
    assert m.settings["last_active_profile"] == p_base
    assert switches == [(p_rule, "rule"), (p_base, "baseline")]


def test_controller_suspended_while_dirty():
    m = make_manager()
    p_base = m.create_profile("Baseline3").id
    p_rule = m.create_profile("RuleTarget3").id
    m.activate_profile(p_base)

    rule = Rule("app", "game.exe", p_rule)
    engine = RuleEngine([rule], trigger_secs=0, linger_secs=0)
    controller = AutoSwitchController(m, engine)
    controller.set_editor_dirty(True)
    assert controller.state() == "suspended-dirty"

    controller.tick({"game.exe"}, "Speakers", 0.0, "12:00")
    assert m.settings["last_active_profile"] == p_base  # untouched while suspended


def test_controller_state_off_when_disabled():
    m = make_manager()
    p1 = m.create_profile("P1").id
    m.activate_profile(p1)
    controller = AutoSwitchController(m, RuleEngine([]))
    controller.set_enabled(False)
    assert controller.state() == "off"


def test_manual_activation_updates_baseline_while_no_rule_holds():
    m = make_manager()
    p_a = m.create_profile("A").id
    p_b = m.create_profile("B").id
    p_rule = m.create_profile("RuleTarget4").id
    m.activate_profile(p_a)

    controller = AutoSwitchController(m, RuleEngine([Rule("app", "game.exe", p_rule)], trigger_secs=0, linger_secs=0))
    m.activate_profile(p_b)  # manual switch while no rule holds -> new baseline

    calls = []
    _spy_activate(m, calls)
    controller.tick({"game.exe"}, "Speakers", 0.0, "12:00")
    assert calls == [p_rule]
    controller.tick(set(), "Speakers", 20.0, "12:00")  # release -> restores the manual choice, not p_a
    assert calls == [p_rule, p_b]


def test_manual_activation_during_hold_updates_baseline_for_later_release():
    # If the user manually switches profiles WHILE a rule currently holds
    # (e.g. picks a different profile mid-game, without the game closing),
    # that pick must become the new baseline: when the rule eventually
    # releases, it should restore the user's latest manual choice, not
    # whatever was active before the rule first took hold.
    m = make_manager()
    p_pre = m.create_profile("PreGame").id
    p_rule = m.create_profile("GameProfile").id
    p_manual = m.create_profile("ManualPick").id
    m.activate_profile(p_pre)

    controller = AutoSwitchController(m, RuleEngine([Rule("app", "game.exe", p_rule)], trigger_secs=0, linger_secs=0))
    controller.tick({"game.exe"}, "Speakers", 0.0, "12:00")  # rule fires, baseline = p_pre
    assert m.settings["last_active_profile"] == p_rule

    m.activate_profile(p_manual)  # manual switch mid-hold (rule condition still true)
    assert controller._baseline == p_manual

    calls = []
    _spy_activate(m, calls)
    controller.tick(set(), "Speakers", 20.0, "12:00")  # rule releases
    # Baseline already equals the currently-active profile (the manual pick),
    # so release needs no extra activation call — but critically, the
    # profile must stay p_manual, NOT revert to the pre-game p_pre.
    assert calls == [], f"release should not need to re-activate anything, got {calls}"
    assert m.settings["last_active_profile"] == p_manual


def test_controller_close_detaches_from_manager_observer_list():
    # Guards against the in-process interface-size rebuild leak: main_window
    # recreates MainWindow (and so a new AutoSwitchController) against the
    # SAME long-lived manager, so an old controller that isn't detached would
    # stay in on_profile_activated forever, accumulating one dead subscriber
    # per resize.
    m = make_manager()
    controller = AutoSwitchController(m, RuleEngine([]))
    assert controller._on_activated in m.on_profile_activated
    controller.close()
    assert controller._on_activated not in m.on_profile_activated
    controller.close()  # must be safe to call twice


def test_adapter_close_detaches_controller():
    _ensure_qapp()
    from rumble.ui.auto_switch_adapter import AutoSwitchAdapter
    m = make_manager()
    adapter = AutoSwitchAdapter(m)
    assert adapter.controller._on_activated in m.on_profile_activated
    adapter.close()
    assert adapter.controller._on_activated not in m.on_profile_activated


def test_main_window_on_auto_switched_does_not_disrupt_editor_preview():
    # _on_auto_switched must only refresh "which profile is active" chrome —
    # NOT reopen/reload the editor. Regression check for a real bug found in
    # whole-branch review: the original implementation called _open_profile()
    # whenever the switched-to profile happened to match the loaded one,
    # which would silently discard a non-dirty live-preview divergence (e.g.
    # a Pre-Amp/Auto session toggle) even though nothing required it.
    _ensure_qapp()
    from PySide6.QtGui import QIcon
    from rumble.ui.main_window import MainWindow
    from rumble.ui.theme.theme_engine import ThemeEngine
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    m = make_manager()
    m.settings["sample_rate_mode"] = "manual"
    m.settings["sample_rate"] = 48000
    m.settings["show_safety_warning"] = False
    pid = m.create_profile("Loaded").id
    m.activate_profile(pid)

    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(_ensure_qapp())
    w = MainWindow(m, theme_engine, QIcon())
    try:
        assert w._loaded_profile_id == pid
        w._on_editor_preview_only()  # session-control divergence, not dirty
        assert w._preview_diverged is True
        assert w._dirty is False

        w._on_auto_switched(pid, "baseline")  # switches to the SAME profile already loaded

        assert w._loaded_profile_id == pid, "editor's loaded profile must not change"
        assert w._preview_diverged is True, (
            "auto-switch must not discard a non-dirty live-preview divergence")
    finally:
        w._auto_switch_adapter.close()
        w._format_adapter.stop()


def test_rules_from_settings_skips_malformed_entries_without_raising():
    # A single malformed rule dict (e.g. a hand-edited or corrupted
    # settings.json missing a required key) must not crash the whole app —
    # AutoSwitchAdapter parses rules unconditionally at MainWindow init.
    m = make_manager()
    p1 = m.create_profile("Good").id
    m.settings["auto_switch_rules"] = [
        Rule("app", "good.exe", p1).to_dict(),
        {"trigger": "app", "match": "bad.exe"},  # missing profile_id
        {"not": "a rule"},
        "not even a dict",
    ]
    rules = asw.rules_from_settings(m)
    assert len(rules) == 1
    assert rules[0].match == "good.exe"


def test_rules_roundtrip_through_settings():
    m = make_manager()
    rules = [Rule("app", "game.exe", "p1"), Rule("time", "22:00-07:00", "p2", enabled=False)]
    m.settings["auto_switch_rules"] = [r.to_dict() for r in rules]
    m.save()

    reloaded = ProfileManager(m.data_dir, config_path=m.config_path)
    loaded_rules = [Rule.from_dict(d) for d in reloaded.settings["auto_switch_rules"]]
    assert loaded_rules == rules


def test_auto_switch_settings_defaults():
    m = make_manager()
    assert m.settings["auto_switch_enabled"] is False
    assert m.settings["auto_switch_rules"] == []


# ── Task 5: UI — AutoSwitchAdapter + AutoSwitchDialog ──────────────────

def test_auto_switch_adapter_start_stop_idempotent():
    _ensure_qapp()
    from rumble.ui.auto_switch_adapter import AutoSwitchAdapter
    m = make_manager()
    adapter = AutoSwitchAdapter(m)
    adapter.start(); adapter.start(); adapter.stop(); adapter.stop()  # must never raise


def test_auto_switch_adapter_marshals_to_main_thread():
    # THE core Task-5 guarantee, mirroring selftest_device_format.py's
    # test_adapter_marshals_to_main_thread: SessionWatcher's callback fires on
    # a foreign (polling) thread, but the connected playing_changed slot MUST
    # run on the Qt main thread. Call the adapter's watcher-callback seam
    # directly from a background thread and prove delivery is queued, not
    # direct.
    from PySide6.QtCore import QCoreApplication, QThread
    from rumble.ui.auto_switch_adapter import AutoSwitchAdapter
    _ensure_qapp()

    m = make_manager()
    adapter = AutoSwitchAdapter(m)
    main_thread = QThread.currentThread()
    captured = {}
    fired = []

    def on_playing_changed(exes):
        captured["thread"] = QThread.currentThread()
        fired.append(exes)

    adapter.playing_changed.connect(on_playing_changed)

    bg_ran_inline = {"inline": False}

    def bg():
        adapter._on_state_from_watcher_thread({"game.exe"})
        bg_ran_inline["inline"] = "thread" in captured

    t = threading.Thread(target=bg)
    t.start()
    t.join()

    assert not bg_ran_inline["inline"], (
        "slot ran inline on the background thread — connection was Direct, "
        "not Queued")
    assert not fired, "queued slot ran before the event loop was spun"

    for _ in range(10):
        QCoreApplication.processEvents()
        if fired:
            break

    assert fired, "playing_changed slot never delivered after processEvents()"
    assert captured["thread"] is main_thread, (
        f"playing_changed slot ran on the wrong thread: {captured['thread']}")


def test_auto_switch_adapter_set_enabled_reflects_state():
    _ensure_qapp()
    from rumble.ui.auto_switch_adapter import AutoSwitchAdapter
    m = make_manager()
    adapter = AutoSwitchAdapter(m)
    adapter.set_enabled(True)
    assert adapter.state() == "active"
    adapter.set_enabled(False)
    assert adapter.state() == "off"


def test_auto_switch_adapter_is_holding_and_tick_drives_controller():
    _ensure_qapp()
    from rumble.ui.auto_switch_adapter import AutoSwitchAdapter
    m = make_manager()
    p_base = m.create_profile("AdapterBaseline").id
    p_rule = m.create_profile("AdapterTarget").id
    m.activate_profile(p_base)

    adapter = AutoSwitchAdapter(m)
    adapter.set_enabled(True)
    assert not adapter.is_holding()
    adapter.controller.engine = RuleEngine(
        [Rule("app", "game.exe", p_rule)], trigger_secs=0, linger_secs=0)
    adapter._playing = {"game.exe"}
    adapter._tick()
    assert adapter.is_holding()
    assert m.settings["last_active_profile"] == p_rule


def test_auto_switch_dialog_lists_existing_rules_in_order():
    _ensure_qapp()
    from rumble.ui.auto_switch_dialog import AutoSwitchDialog
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme
    m = make_manager()
    p1 = m.create_profile("Target One").id
    p2 = m.create_profile("Target Two").id
    m.settings["auto_switch_rules"] = [
        Rule("app", "one.exe", p1).to_dict(),
        Rule("app", "two.exe", p2).to_dict(),
    ]
    dlg = AutoSwitchDialog(m, get_theme(DEFAULT_THEME_NAME))
    assert dlg.list_widget.count() == 2
    assert "one.exe" in dlg.list_widget.item(0).text()
    assert "Target One" in dlg.list_widget.item(0).text()
    assert "two.exe" in dlg.list_widget.item(1).text()


def test_auto_switch_dialog_reorder_and_ok_persists():
    _ensure_qapp()
    from rumble.ui.auto_switch_dialog import AutoSwitchDialog
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme
    m = make_manager()
    p1 = m.create_profile("First").id
    p2 = m.create_profile("Second").id
    m.settings["auto_switch_rules"] = [
        Rule("app", "one.exe", p1).to_dict(),
        Rule("app", "two.exe", p2).to_dict(),
    ]
    dlg = AutoSwitchDialog(m, get_theme(DEFAULT_THEME_NAME))
    dlg.list_widget.setCurrentRow(1)
    dlg._move(-1)  # move "two.exe" rule up to the top
    assert dlg._rules[0].match == "two.exe"
    dlg._on_ok()
    saved = m.settings["auto_switch_rules"]
    assert saved[0]["match"] == "two.exe"
    assert saved[1]["match"] == "one.exe"


# ── Rumble 4.4: native conditional export button ───────────────────────

def test_export_native_conditional_no_device_rules_shows_message():
    _ensure_qapp()
    from rumble.ui import auto_switch_dialog as asd
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme
    m = make_manager()
    p1 = m.create_profile("Target").id
    m.settings["auto_switch_rules"] = [Rule("app", "one.exe", p1).to_dict()]
    dlg = asd.AutoSwitchDialog(m, get_theme(DEFAULT_THEME_NAME))

    shown = []
    real_information = asd.QMessageBox.information
    asd.QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a))
    try:
        dlg._export_native_conditional()  # app-only rule -> nothing translatable
    finally:
        asd.QMessageBox.information = real_information
    assert shown, "expected an information message box, none was shown"


def test_export_native_conditional_opens_preview_for_device_rule():
    _ensure_qapp()
    from rumble.core.audio_devices import ApoDevice
    from rumble.ui import auto_switch_dialog as asd
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme
    m = make_manager()
    p1 = m.create_profile("Target").id
    m.settings["auto_switch_rules"] = [Rule("device", "Speakers", p1).to_dict()]
    dlg = asd.AutoSwitchDialog(m, get_theme(DEFAULT_THEME_NAME))

    real_list_devices = asd.list_apo_devices
    real_exec = asd.NativeConditionalPreviewDialog.exec
    asd.list_apo_devices = lambda: [ApoDevice(guid="{x}", device_name="Speakers", connection_name="")]
    opened = []
    asd.NativeConditionalPreviewDialog.exec = lambda self: opened.append(self.text_edit.toPlainText())
    try:
        dlg._export_native_conditional()
    finally:
        asd.list_apo_devices = real_list_devices
        asd.NativeConditionalPreviewDialog.exec = real_exec
    assert opened, "expected the preview dialog to open"
    assert 'deviceName == "Speakers"' in opened[0]


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
