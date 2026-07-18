# RUMBLE Profile File — Construction Reference

> **Audience:** Anyone (human or AI agent) constructing RUMBLE profile files by
> hand or by tool. This document defines exactly how a canonical profile file
> is built: the blocks it contains, the information each block needs, the
> order RUMBLE writes them in, and the complete inventory of every Equalizer
> APO feature RUMBLE supports — so that any profile can carry any feature.
>
> Companion documents: `PROFILE_SPEC.md` (format overview, locations,
> workflows) and `RUMBLE_SPECIFICATION.md` (full APO command reference).

---

## 1. Construction Principle

A profile is **one UTF-8 text file** made of exactly two blocks, in this order:

```
┌──────────────────────────────────────────────┐
│ BLOCK 1 — Metadata header (# comment lines)  │  ← RUMBLE reads this; APO ignores it
├──────────────────────────────────────────────┤
│ BLOCK 2 — APO command body                   │  ← APO executes this; RUMBLE parses it
└──────────────────────────────────────────────┘
```

Because Block 1 is entirely comments, the whole file is a valid Equalizer APO
config. The same file is stored at `<apo_config_dir>/rumble/user/<id>.txt`
(built-ins in `rumble/defaults/<id>.txt`), activated via `Include:`, exported,
and imported — never converted.

---

## 2. Block 1 — Metadata Header

Seven comment lines, written in this exact order, terminated by one blank line:

| # | Line | Required | Content rules |
|---|------|----------|---------------|
| 1 | `# RUMBLE Profile: <name>` | Yes | Display name. Any Unicode, single line, non-empty. |
| 2 | `# Description: <text>` | Yes (value may be empty) | Single line of free text. |
| 3 | `# Tags: <a, b, c>` | Yes (value may be empty) | Comma-separated labels; whitespace around commas is ignored; empty items dropped. |
| 4 | `# Created: <stamp>` | Yes | `YYYY-MM-DDTHH:MM:SS` (ISO 8601, no timezone, no microseconds). |
| 5 | `# Modified: <stamp>` | Yes | Same format as Created. Update on every content change. |
| 6 | `# Readonly: true` | Only when true | Locks the profile in the UI (built-ins like Flat). Never written when false. Ignored on import; stripped on export. |
| 7 | `# Created by RUMBLE — EQ Profile Manager for Equalizer APO` | Yes | Attribution line, always last. |

Then **one blank line**, then the body.

### Header parsing rules (what RUMBLE accepts when reading)

- Keys are case-insensitive; whitespace around `:` is tolerated.
- Only the **leading comment block** (comments/blanks before the first APO
  command) is scanned. Comments later in the file are never treated as metadata.
- The stamp format `YYYY-MM-DD HH:MM:SS` (space instead of `T`) is also
  accepted and normalised.
- Missing lines are not an error: absent fields fall back to the index
  (`profiles.json`) or to defaults, and RUMBLE rewrites the file with the full
  header on next load — preserving the body byte-for-byte.
- Non-RUMBLE comments in the leading block (e.g. a REW header) are preserved,
  repositioned directly below the RUMBLE header.

---

## 3. Block 2 — APO Command Body

RUMBLE writes body sections in a **fixed order**. When constructing files by
hand, follow the same order — it is the order RUMBLE will normalise the file
into on its next structural save:

| Order | Section | Lines | When present |
|-------|---------|-------|--------------|
| 1 | Stage scope | `Stage: <stage 1> [<stage 2> …]` | When restricting the processing stage (§3.9) |
| 2 | Device scope | `Device: <patterns>` | Always written (`Device: all` if unrestricted); omitted only if explicitly empty |
| 3 | Eval expressions | `Eval: <expression>` (one per line) | When expressions/variables are used (§3.10) |
| 4 | Preamp | `Preamp: <g> dB` | When a preamp value is set (recommended: always) |
| 5 | Convolution | `Convolution: <path>` | When an impulse response is set |
| 6 | Channel routing | `Copy: <expr>` (one per line) | When routing/mix expressions exist |
| 7 | VST plugins | `VSTPlugin: <path>` + optional `VSTPlugin Chunks: <base64>` | One pair per plugin, in order |
| 8 | LoudnessCorrection | `LoudnessCorrection: <k1> <v1> <k2> <v2> …` | When a loudness-correction block is present (§3.11) |
| 9 | Channel sections (repeatable) | `Channel:` → `Preamp:` → `Delay:` → `Filter n:` → `GraphicEQ:` | One group per channel scope, in list order |
| 10 | Conditional blocks | `If:` … `ElseIf:` … `Else:` … `EndIf` | Preserved verbatim, in order |
| 11 | Extra lines | anything else (`Include:`, later `Stage:` lines, unknown commands) | Preserved verbatim, always last |

