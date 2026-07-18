# RUMBLE Profile Specification

> **Audience:** Any AI agent or human wanting to create EQ profiles for RUMBLE.  
> This document fully describes the canonical profile file format, file
> locations, metadata schema, and both automated and manual authoring workflows.

---

## 1. What a Profile Is

A RUMBLE profile is **one single, portable, self-contained text file**. The
same file type is used everywhere — storage, activation, import, export, and
round-trip editing:

| Stage | The same file |
|-------|---------------|
| **Storage** | `<apo_config_dir>/rumble/user/<id>.txt` |
| **Activation** | Referenced by `Include:` from `rumble_active.txt` |
| **Export** | Copied/rendered to any destination as-is |
| **Import** | Any canonical file (or plain APO config) can be imported |

The file embeds *all* profile metadata (name, description, tags, timestamps)
as `#` comment lines in a header block, followed by the Equalizer APO
commands. Because APO ignores comments, **the canonical RUMBLE profile file is
also a valid Equalizer APO config file** — no translation step exists between
"RUMBLE profile" and "APO config"; they are one artifact in different stages
of processing. Native profiles and imported profiles are the same thing.

`<data_dir>/profiles.json` still exists, but it is a **derived index/cache**,
not the source of truth: it stores app settings, profile ordering and the
active flag. At startup RUMBLE reconciles it against the canonical files —
metadata in a file wins over the index, legacy files are migrated in place,
and unindexed `.txt` files found in `rumble/` are registered automatically.

RUMBLE activates a profile by writing an `Include:` pointer in
`<apo_config_dir>/rumble_active.txt`, which is itself included by APO's main
`config.txt`. Only one profile is active at a time.

---

## 2. File Locations

### Equalizer APO config directory

Default: `C:\Program Files\EqualizerAPO\config\`  
(Discovered from the Windows registry: `HKLM\SOFTWARE\EqualizerAPO\ConfigPath`)

```
<apo_config_dir>/
  config.txt                   ← APO entry point; RUMBLE prepends "Include: rumble_active.txt"
  rumble_active.txt             ← Written by RUMBLE; points to the active profile file
  rumble/
    flat.txt                   ← Built-in flat/reference profile (readonly)
    <id>.txt                   ← One canonical profile file per profile
    ...
```

### RUMBLE data directory

Next to `RUMBLE.exe` (or `%APPDATA%\RUMBLE\` in some installs):

```
<data_dir>/
  profiles.json                ← Derived index (ordering, active flag) + app settings
```

---

## 3. Canonical Profile File Format

Each profile is a plain UTF-8 text file. It begins with a metadata header of
`#` comment lines, followed by APO commands:

```
# RUMBLE Profile: <Name>
# Description: <single line, may be empty>
# Tags: <comma-separated, may be empty>
# Created: YYYY-MM-DDTHH:MM:SS
# Modified: YYYY-MM-DDTHH:MM:SS
# Readonly: true                          ← only present when user-locked
# Builtin: true                           ← only present on system-protected built-ins
# Created by RUMBLE

Device: <device>

Preamp: <value> dB

Convolution: <path_to_wav>
Copy: <expression>

VSTPlugin: <path_to_dll>
VSTPlugin Chunks: <base64_data>

Channel: <channel>
Delay: <value> ms
Filter 1: ON <type> Fc <hz> Hz Gain <db> dB Q <q>
Filter 2: ...
GraphicEQ: 20 0; 25 0; ...

If: <condition>
...
EndIf
```

### 3.1 Header field rules

| Header line | Maps to | Rules |
|-------------|---------|-------|
| `# RUMBLE Profile:` | name | Display name; any Unicode; non-empty |
| `# Description:` | description | Single line of free text; may be empty (line still present) |
| `# Tags:` | tags | Comma-separated labels; may be empty (line still present) |
| `# Created:` | created | ISO 8601 `YYYY-MM-DDTHH:MM:SS` (legacy `YYYY-MM-DD HH:MM:SS` accepted) |
| `# Modified:` | modified | Same format as Created |
| `# Readonly: true` | readonly | Optional; only written when true; user lock — blocks edits/deletion until unlocked in the UI |
| `# Builtin: true` | builtin | Optional; only written when true; system protection for the seeded built-in presets — never editable, lockable, renamable or deletable; shown under the "Built-In" filter with a shield badge |

