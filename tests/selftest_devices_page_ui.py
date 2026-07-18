"""Selftest: Settings -> Devices page (D1b per-device profile assignments UI).

Mirrors selftest_listener_settings_ui.py's shape. Covers: the page lists
faked APO-hooked endpoints (name + connection, active-state note); a combo
selection collects into device_profile_assignments and reaches the
settings_saved payload; picking "Follow active profile" clears an
assignment; a stale (endpoint no longer present) assignment is shown
greyed with a clear affordance, never silently dropped; and the
settings-save path (settings.update() + refresh_render_layers(), as
main_window._open_settings() does) actually rewrites the dispatch.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QLabel, QTabWidget

from rumble.core import apo_config, audio_devices
from rumble.core.audio_devices import ApoDevice
from rumble.core.profile_manager import ACTIVE_POINTER_FILENAME, DEFAULT_SETTINGS, ProfileManager
from rumble.ui.settings_dialog import CATEGORIES, SettingsDialog
from rumble.ui.theme.tokens import THEMES, DEFAULT_THEME_NAME

_app = QApplication.instance() or QApplication([])

GUID_A = "{AAAAAAAA-0000-0000-0000-000000000001}"
GUID_B = "{BBBBBBBB-0000-0000-0000-000000000002}"
GUID_STALE = "{CCCCCCCC-0000-0000-0000-000000000003}"

DEVICE_A = ApoDevice(guid=GUID_A, device_name="Speakers", connection_name="Creative BT-W6",
                     flow="Render", state=1)
DEVICE_B = ApoDevice(guid=GUID_B, device_name="Speakers", connection_name="FIIO K13 R2R",
                     flow="Render", state=0)  # inactive

GUID_MIC = "{DDDDDDDD-0000-0000-0000-000000000004}"
GUID_MIC_UNHOOKED = "{EEEEEEEE-0000-0000-0000-000000000005}"
DEVICE_MIC = ApoDevice(guid=GUID_MIC, device_name="Microphone", connection_name="USB Mic",
                       flow="Capture", state=1)
DEVICE_MIC_UNHOOKED = ApoDevice(guid=GUID_MIC_UNHOOKED, device_name="Microphone",
                                connection_name="Webcam Mic", flow="Capture", state=1)


def _mic_settings(**overrides) -> dict:
    """DEFAULT_SETTINGS with microphone profiles SHOWN. show_microphone_profiles
    defaults to off, and while it is off the Capture tab is not built at all
    (microphone EQ being disabled outright, not merely hidden -- see
    test_capture_tab_absent_when_microphone_profiles_hidden), so every test
    below that expects a Capture tab has to opt in."""
    return {**DEFAULT_SETTINGS, "show_microphone_profiles": True, **overrides}


def test_devices_category_present():
    assert "Devices" in CATEGORIES


def test_page_lists_faked_endpoints_with_active_state():
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(
            dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A, DEVICE_B],
            device_profile_options=[("daily", "Daily")])
        assert set(dlg._device_assignment_combos) == {GUID_A, GUID_B}
        combo_a = dlg._device_assignment_combos[GUID_A]
        assert combo_a.itemText(0) == "Follow active profile"
        assert combo_a.count() == 2  # follow + "Daily"
        assert combo_a.currentIndex() == 0  # unassigned defaults to Follow

        page = dlg.stack.widget(CATEGORIES.index("Devices"))
        texts = [w.text() for w in page.findChildren(QLabel)]
        assert any("Creative BT-W6" in t for t in texts), "device_name + connection_name shown"
        assert any("FIIO K13 R2R" in t and "inactive" in t for t in texts), \
            "inactive endpoint gets an active-state note"


def test_combo_selection_collects_into_settings_saved():
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(
            dict(DEFAULT_SETTINGS), THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            device_profile_options=[("daily", "Daily"), ("night", "Night")])
        combo = dlg._device_assignment_combos[GUID_A]
        combo.setCurrentIndex(combo.findData("night"))

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()

        assert emitted[0]["device_profile_assignments"] == {GUID_A: "night"}


def test_follow_active_profile_clears_assignment():
    with tempfile.TemporaryDirectory() as tmp:
        settings = dict(DEFAULT_SETTINGS)
        settings["device_profile_assignments"] = {GUID_A: "daily"}
        dlg = SettingsDialog(
            settings, THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            device_profile_options=[("daily", "Daily")])
        combo = dlg._device_assignment_combos[GUID_A]
        assert combo.currentData() == "daily"
        combo.setCurrentIndex(0)  # Follow active profile

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()

        assert emitted[0]["device_profile_assignments"] == {}


def test_stale_assignment_shown_greyed_and_clearable():
    with tempfile.TemporaryDirectory() as tmp:
        settings = dict(DEFAULT_SETTINGS)
        settings["device_profile_assignments"] = {GUID_A: "daily", GUID_STALE: "night"}
        dlg = SettingsDialog(
            settings, THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],  # GUID_STALE's endpoint isn't present
            device_profile_options=[("daily", "Daily"), ("night", "Night")])

        assert GUID_STALE in dlg._device_stale_rows, "stale assignment surfaced, not silently dropped"
        assert GUID_STALE not in dlg._device_assignment_combos

        # Saving without touching anything must not drop the stale entry.
        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()
        assert emitted[0]["device_profile_assignments"] == {GUID_A: "daily", GUID_STALE: "night"}


def test_clear_stale_assignment_removes_it():
    with tempfile.TemporaryDirectory() as tmp:
        settings = dict(DEFAULT_SETTINGS)
        settings["device_profile_assignments"] = {GUID_A: "daily", GUID_STALE: "night"}
        dlg = SettingsDialog(
            settings, THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            device_profile_options=[("daily", "Daily"), ("night", "Night")])

        dlg._on_clear_stale_device(GUID_STALE)
        assert GUID_STALE not in dlg._working["device_profile_assignments"]
        assert GUID_STALE not in dlg._device_stale_rows

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()
        assert emitted[0]["device_profile_assignments"] == {GUID_A: "daily"}


def test_deleted_profile_assignment_surfaced_not_silently_dropped():
    """D1c: a device whose ENDPOINT is still hooked but whose ASSIGNED
    PROFILE was deleted must not silently fall back to "Follow active
    profile" with no trace -- unlike the stale-endpoint case (which has no
    live combo to show anything in), this combo IS live, so the dead id
    gets its own placeholder entry and stays selected until the user picks
    something else."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = dict(DEFAULT_SETTINGS)
        settings["device_profile_assignments"] = {GUID_A: "ghost-profile"}
        dlg = SettingsDialog(
            settings, THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            device_profile_options=[("daily", "Daily")])  # "ghost-profile" is NOT among these

        combo = dlg._device_assignment_combos[GUID_A]
        assert combo.currentData() == "ghost-profile", \
            "the dead assignment stays visibly selected, not silently reset to Follow"
        assert combo.currentText() != "Follow active profile"
        assert combo.count() == 3  # Follow + Daily + the deleted-profile placeholder

        # Untouched Save keeps the dead assignment as-is (informed, not silent).
        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()
        assert emitted[0]["device_profile_assignments"] == {GUID_A: "ghost-profile"}


