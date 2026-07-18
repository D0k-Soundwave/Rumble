# RUMBLE Feature Map

A complete, code-verified inventory of every feature in RUMBLE: what it does, how a
user reaches it, which entities it touches, and the exact math/logic path behind it.
Compiled 2026-07-16 by reading every module in `rumble/core/` and `rumble/ui/` (not
from memory, not from older docs — this supersedes any prior feature list where they
disagree). Written to also serve as the source material for the in-app About dialog.

Conventions used below: **[UI]** = where a user reaches this; **[Math]** = the exact
formula/algorithm; **[Entities]** = the data objects/files involved.

---

## 1. Core entity model

- **`ProfileConfig`** (`apo_config.py`) — the parsed/structured form of one Equalizer
  APO config file: `device`, `preamp`, `stage`, `eval_lines`, `channel_sections`
  (list of `ChannelSection`), `vst_plugins`, `convolution`, `copy_lines`,
  `conditional_blocks` (If/ElseIf/Else/EndIf, verbatim), `loudness_correction`,
  `extra_lines` (verbatim round-trip preservation of anything unrecognised).
- **`ChannelSection`** — one `Channel: ...` block: `filters` (list of `Filter`),
  `graphic_eq` (a `GraphicEQ:` value string), `delay_ms`/`delay_samples`,
  `preamp_db` (per-channel trim).
- **`Filter`** — one filter band: `type` (PK/PEQ/Modal/LP/LPQ/HP/HPQ/BP/LS/LSC/
  LS 6dB/LS 12dB/HS/HSC/HS 6dB/HS 12dB/NO/AP/IIR), `fc`, `gain`, `q` or `bw_oct`,
  `enabled`. IIR carries raw `iir_order`/`iir_coefficients` instead (parses/
  round-trips; no authoring UI yet — `IIR_EDITOR_AVAILABLE = False`).
- **`ProfileMeta`** (`profile_manager.py`) — the library-index record for one
  profile: id, name, description, tags, file path, timestamps, `active`,
  `readonly` (user-locked), `builtin` (system-protected), `stage`, APO-import
  provenance fields, `fit_sample_rate` (marks a curve-fitted profile's residual
  as re-solvable).
- **Profile file** (`profile_file.py`) — a RUMBLE profile *is* a valid Equalizer
  APO config file: a `#`-comment metadata header (name/description/tags/created/
  modified/readonly/builtin/APO-source-provenance/fit-sample-rate) followed
  directly by the APO command body. No separate "RUMBLE format" — the same file
  round-trips as both.
- **Listener Slot** (`listener_slots.py`) — one of 4 fixed identity slots holding
  a person's Ear Calibration history: up to 10 retained saved runs, a persistent
  per-point "calibration state" (redo any frequency/ear any time), and a custom
  display name.
- **Settings** (`profile_manager.DEFAULT_SETTINGS`, ~90 keys) — one flat JSON dict
  in `data/profiles.json` alongside the active-profile pointer; every toggle
  below is a key in it.

Storage layout: `<APO config dir>/rumble/defaults/*.txt` (built-ins),
`rumble/user/*.txt` (user profiles), `rumble/irs/` (convolution impulse responses),
`data_dir/mic_cal/`, `data_dir/listeners/<slot>/hearing/`, `data_dir/trash/`
(deleted-profile recycle bin), `data_dir/profiles.json` (settings + active id).

---

## 2. Profile library & lifecycle

**[UI]** Left sidebar (`profile_panel.py`) — searchable `QTreeWidget`, grouped by a
computed tag hierarchy (`tag_hierarchy.py`: each profile lands at exactly one leaf,
ordered by tag frequency across the library, deepened only as far as a tag actually
subdivides the group). File menu: New/Import/Export/Rename/Duplicate/Delete/Lock.
System tray: instant profile switching without opening the main window
(`tray_icon.py`, `toast_menu.py`).

- **Create** — flat starter profile (`make_flat_config`): PK bands at 0 dB at the
  classic 10 ISO-octave centres (31…16k Hz) for ≤10 bands, log-spaced for more, up
  to `MAX_FLAT_BANDS = 32`. **[UI]** New Profile dialog (`new_profile_dialog.py`).
- **Duplicate / Rename / Delete / Lock** — standard CRUD. Delete is non-destructive:
  moved to `data_dir/trash/` (`<utc-ts>_<id>.txt` + JSON sidecar), capped at 25
  entries (oldest purged first). **[UI]** Trash/recycle-bin dialog
  (`profile_trash_dialog.py`) restores or permanently purges.