Blank lines between sections are cosmetic; RUMBLE collapses runs of blank
lines to one and APO ignores them entirely.

### 3.1 Device (order 2)

```
Device: all
Device: Realtek USB Audio Headphones; Benchmark
```

- One line. `all` matches every device; otherwise `;`-separated patterns
  matched against device name + connection + GUID.
- Information needed: target device pattern(s), or `all`.

### 3.2 Preamp (order 4)

```
Preamp: -4.5 dB
```

- Signed float, one decimal place when RUMBLE writes it.
- Should be negative enough to absorb the largest positive filter gain:
  `preamp = -(max positive gain) - 1.0` is the safe default (RUMBLE's Auto
  button computes the exact headroom from the summed response).

### 3.3 Convolution (order 5)

```
Convolution: my_room_correction.wav
```

- Path relative to the APO config directory (absolute paths also work).
- Sample rate of the WAV must match the device sample rate.

### 3.4 Copy — channel routing (order 6)

```
Copy: L=L R=R
Copy: L=0.5*L+0.5*R R=0.5*L+0.5*R
Copy: LFE=L L=0.0 R=0.0
```

- One expression set per line; multiple `Copy:` lines allowed and order is
  preserved.
- Information needed: per-target-channel algebraic mix expression.

### 3.5 VSTPlugin (order 7)

```
VSTPlugin: C:\Program Files\VSTPlugins\Limiter.dll
VSTPlugin Chunks: <base64 state blob>
```

- `VSTPlugin Chunks:` is optional plugin state; it must appear on the line(s)
  **after** its `VSTPlugin:` line — RUMBLE attaches chunks to the most recent
  plugin. Multiple plugins are kept in file order.

### 3.6 Channel sections (order 9, repeatable)

Each section scopes everything inside it to a channel selection. Lines within
a section, in order:

```
Channel: L R              ← 1. scope (required to open a section)
Preamp: -1.5 dB           ← 2. optional per-channel preamp ("trim")
Delay: 5.0 ms             ← 3. optional per-channel delay
Filter 1: ON PK Fc 120.0 Hz Gain -2.5 dB Q 1.20     ← 4. filter bands, numbered from 1 per section
Filter 2: ON HS Fc 12000.0 Hz Gain 1.0 dB Q 0.70
GraphicEQ: 20 0; 1000 -1.5; 20000 0                 ← 5. optional graphic EQ curve
```

- **Channel tokens:** `all`, `L`, `R`, `C`, `LFE`, `RL`, `RR`, `SL`, `SR`,
  channel numbers (`1`, `2`, …), or any custom name the device exposes;
  multiple tokens space-separated (`L R`).
- **Preamp:** a `Preamp:` line *inside* a section applies only to that
  section's channels (APO semantics). Multiple preamp lines targeting the
  same channels sum in dB; RUMBLE stores the sum per section
  (`ChannelSection.preamp_db`) and writes one line.
- **Delay:** float, written in ms. RUMBLE also reads `Delay: <n> samples`;
  the samples form is kept verbatim through round-trips (shown in the UI
  converted at the configured processing sample rate) until the value is
  actually edited, at which point it is rewritten in ms.
- **Filters:** see §4. Numbering restarts at 1 in each section (numbers are
  cosmetic to APO; RUMBLE renumbers on save).
- **GraphicEQ:** semicolon-separated `frequency level` pairs (Hz, dB); APO
  interpolates between points (linearly in dB over log-frequency); flat
  outside the defined range. Multiple `GraphicEQ:` lines in one section
  stack in APO (each is its own filter); RUMBLE combines them on read into
  one equivalent line by summing at the union of breakpoints — exact for
  APO's piecewise-linear model.