Notes:
- The header is scanned only in the **leading comment block** (before the
  first APO command). Comments elsewhere are left alone.
- Files with an incomplete header (older RUMBLE versions wrote only the name
  and modified lines; foreign files have no header) still load fine — missing
  fields fall back to the index or sensible defaults, and RUMBLE rewrites the
  file with a full header on the next startup (the APO command body is
  preserved byte-for-byte).
- Exported files never carry `# Readonly:` or `# Builtin:`, and both flags
  are ignored on import, so exported locked/built-in profiles become normal
  editable profiles.
- The built-in profiles live in `core/builtin_profiles_data.py` and are seeded as
  `rumble/builtin_<profile_id>.txt` on startup
  (`ProfileManager.ensure_builtin_profiles()`); a deleted file is regenerated
  on the next launch, and a seeded file whose curve or description no longer
  matches the canonical profile table is refreshed (built-ins are system
  protected, so a mismatch only ever means the table itself was revised).
- Blank lines between sections are cosmetic only. APO ignores them.

---

## 4. Filter Syntax Reference

### 4.1 Standard filter line

```
Filter <n>: ON|OFF <type> Fc <hz> Hz [Gain <db> dB] [Q <q>|BW Oct <bw>]
```

| Field | Required | Notes |
|-------|----------|-------|
| `n` | Yes | Sequential integer starting at 1 |
| `ON\|OFF` | Yes | Enabled/disabled toggle |
| `type` | Yes | See §4.2 |
| `Fc <hz> Hz` | Yes | Centre/corner frequency in Hz |
| `Gain <db> dB` | Depends on type | Signed float; shelf/peaking types only |
| `Q <q>` | Depends on type | Quality factor (dimensionless positive float) |
| `BW Oct <bw>` | Alternative to Q | Bandwidth in octaves; replaces Q when present |

### 4.2 Filter types

| Category | Type tokens | Has Gain | Has Q |
|----------|-------------|----------|-------|
| Peaking | `PK` `PEQ` `Modal` | Yes | Yes |
| Low-pass | `LP` `LPQ` | No | Yes (optional for LP; RUMBLE writes `Q 1.00` by default) |
| High-pass | `HP` `HPQ` | No | Yes (optional for HP; RUMBLE writes `Q 1.00` by default) |
| Band-pass | `BP` | No | Yes |
| Low-shelf | `LS` `LSC` `LS 6dB` `LS 12dB` | Yes | LS/LSC: yes; 6dB/12dB: no |
| High-shelf | `HS` `HSC` `HS 6dB` `HS 12dB` | Yes | HS/HSC: yes; 6dB/12dB: no |
| Notch | `NO` | No | Yes |
| All-pass | `AP` | No | Yes |
| Custom IIR | `IIR` | — | — |

### 4.3 IIR filter line

```
Filter <n>: ON|OFF IIR Order <order> Coefficients <b0/a0> <b1/a0> ... <a1/a0> ...
```

### 4.4 Examples

```
Filter 1: ON PK Fc 1000.0 Hz Gain -3.0 dB Q 1.41
Filter 2: ON HS Fc 8000.0 Hz Gain 2.5 dB Q 0.71
Filter 3: OFF HP Fc 20.0 Hz
Filter 4: ON PK Fc 250.0 Hz Gain -6.0 dB BW Oct 1.00
```

---

## 5. Other Commands

### Preamp
```
Preamp: -6.0 dB
```
Global gain (usually negative to prevent clipping). Recommended: auto-calculate
so the loudest filter doesn't clip (typically `−max_positive_gain − 1 dB`).

### Device
```
Device: all
```
Restrict the profile to a specific audio device name, or `all` for every device.

**Portability:** when a profile arrives on a machine (via File → Import or by
dropping the file into `rumble/`) and its `Device:` pattern matches none of the
APO-enabled endpoints there, RUMBLE rewrites that line to `Device: all` so the
profile is audible immediately instead of silently applying to nothing. The
rewrite only happens when endpoint enumeration succeeds, and only the
unmatched `Device:` lines change — the rest of the file is untouched.
Profiles already in the library are never remapped.

### Channel
```
Channel: all
Channel: L R
Channel: L
Channel: R
```
Subsequent `Filter` and `GraphicEQ` lines apply only to these channels.
Valid tokens: `all`, `L`, `R`, `L R`, `C`, `LFE`, `RL`, `RR`, `RL RR`, or any
custom channel name the device exposes.

