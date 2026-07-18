"""Equalizer APO installation and config path detection via the Windows registry."""
from __future__ import annotations

import winreg
from pathlib import Path

APO_REGISTRY_KEY = r"SOFTWARE\EqualizerAPO"
APO_DEFAULT_INSTALL = Path(r"C:\Program Files\EqualizerAPO")
APO_DEFAULT_CONFIG = APO_DEFAULT_INSTALL / "config"


def _read_registry_value(hive: int, key_path: str, value_name: str) -> str | None:
    try:
        with winreg.OpenKey(hive, key_path) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return str(value)
    except OSError:
        return None


def get_install_path() -> Path | None:
    """Return the Equalizer APO install directory, or None if not found."""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for value_name in ("InstallPath", ""):
            value = _read_registry_value(hive, APO_REGISTRY_KEY, value_name)
            if value and Path(value).exists():
                return Path(value)
    if APO_DEFAULT_INSTALL.exists():
        return APO_DEFAULT_INSTALL
    return None


def get_config_path() -> Path:
    """Return the Equalizer APO config directory.

    Reads HKLM\\SOFTWARE\\EqualizerAPO\\ConfigPath; falls back to the default
    install's config subfolder, then the global default.
    """
    value = _read_registry_value(winreg.HKEY_LOCAL_MACHINE, APO_REGISTRY_KEY, "ConfigPath")
    if value:
        path = Path(value)
        if path.exists():
            return path

    install = get_install_path()
    if install is not None:
        candidate = install / "config"
        if candidate.exists():
            return candidate

    return APO_DEFAULT_CONFIG


def is_apo_installed() -> bool:
    """Best-effort detection of an Equalizer APO installation."""
    if get_install_path() is not None:
        return True
    return APO_DEFAULT_CONFIG.exists()


# A1 (docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md): the fix
# affordance for "your default device isn't APO-hooked" is Equalizer APO's own
# device-hooking tool -- the same dialog its installer runs to ask which
# endpoints to install onto, re-launchable afterward to add/remove endpoints.
# The roadmap's design sketch named it "Configurator.exe"; empirically, on a
# real Equalizer APO install (verified 2026-07-12, this machine, alongside
# Editor.exe/DeviceSelector.exe/Uninstall.exe etc. in the install directory),
# that tool ships as "DeviceSelector.exe" -- "Configurator.exe" is checked
# second in case an older or differently-packaged release still uses that
# name. Code/reality wins per the execution protocol's reconcile-first rule.
CONFIGURATOR_EXE_CANDIDATES = ("DeviceSelector.exe", "Configurator.exe")


def get_configurator_path() -> Path | None:
    """Locate Equalizer APO's device-hooking tool under the detected install
    directory (see CONFIGURATOR_EXE_CANDIDATES above). None when APO isn't
    found or neither candidate exists there."""
    install = get_install_path()
    if install is None:
        return None
    for name in CONFIGURATOR_EXE_CANDIDATES:
        candidate = install / name
        if candidate.exists():
            return candidate
    return None
