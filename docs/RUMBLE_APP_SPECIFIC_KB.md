# RUMBLE Application-Specific Knowledge Base

This document answers, with real code citations, every question needed to merge RUMBLE's
actual implementation into a generic Equalizer-APO / PEQ / GEQ knowledge-base schema. It was
compiled 2026-07-08 by direct codebase research (four parallel research passes over the live
`H:\RUMBLE` tree). All `file:line` references are against the current `main` branch working tree.

Where the app does **not** implement something the generic schema might expect, this is stated
explicitly as **"Not implemented"** rather than omitted — several sections found and corrected
stale assumptions from prior project notes (see §13).

---

## §0 — Entity-Relationship Map

This section did not appear in the original questionnaire but is added to satisfy the requirement
that every entity relationship be explicitly represented, not just implied inside prose answers.

### 0.1 Core profile object graph

```
ProfileMeta (index/list row, in-memory + reconciled to file)
  id, name, description, tags[], file, created, modified,
  active: bool, readonly: bool, builtin: bool,
  apo_source, apo_source_hash, import_signature
  .protected  →  computed = readonly OR builtin
        │
        │  1:1, reconciled by ProfileManager._doc_for_meta() on every write
        ▼
ProfileDocument (parsed .txt file — the actual persisted artifact)
  name, description, tags[], created, modified, readonly, builtin,
  apo_source, apo_source_hash, import_signature,
  found_keys: set        ← tracks which header lines were present vs absent
  config: ProfileConfig  ← 1:1, the EQ payload
        │
        ▼
ProfileConfig
  device: str = "all"                  ← FK-ish reference to ApoDevice.pattern (unenforced string match)
  preamp: float                        ← GLOBAL preamp; only field the editor UI ever writes
  stage: str | None                    ← APO Stage: (Default/Pre-mix/Post-mix/Capture/All/Custom)
  copy_lines: list[str]                ← APO Copy: routing expressions
  conditional_blocks: list[str]        ← APO If/ElseIf/Else/EndIf, raw text, round-tripped verbatim
  extra_lines: list[str]               ← anything unrecognized, preserved for lossless round-trip
  channel_sections: list[ChannelSection]   ← 1:N, ordered, one per "Channel: X" block
        │
        ▼
ChannelSection (one per Channel: block, e.g. "L", "R", "L R", "all")
  channel: str
  filters: list[Filter]                ← 1:N, ordered, rendered as "Filter N: ..." lines
  graphic_eq: str | None               ← raw "freq gain; freq gain; ..." GraphicEQ: payload
  delay_ms / delay_samples             ← per-channel time alignment
  preamp_db: float | None              ← per-SECTION preamp; round-trips but editor never authors it
        │
        ▼
Filter (one PEQ band)
  enabled: bool, type: str ("PK","LSC","HSC","LP","HP","BP","NO","AP","IIR",...)
  fc: float, gain: float, q: float
  use_bw: bool, bw_oct: float          ← Q and bandwidth stored SIMULTANEOUSLY; use_bw picks which is authoritative on render
  iir_order: int, iir_coefficients: str  ← opaque passthrough, NOT sample-rate-aware (gap, see §13)
```

**Channel linking is a derived UI relationship, not a stored one.** A stereo pair is either one
combined `ChannelSection(channel="L R")` or two independent `ChannelSection("L")` /
`ChannelSection("R")`. Which state a loaded profile is "in" is computed on load by
`_sections_identical()` — there is no `linked: bool` field anywhere in the persisted schema.

### 0.2 Response-math relationship (the single most important cross-cutting relationship)

```
Filter[] (per ChannelSection)
        │  eq_math.combined_response(filters, freqs, sample_rate)
        ▼
   biquad cascade dB response (RBJ Audio EQ Cookbook formulas)
        │
        │  + eq_math.graphic_eq_response(graphic_eq_string, freqs)   [if GraphicEQ present]
        │  + ir_response (from ir_analysis.py, if a Convolution: IR is loaded)
        │  + per-channel trim
        ▼
   total combined response curve (one shared function, ONE engine, no duplication)
        │
        ├──► EQGraph (rumble/ui/eq_graph.py) — same-frame live display
        │
        └──► preamp_calc._peak_response_db() — np.max() of the curve
                    │
                    │  + _margin_db(peak, filters)   [escalating 0.5 → 1.0 dB]
                    ▼
             calculate_auto_preamp() = -(peak + margin)
                    │
                    ▼
             ProfileConfig.preamp   (only if "Auto" toggle is ON; otherwise user value is untouched)
```

**A second, independent, inconsistent preamp formula exists** for profiles created via the
measurement-import / merge / gear-simulation dialogs: `autoeq_merge.compute_preamp()` = always
`-(peak + 0.5)`, never escalating. This is a confirmed inconsistency — see §5.2 and §13.

### 0.3 Device / sample-rate relationship

```
Windows OS (WASAPI via pycaw+comtypes)  ──actual device format──►  DeviceFormat(sample_rate, channels, bitdepth)
                                                                          │
   winreg (MMDevices + APO's "Child APOs" key)  ──endpoint list──►  ApoDevice.pattern ("deviceName connectionName")
                                                                          │
DeviceFormatWatcher (COM IMMNotificationClient, 1.5s debounce) ─push──►  ProfileManager.set_detected_sample_rate()
                                                                          │
                                                     ProfileManager.effective_sample_rate() (auto or manual override)
                                                                          │
                                                          EditorPanel.set_sample_rate()
                                                                          │
                              ┌───────────────────────────────────────────┴───────────────────────────────┐
                              ▼                                                                             ▼
              delay_samples → delay_ms display re-derive                         headroom/graph/IR-mismatch recompute
                                                                                  (NOT a Fc-vs-Nyquist re-validation — gap)
```

`ApoDevice.pattern` is also the match target for `auto_switch.Rule(trigger="device")`, and is what
`ProfileConfig.device` is set to when a profile is scoped to one endpoint instead of `"all"`.

### 0.4 Activation / file-system relationship (how a "profile" becomes live audio)

```
config.txt (real Equalizer APO file, owned once by RUMBLE)
   Include: rumble_active.txt
                │
                ▼
rumble_active.txt (rewritten on every activate — the ONLY file rewritten per activation)
   Include: <config_path>/rumble/user/<profile-id>.txt      (or a bypass line if EQ disabled)
                │
                ▼
<profile-id>.txt  =  render_profile_text(ProfileDocument)  =  RUMBLE header comments + render_config_body(ProfileConfig)
```

Activating a profile is **two levels of `Include:` indirection**, not a rewrite of the big config
file or the profile file itself. This is why activation is cheap and why external tools that also
write to `config.txt` are detected as "drift" rather than silently overwritten.

### 0.5 Measurement-provider relationship

```
FederatedCatalog  (unifies search across all registered providers)
   ├── AutoEqProvider   (github raw-content fetch, 7-day disk cache, enabled_by_default=True, commercial_safe=True)
   │        AutoEqEntry{name, source, rig, path, device_type}
   │              │  fetch_parametric() → ParametricEQ.txt (already valid APO Filter: syntax, ingested as-is)
   │              │  fetch_graphic()    → GraphicEQ.txt     (127-pt curve, parsed by autoeq_merge)
   │              │  fetch_fixed()      → FixedBandEQ.txt   (device-native fixed PEQ)
   │              │  fetch_ir()         → minimum-phase .wav → ir_analysis.py
   │              └  fetch_csv()        → raw/target columns → tilt re-targeting via eq_target.tilt_target()
   │
   └── SpinoramaProvider (spinorama.org fetch, 7-day index cache + per-curve cache, enabled_by_default=True, commercial_safe=False)
            SpinoramaEntry{name, brand, model, source, measurement_key, quality, review, shape,
                            speaker_type, ref_6db, ref_3db, directivity_h_avg, directivity_v_avg}
                  │  CEA2034.json "Listening Window" trace  →  eq_target.make_correction()
                  │       (smooth → tilt-target-subtract → boost-cap → cut-floor → edge-taper →
                  │        directivity-aware boost damping)
                  └──►  GraphicEQ: string  →  written directly into a ChannelSection
```

No REW, Squiglink, or Crinacle *federated-catalog provider* exists in code (§6.5, §13 — corrects a
stale project memory). Separately, as of Rumble 3.8 (Session 15), a user's own REW export can be
**imported directly** (not via `FederatedCatalog`) through `rumble/core/rew_import.py` and blended
with or substituted for community data — see §6.1. This is a distinct mechanism from "REW as a
`FederatedCatalog` provider," which still does not exist and was never the shape 3.8 asked for.

### 0.6 Hearing-test / calibration relationship (deliberately NOT connected to EQ generation)

```
Staircase (per ear, per frequency, adaptive up/down)
        │
        ▼
FrequencyTest results = {"left":{freq:dB}, "right":{freq:dB}, "imbalance":{freq:leftRaw-rightRaw}}
        │
        ├──► save_results() → data_dir/hearing/frequency/*.json  (for later overlay comparison only)
        │
        └──► export_text() → plain .txt file with an "Instruction for an AI" block
                                    │
                                    ▼
                    (human/external-AI manually authors a profile — NO CODE PATH connects
                     hearing-test output to ProfileConfig automatically)
```

**Superseded 2026-07-09:** the design that closes this loop — live per-listener correction applied
on top of a loaded profile without modifying it — shipped as "Listener Slots"
(`docs/superpowers/specs/2026-07-09-listener-correction-design.md`, §15). The diagram above still
describes `save_results()`/`export_text()` accurately; what changed is that a slot's saved runs are
now *also* read at render time to derive a weak-ear-only correction (never touching `ProfileConfig`
storage) — see §15.3 for the exact boundary. The earlier
`docs/superpowers/specs/2026-07-06-multi-listener-hearing-correction-design.md` remains PARKED and
unimplemented; it was superseded by the 2026-07-09 design, not resumed.

### 0.7 Auto-switch relationship

```
SessionWatcher (pycaw poll, 1s, daemon thread)  ──"which app/device is active now"──►
        │
        ▼
RuleEngine.evaluate(rules ordered by priority = list order, first match wins)
   Rule{trigger: "app"|"device"|"time", debounce: trigger_secs=2.0 / linger_secs=10.0}
        │
        ▼
AutoSwitchController  ──ProfileManager.activate_profile()──►  rewrites rumble_active.txt (§0.4)
        │
        └── remembers `baseline` = whatever was manually active before any rule fired,
            restores it once no rule holds; suspended while editor is dirty.
```

This is a **RUMBLE-layer** mechanism (rewrites the Include pointer) — it does NOT use Equalizer
APO's own native `If`/`ElseIf` conditional syntax, even though that syntax is fully supported
elsewhere in the app (§11.2). Generating native conditional blocks for this purpose is an
explicitly open backlog item, not built.

---

## §1 — Technical foundation

**1.1 Language / framework.** Python ≥3.10 (`pyproject.toml:8-24`), Qt binding is **PySide6**
(not PyQt5/6) — `dependencies = ["PySide6", "pyqtgraph", "numpy", "pywin32; sys_platform=='win32'"]`.
`requirements.txt` adds `pycaw`, `comtypes`, `sounddevice>=0.5`, `PyAudioWPatch`, `PyInstaller`.
Windows-only desktop app: `classifiers` include `Environment :: Win32 (MS Windows)`. Confirmed
tray-resident (not one-shot): `rumble/__main__.py:251-254` sets
`app.setQuitOnLastWindowClosed(False)`; `MainWindow.closeEvent` (`main_window.py:1448-1454`)
does `event.ignore(); self.hide()` instead of quitting — closing the window minimizes to tray.
GUI (no console) entry point registered: `rumble = "rumble.__main__:main"` (`pyproject.toml:20-21`).

**1.2 Config writing / path detection / install check.** Structural render, not string-template
substitution. `rumble/core/apo_config.py` parses APO text into a `ProfileConfig` tree and
re-renders via `render_config_body()` (`apo_config.py:571`). RUMBLE takes ownership of the real
`config.txt` **once** (`ProfileManager.ensure_first_run`, `profile_manager.py:1136-1228`),
reducing it to a single `Include: rumble_active.txt` pointer (`_render_managed_config`,
`profile_manager.py:1122-1134`) — see §0.4. Individual profile `.txt` files are written once via
`profile_file.render_profile_text(doc)`; activating a profile only rewrites the pointer file.

Config-folder detection is registry-based (`rumble/core/apo_path.py`):
```python
APO_REGISTRY_KEY = r"SOFTWARE\EqualizerAPO"
def get_config_path() -> Path:                      # apo_path.py:33
    value = _read_registry_value(HKEY_LOCAL_MACHINE, APO_REGISTRY_KEY, "ConfigPath")
    if value and Path(value).exists(): return Path(value)
    install = get_install_path()
    if install is not None and (install/"config").exists(): return install/"config"
    return APO_DEFAULT_CONFIG   # C:\Program Files\EqualizerAPO\config
```
Users can override via `settings["config_path_override"]`. Install check:
```python
def is_apo_installed() -> bool:                      # apo_path.py:54-58
    if get_install_path() is not None: return True
    return APO_DEFAULT_CONFIG.exists()
```
Used only for a Settings status label — **does not hard-block startup**; the app only hard-fails
if the resolved `config_path` itself can't be created/written.

**1.3 Live OS device info.** Yes. Sample rate/bit-depth/channel count via **WASAPI through
pycaw + comtypes** (`rumble/core/device_format.py:65-99`, `_mix_format_for_immdevice` →
`IAudioClient.GetMixFormat()`). Active-device / endpoint enumeration is separate — raw `winreg`
reads of the MMDevices tree and APO's own `Child APOs` key (`rumble/core/audio_devices.py:59-114`).
Auto-switch polls pycaw's session list (`AudioUtilities.GetAllSessions()`) separately again.

**1.4 Continuous / tray, live-reload.** Continuous, tray-resident, two independent live-update
paths: (a) `DeviceFormatWatcher` — a COM `IMMNotificationClient` push callback, 1.5s debounce
(`device_format.py:248-379,289`); (b) `AutoSwitchAdapter` — a 1000ms `QTimer` driving a
`SessionWatcher` poll thread. **No `QFileSystemWatcher`** on `config.txt` exists — RUMBLE instead
re-asserts ownership / detects "drift" on every launch rather than watching live.

---

## §2 — Profile data model

**2.1 Top-level profile structure.** Two cooperating structures — see §0.1 for the full field
list of `ProfileMeta` (`profile_manager.py:292-313`) and `ProfileDocument`
(`profile_file.py:68-96`). There is no single unified "Profile" class; they're reconciled by
`_doc_for_meta()` (`profile_manager.py:556-570`) on every write. All fields are dataclass-default
optional, not schema-required; `ProfileDocument`'s metadata fields are deliberately `None`-able so
the parser can distinguish "header line absent" from "header line present but empty"
(`profile_file.py:72-74`).

**2.2 Band/filter structure.** `Filter` dataclass, `apo_config.py:80-101` — see §0.1. Both `q`
and `bw_oct` are always stored; `use_bw` decides which is authoritative at render time.

**2.3 Persistence format.** Flat **UTF-8 plain-text files**, one per profile, that are
*simultaneously valid Equalizer APO config syntax* — not JSON/YAML. Module docstring:
*"No translation step is required between 'RUMBLE profile' and 'APO config'; they are one
artifact"* (`profile_file.py:1-10`). Location: built-ins in `<config_path>/rumble/defaults/*.txt`,
user profiles in `<config_path>/rumble/user/*.txt`. `data/profiles.json` stores only app settings
+ the active-profile id, never profile content — folders are re-scanned on load
(`_scan_profile_folders`, `profile_manager.py:433-469`).

Real example (`_archive\profiles_removed_2026-06-20\user\hifiman_sundara.txt`):
```
# RUMBLE Profile: HIFIMAN Sundara
# Description: AutoEQ tuning · measured by crinacle · GRAS 43AG-7
# Tags: AutoEQ, Over-ear, crinacle
# Created: 2026-06-20T01:08:38
# Modified: 2026-06-20T01:08:38
# Created by RUMBLE

Preamp: -6.1 dB

Channel: all
Filter 1: ON LSC Fc 105.0 Hz Gain 6.5 dB Q 0.70
Filter 2: ON PK Fc 127.0 Hz Gain -2.0 dB Q 0.29
...
```

**2.4 Versioning / undo.** No persisted revision history or named snapshots — every save is a
full overwrite (`_write_profile_file()`, `profile_manager.py:572-582`). Undo/redo
(`rumble/ui/undo_history.py`) is a **linear, in-memory, session-only** whole-state snapshot stack,
`MAX_DEPTH = 100`, cleared and reset to baseline every time a profile is (re)loaded into the
editor (`editor_panel.py:1591-1592`). Nothing touches the filesystem; history is lost on profile
switch or app close.

**2.5 Multi-device / multi-channel scoping.** Device: **one per profile** (or the `"all"`
sentinel) — `ProfileConfig.device: str` is a single field, not a list. Channels: **yes, many per
profile** — `channel_sections: list[ChannelSection]`, each independently editable. "Channel
linking" is a real, current UI feature (`editor_panel.py`) that optionally collapses an identical
stereo L/R pair into one combined `"L R"` section as an editing convenience — see §0.1's note that
this is a derived UI state, not a stored field.

---

## §3 — PEQ (parametric) management

**3.1 Exposed filter types.** Essentially the full APO vocabulary, curated only into UI
categories — `FILTER_CATEGORIES` (`apo_config.py:33-39`):
```python
FILTER_CATEGORIES = {
    "Peak":     ["PK", "PEQ", "Modal"],
    "Pass":     ["LP", "LPQ", "HP", "HPQ", "BP"],
    "Shelf":    ["LS", "LSC", "LS 6dB", "LS 12dB", "HS", "HSC", "HS 6dB", "HS 12dB"],
    "Special":  ["NO", "AP"],
    "Advanced": ["IIR"],
}
```
No deliberate omissions found; `docs/RUMBLE_SPECIFICATION.md:220-229` documents the same list as
canonical.

**3.2 Field visibility per type.** Set-membership lookup tables, not per-type branching —
`apo_config.py:63-69`:
```python
TYPES_WITH_GAIN = {"PK","PEQ","Modal","LS","LSC","LS 6dB","LS 12dB","HS","HSC","HS 6dB","HS 12dB"}
TYPES_WITH_Q    = {"PK","PEQ","Modal","LP","LPQ","HP","HPQ","BP","LS","LSC","HS","HSC","NO","AP"}
TYPES_WITH_BW_VARIANT = {"PK","PEQ","Modal","LP","LPQ","HP","HPQ","BP","LS","LSC","HS","HSC","NO"}
```
`Filter.supports_gain()/supports_q()/supports_bw()` are thin membership checks; `IIR` disables
everything except type (`filter_row.py:386-394`).

**3.3 Q vs. Bandwidth.** Both supported, user-toggled per editor session (not fixed).
Two conversion pairs exist (`eq_math.py`, both derived from the Audio EQ Cookbook's
bandwidth-specified alpha, Rumble 2.1):
```python
# Unwarped (S-domain / low-Fc approximation) — used internally by filter_response()'s
# own bw_oct->Q step and wherever no sample rate is in scope:
def q_to_bw(q):  return (2.0/math.log(2.0)) * math.asinh(1.0/(2.0*q))
def bw_to_q(bw):  return math.sqrt(2.0**bw) / (2.0**bw - 1.0)

# Full RBJ warped relation (adds the w0/sin(w0) bilinear-transform prewarp term) —
# used by FilterRow's Q/BW display+edit, since a sample rate and Fc are in scope there:
def q_to_bw_exact(q, fc, sample_rate):  return q_to_bw(q) / _warp_factor(fc, sample_rate)
def bw_to_q_exact(bw, fc, sample_rate):
    return 1.0 / (2.0 * math.sinh(math.log(2.0)/2.0 * bw * _warp_factor(fc, sample_rate)))
# _warp_factor(fc, sr) = w0/sin(w0), w0 = 2*pi*fc/sr — 1.0 far from Nyquist, unbounded near it
```
The two pairs agree closely at low Fc (e.g. within ~0.0001 oct at 100 Hz) and diverge
significantly approaching Nyquist (e.g. ~0.9 oct apart at Fc = 0.45×sample_rate for Q=1.41) —
exactly where Session 1's Nyquist validation already draws attention. `bw_to_q_exact` floors to
a near-zero Q (0.001) rather than raising `OverflowError` if Fc is clamped to exact Nyquist,
where the warp term is unbounded.

Both `q` and `bw_oct` are stored simultaneously on `Filter`; conversion is applied **only at two
boundaries** — display refresh (`_sync_q_widget_value`, `filter_row.py:305-324`) and edit commit
(`_on_q_changed`, `filter_row.py:415-426`) — not on every unrelated field edit, so there is no
repeated-conversion drift risk for Fc/Gain edits. `filter_response()` (the live graph's actual
biquad math, `eq_math.py:190-194`) still uses the unwarped `bw_to_q()` — only the UI's Q/BW
display and edit path was upgraded to the warped form in Rumble 2.1.

**3.4 Maximum bands.** `MAX_BANDS = 32` (`apo_config.py`, re-exported from `editor_panel.py` for
its long-standing importers; it moved into the core when the curve->band decomposition in §4.3
began needing the same ceiling), enforced as a hard no-op past the cap (`_on_add_band_clicked`,
`editor_panel.py`). As of Rumble 4.12, the constant carries a
rationale comment: Equalizer APO itself imposes no technical filter-count limit, so 32 is a
deliberately arbitrary RUMBLE-side ceiling — generous headroom above realistic use (~10-20
bands) chosen to keep the per-band editor list and live-graph recompute bounded, not a value
derived from any external constraint. New profiles default to 10 bands
(`docs/RUMBLE_SPECIFICATION.md:231-232`). A second, independent `MAX_FLAT_BANDS = 32` exists for
the flat-starter generator (`profile_manager.py:317`).

**3.5 Automatic band placement.** Two distinct things exist, neither of which is "fit a
measurement into discrete PK bands automatically":
- `rumble/core/autoeq.py` is a pure I/O client for the **online AutoEQ community database** —
  downloads a *pre-computed* `ParametricEQ.txt` (already fitted upstream by AutoEQ itself, no
  RUMBLE-side fitting).
- `rumble/core/eq_target.py` computes an automatic correction curve for speakers, but the output
  is a **GraphicEQ curve** (point-for-point), not discrete auto-placed PK/Gain/Q bands.
There is no code path that infers discrete parametric-filter placements (Fc/Gain/Q) from a raw
measurement — manual, one band at a time via `FilterRow`, is the only way to author new PK bands
from scratch.

---

## §4 — GEQ (graphic) management

**4.1 Band grids.** Three named ISO grids plus fully arbitrary custom points —
`rumble/ui/advanced_panel.py:27-34`:
```python
ISO_10_BAND = [31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0]
ISO_15_BAND = [25, 40, 63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6300, 10000, 16000]
ISO_31_BAND = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
               1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000]
```
`ISO_10_BAND` (Rumble 4.11, Session 17) is the standard ISO 266 preferred full-octave series,
added alongside the existing 15/31-band scaffold buttons in `advanced_panel.py`. (The unrelated
"10-band"-ish frequencies elsewhere — the PEQ flat-starter default and AutoEQ's
`FixedBandEQ.txt` — remain different features, not this grid.)

