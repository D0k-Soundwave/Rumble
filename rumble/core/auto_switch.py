"""Phase 3 automatic profile switching — Qt-free core.

`RuleEngine` is pure logic: given the set of currently-playing executable
names, the default output device, and the current time, it decides which
profile (if any) an ordered list of `Rule`s holds active. `SessionWatcher`
(added in Task 3) polls pycaw's audio-session list on its own thread and
feeds `RuleEngine` the playing-exe set; neither ever touches Qt — a thin
`ui/` adapter (Task 5, copying `rumble/ui/device_format_adapter.py`) marshals
watcher callbacks to the main thread.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable

from rumble.core.apo_config import ProfileConfig, render_config_body
from rumble.core.audio_devices import ApoDevice, pattern_matches_text


@dataclass
class Rule:
    """One auto-switch rule: *trigger* fires on *match* -> activate *profile_id*.

    trigger: "app" (match = lowercase exe name), "device" (match = APO-style
    device pattern, see audio_devices.pattern_matches_text), or "time"
    (match = "HH:MM-HH:MM", may cross midnight).
    """
    trigger: str
    match: str
    profile_id: str
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        return cls(
            trigger=d["trigger"],
            match=d["match"],
            profile_id=d["profile_id"],
            enabled=d.get("enabled", True),
        )


def rules_from_settings(manager) -> list[Rule]:
    """Parse `manager.settings["auto_switch_rules"]` into `Rule` objects,
    skipping (never raising on) any malformed entry — e.g. a hand-edited or
    corrupted settings.json missing a required key. Every consumer
    (AutoSwitchAdapter construction and rule-reload) goes through this one
    seam instead of calling `Rule.from_dict()` directly, so a single bad
    entry degrades to "that rule is dropped" rather than crashing the whole
    app at startup — the same fail-safe convention malformed profile-config
    lines get (fall back to verbatim preservation, never crash the parser)."""
    rules = []
    for d in manager.settings.get("auto_switch_rules", []):
        try:
            rules.append(Rule.from_dict(d))
        except (KeyError, TypeError):
            print(f"WARNING: skipping malformed auto-switch rule: {d!r}", file=sys.stderr)
    return rules


def _time_in_window(window: str, hhmm: str) -> bool:
    """True when *hhmm* ("HH:MM") falls in *window* ("HH:MM-HH:MM"),
    handling windows that cross midnight (e.g. "22:00-07:00")."""
    start_s, end_s = window.split("-")
    start, end, now = start_s.strip(), end_s.strip(), hhmm.strip()
    if start <= end:
        return start <= now < end
    return now >= start or now < end


class RuleEngine:
    """Stateful evaluator over an ordered list of `Rule`s.

    Priority = list order, first match wins. App rules debounce: an app
    must play continuously for `trigger_secs` before its rule first fires,
    and once fired, the rule keeps holding for `linger_secs` after the app
    stops playing before releasing (so brief pauses/track changes don't
    flap). `now` is caller-injected monotonic seconds — no wall-clock reads
    here, so behaviour is fully deterministic under test.

    `trigger_secs=2.0` / `linger_secs=10.0` are UX judgment calls, not
    derived from a formula or standard — there's no physical quantity that
    says how long a "brief" system sound or a "brief" pause should be. Their
    real effect on the tool: too short a `trigger_secs` risks an audible
    profile switch (an EQ-curve/gain discontinuity through APO) triggered by
    an incidental short sound (e.g. a notification ding); too short a
    `linger_secs` risks flapping the profile away and back — each flip an
    audible discontinuity — during an ordinary pause/scrub/track-change gap.
    Left as a documented product choice rather than forced into a fake
    derivation.
    """

    def __init__(self, rules: list[Rule], trigger_secs: float = 2.0, linger_secs: float = 10.0):
        self.rules = rules
        self.trigger_secs = trigger_secs
        self.linger_secs = linger_secs
        self._app_state: dict[int, dict] = {}

    def _app_rule_matches(self, index: int, rule: Rule, playing_exes: set[str], now: float) -> bool:
        state = self._app_state.setdefault(index, {"first_seen": None, "last_seen": None, "held": False})
        playing = rule.match.lower() in playing_exes
        if playing:
            if state["first_seen"] is None:
                state["first_seen"] = now
            state["last_seen"] = now
            if state["held"] or (now - state["first_seen"] >= self.trigger_secs):
                state["held"] = True
                return True
            return False
        if state["held"] and state["last_seen"] is not None and (now - state["last_seen"] <= self.linger_secs):
            return True
        state["held"] = False
        state["first_seen"] = None
        return False

    def evaluate(self, playing_exes: set[str], default_device: str, now: float, clock_hhmm: str) -> str | None:
        playing_exes = {e.lower() for e in playing_exes}
        for index, rule in enumerate(self.rules):
            if not rule.enabled:
                continue
            if rule.trigger == "app":
                if self._app_rule_matches(index, rule, playing_exes, now):
                    return rule.profile_id
            elif rule.trigger == "device":
                if pattern_matches_text(rule.match, default_device):
                    return rule.profile_id
            elif rule.trigger == "time":
                if _time_in_window(rule.match, clock_hhmm):
                    return rule.profile_id
        return None


# ---------------------------------------------------------------------------
# Native APO conditional export: an alternative/complement to RuleEngine's
# own Include-pointer switching, for users who want a single self-contained
# config file with no RUMBLE process running.
# ---------------------------------------------------------------------------
#
# Equalizer APO's `If`/`ElseIf` expression language exposes runtime variables
# for the active device (`deviceName`, `connectionName`, `deviceGuid`) and
# format (`sampleRate`, `inputChannelCount`, `outputChannelCount`, `stage`)
# — confirmed against the official Configuration reference
# (sourceforge.net/p/equalizerapo/wiki/Configuration reference/, fetched
# 2026-07-09) — but nothing reflecting "which process is currently playing
# audio" or wall-clock time. So only `Rule.trigger == "device"` translates to
# a native conditional; `app` and `time` rules are reported back as
# untranslatable rather than silently dropped, matching this project's
# fail-safe convention for anything that can't be represented losslessly.
#
# A `Rule.match` for a device trigger is an APO `Device:`-style pattern (see
# `ApoDevice.pattern` — "deviceName connectionName", matched word-wise as a
# substring per `pattern_matches_text`). That is a *different* mechanism from
# the `If:` expression's `deviceName`/`connectionName` variables, which take
# exact-equality comparison (confirmed: `==`, `!=`, `<`, `>`, `<=`, `>=`, and
# the keyword operators `and`/`or`/`xor`/`not` — no `&&`/`||`). So the pattern
# text can't just be quoted verbatim; the matching real device (by comparing
# `ApoDevice.pattern` values) is looked up so its `device_name`/
# `connection_name` fields can be compared individually.


@dataclass
class NativeConditionalResult:
    """Output of `generate_native_conditional()`: the generated block text
    (empty if no rule translated) plus which rules did/didn't make it in, so
    a caller can tell the user exactly what was skipped and why."""
    text: str = ""
    translated: list[Rule] = field(default_factory=list)
    untranslatable: list[Rule] = field(default_factory=list)


def _device_condition(pattern: str, devices: list[ApoDevice]) -> str | None:
    """Translate a device rule's `Device:`-style match pattern into a native
    `deviceName`/`connectionName` equality expression, by finding the real
    endpoint the pattern was built from (`ApoDevice.pattern` is an exact
    match of the pattern text, not a fuzzy one). Returns None when no such
    device is known, or when its name contains a double quote (no confirmed
    escape syntax for that in APO string literals, so it can't be safely
    embedded rather than guessed)."""
    for device in devices:
        if device.pattern != pattern:
            continue
        if '"' in device.device_name or '"' in device.connection_name:
            return None
        clauses = []
        if device.device_name:
            clauses.append(f'deviceName == "{device.device_name}"')
        if device.connection_name:
            clauses.append(f'connectionName == "{device.connection_name}"')
        return " and ".join(clauses) if clauses else None
    return None


def _profile_body_lines(profile: ProfileConfig) -> list[str]:
    """Render just a profile's channel-section body (filters/GraphicEQ/
    per-channel preamp) — no Device:/Stage:/Eval:/etc. header lines, since
    this is meant to be nested inside an `If:`/`ElseIf:`/`Else:` branch,
    indented 2 spaces to match `create_profile_with_convolution()`'s existing
    conditional-block style."""
    reduced = ProfileConfig(device="", preamp=profile.preamp,
                             channel_sections=profile.channel_sections)
    body = render_config_body(reduced)
    # Blank lines dropped, not just left unindented: parse_config() discards
    # blank lines even inside a conditional block (its blank-line skip runs
    # before the in-conditional accumulation), so keeping them would silently
    # fail to round-trip -- and they add nothing inside an indented body.
    return [f"  {ln}" for ln in body.splitlines() if ln.strip()]


def generate_native_conditional(
    rules: list[Rule],
    devices: list[ApoDevice],
    profile_lookup: "Callable[[str], ProfileConfig | None]",
    baseline_profile_id: str | None = None,
) -> NativeConditionalResult:
    """Translate the enabled `device`-trigger rules in *rules* (priority =
    list order, matching `RuleEngine.evaluate()`) into one native
    `If`/`ElseIf`/`Else`/`EndIf` block. `app` and `time` rules can't be
    expressed (see module docstring above) and come back in `.untranslatable`
    instead of being dropped silently. *baseline_profile_id*, if given, backs
    an `Else:` branch (typically the user's last-manually-chosen profile) —
    omitted when None, matching "no native fallback configured" rather than
    guessing one.
    """
    translated: list[Rule] = []
    untranslatable: list[Rule] = []
    lines: list[str] = []

    for rule in rules:
        if not rule.enabled:
            continue
        if rule.trigger != "device":
            untranslatable.append(rule)
            continue
        condition = _device_condition(rule.match, devices)
        profile = profile_lookup(rule.profile_id) if condition is not None else None
        if condition is None or profile is None:
            untranslatable.append(rule)
            continue
        keyword = "If" if not translated else "ElseIf"
        lines.append(f"{keyword}: {condition}")
        lines.extend(_profile_body_lines(profile))
        translated.append(rule)

    if not translated:
        return NativeConditionalResult(untranslatable=untranslatable)

    baseline = profile_lookup(baseline_profile_id) if baseline_profile_id else None
    if baseline is not None:
        lines.append("Else:")
        lines.extend(_profile_body_lines(baseline))
    lines.append("EndIf:")

    return NativeConditionalResult(
        text="\n".join(lines), translated=translated, untranslatable=untranslatable)


# ---------------------------------------------------------------------------
# SessionWatcher: polls pycaw audio sessions for actively-playing processes
# ---------------------------------------------------------------------------
#
# Unlike device endpoints (DeviceFormatWatcher, rumble/core/device_format.py),
# audio sessions expose no push-notification API for "started/stopped
# playing" — so this watcher polls on its own daemon thread instead. Like
# every other watcher in this codebase it is strictly Qt-free: `on_state` is
# called from the polling thread, never the Qt main thread; a `ui/` adapter
# (Task 5, copying `rumble/ui/device_format_adapter.py`) marshals it via a
# queued signal.


def _get_sessions():
    """Deferred import so `import rumble.core.auto_switch` never requires a
    working pycaw/comtypes stack, matching device_format.py's convention. A
    bare module-level function (rather than an inline import in
    `_poll_once`) so tests can monkeypatch this exact seam."""
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetAllSessions()


def _active_exes_from_sessions(sessions) -> set[str]:
    """Pure extraction: lowercase exe names of sessions whose state is
    ACTIVE (state == 1, `pycaw.constants.AudioSessionState.Active`) — i.e.
    actually playing, not merely open. Sessions with no process (system
    sounds, or a process that has since exited) or a non-active state are
    excluded. Never raises: a session whose `.Process`/`.State` access fails
    is skipped rather than aborting the whole scan."""
    exes: set[str] = set()
    for session in sessions:
        try:
            if session.State != 1:
                continue
            process = session.Process
            if process is None:
                continue
            name = process.name()
        except Exception:
            continue
        if name:
            exes.add(name.lower())
    return exes


class SessionWatcher:
    """Polls the pycaw audio-session list on its own daemon thread and calls
    `on_state(playing_exes)` whenever the set of actively-playing executable
    names CHANGES (not on every poll tick — callers only care about
    transitions).

    Qt-free by design: `on_state` fires on the polling thread, never the Qt
    main thread. `.start()`/`.stop()` are idempotent and never raise, even
    if pycaw/comtypes is unavailable or every poll tick fails — matching
    DeviceFormatWatcher's resilience contract.

    `poll_secs=1.0` has one real constraint tying it to `RuleEngine`'s own
    `trigger_secs` (2.0 by default): a poll interval that isn't meaningfully
    smaller than `trigger_secs` would make the trigger debounce theater — a
    single poll tick could jump the elapsed-playing time straight past the
    whole trigger window, defeating the point of requiring several samples
    of "still playing" before a rule fires. At 1.0s poll / 2.0s trigger,
    `RuleEngine` gets >=2 samples across the trigger window, the minimum for
    the debounce to mean anything. Beyond that constraint, the absolute
    value is a latency-vs-overhead tradeoff (worst-case detection latency is
    one `poll_secs`; each tick is a lightweight pycaw session-list COM call)
    with no single "correct" number.
    """

    def __init__(self, on_state: "Callable[[set[str]], None]", poll_secs: float = 1.0):
        self._on_state = on_state
        self._poll_secs = poll_secs
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_state: set[str] | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._poll_secs + 2.0)

    def _run(self) -> None:
        try:
            import comtypes

            try:
                comtypes.CoInitialize()
            except OSError:
                pass  # already initialized on this thread — fine
        except Exception:
            pass
        try:
            while not self._stop_event.is_set():
                self._poll_once()
                self._stop_event.wait(self._poll_secs)
        finally:
            try:
                import comtypes

                comtypes.CoUninitialize()
            except Exception:
                pass

    def _poll_once(self) -> None:
        try:
            exes = _active_exes_from_sessions(_get_sessions())
        except Exception as exc:
            print(f"WARNING: SessionWatcher poll failed: {exc}", file=sys.stderr)
            return
        if exes != self._last_state:
            self._last_state = exes
            try:
                self._on_state(exes)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# AutoSwitchController: glue between RuleEngine and ProfileManager
# ---------------------------------------------------------------------------


class AutoSwitchController:
    """Drives `ProfileManager.activate_profile()` from `RuleEngine.evaluate()`
    results, called from `tick()` wherever per-poll state arrives
    (SessionWatcher's callback and the Phase 2 device-format watcher, both
    via their Qt adapters — this class itself is Qt-free).

    Baseline semantics: `baseline` is the profile that was active at the
    moment a rule FIRST took hold (captured before switching away from it),
    and is what gets restored once no rule holds anymore — so auto-switching
    always returns to whatever the user had manually chosen. A manual
    activation that happens while no rule currently holds (observed via
    `ProfileManager.on_profile_activated`, distinguishing the user's own
    action from this controller's own `activate_profile()` calls via the
    `_initiating` flag) updates `baseline` — the user's choice always wins.

    Suspended while `set_editor_dirty(True)` (an unsaved editor session must
    never be yanked out from under the user by a mid-game switch) or while
    disabled — `tick()` is then a no-op and `state()` reports why.
    """

    def __init__(self, manager, engine: RuleEngine,
                 on_switched: "Callable[[str, str], None] | None" = None):
        self.manager = manager
        self.engine = engine
        self.on_switched = on_switched
        self._enabled = True
        self._dirty = False
        self._baseline: str | None = manager.settings.get("last_active_profile")
        self._held_target: str | None = None
        self._initiating = False
        manager.on_profile_activated.append(self._on_activated)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._held_target = None

    def set_editor_dirty(self, dirty: bool) -> None:
        self._dirty = dirty

    def state(self) -> str:
        if not self._enabled:
            return "off"
        if self._dirty:
            return "suspended-dirty"
        return "active"

    def is_holding(self) -> bool:
        """True while some rule currently holds the active profile — drives
        the status-bar "Auto" badge (shown only while a rule is in effect,
        not merely while auto-switching is enabled)."""
        return self._held_target is not None

    def close(self) -> None:
        """Detach from `manager.on_profile_activated`. Required (not just
        `set_enabled(False)`) whenever this controller's owner is being
        discarded for good against a `ProfileManager` that outlives it — the
        UI's in-process interface-size rebuild recreates MainWindow (and so
        a brand-new AutoSwitchController) against the SAME long-lived
        manager instance, so without this the old controller's callback
        would stay in that list forever, accumulating one dead subscriber
        per rebuild. Safe to call more than once."""
        try:
            self.manager.on_profile_activated.remove(self._on_activated)
        except ValueError:
            pass

    def _on_activated(self, profile_id: str) -> None:
        # Any activation NOT initiated by this controller is a manual choice
        # (the user picking a profile directly) — always becomes the new
        # baseline, even while a rule currently holds: the user's pick isn't
        # fought (tick() only acts on target transitions), but a later
        # release should restore to what the user most recently chose, not
        # to whatever was active before the rule first took hold.
        if self._initiating:
            return  # our own activate_profile() call, not a manual choice
        self._baseline = profile_id

    def _activate(self, profile_id: str, reason: str) -> None:
        self._initiating = True
        try:
            self.manager.activate_profile(profile_id)
        finally:
            self._initiating = False
        if self.on_switched is not None:
            self.on_switched(profile_id, reason)

    def tick(self, playing_exes, default_device: str, now: float, clock_hhmm: str) -> None:
        """Evaluate current state and switch/restore as needed. No-op while
        suspended (dirty editor) or disabled."""
        if self._dirty or not self._enabled:
            return
        target = self.engine.evaluate(set(playing_exes), default_device, now, clock_hhmm)
        current = self.manager.settings.get("last_active_profile")
        if target is not None:
            if self._held_target is None:
                self._baseline = current  # capture baseline at the moment a rule first holds
            if target != self._held_target:
                self._held_target = target
                if target != current:
                    self._activate(target, "rule")
        else:
            if self._held_target is not None:
                self._held_target = None
                if self._baseline is not None and self._baseline != current:
                    self._activate(self._baseline, "baseline")