def test_deleted_profile_assignment_cleared_by_explicit_reselection():
    with tempfile.TemporaryDirectory() as tmp:
        settings = dict(DEFAULT_SETTINGS)
        settings["device_profile_assignments"] = {GUID_A: "ghost-profile"}
        dlg = SettingsDialog(
            settings, THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            device_profile_options=[("daily", "Daily")])
        combo = dlg._device_assignment_combos[GUID_A]
        combo.setCurrentIndex(0)  # explicit "Follow active profile"

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()
        assert emitted[0]["device_profile_assignments"] == {}


def test_capture_tab_lists_faked_endpoints_with_combo():
    """D2: the Capture tab shares the same per-endpoint combo mechanism as
    the Playback tab, populated from apo_capture_devices (a separate list
    from apo_render_devices, but the same self._device_assignment_combos
    dict, keyed by GUID)."""
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(
            _mic_settings(), THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            apo_capture_devices=[DEVICE_MIC],
            device_profile_options=[("daily", "Daily"), ("voice", "Voice - Clean HPF")])
        assert set(dlg._device_assignment_combos) == {GUID_A, GUID_MIC}, \
            "playback and capture combos share one dict, keyed by GUID"
        combo_mic = dlg._device_assignment_combos[GUID_MIC]
        assert combo_mic.itemText(0) == "Follow active profile"
        assert combo_mic.count() == 3  # follow + Daily + Voice - Clean HPF
        assert combo_mic.currentIndex() == 0

        page = dlg.stack.widget(CATEGORIES.index("Devices"))
        texts = [w.text() for w in page.findChildren(QLabel)]
        assert any("USB Mic" in t for t in texts), "capture device name+connection shown"