**4.2 GEQ ↔ PEQ conversion.** **Implemented (Rumble 4.3, Session 19) — GEQ-to-PEQ only, not the
reverse.** `rumble/core/geq_peq_convert.py` converts a GraphicEQ curve into a `Filter` (PK) list:
per-band Q is derived from the grid's own point spacing (a real, derivable relationship — denser
grid points imply a narrower natural bandwidth per band), followed by a bounded Newton-Raphson
pass that corrects dB-additive overlap bleed between adjacent bands so the combined PEQ response
tracks the source GraphicEQ curve closely, and a `MAX_BANDS`-respecting merge (simplifies rather
than silently truncating when a curve would need more bands than the ceiling allows). **As of the
band-fitter work, the "Convert to PEQ" action no longer uses this function — it calls
`fit_peq()` (§4.2b), which produces far better bands (a 127-point curve converts to ~5 fitted bands
at ~0.6 dB, where the point-mapper needed 10+ and still landed 1.5-4 dB out).**
`convert_geq_to_peq()` itself is retained (still exercised by `tests/selftest_geq_to_peq.py`) but is
no longer on any user-facing path. The action is still deliberately LOSSY — it replaces the curve
with bands and discards the residual `build_curve_config` would keep, because a "Convert" that left
a GraphicEQ layer behind would look like it had done nothing — so it still confirms first, and now
quotes a much smaller deviation. GEQ and PEQ remain independent, coexisting layers within the same
`ChannelSection` otherwise — this conversion is an explicit, opt-in, one-way action, not an
implicit reconciliation of the two. The pre-existing `_apply_iso_scaffold()`
(`advanced_panel.py:188-197`), which resamples an existing GEQ curve onto a different *GEQ* point
grid (10/15/31-band) via log-frequency interpolation, remains a separate, unrelated feature
(GEQ-grid-to-GEQ-grid resampling, not a PEQ conversion). **PEQ-to-GEQ (the reverse direction)
remains unimplemented** — out of this item's stated minimum scope ("at minimum, GEQ-to-PEQ").

**4.2a Lossless curve decomposition (curve-derived profiles).** Every profile built from a
measured curve — AutoEQ/Spinorama import, multi-measurement merge, gear simulation, REW import,
room measurement — used to be stored as a bare `GraphicEQ:` line with an empty `filters` list, so
the editor's band list (which renders `section.filters` and nothing else) showed **no bands at
all** and the profile looked empty/invalid. Such profiles are now *decomposed* by
`geq_peq_convert.decompose_geq()` via the shared `autoeq_merge.build_curve_config()`: the curve
becomes fitted, editable PK bands **plus a residual GraphicEQ layer** holding what the bands could
not reproduce. Because APO applies a section's filters and its GraphicEQ line in series, their dB
responses add, so bands + residual reconstruct the original curve — this is *not* the lossy
one-way "Convert to PEQ" action above; nothing is discarded. Reconstruction stays within
`WRITE_PRECISION_DB = 0.1` (the precision `Filter.to_line()` and `format_graphiceq()` round gains
to, i.e. the file format's own floor). Corollary: **a section's `graphic_eq` alone is no longer
"the profile's curve"** — read `autoeq_merge.section_curve()` (bands + GEQ summed) instead, as the
REW blend picker does.

**The residual grid is refined ADAPTIVELY** (`geq_peq_convert.solve_residual`), starting from the
source curve's own points and bisecting only the intervals that miss the target. This is not a
micro-optimisation — it was a correctness fix. The residual interpolates linearly between its points
while the band response it cancels is curved, and on real AutoEQ curves that error is not spread
evenly: it collapses almost entirely into the **top octave**, where biquad response steepens toward
Nyquist and AutoEQ's grids are coarsest (measured on AAW A2H V2: <=0.009 dB from 20 Hz to 12 kHz,
1.9 dB above it). The original uniform-doubling scheme therefore spent ~900 points spectrum-wide
fixing a problem that lived in the last few, hit the 1024 ceiling, and STILL missed target at
0.173 dB. Adaptive refinement lands the same curve at 0.093 dB with 145 points.

**4.2b The band fitter (`geq_peq_convert.fit_peq`).** `convert_geq_to_peq()` (§4.2) *maps* one
band per significant curve point and never chooses where a band belongs, so asking it for more
bands just hands each one fewer grid points to own, driving Q up and gain toward zero — at 32
bands on a 127-point curve, half the bands moved <1 dB and most landed past Q 4 (grid wiggle, not
audible features). `fit_peq()` instead *places* bands: seed one at the frequency where the curve is
still worst-approximated (its Q read off that feature's own half-height width), then re-solve
EVERY band's Fc/gain/Q jointly by Levenberg-Marquardt, because bands overlap in dB and a new one
changes what its neighbours should do. **The band count is an outcome, not an input** — bands are
added until the fit is within `WRITE_PRECISION_DB` of the curve, or the new band's gain rounds
away (`GAIN_EPSILON_DB`), or it improves the RMS fit by less than the file's own rounding noise
(`_ROUNDING_RMS_DB` = half a write quantum over sqrt(3)).

**Measured on 12 real AutoEQ curves** (fetched live from the provider index, not synthesised):
3-11 bands spent of the 32 available, total reconstruction error 0.073-0.099 dB (all within
target), residual 128-145 points, 12-158 ms per fit. The retired point-mapper scores **3-19 dB** on
those same curves — real corrections are far more jagged than the smooth sums-of-bells it was
first checked against, which is precisely why `tests/selftest_curve_decompose.py` now pins one real
curve (`REAL_AUTOEQ_CURVE`, AAW A2H V2) as a fixture rather than testing synthetic curves alone.
Two non-obvious properties, both learned the hard way and both regression-tested there:

* The greedy stopping test reads **RMS**, not peak, error. Peak error is a minimax quantity that
  plateaus — a band can flatten one peak while an equally tall one remains, showing almost no peak
  improvement — and using it as the greedy signal stops the fit several bands early (it quit at 2
  bands with a 3 dB error on a curve it could fit to 0.09 dB with 9).
* Band gain is **bounded to the curve's own peak-to-peak span**. Unbounded least-squares fits a
  +/-8 dB room curve with a -21 dB band against a +17 dB band: it fits beautifully and is useless
  to a human, since dragging either swings the response wildly. A band larger than the whole span
  can only exist to be cancelled by another. A ridge penalty alone does NOT fix this — once in that
  basin, escaping costs more error than any tie-breaking weight will pay, so the box is bounded.

**4.2c Sample-rate re-solve (`refresh_fits_for_sample_rate`).** Biquads warp with sample rate; a
GraphicEQ line does not. So a fitted profile's residual only cancels its bands exactly at the rate
it was solved for — played at another rate it drifts (~0.9 dB for a 96k fit played at 44.1k). Since
the bare-curve profiles this replaced were immune to that (no biquads), decomposition would
otherwise have traded exactness for editability. It does not: `ProfileManager
.refresh_fits_for_sample_rate()`, driven by the existing `DeviceFormatWatcher` via
`main_window._on_device_format_changed`, re-solves the residual whenever the device rate changes,
restoring exactness (<=0.1 dB) while leaving the BANDS untouched — the editable representation
never moves under the user. Four things make this safe, each regression-tested in
`tests/selftest_fit_sample_rate.py`:

* **Only fitted profiles are touched**, identified by the `Fit Sample Rate:` header
  (`profile_file.ProfileDocument.fit_sample_rate`, set by the curve-derived creation paths). On a
  hand-authored profile the GraphicEQ layer is deliberate tuning, not a residual — `rumble/user/
  arc_raiders_cache_hunter.txt` really does carry both bands and a GEQ line — and rewriting it would
  destroy the user's work.
* **Intent is recovered from the profile itself**, as `bands @ old_rate + residual`, never from a
  stored copy of the original curve. That is what makes it edit-safe: if the user has since dragged
  a band, the intent recovered is their EDITED profile, and the re-solve preserves it rather than
  silently reverting to the imported curve.
* **Bands are quantised to their written precision** (`geq_peq_convert.quantize_filters`: Fc/Gain to
  0.1, Q to 0.01) *before* the residual is solved. Otherwise the residual cancels bands that do not
  exist — the fit runs in float, but APO builds its biquads from the rounded values on disk.
* **The residual is written at `RESIDUAL_DECIMALS = 2`**, finer than the 0.1 dB a filter gain is
  written at (APO parses GraphicEQ gains as plain floats, so the decimals are free). At 0.1 dB each
  rewrite would inject +/-0.05 dB, which compounds every time the rate changes; at 2 dp the
  injection is +/-0.005 dB and 8 consecutive rate flips accumulate 0.059 dB total — less than a
  single fresh build.

**4.3 Arbitrary custom bands.** Yes, and this is the default/primary mode, not a special case —
`GraphicEQEditor` starts from 5 free points (`_DEFAULT_GEQ_POINTS`, `advanced_panel.py:25`) and
lets the user freely add/remove/drag any point to any (frequency, gain). The ISO-grid buttons are
explicitly non-destructive "re-lay" helpers that resample the existing curve shape onto standard
centres — not a hard mode switch.

---

## §5 — Preamp / headroom calculation

**5.1 Calculation method.** **True combined-response peak** via N-point magnitude-response
sampling — not a naive sum of band gains, not FFT-based, not closed-form analytic.
`preamp_calc._peak_response_db()` (`preamp_calc.py:51-64`):
```python
def _peak_response_db(filters, geq_string, sample_rate, section_offset_db=0.0, ir_response=None):
    freqs = eq_math.frequency_array()          # 512-point log-spaced grid, 10 Hz–24 kHz
    response = eq_math.combined_response(filters, freqs, sample_rate)
    if geq_string: response = response + eq_math.graphic_eq_response(geq_string, freqs)
    if ir_response is not None: response = response + ir_response
    return float(np.max(response)) + section_offset_db
```
Each filter's magnitude response is the exact RBJ ("Audio EQ Cookbook") biquad transfer function
evaluated at each grid point (`eq_math.py:46-52,55-161`). Documented derivation in
`docs/RUMBLE_Headroom_Maths.md` ("Step 3") with a worked counter-example showing why summing
individual band peaks (e.g. two overlapping +3/+2 dB boosts → +4.5 dB combined, not +5) would be
wrong — the full curve must be evaluated.

**5.2 Safety margin.** Yes, escalating, **not user-configurable via any Settings control**
(`preamp_calc.py:19-31`):
```python
_MARGIN_DEFAULT_DB = 0.5    # ordinary sample-peak guard
_MARGIN_HIGH_DB = 1.0       # escalated guard for inter-sample ("true") peak risk
_HIGH_BOOST_DB = 6.0        # combined boost above this escalates the margin
_SIZEABLE_BOOST_GAIN_DB = 4.0
_SIZEABLE_BOOST_COUNT = 3   # this many ≥4dB boosts also escalates the margin
```
```python
def _margin_db(peak_db, filters):
    if peak_db > _HIGH_BOOST_DB: return _MARGIN_HIGH_DB
    if sum(1 for f in filters if _is_sizeable_boost(f)) >= _SIZEABLE_BOOST_COUNT: return _MARGIN_HIGH_DB
    return _MARGIN_DEFAULT_DB
```
**Derivation** (`docs/RUMBLE_Headroom_Maths.md`, "Step 4" + 2026-07-07 addendum): the steady-state
sinusoidal peak the app computes can under-predict the true worst-case gain (impulse-response L1
norm) for a resonant/ringing filter. A numerical audit found the gap is driven by *boost count and
height*, not Q (a wide Q=0.3 boost measured a bigger gap than narrow ones); against real (pink
noise) program material the gap essentially never bites. `_MARGIN_HIGH_DB = 1.0` was set to match
broadcast/mastering "true-peak" practice (EBU R128/ATSC A/85, ~1 dBTP). The 2026-07-07 commit
(`235ec69`) changed only the **trigger condition** (from Q-based to boost-count-based), not the
margin values themselves.

**Unified as of Rumble 1.3 (Session 2, 2026-07-09).** The two preamp-calculation code paths
previously diverged: the measurement-import/merge/gear-simulation path,
`autoeq_merge.compute_preamp()` (`autoeq_merge.py:120-129`), always used a flat `-(peak + 0.5)`
margin regardless of boost count, while the main editor's "Auto Preamp" button used the escalating
0.5→1.0 dB logic above. `compute_preamp()` now delegates directly to
`preamp_calc.calculate_auto_preamp()` instead of maintaining its own formula, so a profile built
via import/merge/gear-sim and a profile built manually with the identical filter set produce the
identical Preamp value, including for the 3+-boosts-≥4dB case that used to diverge. Its call site
in `autoeq_dialog.py` was updated to thread through a `sample_rate` argument. See §13 for the
before/after correction record and RUMBLE_ACTION_GUIDE.md Tier 6 item 3 for the closeout write-up.

**5.3 Warn or block on save?** **Soft warning only — save is never blocked, Preamp is never
silently force-corrected unless "Auto" is on.** The headroom label
(`editor_panel.py:607-637`) shows green "Headroom: +X.X dB" or red "Clipping risk: X.X dB"; it can
further escalate to a distinct warning style if the *live spectrum analyzer* measures real output
clipping (a separate, measured — not calculated — signal). `MainWindow._on_save_current`
(`main_window.py:519-563`) validates only built-in/readonly/empty-name — **no clipping check
exists in the save path at all.** Preamp is only kept correct automatically if the per-profile
"Auto" toggle or the global "Automatically calculate pre-amp on save" setting (default `True`) is
enabled; with both off, a clipping profile saves exactly as entered. A one-time (dismissible)
general safety dialog appears on app startup, not per-save.

**5.4 Per-channel or global?** **Data model supports per-channel** (`ChannelSection.preamp_db:
float | None`, round-trips correctly per a documented fix — commit `15c5ecf`), **but the editor
UI only ever writes a single global value.** `EditorPanel._apply_auto_preamp()` takes the min
(most attenuating) across all channel signal chains and writes it to one spin box; on save,
`config.preamp = float(self.preamp_spin.value())` — there is no UI path that sets
`ChannelSection.preamp_db`. Per-section values only exist for round-tripping profiles that already
had them at import time (e.g. certain multi-channel AutoEQ-style files).

---

## §6 — Measurement & target curve handling

**6.1 Imported measurement formats.** AutoEQ's own exported formats and Spinorama's JSON, fetched
live from their online catalogues, **plus** (as of Rumble 3.8, Session 15) a user's own REW export.
Confirmed formats: AutoEQ `ParametricEQ.txt` (already valid APO text), `GraphicEQ.txt` (127-point
curve), `FixedBandEQ.txt`, `<Name>.csv` (raw/target columns for preference re-targeting),
minimum-phase `.wav` IRs; Spinorama `CEA2034.json` (Plotly figure, "Listening Window" trace
decoded). `docs/MEASUREMENT_DATA_SOURCES.md:8` confirms: *"Both sources are fetched live over HTTP
(never linking foreign code)."* REW: `rumble/core/rew_import.py` parses both of REW's own export
formats — a frequency-response measurement export (`Freq Hz`/`SPL dB`/`Phase degrees` columns) and
a "Filter Settings" parametric-EQ export — confirmed against REW's own documented conventions
(only lines beginning with a number are imported, matching REW's own `dataimport.html` rule). A
user's REW curve can either replace or blend with existing community data for the same device
(`REW_MEASUREMENT_WEIGHT`, a 2× multiple of Spinorama's own "high quality" weight tier, reflecting
that a user's own real measurement should outweigh any single community source) — exposed as an
explicit choice in the import UI rather than picked silently. **No `.mdat` (REW's binary project
format) support** — only the two plain-text export formats above.

**6.2 Target curves.** **A real named target-curve library now exists** (Rumble 3.4, Session 12) —
`rumble/core/target_curves.py` holds Harman 2018 (over-ear), Harman 2019 (in-ear), and Diffuse-field
(5128 rig), all resampled from AutoEQ's own bundled reference CSVs (each with a comment citing its
source). `eq_target.make_correction()` accepts a `target_curve` argument in place of (or alongside)
the generic linear tilt below, and a "Target curve" selector in the import UI defaults by detected
device type (over-ear → Harman 2018, in-ear → Harman 2019; diffuse-field is opt-in for either
headphones or speakers). **Free-field was deliberately not implemented** — no freely-available,
numerically-complete free-field dataset was found (AutoEQ doesn't bundle one; ISO 11904-1's
free-field part is paywalled), and inventing points would violate this project's no-guessed-
constants rule; left as an open follow-up. The generic tilt math still exists and is still the
default for plain speaker correction (`eq_target.py:66-69`):
```python
def tilt_target(grid, slope_db_per_oct=0.0, pivot_hz=1000.0):
    return [(f, slope_db_per_oct * math.log2(f / pivot_hz)) for f in grid]
```
Speakers default to flat or a mild **-0.9 dB/oct** heuristic room-preference tilt (user-adjustable
-3.0 to +1.0 dB/oct) unless a named curve is explicitly selected. Headphones without a selected
named curve still fall back to **AutoEQ's own baked-in Harman-based target** from its CSV, with
RUMBLE's generic tilt only layered on top when a preference adjustment is requested. **This
corrects the prior note in this section claiming no named curve presets are selectable — three of
the four researched curves now are; only free-field remains unimplemented.**

**6.3 Smoothing.** Fractional-octave, unweighted boxcar-average smoothing —
`octave_smooth(curve, frac_oct=1/3)` (`eq_target.py:38-56`), averaging each point over its ±(frac_oct/2)-octave
neighborhood. Default **1/3 octave**, user-adjustable 0.10–1.00 oct via Settings
(`spin_smoothing_oct`, setting key `spinorama_smoothing_oct`). This is separate from
`spectrum.py`'s live-audio spectrum-analyzer smoothing (IEC 60268-10 Fast/Slow ballistics EWMA),
which is a display feature, not a measurement-import parameter.

**6.4 Fitting algorithm.** **RUMBLE implements no least-squares/gradient-descent/published
band-fitting algorithm of its own** (repo-wide search for `least_squares`, `curve_fit`,
`scipy`, `gradient`, `fit_*`/`optimi*` found nothing in `rumble/core/`). Headphones: RUMBLE consumes
AutoEQ's own **pre-computed** `ParametricEQ.txt` verbatim. Speakers (and any re-tilted headphone
import): a direct subtract-smooth-clamp-taper transform, `eq_target.make_correction()`
(`eq_target.py:88-117`) — `correction(f) = clamp(target(f) − smoothed_measured(f), -12..+6 dB) ×
edge_taper(f)` — producing a point-for-point **GraphicEQ curve** (12 points/octave), not a
discrete-band optimizer output. Defaults: `max_boost_db=6.0`, `max_cut_db=12.0`. **Any
documentation describing RUMBLE as running its own least-squares/gradient-descent PEQ fitting would
be inaccurate to current code.**

**6.5 Community measurement databases.** Two providers exist: `AutoEqProvider` and
`SpinoramaProvider` (`measurements/registry.py:12-20`) — **no REW, Squiglink, or Crinacle
provider exists in code**; Crinacle appears only in a comment as a hypothetical future
never-auto-enable example. Both existing providers are **live-fetched over HTTP** (not bundled),
and both are **`enabled_by_default = True`** — i.e. currently neither is gated behind an opt-in
toggle, and **no Settings UI exposes a per-source enable/disable control at all**
(`registry.py:6`: *"there is no UI to disable a source — all sources are available at all
times"*). `SpinoramaProvider.commercial_safe = False` (owner-held ASR/EAC data) but this only
affects a licensing flag, not whether it's fetched by default. **This corrects the stale project
memory claiming "AutoEQ/REW ship, Squiglink/Spinorama opt-in, Crinacle never" — the accurate
statement is: AutoEQ and Spinorama both ship live-fetch and both are enabled-by-default with no
opt-in gate; REW, Squiglink, and Crinacle providers do not exist in code today.**

---

## §7 — Entity relationships & validation rules

**7.1 Every validation rule.** All manual/expert-editing rules are **soft UI-level range clamps**
— wider than the research-recommended perceptual bounds by design (per RUMBLE_ACTION_GUIDE.md #2.6,
resolved 2026-07-09, Rumble campaign Session 7). **Fc now carries a real Nyquist-derived hard
limit** (`sample_rate * NYQUIST_FRACTION`, `filter_row.py:196-198`, live-updated in
`set_sample_rate()`) rather than the fixed 96000 Hz literal this row previously showed — the "no
Nyquist limit exists" gap noted below was resolved by Session 1, see §13 item 8.

**The Rumble 2.6 decision, per parameter** (guide's comparison: research range vs. this app's UI
clamp): keep every *manual/expert-editing* UI clamp in the table below unchanged — a general-
purpose editor is legitimately more permissive than a perceptually-reasoned default, and an expert
user deliberately reaching for Q=80 or a 30 dB boost is not a bug. Separately, tighten every
*auto-generated* curve toward the tighter research-recommended range:

- **Gain**: research range ±20 dB hard / soft −12 (cut) / +6 (boost). Auto-generated speaker
  corrections already enforced exactly this via `eq_target.make_correction()`'s boost
  cap/cut floor (Rumble 1.4, unchanged by this session — 6/12 dB was already inside the research
  bound, not loosened to meet it). The gap: `autoeq_merge`'s gear-simulation delta curve
  (`target − source`, `autoeq_dialog.py:_create_sim_profile`) had **no gain bound at all** — two
  arbitrary measured curves (e.g. an in-ear vs. an over-ear headphone) can differ by more than
  either curve's own range. Fixed with a new `eq_target.clamp_gain()` helper (same
  default 6/12 dB cap, same `ClampSummary` reporting, reusing the existing clamp-notice UI).
  A structurally identical gap survived in the same class of operation until 2026-07-17:
  `AutoEqProvider.fetch_curve()` (`measurements/autoeq_provider.py`) re-derives a headphone
  correction as the same kind of unbounded `target − measured` delta whenever a preference tilt
  or a named target curve is requested — this path was missed when gear-sim was fixed, since it
  lives in a different module. Now also routed through `clamp_gain()`.
- **Fc**: research range 20–20,000 Hz vs. this app's Nyquist-derived UI ceiling (up to 24,000 Hz at
  48 kHz). No change: every auto-generation grid already stays inside the research band by
  construction — `eq_target.log_grid()` defaults to 20–20,000 Hz and `make_correction()`'s default
  usable `band` is `(20.0, 16000.0)` — so the wider Fc ceiling only ever applies to a human typing a
  value in, which is the intentionally-wider manual/expert case.
- **Q / Bandwidth**: research range Q≤20 / BW≤4 oct vs. this app's Q≤100 / BW≤12 oct manual clamp.
  No auto-generation call site currently produces a discrete per-band Q or Bandwidth value at all —
  every auto-generated correction today is a GraphicEQ gain curve (points, no Q/BW), not a
  parametric filter — so the wider manual clamp is the *only* bound in play right now; there is
  nothing to tighten yet. Flagged for whoever builds Tier 4.10 (crossover-design tool, the first
  planned feature that would auto-generate parametric Q/BW values): apply the tighter Q≤20/BW≤4 oct
  research bound there rather than reusing the manual UI's wider ceiling.

