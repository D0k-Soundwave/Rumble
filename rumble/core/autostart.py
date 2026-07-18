"""Windows autostart registration via the per-user Run registry key."""
from __future__ import annotations

import sys
import winreg

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "RUMBLE"


def _executable_command() -> str:
    """Command line used to relaunch RUMBLE at login, quoted for the registry."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --start-minimised'
    return f'"{sys.executable}" "{sys.argv[0]}" --start-minimised'


def set_autostart(enabled: bool) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_WRITE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _executable_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
