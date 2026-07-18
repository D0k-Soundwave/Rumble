# RUMBLE — Application Specification

RUMBLE
A complete Windows desktop EQ-profile manager for Equalizer APO.

*This document summarises the whole application: what it is for, every feature
and function, all file formats it produces and understands, the complete set of
valid profile-format options, how it integrates with Equalizer APO, and what a
"full" profile requires. It is a living reference for the current build.*

---

## 1. Purpose & Intended Use

Equalizer APO is a powerful system-wide audio equalizer for Windows, but its
native interface is a hand-edited text configuration (`config.txt`). RUMBLE
is the polished front-end on top of it. It turns the chore of editing APO
config files into a managed workflow:

- **Create / edit** EQ profiles visually (parametric bands + a live response graph).
- **Organize** a library of profiles by tag hierarchy.
- **Preview** unsaved edits live, in real time, through the actual audio path.
- **Activate** any profile system-wide instantly, or bypass EQ entirely.
- **Acquire** profiles without dialing by hand — via the AutoEQ online database,
  an AI-generation prompt, or importing files.
- **Back up / restore / share** the whole library and settings.

Intended user: anyone from a casual listener wanting better headphone sound to a
critical listener or audio engineer who wants fast, organized, reversible control
over Equalizer APO. The app never requires the user to touch APO's raw config.

---

## 2. Technology & Platform

- **OS:** Windows (10/11). Depends on the Windows registry (APO detection,
  autostart) and Equalizer APO.
- **Language/UI:** Python + PySide6 (Qt 6), Fusion style with generated QSS
  theming. Frequency graph via pyqtgraph + numpy.
- **Distribution:** PyInstaller one-dir build; per-user Inno Setup installer
  (no admin, installs to `%LOCALAPPDATA%\Programs\RUMBLE` by default, data stored
  next to the executable). Bundles the VC++ runtime (run only if missing).
- **Window:** fixed-size compact layout **1000×580**; an optional **Double (2×)**
  interface size scales it to **2000×1160**. The window is intentionally not
  freely resizable.

---

## 3. Equalizer APO Integration

RUMBLE and APO share **one artifact**: a RUMBLE profile file *is* a valid
APO config file (metadata lives in `#` comment lines APO ignores). No translation
step exists between "RUMBLE profile" and "APO config".

### 3.1 Detection (`core/apo_path.py`)
- Reads `HKLM\SOFTWARE\EqualizerAPO\ConfigPath` for the config directory, then
  `InstallPath`, falling back to `C:\Program Files\EqualizerAPO\config`.
- `get_config_path()` resolves the active config directory (honouring the
  `config_path_override` setting); `is_apo_installed()` gates functionality and
  drives setup warnings.

### 3.2 config.txt ownership (first run, idempotent)
RUMBLE **owns** `config.txt` outright so its active profile truly overrides
APO rather than stacking on top of pre-existing content. On startup
`ensure_first_run()`:
1. Ensures the APO `config` directory and the `config/rumble/` subtree exist. If
   the directory can't be created or accessed it reports
   `config_accessible=False` instead of crashing, and startup recovers (§3.7).