| Rule | Value | Type | Reason | Location |
|---|---|---|---|---|
| Band Fc range | 1.0 Hz – `sample_rate × 0.5` (hard Nyquist), soft warn above ×0.45 | Hard UI clamp + soft warning | Matches physical bound (Nyquist); intentionally not the tighter 20–20,000 Hz research range, since a human may legitimately need to place a filter closer to Nyquist than "typical" | `filter_row.py:196-198`, `NYQUIST_FRACTION` |
| Band Gain range | -30.0–30.0 dB | Hard UI clamp | Intentionally wider than the ±20 dB research bound — manual/expert editing, not auto-generation | `filter_row.py:209-210` |
| Band Q range | 0.1–100.0 | Hard UI clamp | Intentionally wider than the ≤20 research bound; no auto-generation path produces a bare Q value yet | `filter_row.py:223-224,319` |
| Band BW range | 0.01–12.0 oct | Hard UI clamp | Intentionally wider than the ≤4 oct research bound; same "no auto-generation call site yet" rationale as Q | `filter_row.py:312` |
| Global Preamp range | -30.0–0.0 dB (never positive) | Hard UI clamp + algorithmic | Preamp only ever attenuates — a positive value would defeat its own purpose. Fixed 2026-07-17: `calculate_auto_preamp()` previously had no floor clamp of its own and relied on the editor's spin-box `setValue()` to silently truncate an over-negative result on load, which then got saved back — reintroducing clipping. Both sides now share `preamp_calc.PREAMP_MIN_DB`. | `editor_panel.py:554`; `preamp_calc.py:32,112` |
| Section trim range | -30.0–30.0 dB | Hard UI clamp | Manual/expert control, not auto-generated | `editor_panel.py:773` |
| Section delay range | 0.0–5000.0 ms | Hard UI clamp | Matches realistic multi-speaker/sub distance alignment range | `editor_panel.py:794` |
| LoudnessCorrection level/offset | -100.0–100.0 | Hard UI clamp | Matches upstream Equalizer APO's own accepted range for the command (§14.1) | `advanced_panel.py:407,425` |
| Max bands per profile | 32 | Hard block (no-op past cap) | Higher than the ~10-20 band point of diminishing returns for headphone correction (research finding); confirmed (Rumble 4.12, Session 17) as a deliberate arbitrary round-number RUMBLE-side ceiling — Equalizer APO itself imposes no filter-count limit | `editor_panel.py:36,950-952` |
| Default new-profile band count | 1–32 | Hard UI clamp (setting) | Mirrors the Max-bands ceiling above | `settings_dialog.py:395-399` |
| Correction boost cap (speaker/tilt import) | 6.0 dB default, adjustable | Soft, silent clamp, reported via `ClampSummary` | Matches (and is tighter than) the ±20 dB / soft +6 research bound for auto-generated gain | `eq_target.py:188,232` |
| Correction cut floor | 12.0 dB | Soft, silent clamp, reported via `ClampSummary` | Matches the soft −12 dB research bound for auto-generated gain | `eq_target.py:188,236` |
| Gear-sim delta-curve gain bound | 6.0/12.0 dB default (new, Rumble 2.6) | Soft, silent clamp, reported via `ClampSummary` | Same research bound as the row above — previously unbounded, since a curve difference has no inherent range | `eq_target.py:clamp_gain()`; `autoeq_dialog.py:_create_sim_profile` |
| Preamp anti-clip margin | 0.5 / escalates to 1.0 dB | Algorithmic, silent | Derived from real measured biquad-cascade peak-vs-steady-state gaps (see `RUMBLE_Headroom_Maths.md`), not a round-number guess | `preamp_calc.py:19-48` |
| Clipping / negative headroom | — | **Soft warning only**, never blocks Save | Deliberate: manual-Preamp workflow is explicitly supported (§7.3-3) | `editor_panel.py:607-637`; `main_window.py:519-563` |
| Directivity-based boost damping (speaker) | scales cap 50%→100% over 15°–40° directivity | Soft, silent | Calibrated against real Spinorama good/poor-directivity speaker data (see `eq_target.py`'s Phase F0 comment block) | `eq_target.py:169-196` |
| Save refusal: built-in / readonly / empty name | — | **Hard block** | Data-integrity guarantee, not a numeric range | `main_window.py:526-541` |

**7.2 Automatic recalculation on downstream edits.** Automatic via one funnel of Qt signals — every
band edit (`FilterRow.changed`) and every advanced-panel edit connects to `_on_field_changed()` →
`_recompute(mark_dirty=True)` (`editor_panel.py:965,983-985,1785-1808`):
```python
def _recompute(self, *, mark_dirty: bool) -> None:
    self._refresh_ir_result()
    if self.btn_auto_preamp.isChecked():
        self._apply_auto_preamp()
    self._update_headroom_label()
```
The headroom label always recomputes; Preamp itself only moves if "Auto" is on. Sample-rate
changes trigger the same recompute path (`set_sample_rate()`, `editor_panel.py:1828-1865`) but —
consistent with §7.1's finding — **never revalidate any band's Fc against the new Nyquist limit**,
because no such check exists at all. Preamp/Auto toggles are wired as "per-session controls" that
refresh the live preview without marking the profile dirty — deliberate, documented behavior.

**7.3 Known gaps / inconsistencies** (confirmed from code, no TODO/FIXME comments exist anywhere
in the reviewed math/validation files — the team documents caveats as prose instead). **All seven
items below were open at the time this section was first written (2026-07-08); by the Rumble
campaign's closeout (Session 29, 2026-07-10) all seven are resolved — kept here for the record with
resolution notes rather than deleted, per this project's documentation-consistency convention:**

1. **RESOLVED (Rumble 1.3, Session 2).** Two inconsistent preamp-margin formulas coexisted — the
   editor's "Auto" button escalated 0.5→1.0 dB by boost count/height, while the
   measurement-import/merge/gear-sim path (`autoeq_merge.compute_preamp()`) always used a flat 0.5
   dB regardless of boost count. `compute_preamp()` now delegates to the same escalating
   `preamp_calc.calculate_auto_preamp()` — see §5.2.
2. **RESOLVED (Rumble 1.1, Session 1).** No Fc-vs-sample-rate (Nyquist) validation existed anywhere
   — see §7.1's table row and §13 item 8 for the fix (live-updated hard limit at 0.5× sample rate,
   soft warning above 0.45×, re-validated on every sample-rate change).
3. **Still accurate, unresolved by design (not a gap).** Preamp recalculation on edit remains
   opt-in, not guaranteed: with both "Auto preamp" (per-profile) and "Automatically calculate
   pre-amp on save" (global default `True`) turned off, a user can add large boosts and save with
   zero automatic correction and zero block — only the passive red "Clipping risk" label, now
   joined by a save-time confirmation dialog when headroom is negative and Auto is off (Rumble 5.1,
   Session 27) rather than a fully silent save. Deliberate design — manual-Preamp workflow is
   explicitly supported.
4. **RESOLVED (Rumble 1.4, Session 3).** Correction-curve clamping was silent — `eq_target.
   make_correction()`'s boost/cut caps and the directivity-aware boost damping altered the
   generated correction with no on-screen indication. A `ClampSummary` is now threaded up to the
   import/merge dialog, which shows a "N points were limited by the boost/cut cap" notice whenever
   the clamped delta exceeds 0.5 dB.
5. **RESOLVED (Rumble 3.4, Session 12).** No named target-curve library was implemented — see §6.2
   for the shipped Harman 2018/2019 + diffuse-field library (free-field intentionally not
   implemented, no freely-available dataset found).
6. **RESOLVED (Rumble 1.5, Session 1).** `device_format_adapter.py`'s module docstring stated
   "after its 500 ms debounce" though the actual debounce was raised to 1.5s in commit `235ec69`
   (`device_format.py:289`) — comment now matches the real constant.