### Delay
```
Delay: 5.0 ms
```
Per-channel delay in milliseconds. Place after `Channel:` and before filters.

### GraphicEQ
```
GraphicEQ: 20 0; 25 0; 31.5 -1.2; 40 -2.0; ... 20000 0
```
Space-separated `frequency level` pairs, separated by semicolons. Frequency in
Hz, level in dB. APO interpolates between points.

### Convolution (impulse response)
```
Convolution: my_room.wav
```
Path is relative to the APO config directory. WAV must be mono or stereo.

### Copy (channel mixing)
```
Copy: L=L R=R
Copy: L=0.5*L+0.5*R R=0.5*L+0.5*R
```
Remaps or mixes channels using algebraic expressions.

### VSTPlugin
```
VSTPlugin: C:\path\to\plugin.dll
VSTPlugin Chunks: <base64>
```

### Conditional blocks
```
If: <device name>
Filter 1: ON PK Fc 500.0 Hz Gain -3.0 dB Q 1.0
EndIf
```

---

## 6. The Index (`profiles.json`)

The index is **derived from the canonical files** — when the two disagree,
the file wins. It persists only what the file cannot: app settings, list
ordering, and which profile is currently active.

```json
{
  "profiles": [
    {
      "id":          "studio_reference",
      "name":        "Studio Reference",
      "description": "Flat target for mastering work",
      "tags":        ["Reference", "Mastering"],
      "file":        "rumble/studio_reference.txt",
      "created":     "2026-06-09T14:00:00",
      "modified":    "2026-06-09T14:30:00",
      "active":      false,
      "readonly":    false
    }
  ],
  "settings": {
    "config_path_override": null,
    "default_bands": 10,
    "auto_preamp": true,
    "start_with_windows": true,
    "start_minimised": true,
    "theme": "RUMBLE",
    "graph_y_range": 30,
    "bypassed": false
  }
}
```

### Index field rules

| Field | Type | Rules |
|-------|------|-------|
| `id` | string | URL-safe slug: lowercase, underscores only. Must be unique. Generated by slugifying the name. |
| `name` / `description` / `tags` / `created` / `modified` / `readonly` | — | Cached copies of the canonical file's header (see §3.1). |
| `file` | string | Always `rumble/user/<id>.txt`. Forward slashes. |
| `active` | bool | Exactly one profile should have `true`. RUMBLE manages this; it is deliberately *not* part of the portable file. |

---

## 7. ID Generation Rules

The `id` is derived from the profile name (or, for files discovered in the
`rumble/` directory, from the filename stem):
1. Lowercase the name.
2. Replace any run of non-alphanumeric characters with a single underscore.
3. Strip leading/trailing underscores.
4. If the result is empty, use `"profile"`.
5. If the id already exists, append `_2`, `_3`, etc.

Examples: `"Studio Reference"` → `studio_reference`, `"Beyerdynamic DT 990 Pro"` → `beyerdynamic_dt_990_pro`

---

## 8. Creating Profiles — Automated (AI Agent)

Because the canonical file is self-contained, an agent only has to **write
one file** — no JSON editing required.

### Step 1 — Locate the APO config directory

Read `HKLM\SOFTWARE\EqualizerAPO\ConfigPath` from the registry, or default to
`C:\Program Files\EqualizerAPO\config\`. (If RUMBLE's `profiles.json` sets
`config_path_override`, that path wins.)

### Step 2 — Design the EQ curve

Determine the filter parameters. Guidelines:
- Keep preamp negative enough to prevent clipping: `preamp = -(max positive gain) - 1.0`
- Use `PK` (peaking) for targeted boosts/cuts.
- Use `LS`/`HS` for broad tonal shaping.
- Use `HP`/`LP` to roll off subsonic rumble or unwanted highs.
- Prefer Q values between 0.5 (very broad) and 8.0 (very narrow).
- For headphone profiles, a typical correction has 8–15 bands.

### Step 3 — Write the canonical file

Write to `<apo_config_dir>/rumble/user/<id>.txt` (create the `rumble/user/`
subdirectory if it doesn't exist; built-in profiles live in `rumble/defaults/`).
Minimal valid example:

```
# RUMBLE Profile: Warm Bass Boost
# Description: Elevated low end with a slight high-frequency rolloff
# Tags: Music, Headphones
# Created: 2026-06-09T14:00:00
# Modified: 2026-06-09T14:00:00
# Created by RUMBLE

