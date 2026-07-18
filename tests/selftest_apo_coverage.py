"""Self-test for A1 (docs/superpowers/plans/2026-07-11-adaptive-frontend-
roadmap.md "APO-coverage health check"): warning when the default playback
device isn't APO-hooked.

Covers: GUID extraction from pycaw ids, the hooked/not-hooked/undeterminable
decision matrix (faked enumeration, no real COM/registry), configurator-path
discovery, dismissal persistence, and the banner's show/hide wiring in
MainWindow (offscreen Qt).

Run:  python tests/selftest_apo_coverage.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_path, audio_devices
from rumble.core.audio_devices import ApoDevice
from rumble.core.profile_manager import ProfileManager

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# extract_endpoint_guid — pure
# ---------------------------------------------------------------------------

def test_extract_guid_from_prefixed_id():
    check(audio_devices.extract_endpoint_guid("{0.0.0.00000000}.{ABCDEF01-1234-5678-9ABC-DEF012345678}")
          == "{ABCDEF01-1234-5678-9ABC-DEF012345678}",
          "extracts the trailing brace-wrapped GUID from a prefixed pycaw id")


def test_extract_guid_bare_guid_passthrough():
    check(audio_devices.extract_endpoint_guid("{ABCDEF01-1234-5678-9ABC-DEF012345678}")
          == "{ABCDEF01-1234-5678-9ABC-DEF012345678}",
          "a bare {guid} id (no prefix) is returned as-is")


def test_extract_guid_none_input():
    check(audio_devices.extract_endpoint_guid(None) is None, "None input -> None")


def test_extract_guid_empty_input():
    check(audio_devices.extract_endpoint_guid("") is None, "empty string -> None")


def test_extract_guid_no_guid_shape():
    check(audio_devices.extract_endpoint_guid("not-a-guid-at-all") is None,
          "a string with no trailing {...} token -> None (never guessed)")


# ---------------------------------------------------------------------------
# default_render_guid — COM seam smoke test
# ---------------------------------------------------------------------------

def test_default_render_guid_never_raises():
    result = audio_devices.default_render_guid()
    check(result is None or isinstance(result, str),
          "default_render_guid() never raises; returns None or a GUID string")


# ---------------------------------------------------------------------------
# default_device_hooked — the hooked / not-hooked / undeterminable matrix,
# with default_render_guid() and list_apo_devices() faked (no real COM/registry).
# ---------------------------------------------------------------------------

def _with_fakes(render_guid, devices, fn):
    real_guid_fn = audio_devices.default_render_guid
    real_list_fn = audio_devices.list_apo_devices
    audio_devices.default_render_guid = lambda: render_guid
    audio_devices.list_apo_devices = lambda: devices
    try:
        fn()
    finally:
        audio_devices.default_render_guid = real_guid_fn
        audio_devices.list_apo_devices = real_list_fn


def test_hooked_true_when_guid_matches_a_render_device():
    devices = [ApoDevice(guid="{AAAA}", flow="Render", state=1)]
    _with_fakes("{AAAA}", devices,
                lambda: check(audio_devices.default_device_hooked() is True,
                               "default endpoint's GUID matches a hooked Render device -> True"))


def test_not_hooked_when_guid_absent():
    devices = [ApoDevice(guid="{AAAA}", flow="Render", state=1)]
    _with_fakes("{ZZZZ}", devices,
                lambda: check(audio_devices.default_device_hooked() is False,
                               "default endpoint's GUID matches no hooked device -> False"))


def test_not_hooked_when_no_devices_hooked_at_all():
    _with_fakes("{ZZZZ}", [],
                lambda: check(audio_devices.default_device_hooked() is False,
                               "no APO-hooked devices at all -> False, not None"))


def test_undeterminable_when_no_default_guid():
    devices = [ApoDevice(guid="{AAAA}", flow="Render", state=1)]
    _with_fakes(None, devices,
                lambda: check(audio_devices.default_device_hooked() is None,
                               "no default-device GUID (COM failure) -> None, never guessed"))


def test_guid_comparison_is_case_insensitive():
    devices = [ApoDevice(guid="{aaaa-bbbb}", flow="Render", state=1)]
    _with_fakes("{AAAA-BBBB}", devices,
                lambda: check(audio_devices.default_device_hooked() is True,
                               "GUID comparison ignores case"))


def test_capture_flow_match_does_not_count_as_hooked():
    # Same GUID string, but only registered under Capture -- the default
    # playback query must not be satisfied by a capture-side registration.
    devices = [ApoDevice(guid="{AAAA}", flow="Capture", state=1)]
    _with_fakes("{AAAA}", devices,
                lambda: check(audio_devices.default_device_hooked() is False,
                               "a Capture-flow entry with the same GUID does not count as hooked"))


# ---------------------------------------------------------------------------
# get_configurator_path — candidate search order, faked install dir
# ---------------------------------------------------------------------------

def test_configurator_path_prefers_device_selector():
    tmp = Path(tempfile.mkdtemp(prefix="rumble_apocov_"))
    (tmp / "DeviceSelector.exe").write_bytes(b"")
    (tmp / "Configurator.exe").write_bytes(b"")
    real_install = apo_path.get_install_path
    apo_path.get_install_path = lambda: tmp
    try:
        check(apo_path.get_configurator_path() == tmp / "DeviceSelector.exe",
              "DeviceSelector.exe is preferred when both candidates exist")
    finally:
        apo_path.get_install_path = real_install


def test_configurator_path_falls_back_to_configurator_exe():
    tmp = Path(tempfile.mkdtemp(prefix="rumble_apocov_"))
    (tmp / "Configurator.exe").write_bytes(b"")
    real_install = apo_path.get_install_path
    apo_path.get_install_path = lambda: tmp
    try:
        check(apo_path.get_configurator_path() == tmp / "Configurator.exe",
              "falls back to Configurator.exe when DeviceSelector.exe is absent")
    finally:
        apo_path.get_install_path = real_install


def test_configurator_path_none_when_neither_exists():
    tmp = Path(tempfile.mkdtemp(prefix="rumble_apocov_"))
    real_install = apo_path.get_install_path
    apo_path.get_install_path = lambda: tmp
    try:
        check(apo_path.get_configurator_path() is None,
              "neither candidate present -> None")
    finally:
        apo_path.get_install_path = real_install


def test_configurator_path_none_when_apo_not_installed():
    real_install = apo_path.get_install_path
    apo_path.get_install_path = lambda: None
    try:
        check(apo_path.get_configurator_path() is None,
              "APO not installed at all -> None")
    finally:
        apo_path.get_install_path = real_install


# ---------------------------------------------------------------------------
# ProfileManager dismissal persistence
# ---------------------------------------------------------------------------

def _temp_manager() -> ProfileManager:
    tmp = Path(tempfile.mkdtemp(prefix="rumble_apocov_mgr_"))
    return ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")


def test_dismiss_persists_across_reload():
    m = _temp_manager()
    check(m.is_apo_coverage_dismissed("{AAAA}") is False, "nothing dismissed initially")
    m.dismiss_apo_coverage_warning("{AAAA}")
    check(m.is_apo_coverage_dismissed("{AAAA}") is True, "dismissal visible immediately")
    m2 = ProfileManager(data_dir=m.data_dir, config_path=m.config_path)
    check(m2.is_apo_coverage_dismissed("{AAAA}") is True, "dismissal survives reload")
    check(m2.is_apo_coverage_dismissed("{BBBB}") is False, "a different GUID stays undismissed")


def test_dismiss_is_idempotent():
    m = _temp_manager()
    m.dismiss_apo_coverage_warning("{AAAA}")
    m.dismiss_apo_coverage_warning("{AAAA}")
    check(m.settings["apo_coverage_dismissed_guids"].count("{AAAA}") == 1,
          "dismissing the same GUID twice does not duplicate it")


def test_dismiss_none_guid_never_treated_as_dismissed():
    m = _temp_manager()
    check(m.is_apo_coverage_dismissed(None) is False,
          "an undeterminable (None) endpoint identity is never 'dismissed'")


# ---------------------------------------------------------------------------
# ApoCoverageBanner widget — offscreen Qt
# ---------------------------------------------------------------------------

def _ensure_qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_banner_emits_configurator_requested():
    _ensure_qapp()
    from rumble.ui.apo_coverage_banner import ApoCoverageBanner
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    banner = ApoCoverageBanner(get_theme(DEFAULT_THEME_NAME))
    calls = []
    banner.configurator_requested.connect(lambda: calls.append(1))
    banner.configurator_button.click()
    check(calls == [1], "clicking the fix button emits configurator_requested")


def test_banner_emits_dismissed():
    _ensure_qapp()
    from rumble.ui.apo_coverage_banner import ApoCoverageBanner
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    banner = ApoCoverageBanner(get_theme(DEFAULT_THEME_NAME))
    calls = []
    banner.dismissed.connect(lambda: calls.append(1))
    banner.dismiss_button.click()
    check(calls == [1], "clicking Dismiss emits dismissed")


def test_banner_has_a11y_text():
    _ensure_qapp()
    from rumble.ui.apo_coverage_banner import ApoCoverageBanner
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    banner = ApoCoverageBanner(get_theme(DEFAULT_THEME_NAME))
    check(bool(banner.configurator_button.accessibleDescription()),
          "fix button has accessible description text")
    check(bool(banner.dismiss_button.accessibleDescription()),
          "dismiss button has accessible description text")


# ---------------------------------------------------------------------------
# MainWindow._check_apo_coverage() — full show/hide/dismiss wiring
# ---------------------------------------------------------------------------

def _fresh_window_manager():
    from rumble.core.profile_manager import make_flat_config
    tmp = Path(tempfile.mkdtemp(prefix="rumble_apocov_win_"))
    manager = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    manager.ensure_first_run()
    created = manager.create_profile("Alpha", config=make_flat_config(5))
    manager.activate_profile(created.id)
    return manager


def _make_window(manager):
    from PySide6.QtGui import QIcon
    from rumble.ui.main_window import MainWindow
    from rumble.ui.theme.theme_engine import ThemeEngine
    from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

    app = _ensure_qapp()
    theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    theme_engine.apply(app)
    return MainWindow(manager, theme_engine, QIcon())


def test_banner_visible_when_not_hooked_and_not_dismissed():
    manager = _fresh_window_manager()
    real_hooked = audio_devices.default_device_hooked
    real_guid = audio_devices.default_render_guid
    audio_devices.default_device_hooked = lambda: False
    audio_devices.default_render_guid = lambda: "{WIN-AAAA}"
    try:
        w = _make_window(manager)
        check(not w.apo_banner.isHidden(), "banner shown: not hooked, not dismissed")
        w.tray_icon.hide()
    finally:
        audio_devices.default_device_hooked = real_hooked
        audio_devices.default_render_guid = real_guid


def test_banner_hidden_when_hooked():
    manager = _fresh_window_manager()
    real_hooked = audio_devices.default_device_hooked
    real_guid = audio_devices.default_render_guid
    audio_devices.default_device_hooked = lambda: True
    audio_devices.default_render_guid = lambda: "{WIN-BBBB}"
    try:
        w = _make_window(manager)
        check(w.apo_banner.isHidden(), "banner hidden: default device is hooked")
        w.tray_icon.hide()
    finally:
        audio_devices.default_device_hooked = real_hooked
        audio_devices.default_render_guid = real_guid


def test_banner_hidden_when_undeterminable():
    manager = _fresh_window_manager()
    real_hooked = audio_devices.default_device_hooked
    real_guid = audio_devices.default_render_guid
    audio_devices.default_device_hooked = lambda: None
    audio_devices.default_render_guid = lambda: None
    try:
        w = _make_window(manager)
        check(w.apo_banner.isHidden(), "banner hidden when hooked-state can't be determined")
        w.tray_icon.hide()
    finally:
        audio_devices.default_device_hooked = real_hooked
        audio_devices.default_render_guid = real_guid


def test_banner_hidden_when_endpoint_already_dismissed():
    manager = _fresh_window_manager()
    manager.dismiss_apo_coverage_warning("{WIN-CCCC}")
    real_hooked = audio_devices.default_device_hooked
    real_guid = audio_devices.default_render_guid
    audio_devices.default_device_hooked = lambda: False
    audio_devices.default_render_guid = lambda: "{WIN-CCCC}"
    try:
        w = _make_window(manager)
        check(w.apo_banner.isHidden(),
              "not hooked but this endpoint's warning was previously dismissed -> hidden")
        w.tray_icon.hide()
    finally:
        audio_devices.default_device_hooked = real_hooked
        audio_devices.default_render_guid = real_guid


def test_dismiss_button_hides_banner_and_persists():
    manager = _fresh_window_manager()
    real_hooked = audio_devices.default_device_hooked
    real_guid = audio_devices.default_render_guid
    audio_devices.default_device_hooked = lambda: False
    audio_devices.default_render_guid = lambda: "{WIN-DDDD}"
    try:
        w = _make_window(manager)
        check(not w.apo_banner.isHidden(), "sanity: banner starts visible")
        w.apo_banner.dismiss_button.click()
        check(w.apo_banner.isHidden(), "dismiss button hides the banner immediately")
        check(manager.is_apo_coverage_dismissed("{WIN-DDDD}"),
              "dismiss button persists the dismissal for this endpoint's GUID")
        w.tray_icon.hide()
    finally:
        audio_devices.default_device_hooked = real_hooked
        audio_devices.default_render_guid = real_guid


def test_format_changed_recheck_updates_banner():
    # Simulates DeviceFormatAdapter.format_changed: starts hooked (banner
    # hidden), then the "device" becomes un-hooked and the same recheck path
    # _on_device_format_changed uses must show the banner.
    manager = _fresh_window_manager()
    real_hooked = audio_devices.default_device_hooked
    real_guid = audio_devices.default_render_guid
    audio_devices.default_device_hooked = lambda: True
    audio_devices.default_render_guid = lambda: "{WIN-EEEE}"
    try:
        w = _make_window(manager)
        check(w.apo_banner.isHidden(), "sanity: starts hooked, banner hidden")
        audio_devices.default_device_hooked = lambda: False
        w._on_device_format_changed()
        check(not w.apo_banner.isHidden(), "recheck on format_changed shows the banner once un-hooked")
        w.tray_icon.hide()
    finally:
        audio_devices.default_device_hooked = real_hooked
        audio_devices.default_render_guid = real_guid


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for fmsg in FAILURES:
            print(f"  - {fmsg}")
        sys.exit(1)
    print("ALL CHECKS PASSED")
    sys.exit(0)
