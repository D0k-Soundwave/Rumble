"""RUMBLE entry point: bootstraps fonts, theme, single-instance guard, the
profile manager, first-run setup, and shows the main window or launches
straight to tray depending on settings."""
from __future__ import annotations

import gc
import json
import os
import sys
from pathlib import Path

import shiboken6
from PySide6.QtCore import QAbstractNativeEventFilter, QTimer
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from rumble.core import autostart
from rumble.core.custom_themes import load_custom_themes
from rumble.core.profile_manager import ProfileManager
from rumble.ui.eq_graph import install_accessibility
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, THEMES, get_theme

APP_ID = "RUMBLE-EqualizerAPO-ProfileManager-SingleInstance"

# Launching RUMBLE again while it is running is a request to see it, not an
# error: the second process asks the running one to come to the front and then
# exits quietly. The hand-off is a registered window message (a system-wide
# unique id derived from this name, so no other app can collide with it),
# broadcast rather than posted to a known HWND — the running instance's window
# handle changes across the in-process UI rebuilds below, and while it sits in
# the tray there is no findable window at all. Only a process that registered
# the same name sees the message; everyone else ignores an id they don't know.
ACTIVATE_MSG_NAME = "RUMBLE-EqualizerAPO-ProfileManager-Activate"
HWND_BROADCAST = 0xFFFF
ASFW_ANY = -1

# Explicit Windows AppUserModelID. Without this, a GUI launched via pythonw.exe
# inherits the host interpreter's taskbar identity — so the taskbar button shows
# pythonw's icon and groups under it instead of showing RUMBLE's own icon. Setting
# a stable, app-specific id makes Windows treat RUMBLE as its own application, so
# the window icon (set on the QApplication/window) is used on the taskbar too.
APP_USER_MODEL_ID = "RUMBLE.EqualizerAPO.ProfileManager"


def _set_app_user_model_id() -> None:
    """Declare an explicit AppUserModelID so the taskbar uses RUMBLE's icon and
    groups its windows under RUMBLE, not the pythonw.exe host. Windows-only and
    best-effort — a failure here only affects taskbar grouping, never startup."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

# The QAccessible factory is process-global; install it once even across the
# in-process UI rebuilds that a live interface-size change triggers.
_ACCESSIBILITY_INSTALLED = False


def _install_accessibility() -> None:
    """Install the EQ graph's keyboard-navigation accessibility once. The
    QAccessible factory is process-global, so install it a single time even
    across the in-process UI rebuilds a live interface-size change triggers."""
    global _ACCESSIBILITY_INSTALLED
    if not _ACCESSIBILITY_INSTALLED:
        install_accessibility()
        _ACCESSIBILITY_INSTALLED = True

# Bundled, read-only resources (fonts, icon) live inside the package. In dev
# that is this file's directory; in a PyInstaller build the entry script's
# __file__ points at the bundle root rather than the package, so resolve the
# bundled copy under <_MEIPASS>/rumble instead (the spec stages it there, which
# is also where the imported rumble.ui.* modules look via __file__.parent.parent).
if getattr(sys, "frozen", False):
    PACKAGE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "rumble"
else:
    PACKAGE_DIR = Path(__file__).resolve().parent
FONTS_DIR = PACKAGE_DIR / "assets" / "fonts"
ICON_PATH = PACKAGE_DIR / "rumble.ico"

# The app's type system (registered at startup, used via the theme QSS):
#   Inter         — UI / body text (the readability workhorse)
#   Space Grotesk — display / headers / wordmark (premium-gear character)
#   IBM Plex Mono — numeric readouts (dB / Hz / Q) for an instrument-panel feel


def _data_dir() -> Path:
    """Root for writable runtime data (the ``data/`` folder lives under this).

    Next to the executable when frozen; the repo root (the package's parent) in
    development, so the existing checked-out ``data/`` keeps working."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PACKAGE_DIR.parent


def _load_fonts() -> None:
    """Register every bundled .ttf so the theme can reference the families by
    name. Globbing keeps this in sync as font files are added or removed."""
    if not FONTS_DIR.is_dir():
        return
    for path in sorted(FONTS_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(path))


def _read_saved_settings() -> dict:
    """Read persisted settings without the profile manager — needed before
    QApplication exists, because the UI scale must be set via environment
    variables prior to Qt initialising."""
    try:
        data = json.loads((_data_dir() / "data" / "profiles.json").read_text(encoding="utf-8"))
        return dict(data.get("settings") or {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def _primary_display_size() -> tuple[int, int]:
    """The primary display's true pixel resolution via EnumDisplaySettings,
    which reports the real display mode regardless of DPI virtualisation
    (GetSystemMetrics lies before a DPI awareness context is set)."""
    import ctypes
    from ctypes import wintypes

    class DEVMODEW(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", ctypes.c_wchar * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            ("dmPositionX", wintypes.LONG),
            ("dmPositionY", wintypes.LONG),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", ctypes.c_wchar * 32),
            ("dmLogPixels", wintypes.WORD),
            ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD),
            ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD),
            ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD),
            ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD),
            ("dmPanningHeight", wintypes.DWORD),
        ]

    ENUM_CURRENT_SETTINGS = -1
    devmode = DEVMODEW()
    devmode.dmSize = ctypes.sizeof(DEVMODEW)
    try:
        if ctypes.windll.user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS,
                                                     ctypes.byref(devmode)):
            return int(devmode.dmPelsWidth), int(devmode.dmPelsHeight)
    except Exception:
        pass
    return (0, 0)