- A profile may have any number of sections. Filters before any `Channel:`
  line are treated as an implicit `Channel: all` section. Consecutive
  sections with the *same* channel name are merged when RUMBLE reads the file.
- Empty sections (no delay, no filters, no GraphicEQ) are dropped on save.
- **Linked channels have no special syntax.** RUMBLE's editor links channel
  sections by default (one edit clones bands, GraphicEQ and delay to every
  section); a "linked" profile is simply one whose sections carry identical
  content. RUMBLE infers the link state on load: identical sections open
  linked, diverged sections open unlinked.

### 3.7 Conditional blocks (order 10)

```
If: deviceName == "Speakers"
Filter 1: ON PK Fc 500.0 Hz Gain -3.0 dB Q 1.0
ElseIf: sampleRate == 44100
Filter 1: ON PK Fc 1000.0 Hz Gain -2.8 dB Q 2.0
Else:
Filter 1: ON PK Fc 1000.0 Hz Gain -3.0 dB Q 2.0
EndIf
```

- Everything from `If:` to the matching `EndIf` is preserved **verbatim**
  (indentation, inner commands, comments). RUMBLE's UI edits these as raw text
  with syntax highlighting; it does not restructure their contents.
- Runtime variables available: `sampleRate`, `inputChannelCount`,
  `outputChannelCount`, `deviceName`, `connectionName`, `deviceGuid`, `stage`.

### 3.8 Extra lines (order 11)

Any line RUMBLE does not structurally model is preserved verbatim for
round-trip safety and re-emitted at the **end of the body** on save:

```
Include: extras/sub_eq.txt
```

This guarantees no content is ever silently dropped — but note that a
structural save will *relocate* such lines to the end, which can change their
scoping relative to `Channel:`/`Device:` lines. What is and is not
structurally modelled:

- The **first** `Stage:` line is structured (`ProfileConfig.stage`) and always
  rendered as the first body line. Any *later* `Stage:` lines (mid-file stage
  switching) stay verbatim here at the end — if a profile depends on them,
  keep that logic inside a conditional block (preserved in place) or accept
  end-of-file placement.
- **All** `Eval:` lines are structured (`ProfileConfig.eval_lines`) and
  rendered together right after `Device:`. Hoisting widens a variable's scope
  (safe for the usual define-once-use-later pattern); a profile that
  *reassigns* the same variable mid-file will see only the last value after a
  structural save.
- `Include:` and unknown commands are always verbatim extra lines.
- `LoudnessCorrection:` (written by Equalizer APO's own loudness-calibration
  tool, not by RUMBLE) is **structured** as of Phase 1
  (`ProfileConfig.loudness_correction`, a generic ordered key/value list so
  unrecognised keys still round-trip): rendered after VST plugins, before
  channel sections. See §3.11 for the full grammar.

### 3.9 Stage (order 1)

```
Stage: post-mix
Stage: pre-mix post-mix capture
```

- Restricts every following command to the named processing stage(s):
  `pre-mix` (per input channel, before mixing), `post-mix` (after mixing to
  the output layout) and/or `capture` (recording devices, e.g. microphones).
  When absent APO defaults to `post-mix capture`.
- RUMBLE stores the first Stage line (lower-cased) in `ProfileConfig.stage`
  and writes it as the first body line so it scopes the whole profile.

### 3.10 Eval (order 3)

```
Eval: linGain = 0.5
```

- Evaluates an APO expression, typically to define variables used (via
  backticks) in later commands. One expression per line.
- RUMBLE collects every `Eval:` line into `ProfileConfig.eval_lines` and
  writes them directly after `Device:` so definitions precede use.

### 3.11 LoudnessCorrection (order 8)

```
LoudnessCorrection: State 1 ReferenceLevel 0 ReferenceOffset 0 Attenuation 1.0
```

- Written by Equalizer APO's own loudness-calibration tool (`Editor.exe`),
  not authored from scratch by RUMBLE's EQ engine — RUMBLE structures it for
  round-trip safety and light editing, not for running the calibration flow
  itself. Global (profile-level), not per-channel; at most one per profile.