2. If `config.txt` is not already RUMBLE-managed — detected by the
   `# RUMBLE-Managed:` marker — **or** a foreign tool has written its own APO
   directives alongside RUMBLE's block, RUMBLE takes (or re-asserts) ownership:
   - The displaced foreign content is captured and offered for one-time import,
     and any foreign `Include:` (e.g. Peace's) raises a "another tool is
     co-writing your config" warning.
   - On the **genuine first run** the original `config.txt` is copied
     **byte-for-byte** to `config.txt.rumble_backup` — the *sacred* backup,
     written once and never overwritten, so hand-back/uninstall restores the
     user's pre-RUMBLE file exactly. Any **later** displacement (config.txt
     clobbered after RUMBLE owned it, or a tool appending to RUMBLE's block) is
     preserved to a timestamped `config.txt.rumble_drift-<ts>` file, so nothing is
     lost even if the user declines the import.
   - `config.txt` is rewritten to a small managed block: the marker + a single
     `Include: rumble_active.txt`.
3. Writes the active pointer if absent.

This is safe to run every launch — a clean managed `config.txt` (marker present,
no foreign content) is a no-op.

**Hand back / resume.** Settings → "Hand back to APO" restores
`config.txt.rumble_backup` verbatim and sets `apo_managed=false` so RUMBLE stops
re-taking ownership until the user resumes — the same restore the uninstaller
performs.

**Import dedup.** A source already imported, or previously declined
(`ignored_apo_sources`), is never re-offered — by the boot reconcile or the
manual Settings → "Import APO configs" action, which share one
`importable_candidates()` filter — so repeating an import never creates
duplicate profiles.

### 3.3 The active pointer (`rumble_active.txt`)
A tiny APO file in the config directory that the managed `Include:` points at.
RUMBLE rewrites **only this pointer** to switch what APO plays:
- **Active profile:** `Include: rumble/user/<id>.txt` (built-ins:
  `rumble/defaults/<id>.txt`). Include paths always use POSIX `/` separators,
  consistent on every OS.
- **Bypass / none:** a comment-only file (no active commands → flat passthrough).

Switching profiles never rewrites `config.txt` again; it only repoints this file.

### 3.4 Live preview (`rumble/rumble_preview.txt`)
Unsaved editor state is auditioned by writing the rendered config body to this
scratch file and pointing `rumble_active.txt` at it. Any canonical write
(activate, save, bypass) supersedes the preview and points back at the saved
profile. The scratch file is clearly marked "temporary, safe to delete" and is
excluded from profile auto-discovery.

### 3.5 Bypass
A persisted on/off state. When bypassed, the pointer is rewritten to the
comment-only passthrough so APO applies no EQ, while the chosen profile is
remembered for when bypass is turned off.

### 3.6 Uninstall footprint
Complete removal cleans: the install dir + data, the `HKCU` autostart entry, and
the APO footprint — restoring the original `config.txt` from
`config.txt.rumble_backup` (verbatim), then removing the backup, any
`config.txt.rumble_drift-<ts>` snapshots, `rumble_active.txt`, `rumble_preview.txt`,
and the `config/rumble/` tree.

### 3.7 Startup robustness
A user-set `config_path_override` that can't be created or accessed (e.g. a path
on an unplugged drive) no longer strands the app behind a fatal error. RUMBLE
warns, ignores the bad override **for that session only** (the persisted value is
kept and retried next launch), and falls back to the default APO path so the
window — and Settings, where the path is fixed — still opens. Correcting the
override in Settings clears the session fallback immediately. Only a genuinely
missing APO install (even the default path inaccessible) still shows the hard
"can't access the config folder" message.

---

## 4. Profile File Format (canonical)

A profile is a single, portable, self-contained **UTF-8 text file**. Stored at
`config/rumble/defaults/<id>.txt` (built-in) or `config/rumble/user/<id>.txt`
(user). The file is the **sole source of truth**; the app's JSON stores no
per-profile copy.

### 4.1 Canonical header
Leading `#` comment lines carry all metadata (APO ignores them):

```
# RUMBLE Profile: <Name>
# Description: <single line, may be empty>
# Tags: <comma-separated, may be empty>
# Created: YYYY-MM-DDTHH:MM:SS
# Modified: YYYY-MM-DDTHH:MM:SS
# Readonly: true                 (only present when user-locked)
# Builtin: true                  (only present on system-protected built-ins)
# Created by RUMBLE
```

- **Tags** use `,` to separate tags and `/` to express hierarchy within a tag
  (e.g. `Music / Rock, Situation / Reference`). Tags are UTF-8 text.
- Date fields accept ISO (`T`-separated) and legacy space-separated forms.

### 4.2 Body
Everything after the comment block is a standard APO command body (see §5).

### 4.3 Accepted/tolerated variants (for import & migration)
The parser is permissive so foreign and partial files load and then migrate:
- **Partial headers** — any subset of the canonical header lines.
- **Foreign APO config files with no header at all** — absent metadata fields are
  reported as "not stated" (vs "stated empty") so the app fills in sensible
  defaults and rewrites a full canonical header on load.
- Non-RUMBLE leading comments (e.g. REW/measurement-tool headers) are preserved
  below the canonical header on metadata rewrites.
- The APO body is preserved **byte-for-byte** on metadata-only edits, so
  hand-tuned or foreign formatting survives.

---

## 5. Valid Format Options (complete)

RUMBLE parses, edits, round-trips and renders the full APO command set below.
Any unrecognised line is **preserved verbatim** so importing never silently
drops content. Comment lines (`#`) are skipped by the APO parser.

