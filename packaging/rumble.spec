# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller build spec for RUMBLE -- one-folder, windowed build.

Invoked via `packaging/build.ps1` as:
    pyinstaller --clean --noconfirm packaging/rumble.spec
PyInstaller executes the spec with its working context anchored to the
spec file's own directory (exposed as the `SPECPATH` global it injects),
NOT the caller's CWD -- so every path below is built from `ROOT`
(SPECPATH's parent, i.e. the repo root) rather than assumed-relative
strings. This matches installer.iss's expectation of `dist\RUMBLE\RUMBLE.exe`
(a one-folder COLLECT output, not onefile) and rumble/rumble.ico as the icon.

Audio backends (Phase 4) needing explicit verification here, empirically,
not just assumed from a hook existing:
  - sounddevice: bundles PortAudio via the pyinstaller-hooks-contrib
    hook-sounddevice.py (ships with `_pyinstaller_hooks_contrib`, already a
    transitive PyInstaller dependency) -- it locates
    <site-packages>/_sounddevice_data/portaudio-binaries/libportaudio64bit.dll
    and stages it back under the same relative `_sounddevice_data/...` path
    inside the frozen bundle. Confirmed present in dist\RUMBLE\_internal\
    after a real build (see task-6-report.md for the file listing) -- no
    extra binaries=[] entry needed for this one.
  - pyaudiowpatch: a plain `import _portaudiowpatch as pa` inside
    pyaudiowpatch/__init__.py pulls in the compiled extension module
    `_portaudiowpatch.cp3xx-win_amd64.pyd` (a top-level module living
    directly in site-packages, not inside the pyaudiowpatch package dir) via
    PyInstaller's ordinary import-graph + binary-dependency analysis -- no
    dedicated hook exists for pyaudiowpatch, but none was needed. Also
    confirmed present in dist\RUMBLE\_internal\ after a real build.

rumble/assets (fonts, icons) and rumble/rumble.ico are staged under `rumble/`
inside the bundle because rumble/__main__.py's PACKAGE_DIR (frozen branch)
resolves to `<_MEIPASS>/rumble`, and rumble/ui/icons.py's ICONS_DIR resolves
relative to its own __file__ two parents up -- same `rumble/assets/...` path
either way.
"""
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "rumble", "assets"), "rumble/assets"),
    (os.path.join(ROOT, "rumble", "rumble.ico"), "rumble"),
]
datas += collect_data_files("pycaw")

a = Analysis(
    [os.path.join(ROOT, "rumble", "__main__.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "sounddevice",
        "pyaudiowpatch",
        "win32com",
        "win32com.client",
        "pycaw",
        "pycaw.pycaw",
        "comtypes",
        "comtypes.stream",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RUMBLE",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(ROOT, "rumble", "rumble.ico")],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RUMBLE",
)