- Generic ordered key/value token pairs, in file order. Confirmed keys (from
  `Editor.exe`'s own default output — see
  `docs/superpowers/plans/phase1-grammar-notes.md`): `State` (`1`/`0`
  on/off), `ReferenceLevel` (dB, calibration reference), `ReferenceOffset`
  (dB, offset against the reference), `Attenuation` (mix/curve-strength
  ratio). Any other key APO ever writes round-trips losslessly alongside
  them, in its original position.
- RUMBLE stores it as `ProfileConfig.loudness_correction`
  (`None` when the profile has no block at all), with typed accessors
  `get(key)` / `set(key, value)` and an `enabled` property backed by
  `State`. Written after `VSTPlugin` lines, before the first `Channel:`
  section (§3 order table, row 8).
- RUMBLE's Advanced → Loudness editor exposes the on/off toggle plus
  `ReferenceLevel`/`ReferenceOffset` as two centre-justified spin fields;
  any other key already in the block (e.g. `Attenuation`) is left untouched
  rather than rebuilt from UI state, and a field the user never edits keeps
  its original string value (and precision) exactly, even if the spin box
  can't represent it losslessly. Turning the toggle off on a pre-existing
  block keeps the block with `State 0` rather than deleting it.
- A line that matches `LoudnessCorrection:` but carries zero tokens, or an
  odd number of tokens (not a complete set of key/value pairs), is not
  treated as a loudness block at all — it falls back to a verbatim
  `extra_lines` entry like any other unrecognised command (§3.8).

---

## 4. Filter Lines — Complete Reference

### 4.1 Standard form

```
Filter <n>: ON|OFF <type> Fc <hz> Hz [Gain <g> dB] [Q <q> | BW Oct <bw>]
```

Information needed per band: enabled state, type, frequency, and (depending
on type) gain and Q **or** bandwidth.

RUMBLE's written precision: `Fc` one decimal, `Gain` one decimal, `Q` two
decimals, `BW Oct` two decimals.

### 4.2 Every supported filter type

| Type token | Meaning | Gain | Q | BW Oct alternative |
|-----------|---------|------|---|--------------------|
| `PK` | Peaking (parametric bell) | Yes | Yes | Yes |
| `PEQ` | Peaking, BW-octave convention | Yes | Yes | Yes |
| `Modal` | Modal (room mode) | Yes | Yes | Yes |
| `LP` | Low-pass | — | Yes (optional) | Yes |
| `LPQ` | Low-pass with Q | — | Yes | Yes |
| `HP` | High-pass | — | Yes (optional) | Yes |
| `HPQ` | High-pass with Q | — | Yes | Yes |
| `BP` | Band-pass | — | Yes | Yes |
| `LS` | Low-shelf | Yes | Yes | Yes |
| `LSC` | Low-shelf, centre-frequency | Yes | Yes | Yes |
| `LS 6dB` | Low-shelf, fixed 6 dB/oct | Yes | — | — |
| `LS 12dB` | Low-shelf, fixed 12 dB/oct | Yes | — | — |
| `HS` | High-shelf | Yes | Yes | Yes |
| `HSC` | High-shelf, centre-frequency | Yes | Yes | Yes |
| `HS 6dB` | High-shelf, fixed 6 dB/oct | Yes | — | — |
| `HS 12dB` | High-shelf, fixed 12 dB/oct | Yes | — | — |
| `NO` | Notch | — | Yes | Yes |
| `AP` | All-pass | — | Yes | — |
| `IIR` | Custom IIR by coefficients | see §4.3 | | |

Parameter ranges enforced by the RUMBLE UI: Fc 1–96000 Hz (practical
20–20000), Gain −30…+30 dB, Q 0.1–100, BW Oct 0.01–10.

For every Q-capable type RUMBLE **always writes an explicit Q** (default
`Q 1.00` when none was specified), including plain `LP`/`HP` — APO treats the
Q on those as optional but accepts it.

### 4.3 Custom IIR form

```
Filter <n>: ON IIR Order <order> Coefficients <b0/a0> <b1/a0> ... <a1/a0> <a2/a0> ...
```

- Coefficient count must be `2 * (order + 1)`.
- Information needed: order and the full normalised coefficient list (RUMBLE
  stores the coefficient string verbatim).

### 4.4 Parser leniencies (accepted on read, normalised on save)