### 5.1 Global / structural commands
| Command | Form | Notes |
|---|---|---|
| **Preamp** | `Preamp: <±dB> dB` | Global gain; auto-preamp can compute it on save. |
| **Device** | `Device: <pattern>` | Targets a specific output device; `all` = every device. |
| **Channel** | `Channel: <spec>` | Starts a per-channel section. Specs: `all`, `L`, `R`, `L R`, `C`, `LFE`, `L R C`, `RL RR`. Consecutive same-channel sections merge. |
| **Filter** | `Filter <n>: ON\|OFF <type> …` | One EQ band — see §5.2. `ON`/`OFF` toggles bypass per band. |
| **GraphicEQ** | `GraphicEQ: f1 g1; f2 g2; …` | Variable-band graphic EQ line (e.g. AutoEQ's 31-band export). Per channel section. |
| **Convolution** | `Convolution: <path>` | Impulse-response (room correction / cabinet) — `.wav`/`.pcm`. IR files are bundled into `rumble/irs/` on import. |
| **Delay** | `Delay: <value> ms` or `Delay: <value> samples` | Per-channel delay; samples are converted using the 48 kHz default rate. |
| **Copy** | `Copy: <expr>` | APO channel-copy/matrix expression, preserved. |
| **VSTPlugin** | `VSTPlugin: <path>` (+ `VSTPlugin Chunks: <data>`) | VST insert with optional state chunk. |
| **If / ElseIf / Else / EndIf** | conditional block | Whole block preserved verbatim in order. |
| *(any other line)* | — | Preserved verbatim (round-trip safe). |

### 5.2 Filter types (the `<type>` in a `Filter` line)
Each filter has a centre frequency `Fc … Hz`; gain and bandwidth apply per type.
Bandwidth may be expressed as **`Q <value>`** or **`BW Oct <value>`** where the
type supports it.

**Peak**
- `PK` — Peak/bell (the standard parametric band). Gain + Q/BW.
- `PEQ` — Peak with bandwidth parameterisation.
- `Modal` — Modal/resonant peak.

**Pass**
- `LP` / `HP` — Low-pass / High-pass.
- `LPQ` / `HPQ` — Low-pass / High-pass with Q.
- `BP` — Band-pass.

**Shelf**
- `LS` / `HS` — Low-shelf / High-shelf. Gain (+ Q/BW).
- `LSC` / `HSC` — Low-shelf / High-shelf with configurable centre/Q.
- `LS 6dB`, `LS 12dB`, `HS 6dB`, `HS 12dB` — fixed-slope shelves (no Q).

**Special**
- `NO` — Notch.
- `AP` — All-pass.

**Advanced**
- `IIR` — Raw biquad/IIR: `Filter <n>: ON IIR Order <k> Coefficients <…>`.

Editor limit: up to **32 bands** per section (`MAX_BANDS = 32`); new profiles
default to **10** bands. Default sample rate for conversions: **48000 Hz**.

### 5.3 Example (canonical profile body)
```
Preamp: -6.1 dB
Channel: L R
Filter 1: ON LSC Fc 105 Hz Gain 6.5 dB Q 0.70
Filter 2: ON PK  Fc 127 Hz Gain -2.0 dB Q 0.29
Filter 3: ON PK  Fc 2009 Hz Gain 6.5 dB Q 2.96
Filter 10: ON HSC Fc 10000 Hz Gain 1.2 dB Q 0.70
```

---

## 6. What a "Full" Profile Needs

A profile is valid with as little as a single `Filter` or `GraphicEQ` line, but a
**complete, well-formed RUMBLE profile** has:

1. **A full canonical header** — Name, Description, Tags, Created, Modified, and
   the attribution line (Readonly/Builtin only when applicable). Files missing
   any required header key are migrated to a full header on load.
2. **A Preamp line** — recommended whenever bands add gain, to prevent clipping
   (auto-preamp can compute a safe value on save).
3. **At least one channel section** — most profiles use a single `Channel: L R`
   (or implicit `all`); unlinking channels splits into separate `L`/`R` sections.
4. **One or more bands** — parametric `Filter` lines and/or a `GraphicEQ` line.
5. **UTF-8 metadata** — names, descriptions and tags are full UTF-8 text
   (accents, non-Latin scripts, symbols all supported); tags comma-separated
   with `/` hierarchy.
6. **Resolvable resources** — any `Convolution`/`VSTPlugin` path should point to a
   bundled/known file (IR files import into `rumble/irs/`).

Device portability: foreign `Device:` lines that don't match a current output are
remapped to the first active EQ device on import/discovery ("never *all*" — `all`
is only ever chosen explicitly by the user).

---

## 7. Formats Produced & Understood

| Format | Direction | Purpose |
|---|---|---|
| `.txt` (canonical profile / any APO config) | import + export | The core artifact; APO-compatible. |
| `.zip` profile **bundle** | import + export | A profile plus its convolution IR file(s). |
| `.zip` profile **pack** | import | Several profiles in one archive — every contained `.txt` is imported, each with its IR. |
| AutoEQ `ParametricEQ.txt` | import (online) | Fetched from the AutoEQ database; valid APO, ingested directly. |
| `.rumble-theme` | import + export | A shareable colour theme. |
| Backup `.zip` | export + restore | Whole-library snapshot (see §8.3). |
| `config.txt`, `rumble_active.txt`, `rumble_preview.txt` | written | APO integration files (see §3). |

---

## 8. Features & Functions

### 8.1 Profile management (CRUD)
- **Create** new profiles (choose name, speaker layout, starting band count).
- **Edit** parametric bands with a live frequency-response graph; per-band
  type/Fc/gain/Q-or-BW/enable; add/remove bands (to 32).
- **Live tuning is always on** — every edit previews instantly through APO; there
  is no separate "live" toggle.
- **Pre-Amp & Auto** are session controls — they update the live preview but never
  mark the profile dirty (auto computes a safe pre-amp; toggling is non-destructive).
- **Channel linking** — link state is derived from section identity; unlinking a
  combined `all`/`L R` section splits it into independent `L`/`R` sections so
  channels can diverge.
- **Rename, Duplicate, Delete.**
- **Lock/Unlock** (user readonly). **Built-in** profiles are system-protected
  (cannot be overwritten/renamed/deleted; duplicate to customise). *The shipped
  built-in library is currently empty — the seeding/protection infrastructure is
  in place for a curated set to be authored later.*
- **Open vs Activate**: single-clicking a profile **opens** it in the editor (for
  viewing/editing) without changing the live EQ; **double-clicking activates** it
  system-wide. Opening a different profile tears down any live preview so APO
  reverts to the still-active profile; Activate / Save / Bypass also supersede it.
- **Bypass** (flat passthrough) toggles APO off without losing the chosen profile.
- **Export** a single profile (or bundle with its IR).
- Files are archived, never hard-deleted, on destructive operations where
  applicable.

### 8.2 Acquiring profiles
- **AutoEQ database browser** (Tools → Browse AutoEQ Database): searches the full
  online AutoEQ catalogue (~6,290 measured headphones/IEMs across 23 sources),
  filter by type/source, one-click import (the device's `ParametricEQ.txt` is
  fetched and ingested directly). Index is cached locally (7-day TTL, works
  offline from cache). Network calls run off the UI thread.
- **AI generation** (Tools → AI Profiles): a comprehensive prompt to paste into
  any AI assistant (ChatGPT/Gemini/…) that interviews the user and returns
  a ready-to-import profile (UTF-8, with comma-separated tags).
- **Import**: one or many `.txt` files and/or `.zip` bundles/packs at once.

### 8.3 Backup & Restore
Settings → Backup snapshots the **entire data directory + the APO `rumble` tree**
(all profiles, settings, custom themes, IR files) into one `.zip`. Restore is
verbatim.

### 8.4 Library organization & browsing
- **Searchable tag tree** (main window left panel): a collapsible tree built from
  the tag hierarchy, split into top-level **User** and **Built-in** branches with
  tags nested beneath and an **Untagged** bucket. Mirrors the tray's model.
- **Search box** filters the whole tree and auto-expands matches.
- **Active vs selected** are visually distinct: the active (live) profile shows an
  accent check-circle + accent name; selection is the row highlight (= editing).
- **Marquee scrolling**: any main-window row text wider than its column scrolls
  smoothly and continuously (slow, ~15 px/s, time-based); short text shows a clean
  ellipsis at rest.

### 8.5 System tray
- Tray icon with a **toast popup** that shows the same tag hierarchy as a quick
  collapsible profile switcher, plus **Bypass EQ**, **Open**, **Settings**, **Quit**.
- Left-click opens the main window; right-click opens the toast. The toast
  auto-hides ~4 s after the pointer leaves it (and closes on any outside click),
  but stays open while the pointer is over it.
- The app runs close-to-tray (EQ keeps running in APO even when the window is closed).

### 8.6 Theming — "rounded glass HUD"
The look is glassmorphism × sci-fi HUD: translucent glass surfaces over a soft
backdrop gradient, rim-light edges, a painted accent **corner-tick frame**
(`HudFrame`) around the EQ graph, and restrained per-widget glow/elevation.
- **9 built-in themes** (default **RUMBLE**) plus a live **System** option
  that mirrors the Windows accent:
  **RUMBLE**, **Nocturne**, **Crimson**, **Ember**, **Verdant**, **Aurora**,
  **Titanium**, **Daylight** (light), **Parchment** (light).
- Each theme is a dict of colour tokens plus optional **structural levers**
  (`radius_scale`, `sheen`, `bevel`, `glow`, `glass_alpha`) that tune its shape
  and gloss; missing levers default to the engine's neutral baseline, so a
  colour-only (imported) theme renders cleanly without inheriting another theme's
  shape.
- A **Theme menu** lists all themes (including imported/custom and System) and
  switches instantly (live in-process rebuild). Themes auto-sort by colour
  family (dark→light), with System last.
- A **Theme Creator** (Settings) builds custom themes from any base; **Import/
  Export** custom themes as `.rumble-theme` files. A saved theme name that no
  longer maps to a registered theme self-heals to the default on load.

**Effect safety.** Real depth comes from `QGraphicsDropShadowEffect` applied only
to safe leaf widgets (`card`/`hero`/`raised`/`primary`); the engine never shadows
the pyqtgraph EQ graph (wrapped in the painted `HudFrame` instead) or any widget
nested inside a scroll area, where a shadow would smear.

### 8.7 Settings
Persisted in `data/profiles.json` (settings + active profile id only). Keys
include: `config_path_override`, `default_bands` (10), `auto_preamp`,
`start_with_windows`, `start_minimised`, `theme`,
`ui_double_size`, `graph_y_range`, `bypassed`,
`show_safety_warning`, `show_builtin_profiles`, `apo_managed`,
`ignored_apo_sources`, `last_active_profile`.
`apo_managed` / `ignored_apo_sources` are owned by the profile manager (changed
live by Hand-back-to-APO and import), so the Settings dialog never writes them
back. (New settings must be added to `DEFAULT_SETTINGS` or they are dropped on
load.)

### 8.8 Accessibility & UX
- Descriptive accessibility text on controls; status tips in the status bar.
- House style: centre-justified value fields, ghost toggle buttons (not
  checkboxes), labels aligned to shared gutters.

---

## 9. Data & File Layout

```
<APO config dir>/                     (e.g. C:\Program Files\EqualizerAPO\config)
├── config.txt                        RUMBLE-owned: marker block + the active Include
├── config.txt.rumble_backup           sacred one-time backup of the user's original
├── config.txt.rumble_drift-<ts>       later displaced content (drift / foreign co-write)
├── rumble_active.txt                  the active pointer RUMBLE rewrites to switch
└── rumble/
    ├── defaults/<id>.txt             built-in profiles
    ├── user/<id>.txt                 user profiles
    ├── irs/<file>                    bundled convolution impulse responses
    └── rumble_preview.txt             live-preview scratch (excluded from scans)

<app data dir>/  (next to the exe when frozen)
├── profiles.json                     settings + active profile id ONLY
└── autoeq_index.md                   cached AutoEQ catalogue (7-day TTL)
```

Profile discovery is **live**: folders are scanned on startup, so files
added/removed/edited externally are reflected immediately. JSON holds no
per-profile index — the files are authoritative.

---

## 10. Constraints & Conventions

- **Windows + Equalizer APO required.** APO must be installed and its config dir
  writable for activation/preview to work.
- **UTF-8** profile files and metadata (names, descriptions, tags) — full
  Unicode supported.
- **Up to 32 bands** per channel section; new profiles default to 10.
- **48 kHz** default sample rate for sample↔ms conversions.
- **Never "all" device** automatically — defaults to the first active EQ device;
  `all` is user-chosen only.
- **One artifact**: RUMBLE profiles are valid APO configs and vice-versa; metadata
  is comments. The APO body is never reflowed on metadata-only edits.
- Unknown APO commands are preserved verbatim — round-tripping is non-destructive.

---

*End of specification.*