- **Built-in profiles** — 7 authored, hardware-agnostic profiles
  (`builtin_profiles_data.py`), system-protected (can't edit/delete/lock-toggle),
  shield badge in the tree, re-synced on every launch (`ensure_builtin_profiles`
  wipes stale copies).
- **Import** — plain `.txt` APO config, `.zip` bundle (profile + its convolution
  IR, re-homed into `rumble/irs/`), Peace `.peace` INI saves, REW filter-settings
  exports. Batch import reports per-file failures rather than aborting.
- **Export** — single `.txt`, or a `.zip` bundle carrying any convolution IR the
  profile references.
- **Backup / Restore** (`backup_library`/`restore_library`) — one zip snapshotting
  the *entire* data dir plus the APO `rumble/` tree (profiles, mic cals, listener
  history, custom themes, IRs); restore is verbatim. **[UI]** Settings →
  Backup & Restore.
- **Foreign-file tolerance** — a bare foreign APO config with no header at all
  parses fine (absent fields read as `None`) and is migrated to the full header
  in place on first load.

---

## 3. Equalizer APO integration & ownership

**[Entities]** `config.txt` (APO's real config, in the registry-discovered
`ConfigPath`), `rumble_active.txt` (RUMBLE's Include pointer), `rumble_preview.txt`
(live-edit/ABX scratch), `rumble_listener_active.txt` / `rumble_device_<hash>.txt`
(composed-render scratch files).

- **Path discovery** (`apo_path.py`) — reads `HKLM\SOFTWARE\EqualizerAPO`
  (`InstallPath`, `ConfigPath`), falls back to `C:\Program Files\EqualizerAPO`.
- **Ownership takeover** (`ensure_first_run`) — on first run, the pre-existing
  `config.txt` is saved verbatim to `config.txt.rumble_backup`, then replaced with
  a managed marker + an `Include:` pointer at the active profile. Idempotent: a
  present marker is a no-op; a missing marker with an existing backup means
  *drift* (another tool or a reinstall clobbered it) and ownership is
  re-asserted, with the displaced content preserved as a timestamped
  `config.txt.rumble_drift-<ts>` snapshot (never overwrites the original backup).
- **Hand back to APO / Resume control** — `release_control()` restores the
  original `config.txt` verbatim and stops auto-reasserting; `resume_control()`
  re-runs the takeover. **[UI]** Settings → Equalizer APO.
- **Drift-snapshot restore** — list and restore any preserved drift snapshot
  verbatim. **[UI]** `drift_restore_dialog.py`.
- **Config Health viewer** (`config_health_dialog.py` / `raw_config_view.py`) —
  read-only view of the fully-`Include:`-flattened, currently-*applying* config
  text, each line tagged with its source file. **[UI]** Tools → Config Health.
- **Foreign-tool coexistence dashboard** (`foreign_tool_detection.py`,
  `coexistence_dialog.py`) — three honestly-scoped signals: foreign `Include:`
  targets/raw directives currently in `config.txt`, preserved drift-snapshot
  count, and known competing GUI processes running right now (`Peace.exe`,
  `Editor.exe`, detected via `tasklist`). **[UI]** Tools → Foreign-Tool
  Coexistence.
- **Existing-APO-config import offer** (`apo_import_dialog.py`,
  `discover_importable_configs`/`sync_apo_imports`) — on boot, offers any
  existing APO config content (or content displaced by ownership takeover) as an
  importable profile; declined sources are remembered (`ignored_apo_sources`) so
  they aren't re-offered.
- **Device-format detection** (`device_format.py`) — live WASAPI mix-format query
  (sample rate/bit depth/channel count) for the default or a named device, with a
  push-notification watcher (`DeviceFormatWatcher`, IMMNotificationClient,
  1.5 s-debounced) that keeps the UI's shown format current.
- **Device enumeration** (`audio_devices.py`) — every endpoint APO is hooked onto
  (`HKLM\...\EqualizerAPO\Child APOs`), cross-referenced against MMDevices for
  friendly names; APO-style pattern matching (`;`-alternatives, word-wise
  substring, `all` keyword) used everywhere a `Device:` line is written or
  matched. Also detects the "Audio enhancements" OS toggle being off per-endpoint
  (`enhancements_disabled`) — which silently makes the whole APO chain, and so
  RUMBLE, do nothing — and flags a default device that isn't APO-hooked at all
  (`default_device_hooked`, the apo-coverage banner).
- **Sample-rate handling** — global processing sample rate, "auto" (detects the
  live default device's WASAPI rate) or "manual". A curve-fitted profile's
  parametric bands + residual GraphicEQ are automatically re-solved
  (`refresh_fits_for_sample_rate`) when the effective rate changes, because a
  biquad's response warps with sample rate while a GraphicEQ line does not.
- **Per-device profile dispatch (D1a)** — different profiles can be assigned to
  different playback/capture endpoints (`device_profile_assignments`, keyed by
  endpoint GUID); render writes one composed scratch file per assignment and a
  branch structure where assignment blocks render *before* the `Device: all`
  fallback (APO claims a device by the first matching block).

---

## 4. Filter math & the live response graph

**[UI]** Editor panel's frequency-response graph (`eq_graph.py`, pyqtgraph).
**[Math]** `eq_math.py` — every RBJ Audio EQ Cookbook biquad form implemented
directly: peaking (PK/PEQ/Modal), low/high-pass (LP/HP/LPQ/HPQ), band-pass (BP),
notch (NO), all-pass (AP), low/high shelf (LS/LSC/HS/HSC) with `A = 10^(gain/40)`,
plus fixed-Q 6 dB/oct and 12 dB/oct shelf approximations (Q = 0.5 / 0.707). Raw
custom `IIR` coefficients evaluate their transfer function directly. `combined_response()`
sums every enabled filter's dB response (matching how APO stacks biquads in
series) plus any `GraphicEQ:` layer (log-linear interpolation between its
points, flat beyond the endpoints). `Q ↔ bandwidth-in-octaves` conversion has
both a standard unwarped form and a Nyquist-exact warped form
(`q_to_bw_exact`/`bw_to_q_exact`, the full RBJ `w0/sin(w0)` prewarp term).

- **32-band ceiling** (`MAX_BANDS`) — a deliberate RUMBLE-side UI/performance
  cap; APO itself has no filter-count limit.
- **GraphicEQ ↔ Parametric conversion** (`geq_peq_convert.py`):
  - `convert_geq_to_peq` — one PK band per significant curve point, bandwidth
    from the point's owned log-frequency span to its neighbours, then a bounded
    Newton-Raphson pass (2-3 iterations) corrects for adjacent-band skirt
    overlap so each band's own centre hits its target gain exactly.
  - `fit_peq` — a fundamentally different, better approach: bands are *placed*
    greedily where the curve is worst-approximated (Q read off that feature's
    own -3 dB width) and every band's Fc/gain/Q is jointly re-solved by
    Levenberg-Marquardt (with a ridge penalty preventing degenerate
    huge-opposing-band solutions) until the fit is within 0.1 dB (APO's own
    write precision) or further bands can't improve on the file's own rounding
    noise.
  - `decompose_geq` — splits an imported curve into fitted PK bands (editable in
    the UI) plus a residual GraphicEQ layer (2-decimal precision, adaptively
    refined grid) that exactly restores whatever the parametric fit still
    misses; this is what every curve-derived profile (AutoEQ, Spinorama, REW,
    room correction) actually stores, not a bare GraphicEQ dump.
- **Undo/redo** (`undo_history.py`) — full snapshot-based history over the
  editor's live `ProfileConfig` state.
- **Solo / A-B compare** — per-channel solo and an A/B toggle inside the editor
  for comparing two edit states live.

---

## 5. Auto-preamp & headroom (`preamp_calc.py`)

**[Math]** Computes the peak (dB) of the fully composed response (parametric
filters + GraphicEQ + optional convolution-IR curve + any per-channel trim
offset) across a 512-point 10 Hz–24 kHz log grid, then returns
`-(peak + margin)`, quantised down (floored) to APO's 0.1 dB precision so the
loudest point never exceeds 0 dBFS. Margin is 0.5 dB normally, escalating to
1.0 dB when the combined boost exceeds 6 dB *or* three-or-more bands each boost
≥4 dB simultaneously (empirically, inter-sample-peak risk tracks boost *count*,
not Q). **[UI]** Auto-Preamp toggle in the editor (session control — never marks
the profile dirty). `calculate_headroom()` is the inverse: given a chosen
preamp, reports the remaining (or exceeded) headroom in dB.

---

## 6. Measurement import & correction pipeline

**[UI]** Tools → Browse Measurement Database (`autoeq_dialog.py`) — one unified,
federated search across every enabled provider (headphones + speakers), with
device-type as a facet rather than a mode switch.

- **Providers** (`measurements/registry.py`, `base.py`, `federated.py`):
  - **AutoEQ** (`autoeq.py`, `autoeq_provider.py`) — ~4,000 measured
    headphones/IEMs from `github.com/jaakkopasanen/AutoEq`'s published results;
    fetches its `INDEX.md` (7-day local cache), then a device's own
    ParametricEQ.txt (already valid APO syntax, parsed with zero translation),
    GraphicEQ.txt, FixedBandEQ.txt, minimum-phase convolution IR (44.1/48 kHz),
    or CSV raw/target columns.
  - **Spinorama** (`spinorama.py`, `spinorama_provider.py`) — anechoic
    (Klippel NFS) speaker measurements aggregated from ASR/Erin's Audio Corner/
    others at spinorama.org, fetched as a sharded JSON index plus per-speaker
    CEA-2034 Plotly-JSON curve files (On Axis / Listening Window / Early
    Reflections / Sound Power / Estimated In-Room Response). Both providers are
    enabled by default, live-fetch over HTTP, no opt-in gate.
  - Both live behind one `MeasurementProvider` interface so the browse UI never
    branches on source.
- **AutoEQ merge / gear simulation** (`autoeq_merge.py`) — average several
  measurements of the same device (log-linear interpolation onto the union
  frequency grid, optionally quality-weighted), or build a *gear-simulation*
  delta curve (`target − source`) so one headphone's EQ can mimic another's
  target signature. **[UI]** AutoEQ browser's Browse & Merge / Gear Simulation
  tabs.
- **Speaker correction** (`eq_target.py`) — `correction(f) = clamp(target(f) −
  smoothed_measured(f))`: fractional-octave smoothing (flat ⅓-oct, or AutoEQ's
  own frequency-dependent adaptive scheme — narrow below 6 kHz, 2-octave-wide
  above 8 kHz, since rig/room noise dominates real device differences up there),
  boost-capped (default 6 dB) and cut-floored (default 12 dB) with a soft
  band-limit taper at the edges. Target is either a straight log-frequency tilt
  (`slope_db_per_oct`, default settable 0…-0.9) or a named published curve.
  **Directivity-aware damping**: a speaker's published horizontal/vertical
  directivity estimate can shrink the boost cap (`directivity_boost_cap`,
  calibrated against real ASR/EAC data — good vs. poor directivity separated
  cleanly by `min(h_avg, v_avg)` at 40°/15° thresholds), and a per-frequency
  Listening-Window-vs-Sound-Power local-trend deviation can suppress correction
  at frequencies showing a directivity artifact rather than a genuine on-axis
  error (`directivity_gap_suppression`).
- **Named target curves** (`target_curves.py`) — real, cited AutoEQ-bundled
  target datasets: Harman over-ear 2018, Harman in-ear 2019, Diffuse-field
  (5128 rig); defaulted by device type.
- **REW import** (`rew_import.py`) — parses REW's "Export Measurement as text"
  (frequency-response curve) and "Export filter settings as text" (reuses the
  APO config parser directly, since REW's own filter-line syntax is
  RBJ-biquad-compatible); a user's own REW measurement is weighted 2× AutoEQ/
  Spinorama's own "high" quality tier when blended with community data
  (`blend_with_community`). **[UI]** File → Import from REW; offers replace-vs-
  blend via `rew_import_dialog.py`.
- **Peace import** (`peace_import.py`) — parses Peace's own INI save format
  (not raw APO text) directly into `ProfileConfig`/`ChannelSection`/`Filter`
  objects; reports any band using a Peace-internal multi-stage filter design
  (Butterworth/Linkwitz-Riley cascades, adjustable-Q shelf variants) that has no
  single APO filter type, rather than silently dropping it. **[UI]** File →
  Import from Peace.
- **Room/impulse-response measurement** (`measure_dialog.py`, `measurement.py`,
  `ir_analysis.py`) — a room-measurement wizard, plus WAV impulse-response
  analysis (RIFF chunk walker supporting PCM 16/24/32-bit and float 32/64,
  WAVE_FORMAT_EXTENSIBLE): FFT magnitude in dB, fractional-octave smoothed,
  sampled onto the graph grid — feeds both the response graph and the
  anti-clipping preamp calculation. `check_rate_mismatch()` warns when a
  convolution IR's own embedded sample rate differs from the device's (APO does
  not resample IRs at load time).
- **Microphone calibration** (`mic_cal.py`) — parses miniDSP/UMIK calibration
  text files; a two-tier trust model (an uncalibrated mic is trusted only below
  ~300 Hz, the Schroeder-transition ceiling for domestic rooms; a real
  file/transfer calibration is trusted full-range); **transfer calibration**
  derives an unknown mic's own error curve from a side-by-side measurement
  against an already-calibrated reference mic; **Spinorama-referenced
  estimation** derives a rougher calibration by comparing a gated (quasi-
  anechoic) measurement of a speaker the user owns against that speaker's
  published anechoic curve.

---

## 7. Ear Calibration (hearing test suite)

**[UI]** Settings → Ear Calibration → Run Ear Calibration… (`hearing_dialog.py`).
Up to 4 nameable **Listener Slots**, switchable from the header/tray dropdown
(session-only — not sticky across restarts).

- **Test A — per-ear frequency staircase** (`hearing_test.py`) — one progressive
  test, no separate Quick/Full modes: an adaptive weighted up/down staircase
  (`Staircase`, step_down=8 dB/step_up=4 dB, 5 reversals) measures each ear's
  threshold at 11 frequencies (125 Hz–16 kHz), interleaved left/right per
  frequency to avoid fatigue bias. A 5-point baseline subset (250/1000/4000/
  8000/16000 Hz) is tested first and is independently "usable"; the remaining
  6 points add detail and seed their staircase from log-frequency interpolation
  between already-measured neighbours. A point the listener can't hear twice in
  a row at the loudest presentable level (-10 dBFS) records a "gave up" lower
  bound rather than a guessed threshold, and — beyond the frequency where an
  ear first gave up in a given direction — later points become fast two-click
  "ceiling probes" instead of being silently auto-skipped (so a genuine
  hardware notch doesn't masquerade as total inaudibility past it).
  `normalise()` expresses each ear's curve relative to its own 1 kHz reading
  (for display) and computes L−R imbalance from the *raw* thresholds (so a
  real constant offset isn't hidden by per-ear normalisation).
  A **persistent per-point dashboard** (`calibration_history_dialog.py`,
  `listener_slots.py`) lets any point be re-tested any time, in any order,
  across sessions — corrections read the *current working state*, not a
  run-average, though up to 10 full saved runs are still retained per slot for
  history/export.
- **Volume reference step** — before testing, the listener adjusts their real
  (OS + any external amp) volume against a bracketed spoken reference clip
  (quieter → target → louder, so "too quiet"/"too loud" are both audible
  either side of the target) until the middle segment feels right — described
  against real-world comparisons (a hair dryer, a whistling kettle, a school
  bell, a pedestrian crossing signal, a whistle blast) targeting roughly
  75 dB SPL (`loudness_layer.REFERENCE_CLIP_TARGET_PHON`), raised from an
  earlier 60 dB "normal conversation" default to give the staircase's -10 dBFS
  ceiling enough real-world headroom to reach genuine elevated thresholds at
  12-16 kHz rather than false-"gave up" there, while staying 15 dB under
  NIOSH/OSHA's 85 dB/8hr damage-risk threshold. Each of the five comparison
  sounds is also playable standalone, independently level-matched to the same
  target, both from this page and from Settings → Ear Calibration (so a
  listener can re-check their volume any time without opening the test). An
  optional "measured level (dB SPL)" field lets a listener with a real SPL
  meter reading override the assumed 75 dB default for a more accurate
  anchor. The captured Windows-volume reading anchors both a same-hardware
  cross-session consistency check and (optionally) the ISO 226 loudness
  layer's reference point.
- **Test B — digit-triplet noise screening** (`digit_screening.py`) — an
  informal, clearly-disclaimed *screening estimate* (not diagnostic): random
  3-digit triplets (no adjacent-digit repeats) mixed into calibrated pink noise
  at a symmetric-step staircase-controlled SNR (8 reversals) converges on an
  SRT (speech-reception threshold, the 50%-correct SNR). Classified "typical"
  vs. "consider assessment" against a cited literature threshold
  (-5.55 dB SNR, Smits & Houtgast). **Note:** permanently abandoned as a
  product decision (2026-07-09) and its entire UI (`hearing_dialog.py`'s
  consent/test/results pages, the picker's "Noise Screening" button) was
  removed 2026-07-16 — there is no way to reach it from the app. The
  Qt-free core module is kept, still fully covered by its own test
  (`selftest_digit_screening.py`), in case it's ever reused for something
  else, but nothing in the UI references it anymore.
- **Correction derivation** (`listener_correction.py`) — weak-ear-only,
  boost-only (never cuts the stronger ear), capped at +6 dB: positive L−R
  imbalance boosts the left ear, negative boosts the right. Composed onto
  whichever channel sections address L/R at render time only — never written
  into stored profile files.
- **Export** — a plain-text report (per-ear tables, imbalance table, volume
  reference, an explicit no-response section) written with an embedded
  instruction paragraph aimed at an AI reading the file to author a
  compensation profile by hand.

---

## 8. ISO 226 volume-linked loudness compensation

**[UI]** Settings → Enhancements → Loudness compensation (ISO 226). Off by
default (needs calibration first).
**[Math]** `iso226.py` implements the real ISO 226:2003 equal-loudness-contour
formula (29 tabulated frequencies, published `alpha_f`/`L_U`/`T_f`
coefficients, numerically cross-checked against two independent open-source
reproductions) — not upstream Equalizer APO's own `LoudnessCorrection:`, which
is a hand-tuned heuristic. `loudness_layer.py` derives a compensation curve from
the gap between a calibrated reference listening level (phon, default 80 —
one of ISO 226's own headline illustrated contours) and the *live* WASAPI
master-volume reading, on the assumption that 1 dB of volume change ≈ 1 phon
of perceived level. Calibrated either directly (capture current volume as
"my normal level") or extrapolated from the Ear Calibration volume-reference
step. Composed symmetrically across every channel, clamped to the same ±6/-12 dB
soft bounds as every other auto-generated curve, recomputed live as the volume
knob moves (`loudness_volume_adapter.py` polls WASAPI).

---

## 9. Session tone trim ("B1")

**[UI]** A bass/treble/tilt strip in the editor (`tone_trim_widget.py`) —
"it's 11pm and this track is harsh" instant global tone control.
**[Math]** `tone_trim.py`: a bass shelf below 250 Hz and a treble shelf above
4000 Hz (each ±6 dB max) plus a log-frequency tilt (±3 dB/oct max, pivoted at
1 kHz), summed and clamped to the standard ±6/-12 dB bounds. Session-scoped —
never persisted, resets to flat every launch — with a "Bake into profile…"
escape hatch that writes the current trim into the profile's real GraphicEQ.

---

## 10. Bass management & speaker crossovers

**[UI]** Tools → Bass Management (`bass_management_dialog.py`, a wizard) and
Tools → Crossover Design (`crossover_dialog.py`).
**[Math]**
- **Crossover design** (`crossover.py`) — Linkwitz-Riley alignments built by
  cascading Butterworth 2nd-order sections (the standard analog-prototype
  pole-angle formula): LR2 = one Q=0.5 section, LR4 = two cascaded Q=0.7071
  sections, LR8 = a doubled 4th-order Butterworth. An optional all-pass
  cascade at the same Fc/Q corrects group-delay mismatch against a branch that
  can't be directly edited (e.g. a powered sub's own built-in low-pass).
- **Bass management** (`bass_management.py`) — for 2.1/4.1/5.1-style layouts:
  a high-pass on every main channel, a low-pass on the sub/LFE channel, both at
  a shared crossover frequency (Linkwitz-Riley flatness requires it), a summed
  bass-redirection feed (`1/N` weight per N main channels — the standard
  never-exceeds-unity AVR convention), LR2's mandatory polarity inversion on
  the sub feed (LR2's HP+LP sum nulls in-phase at crossover; LR4 doesn't need
  it), physical-distance-to-delay conversion (343 m/s reference speed of
  sound, farthest channel = 0 ms reference, since APO's `Delay:` can only add
  delay), and a headroom-safe global preamp recomputed as the minimum
  auto-preamp across every composed section (with a +6.02 dB, `20·log10(2)`,
  headroom allowance for the summed feed's own doubling risk).
- **Speaker time alignment / mic-sweep measurement** (`time_alignment.py`) —
  plays a calibrated log sweep out of one output channel at a time while
  recording via a real microphone, measures each channel's arrival offset via
  GCC-PHAT (Generalized Cross-Correlation with Phase Transform — robust to
  room-reflection false peaks, unlike raw cross-correlation), and rebases
  delays to the latest-arriving channel (again, APO's `Delay:` can only add).
  The correlation/offset math is pure numpy and fully unit-tested; the actual
  play-and-record orchestration is a thin hardware shell not covered by the
  automated test gate (no mic in CI).

---

## 11. Headphone crossfeed

**[UI]** Settings → Enhancements → Crossfeed (headphones). Off by default.
**[Math]** `crossfeed.py` — pure APO config text (gain/filter/delay recipes),
never a licensed/bundled HRTF dataset:
- **Named circuits**: Chu Moy (-6 dB / 700 Hz low-pass, no delay), Jan Meier
  (-9.5 dB / 650 Hz, no delay — RUMBLE's default), Linkwitz-style (-4.5 dB /
  700 Hz + an explicit ~300 µs interaural-time-difference delay — hardware-
  verified live), Bauer/bs2b default (-4.5 dB / 700 Hz, level-only), and a
  plain full-band "Direct Blend". All figures cited from the original
  published circuits or bs2b's own preset table.
- **Strength (0-100%)** scales the cross-path gain in the *linear* voltage
  domain (interpolating toward unity, not scaling the dB figure directly),
  so 100% reproduces the preset exactly and 0% is silently skipped.
- **Custom mode** — user-set gain/frequency/delay, rendered at full strength
  (no separate scalar on top).
- **Soundstage mode** — a distinct algorithm: mid-side decomposition
  (M=(L+R)/2, S=(L−R)/2) with a low-shelf *cut* on the side channel only,
  ported directly from FFmpeg's `af_crossfeed` filter math (RBJ low-shelf
  coefficients, `gain = -30·strength`, `Fc = (1-range)·2100 Hz`).
- Applies only to profiles whose channel layout actually carries both L and R
  (`can_crossfeed`); rendered as raw text appended after the composed profile
  body, never stored in the profile file itself.

---

## 12. Profile-switch loudness matching ("A2")

**[UI]** Settings → Enhancements → Loudness matching. Off by default.
**[Math]** `abx.py`'s `estimated_gain_db()` (see §14) estimates each rendered
profile's broadband power-domain gain; when enabled, every composed render's
estimated gain is pulled down (never boosted) to match a flat/EQ-off anchor
(0 dB), so switching profiles — or toggling bypass — never produces a level
jump that could masquerade as a quality difference.

---

## 13. Render-layer composition order

**[Entities/Math]** `profile_manager._apply_render_layers()` — the single choke
point every render (canonical activation, live preview, ABX playback) passes
through, always in this order: **1. Listener (ear) correction** → **2. Session
tone trim** → **3. ISO 226 loudness** → **4. Profile loudness matching**
(always last: a flat preamp delta on top of whatever curve the earlier layers
produced, so it shifts level without touching shape). **5. Crossfeed** is
appended as raw text after everything else, outside the composed
`ProfileConfig` entirely. None of layers 1-4 ever mutate a stored profile file
— they're computed fresh into a scratch Include target on every render.

---

## 14. Blind A/B/X listening test

**[UI]** `abx_dialog.py`, reachable from the profile library (compares
candidate profiles/configs).
**[Math]** `abx.py`:
- **Level matching** (`level_match`) — before any blind trial, every candidate
  is gain-matched to the quietest one (reduction only, never boosts) using a
  power-domain (linear, not dB) average of the combined response across the
  20 Hz–20 kHz log grid — equivalent to the RMS change pink noise would show,
  the physically correct proxy for real program material's perceived loudness.
- **Trial engine** (`ABXSession`) — a cryptographically random (or
  test-injectable deterministic) hidden reference X per trial; open-ended, no
  fixed trial count.
- **Statistics** (`binomial_p`/`verdict`) — exact one-sided binomial p-value
  computed in log-space (avoids `OverflowError` on unbounded trial counts),
  judged at the conventional p<0.01 ("reliably hear a difference") / p<0.05
  ("probably") / else ("consistent with guessing") thresholds.
- **Playback safety** (`ABXRunner`) — previews candidates through the same
  preview mechanism live-tuning uses, guaranteed to restore the listener's true
  active profile on every exit path, including an exception mid-trial.

---

## 15. Live spectrum analyzer & level meters

**[UI]** Analyser toggle beside the response graph (`spectrum_overlay.py`,
`eq_graph.py`). **Off by default** — the WASAPI loopback capture backing it
once destabilised COM on at least one machine under a live Qt event loop; the
underlying crash (`RPC_E_CANTCALLOUT_ININPUTSYNCCALL`) was fixed by moving the
entire PortAudio/WASAPI lifecycle onto its own COM multi-threaded-apartment
worker thread, but the feature stays opt-in.
- **Capture** (`audio/capture.py`) — WASAPI loopback via `pyaudiowpatch`, with
  auto-reconnect on device death.
- **Shaping** (`spectrum.py`) — interpolates onto a fixed log grid, applies
  pink-noise tilt compensation (+3.0097 dB/octave — the exact physically
  correct compensation for a 1/f-PSD signal read on a 20·log10 magnitude
  scale, not a tuned number), then per-frequency asymmetric exponential
  smoothing using real IEC 60268-10 "Fast"/"Slow" RTA ballistics time
  constants (125 ms / 1000 ms), plus a decaying peak-hold trace.
- **Modes** — Output (post-EQ, measured), Source/estimated (post-EQ minus the
  calculated EQ response, i.e. the estimated pre-EQ signal — `subtract_response`),
  or both overlaid.
- **Drift detection ("Rumble 3.7")** — Pearson correlation between the measured
  spectrum and the calculated EQ curve's shape, judged for statistical
  significance via Fisher's r-to-z transform at a strict 99% confidence bar,
  pooled across many ticks (not one frame): catches a competing driver-level
  DSP, a muted/bypassed EQ, or the wrong output device — cases that don't
  necessarily clip and so evade the separate 0 dBFS clipping detector.
- **VU/Peak/Reference level meter panel** (`vu_meter.py`, `level_meter.py`) —
  a fixed-colour (theme-independent) segmented LED bargraph beside the graph,
  showing both measured output and estimated input L/R. Three ballistics
  modes, each derived from a named standard: **VU** (ANSI C16.5-1942/IEC
  60268-17, symmetric 300 ms-to-99% integration), **Peak** (IEC 60268-10,
  ~instant attack + 20 dB/s linear decay with a decaying hold cap), and
  **Reference** (Bob Katz's K-System, K-12/K-14/K-20 headroom-marked
  variants of the same VU-style ballistics).

---

## 16. Automatic profile switching

**[UI]** Settings → Auto-Switch (`auto_switch_dialog.py`); off by default.
**[Math/Entities]** `auto_switch.py`:
- **Rules** — ordered list, first match wins: trigger by foreground **app**
  (a currently-playing audio session's exe name, debounced 2 s to trigger and
  10 s lingering after it stops to avoid flapping on pauses), by **device**
  (APO-style pattern against the live default output), or by **time-of-day**
  window (may cross midnight).
- **`SessionWatcher`** polls pycaw's audio-session list on its own thread
  (1 Hz) for actively-playing process names; `DeviceFormatWatcher` (§3) covers
  the device trigger via push notification.
- **`AutoSwitchController`** drives `ProfileManager.activate_profile()`,
  tracking a "baseline" (whatever was manually active before a rule first
  took hold) to restore once no rule holds; suspended entirely while the
  editor has unsaved changes.
- **Native APO conditional export** — an alternative, complementary path:
  device-trigger rules can be translated into a self-contained APO
  `If`/`ElseIf`/`Else`/`EndIf` block (using APO's own `deviceName`/
  `connectionName` runtime variables) that works with *no RUMBLE process
  running at all*. App/time rules can't be expressed this way (APO has no
  "which process is playing" or wall-clock variable) and are reported back as
  untranslatable rather than silently dropped.

---

## 17. Theming

**[UI]** Settings → Appearance / Theme Creator; Theme menu.
- **7 built-in themes** (Soundwave — default — Panther, Malice, Prime,
  Screamer, Bludgeon, Sage) plus a live **System** theme that tracks Windows'
  light/dark setting.
- **Custom themes** — a full token editor (backgrounds, borders, accents,
  text, status colours, graph colours, scrollbar), validated (every required
  hex-colour token present and well-formed), stored per-user, importable/
  exportable as portable `.rumble-theme` JSON files.
- **Visual system** (`theme_engine.py`, `hud_frame.py`, `effects.py`) —
  token-driven QSS (translucent glass panels, rim-light, backdrop gradients),
  painted accent corner-tick framing (`HudFrame`), drop-shadow glow/elevation
  effects — deliberately kept off the graph/scroll/panel surfaces for
  legibility. Per-theme levers (`radius_scale`, `sheen`, `bevel`, `glow`,
  `glass_alpha`) are optional and layout-safe.

---

## 18. Window, density & accessibility

- **Frameless window chrome** (`frameless.py`) — custom drag region + minimize/
  close, no native title bar; a standardised pop-out dialog ceiling
  (`STANDARD_DIALOG_WIDTH`/`HEIGHT`, `scrollable_page()` for anything that
  would overflow it) so no dialog ever grows past Settings' own size, and no
  dialog carries its own Close button (chrome X only).
  **Fixed-size window**: 1000×580 compact, or 2000×1160 at the "Double" (2×)
  interface-size setting — live-switchable, rebuilt in-process.
- **Marquee scrolling** (`marquee_label.py`, `editable_marquee.py`) —
  overflowing labels (profile names, headers) always slow-scroll (15 px/s)
  rather than only on hover.
- **Accessibility** (`a11y.py`) — every interactive control carries a
  screen-reader-style `describe()` annotation (name + help text). A prior TTS
  "talking" reader and a global focus-outline stylesheet were built and then
  removed by design decision; only keyboard operability and `describe()`
  tooltips remain.

---

## 19. AI-assisted profile authoring (no built-in AI)

**[UI]** Tools → AI Profiles, and the About dialog's "AI Profiles" tab
(`ai_profiles_dialog.py`). **RUMBLE contains no AI/LLM of its own and makes no
network call to any AI provider.** This feature is a pair of carefully written,
versioned copy-paste prompt templates the user pastes into an AI assistant of
their choice:
- **Universal prompt** — for any capable web-connected assistant (ChatGPT,
  Gemini, Copilot, including free tiers): device-accurate correction,
  online gear research, measurement fitting, speaker time-alignment guidance,
  the optional tone-control layer, and a 10-band export.
- **Minimal prompt** — for small/offline/local models (Llama, Mistral, Qwen,
  Gemma, Phi, DeepSeek) or any assistant without web access: a
  preference-only interview producing a sensible general-purpose profile, no
  internet or heavy maths required.
- The AI's output is a plain profile the user pastes into a new profile or
  imports as a `.txt` file — the same import path as any other APO config.
- The About/AI-Profiles page also documents extra APO syntax an AI (or a
  human) can hand-author beyond the core interview — `LoudnessCorrection:`
  and Ear-Calibration-derived compensation — and exactly where in the editor
  each becomes editable afterward.

---

## 20. Settings surface (11 pages, 4 groups)

`settings_dialog.py` — every control applies itself live (250 ms debounce),
no Save button:
- **Listening**: Listeners (the 4 slots, naming, default slot), Ear Calibration
  (launch, correction on/off), Enhancements (ISO 226 loudness, crossfeed,
  profile loudness matching).
- **Profiles**: Library (built-in/microphone-profile visibility, unsaved-
  changes warning), Corrections (measurement-import defaults: target tilt,
  smoothing, directivity limiting, adaptive smoothing, subsonic crossover).
- **Playback**: Devices (per-device profile assignment), Auto-Switch (rules),
  Equalizer APO (ownership hand-back/resume, config path override, sample
  rate mode).
- **Application**: Appearance (theme, interface size, graph height, analyzer
  defaults), Theme Creator, Startup (autostart, start minimised, safety
  warning), Backup & Restore.
- **Selective settings reset** (`settings_reset.py`) — 8 user-choosable
  groups (Appearance / EQ defaults / Sound layers / Startup & warnings / APO
  integration / Auto-switch rules / Per-device assignments / Volume
  calibration) rather than one all-or-nothing button; a short explicit list of
  keys (APO ownership state, active profile, live bypass state, the one-shot
  legacy-migration gate) is permanently protected from any reset because
  they're state/actions, not preferences.

---

## 21. Advanced per-profile editing surface

`advanced_panel.py`, four tabs, full raw-APO power-user access beyond the
parametric band list:
- **Graphic EQ** — a freeform draggable tone curve per channel, on top of the
  parametric bands.
- **Processing** — Processing Stage (default / pre-mix / capture-only),
  Convolution (impulse-response file selection, with the rate-mismatch
  warning from §6), VST plugin chaining (path + optional serialized state
  "Chunks"), and a `LoudnessCorrection:` editor (upstream APO's own hand-tuned
  loudness heuristic, distinct from RUMBLE's ISO 226 layer).
- **Routing** — raw `Copy:` expression editing (channel mixing/redirection).
- **Scripting** — `If`/`ElseIf`/`Else`/`EndIf` conditional block editing and
  `Eval:` expression definitions, both round-tripped verbatim from/to real APO
  syntax.

---

## 22. Safety & correctness invariants worth knowing

- Every auto-generated correction curve (listener correction, ISO 226,
  session trim, speaker corrections) is soft-clamped to the same
  research-grounded bounds (+6 dB boost / −12 dB cut by default) via one
  shared `eq_target.clamp_gain()`, and every clamp reports what it actually
  changed (`ClampSummary`) so a silently-truncated correction is disclosed,
  not hidden.
- The global preamp is *always* recomputed as the minimum (most negative,
  safest) auto-preamp across every render layer's composed sections — no
  layer can silently push the signal over 0 dBFS.
- APO's `Delay:` can only ever *add* delay, never subtract it — every
  delay-producing feature (bass management, time alignment) rebases to the
  slowest/farthest channel as its zero reference, never the fastest.
- Curve math never invents frequency points: every merge/sum/delta operation
  reconciles onto the sorted union of the real input points, log-linear
  interpolated, exactly matching how APO's own `GraphicEQ:` format
  interpolates at runtime.