7. **RESOLVED (Rumble 1.2, Session 2).** `docs/RUMBLE_SPECIFICATION.md` item #16 (custom IIR
   filters) described "IIR" as a selectable filter type producing "a filter with no usable fields"
   since no coefficient-editor UI exists. "Custom IIR" is no longer offered in the filter-type
   dropdown (`apo_config.IIR_EDITOR_AVAILABLE = False`; `filter_row.py::_populate_type_dropdown()`)
   — a user can no longer newly select it, which removes the dead-end trap this item described. An
   existing IIR filter round-tripped from an imported/foreign config still loads, displays
   (read-only), and re-saves correctly. The coefficient-editor UI itself remains unbuilt
   (`docs/PLANNED_WORK.md` item #16, still open, out of this campaign's scope) — this item's gap
   was specifically about the dead-end-selection trap, which is now closed.

---

## §8 — Device / channel / sample-rate handling

**8.1 Channel-layout → `Channel:` mapping.** Plain ordered-name table, `editor_panel.py:39-46`:
```python
CHANNEL_PRESETS = {
    "Stereo / Headphones":  ["L", "R"],
    "Stereo + Sub (2.1)":   ["L", "R", "LFE"],
    "Surround (4.0)":       ["L", "R", "RL", "RR"],
    "Surround + Sub (4.1)": ["L", "R", "RL", "RR", "LFE"],
    "5.1 Surround":         ["L", "R", "C", "LFE", "RL", "RR"],
    "7.1 Surround":         ["L", "R", "C", "LFE", "RL", "RR", "SL", "SR"],
}
```
Each channel name becomes its own `Channel: <name>` block; an arbitrary custom layout is also
supported via a free-text prompt. Only the stereo L/R pair supports the "linked" collapse into one
`"L R"` block — surround layouts always keep one section per channel.

**8.1.1 Reconciliation with Equalizer APO's official channel table — decided, Rumble 6.4.**
Confirmed against APO's own "Configuration reference" wiki, "Channel (since version 0.8)" section
(checked 2026-07-10; primary source, not the summarized/incomplete version some AI search
summaries return). APO's official table:

| Configuration | L | R | C | LFE | RL | RR | RC | SL | SR |
|---|---|---|---|---|---|---|---|---|---|
| Mono | | | 1 | | | | | | |
| Stereo | 1 | 2 | | | | | | | |
| Quadraphonic | 1 | 2 | | | 3 | 4 | | | |
| Surround | 1 | 2 | 3 | | | | 4 | | |
| 5.1 Surround | 1 | 2 | 3 | 4 | | | | 5 | 6 |
| 5.1 Surround | 1 | 2 | 3 | 4 | 5 | 6 | | | |
| 7.1 Surround | 1 | 2 | 3 | 4 | 5 | 6 | | 7 | 8 |

(Yes, "5.1 Surround" is listed twice — APO treats RL/RR and SL/SR as two equally-valid identifier
sets for the same physical 5th/6th channel, matching the two different ways Windows itself has
labeled 6-channel "5.1" configurations over the years.)

**Decision: kept intentionally different — curated consumer list, not a mirror.** Per-row
comparison against `CHANNEL_PRESETS`:
- `"Stereo / Headphones"` = `[L, R]` — exact match to APO's Stereo row.
- `"Surround (4.0)"` = `[L, R, RL, RR]` — matches APO's **Quadraphonic** identifiers exactly, but
  under a friendlier name. APO's own config literally named "Surround" is a *different* 4-channel
  layout (`L, R, C, RC`) that RUMBLE does not expose as a preset — naming collision noted so nobody
  "fixes" this as a bug later.
- `"5.1 Surround"` = `[L, R, C, LFE, RL, RR]` — matches one of APO's two official 5.1 identifier
  variants (the RL/RR one, not SL/SR). Both resolve to the same channels per APO; RL/RR was kept
  as the more recognizable consumer term. Not a bug.
- `"7.1 Surround"` = `[L, R, C, LFE, RL, RR, SL, SR]` — exact match to APO's 7.1 row.
- `"Stereo + Sub (2.1)"` and `"Surround + Sub (4.1)"` — **no corresponding row exists in APO's
  table at all.** APO's own docs state: *"If a channel configuration is not supported, channels
  can only be selected by number."* This means on real hardware whose Windows-reported speaker
  format APO doesn't recognize as one of its six named configurations, the `LFE` identifier
  specifically may not resolve to any channel — a profile built from the 2.1/4.1 preset could
  silently produce a no-op `Channel: LFE` block on such a device.

  **Follow-up fix (same day, post-6.4): surfaced, not silently renamed.** Renaming these two
  presets' channels to bare numeric indices was considered and rejected — several other shipped
  features pattern-match the literal `"L"`/`"R"`/`"LFE"` strings (`listener_correction.py`'s L/R
  ear detection, `main_window.py`'s LFE-only-bass-filter warning from Rumble 5.4, and
  `time_alignment.py`'s `STANDARD_CHANNEL_ORDER`), so silently swapping to numbers would break
  those working features for anyone using these two presets. Instead, `editor_panel.py` now
  exposes `NON_STANDARD_CHANNEL_LAYOUTS` (`{"Stereo + Sub (2.1)", "Surround + Sub (4.1)"}`) and
  `NON_STANDARD_CHANNEL_LAYOUT_WARNING`, driving a one-time notice at the point of selection:
  `NewProfileDialog`'s inline hint updates in place when either layout is picked for a new
  profile, and `EditorPanel._on_preset_changed()` shows a `QMessageBox.information()` when either
  is picked for an existing profile (only after the layout actually takes effect — guarded against
  the "user cancelled a destructive-change confirmation" path). Covered by
  `tests/selftest_channel_layout_warning.py`. This makes the user aware at decision time rather
  than leaving a silent no-op to be discovered later; it does not — and given the pattern-matching
  dependency above, should not — change what gets written to the `.txt` config file.

Rationale for keeping the lists different rather than reconciling to an exact mirror: APO's table
includes entries no RUMBLE user needs (Mono, the C/RC "Surround" variant) and is missing the
2.1/4.1 shorthand names most consumers actually think in — mirroring it exactly would make the
preset dropdown both longer and less recognizable. See the code comment directly above
`CHANNEL_PRESETS` in `editor_panel.py:50-72` for the same writeup kept in sync with this section.

**8.2 Runtime sample-rate change.** Handled live but **only as a display/derivation refresh, not
an IIR-coefficient recompute.** `DeviceFormatWatcher` → `ProfileManager.set_detected_sample_rate()`
→ `EditorPanel.set_sample_rate()`, which re-derives delay ms↔samples display and re-runs
headroom/graph/IR-mismatch checks — explicitly *not* marked dirty ("a display conversion, not an
edit"). **No IIR/custom-coefficient recalculation exists** — `Filter.iir_coefficients` is stored
and round-tripped as an opaque string with no dependency on sample rate anywhere in the codebase.
This is a real gap: switching to a device with a different sample rate does not regenerate or warn
about stale IIR coefficients, which are inherently sample-rate-specific in Equalizer APO.
Confirmed via `git show 235ec69`: the 1.5s debounce value itself was derived from **measuring**
the real (undebounced) Windows endpoint-notification stream — a fast notification cluster plus one
confirmation ~1.0–1.1s later — not guessed.

**8.3 Per-device profiles via `Device:`.** Fully supported. `ProfileConfig.device` round-trips
APO's `Device:` line; the editor's device combo is populated from real APO-hooked endpoints via
`audio_devices.list_apo_devices()`, each rendering an exact `Device:` match pattern
(`"deviceName connectionName"`).

---

## §9 — Import / export & interoperability

**9.1 Export formats.** Plain `.txt` profile export (still APO syntax + RUMBLE metadata header,
not a different format); a bundle zip export (`.txt` + referenced IR file); a full-library backup
zip (`manifest.json` + entire `data/`+`rumble/` trees, proprietary format, versioned via
`BACKUP_FORMAT_VERSION`). **No REW-text exporter, no standalone GraphicEQ-line exporter, and no
JSON profile-export format exist** — flagged as not implemented.

**9.2 Round-trip import of existing APO configs.** **Yes — genuine round-trip**, reusing the same
parser/renderer pair used for writing (`apo_config.parse_config()` / `render_config_body()`).
`discover_importable_configs()` / `import_apo_candidates()` (`profile_manager.py:1270-1339+`)
flatten nested `Include:` chains (`resolve_includes()`), parse into the full structured
`ProfileConfig` (filters, channels, GraphicEQ, Delay, VSTPlugin, LoudnessCorrection, If/ElseIf
blocks preserved verbatim, unknown lines preserved in `extra_lines`), and this is the same
infrastructure used to reconcile "drift" on every launch — a durable path, not a one-off migration
script.

**9.3 Preset sharing / cloud sync / community library.** **Not implemented.** No cloud sync, no
community-sharing feature, no marketplace. The only cross-user mechanisms present are (a) the
read-only AutoEQ community database fetch (not RUMBLE-hosted or user-contributed), and (b) manual
export/backup zips a user could hand to someone else — no in-app discovery/upload/download of
other RUMBLE users' presets.

---

## §10 — UI/UX data requirements

**10.1 Parameter widgets.** Numeric fields only (`QDoubleSpinBox`) — no literal sliders for band
parameters:
- Fc: `1.0` Hz – `sample_rate × 0.5` (live Nyquist-derived hard ceiling as of Rumble 1.1, Session 1;
  no longer the fixed 96000.0 Hz literal), 1 decimal, soft-warning style above ×0.45
  (`filter_row.py:196-198`)
- Gain: `-30.0`–`30.0` dB, 1 decimal (`filter_row.py:197-203`)
- Q: `0.1`–`100.0` (2 decimals) / BW mode: `0.01`–`12.0` oct (`filter_row.py:283-302`)
- Preamp: `-30.0`–`0.0` dB, 1 decimal (`editor_panel.py:394-396`)
- Trim: `-30.0`–`30.0` dB; Delay: `0.0`–`5000.0` ms (`editor_panel.py:773-796`)
- GraphicEQ points: dragged handles, clamped freq `[1.0, 96000.0]`, gain `[-30.0, 30.0]`
  (`advanced_panel.py:244-251`)
- LoudnessCorrection level/offset: `-100.0`–`100.0` dB (`advanced_panel.py:407-431`)

The **graph itself is a drag-based analog control** — `EQGraph`'s `_BandHandle` (pyqtgraph
`TargetItem`) lets the user drag a point to set Fc/Gain, with keyboard-arrow fine control (0.5 dB
step, 0.1 dB with Shift, 3 dB with PageUp/Down).

**10.2 Live combined-response graph.** Yes, recomputed on every edit
(`EQGraph._update_response_curves()`). **Same engine as the Preamp calculation** — both call
`eq_math.combined_response()` (§0.2); no duplicated math. The graph also overlays a live spectrum
analyzer (real WASAPI loopback capture, `SpectrumController`) as a separate, independently-drawn
*measured* trace, and can drive a "measured clipping" indicator that overrides the theoretical
headroom label when real sustained output peaks are detected. As of Rumble 3.7 (Session 14), this
measured-vs-calculated comparison is no longer clipping-specific only: `spectrum.
drift_correlation()`/`drift_expected()` generalize it into a broader drift signal (divergence
across a meaningful frequency range, not a single point, with its threshold derived from real
captured spectrum data against a known-correct EQ to establish a realistic noise floor) — surfaced
as a distinct UI indicator alongside, not replacing, the existing clipping-specific one. See §12.9.

**10.3 Simple vs. advanced mode.** **Not implemented as a complexity-hiding toggle.** Instead, a
flat 3-tab layout: "EQ Editor" (graph + Fc/Gain/Q strip, always visible), "Advanced" (Graphic EQ /
Processing / Routing / Scripting sub-tabs), "Raw Config" (literal APO text). Nothing is ever
hidden based on a skill-level setting. (A separate `density.py` "compact vs. Double" window-scale
mechanism exists but is a visual-scaling accessibility feature, not a feature-hiding mode — do not
conflate the two.)

---

## §11 — Roadmap / extensibility

**11.1 Planned but not built.**
- **Room correction via Convolution**: `ir_analysis.py` is shipped, working code, but strictly
  **read-only IR analysis/preview** — it does not measure a room or derive a corrective IR. The
  user must supply an externally-generated IR file.
- **Multi-speaker delay/time-alignment**: per-channel delay is implemented (manual ms entry,
  tooltip guidance "~1 ms per 34 cm of extra distance"), **and** (Rumble 4.9, Session 25) an
  automatic/measured path now exists too — see §12.9 for the full mic-sweep/GCC-PHAT write-up.
- **Loudness compensation via ISO 226**: **Built** (Rumble 3.5, Session 16, 2026-07-09) as a real,
  RUMBLE-authored ISO 226:2003 equal-loudness-contour computation — see the new §16 write-up. The
  `LoudnessCorrection:` editor described above is unchanged and remains a thin editor over
  upstream's own heuristic curve; the two are independent, addressable separately.
- **Crossover design**: **Built (Rumble 4.10, Session 26).** `rumble/core/crossover.py` derives
  Linkwitz-Riley Q values from the standard Butterworth pole-angle formula (LR4 = 2 cascaded
  Q=1/√2 stages; LR8 = 4 stages at a 4th-order Butterworth's two pole-derived Q values, each
  twice — numerically verified to sum flat to 0 dB across the transition band). `rumble/ui/
  crossover_dialog.py` (Tools → Crossover Design…) lets the user pick Fc/slope/target channels,
  add optional all-pass phase alignment (`AP` filter type), preview both branches live, and save
  as a new profile. `SpinoramaProvider.suggested_filters()`'s single subsonic-protection high-pass
  suggestion remains a separate, narrower feature.
- **Multi-listener idea**: **Built, as "Listener Slots" (Rumble 3.3, Sessions 8, 9.1-9.8).** See
  §15 for the full write-up. The original parked spec
  (`docs/superpowers/specs/2026-07-06-multi-listener-hearing-correction-design.md`) remains
  parked/unimplemented as written — it was superseded by a fresh brainstorm-and-plan pass
  (`docs/superpowers/specs/2026-07-09-listener-correction-design.md`) before implementation, not
  resumed from the parked doc directly.
- Per `docs/PLANNED_WORK.md`, now shipped: device-aware auto-switching via native APO `If`/
  `ElseIf` generation (Rumble 4.4, Session 21 — an explicit export/complement to the existing
  RUMBLE-layer `auto_switch.py`, not a replacement; `app`/`time`-trigger rules have no native
  equivalent and are reported as untranslatable), a foreign-tool coexistence dashboard (Rumble
  4.8, Session 24), a config-health/effective-chain viewer (Rumble 4.6, Session 20), a
  backup-history restore-picker UI (Rumble 4.1, Session 18), and a Peace `.peace` importer (Rumble
  4.5, Session 23). **Still open, out of this campaign's scope:** per-angle directivity curve fetch
  (`docs/PLANNED_WORK.md` #6, tier (b) — distinct from the now-shipped Rumble 3.6 gap-suppression,
  see that item's note) and a custom-IIR coefficient editor (`docs/PLANNED_WORK.md` #16 — "Custom
  IIR" is no longer even selectable as of Rumble 1.2, Session 2, which closes the worse "dead-end
  selection" trap this item used to describe, but the editor itself remains unbuilt).
- **Also built this campaign, not previously listed here:** automated measured multi-speaker
  time-alignment via mic sweep (Rumble 4.9, Session 25 — see §12.9), GEQ→PEQ conversion (Rumble
  4.3, Session 19 — see §4.2), a real named target-curve library (Rumble 3.4, Session 12 — see
  §6.2), a REW import path (Rumble 3.8, Session 15 — see §6.1), directivity-gap correction
  suppression (Rumble 3.6, Session 13 — see §7.1), a generalized spectrum drift indicator (Rumble
  3.7, Session 14 — see §10.2/§12.9), and an ISO 226 loudness-compensation layer (Rumble 3.5,
  Session 16 — see §16).

**11.2 Copy / Stage / If-ElseIf usage.** **All four are fully implemented**, with dedicated UI
editors — not merely parsed-and-preserved:
- `Copy:` routing — `ProfileConfig.copy_lines`, editable via `CopyEditor` (free-text mixing
  expressions like `"L=L+0.5*R"`).
- `Stage:` — `ProfileConfig.stage`, curated dropdown (Default/Pre-mix/Post-mix only/Capture
  only/All stages) plus a custom passthrough.
- `If`/`ElseIf`/`Else`/`EndIf` — `ProfileConfig.conditional_blocks`, editable via
  `ConditionalEditor` (raw text with syntax highlighting, documented runtime variables:
  `sampleRate, inputChannelCount, outputChannelCount, deviceName, connectionName, deviceGuid,
  stage`).
- `Eval` expressions (adjacent feature) — also implemented via `EvalEditor`.
**Automatic generation** of `If`/`ElseIf` blocks for device-aware auto-switching is now also built
(Rumble 4.4, Session 21) — `auto_switch.generate_native_conditional()`, exposed via an "Export as
Native Conditional…" button in `AutoSwitchDialog`, translates *device*-trigger rules into a native
block as an explicit user action complementing (not replacing) the existing RUMBLE-layer
`Include:`-rewriting mechanism (§0.7). `app`/`time`-trigger rules have no native APO expression-
language equivalent and come back as `.untranslatable`, listed rather than silently dropped. Manual
authoring of all four commands was already fully shipped before this addition.

**11.3 Known math limitations/technical debt.** No TODO/FIXME/XXX comments exist in the EQ-math
files; caveats are documented as prose instead:
- Shelf filters (6/12 dB variants) use approximated fixed-Q values to resemble first/second-order
  shelving responses — "APO's exact internal implementation may differ slightly" (`eq_math.py:3-7`).
- The graph's dashed "points" connector line through band gains is deliberately non-physical
  (straight lines through raw gain values, for readability of gain deltas between adjacent bands).
- The preamp anti-clip margin (§5.2) is an empirically-calibrated heuristic, not an exact bound —
  documented gap between steady-state peak and true worst-case impulse-response peak for resonant
  filters.
- `ir_analysis.py` deliberately caps at 10 seconds of audio and does power-domain fractional-octave
  smoothing — a preview approximation, not a full-fidelity convolution engine (APO does that at
  runtime).
- `autoeq_merge.interp_log` clamps out-of-range grid points to the curve's nearest endpoint rather
  than extrapolating — documented simplification.
- The most concrete open "EQ math" gap: custom IIR filters are selectable but unusable (no
  coefficient editor) — §7.3 item 7.

---

## §12 — Built-in tools, test routines, and external database integrations

**12.1 AutoEQ headphone/IEM database.** **Live-fetched from GitHub raw content, with a local disk
cache — not a bundled snapshot.** Source: `raw.githubusercontent.com/jaakkopasanen/AutoEq/master/results/INDEX.md`.
Cache: `data_dir/autoeq_index.md`, reused while younger than **7 days**
(`DEFAULT_MAX_AGE_DAYS = 7`); a forced or expired refresh re-downloads; network failure falls back
to a stale cache if one exists. Parsed schema: `AutoEqEntry{name, source, rig, path, device_type}`
(`device_type` classified from the path string as Over-ear/In-ear/Earbud/Other). Per-device
fetches (parametric/graphic/fixed/IR/CSV) are fresh `urllib.request` GETs, not disk-cached
individually. Update cadence: 7-day TTL auto-refresh **plus** a manual "Refresh Index" button.
Provider metadata: `license="MIT"`, `commercial_safe=True`, `enabled_by_default=True`.

**12.2 Speaker database (Spinorama.org).** Also live-fetched, disk-cached, not bundled. Base:
`spinorama.org`; index shards `metadata-<shard>.json` merged and cached (7-day TTL, same
stale-on-failure fallback); per-measurement CEA2034/Estimated-In-Room curve JSON files are
**separately cached** on disk keyed by SHA1 of the URL (worth caching — ~40 KB Plotly figures vs.
AutoEQ's cheap one-line files). Parsed schema: `SpinoramaEntry{name, brand, model, source,
measurement_key, quality, review, shape, speaker_type, ref_6db, ref_3db,
directivity_horizontal_avg, directivity_vertical_avg, device_type="Speaker"}`. Curve decoding
handles both plain-list and base64 typed-array Plotly trace formats; default trace pulled is
"Listening Window". Provider metadata: `license="data: owner-held (ASR/EAC)"`,
`commercial_safe=False`, `attribution="spinorama.org"`, but **`enabled_by_default=True`** — the
`commercial_safe` flag is licensing-only, it does not gate whether the provider fetches by
default. Both providers are unified through `FederatedCatalog` for one merged search UI.

**12.3 Hearing / "Ear Calibration" test — full protocol.** UI-branded "Ear Calibration"
(renamed text-only, 2026-07-08); internally "Test A." Measures **per-ear, per-frequency hearing
threshold** (quietest audible level) — NOT loudness matching, NOT a measurement of the playback
chain's own response. Explicitly framed as informal/relative, never medical.

**Single progressive test + per-point dashboard (2026-07-13 redesign).** The old two-mode
choice (standard 11-point vs a coarse 5-point "Quick" toggle) is GONE — there is one test over
one canonical point list (11 frequencies x 2 ears), fronted by a persistent dashboard
(`_build_freq_dash_page()`): a grid of per-point chips showing each point's state (measured
relative-dB value / skipped / "limit" for ceiling give-ups / "not yet", flagged points marked),
clickable to re-test ANY single point, in any order, across any number of sessions. Point results
live in a per-slot working state file (`listener_slots.load_calibration_state()` /
`calibration_state.json`; seeded in-memory from the latest saved run for pre-dashboard slots) and
every completed point is written into it IMMEDIATELY, so leaving mid-pass loses only the point
still being narrowed down. "Continue Calibration" queues the unmeasured points in
`progressive_order()`: the five baseline frequencies `[250,1000,4000,8000,16000]`
(`BASELINE_FREQS`, the old Quick set, each half an outward sweep from the 1 kHz anchor) first,
then the detail infill — after the baseline half a milestone note says the calibration is usable
("stop here or keep going"; `baseline_complete()`, skips count as resolved). A volume guard on
the dashboard compares the state's stored volume reference against the current master volume and
offers a one-click restore (`master_volume.set_master_volume_percent()`, added for this), since
thresholds are only comparable at the volume they were measured at. The volume-prep
(reference-voice) page shows once per dialog sitting, before the first pass only.

Protocol per point: adaptive up/down `Staircase` — asymmetric steps (`step_down=8.0` dB,
`step_up=4.0` dB, 2:1 ratio, NOT the clinical 2-down-1-up rule), 5 reversals needed, bounds -90
to -10 dBFS, halving step size after the first reversal for finer convergence. Threshold = mean
of the last 4 reversal levels. Each new point's staircase is **seeded** +8 dB above the known
neighbours — from the log-frequency INTERPOLATION when two measured neighbours bracket the point
(every detail point after the baseline half, and most re-tests; persistent-state thresholds count
as neighbours, this-run results take precedence, and a re-test never seeds from its own old
value), else the nearest single neighbour; cold-start -30 dBFS only with nothing measured at all.
**Ceiling give-up** (`ceiling_give_up=2`, Test A only — Test B's symmetric SNR staircase keeps
the old pinned-reversal behavior): two consecutive "not heard" at the -10 dBFS ceiling ends the
point immediately, recording the ceiling as an "at or beyond" threshold. That ear's queued points
**more extreme than the give-up frequency** (a per-direction frequency boundary, `_give_up_freq`)
become **ceiling probes** — presented with their staircase started AT the ceiling instead of
being auto-skipped (user decision 2026-07-13, replacing both the original whole-direction abort
and the interim beyond-boundary skip): a truly inaudible probe resolves in exactly two clicks via
the same give-up rule, while a hearable one falls into a normal descending search — the case the
skip got wrong, since the test measures ears THROUGH hardware and headphone response nulls are
common in the 8-12 kHz region (give up at a hardware null at 8 kHz ⇒ a genuinely hearable 12 kHz
still gets measured, at worst ~8 wasted clicks total for a truly HF-deaf listener). Points closer
in than the boundary are unaffected. The one remaining hard-skip: can't hear the 1 kHz anchor at
max ⇒ the ear's whole remaining sweep records None (`_anchor_gave_up`), since normalise() can't
anchor its curve and no measurement there has a consumer. Ears are interleaved (L,R,L,R,...) per frequency to reduce fatigue bias; boundaries are
per-ear. Tone: 10ms-fade calibrated sine, played 3x at 0.2s each, 0.15s apart, auto-played on
test start/after every answer/on resume (300 ms delay) — playback is **non-blocking**
(2026-07-13: `hearing_test.start_tone()` + a 30 ms QTimer poll of `engine.is_playing` sequences
the three repeats; the old `present_tone()` blocking loop froze the event loop ~1 s per play), a
painted **"TONE" light** (`_PlayLight`) is lit exactly while a tone is sounding, and the test
page shows a per-point response trail ("v" heard / "^" not heard) + a "Narrowing: n/5 reversals"
convergence meter (`Staircase.reversals`/`reversals_needed`, added public) plus a **live chart**
that gains each point the moment it locks in (completed thresholds only — the current tone's
level is deliberately never shown, since seeing it invites answering by expectation instead of by
ear). Equalizer APO is fully bypassed while the prep/test/results pages show (`BypassGuard`,
restores prior bypass state on any exit); the dashboard itself is silent and unbypassed.

**Gave-up points are never plotted** (`_plottable_curve()`, both charts). A give-up stores the
CEILING level as its threshold — "at or beyond the loudest we can present", not a reading — so
normalised against a typical 1 kHz anchor it lands near **+48 dB** and squashes every genuine
point into the bottom of the chart. Which points gave up is carried explicitly by
`FrequencyTest.gave_up_pairs` (added 2026-07-13) rather than re-inferred from the threshold value,
because a ceiling reading alone cannot distinguish "couldn't hear it at all" from "only just heard
it at the maximum". They stay visible, honestly, as `limit` chips on the dashboard. Both charts
also set **explicit x-ticks at the tested frequencies** — pyqtgraph's log-scale picker otherwise
lands on values like `3.16228k` (10^0.5), and the formatter rendering them (`eq_graph.
_format_freq`) belongs to the main EQ graph, not this dialog. Dashboard chips deliberately use the
DEFAULT (bordered) `QPushButton` look rather than the borderless `ghost` class the surrounding nav
buttons use: clicking a chip to re-test that point is the entire feature, and as flat text a chip
reads as a read-only label nobody would think to click.

Output: `{"left": {freq: relative_dB}, "right": {...}, "imbalance": {freq: left_raw - right_raw}}`
— each ear's values normalized relative to its own 1000 Hz reading; imbalance computed from RAW
(uncalibrated) readings. `frequency_flags()` flags ±6 dB deviations as "worth a second look"
(explicitly not a clinical threshold). **Does NOT feed into EQ generation automatically.** The
only output path is `export_text()` — a plain-text file ending with an "Instruction for an AI"
block asking a human/external AI to *manually* author a compensation profile (capped at +6 dB
single-band boost, explicitly told not to attempt correcting for absolute hearing loss). The
working state is the live source (dashboard chips, live correction, export — export/results now
cover the WHOLE working calibration, not just the pass that finished); completing a pass also
snapshots the state as a timestamped JSON run for the overlay-comparison combo (10-run
retention). **No code path touches `ProfileConfig` from a hearing-test result** (listener
correction stays render-time composition, §15).

**12.4 Noise Screening ("Test B", `digit_screening.py`).** **Fully implemented and functional,
deliberately hidden from the picker UI, and — as of 2026-07-09 — permanently abandoned by user
decision, not just paused pending a voice fix.** Originally hidden 2026-07-07 "for now" (its data
doesn't feed anything the Frequency Test doesn't already cover, and the digit-clip TTS voice was
judged "extremely artificial"); the Rumble campaign's Session 22 ("re-enable once the TTS voice is
redone") was struck 2026-07-09 rather than executed — see the campaign plan file. Code, tests, and
assets are left in place (untouched, not deleted) per project convention, but no further work is
planned and the button is not intended to be re-added. The button is built and wired but simply
never added to the visible layout — reachable only via test code.

What it does: digit-triplet-in-noise speech-reception-threshold screening. Random 3-digit triplet
(no adjacent-equal digits) mixed with calibrated pink noise; symmetric adaptive SNR staircase
(`step_down=2.0`, `step_up=2.0` dB, 8 reversals, floor -15 / ceil +10 dB SNR); all 3 digits must
match in order for "correct." Output: **SRT** (Speech Reception Threshold) in dB SNR — the
50%-correct point. Classification uses `REFERRAL_SNR_DB = -5.55`, derived from cited literature
(Smits & Houtgast 2005/2006; UK Biobank Dawes et al. 2013) with two explicitly flagged limitations
(paywalled primary sources not independently re-verified; a different synthesized-voice/uncalibrated
playback chain than the validated literature). Output saved as JSON/exportable text, same
"informational only" framing — but with **no frequency table and no mention of EQ profiles/AI
authorship**; this test's data is not meant to drive EQ generation at all.

**12.5 ABX testing tool.** Blind statistical validation of whether a listener can actually hear a
difference between saved profiles (and/or "Flat (no EQ)") — not gear/measurement comparison. The
dialog randomly draws **up to 5 candidates** from the saved-profile pool plus the synthetic Flat
entry (the listener never picks them, so there is nothing to unconsciously bias toward).
`level_match()` gain-matches all candidates down to the quietest one first (reuses
`eq_math.combined_response`, power-domain/linear averaging across log-spaced frequencies ≈
equal-energy-per-octave, tolerance 0.1 dB; reduction-only so matching can't clip) since a louder
signal is reliably judged "better" regardless of shape. `ABXSession` is **open-ended** — there is
no trial-count field; every answer is one trial and the listener stops via "Show Results". Each
trial the listener hears any candidate (anonymous lettered buttons A–E — which profile hides
behind which letter is never shown up front) and the hidden X, answers via a "which profile is X"
dropdown of the candidate profile names (alphabetical, deliberately not button order, which would
betray the letter mapping), and is told **immediately** whether they were right and what X was
(X is re-rolled each trial). Exact one-sided
binomial p-value at the **1-in-N** chance rate; verdict thresholds p<0.01 = "reliably hear a
difference," p<0.05 = "probably," else "consistent with guessing." A context manager guarantees
the real active profile is restored on any exit path, including exceptions. The dialog refuses to
open while the editor has unsaved changes.

**12.6 Gear Simulation / AutoEQ merge.** Given a source gear (what the user owns, possibly
merged/averaged from several measurements) and a target gear (desired sound), computes
`delta_curve(target, source)` — point-for-point dB difference on a reconciled common log-frequency
grid — and saves it as a new stereo profile. Preamp for the new profile uses `compute_preamp()`,
which as of Rumble 1.3 (Session 2) delegates to the same escalating-margin
`preamp_calc.calculate_auto_preamp()` the main editor's Auto Preamp button uses (see §5.2) rather
than the flat-0.5-dB formula this section previously described. Multi-measurement merging (its own "Merge" tab)
averages dB values across curves on a common grid — exact for APO's piecewise-linear-in-dB
GraphicEQ model. Resulting profiles are explicitly labeled "Provisional... Approximate — verify by
ear."

**12.7 `ir_analysis.py` current scope.** **Read-only impulse-response analysis for
preview/math purposes — does NOT generate convolution profiles.** The `Convolution:` file path is
set manually by the user via file browser; RUMBLE then reads the WAV (custom RIFF parser supporting
PCM 16/24/32-bit and IEEE float, chosen because stdlib `wave` rejects the common IEEE-float export
format), computes an FFT-magnitude response with 1/6-octave power smoothing, caps at 10 seconds,
and feeds the result into both the graph overlay and the preamp/headroom peak calculation. Warns
(does not block) on a sample-rate mismatch between the IR file and the device's effective rate,
since "Equalizer APO does not resample convolution IRs at load time." **No RUMBLE code measures a
room via sweep/mic capture and derives a corrective IR** — this is the concrete gap behind
"room correction is not built" in §11.1.

**12.8 `auto_switch.py` automatic profile switching.** A Qt-free rule engine
(`RuleEngine`/`Rule{trigger: "app"|"device"|"time"}`) plus a pycaw session poller
(`SessionWatcher`) plus a controller (`AutoSwitchController`) that calls
`ProfileManager.activate_profile()`. Priority = list order, first match wins. App-rule
debounce/hysteresis: a 2.0s continuous-play trigger before first firing, a 10.0s linger after the
app stops before releasing — explicitly designed to avoid "flapping" from brief pauses/notification
dings. The controller remembers a `baseline` (whatever was manually active before any rule fired)
and restores it once no rule holds; suspended while the editor is dirty or the feature is disabled.
See §0.7 — this is a RUMBLE-layer mechanism, distinct from (and not using) native APO conditionals.

**12.9 Other built-in tools found (catch-all).**
- **Live Spectrum Analyzer overlay** (`rumble/core/spectrum.py` + `spectrum_overlay.py` +
  `SpectrumController`) — real WASAPI loopback capture (`audio/capture.py`, auto-reconnecting),
  drawn as a translucent live trace behind the calculated response curve, with Peak Hold and
  "Output" vs. "Source (estimated)" modes (the latter subtracts the calculated response from the
  measured spectrum). Uses IEC 60268-10 Fast/Slow ballistics for smoothing. Drives a "measured
  clipping" indicator that overrides the theoretical headroom label when real sustained output
  peaks are detected — more authoritative than the calculated estimate. As of Rumble 3.7 (Session
  14), `spectrum.drift_correlation()`/`drift_expected()` generalize this same measured-vs-
  calculated comparison into a broader drift indicator (competing DSP, wrong device selected, a
  muted/bypassed EQ) surfaced as a distinct UI signal alongside the clipping-specific one.
- **Mic-sweep automatic time-alignment** (`rumble/core/time_alignment.py`, Rumble 4.9, Session 25)
  — plays an exponential (Farina) sine sweep through each physical channel in turn and measures its
  arrival time at a measurement microphone via GCC-PHAT cross-correlation (Knapp & Carter, 1976),
  robust against room-reflection false peaks unlike a raw correlation. The correlation/offset math
  (`detect_offset_samples`, `measure_channel_delays`, `build_channel_sweep`) is pure numpy and
  fully unit-tested with synthetic signals; the real play-and-record orchestration
  (`run_measurement`) is a thin DI-seamed shell over that core, not itself covered by the CI gate
  since it needs real mic hardware. A "Measure" button next to the per-channel Delay field in
  `editor_panel.py` drives it (requires unlinked channels with standard single-channel names);
  results are rebased so the latest-arriving channel gets 0 ms and earlier channels get positive
  delay, since APO's `Delay:` line can only add delay, never subtract it (fixed 2026-07-13 — it
  originally rebased to the earliest arrival, the sign bug documented in 23.3).
- **`rumble/core/audio/metering.py`** — RMS/peak dBFS metering and Hann-windowed rFFT spectrum
  magnitude helpers, the numeric backbone under the spectrum analyzer and tone calibration.
- **`rumble/core/audio/tones.py`** — calibrated sine-tone and pink-noise generation with
  raised-cosine fades, shared by both hearing tests.
- **`rumble/core/audio/engine.py`** (`ToneEngine`) — playback engine used by both hearing tests.
- **`rumble/core/eq_target.py`** — the speaker-correction math library itself (tilt targets, octave
  smoothing, boost-capping, band-limiting, directivity-aware boost damping calibrated against real
  Spinorama data per an internal empirical audit) — the engine behind Spinorama's `fetch_curve`,
  not a standalone user tool.
- **Backup/drift snapshotting** — `ProfileManager` writes timestamped
  `config.txt.rumble_drift-<ts>` snapshots whenever `config.txt` is externally clobbered or
  co-written by a foreign tool; backend shipped, no restore-picker UI yet (open backlog item).
- **Tag hierarchy / custom themes / autostart** — organizational/cosmetic, not EQ/calibration
  tools, out of scope here but present in `rumble/core/`.

---

## §13 — Corrections to prior assumptions / stale project notes

This research surfaced several places where existing project memory or prior documentation does
not match the current code. These should be treated as authoritative corrections:

1. **Measurement-provider gating** (§6.5) — prior note "AutoEQ/REW ship, Squiglink/Spinorama
   opt-in, Crinacle never" is **wrong on every clause except AutoEQ shipping**. Correct: only
   AutoEQ and Spinorama exist as providers; both are live-fetch (not bundled); both are
   enabled-by-default with **no opt-in gate at all**; REW, Squiglink, and Crinacle providers do
   not exist in code.
2. **Copy/Stage/If-ElseIf are fully implemented**, not merely parsed-and-preserved as might be
   assumed from a "not yet used" framing — all four have dedicated UI editors (§11.2).
   **RESOLVED 2026-07-09** (Rumble 4.4, Session 21): *automatic generation* of `If`/`ElseIf` for
   device-aware switching, previously the one unbuilt use of these commands, now also exists as an
   explicit export action (`auto_switch.generate_native_conditional()`) for device-trigger rules.
3. **The hearing test does not feed into EQ generation** — it only exports a plain-text file for
   manual/AI-assisted profile authoring (§0.6, §12.3). This remains true for *stored* profile
   generation. **UPDATED 2026-07-09**: it now separately feeds a render-time-only correction layer
   ("Listener Slots", §15) that composes a weak-ear-only curve on top of whatever profile is
   playing, without ever writing to `ProfileConfig`/`ChannelSection` storage — so "the hearing test
   automatically adjusts a profile" is still incorrect, but "the hearing test can now automatically
   adjust what you *hear*" is correct as of this feature. The 2026-07-06 multi-listener spec
   referenced here as PARKED was superseded by the 2026-07-09 design that actually shipped.
4. **Noise Screening ("Test B") is functional, not stubbed-out, but permanently abandoned** — it's
   a complete, tested feature, technically one line away from being re-wired into the visible
   layout, but the user decided 2026-07-09 to leave it hidden for good rather than fix its TTS
   voice and re-enable it (§12.4). Don't describe it as "pending" or "queued" work.
5. **"Simple mode vs. advanced mode" is not a complexity-hiding toggle** — it's a fixed 3-tab
   layout where nothing is ever hidden (§10.3). Do not describe RUMBLE as having a beginner/expert
   mode switch.
6. **GEQ↔PEQ conversion does not exist** (§4.2) — was accurate as of this section's writing.
   **RESOLVED 2026-07-09** (Rumble 4.3, Session 19): a one-way GEQ-to-PEQ conversion (grid-spacing-
   derived per-band Q, Newton-Raphson overlap correction) now exists; GEQ and PEQ still coexist
   independently and are still summed, never automatically reconciled — the conversion is an
   explicit opt-in user action, not implicit merging.
7. **Two inconsistent preamp-margin formulas coexist** (§5.2, §7.3-1) — this was new information at
   the time, not previously documented anywhere found in this pass, and was a genuine consistency
   bug. **RESOLVED 2026-07-09** (Rumble 1.3, Session 2): `autoeq_merge.compute_preamp()` now
   delegates to `preamp_calc.calculate_auto_preamp()`, see §5.2.
8. **No Nyquist/sample-rate Fc validation exists** (§7.1, §7.3-2) — worth flagging since the
   project's own "derive constants, don't guess" convention would normally expect such a bound to
   exist and be documented; it appears to be a genuine, unaddressed gap. **RESOLVED 2026-07-09**:
   live Nyquist validation now exists, see Rumble campaign Session 1.

---

## §14 — Upstream Equalizer APO semantics: `LoudnessCorrection:` and `VSTPlugin:`

Confirmed 2026-07-09 (Rumble campaign Session 6) directly from Equalizer APO's own source, not the
wiki. **Correction to the plan that spawned this research:** there is no `jthedering/EqualizerAPO`
GitHub repo — upstream's canonical source lives on SourceForge
(`sourceforge.net/projects/equalizerapo/`); the GitHub read-only mirror is
`github.com/mirror/equalizerapo`, read at commit `53d885f7f1a097b457e17a5206b7d60f647877a8`
(2024-09-27, "Added: Support for ARM64 architecture"). SourceForge's own Configuration Reference
wiki page (`sourceforge.net/p/equalizerapo/wiki/Configuration reference/`) does **not** document
either command at all — confirming the plan's own note that the wiki was "found insufficient."

**14.1 `LoudnessCorrection:` — confirmed NOT an ISO 226 equal-loudness-contour implementation.**

It is a **heuristic two-shelf-filter volume-compensation curve**, hand-tuned by upstream, not a
standard loudness-contour computation. This confirms and gives full source-level detail for the
existing §11.1 note ("implemented only as a thin editor... not a RUMBLE-authored ISO 226
equal-loudness-contour computation") — the upstream side isn't ISO 226 either.

- **Files:** `filters/loudnessCorrection/LoudnessCorrectionFilter.{cpp,h}`,
  `LoudnessCorrectionFilterFactory.cpp`, `VolumeController.cpp`.
- **Config-line syntax** (parsed via regex in `FilterParameters`, `LoudnessCorrectionFilter.h`):
  `LoudnessCorrection: State (0|1) ReferenceLevel <int dB> ReferenceOffset <int dB> [Attenuation <0.0-1.0>]`.
  `State` and `ReferenceLevel`/`ReferenceOffset` are required; `Attenuation` is optional and
  defaults to `1.0` if omitted or unparseable. The constructor additionally hard-clamps
  `attenuation` to `[0.0, 1.0]` regardless of what was parsed.
- **Volume source:** `VolumeController` reads the **WASAPI endpoint (master) volume** via
  `IAudioEndpointVolume::GetMasterVolumeLevel` — the system output device's master volume in dB,
  not a per-session or per-app volume.
- **Core algorithm** (`getLShelfParamter`/`getHShelfParamter`, both driven by one shared
  `volDiff = referenceLevel - referenceOffset - volume`):
  - **Low shelf:** fixed at **75 Hz, Q=0.52**. If `volDiff > 0` (volume below reference):
    `gain = volDiff * 0.55 / (1 - 0.55) * attenuation`, and an equal-and-opposite `preAmp = -gain`
    is applied (so the low-shelf boost is preamp-compensated, not just added on top). If
    `volDiff < 0` (volume above reference): `gain = volDiff * 0.55 * exp(volDiff / 90.0) *
    attenuation` (exponentially tapered cut), and `preAmp = 0`.
  - **High shelf:** fixed at **10,000 Hz, Q=0.9**. Uses `volume + preAmp` (i.e. the low shelf's
    preamp adjustment feeds into the high-shelf's effective volume) rather than raw `volume`. If
    `volDiff > 0`: `gain = volDiff * 0.225 * exp(-volDiff / 100.0) * attenuation`. If
    `volDiff < 0`: `gain = volDiff * 0.175 * exp(volDiff / 80.0) * attenuation`.
  - **Neutral/bypass shortcut:** if both shelf gain magnitudes are `< 0.2` dB, the filter marks
    itself `_neutral` and passes audio through unmodified ("best quality" — avoids unnecessary
    filtering when the correction would be inaudible).
  - All four gain formulas are asymmetric between boost and cut (different constants and different
    exponential-decay time constants for `volDiff > 0` vs. `< 0`) — this is a deliberately
    hand-tuned curve shape, not a symmetric approximation, and not derived from any cited
    psychoacoustic standard in the source itself (no comment references ISO 226, Fletcher-Munson,
    or Robinson-Dadson).
- **No named-standard citation anywhere in upstream's own source** for these specific constants
  (75 Hz/10 kHz corner frequencies, the 0.55/0.225/0.175 gain multipliers, the 90/100/80 ms
  time-constant-like divisors) — they appear to be Jonas Thedering's own empirically-tuned values,
  not a documented derivation. RUMBLE should **not** describe its own `LoudnessEditor` UI as
  exposing "ISO 226 loudness compensation" — it is exposing upstream's proprietary heuristic
  curve, full stop. If RUMBLE ever wants a real ISO-226-based layer (Tier 3.5 in the Rumble
  campaign), that is necessarily new RUMBLE-side math layered on top of (or instead of) this
  command, not a configuration of it.

**14.2 `VSTPlugin:` — confirmed syntax and scope.**

- **Files:** `filters/VSTPluginFilter.{cpp,h}`, `filters/VSTPluginFilterFactory.cpp`,
  `helpers/VSTPluginLibrary.{cpp,h}`.
- **Config-line syntax:** `VSTPlugin: Library "<path.dll>" [ChunkData "<base64>"] [<ParamName>
  <float>]...` — parsed as space-separated key/value pairs (quote-aware split) processed in order;
  any key that isn't literally `Library` or `ChunkData` is treated as a named plugin parameter and
  its value coerced to `float` via `wcstof`. `Library` is required (a relative path resolves
  against the default VST plugin directory via `VSTPluginLibrary::getDefaultPluginPath()`, an
  absolute path is used as-is); `ChunkData` and individual named parameters are optional and
  mutually exclusive in practice (a plugin either uses opaque base64 chunk state or named
  parameters, not both).
- **Scope:** loaded per filter-chain position exactly like any other command (`Filter:`,
  `Copy:`, etc.) — i.e. it can appear per-channel/per-stage/inside an `If` block the same as any
  other command, there is no separate global-only scope. It is **not** a device-global plugin
  host; it's an in-line DSP node in the same processing graph as RUMBLE's other filters.
- **Format support:** VST2 only in practice — the loader (`VSTPluginLibrary`) speaks the classic
  `AEffect`/`vstPluginMain` C ABI. Native VST3 is not supported; the ARM64 commit message found
  during this research (`53d885f`, 2024-09-27) explicitly notes "limited VST support as native
  ARM64 VST plugins are rare," consistent with VST2's aging 32/64-bit-only plugin ecosystem.
- **Bitness/architecture constraint:** the plugin DLL's architecture must match the running
  EqualizerAPO process (32-bit APO cannot load a 64-bit VST DLL or vice versa) —
  `VSTPluginLibrary::initialize()` returns a distinct `WRONG_ARCHITECTURE` error code specifically
  for this case, surfaced via `LogF("...has wrong architecture, must be %d-bit", bitDepth)`.
- **Failure modes surfaced to the user via log, not a config error:** file not found, library
  failed to load, required VST entry points missing, wrong architecture — all four produce a
  `LogF` message (visible in APO's log file) rather than a hard config-parse failure; a bad
  `VSTPlugin:` line is silently skipped (no filter is added) rather than blocking the rest of the
  config from loading.

**Follow-up (not actioned this session, per Session 6's own scope: confirm-and-document only):**
RUMBLE's `LoudnessEditor` (`rumble/ui/advanced_panel.py` ~353-533) currently presents
`ReferenceLevel`/`ReferenceOffset`/`Attenuation` as opaque numeric fields. Given §14.1's confirmed
algorithm, a future UX pass could show the user what the resulting shelf gains actually look like
(e.g. a live preview curve at the current WASAPI volume) rather than three bare number fields —
flagged for a future session, not implemented here.

## §15 — Listener Slots (per-listener render-time correction)

Shipped 2026-07-09 (Rumble campaign Sessions 8, 9.1-9.8). Implements
`docs/superpowers/specs/2026-07-09-listener-correction-design.md`; full implementation record in
`docs/superpowers/plans/2026-07-09-listener-correction-implementation.md`. This is the feature that
closes the loop §0.6/§13 item 3 describe as PARKED — but strictly as a render-time-only additive
correction, never as automatic EQ generation from Test A data (see §15.3).

**15.1 Data model.**
- 4 fixed, nameable "Listener Slots" (never called "Profiles" — distinct from `ProfileConfig`
  profiles). `SLOT_IDS = (1, 2, 3, 4)` in `rumble/core/listener_slots.py`.
- Each slot stores its own history of Test A ("Ear Calibration") runs under
  `data_dir/listeners/<slot_id>/hearing/frequency/*.json` — the same JSON shape
  `hearing_test.save_results()` already produces (`norm`/`raw` dicts), just rooted per-slot.
- 10-run retention (`MAX_SAVED_RUNS`): saving an 11th run for a slot deletes the oldest outright —
  normal end-user runtime retention, explicitly exempted from this repo's dev-side "never delete,
  archive instead" convention (design spec §3.2).
- `effective_imbalance_curve(data_dir, slot_id)` is the single view every consumer reads.
  2026-07-13 (single-test dashboard redesign): a slot with a working `calibration_state.json`
  reads its imbalance from that state directly — "what the dashboard list shows is what corrects",
  so a re-tested point takes FULL effect immediately instead of being diluted through a history
  average. Legacy slots (saved runs only, dashboard never used) keep the original behavior:
  the average of every saved run's imbalance curve via `autoeq_merge.average_curves()` (design
  spec §3.3). Empty when a slot has nothing measured.
- The working calibration state (`calibration_state.json` under `<slot>/hearing/`,
  `load_calibration_state`/`save_calibration_state`/`record_calibration_point`/`calibration_raw`/
  `clear_calibration_state`): per-point `{threshold, gave_up, measured_utc}` plus the volume
  reference it was measured at — the single progressive test's cross-session store (§12.3). A
  missing file loads seeded (in memory only) from the slot's latest saved run, so pre-dashboard
  listeners keep their calibration; `clear_calibration_state` deletes only the state file, never
  run history.
- **A gave-up point is a CENSORED observation, and never reaches the math (fixed 2026-07-13).** When
  a staircase hits the ceiling twice without a response (`Staircase.gave_up`), the point is recorded
  with `gave_up: True` and a `threshold` equal to **the ceiling level the ear failed to hear** — a
  LOWER BOUND on the real threshold ("at least this loud, possibly far beyond"), not a measurement.
  Passing that number to `normalise()` produced a huge bogus imbalance (a ceiling of -6 dB against a
  typical -50 dB opposite ear reads as **+44 dB**), which the correction then acted on: it spent its
  capped boost, and made every other frequency pay for that boost through the global anti-clipping
  preamp, **pushing energy into a band the ear cannot hear at any testable level.** Backwards, and
  for nothing. `calibration_raw(state, measured_only=True)` therefore maps a gave-up point to `None`
  (the value `normalise()` already treats as "no data"), and **every math consumer passes it**:
  `effective_imbalance_curve()` (the correction), the dialog's chart curves, outlier
  `frequency_flags()`, the "measured" counter, and staircase seeding — seeding a re-test from the
  ceiling it already failed would open the retry on the loudest tone in the test and bias it straight
  back into another give-up. The key stays in the dict, so "tested" stays distinguishable from "never
  tested" (`baseline_complete()` keys off presence, not value, and is unaffected).
  What must still REPORT them gets them from `no_response_freqs(state)`: the dashboard's "limit"
  chips, and `hearing_test.export_text(..., no_response=...)`, which gives them their own section —
  a dead band is the single most important finding on that page, and silently dropping it (or
  mislabelling it "skipped", as though the listener had declined the test) would be the worse bug.
- Custom display names live in `data_dir/listeners/names.json`
  (`load_slot_names`/`save_slot_names`); a slot with no saved name falls back to `"Listener N"`.
- Legacy migration: any pre-existing global Test A history
  (`data_dir/hearing/frequency/`, from before this feature existed) is copied — not moved — into
  Slot 1 on first boot after upgrade, gated by a one-time `legacy_hearing_migrated` settings flag so
  it never re-runs after a user edits/deletes Slot 1's own history.

**15.2 Correction math (weak-ear-only, +6dB-capped).**
- `rumble/core/listener_correction.py`, `CORRECTION_MAX_BOOST_DB = 6.0` — reuses the
  already-documented +6dB single-boost cap from `hearing_test.py`'s own export instruction text and
  the auto-correction pipeline's own boost cap (`RUMBLE_ACTION_GUIDE.md`), not a newly guessed
  number.
- `derive_correction(imbalance_curve)` turns an averaged imbalance curve (`left_raw - right_raw` per
  `hearing_test.normalise()`'s convention) into an `EarCorrection(left, right, clamp)`: positive
  imbalance ⇒ left measured weaker ⇒ LEFT gets boosted; negative ⇒ RIGHT gets boosted. The stronger
  ear is **never cut** — enforced via `eq_target.clamp_gain(..., max_cut_db=0.0)`, an invariant of
  the clamp call itself, not just an assumption about how the input curves are built.
- `EarCorrection.is_flat` is true when both ears' correction curves are all-zero (including when the
  active slot has no Test A data) — the fast-path/no-op case checked before any composition work.

**15.3 Render-time-only composition — the storage boundary.**
- `compose_profile()`/`compose_channel_sections()` in `listener_correction.py` **never mutate**
  `ChannelSection`/`ProfileConfig` storage. They return a new, composed `ProfileConfig` only, for
  the two real render choke points — correction is merged into channel sections' `GraphicEQ` via
  `autoeq_merge.sum_curves()`, and the profile's global preamp is recomputed as the minimum
  auto-preamp across the composed sections (`preamp_calc.calculate_auto_preamp()`) — the same
  pattern `editor_panel.py` already used for multi-section preamp, generalized to the render path.
- The two choke points (both in `ProfileManager`, `rumble/core/profile_manager.py`), routed through
  one shared private helper `_apply_listener_correction()`:
  - `preview_config()` — live editor preview **and** ABX playback (`ABXRunner.present()` calls
    through this).
  - `_write_active_pointer()` — canonical profile activation (including on profile save). This was
    a genuine architectural finding surfaced by this feature's design work: activating a profile
    does **not** normally re-render it — it just points an `Include:` line at an already-rendered
    static file — so listener correction needed new composition logic inserted here too, not merely
    "wrapped" around an existing render call. When a correction is active, this choke point writes a
    new scratch file (`rumble_listener_active.txt`, mirroring `preview_config()`'s own
    `rumble_preview.txt` pattern) instead of pointing directly at the profile's own static file.
- A flat (no-op) correction returns the input `ProfileConfig` itself (identity, zero-copy) — both
  choke points behave exactly as they did before this feature existed whenever no correction is
  active.

**15.4 Session-control switching semantics.**
- `ProfileManager.active_listener_slot` is session-only state, the same category as Pre-Amp/Auto
  (§0.6/§0.7 lineage — see `project_preamp_session_controls` project memory) — it resets to
  `settings["default_listener_slot"]` (`DEFAULT_SETTINGS`, default `1`) on every launch. A
  mid-session switch via `ProfileManager.set_active_listener_slot()` is deliberately **not sticky**
  across restarts.
- `set_active_listener_slot()` fires `on_listener_changed` callbacks and immediately re-renders the
  active pointer (when not currently previewing) so a slot switch takes effect without requiring a
  save/re-activate.
- `get_active_correction()` caches the derived `EarCorrection` per active slot;
  `invalidate_listener_correction_cache()` must be called after saving new Test A data for the
  active slot so the next render picks up the fresh average.

**15.5 UI surfaces (thin consumers only).**
- Header dropdown and tray-menu mirror for switching the active slot.
- Settings → Listeners page: 4 name fields, a "Default Listener Slot" combo, an "Apply the active
  listener's ear correction" checkbox (`listener_correction_enabled`, default True — added
  2026-07-11 to give the slot-derived correction the same explicit off-switch the ISO 226 layer
  has; `get_active_correction()` returns `NO_CORRECTION` while off, slot data/cache untouched, so
  a listener can A/B "corrected vs not" without re-measuring), and the `warn_unsaved_changes`
  checkbox (grouped here per design spec §7's own note, not because the warn toggle is
  conceptually a listener setting). Saving Settings now calls
  `ProfileManager.refresh_render_layers()` so render-layer toggles (this one, the ISO 226
  enable/phon) take effect on the live chain immediately — previously they waited for the next
  activation.
- `EQGraph` shows two extra `pg.PlotDataItem` curves ("L effective"/"R effective") only when the
  active correction is non-flat, reusing the main curve's accent color with a distinct dash-dot pen
  — no new per-theme color tokens were added across the 10 themes.

**15.6 Backup/restore coverage.**
- `data_dir/listeners/` requires zero special-casing in `ProfileManager.backup_library()`/
  `restore_library()` — both already walk the whole `data_dir` recursively via
  `_iter_backup_files()`'s `rglob("*")` with no include-list. Verified (not just assumed) by
  `tests/selftest_listener_backup.py` (Rumble Session 9.8).

---

## §16 — ISO 226 loudness-compensation layer (volume-linked, Rumble 3.5)

Built 2026-07-09 (Session 16), gated on Session 6 (§14.1 — confirmed upstream's own
`LoudnessCorrection:` is a hand-tuned heuristic, *not* ISO 226) and Sessions 9.1-9.8 (§15's
"layer, don't merge" additive-correction mechanism this item explicitly reuses).

**16.1 The real standard, not an approximation.** `rumble/core/iso226.py` implements ISO 226:2003
Table 1's own SPL-from-phon formula and its 29-frequency coefficient table (`alpha_f`, `L_U`,
`T_f`, 20 Hz-12500 Hz) verbatim — cross-checked against two independent open-source reproductions
of the standard's own published constants (Jeff Tackett's `iso226.m`, MATLAB File Exchange #7028;
`github.com/IoSR-Surrey/MatlabToolbox`'s reimplementation), both bit-identical, and numerically
verified in `tests/selftest_iso226.py` against the formula's own defining identity
(`equal_loudness_spl(1000 Hz, phon) == phon` for any phon, since ISO 226:2003 defines the phon
scale itself at 1 kHz). Valid range 0-90 phon per both sources' documented usage note; inputs are
clamped, not extrapolated.

**16.2 What the compensation curve is.** `iso226.loudness_compensation_curve(reference_phon,
current_phon)` returns, at each of the standard's 29 frequencies, the dB adjustment that keeps a
signal's perceived spectral balance the same at `current_phon` as it was at `reference_phon` —
derived directly from the equal-loudness contour gap between the two levels (not a new heuristic
shape; the physics is entirely the cited standard's own tabulated data). Zero by construction at
1 kHz; positive (boost) at bass/treble when `current_phon < reference_phon` (listening quieter than
the reference), matching the well-known perceptual effect the contours themselves describe.

**16.3 Volume-linking mechanism.** `rumble/core/master_volume.py` reads the live WASAPI master
(endpoint) volume via `IAudioEndpointVolume::GetMasterVolumeLevel` — the *same* API §14.1 confirmed
upstream's own `LoudnessCorrection:` filter uses (`VolumeController.cpp`), deferred-imported and
fail-safe (`None` on any COM failure) exactly like `device_format.py`'s existing convention.
`ProfileManager.calibrate_loudness_reference()` captures the current reading as
`settings["iso226_reference_volume_db"]` — "this is my normal listening volume, matching
`settings["iso226_reference_phon"]` (default 80, one of the standard's own commonly-illustrated
example contours)". `loudness_layer.derive_loudness_correction()` then models
`current_phon = reference_phon + (current_volume_db - reference_volume_db)` — a documented modeling
assumption (1 dB of WASAPI attenuation ≈ 1 phon of perceived loudness change), not an invented
numeric constant, and the same practical approximation volume-linked "loudness" controls generally
use since RUMBLE cannot know absolute acoustic SPL without a calibration measurement it doesn't have.
Flat whenever the reference hasn't been calibrated, the layer is disabled
(`iso226_loudness_enabled`, default `False`), or a live volume read fails.

**16.3a Conversation-anchored calibration (2026-07-11).** A second, better-anchored calibration
path: the Ear Calibration intro page's volume step (listener adjusts real-world volume until the
committed -20 dBFS RMS speech clip sounds like normal conversation) now offers a default-on
checkbox ("Also use this volume as my loudness-compensation baseline"). Starting the run then
calls `ProfileManager.calibrate_loudness_reference_from_conversation()`: conversational speech ≈
60 dB SPL ≈ **60 phon** (`loudness_layer.CONVERSATION_PHON`; phon = dB SPL at 1 kHz by
definition), so the layer **extrapolates** the mix-reference anchor as
`conversation_volume + (reference_phon − 60)` dB — +20 dB with the default 80-phon reference —
via `loudness_layer.reference_volume_from_conversation()`, keeping `iso226_reference_phon`'s
meaning intact rather than re-defining it per calibration path. The plain Settings button
(16.3) still exists and captures the current volume AT `reference_phon` directly — correct only
when the user is genuinely listening at that loud level when they click it.

**16.3b Volume-knob tracking (2026-07-11).** Renders only happen at the choke points, so a
volume change while a profile just sat active used to keep the stale curve until the next render.
`rumble/ui/loudness_volume_adapter.py` (`LoudnessVolumeAdapter`, started unconditionally by
MainWindow like the format adapter) closes this: a GUI-thread QTimer at auto_switch_adapter's
established 1000 ms cadence polls the master volume and calls the new
`ProfileManager.refresh_render_layers()` when it moved ≥1.0 dB (the model's own 1 dB ≈ 1 phon
granularity) since the last applied render. `refresh_render_layers()` refuses while previewing
(never clobbers a live-tuning preview or ABX trial — the adapter retries a refused refresh on a
later tick), while bypassed, or with no active profile; ticks early-out to two dict reads while
the layer is disabled/uncalibrated.

**16.4 Independent of Listener Slots, same render mechanism.** `rumble/core/loudness_layer.py`
mirrors `listener_correction.py`'s shape (`LoudnessCorrection` dataclass, `is_flat`,
`compose_profile()` reusing `autoeq_merge.sum_curves()`/`preamp_calc.calculate_auto_preamp()`) but
is symmetric across every channel section (no per-ear split — loudness perception isn't ear-specific)
and reads live volume on every call rather than caching (the entire point is staying in sync with
the volume knob). `ProfileManager._apply_render_layers()` composes listener correction then the
loudness layer, in that order, at both real render choke points (`preview_config()`,
`_write_active_pointer()`) — both are additive dB curves, so order doesn't change the physical
result. Boost/cut clamped to the same 6.0/12.0 dB bound `eq_target.clamp_gain()` already uses by
default elsewhere in the codebase (§7.1), reported via the same `ClampSummary`.

**16.5 Settings UI.** Added to the existing Settings → Listeners page (not a new category): an
enable toggle, a reference-phon spinbox (0-90, matching `iso226.MIN_PHON`/`MAX_PHON`), and a
"Calibrate at Current Volume" button + status label. The calibrate action is an immediate,
persisted side effect (calls `ProfileManager.calibrate_loudness_reference()` directly, like the
existing Backup/Restore buttons) rather than a working-copy field gated behind the dialog's Save
button.

**16.6 Backup/restore coverage.** No new `data_dir` subfolder — the three new settings keys
(`iso226_loudness_enabled`, `iso226_reference_phon`, `iso226_reference_volume_db`) live in
`profiles.json` like every other setting, already covered by the existing whole-`data_dir` backup
walk (§15.6) with no special-casing needed.

## §17 — Equalizer APO virtual-channel `Copy:`/`Channel:` crossfeed plumbing (confirmed live, Session S6)

Confirmed 2026-07-12 (RUMBLE adaptive front-end campaign, Session S6 — a hardware spike, per the
roadmap's B2 "Validation spike FIRST" requirement) by actually listening on real hardware, not by
reading upstream source (contrast §14, which was a source-level investigation). Deliberately
empirical: the B2 design's claim that "APO creates virtual channels for unknown names" via `Copy:`
was a planning-time assumption, not yet verified against a real install.

**17.1 What was tested.** `tests/verify_crossfeed_live.py` (human-in-the-loop, excluded from the
gate) built and previewed this config on the real Equalizer APO install via
`ProfileManager.preview_config()`, wrapped in `ABXRunner` for guaranteed restore:

```
Copy: XL=R
Copy: XR=L

Channel: XL XR
Preamp: -6.0 dB
Delay: 0.3 ms
Filter 1: ON LP Fc 700.0 Hz Q 0.71

Copy: L=L+XL R=R+XR
```

Values are the Chu Moy classic crossfeed preset (-6 dB, ~700 Hz low-pass) plus a Linkwitz-style
~300 microsecond ITD delay, both cited in the B2 roadmap design section — not tuned for release,
only representative enough to make a leak clearly audible or clearly absent. LP filter Q uses
`rumble.core.crossover.BUTTERWORTH_Q` (the codebase's existing standard single-stage alignment for an
APO `LP` filter).

**Hardware:** default output device "Speakers (FIIO K13 R2R)", 96 kHz / 32-bit, 2 channels (plain
stereo headphones, nothing unusual about the signal chain — no surround receiver or multichannel
interface involved).

**17.2 Findings — all four checks confirmed working, after one methodology correction.**

- **(a) Does audio from the virtual channels mix back into L/R at all — YES.** A 300 Hz tone
  hard-panned to one channel produced an audible, quieter leak in the opposite channel, both
  directions (left-source leaking into right, and right-source leaking into left).
- **(b) Does the cross path obey the filter (and, by inference, the delay) — YES for the filter,
  not independently testable for the delay.** A 4000 Hz tone's leak was distinctly quieter/duller
  than the 300 Hz tone's leak — direct listening confirmation that the `Filter 1: ON LP Fc 700.0 Hz`
  line inside the `Channel: XL XR` section is actually acting on the cross path, not being ignored.
  The ~300 microsecond `Delay:` was **not** independently confirmed by ear — a delay that short is
  below the threshold of being perceived as a distinct echo in isolation (it only matters as part of
  authentic interaural timing, not as a audible standalone cue). Its acceptance rests on inference:
  APO did not silently drop or error on the `Channel: XL XR` block (the filter clearly took effect),
  so there is no observed reason to believe the `Delay:` line inside the same block was rejected
  while the `Filter:` line inside it was honored.
- **(c) Does APO accept `Channel:` sections for virtual (user-invented) channel names — YES,
  including a combined multi-name section (`Channel: XL XR` naming two virtual channels in one
  block, the same combined-section pattern already used for real channels elsewhere in this
  codebase, e.g. linked `Channel: L R`).** The filter/delay/gain inside that section measurably
  affected the audio, which is only possible if APO actually created and routed through the `XL`/`XR`
  virtual channels the preceding `Copy:` lines referenced.
- **(d) Channel-count/layout constraints — none observed.** Tested only on a plain 2-channel stereo
  device; the B2 roadmap's own decision (crossfeed is stereo-only, hidden/soft-disabled on
  non-stereo layouts) was not stress-tested against an actual multichannel device here — no such
  device was available in this session — but nothing about the virtual-channel mechanism itself
  appeared width-limited or channel-count-sensitive at the config level.

**17.3 Methodology correction, recorded for future live/manual test scripts.** The first full run
used 3-second test tones (matching `verify_audio_live.py`'s convention) and produced an apparent
**null result — no leak audible on any phase**, which briefly looked like a refutation of the whole
B2 design. A quick control test isolated the cause: `Copy: L=L+R` / `Copy: R=R+L` using only *real*
channel names (no virtual channels at all) also produced no perceptible effect at 3 seconds, but a
clearly correct unity-sum (equally loud in both ears) at 9 seconds — the tone duration itself, not
the virtual-channel mechanism, was too short for the user to confidently judge a quiet secondary
cue against a loud primary one. `verify_crossfeed_live.py`'s `TONE_DURATION_S` was raised from 3.0
to 9.0 as a result. **Lesson for any future manual A/B/leak-style listening script in this
codebase: default to longer tones (order 8-10s) for quiet/secondary-cue judgments, not the
1-4s durations that suffice for simple "is this audible at all" checks like `verify_audio_live.py`'s
tone-per-channel sanity test.**

**17.4 Data-model gap noted while building the test config (informational for S7, not a blocker).**
`apo_config.render_config_body()` always renders every `ProfileConfig.copy_lines` entry before every
`channel_sections` entry (its own docstring's canonical order) — there is no field for a `Copy:`
line that must render *after* a `Channel:` section, which the second `Copy: L=L+XL R=R+XR` line
needs. The test script works around this by placing that line in `extra_lines` (rendered last,
after all channel sections), which happens to land in the right position here only because there is
exactly one crossfeed-related channel section and nothing else needs `extra_lines`. This is not
robust for a general-purpose generator — S7 (B2-core: crossfeed generator + render layer) already
plans to "append rendered crossfeed text to the scratch file body" as raw text rather than going
through `ProfileConfig`'s structured fields at all (its own roadmap "Compose" note), which sidesteps
this gap entirely; this finding just confirms that decision is necessary, not optional — a
structured `ProfileConfig` round-trip of a full crossfeed block is not achievable with the model's
current fields.

**Conclusion: the B2 design's core assumption is CONFIRMED, not refuted.** S7/S8 are not blocked —
no Amendment needed. The one actionable takeaway is §17.4 (S7 should emit the crossfeed block as
raw appended text, per the roadmap's own existing plan, not via `ProfileConfig.copy_lines`/
`channel_sections`) and §17.3 (reuse the 9s tone-duration lesson if S7/S8 add their own manual
listening-verification scripts).

**17.5 B2-core and B2-ui as shipped (Sessions S7/S8, campaign B closed 2026-07-12).**
`rumble/core/crossfeed.py` implements exactly the block in §17.1, generated rather than
hand-written, from three named presets (`CrossfeedPreset(label, gain_db, freq_hz, delay_us)`):
Chu Moy (-6.0 dB, 700 Hz, no delay), Jan Meier (-9.5 dB, 650 Hz, no delay — the stronger, narrower
sibling design), Linkwitz-style (-4.5 dB, 700 Hz, 300 microsecond delay). The Linkwitz gain was
originally shipped as an unsourced -4.0 dB approximation (S7 `[REVIEW]`; no single published
headline attenuation figure exists for Linkwitz's passive network, unlike the other two presets'
cited figures); resolved 2026-07-13 by tracing the preset family to its source: the Chu Moy
(700 Hz/6.0 dB) and Meier (650 Hz/9.5 dB) figures are exactly bs2b's (bs2b.sourceforge.net)
`cmoy`/`jmeier` presets, and bs2b's own default — 700 Hz/**4.5 dB**, modelling Benjamin Bauer's
30-degree/3-metre virtual speaker placement (JAES 1961), the design Linkwitz's 1971 circuit was
derived from — is the sourced feed level for the delay-based Bauer/Linkwitz lineage preset. A strength
scalar (0-100%) interpolates the preset's gain toward unity **in the linear/voltage domain**
(`crossfeed._scaled_gain_db()`), not by scaling the dB number directly — the delay is never
strength-scaled, only the gain. The roadmap's non-stereo hide/soft-disable decision was originally
implemented as `crossfeed.is_stereo_only(channel_sections)`, since replaced — see 17.7.

**17.7 `can_crossfeed()` — the gate, widened to its true precondition (2026-07-13).** After 17.6's
combined-`L R` fix, crossfeed was STILL greyed out for the user, because the `Channel:`-less case
remained: `is_stereo_only([])` returned `False`. That is the shape of essentially every real
headphone profile — an imported AutoEQ/convolution export is `Device:` + `Preamp:` +
`Convolution:`/`Filter:` lines with **no `Channel:` line anywhere** (APO applies such a profile to
every channel). So the gate was False for the one profile type crossfeed exists to serve.

`is_stereo_only()` is therefore replaced by **`crossfeed.can_crossfeed(channel_sections)`**, gating
on the layer's ACTUAL precondition rather than a layout aesthetic: *the stream must carry an L and
an R*. The emitted block mixes L into R and back, touches nothing else, and is appended as raw text
AFTER the profile body — so how that body groups its own `Channel:` sections never mattered. Hence:
no sections at all -> **True** (implicit all channels); an `all` section -> True; any layout naming
both L and R -> True, **including multichannel** (`L R C LFE RL RR`) since C/LFE/surrounds are left
untouched; only a layout with no stereo pair at all (a lone `C`, `LFE`) -> False, the sole genuine
no-op. Call sites: `profile_manager._render_crossfeed_block()`, `main_window._open_settings()`.
(The `insert_crossfeed_into_config()` call site named here was removed in stage 3 — see §17.8.)

**The same `Channel:`-less hole existed in listener correction**, and was worse:
`compose_channel_sections([])` had nothing to iterate and returned NO sections, so **per-ear Ear
Calibration correction silently did nothing on exactly the AutoEQ/convolution headphone profiles it
exists to correct** — verified against the user's real active profile, which was getting neither
crossfeed nor correction. Fixed by treating an empty section list as the implicit `all` section it
is, which the existing loop then expands per ear. Regression-pinned in both suites, including a test
built from a real `Convolution:` + sample-rate-conditional profile body.

**17.6 The combined-`L R` stereo bug (found and FIXED 2026-07-13) — the "reuse the convention"
lesson.** As shipped, `is_stereo_only()` carried its OWN literal copy of
`listener_correction.compose_channel_sections`'s channel-identity rule: a section counted as stereo
only if its channel string was exactly `"L"`, `"R"`, or `"all"`. Both modules' docstrings even
called out a combined `"L R"` section as explicitly NOT stereo — a deliberate decision, which is
why it survived review. It was wrong, and it broke both features on nearly every real profile:
`builtin_profiles_data._body()` writes **`Channel: L R`** (one combined section — as do RUMBLE's own
AI prompts, §8 of `RUMBLE_Universal_AI_Prompt.md`), so all seven built-in **playback** profiles failed
the check. Consequences, both silent:
- **Crossfeed was permanently greyed out** on every built-in playback profile ("The active profile
  isn't plain stereo…"), and enabled ONLY on the three **mic** built-ins (`Channel: all`), where it
  is meaningless. Not just a UI grey-out: `_render_crossfeed_block()` and
  `insert_crossfeed_into_config()` apply the same predicate, so the layer rendered nothing anyway.
- **Per-ear listener correction was a no-op** on those same profiles: a `"L R"` section fell through
  `compose_channel_sections`' untouched `else` branch, so a listener could complete a full Ear
  Calibration and receive zero correction, with no error or warning.

Fix: ONE definition, `apo_config.stereo_section_ears(channel)` (beside `ChannelSection` itself),
returning the ear tokens a section addresses — `"L"`→`[L]`, `"R"`→`[R]`, `"L R"`/`"R L"`/`"all"`→
`[L, R]`, case-insensitive; `None` for anything naming a non-stereo channel. `crossfeed.
is_stereo_only()` and `listener_correction.compose_channel_sections()` both delegate to it (the
latter now splits a combined section per ear exactly as it already did for `"all"`, cloning every
field so filters/trim/delay still reach both ears). Regression-pinned in both selftests by
iterating the REAL `BUILTIN_PROFILES` — a gate that never tests the profiles users actually run is
how this hid in the first place. The crossfeed block itself never needed the restriction: it
addresses the stream's `L`/`R` directly, AFTER the profile body, so how that body groups its own
`Channel:` sections was always irrelevant to it.

Per §17.4's own conclusion, the block is never round-tripped through `ProfileConfig` — it is raw
text appended after the rest of the composed render body. `ProfileManager._render_layered_body()`
is the one choke point that does this (composes `_apply_render_layers()`, renders it, appends
`_render_crossfeed_block()`'s output if non-empty), shared by both `preview_config()` and
`_write_active_pointer()` so canonical activation, the listener-corrected scratch file, and live
ABX/preview playback all carry crossfeed identically. Settings as shipped in S8: `crossfeed_enabled`
(default `False`), `crossfeed_preset` (default `"meier"`), `crossfeed_strength` (default `100`) —
plain machine/session-scoped keys in `profiles.json`, same rationale as ISO 226's own settings
(§16.5). **Superseded in stage 3 (§17.8):** crossfeed is now primarily a per-profile property, the
`crossfeed_enabled` bool is gone, and the machine-level key is the three-way `crossfeed_mode` source
switch; the global preset/strength keys survive as the fallback/override *value*.

Settings → Listeners gained a "CROSSFEED (HEADPHONES)" section (S8) mirroring the ISO 226
section's shape exactly: enable checkbox, preset combo (`crossfeed.PRESETS`' `.label`s), strength
slider (0-100%) with a centred percentage value label, and an "Insert into profile..." button.
`SettingsDialog` takes a new `active_profile_is_stereo` constructor argument — the dialog itself
has no profile-config access, so `main_window._open_settings()` computes it once per dialog open
via `crossfeed.is_stereo_only(mgr.load_profile_config(active_meta.id).channel_sections)` and
passes it in. When `False`, all four crossfeed controls are soft-disabled (`setEnabled(False)`,
values kept untouched — not reset) and a muted explanatory note is shown, rather than hiding the
section outright, so the setting stays visible and legible even while it has no effect.

**17.6 Tray/toast quick toggle (as shipped in S8; reworked in §17.8).** `rumble/ui/toast_menu.py`
gained a checkable "Crossfeed" `QPushButton` (`action_crossfeed`) alongside the existing
Bypass/Auto-Switch quick toggles, relayed through `rumble/ui/tray_icon.py` exactly like those two
(`crossfeed_toggled` signal, `set_crossfeed_state()` pull-based sync method). As originally shipped,
`MainWindow._on_crossfeed_toggled()` persisted `crossfeed_enabled` immediately (`manager.save()`)
and called `refresh_render_layers()` for an instant live re-render — the "persist immediately,
apply live, not gated by the Settings dialog's Save button" shape `_on_auto_switch_toggled()`
established. **Stage 3 (§17.8) repointed this at `crossfeed_mode`:** the toggle is now a two-state
view of the three-state mode (On restores the last non-off mode, Off parks it), and syncs with the
main-UI source switch, not a Settings checkbox.

**17.7 "Insert into profile..." (S8; REMOVED in stage 3, §17.8).** As shipped in S8,
`ProfileManager.insert_crossfeed_into_config(config)` wrote the current preset/strength as the same
rendered block `_render_crossfeed_block()` produces, but into `config.extra_lines` instead of only
at the render choke point — the block is self-contained (its own `Copy:`/`Channel:`/`Copy:`
structure, independent of the profile's own `channel_sections`) and `apo_config.render_config_body()`
always renders `extra_lines` last and verbatim (§17.4's original finding), exactly the position the
block needs. `MainWindow._on_insert_crossfeed()` mirrored B1's "Bake into profile..." shape (refuse
on a dirty editor; on success load a dirty unsaved change and turn `crossfeed_enabled` off to avoid
a double-apply), with a `SettingsDialog.note_crossfeed_inserted()` to sync the checkbox. **This whole
escape hatch was removed in stage 3:** a profile now carries its crossfeed natively in its
`# Crossfeed:` header (§17.8), so there was nothing left to "insert" as portable text —
`insert_crossfeed_into_config`, `_on_insert_crossfeed` + its wiring, and the
`insert_crossfeed_requested`/`note_crossfeed_inserted` pair are all gone.

**17.8 Per-profile crossfeed + the one-place source switch (stage 3, 2026-07-18/19).** Crossfeed
stopped being a machine-level bool and became primarily a property of the profile, with a single
machine-level switch choosing how it is applied. The pieces:

- **Per-profile value.** A profile carries its own selection in its file header
  (`# Crossfeed: preset=<key> strength=<n>`, `crossfeed.format_spec`/`parse_spec` ↔
  `ProfileMeta.crossfeed: CrossfeedSpec`), edited in the editor's crossfeed row
  (`EditorPanel._build_crossfeed_row` — the "This profile" checkbox + circuit combo + strength
  slider) and persisted alongside the EQ via `ProfileManager.set_profile_crossfeed()`. RUMBLE's AI
  profile prompts emit that header line, so an AI profile arrives with a crossfeed selection and it
  applies automatically. The block is still render-only raw text (§17.4) — never baked into the
  profile's `ProfileConfig`/`extra_lines`.

- **The three-way source mode.** The machine-level key is now `crossfeed_mode`
  (`"off"|"profile"|"global"`, default `"profile"`), replacing the `crossfeed_enabled` bool.
  `ProfileManager._effective_crossfeed(own)` is the whole rule: `off` → nothing; `global` → the
  global value for every profile (override); `profile` → the profile's own value, falling back to
  the global value when it has none. `_global_crossfeed_spec()` no longer has an enable gate — it
  returns the global value whenever `is_active()`, else `None`.

- **The main-UI switch.** A compact Off / Per profile / Global dropdown on the editor's crossfeed
  row (`combo_crossfeed_mode`, signal `crossfeed_mode_changed`) drives `crossfeed_mode`.
  `MainWindow._apply_crossfeed_mode()` is the single choke point: persist, `refresh_render_layers()`,
  and keep every surface in sync (the editor switch via `set_crossfeed_mode()`, the tray two-state
  toggle via `set_crossfeed_state()`, and a Settings Reset via `_apply_settings_live`). The tray
  toggle remembers the last non-off mode (`_last_crossfeed_mode`) to restore on On.

- **The Settings card is now the global VALUE only.** "Global crossfeed value"
  (`settings_dialog.py`) keeps the preset combo, strength slider, and Custom gain/cutoff/delay, but
  lost its Enable checkbox (the switch owns on/off) and the Insert button (§17.7). It emits only
  `crossfeed_preset`/`crossfeed_strength`/`crossfeed_custom_*`.

- **Off by default, without hiding the feature.** `crossfeed_strength` now defaults to **0** (was
  100), so out of the box the default `"profile"` mode renders each profile's OWN crossfeed
  immediately but nothing for un-set profiles — the faithful translation of the old
  `crossfeed_enabled=False` default, which kept crossfeed the opt-in taste/positioning effect it
  always was rather than silently applying meier/100 to every stereo profile. Raising the global
  strength in Settings is what opts a machine into a global fallback/override. (A regression the
  full gate caught: with the old strength-100 default, un-set profiles fell back to an active global
  value, adding a render layer that changed the active-pointer shape and broke `selftest_solo_ab`'s
  plain-render assertion — see the manager-level guard test
  `test_default_settings_render_only_explicit_per_profile_crossfeed`.)

## §18 — Session tone trim: bass/treble/tilt (Sessions S4/S5, campaign B, Rumble adaptive front-end)

Built 2026-07-12. Roadmap B1: "it's 11pm and this track is harsh" — an instant, global,
session-only tone control, deliberately not a hearing correction (contrast §15's per-ear Listener
Slots) and not persisted (contrast every other render layer in this doc, which lives in
`profiles.json`).

**18.1 The curve.** `rumble/core/tone_trim.py`'s `derive_trim_curve(bass_db, treble_db,
tilt_db_per_oct)` sums three pieces via `autoeq_merge.sum_curves()`: a flat bass shelf below
`BASS_SHELF_HZ` (250 Hz), a flat treble shelf above `TREBLE_SHELF_HZ` (4000 Hz) — both corners are
members of `advanced_panel.ISO_10_BAND`'s ISO 266 preferred series, the same low/high regions
`builtin_profiles_data`'s authored tone-character profiles already shape with LSC/HSC filters — and
`eq_target.tilt_target()` pivoted at 1 kHz. Shelf range is `MAX_SHELF_DB = 6.0` (matches
`eq_target.clamp_gain()`'s own default max boost, the codebase-wide soft cap); tilt range is
`MAX_TILT_DB_PER_OCT = 3.0` (the magnitude of `settings_dialog.py`'s Spinorama
`spin_default_tilt.setRange(-3.0, 1.0)` — that picker is asymmetric, this session knob is
deliberately symmetric, see the progress file's S4 `[REVIEW]` note). The summed curve is clamped to
6.0 dB boost / 12.0 dB cut, the same bound every other render layer in this doc uses. Returns `[]`
(no curve math at all) when all three inputs are exactly zero — the common case, since the trim
resets to flat on every fresh `ProfileManager`.

**18.2 Composition and render order.** `compose_profile()` mirrors `loudness_layer.py`'s own shape:
zero-copy when flat, symmetric across every channel section (a taste knob, not ear-specific), global
preamp recomputed as the minimum auto-preamp across the composed sections. `ProfileManager`'s
`_apply_render_layers()` composes it between listener correction and the ISO 226 loudness layer
(§16.4) — after listener correction because that layer may expand a linked "all" section into L/R
first, before ISO 226 because A2's own always-last loudness-matching invariant
(`_apply_loudness_matching()`'s own docstring) already claims the final slot. `ProfileManager.session_trim`
(`bass_db`/`treble_db`/`tilt_db_per_oct`, all `0.0` at construction) is session/machine state exactly
like Listener Slots' `active_listener_slot` (§15) — never written to `profiles.json`.
`set_session_trim(**values)` updates it and calls `refresh_render_layers()` (§16.3b's live-apply
mechanism), so a slider drag reaches the active render within one poll-adapter tick.

**18.3 UI: one widget class, two hosts.** `rumble/ui/tone_trim_widget.py`'s `ToneTrimWidget` (three
sliders + centred value labels + Reset + "Bake into profile..." button) is instantiated twice —
once inside the tray's `ToastMenu`, once inside a frameless `Qt.WindowType.Popup` opened from a
status-bar button in the main window — and both instances stay in sync because both ultimately read/
write the same `ProfileManager.session_trim` through `MainWindow._on_tone_trim_changed()`/
`_refresh_tone_trim_ui()`. Slider changes are debounced 250 ms before reaching the manager (a drag
fires many `valueChanged` signals per tick); Reset bypasses the debounce and applies immediately. A
status-bar chip is visible only while `any(session_trim.values())`.

**18.4 "Bake into profile...".** Writes the current trim as real `GraphicEQ` content into the
*active* profile's editor state (via `ProfileManager.bake_session_trim()`, a public wrapper around
the same `_apply_tone_trim()` compose step used at render time) and resets the session trim to flat
— the escape hatch for a listener who decides a session tweak should become permanent and portable.
Refuses with a message-box while the editor has unsaved changes (`self._dirty` alone, not the ABX
opener's stricter `_dirty or _preview_diverged` guard — see the progress file's S5 `[REVIEW]` note),
leaving both the trim and the editor state untouched on refusal.

## §19 — Room measurement + microphone calibration (Sessions S9-S17, campaign C, Rumble adaptive front-end)

Built 2026-07-12/13. Roadmap C: "measure your room, correct toward a target, verify the result" —
a self-contained wizard (Tools -> "Measure Room...") layered on top of two Qt-free core modules
(`rumble/core/audio/mic_capture.py`, `rumble/core/measurement.py`) plus a calibration-tier model
(`rumble/core/mic_cal.py`) that everything else in this section is built to respect. Speakers only —
headphone measurement without a dedicated coupler rig isn't offered (`rumble/ui/measure_dialog.py`'s
own `_SPEAKERS_ONLY_NOTE`).

**19.1 Capture.** `rumble/core/audio/mic_capture.py` (S9) is the input-side twin of `engine.py`:
`list_input_devices()` enumerates via `sd.query_devices()` filtered on `max_input_channels > 0`;
`MicRecorder` mirrors `ToneEngine`'s async `record()`/`stop()`/`on_done` shape but always opens
mono, and latches a clip flag the first time any sample reaches full scale (never clears mid-take).
Both reuse `engine.AudioError`/`_classify_portaudio_error` for the same `{"exclusive", "no-device",
"other"}` failure taxonomy playback already has.

**19.2 ESS sweep + deconvolution.** `rumble/core/measurement.py` (S11/S12) implements A. Farina's
exponential-sine-sweep method: `generate_ess()` produces a matched sweep/inverse-filter pair
(self-normalized so `sweep * inverse` peaks at exactly 1.0), `deconvolve()` FFT-convolves a
recording against the inverse filter and slices from the fixed `len(inverse) - 1` offset to discard
harmonic-distortion images (which land at negative time relative to the true linear IR — see the
module docstring for the full derivation), `ir_to_magnitude()` converts to `(freq, dB)` points on
`eq_math`'s log grid. `time_gate()` windows an IR to its early direct-sound portion with a
raised-cosine taper — the mechanism §19.6's estimation sub-flow uses for quasi-anechoic measurement.
Also: `measure_rta()` (pink-noise RTA fallback, POWER-domain block averaging per Welch 1967 — dB-domain
averaging of independent noise realizations is a biased, downward estimator), `average_positions()`
(thin wrapper on `autoeq_merge.average_curves`), `smooth_measurement()` (thin wrapper on
`eq_target.octave_smooth_adaptive`), and `agc_check()` (plays the same sweep twice 6 dB apart and
flags Windows mic "enhancement" processing if the captured delta compresses below 4.5 dB).

**19.3 The synchronized play+record helper.** Every real measurement in the wizard — the AGC check
and every position capture — goes through ONE method, `MeasureDialog._synchronized_record()` (S14
Amendment, replacing S13's crude unsynchronized first draft outright): background thread starts
`MicRecorder.record()`, main thread sleeps `_RECORD_LEAD_IN_S` (0.3 s), plays, waits for playback,
joins the recording thread, then trims the lead-in samples off the front of the returned buffer so
`recorded[0]` lines up with the instant playback began — `deconvolve()`'s own documented alignment
assumption. Mirrors `rumble/core/time_alignment.py`'s `run_measurement()` pattern; the recording side
blocks on a `threading.Event` rather than a blocking `record()` call because `MicRecorder.record()`
is itself already async (a necessary, not divergent, adaptation — see that method's own docstring).

**19.4 The two-tier (three-source) calibration model.** `rumble/core/mic_cal.py`'s
`correction_band(cal)` is the single choke point every correction in the wizard fits into:

| `cal` | Trusted band | Rationale |
|---|---|---|
| `None` (no cal / standard mic) | `20`-`300` Hz (clamped 200-500) | Below the Schroeder transition, room modal behavior dominates and any smooth-response capsule captures it correctly; above it, an unknown mic's own coloring is the same order of magnitude as real speaker differences. |
| `source="file"` (purchased cal, e.g. UMIK) | full `20`-`20000` Hz | Genuine, manufacturer-measured calibration. |
| `source="transfer"` (S16, §19.5) | full `20`-`20000` Hz | Derived by cancelling speaker+room against a borrowed calibrated mic — trusted the same as a purchased file. |
| `source="estimated"` (S17, §19.6) | its own stored `band` (falls back to the `None` band if missing) | Extra uncertainty (a single reference speaker's unit variance + user positioning) means only a bounded, self-reported range is trusted, not the full range. |

Calibration curves are stored under `data_dir/mic_cal/` as a GraphicEQ-format `.txt` (reusing
`autoeq_merge.format_graphiceq`/`parse_graphiceq`) plus a small JSON sidecar (`name`/`source`/`band`)
via `save_cal()`/`list_cals()`/`load_cal()`. `subtract_cal(measurement, cal)` — `measurement - cal`
on their reconciled common grid via `autoeq_merge.delta_curve()` — is the one arithmetic primitive
every derivation below reuses rather than re-deriving.

**19.5 Transfer calibration (S16).** `derive_transfer_cal(reference_curve, reference_cal,
unknown_curve)`: `ground_truth = subtract_cal(reference_curve, reference_cal)` (removes the
reference mic's own known error), then `subtract_cal(unknown_curve, ground_truth)` gives the unknown
mic's own response error, smoothed via `eq_target.octave_smooth_adaptive`. The wizard's sub-flow
(entry point: setup page's "Calibrate this mic against a reference...") does 2 passes per mic,
averaged via `autoeq_merge.average_curves` — position repeatability (a FIXED mic stand, same exact
spot for both mics) is the stated error floor, not the math.

**19.6 Spinorama-referenced estimation (S17).** For a user with no reference mic to borrow but who
owns a speaker in the Spinorama database: the speaker's own published anechoic On Axis curve stands
in for a reference mic's cal file. Entry point: setup page's "Estimate calibration from a speaker I
own..." button, which reuses `rumble/ui/autoeq_dialog._MeasurementPanel` (the same reusable
search/select panel the unified measurement browser is built from) fed a bare `SpinoramaProvider`
catalogue directly (not `FederatedCatalog`) so only speakers are ever offered — no new search-list
code was written for this.

Flow: pick a speaker -> one gated ESS measurement (`~1 m` on-tweeter-axis placement is instructed,
not enforced) -> `measurement.detect_first_reflection_gate_ms(ir, sample_rate)` (new this session:
the direct-sound peak marks t=0, the first secondary peak clearing 15% of the direct peak's
magnitude within a 0.5-15 ms search window marks the earliest room reflection; falls back to the
15 ms ceiling for an unusually clean capture with no detected reflection) picks the gate length,
shown to the user -> `measurement.time_gate()` -> `measurement.ir_to_magnitude()` (raw, no cal
subtraction — this measures the SPEAKER, not validated against any mic cal) -> fetch the speaker's
raw On Axis curve via `spinorama.fetch_curve(entry, curve="On Axis", ...)` directly, **not**
`SpinoramaProvider.fetch_curve()` (that wrapper runs `eq_target.make_correction` and returns a
finished EQ correction, not the raw published curve this derivation needs as ground truth) ->
`mic_cal.derive_estimated_cal(gated_measurement, anechoic_on_axis)`: `subtract_cal()` (same
"measurement minus ground truth" shape as transfer calibration's own second step), then smoothed
much more heavily than every other auto-derived curve — uniformly at
`eq_target.ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT` (2.0 octaves, the widest window
`octave_smooth_adaptive` documents anywhere, applied flat rather than its usual narrow-to-wide
taper) — since this estimate carries an extra unknown (the reference speaker's own unit variance) on
top of the mic's response.

Confidence band: `mic_cal.estimated_band(gate_ms) = (1 / (gate_ms / 1000), 10000.0)` — the lower
bound is the standard gated-measurement resolution limit (a T-second gate can't resolve below
`1/T` Hz), the 10 kHz ceiling is adopted directly from the roadmap's own stated confidence limit
(positioning/unit-variance dominate above there), clamped so an implausibly short gate can never
invert the band. Shown to the user verbatim before saving: `"Estimated calibration, +/-2-3 dB,
<f_lo>-<f_hi> Hz (gate <n> ms, from the measured first reflection)."` Saved via
`save_cal(source="estimated", band=...)`.

**19.7 Tier integration: the tightened boost cap.** `MeasureDialog._correction_max_boost_db()` feeds
`eq_target.make_correction`'s `max_boost_db` — the standard 6.0 dB default for every tier except
`source="estimated"`, which uses `mic_cal.ESTIMATED_TIER_MAX_BOOST_DB = 3.0`: an estimated
calibration's own +-2-3 dB uncertainty shouldn't get compounded with a full-strength correction on
top of it. The 3.0 dB figure is not a new invention — it reuses
`eq_target.directivity_boost_cap()`'s own existing worst-case damping fraction
(`_DIRECTIVITY_MIN_CAP_FRACTION = 0.5`, "maximum reduction is half the cap", §7's directivity-cap
precedent) applied to the standard 6.0 dB cap, on the reasoning that an estimated mic calibration's
uncertainty is exactly the kind of case that precedent already exists for.

**19.8 The wizard's page flow.** `rumble/ui/measure_dialog.py`'s `MeasureDialog`: intro/safety (engages
`BypassGuard` + builds the real `ToneEngine`/`MicRecorder` once) -> mic & output setup (input device,
cal choice with a live tier label, entry points into §19.5/§19.6's sub-flows) -> level/AGC sanity
check -> baseline measurement (3-6 positions, spatially averaged + smoothed) -> target choice
(`target_curves` + linear tilt) -> correction generation (SAME `eq_target.make_correction` pipeline
`spinorama_provider.py` uses) -> results (save as a profile tagged "Room Correction", NOT activated)
-> verify (re-measures the saved profile via `preview_config()`, overlays measured-before/target/
measured-after, reports achieved-vs-predicted RMS deviation from target within the tier's own band)
-> done. `_capture_raw_curve()` (baseline/transfer-cal's shared capture chain) and `_capture_raw_ir()`
(S17: the same chain minus the final magnitude conversion, so the mic-estimation sub-flow can gate
the IR first) both funnel through `_synchronized_record()` (§19.3) — no measurement path in this
wizard has its own separate play/record implementation.

## §20 — Equalizer APO `Device:` dispatch semantics (confirmed live, Session S18)

Confirmed 2026-07-13 on real hardware (two APO-hooked outputs: Speakers/Creative BT-W6, a Bluetooth
speaker, and Speakers/FIIO K13 R2R, a wired USB DAC) via `tests/verify_device_dispatch_live.py`,
ahead of D1a (per-device profile dispatch, roadmap section D1). D1's design assumes a dispatch file
shape: several `Device: <pattern>` blocks (one per assignment) followed by an UNSCOPED trailing
block as the fallback for unassigned endpoints. This session confirms exactly how that shape
actually behaves — and the confirmed mechanism differs from what the roadmap's own design note
guessed at, even though the practical outcome D1 wants still holds.

**20.1 Test config.** One real config file, three `Device:` blocks, each carrying an audibly
distinct pink-noise-reshaping marker chosen so "scoped-only" / "stacked" / "superseded" would each
sound different by ear:

```
Device: Speakers Creative BT-W6      (device A's own pattern)
Channel: all
Filter 1: ON LP Fc 500.0 Hz Q 0.71    -> "muffled, bass-only, no highs"

Device: Speakers FIIO K13 R2R         (device B's own pattern)
Channel: all
Filter 1: ON HP Fc 4000.0 Hz Q 0.71   -> "thin, no bass/mids"

Device: all                            (unscoped trailing fallback)
Channel: all
Filter 1: ON PK Fc 8000.0 Hz Gain 12.0 dB Q 1.00   -> "bright treble sizzle"
```

Written directly as raw concatenated `apo_config.render_config_body()` output (three independent
`ProfileConfig` objects — the data model has exactly one `device` field per `ProfileConfig`, so a
multi-block dispatch file cannot be expressed as a single `ProfileConfig`; same "data model has no
slot for this shape" situation §17's crossfeed spike hit with its post-`Channel:` `Copy:` line, and
the same raw-text-concatenation resolution). Staged into the real config via a hand-built
`Include:` scratch file under `rumble_active.txt`, since `ProfileManager.preview_config()` also only
accepts one `ProfileConfig`.

**20.2 Results.** Four listening phases (device A and device B, each first under the full
three-block dispatch, then again under a fallback-marker-alone reference config with no per-device
blocks at all), all consistent and unambiguous:

- **Device A, full dispatch:** muffled/bass-only — A's own 500 Hz low-pass, and *only* that. No
  treble sizzle from the trailing fallback block.
- **Device B, full dispatch:** thin/no bass-mids — B's own 4000 Hz high-pass, and *only* that.
  Again no fallback sizzle.
- **Device A, fallback-alone reference:** a distinctly different bright/high-mid "static" character
  — confirms the fallback marker is real, audible, and simply never showed up in the A result above.
- **Device B, fallback-alone reference:** a "fuller" high hiss (retains bass/body, unlike the true
  high-pass) — again confirms the fallback marker is real and audibly different from B's own
  high-pass result.

**Neither device ever heard the fallback's marker stacked on top of, nor substituted in place of,
its own device-scoped block** — despite `Device: all` textually appearing last in the file and
`all` matching every device unconditionally.

**20.3 Confirmed mechanism: first matching `Device:` block wins, not last.** The roadmap's D1 design
note assumed "APO applies the last matching lines" (hence: put the global fallback last, expecting
either that it wins for everyone, or at minimum stacks). Both readings are refuted by 20.2 — if
"last matching wins" were true, **both** devices would have heard only the trailing `Device: all`
block's marker (it is, textually, the last block matching either device), never their own earlier
block. What was actually observed is the opposite: each device is claimed by the **first**
`Device:` block in file order whose pattern matches it; once claimed, later blocks that would also
match (including a trailing `all`) are skipped entirely for that device — not layered on, not
overriding. This is a real, if subtle, correction: **file order determines which block wins, and
earlier wins, not later.**

Practical consequence for D1a's dispatch renderer: it already places per-device assignment blocks
before the global-fallback block (the correct order for this confirmed mechanism to produce the
intended per-device isolation) — so **no design change is needed**, but the render code's own
comments/docstrings should describe the mechanism as "first-claim, file-order-first-wins," not
"last matching line wins" as the roadmap prose states, so a future reader doesn't get the reasoning
backwards even though the current implementation shape is already correct. If a future change ever
reorders blocks (e.g. some dynamic assembly that appends new assignments after the fallback rather
than before it), that reordering would silently break dispatch — assignments must always render
before the fallback, never after.

**20.4 Pattern matching (item c): confirmed reliable.** Both hooked endpoints happen to share the
literal word "Speakers" in `device_name` (Windows reports both as generic "Speakers" with the real
distinguishing information only in `connection_name`) — a real-world case where a naive
device-name-only match could have cross-matched. `ApoDevice.pattern`'s "deviceName connectionName"
word-list (`Speakers Creative BT-W6` vs. `Speakers FIIO K13 R2R`) disambiguated the two correctly in
both directions with no cross-talk observed across four phases.

**20.5 Include: indirection (item d): confirmed working.** Every audible change in this spike
arrived exclusively through `rumble_active.txt`'s `Include:` line pointing at a hand-built scratch
file under `rumble/` — there is no other path by which these blocks could have reached the live APO
config. Both the initial dispatch-config swap and the mid-spike swap to the fallback-only reference
config took effect correctly (after a transient retry — see 20.6).

**20.6 Operational finding: transient `PermissionError` on `rumble_active.txt` writes.** One write to
the real `rumble_active.txt`, immediately following ~14s of continuous playback routed through it,
failed with `WinError 5` (access denied) even though the same path had been written successfully
seconds earlier and the account has normal write access to the file. Most likely Equalizer APO's own
hot-reload file watcher (or antivirus real-time scanning) briefly holding a read lock right after
detecting a change, not a real permissions problem — a short retry-with-backoff (3-5 attempts, ~0.3s
apart) cleared it every time it was tried. Not confirmed as APO-specific (could equally be Windows
Defender or another watcher); worth a defensive retry wrapper around `rumble_active.txt` writes
generally if D1a's dispatch renderer writes that file more frequently than today's code does (this
session's own `write_pointer_with_retry()` in `tests/verify_device_dispatch_live.py` is the
reference implementation).

**20.7 Methodology note (not an APO finding, a spike-process one).** The first two run attempts of
this spike failed to produce usable listening data: a fixed 3s-then-10s countdown timer printed to
the console was useless because the user had no live view of a background tool call's console output
before the countdown had already elapsed — the same class of "tone duration/pacing chosen from the
wrong side of the interaction" problem §17.3 hit with S6's original 3s tones, but on the pacing
mechanism itself rather than the tone length. The working fix was to drop timed pacing entirely: the
committed script now states the target device and exact listening cue, blocks on a real `input()`
keypress before playing, and offers an on-demand replay of any single tone or whole phase-pair. This
requires a live stdin the user types into directly, so **this class of script must be run by the
user directly in their own terminal, not invoked through an agent tool call** (no live keyboard
channel exists between the two) — worth carrying forward as a standing note for any future
`verify_*_live.py` script that needs paced human interaction rather than a single unattended pass.

## §21 — Per-device profile dispatch, as shipped (D1a/D1b/D1c, campaign D1 closeout)

Reconciles §20's live-hardware spike against the full D1 implementation (Sessions S19-S21):
`settings["device_profile_assignments"]` (endpoint GUID -> profile id), the `rumble_active.txt`
dispatch renderer, the Devices settings page, and every override/reconciliation edge D1c added.

**21.1 Data + render shape.** One plain setting,
`settings["device_profile_assignments"]: dict[str, str]` (endpoint GUID -> profile id), default
`{}` — unassigned endpoints are unaffected (empty dict renders byte-identical to the pre-D1 single-
profile pointer). When one or more assignments are valid (`ProfileManager._valid_device_assignments()`
drops any assignment whose profile id no longer exists — see 21.4), `rumble_active.txt` becomes a
dispatch: one `Device: <pattern>` block per assignment (pattern from `audio_devices.ApoDevice.pattern`,
resolved fresh at render time, never cached), Include-ing that profile's own branch, followed by an
explicit `Device: all` block Include-ing the global active profile as the trailing fallback —
assignment blocks always render BEFORE the fallback, per §20.3's confirmed first-claim/file-order-
first-wins mechanism (reordering this would silently break per-device isolation). Every render layer
(listener correction, tone trim, ISO 226 loudness, loudness matching, crossfeed) composes onto EVERY
branch independently via the shared `_dispatch_branch_target()`/`_render_layered_body()` choke point —
they are listener-scoped and machine-scoped, not device-scoped, so they apply uniformly regardless of
which branch a device lands in. A branch with no active render layer Includes the profile's own file
directly (no scratch); an active branch composes to its own scratch file under `rumble/` — the global
fallback keeps its pre-D1 name (`rumble_listener_active.txt`) for byte-identical zero-assignment
behavior, each per-device branch gets `rumble_device_<8-char-sha256-of-guid>.txt`
(`DEVICE_SCRATCH_PREFIX = "rumble_device_"`, public, for display/debug filtering).

**21.2 UI (D1b).** Settings -> Devices: one row per APO-hooked render endpoint
(`audio_devices.list_apo_devices()`, filtered to `flow == "Render"`), each a combo of "Follow active
profile" (default) or any saved profile. No `ProfileManager` setter exists for this setting — the
Devices page mutates `settings["device_profile_assignments"]` directly through the normal working-
dict/`_collect()`/`settings_saved` flow every other settings-gated layer already uses; the existing
post-accept `manager.settings.update(...)` + `manager.refresh_render_layers()` in
`main_window._open_settings()` re-renders the dispatch with zero dedicated wiring. The A1 health
banner's "Manage Devices..." button opens Settings straight to this page
(`initial_category="Devices"`).

**21.3 Overrides always replace the WHOLE dispatch, never one branch (D1c, `selftest_device_dispatch.py`
tests 8-9).** Preview (`preview_config()`/`end_preview()`), ABX trials (`abx.ABXRunner`, which is
itself just `preview_config()`/`end_preview()` under the hood — confirmed by a dedicated ABX-mid-
assignments test reusing `selftest_abx.py`'s own manager-fixture shape rather than editing that file),
and Bypass (`set_bypass()`) all write an unscoped, fully-overriding `rumble_active.txt` — no `Device:`
scoping survives any of the three — and restore the dispatch VERBATIM (byte-identical, assignments
and all) once the override ends. This was already true from S19's own choke-point design (every
override funnels through `_write_active_pointer()` or fully replaces it); D1c's job was proving it
with tests, not changing behavior.

**21.4 Reconciliation edge: a device's ENDPOINT missing vs. its ASSIGNED PROFILE deleted are two
different failure modes, surfaced two different ways.** An endpoint no longer hooked by APO (S20):
shown in its own "UNAVAILABLE DEVICES" section with a "Clear assignment" button, since there's no
live combo to represent a device that isn't currently in `apo_render_devices` at all. A profile
deleted out from under a live assignment (D1c): the device's row and combo are both still live, but
`combo.findData(dead_id)` previously returned -1, silently landing on "Follow active profile" with
no visible trace anything had ever been assigned (S19's `_valid_device_assignments()` already drops
it render-side, unconditionally and correctly — this was purely a Settings-page legibility gap).
D1c closes it: when the working assignment's profile id isn't among the page's known
`device_profile_options`, the combo gets one extra synthetic entry, "(deleted profile -- was
assigned)", carrying the dead id — so it's shown selected instead of silently defaulting, and stays
that way (an untouched Save re-persists the dead id as-is) until the user explicitly reassigns or
picks "Follow active profile" to clear it. Mirrors the UNAVAILABLE DEVICES section's own "kept until
cleared, never silently dropped" convention.

**21.5 Backup/restore must explicitly re-render the dispatch (D1c finding).** `rumble_active.txt`
lives at `config_path` root, not under `rumble_dir` — so it was never itself a zip member in either
direction, and `restore_library()` previously left whatever pointer was already on disk untouched
after swapping in restored settings/profiles. A backup taken with active `device_profile_assignments`
would restore the SETTING correctly but the on-disk dispatch would silently stay stale (the pre-
restore render, possibly for a completely different active profile/assignment set) until some later
event happened to trigger `refresh_render_layers()`. Fixed by having `restore_library()` call
`self._write_active_pointer(self.get_active_profile())` directly (not `refresh_render_layers()`,
which refuses mid-preview/bypass — a restore is an authoritative state replace and must win
regardless) as its last step, right after `_load()` repopulates settings/active-profile state from
the restored archive. Separately, `backup_library()` now also skips `rumble_listener_active.txt` and
`rumble_device_*.txt` when zipping the `rumble/` tree (previously only `rumble_preview.txt` was
excluded) — these are render scratch regenerated on demand from settings + profiles, so shipping a
stale device-GUID-hash scratch file into a backup (possibly for an endpoint that doesn't even exist
on the restoring machine) would just be dead weight the next render sweeps away anyway.

**21.6 Hand-back-to-APO (`release_control()`) must clean the WHOLE render footprint, not just the
top-level pointer (D1c finding).** Two bugs, both pre-existing (not introduced by D1a/D1b), both
fixed this session: (a) `release_control()` unlinked `PREVIEW_FILENAME` at `config_path` root, but
`preview_config()` has only ever written it under `rumble_dir` (`self.rumble_dir / PREVIEW_FILENAME`)
— the unlink call was always a silent no-op against a path that never held that file. (b)
`release_control()` never touched `LISTENER_ACTIVE_FILENAME` or any `rumble_device_*.txt` at all, so a
hand-back while a dispatch (or even just the pre-D1 composed-fallback scratch) was active left that
render scratch behind. Fixed: `release_control()` now removes `ACTIVE_POINTER_FILENAME` from
`config_path` root, `PREVIEW_FILENAME`/`LISTENER_ACTIVE_FILENAME` from the CORRECT `rumble_dir`
location, and sweeps every `rumble_device_*.txt` via the existing `_cleanup_stale_device_scratch(set())`
— all while still never touching the `rumble/` profile library itself (`defaults/`, `user/`, `irs/`),
since hand-back is reversible via `resume_control()` and user profiles must survive it. Separately
verified (text-review only, no gate hook exists for `.iss` files): the UNINSTALLER's own
`packaging/installer.iss` already covers `rumble_device_*.txt` for free — its final uninstall step is
`DelTree(ConfigDir + 'rumble', True, True, True)`, an unconditional recursive delete of the entire
`rumble/` subtree (correct for a full uninstall, unlike the reversible in-app release path above) —
so no installer.iss change was needed for this session's dispatch scratch, only the in-app release
path required a real fix.

**21.7 Config-health viewer already traces per-device branches legibly — verified, not extended.**
`resolve_includes_traced()`/`effective_config_report()` are pure line-by-line flatteners with no
special-casing of `Device:` blocks at all; a multi-device dispatch's `Device: <pattern>` lines pass
through untouched (tagged with `rumble_active.txt` as their source) immediately followed by that
branch's own recursively-traced content (tagged with ITS scratch file's name) before the next
`Device:` line — confirmed correct and non-overlapping between branches by a dedicated test
(`selftest_config_health.py::test_effective_config_report_traces_per_device_dispatch_legibly`). No
code change was needed; the "Sources: A -> B -> C" summary line just lists the internal scratch
filenames (same convention every other traced source already uses — profile files show by filename
too, not a friendly display name), which is truthful, not mislabeled.

**21.8 Auto-Switch precedence (roadmap note, now documented in-app too).** Auto-Switch rules
(`settings["auto_switch_rules"]`) only ever select the GLOBAL active profile — they have no
per-device awareness and D1 added none. A device with an explicit `device_profile_assignments` entry
always wins for that endpoint regardless of what Auto-Switch just set globally; an unassigned device
simply follows whatever Auto-Switch (or a manual `activate_profile()`) last set as the global active
profile. This is a precedence rule, not a rule-engine change — no code in `auto_switch.py` itself
needed to change or is aware `device_profile_assignments` exists. The Devices settings page states
this explicitly (§21.2's precedence note, `describe()`'d for a11y).

## §22 — Capture-device (microphone) EQ, as shipped (D2, Session S22)

**22.1 One dict, two flows.** D2 does NOT add a second settings key — `settings[
"device_profile_assignments"]` (§21.1) is reused verbatim for Capture-flow endpoints too, since GUIDs
are unique per endpoint regardless of flow. `ProfileManager._device_for_guid(guid)` (new; replaces the
old body of `_device_pattern_for_guid()`, which now calls it) scans `audio_devices.list_apo_devices()`
fresh every render and returns the whole `ApoDevice` (pattern AND flow), not just the pattern string —
`_render_dispatch()` uses the `.flow` to decide whether an assignment's block needs `Stage: capture`.

**22.2 Render shape: `Stage: capture` precedes that assignment's own `Device:` line, nothing else
changes.** For a Capture-flow assignment, the block is:
```
Stage: capture
Device: <pattern>
Include: <target>
```
instead of D1's plain `Device: <pattern>` / `Include: <target>`. This mirrors
`apo_config.render_config_body()`'s own canonical Stage-before-Device order (§11.2) — Stage: was
already "parsed/rendered structurally" per the S21->S22 Amendment, so no new parsing/rendering
mechanism was built, just the same ordering applied to a hand-assembled dispatch block instead of a
`ProfileConfig`. Render-flow assignments and the trailing `Device: all` fallback are unchanged — no
Stage: line, byte-identical to D1. An assignment whose endpoint can't currently be resolved (unhooked,
removed) also gets no Stage: line (flow is unknown, so none is guessed) — same bare-GUID-pattern
fallback D1 already had, now confirmed by `selftest_device_dispatch.py` test 12. Render layers
(listener correction, tone trim, ISO 226, loudness matching, crossfeed) compose onto a capture branch
exactly like any other branch via the existing `_dispatch_branch_target()` choke point — flow has no
bearing on that machinery at all.

**22.3 `[REVIEW]` — Stage:+Device: combined has NOT been confirmed on real hardware.** KB §20's live
spike (S18) only exercised `Device:` alone, on two Render endpoints — it never tested a `Stage:` line
together with a `Device:` line in the same block, and no Capture endpoint was APO-hooked on the
development machine at S22 time to test with (`audio_devices.list_apo_devices()` returned zero Capture
entries; confirmed by direct query at the start of this session). The S22 spec's own fallback for this
exact situation ("if S18 didn't cover it, do a 15-minute scoped verification with the user IF present,
otherwise record a BLOCKING follow-up and ship the render-side behind the existing tests only") was
taken: no capture device was available to verify against regardless of user presence, so this shipped
render-side, tests-only, flagged here. Two things worth confirming on real hardware before relying on
this in production: (a) that APO actually scopes a `Stage: capture` + `Device: <pattern>` block to
JUST that endpoint's capture stream, not (for example) applying to that endpoint's Render stream too
if the same physical device exposes both, or silently ignoring the Stage: line when combined with
Device: in one block; (b) whether the existing unscoped `Device: all` fallback block (no Stage: line,
which `apo_config.py`'s own comment says APO defaults to "post-mix + capture") already means EVERY
capture endpoint picks up the global playback profile's processing by default even before D2 — a
pre-existing, not D2-introduced, behavior that D2's own Capture tab does not attempt to suppress or
warn about. See the progress file's Follow-ups for the actionable item.

*Status 2026-07-13:* still zero APO-hooked Capture endpoints on this machine (re-checked; several
active mics exist unhooked), so the live confirmation remains open, but two things changed: (1)
`tests/verify_capture_dispatch_live.py` now exists -- a fully programmatic spike (two-tone probe
through the speakers, recorded through the hooked mic, band-ratio verdicts across four config
phases including a `Stage: post-mix`+`Device:` control that detects a silently-ignored Stage:
line); hook a mic via Configurator.exe and run it to close this item. (2) Question (b) has a
documentation-level answer: the Equalizer APO Configuration reference states "Initially, the
selected stages are post-mix and capture, so that the filtering commands are executed exactly once
for output and input devices" -- so the unscoped `Device: all` fallback is expected to process
every hooked capture endpoint with the global playback profile; treat as likely-yes pending the
live run.

**22.4 A1 extension: un-hooked capture endpoints noted inside the Devices page only.** New
`audio_devices.list_all_capture_endpoints()` walks the `MMDevices\Audio\Capture` registry tree
directly (every capture endpoint Windows knows about), unlike `list_apo_devices()` (which only walks
the Child APOs key, i.e. already-hooked endpoints). The Capture tab diffs the two lists and shows a
muted note naming any active capture endpoint that isn't APO-hooked yet — informational only, no new
banner, no dismiss-state, no tray toast (unlike A1's own playback-side warning), per the D2 spec's
explicit "no new banners" instruction.

**22.5 Mic built-ins use `Stage: capture` + `Channel: all`, not `Channel: L R`.** The seven pre-D2
built-ins (`builtin_profiles_data._body()`) hardcode a stereo `Channel: L R` section, correct for
playback devices. D2's three mic built-ins use a `_mic_body()` helper emitting `Channel: all` instead —
capture endpoints are commonly mono, and `all` is `ChannelSection.channel`'s own documented default,
channel-count-agnostic. `rumble_voice_clean_hpf` (HP 90 Hz Q 0.70, citing the standard 80-100 Hz
broadcast/podcast mic high-pass convention), `rumble_voice_presence` (HSC 3500 Hz +2.5 dB Q 0.70), and
`rumble_voice_broadcast` (the HP 90 Hz plus a PK 3000 Hz +2.0 dB Q 1.00 presence bump — a peaking
filter, not the Presence profile's shelf — and an HSC 8000 Hz -2.0 dB sibilance tame) — all tagged
`Microphone` (plus `Situation / Voice`, `Situation / Podcast`), all seeded/system-protected exactly
like the seven playback built-ins (no special-cased code path — `ensure_builtin_profiles()` is already
content-agnostic). Preamp values computed with `preamp_calc.calculate_auto_preamp()` per the module's
own convention (§8 above): 0.0 dB (pure HP, no boost), -3.0 dB, -2.5 dB respectively.

They also lead with a **`Stage: capture`** line, which is what actually MAKES them mic profiles:
`ProfileMeta.capture_only` is `stage == "capture"` and reads nothing else, so the `Microphone` tag is
decorative as far as code is concerned. **BUG, fixed 2026-07-15:** `_mic_body()` shipped without that
line from D2 onward, so all three parsed to `stage=None` → `capture_only` False → the Library's
"Microphone profiles" toggle (`show_microphone_profiles`, wired since D2 through
`main_window._load_profile_list()`) filtered on a predicate that was never true for any profile and
therefore appeared to do nothing at all. The `Stage: capture` the dispatch writes (§22.2) goes into
`rumble_active.txt`, never into the profile file, so it could not stand in. `ensure_builtin_profiles()`
regenerates defaults/ on every launch and writes on change, so existing installs self-heal.

**22.6 Hiding microphone profiles DISABLES microphone EQ; it is not a view filter.**
`show_microphone_profiles` (default **off**) gates three things, not one:
- **Library/tray** — `main_window._load_profile_list()` drops `capture_only` profiles.
- **Dispatch** — `ProfileManager._valid_device_assignments()` drops any assignment whose endpoint
  resolves to `flow == "Capture"`, so no capture branch reaches `rumble_active.txt` and APO applies
  nothing to a mic. Done at that choke point (not in `_render_dispatch()`) so a suppressed sole
  assignment empties the dict and the render collapses to the plain no-assignments fast path,
  byte-identical to never having assigned one. An **unresolvable** endpoint is never suppressed —
  flow is unknown, so nothing is guessed, matching §22.2's own no-Stage-line rule.
- **Settings → Devices** — the Capture tab is not built at all (a control with no effect otherwise).
  Capture GUIDs still count as "known" there, so a hidden mic is never listed under *Unavailable
  devices*, whose Clear button would destroy the assignment. Toggling rebuilds the Devices page live
  (`_on_show_microphone_toggled`); because `_schedule_apply()` is debounced, that handler writes
  `_working["show_microphone_profiles"]` through itself first, or it would rebuild the page from the
  pre-toggle value. `_collect_device_assignments()` reads the working dict first and only overwrites
  GUIDs it still has a combo for, which is why capture assignments survive the rebuild.

Nothing is destroyed in any of the three: profiles stay on disk, assignments stay in settings, and
re-enabling restores the previous state exactly. **The calibration-mic path is deliberately exempt** —
`core/mic_cal.py`, `core/audio/mic_capture.py` and `ui/measure_dialog.py` never consult this setting
(measurement records from an input device directly and never routes through APO profiles or device
assignment), so room measurement and mic calibration keep working at full function while microphone
EQ is off. The editor's Advanced → Stage dropdown keeps its "Capture only (microphones)" entry
regardless: `Stage` is a general APO passthrough field, and removing an option a loaded profile may
already be set to would leave a value the UI could not represent.

## §23 — Bass-management wizard, as shipped (D3, Sessions S23/S24, campaign D closeout)

**23.1 "SUB" in the roadmap prose means the literal channel `"LFE"`, never a channel named "SUB".**
`rumble/core/bass_management.py::SUB_CHANNEL = "LFE"` — `CHANNEL_PRESETS` (`editor_panel.py`),
`STANDARD_CHANNEL_ORDER` (`time_alignment.py`) and the LFE-only-bass-filter warning (`main_window.py`)
all agree on this identifier; `build_bass_management()` raises `ValueError` on a layout without it.

**23.2 Core (D3a, S23): `build_bass_management(layout, xover_hz, alignment, sub_trim_db, distances=None,
main_trims_db=None, sample_rate=DEFAULT_SAMPLE_RATE) -> BassManagementResult(channel_sections,
copy_lines, preamp_db)`.** Profile CONTENT, not a render layer — the caller merges the result into a
`ProfileConfig` through the normal editor/dirty flow, exactly like `autoeq_merge.py`'s or
`listener_correction.py`'s own composed output elsewhere. `alignment` is `"LR2"` (one cascaded
Q=0.5 section per side, 12 dB/octave) or `"LR4"` (two cascaded Q=0.70711 sections per side, 24
dB/octave) — both cross at exactly -6.021 dB with exactly complementary magnitudes against
`eq_math.py`'s own biquad model. **LR2 polarity inversion (bug found and fixed 2026-07-13):**
S23's original "sum flat to 0.0 dB" numeric verification summed MAGNITUDES, which is blind to
phase — the COMPLEX (acoustic) sum is allpass-flat for LR4 as-is, but LR2's HP/LP outputs are 180
degrees out of phase (classical LR result, Linkwitz JAES 1976), giving a measured -65 dB null at
the crossover when summed in phase. As every real LR2 implementation does, the sub side is now
polarity-inverted — the whole summed feed, via negative `Copy:` factors
(`Copy: LFE=-1*LFE+-0.5*L+-0.5*R` for a 2.1 LR2 layout; APO's documented syntax, Configuration
reference example `Copy: L=R+-6dB*C`) — after which |HP-LP| is flat to floating-point precision;
`selftest_bass_management.py` now asserts both the null-without-inversion and the
flat-with-inversion facts in the complex domain. Negative `Copy:` factors have not had a project
hardware spike yet — worth a one-minute listen at the crossover the first time LR2 runs live. LR4
keeps plain positive weights (`Copy: LFE=LFE+0.5*L+0.5*R` for 2.1): the LFE summed feed weights
each of N main channels at `1/N` — the standard bass-redirection convention that bounds worst-case
(every redirected source simultaneously full-scale and in phase) to exactly unity gain, generalised
from the roadmap's own 0.5+0.5 two-channel example. `sub_trim_db=0.0` (or an unset main trim) stores as `preamp_db=None`,
matching `ChannelSection`'s existing "no Preamp: line" convention — a spinner left at its default
never touched produces no line, not a `Preamp: 0.0 dB` line. Distance-to-delay is direct manual
centimetre entry (`time_alignment.py` has no physical-distance input to reuse), rebased so the
FARTHEST channel gets 0 ms and every closer channel gets a positive top-up — the opposite reference
point from `time_alignment.measure_channel_delays()`'s own earliest-arrival rebase (see 23.3), because
here the physically-correct reference is "the channel that already arrives latest and cannot be sped
up," not "the channel that happened to be measured as arriving first." Safety preamp: evaluates
`preamp_calc.calculate_auto_preamp()` across EVERY composed section (each main's HP + trim, the sub's
LP + trim + a flat `+20*log10(2) ≈ +6.02 dB` summed-feed headroom cost) and takes the minimum
(most conservative) as the single profile-level preamp — the same "recompute the global preamp as the
min across composed sections" convention `tone_trim.py`/`loudness_layer.py` already establish.

**23.3 Sign bug in `time_alignment.measure_channel_delays()` — found by S23, CONFIRMED and FIXED
2026-07-13.** S23 worked the function's then-selftest fixture
(`test_measure_channel_delays_rebases_to_earliest`: L arrives first, R +10 ms, LFE +20 ms; function
returned delay 0/10/20 ms) through by hand as if those values were written directly into `Delay:`
lines — the gap between channels DOUBLED instead of closing to zero (L stays at t=3000, R becomes
480+3480=3960, LFE becomes 960+3960=4920 in the fixture's own sample units). The function's own
docstring said delay should "push the early channels back to match the latest one," which is the
opposite of what the code computed (rebased to the EARLIEST arrival at 0 ms, adding MORE delay onto
channels that already arrive later, rather than rebasing to the LATEST arrival). The fix rebases to
the LATEST arrival (`latest - offset` instead of `offset - earliest`), matching the docstring's
stated rationale and `bass_management._distance_delays_ms()`'s farthest-gets-0 convention (23.2);
the tests were renamed/rewritten (`test_measure_channel_delays_rebases_to_latest`, plus a new
end-to-end `test_measure_channel_delays_applying_result_aligns_arrivals` asserting arrival + delay
converges to a common instant for every channel). NOTE: S23's "not called from anywhere in the
shipped UI" claim was wrong — the editor's Measure button (`editor_panel.
_on_measure_delay_clicked()`, Rumble 4.9, see the module list above) has always fed
`run_measurement()`'s output straight into per-section `Delay:` values, so the bug WAS live in a
shipped feature; anyone who used Measure before 2026-07-13 got delays that widened the channel
spread and should re-measure.

**23.4 Wizard (D3b, S24): `rumble/ui/bass_management_dialog.py`, Tools -> Bass Management…, enabled
only when the active profile's layout includes `SUB_CHANNEL`** (reuses `editor_panel.
layout_channel_tokens()`/`detect_preset()` — the editor's own channel-preset detection, factored out
into free functions this session specifically so this dialog and `MainWindow`'s menu-enablement check
can both call it without a live `EditorPanel` instance). Page flow: layout confirm -> crossover
(spinner, 40-160 Hz practical range, THX's published 80 Hz convention as the default) -> alignment
(LR2/LR4) -> per-channel trims -> distances (manual cm/ms entry only) -> preview (live
`manager.preview_config()`, bracketed the same "un-bypass, preview, restore on every exit path"
shape `measure_dialog.py`'s S15 verify-pass preview uses) -> Apply, which emits
`apply_requested(profile_id, ProfileConfig)` rather than writing a profile file directly — `MainWindow.
_on_apply_bass_management()` loads it into the editor as a dirty, unsaved change through the normal
save flow, the same shape `_on_bake_tone_trim` established (the dialog itself
never touches `editor_panel`). Per the S23->S24 Amendment's own documented fallback, "use measured
distances" is NOT wired: no Campaign C measurement data exists anywhere a wizard could reach (grepped —
`measure_channel_delays()`/`run_measurement()` are not called from any shipped UI, and
`measure_dialog.py` stores only single-position frequency-response curves, never multi-channel timing
data), independently of 23.3's blocking sign-bug finding. The distances page shows a disabled "Use
measured distances" button with an explanatory `describe()` note instead of hiding the affordance
entirely, so the gap is visible rather than silent. `ProfileConfig.preamp` is set directly to
`BassManagementResult.preamp_db` (never recomputed) and existing `Copy:` lines starting with
`LFE=` are replaced (not duplicated) on a repeat wizard run, so applying the wizard twice in a row to
the same profile is idempotent.

---

## §24 — `Convolution:` is cumulative: the double-convolution bug (found and FIXED 2026-07-13)

**24.1 The upstream semantic.** Equalizer APO's documentation says a `Convolution:` line "**Adds** a
convolver" — the same additive wording as `Filter:` and `VSTPlugin:`, and unlike `Preamp:`/`Device:`/
`Stage:`, which *set* a value. Two `Convolution:` lines that reach the same audio therefore convolve
the signal twice, in series. In the frequency domain that squares the transfer function, i.e. **every
dB of the correction is applied at exactly 2x** (a -16.8 dB point becomes -33.6 dB). Nothing warns:
the config parses, APO loads it, and the audio is simply twice as corrected as designed.

**24.2 What RUMBLE was emitting.** `ProfileManager.create_profile_with_convolution()` writes multi-rate
IR profiles (AutoEQ ships a 44.1/48 kHz minimum-phase pair) as an `If: sampleRate == …` conditional so
APO picks the matching file at runtime. It ALSO set the top-level `ProfileConfig.convolution` field,
under a docstring claiming that field was *"used only by RUMBLE's own preview/export bookkeeping —
actual runtime device matching is decided by the conditional block."* **That was false.**
`apo_config.render_config_body()` writes `config.convolution` out as a real, unconditional
`Convolution:` line (lines 733-734). The rendered profile was:

```
Convolution: irs/foo_48000.wav          <- unconditional: ALWAYS convolves
If: sampleRate == 44100
  Convolution: irs/foo_44100.wav
ElseIf: sampleRate == 48000
  Convolution: irs/foo_48000.wav        <- and again
Else:
  Convolution: irs/foo_48000.wav
EndIf:
```

The conditional's `Else:` branch is exhaustive — *some* branch always runs (§0.3) — so the bare line
was never a fallback for anything. It was pure duplication, and every AutoEQ-imported headphone
profile ran its correction at double strength. Measured on the user's real active profile (HIFIMAN
Sundara Closed-Back, the one affected profile of 12): 1 kHz -9.61 -> -19.23 dB, 16 kHz -16.81 ->
-33.61 dB. No clipping — that IR is all-attenuation (in-band peak -0.20 -> -0.39 dB) — so the failure
was inaudible-as-a-defect and audible only as "this profile is too aggressive."

**24.3 The fix, and the invariant it enforces.** Multi-rate profiles now carry the conditional
**only**; single-rate profiles keep their plain unconditional line. The invariant, pinned in
`selftest_convolution_import.py` against the RENDERED body (not the object graph — the bug lived
entirely in rendering, so an object-level assertion would have missed it): *the body APO reads has
exactly one `Convolution:` line per executable path, and a profile with a conditional convolution has
no unconditional one.*

Because the IR path now lives inside opaque conditional-block text rather than in a field, every
consumer that asks "which IR is this profile's?" has to look in both places. Two helpers in
`apo_config` centralise that, and `ProfileManager._primary_convolution()` is the single answer:

- `conditional_convolution_paths(profile)` — every IR path any branch convolves, in order.
- `fallback_convolution_path(profile)` — the `Else:` branch's IR, i.e. the profile's **declared
  default-rate** file. This, not `paths[0]`, is the right rate-agnostic answer: the branches are
  emitted in ascending rate order, so `paths[0]` is whichever rate sorts first (44.1 kHz), which is
  not the default.

Fixed consumers: `resolve_convolution_ir()`, `_resolve_ir_response()` (the §5 preamp-headroom /
§0.2 response-math path — note it read the bare field, so pre-fix it modelled the IR **once** while
APO applied it twice: headroom was computed against a curve half as deep as the one actually
running, and post-fix it would have seen no IR at all had it not been repointed at
`_primary_convolution()`), `_has_real_eq()` (the APO-import filter, which was scoring a
conditional-only config as "no audible shaping, not worth importing"), and `export_profile_bundle()`,
which now (a) refuses to re-add the bare line to a conditional profile and (b) bundles **every**
branch's IR, not just the default — a bundle carrying only `foo_48000.wav` silently broke on any
machine running at 44.1 kHz. `_rehome_bundle_irs()` is the import-side mirror: it extracts and
repoints IRs referenced inside conditional blocks, which the old import path ignored.

**24.4 Repairing an already-written profile.** Profiles are plain text on disk, so a profile written
by the buggy code stays broken until rewritten. The one affected file was repaired in place by
deleting the bare line (backed up first to `<apo config>/rumble/backups/`, per the no-permanent-delete
rule; `backups/` is outside the `user/`+`defaults/` scan roots, so it is not re-scanned as a profile).
No migration shipped: RUMBLE 1.0 has no existing installed base to migrate.

**24.5 The transferable lesson.** This is the *third* bug in this codebase (with §17.6's combined-`L R`
and §17.7's `Channel:`-less hole) where a **docstring asserted a behaviour the code did not have**, and
the assertion is what let it survive review — here, a comment stating a field was inert bookkeeping
when a `render_*` function 200 lines away wrote it straight into the output. When a field is claimed
to be non-emitting, grep the renderer.

## §25 — Window/dialog construction: frameless chrome (campaign closeout, Session S6)

**25.1 Every top-level window is frameless.** MainWindow and all ~19 dialog classes under
`rumble/ui/*.py` carry `Qt.WindowType.FramelessWindowHint` — there is no native OS titlebar anywhere in
the app. This was a deliberate, user-approved campaign (`docs/superpowers/plans/
2026-07-15-frameless-chrome-progress.md`), not an accident of one dialog; a new window that still has
a native titlebar is a bug, not a style choice.

**25.2 How to build a new dialog.** Subclass `rumble.ui.frameless.FramelessDialog`, not `QDialog`
directly. `FramelessDialog.__init__` builds a chrome strip (drag region + title label synced to
`setWindowTitle()` + a close button) above `self.body` — a plain `QWidget` with no layout of its own.
Content goes on `self.body`, not `self`: `QVBoxLayout(self.body)` (or `QHBoxLayout`), never
`QVBoxLayout(self)`. `self.close()` from the chrome's close button resolves through whatever
`closeEvent`/`reject()` override the dialog defines, exactly as a native titlebar X would — several
dialogs (`HearingSuiteDialog`, `MeasureDialog`, `ABXDialog`, `BassManagementDialog`) rely on this to
stop live audio/preview on close, so never bypass `self.close()` with a direct `self.hide()` or similar.

**25.3 Every dialog is fixed-size, permanently.** No dialog in the app is user-resizable — a frameless
window has no native resize grip, and layering one on afterward was judged out of proportion to this
feature. New dialogs should call `setFixedSize(width, height + CHROME_STRIP_HEIGHT)` (import
`CHROME_STRIP_HEIGHT` from `rumble.ui.frameless`) rather than `setMinimumSize`/`resize()` — the latter
either allow programmatic resizing that never gets exercised in practice, or (per `resize()` alone)
leave Qt's real on-screen size unclear until measured. If a dialog holds variable-length content
(a log, a report, a rule list), give that content its own internal scroll container (`QScrollArea`,
`QPlainTextEdit`, `QTableWidget`/`QListWidget`'s native scrollbars) rather than growing the fixed
window to fit worst-case content.

**25.4 Measure the real on-screen size, don't trust `sizeHint()`/`resize()` alone.** Two converted
dialogs (`CrossoverDialog`, `MeasureDialog`) had a real on-screen size larger than either their
`sizeHint()` or their own `resize()` call suggested: `sizeHint()` can report a stale pre-layout
estimate for word-wrapped labels before the layout has converged on a real width, and a
`QStackedWidget` aggregates minimum-size constraints across *every* page it holds, not just the
current one — so a dialog can render larger than its constructor's `resize()` target even before any
page navigation happens. When fixing a dialog's size, open it (or construct it and call `show()` +
`processEvents()`) and read `.size()` after the fact, across every page/state it can be in — don't
lock in a number computed before layout.

**25.5 QMessageBox/QInputDialog stay native — permanently, not a gap.** Every stock Qt dialog type
(`QMessageBox.information/question/warning`, `QInputDialog.getText`, etc.) used throughout the app
keeps its native OS frame. Theming every built-in Qt dialog type to match the custom chrome was
judged disproportionate to this feature's scope; this is a permanent decision, not deferred work — do
not "finish the job" by wrapping these in `FramelessDialog` later without a fresh decision to do so.