def _apply_ui_scale(settings: dict) -> None:
    """Apply the 'double size' interface option. Qt only reads these env vars
    while a QApplication is being constructed, so this runs immediately before
    every (re)construction — including in-process rebuilds when the setting is
    toggled live. Replaces Windows display scaling with an exact 2x factor so
    every UI element renders at twice its designed size; skipped when the
    primary display is too small to fit the doubled 1000x580 window
    (2000x1160 plus a safety margin for the taskbar -- MainWindow has had no
    native title bar since the frameless-chrome campaign, see
    docs/superpowers/plans/2026-07-15-frameless-chrome-progress.md)."""
    double = bool(settings.get("ui_double_size"))
    if double:
        width, height = _primary_display_size()
        double = width >= 2000 and height >= 1280
    if double:
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
        os.environ["QT_SCALE_FACTOR"] = "2"
    else:
        os.environ.pop("QT_ENABLE_HIGHDPI_SCALING", None)
        os.environ.pop("QT_SCALE_FACTOR", None)


def _acquire_single_instance_lock() -> int | None:
    """Hold a named Win32 mutex for the lifetime of the process. A kernel
    mutex rather than QSharedMemory because Qt objects do not survive the
    in-process QApplication rebuilds used for live interface-size changes."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    handle = kernel32.CreateMutexW(None, False, APP_ID)
    if not handle:
        return None
    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle


def _register_activate_message() -> int:
    """The system-wide id for ACTIVATE_MSG_NAME. Every process that registers
    the same name gets the same id back, which is what lets the two instances
    agree on a message without knowing anything else about each other.
    Returns 0 if registration fails."""
    import ctypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterWindowMessageW.restype = ctypes.c_uint
    user32.RegisterWindowMessageW.argtypes = (ctypes.c_wchar_p,)
    return int(user32.RegisterWindowMessageW(ACTIVATE_MSG_NAME))


def _signal_running_instance() -> bool:
    """Ask the instance already holding the mutex to show itself. False if the
    message could not be posted, in which case the caller should fall back to
    telling the user where RUMBLE is."""
    import ctypes
    message_id = _register_activate_message()
    if not message_id:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # Windows only lets the process that currently owns the foreground hand
    # activation rights to another one. That is this process — the user just
    # launched it — so pass the right along; without it the running instance's
    # SetForegroundWindow is downgraded to a flashing taskbar button.
    try:
        user32.AllowSetForegroundWindow.argtypes = (ctypes.c_int,)
        user32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass
    user32.PostMessageW.restype = ctypes.c_bool
    user32.PostMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p)
    return bool(user32.PostMessageW(ctypes.c_void_p(HWND_BROADCAST), message_id, None, None))


class _ActivationFilter(QAbstractNativeEventFilter):
    """Watches the running instance's message loop for the broadcast a second
    launch sends, and raises the main window when it arrives. An application
    filter sees every message the process receives, whichever of its windows
    was targeted — so this works while RUMBLE is minimised or in the tray."""

    def __init__(self, message_id: int, on_activate) -> None:
        super().__init__()
        self._message_id = message_id
        self._on_activate = on_activate

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt override
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            import ctypes
            from ctypes import wintypes
            try:
                msg = wintypes.MSG.from_address(int(message))
            except (TypeError, ValueError):
                return False, 0
            if msg.message == self._message_id:
                self._on_activate()
                return True, 0
        return False, 0


def _run_apo_adoption(manager, setup: dict, theme: dict) -> None:
    """First-run/boot adoption of an existing Equalizer APO setup: warn if a
    rival tool is also writing the config, then offer to import any real,
    non-boilerplate configs the reconcile found. Runs before the main window so
    imported profiles are present when it builds."""
    foreign = setup.get("foreign_includes") or []
    if foreign:
        QMessageBox.warning(
            None, "RUMBLE",
            "Another Equalizer APO tool also appears to write to your config:\n\n"
            + "\n".join(f"   •  {f}" for f in foreign)
            + "\n\nRUMBLE now manages the Equalizer APO config and has saved "
            "your previous setup as config.txt.rumble_backup. For best results, "
            "use only one Equalizer APO manager.")

    try:
        candidates = manager.sync_apo_imports()
    except Exception:
        candidates = []
    if not candidates:
        return

    from rumble.ui.apo_import_dialog import ApoImportDialog
    dlg = ApoImportDialog(candidates, theme)
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    chosen = dlg.selected() if accepted else []
    if chosen:
        manager.import_apo_candidates(chosen)
    # Remember everything not imported so we don't re-offer it every launch
    # (Settings → "Import APO configs" honours the same exclusion).
    declined = [c.source for c in candidates if c not in chosen]
    if declined:
        manager.ignore_apo_sources(declined)


def main() -> int:
    """Build the Qt application and main window, looping to rebuild them
    in-process whenever the interface size setting changes: Qt only reads the
    scale env vars during QApplication construction, so a live size change
    means constructing a fresh QApplication. The profile manager (and any
    unsaved edit) survives the rebuild, making the resize feel instant."""
    _set_app_user_model_id()
    root_dir = _data_dir()
    manager: ProfileManager | None = None
    lock: int | None = None
    first_build = True
    pending_edit = None
    reopen_settings = None

    while True:
        settings = manager.settings if manager else _read_saved_settings()
        _apply_ui_scale(settings)
        app = QApplication(sys.argv)
        app.setApplicationName("RUMBLE")
        app.setOrganizationName("RUMBLE")
        app.setQuitOnLastWindowClosed(False)

        app_icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()
        if not app_icon.isNull():
            app.setWindowIcon(app_icon)

        if first_build:
            lock = _acquire_single_instance_lock()
            if lock is None:
                # RUMBLE is already running. A launch that Windows itself made
                # (autostart, which asks for the tray) must not yank the window
                # up; a launch the user made is them looking for the window, so
                # hand it to the running instance and get out of the way.
                if "--start-minimised" in sys.argv[1:] or _signal_running_instance():
                    return 0
                QMessageBox.information(None, "RUMBLE",
                                        "RUMBLE is already running. Check the system tray.")
                return 0

        _load_fonts()

        theme_engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
        theme_engine.apply(app)

        if manager is None:
            manager = ProfileManager(data_dir=root_dir / "data")
            load_custom_themes(root_dir / "data")

        saved_theme = manager.settings.get("theme", DEFAULT_THEME_NAME)
        # Self-heal a stale saved theme. If the persisted name no longer maps to
        # a registered theme (a renamed/removed build theme, or a deleted custom
        # one) — checked after load_custom_themes so imports still count —
        # fall back to the default and persist it, so the Appearance swatch
        # selects correctly and theme exports get the right filename.
        if saved_theme != "System" and saved_theme not in THEMES:
            saved_theme = DEFAULT_THEME_NAME
            manager.settings["theme"] = saved_theme
            manager.save()
        if saved_theme != DEFAULT_THEME_NAME:
            theme_engine.switch(app, get_theme(saved_theme))

        if first_build:
            setup = manager.ensure_first_run()
            if not setup.get("config_accessible", True) and manager.settings.get("config_path_override"):
                # The user-set custom config path can't be created/accessed.
                # Don't strand them behind a fatal startup error they can only
                # fix from inside the app: ignore the bad override for this
                # session, warn, and retry against the default APO path so the
                # window — and Settings, where they can correct the path —
                # still opens. The persisted override is left untouched (so a
                # later launch retries it, e.g. once an unplugged drive is back);
                # the session flag is never saved, so the adoption save() below
                # can't quietly wipe the user's setting.
                bad_override = manager.settings.get("config_path_override")
                QMessageBox.warning(
                    None, "RUMBLE",
                    "RUMBLE can't use the custom Equalizer APO config "
                    "folder you set:\n\n"
                    f"{bad_override}\n\n"
                    "Falling back to the default location for this session. "
                    "Update or clear the override in Settings → APO Integration "
                    "to fix this permanently.")
                manager._ignore_config_override = True
                setup = manager.ensure_first_run()
            if not setup.get("config_accessible", True):
                QMessageBox.critical(
                    None, "RUMBLE",
                    "RUMBLE can't access the Equalizer APO configuration "
                    "folder:\n\n"
                    f"{manager.config_path}\n\n"
                    "Make sure Equalizer APO is installed and that you have "
                    "permission to write to its config folder, then launch "
                    "RUMBLE again.")
                return 1
            manager.ensure_builtin_profiles()
            _run_apo_adoption(manager, setup, get_theme(saved_theme))
            if manager.settings.get("start_with_windows"):
                try:
                    autostart.set_autostart(True)
                except Exception:
                    pass

        window = MainWindow(manager, theme_engine, app_icon)
        _install_accessibility()

        # Listen for a second launch. The filter and the window it raises both
        # die with this QApplication, so re-install them on every rebuild.
        # winId() forces the native handle into existence even when we start
        # straight to the tray and never show the window: a broadcast only
        # reaches processes that own at least one top-level window.
        activate_msg = _register_activate_message()
        if activate_msg:
            window.winId()
            activation_filter = _ActivationFilter(activate_msg, window._show_and_raise)
            app.installNativeEventFilter(activation_filter)

        if pending_edit is not None:
            window.restore_unsaved_edit(*pending_edit)
            pending_edit = None

        if first_build:
            start_minimised = "--start-minimised" in sys.argv[1:] or (
                manager.settings.get("start_with_windows") and manager.settings.get("start_minimised")
            )
            if not start_minimised:
                window.show()
        else:
            # Rebuild after a live size change — bring the window straight back.
            window.show()
            window.raise_()
            window.activateWindow()
            if reopen_settings is not None:
                # The size change was clicked inside the settings dialog:
                # reopen it (with its carried state) once the event loop runs.
                QTimer.singleShot(0, lambda w=window, s=reopen_settings: w.reopen_settings(s))
                reopen_settings = None
        first_build = False

        exit_code = app.exec()
        rescale_requested = bool(app.property("rumble_rescale"))
        pending_edit = getattr(app, "rumble_pending_edit", None)
        reopen_settings = getattr(app, "rumble_reopen_settings", None)

        # Qt supports sequential QApplication instances, but the old one must
        # be destroyed on the C++ side first — a plain `del` leaves it alive
        # behind QApplication.instance().
        shiboken6.delete(window)
        shiboken6.delete(app)
        del window, app
        gc.collect()

        if rescale_requested and QApplication.instance() is None:
            continue
        break

    # The mutex handle is released by the OS at process exit.
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