- The band number (`Filter 3:`) is optional and ignored; bands are renumbered.
- `ON`/`OFF`, type tokens and keywords are case-insensitive.
- `BW Oct` takes precedence over `Q` if both appear on a line.
- Missing `Fc` defaults to 1000 Hz, missing `Gain` to 0 dB, missing `Q` to 1.0.

---

## 5. Feature Coverage Matrix

Every Equalizer APO feature RUMBLE currently supports, and where it lives in
the canonical file. **Any profile may carry any combination of these** — the
format, parser, renderer and UI handle all of them in a single file:

| APO feature | File section (§) | Parsed to structure | Editable in UI |
|---|---|---|---|
| `Preamp` | §3.2 | `ProfileConfig.preamp` | Preamp spinbox + Auto/headroom |
| `Filter` — all 18 standard types | §4.2 | `Filter` per band | Filter table + draggable EQ graph |
| `Filter` — BW Oct variant | §4.2 | `Filter.use_bw/bw_oct` | Q ⇄ BW toggle per band |
| `Filter` — custom `IIR` | §4.3 | `Filter.iir_order/iir_coefficients` | Raw coefficient entry |
| `GraphicEQ` | §3.6 | per `ChannelSection.graphic_eq` | Point-drag GraphicEQ editor |
| `Convolution` | §3.3 | `ProfileConfig.convolution` | File picker (Advanced) |
| `Delay` (ms or samples, unit preserved) | §3.6 | per `ChannelSection.delay_ms` / `delay_samples` | Per-channel delay row + link |
| `Preamp` inside a `Channel:` section | §3.6 | per `ChannelSection.preamp_db` | Per-channel Trim spin |
| `Copy` (routing/mixing) | §3.4 | `ProfileConfig.copy_lines` | Expression list (Advanced) |
| `Device` | §3.1 | `ProfileConfig.device` | Device combo |
| `Channel` (multi-section) | §3.6 | `ProfileConfig.channel_sections` | Channel tabs + layout presets |
| `VSTPlugin` + `VSTPlugin Chunks` | §3.5 | `ProfileConfig.vst_plugins` | Plugin list (Advanced) |
| `If`/`ElseIf`/`Else`/`EndIf` | §3.7 | `ProfileConfig.conditional_blocks` (verbatim) | Raw block editor (Advanced) |
| `Stage` (leading line) | §3.9 | `ProfileConfig.stage` | "Apply during" selector (Advanced) |
| `Eval` | §3.10 | `ProfileConfig.eval_lines` | Scripting tab (Advanced) |
| `LoudnessCorrection` | §3.11 | `ProfileConfig.loudness_correction` | Advanced → Loudness editor (toggle + Reference level/Offset spin) |
| Later `Stage:` lines, `Include`, unknown commands | §3.8 | `ProfileConfig.extra_lines` (verbatim) | Preserved; visible in Raw Config |
| Profile metadata (name/description/tags/dates/readonly) | §2 | `ProfileMeta` / header | Name & description fields; tags in sidebar |

---

## 6. Full-Feature Template

A single profile exercising **every** supported feature, in canonical order.
This file parses, renders, activates and round-trips through RUMBLE unchanged:

```
# RUMBLE Profile: Kitchen Sink Reference
# Description: Demonstrates every Equalizer APO feature RUMBLE supports
# Tags: Reference, Demo
# Created: 2026-06-09T12:00:00
# Modified: 2026-06-09T12:00:00
# Created by RUMBLE — EQ Profile Manager for Equalizer APO

Stage: post-mix

Device: all

Eval: linGain = 0.5

Preamp: -6.5 dB

Convolution: room_correction.wav
Copy: L=0.9*L+0.1*R R=0.9*R+0.1*L

VSTPlugin: C:\Program Files\VSTPlugins\Limiter.dll
VSTPlugin Chunks: AAEXAMPLEBASE64STATE==

LoudnessCorrection: state 0

Channel: L R
Delay: 2.5 ms
Filter 1: ON HP Fc 20.0 Hz Q 1.00
Filter 2: OFF HPQ Fc 25.0 Hz Q 0.70
Filter 3: ON LS Fc 80.0 Hz Gain 4.0 dB Q 0.71
Filter 4: ON LS 6dB Fc 60.0 Hz Gain 2.0 dB
Filter 5: ON LS 12dB Fc 50.0 Hz Gain 1.5 dB
Filter 6: ON LSC Fc 100.0 Hz Gain 2.0 dB Q 0.90
Filter 7: ON PK Fc 250.0 Hz Gain -6.0 dB BW Oct 1.00
Filter 8: ON PEQ Fc 500.0 Hz Gain -2.0 dB Q 1.50
Filter 9: ON Modal Fc 63.0 Hz Gain -8.0 dB Q 8.00
Filter 10: ON BP Fc 1000.0 Hz Q 1.41
Filter 11: ON NO Fc 50.0 Hz Q 10.00
Filter 12: ON AP Fc 900.0 Hz Q 0.71
Filter 13: ON HS Fc 8000.0 Hz Gain 2.5 dB Q 0.71
Filter 14: ON HS 6dB Fc 10000.0 Hz Gain 1.0 dB
Filter 15: ON HS 12dB Fc 11000.0 Hz Gain 1.0 dB
Filter 16: ON HSC Fc 9000.0 Hz Gain -1.5 dB Q 0.80
Filter 17: ON LPQ Fc 19000.0 Hz Q 0.70
Filter 18: ON LP Fc 20000.0 Hz Q 1.00
Filter 19: ON IIR Order 2 Coefficients 0.0380602 0.0761205 0.0380602 1.2706 -1.84776 0.729402
GraphicEQ: 20 0; 100 -1.0; 1000 0; 10000 1.5; 20000 0

Channel: LFE
Preamp: -1.5 dB
Delay: 384 samples
Filter 1: ON LP Fc 120.0 Hz Q 1.00
Filter 2: ON PK Fc 45.0 Hz Gain 3.0 dB Q 1.00

If: deviceName == "Speakers"
Filter 20: ON PK Fc 500.0 Hz Gain -3.0 dB Q 1.00
ElseIf: sampleRate == 44100
Filter 20: ON PK Fc 1000.0 Hz Gain -2.8 dB Q 2.00
Else:
Filter 20: ON PK Fc 1000.0 Hz Gain -3.0 dB Q 2.00
EndIf
```

> Verified: this exact template round-trips through RUMBLE's parser and
> renderer with all 19+2 filter bands, both channel sections, the per-channel
> preamp, the samples-unit delay, GraphicEQ, convolution, routing, VST state,
> the conditional block, the structured `Stage:` / `Eval:` lines, and the
> structured `LoudnessCorrection:` block intact (see
> `tests/selftest_profile_format.py`, section 10). See §3.8 for
> `LoudnessCorrection:` grammar notes.

---

## 7. Construction Checklist

Build order summary — write the file top to bottom:

1. **Header (7 lines + blank):** name → description → tags → created →
   modified → (readonly) → attribution.
2. **Stage:** only when restricting the processing stage.
3. **Device:** `Device: all` unless scoping to specific hardware.
4. **Eval expressions**, if any, so definitions precede use.
5. **Preamp:** always include; `-(max positive gain) - 1.0` dB if unsure.
6. **Global processing:** Convolution, then Copy lines, if used.
7. **VST plugins:** path line, then its Chunks line, per plugin.
8. **One group per channel scope:** `Channel:` → `Preamp:` → `Delay:` →
   numbered `Filter` lines (from 1) → `GraphicEQ:`.
9. **Conditional blocks**, if any.
10. **Anything else** (`Include:`, later `Stage:` lines) last.

Validate before shipping:

- [ ] UTF-8, plain text, `.txt` extension.
- [ ] Header complete and in order; stamps are `YYYY-MM-DDTHH:MM:SS`.
- [ ] At least one `Filter` or `GraphicEQ` line.
- [ ] Every filter type token is from §4.2; gain/Q only on types that take them.
- [ ] IIR coefficient count = `2 * (order + 1)`.
- [ ] `VSTPlugin Chunks:` follows its `VSTPlugin:` line.
- [ ] Preamp present and negative enough for the largest boost.
- [ ] No two consecutive sections share the same channel name (they'd merge).
- [ ] File placed at `rumble/user/<id>.txt` (or imported via the UI).