def test_capture_combo_selection_collects_into_settings_saved():
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(
            _mic_settings(), THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_capture_devices=[DEVICE_MIC],
            device_profile_options=[("voice", "Voice - Clean HPF")])
        combo = dlg._device_assignment_combos[GUID_MIC]
        combo.setCurrentIndex(combo.findData("voice"))

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()

        assert emitted[0]["device_profile_assignments"] == {GUID_MIC: "voice"}


def test_capture_tab_notes_unhooked_microphones():
    """D2's A1 extension: a capture endpoint Windows reports but Equalizer
    APO hasn't hooked yet is noted inside the Capture tab, not surfaced as
    a new banner. A device APO already has hooked (DEVICE_MIC) must NOT be
    listed as "unhooked", even though it's also returned by
    list_all_capture_endpoints() (every APO-hooked endpoint is also a
    real Windows capture endpoint)."""
    real_list_all = audio_devices.list_all_capture_endpoints
    audio_devices.list_all_capture_endpoints = lambda: [DEVICE_MIC, DEVICE_MIC_UNHOOKED]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dlg = SettingsDialog(
                _mic_settings(), THEMES[DEFAULT_THEME_NAME], Path(tmp),
                apo_capture_devices=[DEVICE_MIC],
                device_profile_options=[])
            page = dlg.stack.widget(CATEGORIES.index("Devices"))
            texts = [w.text() for w in page.findChildren(QLabel)]
            assert any("Webcam Mic" in t for t in texts), \
                "un-hooked capture endpoint noted inside the Devices page"
            note_texts = [t for t in texts if "Webcam Mic" in t]
            assert not any("USB Mic" in t for t in note_texts), \
                "an already-hooked capture endpoint isn't also listed as unhooked"
    finally:
        audio_devices.list_all_capture_endpoints = real_list_all


def test_capture_tab_absent_when_microphone_profiles_hidden():
    """Hiding microphone profiles disables microphone EQ outright (the
    manager drops capture branches from the dispatch to match), so the tab
    that assigns it must not be left behind as a control with no effect."""
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(
            _mic_settings(show_microphone_profiles=False),
            THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            apo_capture_devices=[DEVICE_MIC],
            device_profile_options=[("daily", "Daily")])
        assert set(dlg._device_assignment_combos) == {GUID_A}, \
            "no capture combo exists while microphone profiles are hidden"
        page = dlg.stack.widget(CATEGORIES.index("Devices"))
        tabs = page.findChild(QTabWidget)
        assert [tabs.tabText(i) for i in range(tabs.count())] == ["Playback"]
        texts = [w.text() for w in page.findChildren(QLabel)]
        assert not any("USB Mic" in t for t in texts), "hidden mic is not listed"


