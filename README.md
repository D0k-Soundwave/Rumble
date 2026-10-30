# RUMBLE

A Windows desktop app for creating, organising, previewing and instantly
activating [Equalizer APO](https://sourceforge.net/projects/equalizerapo/)
equalization profiles for headphones, IEMs and speakers.

![RUMBLE main window: profile tree, EQ curve editor, level meters and filter bands](docs/images/rumble-main-window.png)

> RUMBLE manages EQ profiles for Equalizer APO; it does **not** install APO.
> Equalizer APO is a separate, system-level audio driver you install yourself
> (it requires administrator rights and a reboot). The app and installer link you
> to its download page.

## Project layout

```
rumble/            The application — one self-contained package
  __main__.py       Entry point (python -m rumble)
  core/             Profile/APO logic, AutoEQ, audio devices
  ui/               Qt windows, dialogs, glass-HUD theming
  assets/           Bundled fonts, icons, audio, and AI-prompt markdown
  rumble.ico
packaging/         Build recipe (PyInstaller spec, Inno Setup script, build.ps1)
tests/             Regression gate (run_all_tests.py + selftest_*/test_*/verify_*)
tools/             Dev-only utilities (renderers, previews, audits)
docs/              Design & specification documents
data/              Local runtime data (dev only; shipped app writes next to the exe)
```

## Development

```powershell
# from the repo root, in your virtualenv
pip install -e .

python -m rumble                 # run the app
python tests/run_all_tests.py   # run the full regression gate (offscreen)
```

The app reads/writes its data folder next to the executable when frozen, and at
the repo root (`./data`) in development.

## Building the Windows installer

Produces a **per-user, no-administrator** installer (`dist\RUMBLE-Setup.exe`).

Prerequisites:
- The dev dependencies installed (`pip install -e .`) including PyInstaller.
- [Inno Setup 6](https://jrsoftware.org/isdl.php) (provides `ISCC.exe`) for the
  installer step.

```powershell
pwsh packaging\build.ps1                 # exe + installer
pwsh packaging\build.ps1 -SkipInstaller  # just dist\RUMBLE\RUMBLE.exe
```

Stage 1 (PyInstaller, `packaging/rumble.spec`) bundles the app and Python into
`dist\RUMBLE\RUMBLE.exe`. Stage 2 (Inno Setup, `packaging/installer.iss`) wraps it
into `dist\RUMBLE-Setup.exe`, which installs to
`%LOCALAPPDATA%\Programs\RUMBLE`, adds Start-menu/optional desktop shortcuts,
offers an optional link to the Equalizer APO download page, and uninstalls
cleanly (removing the install folder, the per-user autostart entry, and RUMBLE's
footprint inside the APO config tree).