Device: all

Preamp: -4.0 dB

Channel: all
Filter 1: ON LS Fc 200.0 Hz Gain 4.0 dB Q 0.71
Filter 2: ON PK Fc 80.0 Hz Gain 3.0 dB Q 0.80
Filter 3: ON HS Fc 8000.0 Hz Gain -1.0 dB Q 0.71
```

The header lines are optional but recommended — a bare APO config also works
(RUMBLE derives the name from the filename and fills in the header on first
load).

### Step 4 — Restart or reload RUMBLE

RUMBLE scans `rumble/` on startup and registers any new files automatically.
If RUMBLE is running, either kill the process (`taskkill /IM RUMBLE.exe /F`)
and relaunch, or use **File → Import Profile…** in the UI to load the file
immediately.

---

## 9. Creating Profiles — Manual

### Option A: Via RUMBLE UI (recommended)

1. Open RUMBLE.
2. Click **+ New Profile** in the left sidebar.
3. Enter a name and optional description/tags.
4. Use the filter table and EQ graph to design the curve.
5. Click **Save**. The canonical file is written automatically (and the index updated).

### Option B: Import an existing file

1. In RUMBLE, use **File → Import Profile…** (or the import button in the sidebar).
2. Select any `.txt` file — a canonical RUMBLE profile or a plain APO config
   (e.g. a REW export) — or a `.zip` profile bundle (see below). One import
   handles both; the file type alone says whether an impulse response is
   included.
3. RUMBLE uses the embedded metadata when present (name, description, tags,
   created), otherwise derives a name from the filename.

### Profile bundles (`.zip`) — profiles with a Convolution IR

A profile's `Convolution:` line references an impulse response file that is
not part of the profile text. To share such profiles intact, RUMBLE uses a
zip **bundle**:

```
<profile_id>.txt              ← canonical profile, Convolution: irs/<file>
irs/<impulse_response>.wav    ← the referenced IR file
```

- **Export:** profiles without convolution always export as plain `.txt`.
  When a profile has a `Convolution:` line *and* the IR file is found on
  disk, RUMBLE offers a choice: bundle (`.zip`, sounds identical elsewhere)
  or plain `.txt` (the convolution step is skipped wherever the file is
  missing).
- **Import:** a bundle's IR is stored at `<apo_config_dir>/rumble/irs/` and
  the `Convolution:` line repointed to the relative `irs/<file>` path
  (APO resolves it against the profile's own directory). An identical file
  already there is reused; a different file with the same name is never
  overwritten — the import gets a numbered copy (`name_2.wav`) instead.
- Hand-made zips work too: the first `.txt` member is taken as the profile,
  and the IR is matched by archive path or bare filename.

### Option C: Drop a file into the rumble/ directory

1. Write a canonical profile file (see §3) — or any valid APO config.
2. Copy it to `<apo_config_dir>/rumble/<anything>.txt`.
3. Restart RUMBLE — the file is discovered and registered automatically.

---

## 10. Activating a Profile (How RUMBLE Does It)

For reference — RUMBLE handles this automatically, but for manual workflows:

1. Set `"active": true` on the desired profile in `profiles.json`; set all others to `false`.
2. Overwrite `<apo_config_dir>/rumble_active.txt` with:
   ```
   # RUMBLE active profile pointer — do not edit by hand
   Include: rumble/user/<id>.txt
   ```
3. APO picks up the change in real time (no reboot required).

To bypass all EQ, write this to `rumble_active.txt` instead:
```
# RUMBLE bypass — EQ temporarily disabled
# This file intentionally contains no active commands.
```

---

## 11. Validation Checklist

Before a profile is considered valid, verify:

- [ ] File exists at `rumble/user/<id>.txt` relative to the APO config dir.
- [ ] File is UTF-8 encoded.
- [ ] Header starts with `# RUMBLE Profile: <name>` (recommended; auto-filled if absent).
- [ ] `Created` / `Modified` header values are `YYYY-MM-DDTHH:MM:SS`.
- [ ] At least one `Filter` line or a `GraphicEQ` line is present.
- [ ] All `Filter` lines use a recognised type token (see §4.2).
- [ ] `Preamp` value is present and negative enough to prevent clipping.
- [ ] The file parses as a valid APO config (comments aside, every line is an APO command).