def test_hidden_capture_assignment_is_not_shown_as_unavailable_or_dropped():
    """A hidden microphone is DISABLED, not unavailable: its assignment must
    neither appear under "Unavailable devices" (whose Clear button would throw
    it away) nor be silently dropped from the collected settings."""
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(
            _mic_settings(show_microphone_profiles=False,
                          device_profile_assignments={GUID_MIC: "voice"}),
            THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            apo_capture_devices=[DEVICE_MIC],
            device_profile_options=[("voice", "Voice - Clean HPF")])
        assert GUID_MIC not in dlg._device_stale_rows, \
            "a hooked-but-hidden mic is not an unavailable device"

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()
        assert emitted[0]["device_profile_assignments"] == {GUID_MIC: "voice"}, \
            "the assignment survives with no combo to read it back from"


def test_toggling_microphone_profiles_rebuilds_the_devices_page_live():
    """Every other control in the dialog takes effect the moment it is
    touched; the Capture tab must appear/disappear with its governing Library
    toggle rather than waiting for the next open. The capture assignment must
    ride through the rebuild, since its combo stops existing halfway."""
    with tempfile.TemporaryDirectory() as tmp:
        dlg = SettingsDialog(
            _mic_settings(device_profile_assignments={GUID_MIC: "voice"}),
            THEMES[DEFAULT_THEME_NAME], Path(tmp),
            apo_render_devices=[DEVICE_A],
            apo_capture_devices=[DEVICE_MIC],
            device_profile_options=[("voice", "Voice - Clean HPF")])
        index = CATEGORIES.index("Devices")

        def tab_names():
            tabs = dlg.stack.widget(index).findChild(QTabWidget)
            return [tabs.tabText(i) for i in range(tabs.count())]

        assert tab_names() == ["Playback", "Capture"]

        dlg.chk_show_microphone.setChecked(False)
        assert tab_names() == ["Playback"], "Capture tab goes away as soon as it is switched off"
        assert GUID_MIC not in dlg._device_assignment_combos

        dlg.chk_show_microphone.setChecked(True)
        assert tab_names() == ["Playback", "Capture"], "and comes back"
        combo = dlg._device_assignment_combos[GUID_MIC]
        assert combo.currentData() == "voice", \
            "the assignment survived the round trip and repopulates its combo"

        emitted = []
        dlg.settings_saved.connect(emitted.append)
        dlg._apply_now()
        assert emitted[0]["show_microphone_profiles"] is True
        assert emitted[0]["device_profile_assignments"] == {GUID_MIC: "voice"}


def test_settings_save_path_rerenders_dispatch():
    """Simulates main_window._open_settings()'s post-accept path
    (manager.settings.update(new_settings) + manager.refresh_render_layers())
    against a real ProfileManager -- confirms the dispatch actually
    rewrites when device_profile_assignments changes, not just that the
    dialog collects the right dict."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        config_dir = Path(tmp) / "config"
        manager = ProfileManager(data_dir=data_dir, config_path=config_dir)
        manager.ensure_first_run()
        meta = manager.create_profile("Daily", config=apo_config.ProfileConfig(
            channel_sections=[apo_config.ChannelSection(channel="all")]))
        manager.activate_profile(meta.id)

        pointer_path = config_dir / ACTIVE_POINTER_FILENAME
        before = pointer_path.read_text(encoding="utf-8")
        assert "Device:" not in before

        real_list = audio_devices.list_apo_devices
        audio_devices.list_apo_devices = lambda: [DEVICE_A]
        try:
            manager.settings.update({"device_profile_assignments": {GUID_A: meta.id}})
            assert manager.refresh_render_layers() is True
        finally:
            audio_devices.list_apo_devices = real_list

        after = pointer_path.read_text(encoding="utf-8")
        assert after != before, "saving an assignment must rewrite the active pointer"
        assert "Device:" in after and "Device: all" in after


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
