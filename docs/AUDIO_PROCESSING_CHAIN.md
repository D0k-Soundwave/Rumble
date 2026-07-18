# RUMBLE Audio Processing Chain and Maths Reference

Every entity between a playback application and the listener's ear that RUMBLE
either controls, generates content for, measures, or compensates around --
what it is, the rules and variables that govern it, the mathematics behind it,
and how it integrates with every other entity in the chain.

Companion documents: `RUMBLE_SPECIFICATION.md` (product spec),
`PROFILE_SPEC.md` / `PROFILE_FILE_CONSTRUCTION.md` (profile file format),
`RUMBLE_Headroom_Maths.md` (the preamp derivation in prompt-ready form),
`eq_apo_entity_schema.json` (machine-readable entity/relationship schema),
`RUMBLE_FEATURE_MAP.md` (feature-to-code map).

---

## 1. The end-to-end chain, entity by entity

```
 1. Playback application (any: player, browser, game)
 2. Windows audio session (WASAPI, per-app volume/mute)
 3. Windows audio engine mix (shared-mode mix format: the sample rate /
    bit depth / channel count set on the endpoint -- device_format.py reads
    this; every filter downstream runs at this rate)
 4. Equalizer APO (a system-wide Audio Processing Object attached to the
    endpoint; loads config.txt, live-reloads on file change, processes
    audio as float at the mix format's sample rate and channel layout)
 5. config.txt                      <- owned by RUMBLE (managed marker block)
 6.   Include: rumble_active.txt    <- the single pointer RUMBLE rewrites
 7.     rumble_active.txt           <- either a plain Include of the active
                                       profile, a composed render (when any
                                       render layer is active), or a
                                       per-device dispatch (Device: blocks)
 8.       rumble/<user|defaults>/<id>.txt   <- the canonical profile file
 9. The profile body: Preamp, Filters, GraphicEQ, Convolution, Delay,
    Copy, Channel sections, VSTPlugin, Eval, If/Else blocks
10. Render layers RUMBLE composes on top at activation/preview time:
    listener correction -> tone trim -> ISO 226 loudness -> loudness
    matching, then a crossfeed block appended as raw text
11. Master volume (WASAPI endpoint volume, read in dB by master_volume.py --
    input to the loudness layer, downstream of APO's processing)
12. DAC / amplifier / transducer (headphone or speaker)
13. The room (for speakers: measured by the ESS measurement chain)
14. The listener's ears (measured by Test A / Test B; corrected per-ear)
```

Entities 5-10 are text files RUMBLE writes; everything RUMBLE does to sound
is expressed as Equalizer APO config text -- RUMBLE performs no DSP of its
own on the playback path. Entities 11-14 are measured or compensated for by
the features in section 9. Exclusive-mode (bit-exact) streams bypass the
Windows audio engine and therefore bypass APO and everything RUMBLE does.

---

## 2. Equalizer APO: rules and variables

### 2.1 File and syntax rules

- Config files are plain UTF-8 text, one command per line, `key: value`.
- `#` starts a comment; APO ignores comment lines (RUMBLE's profile
  metadata header lives entirely in comments for this reason).
- Files are re-read live whenever they change on disk -- writing
  `rumble_active.txt` is what makes a profile audible instantly.
- `Include:` splices another file in place; include chains are how
  config.txt -> rumble_active.txt -> profile file works.
- Commands scope to the channels selected by the most recent `Channel:`
  line; before any `Channel:` line a command applies to all channels.

### 2.2 Command reference (all commands RUMBLE models)

| Command | Form | Rules |
|---|---|---|
| `Preamp` | `Preamp: -6.5 dB` | Flat gain. Multiple preamps on the same channel selection sum in dB. Rendered by RUMBLE to one decimal place (0.1 dB grid). |
| `Filter` | `Filter 1: ON PK Fc 1000 Hz Gain -3.0 dB Q 1.41` | One biquad. `ON`/`OFF` toggles; types below. `Q` or `BW Oct` variants both parse. |
| `Filter` (raw IIR) | `Filter: ON IIR Order n Coefficients b0 b1 ... a1 a2 ...` | Arbitrary IIR coefficients, carried and graphed by RUMBLE, not editable band-by-band. |
| `GraphicEQ` | `GraphicEQ: 20 0; 100 -2; 20000 0` | Breakpoint list `freq gain; ...`. APO interpolates linearly in dB over log-frequency. Multiple GraphicEQ lines in one section stack: RUMBLE combines them exactly at the union of breakpoints. |
| `Convolution` | `Convolution: path.wav` | Convolves with an impulse response WAV. APO does NOT resample the IR: an IR recorded at a different rate than the device plays back wrong (RUMBLE's `check_rate_mismatch()` warns). |
| `Delay` | `Delay: 3.5 ms` or `Delay: 168 samples` | Per selected channels. RUMBLE preserves the samples form verbatim. |
| `Copy` | `Copy: XL=R XR=L` or `Copy: L=0.5*L+0.5*R` | Channel routing/mixing with linear coefficients; can create virtual channels (crossfeed's XL/XR). |
| `Channel` | `Channel: L R` / `Channel: all` | Selects the channels subsequent commands apply to. RUMBLE parses consecutive same-channel sections into one ordered `ChannelSection`. |
| `Device` | `Device: Speakers FIIO; all` | Gates following lines to matching endpoints. Word-wise, case-insensitive matching against the device/connection name; `;` separates alternatives; `all` matches everything. Basis of per-device dispatch. |
| `Stage` | `Stage: pre-mix` / `post-mix` / `capture` | Where in the pipeline the config applies. RUMBLE models the first Stage line; later ones are preserved verbatim. |
| `Eval` | `Eval: var = expression` | Expression variables usable in later commands. RUMBLE hoists Eval lines to just after Device so definitions precede use (a variable reassigned mid-file sees only its last value -- documented divergence, PROFILE_FILE_CONSTRUCTION.md sec. 3.8). |
| `If/ElseIf/Else/EndIf` | `If: sampleRate == 48000` | Conditional blocks (e.g. rate-conditional convolution paths). RUMBLE parses the structure, preserves content verbatim, and uses it for rate-switched IR selection. |
| `Include` | `Include: rumble_active.txt` | File splice. RUMBLE emits its own include pointers and recognises them as self-owned when auditing config.txt. |
| `LoudnessCorrection` | `LoudnessCorrection: State ...` | Upstream APO's own volume-linked loudness heuristic (hand-tuned, NOT ISO 226 -- verified against its source). Parsed as an ordered token-pair list so unknown keys round-trip. RUMBLE's own loudness layer (section 5.4) is the ISO 226 implementation. |
| `VSTPlugin` | `VSTPlugin: path.dll` + `VSTPlugin Chunks: <blob>` | Hosts a VST2 plugin. The Chunks preset blob is opaque (written by APO's Configurator); RUMBLE tracks/relinks paths but never decodes chunks. |

Anything RUMBLE does not structurally model is preserved verbatim in
`extra_lines` and re-emitted at the end of the body.

### 2.3 Filter types

| Type | Meaning | Parameters |
|---|---|---|
| `PK` / `PEQ` / `Modal` | Peaking (bell) | Fc, Gain, Q or BW |
| `LP` / `LPQ` | Low-pass (2nd order, 12 dB/oct) | Fc (LPQ adds Q) |
| `HP` / `HPQ` | High-pass (2nd order, 12 dB/oct) | Fc (HPQ adds Q) |
| `BP` | Band-pass | Fc, Q |
| `LS` / `LSC` | Low shelf (LS fixed-slope, LSC with Q) | Fc, Gain, (Q) |
| `HS` / `HSC` | High shelf (HS fixed-slope, HSC with Q) | Fc, Gain, (Q) |
| `NO` | Notch | Fc, Q |
| `AP` | All-pass (phase only) | Fc, Q |
| `IIR` | Raw coefficients | Order, coefficient list |

RUMBLE caps a profile at `MAX_BANDS = 32` filters per channel section (a
deliberate UI/complexity ceiling, not an APO limit).

### 2.4 Core parameters and variables

- **Fc (Hz)** -- centre/corner frequency. Graph and grids span 10 Hz -
  24 kHz (`GRAPH_FMIN`/`GRAPH_FMAX`, 512 log-spaced points); audible-band
  work uses 20 Hz - 20 kHz.
- **Gain (dB)** -- band gain. Cascaded filters multiply in linear
  magnitude, therefore ADD in dB -- the single most load-bearing rule in
  the whole chain (headroom, GEQ->PEQ, layer composition all rest on it).
- **Q / BW (octaves)** -- two parameterisations of the same width (maths
  in section 6.3).
- **Preamp (dB)** -- flat gain; multiple lines sum per channel.
- **Sample rate** -- set by the endpoint mix format, not the config.
  RUMBLE's default assumption is `DEFAULT_SAMPLE_RATE = 48000` Hz, replaced
  by the real endpoint rate (`device_format.py`) whenever it is readable.
  Biquad responses warp with rate (section 6.3); GraphicEQ lines do not.
- **Channel identifiers** -- `L R C LFE RL RR SL SR` (7.1 superset),
  layouts from mono through 7.1 as in `eq_apo_entity_schema.json`;
  `all` selects everything; `Copy:` may reference virtual names.
- **Delay (ms or samples)** -- `ms` is rate-independent; `samples` scales
  with the mix rate.
- **Eval variables** -- e.g. `sampleRate`, usable in `If:` conditions
  (rate-conditional convolution) and expressions.

---

## 3. The profile file (the unit everything else moves)

A RUMBLE profile is ONE portable UTF-8 text file that is simultaneously the
stored profile and a valid APO config -- comment-line metadata header, then
the raw APO command body:

```
# RUMBLE Profile: <Name>
# Description: <one line>
# Tags: <comma separated>
# Created: YYYY-MM-DDTHH:MM:SS
# Modified: YYYY-MM-DDTHH:MM:SS
# Readonly: true                    (only when user-locked)
# Builtin: true                     (only on protected built-ins)
# Fit Sample Rate: 48000            (only on curve-fitted profiles)
# Created by RUMBLE — EQ Profile Manager for Equalizer APO

Preamp: -5.0 dB
Filter 1: ON PK Fc 1000 Hz Gain -3.0 dB Q 1.41
...
```

Rules: keys case-insensitive; partial headers and header-less foreign APO
configs parse fine (absent fields report as None and the file is migrated to
the full header in place on load); only the leading comment block is scanned
for metadata; provenance keys (`APO Source`, `APO Source Hash`,
`Import Signature`) let the boot reconcile keep APO-config imports in sync
without duplicating.

`Fit Sample Rate` is the integration hook for curve-fitted profiles: biquads
warp with sample rate while a GraphicEQ residual line does not, so the
residual only cancels exactly at the stamped rate -- its presence is what
authorises the manager to re-solve the residual when the device rate
changes, and its absence is what protects hand-authored GraphicEQ layers
from ever being rewritten.

---

## 4. Ownership chain and safety entities

- **Managed config.txt** -- RUMBLE replaces `config.txt` with a marker
  block (`# RUMBLE-Managed: 1`) plus exactly one line:
  `Include: rumble_active.txt`. The marker is the source of truth for
  ownership.
- **Sacred backup** -- the user's original config.txt is saved
  byte-for-byte as `config.txt.rumble_backup` on first takeover and never
  touched again; "Hand back to APO" and the uninstaller restore it.
- **Drift files** -- if config.txt is overwritten after RUMBLE owned it, or
  a foreign tool writes alongside the block, the displaced content is
  preserved as `config.txt.rumble_drift-<timestamp>` and ownership is
  re-asserted; the drift-restore dialog offers the content back.
- **Foreign tool detection** -- `Include:` targets outside RUMBLE's own
  set (e.g. Peace's `peace.txt`) and non-comment directives written
  directly into config.txt are surfaced as warnings
  (`_foreign_includes()` / `_foreign_raw_directives()`).
- **Scratch files** (all under the APO config dir, all disposable):
  `rumble_active.txt` (active pointer), `rumble_preview.txt` (live
  preview), `rumble_listener_active.txt` (composed render output),
  `rumble_device_<hash>.txt` (per-device dispatch branches).

---

## 5. The render pipeline (what actually reaches APO)

When no render layer is active, `rumble_active.txt` is a single `Include:`
of the canonical profile file -- the profile IS the render. When any layer
is active, RUMBLE composes a render at activation/preview time; profile
storage is NEVER mutated by any layer.

Composition order (`ProfileManager._apply_render_layers()`):

```
base profile
  -> 5.1 listener correction   (per-ear; may split an "all" section to L/R)
  -> 5.2 session tone trim     (symmetric)
  -> 5.3 ISO 226 loudness      (symmetric, volume-linked)
  -> 5.4 loudness matching     (flat preamp delta, always last)
then
  -> 5.5 crossfeed block       (raw text appended after the rendered body)
  -> 5.6 per-device dispatch   (wraps the result in Device: blocks)
```

Layers 5.1-5.3 are additive dB curves merged with
`autoeq_merge.sum_curves()`, so their relative order does not change the
physical result -- only which section shapes get expanded first. Each layer
re-runs the anti-clipping preamp maths (section 7) over the composed result,
so boosts added by a layer never introduce clipping.

### 5.1 Listener correction (weak-ear-only, per-ear)

Input: the active Listener Slot's averaged Test A imbalance curve (per-ear
threshold differences, section 9.10). Derivation (`derive_correction`):
boost ONLY the weaker ear at frequencies where it lags
(`CORRECTION_MAX_BOOST_DB = 6.0`, `CORRECTION_MAX_CUT_DB = 0.0` -- never
cut, never touch the better ear). The curve is applied to the L or R
section (expanding a linked stereo/"all" section into L and R when needed)
via `sum_curves`, and the preamp is re-derived.

### 5.2 Session tone trim (bass / treble / tilt)

Session-scoped taste knob, never persisted (dies with the process) but
bakeable into a profile on request. Curve (`derive_trim_curve`): a bass
shelf below `BASS_SHELF_HZ = 250 Hz`, a treble shelf above
`TREBLE_SHELF_HZ = 4000 Hz` (each clamped to +-`MAX_SHELF_DB = 6.0` dB),
plus a straight tilt of up to `MAX_TILT_DB_PER_OCT = 3.0` dB/oct, evaluated
on a 20 Hz - 20 kHz log grid; net boost capped at `MAX_BOOST_DB = 6` dB and
net cut at `MAX_CUT_DB = 12` dB.

### 5.3 ISO 226 loudness layer (volume-linked)

The real equal-loudness implementation (upstream APO's own
`LoudnessCorrection:` is a hand-tuned heuristic, verified against its
source -- not ISO 226).

- ISO 226:2003 Table 1 gives, at 29 tabulated frequencies, coefficients
  (alpha_f, L_U, T_f) from which `equal_loudness_spl(f, phon)` computes the
  SPL needed for that frequency to sound as loud as a `phon` dB SPL 1 kHz
  tone. By definition `SPL(1000 Hz, phon) == phon`. Valid phon range 0-90
  (inputs clamped, never extrapolated).
- Compensation curve for listening at `current_phon` instead of
  `reference_phon`:

  ```
  curve(f) = [equal_loudness_spl(f, current) - current]
           - [equal_loudness_spl(f, reference) - reference]
  ```

  Zero at 1 kHz by construction; positive (boost) at bass/treble when
  listening quieter than reference. Boost capped at 6 dB, cut at 12 dB.
- The phon values come from the volume link: reference level is calibrated
  once (the "conversation level" flow anchors
  `REFERENCE_CLIP_TARGET_PHON = 75` phon to a real WASAPI volume reading),
  then the current WASAPI master volume (`master_volume.py`, the same
  `IAudioEndpointVolume` source upstream APO uses) maps the live volume to
  `current_phon`. A settings-driven poller re-renders when volume changes.

### 5.4 Profile-switch loudness matching

A louder profile sounds "better" purely from level; this layer removes that
bias when enabled. `abx.estimated_gain_db()` estimates the composed config's
broadband gain (power-domain average of the combined response across the
log grid -- equivalent to the RMS change pink noise would show, the correct
proxy for program material) plus its preamp. The layer applies
`preamp -= (estimate - 0 dB)` -- reduction only, never boost -- anchored at
0 dB so every profile also matches EQ-off (bypass does not jump loudness).
Tolerance 0.1 dB (APO renders preamps at one decimal). Cached against the
rendered body text so volume-poller ticks do not re-run the estimate.

### 5.5 Headphone crossfeed (appended block)

APO-native text (`Copy:`/`Channel:`/`Filter:`/`Delay:`), no DSP of RUMBLE's
own, validated on real hardware. Block shape:

```
Copy: XL=R XR=L            (virtual cross channels)
Channel: XL XR
Preamp: <gain dB>          (the feed level)
Filter 1: ON LP Fc <f> Hz  (only presets with a low-pass corner)
Delay: <d> ms              (only presets with an interaural delay)
Copy: L=L+XL R=R+XR        (mix the feeds back in)
```

This second `Copy:` must render AFTER the `Channel:` section, which the
structured renderer cannot express -- hence raw-text append, never
round-tripped through `ProfileConfig`.

Presets (all cited circuits):

| Mode | Feed gain | LP corner | Delay |
|---|---|---|---|
| Chu Moy | -6.0 dB | 700 Hz | none |
| Jan Meier | -9.5 dB | 650 Hz | none |
| Linkwitz-style | -4.5 dB | 700 Hz | 300 us |
| Bauer (bs2b default) | -4.5 dB | 700 Hz | none |
| Direct Blend | -6.0 dB | none | none |
| Custom | user gain (-20..0 dB) | user (200-2000 Hz) | user (0-690 us; capped at the Woodworth interaural-time-difference ceiling for an average adult head) |

Strength (0-100%) interpolates the feed toward unity in the LINEAR voltage
domain (`20*log10` of the interpolated linear gain), so 100% reproduces the
preset exactly and 0% renders nothing. Custom always renders its own exact
figures. Applies only when the layout carries both L and R
(`can_crossfeed`); C/LFE/surrounds pass untouched. "Insert into profile"
writes the current block into the profile as portable text and disables the
machine-level setting so it is not applied twice.

### 5.6 Per-device dispatch

With device-profile assignments set, `rumble_active.txt` becomes a dispatch:
per assigned endpoint, a `Device: <pattern>` block including that device's
composed scratch file (`rumble_device_<8-char-hash>.txt`), with unassigned
devices falling through to the global active profile. Device matching is
word-wise, case-insensitive friendly-name matching (never GUID equality --
WASAPI ids and APO GUIDs are not byte-identical). An automatic profile
switcher (`auto_switch.py`) can also change the global active profile when
the default endpoint changes.

---

## 6. Core filter mathematics

All parametric filter maths follows the Audio EQ Cookbook (Robert
Bristow-Johnson). Implemented in `eq_math.py` (graph + headroom), reused by
ABX level matching, GEQ->PEQ conversion, crossover design and the loudness
layers -- one implementation, never duplicated.

### 6.1 Biquad coefficients

Intermediate values at sample rate Fs:

```
w0    = 2*pi*Fc/Fs        cosw0 = cos(w0)      sinw0 = sin(w0)
alpha = sinw0 / (2*Q)
A     = 10^(gain_dB/40)   sqrtA = sqrt(A)      (gain types only)
```

| Type | b0, b1, b2 | a0, a1, a2 |
|---|---|---|
| PK | `1+alpha*A`, `-2*cosw0`, `1-alpha*A` | `1+alpha/A`, `-2*cosw0`, `1-alpha/A` |
| LP | `(1-cosw0)/2`, `1-cosw0`, `(1-cosw0)/2` | `1+alpha`, `-2*cosw0`, `1-alpha` |
| HP | `(1+cosw0)/2`, `-(1+cosw0)`, `(1+cosw0)/2` | `1+alpha`, `-2*cosw0`, `1-alpha` |
| BP | `alpha`, `0`, `-alpha` | `1+alpha`, `-2*cosw0`, `1-alpha` |
| NO | `1`, `-2*cosw0`, `1` | `1+alpha`, `-2*cosw0`, `1-alpha` |
| AP | `1-alpha`, `-2*cosw0`, `1+alpha` | `1+alpha`, `-2*cosw0`, `1-alpha` |
| LSC | `A*((A+1)-(A-1)*cosw0+2*sqrtA*alpha)`, `2*A*((A-1)-(A+1)*cosw0)`, `A*((A+1)-(A-1)*cosw0-2*sqrtA*alpha)` | `(A+1)+(A-1)*cosw0+2*sqrtA*alpha`, `-2*((A-1)+(A+1)*cosw0)`, `(A+1)+(A-1)*cosw0-2*sqrtA*alpha` |
| HSC | `A*((A+1)+(A-1)*cosw0+2*sqrtA*alpha)`, `-2*A*((A-1)+(A+1)*cosw0)`, `A*((A+1)+(A-1)*cosw0-2*sqrtA*alpha)` | `(A+1)-(A-1)*cosw0+2*sqrtA*alpha`, `2*((A-1)-(A+1)*cosw0)`, `(A+1)-(A-1)*cosw0-2*sqrtA*alpha` |

Fixed-slope `LS`/`HS` (6 or 12 dB/oct, no Q parameter) are approximated
with fixed Q values chosen to resemble 1st/2nd-order shelving
(`_FIXED_SLOPE_Q`); APO's internal implementation may differ slightly --
the graph promises a close visual approximation.

### 6.2 Magnitude evaluation

With `phi = 2*pi*f/Fs`, for a quadratic `c0 + c1*z^-1 + c2*z^-2`:

```
mag2(c0,c1,c2) = c0^2 + c1^2 + c2^2
               + 2*(c0*c1 + c1*c2)*cos(phi) + 2*c0*c2*cos(2*phi)

gain_dB(f) = 10 * log10( mag2(b0,b1,b2) / mag2(a0,a1,a2) )
```

(The a0 normalisation cancels in the ratio.) Raw `IIR` filters evaluate the
same way over their full coefficient lists.

### 6.3 Q and bandwidth conversions

Analog-prototype (rate-independent) forms:

```
BW_oct = (2/ln 2) * asinh( 1/(2Q) )
Q      = sqrt(2^BW) / (2^BW - 1)
```

Exact digital forms account for bilinear frequency warping near Nyquist
with `warp = w0 / sin(w0)` (w0 as above):

```
BW_exact = BW_analog / warp        Q_exact = from BW_exact inverse
```

At low Fc/Fs the warp factor approaches 1 and the forms agree.

### 6.4 GraphicEQ maths

APO interpolates GraphicEQ breakpoints linearly in dB over log10 frequency:

```
geq_dB(f) = g1 + (g2-g1) * (log10 f - log10 f1) / (log10 f2 - log10 f1)
```

holding the first/last gain outside the breakpoint range. Multiple
GraphicEQ lines in one section stack; RUMBLE combines them exactly at the
union of their breakpoints (no invented frequencies). The same log-linear
interpolation convention is used by every curve consumer in the codebase
(`autoeq_merge`, correction layers, mic cal).

### 6.5 Combined response

The full-chain response at f is the dB SUM of: every enabled filter's
`gain_dB(f)`, the GraphicEQ contribution, the convolution IR's measured
response (section 9.14), and the preamp -- evaluated on the 512-point
log grid for the graph and a >= 480-point grid for headroom.

---

## 7. Headroom: the anti-clipping preamp

Full derivation and validation data in `RUMBLE_Headroom_Maths.md`; summary:

1. Compute the combined response (section 6.5, excluding preamp) on a
   dense 20 Hz - 20 kHz log grid; find `combined_peak` (and its frequency,
   reported as the preamp basis).
2. ```
   preamp = -(combined_peak + margin)   if combined_peak > 0 else 0.0
   margin = 0.5 dB                       normally
          = 1.0 dB                       if any boost region exceeds ~6 dB, or
                                         3+ simultaneous boosts of >= 4 dB
   ```
3. Quantised to APO's 0.1 dB rendering grid; recomputed on every filter or
   GraphicEQ change; disabled filters excluded.

The escalation trigger is empirical, not folklore: the true worst-case gain
of an LTI filter is the L1 norm of its impulse response, which exceeds the
steady-state `combined_peak` most when SEVERAL sizeable boosts sit at
different frequencies (measured: +2.5 dB gap for 2 separated 4 dB boosts,
+4.8 dB for 5) -- while against real program material (pink-noise-like) the
gap never materialised. The 1.0 dB escalated margin matches broadcast
true-peak practice (EBU R128 / ATSC A/85) rather than chasing the
adversarial case. Every render layer re-runs this after composing its
curve, so layers can never introduce clipping; bass management adds its own
`20*log10(2) = 6.02 dB` headroom term for the worst-case coherent L+R sum
into the sub feed.

---

## 8. Integration rules that cut across everything

- **One curve shape.** Every correction-like entity -- AutoEQ results,
  Spinorama responses, mic cals, hearing imbalances, loudness contours,
  tone trims -- is a `list[(freq_hz, gain_db)]` sorted by frequency.
  Merging is `sum_curves()` on the union grid with log-linear
  interpolation; no frequency points are ever invented.
- **Layer, don't merge.** Machine/session state (correction layers,
  crossfeed, trim) composes at render time; profile files stay portable
  and untouched. Explicit "bake/insert into profile" actions are the only
  way session state becomes profile content.
- **dB addition.** Cascades add in dB (section 6.5); every composition,
  conversion and headroom rule derives from this.
- **Sample rate flows downhill.** The endpoint mix format sets the rate;
  biquads warp with it, GraphicEQ lines and `ms` delays do not, IRs are
  not resampled. `Fit Sample Rate`, `If: sampleRate` conditionals, and the
  device-format warnings all exist to manage this asymmetry.
- **Reduction-only loudness.** Level-matching entities (loudness matching,
  ABX level match) only ever attenuate, so they can never clip.

---

## 9. Feature reference (every audio feature, its function and maths)

### 9.1 Profile editor and live graph
Band list (<= 32 bands), per-channel sections, GraphicEQ editor, raw config
view, undo/redo, live preview via `rumble_preview.txt`. The graph renders
`eq_math.combined_response` (section 6) at the device's real sample rate on
512 log points, 10 Hz - 24 kHz; Q/BW toggle uses section 6.3.

### 9.2 AutoEQ headphone import
Downloads AutoEQ's published results (127-point standard grid), merges
multiple measurements of the same model with quality-weighted averaging
(`autoeq_merge`), optionally decomposes the curve into PK bands plus a
GraphicEQ residual solved at the device rate (stamped `Fit Sample Rate`).
Consensus view surfaces measurement disagreement.

### 9.3 Speaker correction (Spinorama)
`eq_target.py`: correction = target - measured Listening Window, where
`target(f) = slope_db_per_oct * log2(f / pivot_hz)` (slope 0 = flat
anechoic; ~ -0.9 dB/oct approximates a preferred in-room response).
Fractional-octave smoothed (default 1/3 oct; adaptive smoothing widens from
1/12 oct below 6 kHz to 2 oct above 8 kHz with log-interpolated blend
between), boost-capped, band-limited with a soft taper so driver roll-off
and ultrasonic rise are not boosted, plus an HF guardrail.

### 9.4 REW import
Parses REW-exported filter sets into canonical bands; foreign comment
lines preserved.

### 9.5 GEQ ladders and GEQ->PEQ conversion
10/15/31-band ISO ladders. Conversion (`geq_peq_convert`): each point's PK
bandwidth spans the log-midpoints to its neighbours (via `bw_to_q`), then a
bounded Newton-Raphson pass corrects skirt overlap by driving each band's
centre-frequency combined response (dB-summed, exactly as APO stacks) to
its target -- the system is diagonally dominant, so a handful of iterations
converge. Residual divergence between grid points is reported, not hidden.

### 9.6 Crossover designer
Linkwitz-Riley presets built by cascading RBJ Butterworth-2 sections
(APO's LP/HP is itself one 12 dB/oct biquad): LR4 = two sections at
Q = 1/sqrt(2); LR8 = the Butterworth-4 pair taken twice, section Qs from
the pole-angle formula `theta_k = (2k-1)*pi/(2N)`, `Q_k = 1/(2 cos theta_k)`.
LR alignments sum flat (0 dB) at every frequency by construction.

### 9.7 Bass management
Generates profile content (not a render layer): high-pass crossovers on the
mains, low-pass plus a summed feed (`Copy: LFE=LFE+0.5*L+0.5*R` style) on
the sub channel `LFE`, per-channel level trims, distance-derived delays at
`SPEED_OF_SOUND = 343 m/s` (34.3 cm per ms -- the furthest speaker gets
0 ms, nearer ones are delayed by the distance difference), and a safety
preamp including the 6.02 dB coherent-sum term. Merged through the normal
editor/dirty flow.

### 9.8 Time alignment (measured)
Plays a calibrated log sweep per channel, records at the listening
position, and finds each channel's arrival offset with GCC-PHAT
(phase-transform-weighted cross-correlation -- robust to level and
coloration). Relative offsets become per-channel `Delay:` values, measuring
true end-to-end latency (driver + DSP + acoustic) that distance maths alone
cannot see. Pure-numpy measurement core; hardware shell injected.

### 9.9 Room measurement (ESS)
Farina exponential sine sweep: equal energy per octave; FFT deconvolution
with the matched inverse filter yields the impulse response, with harmonic
distortion products of order k landing at fixed NEGATIVE time offsets
`-T*ln(k)/ln(f2/f1)` -- strictly before the causal linear IR, so taking the
causal window discards them. Magnitude extraction and time gating follow.
Mic capture records through the APO-hooked device; capture and playback
plumbing live in `rumble/core/audio/`.

### 9.10 Hearing test suite and listener slots
- **Test A (Ear Calibration)**: per-ear adaptive staircase thresholds at
  11 standard audiometric frequencies (125 Hz - 16 kHz; 5-frequency coarse
  baseline first), levels clamped to -90..-10 dBFS, results normalised and
  flagged when the interaural gap exceeds 6 dB. Volume-reference clip and
  real-world reference sounds anchor the absolute level; volume-drift
  notes invalidate stale runs.
- **Test B (digit screening)**: digit-triplet-in-noise SRT (SNR for 50%
  correct) with a symmetric-step staircase; referral threshold per the
  standard digit-triplet convention. Screening only -- it feeds advice,
  not correction curves.
- **Listener slots**: named per-listener storage, 10-run retention, one
  averaged "effective" imbalance view that the correction layer (5.1) and
  exports read.

### 9.11 ISO 226 loudness and calibration
Section 5.3. Also exposed as a one-shot "bake" and in settings UI; the
75-phon conversation-level calibration ties the phon scale to the user's
real volume scale.

### 9.12 Tone trim
Section 5.2; bakeable via `bake_session_trim()`.

### 9.13 Crossfeed
Section 5.5.

### 9.14 Convolution and IR analysis
`ir_analysis.py` reads IR WAVs with its own RIFF walker (PCM 16/24/32,
float 32/64, extensible, any channel count averaged -- stdlib `wave`
rejects float WAVs), computes the absolute FFT magnitude in dB
(power-domain fractional-octave smoothed, sampled onto the log grid,
including overall gain -- exactly what headroom needs; no normalisation, no
resampling), caches per (path, mtime), and warns on IR-vs-device rate
mismatch since APO does not resample IRs. Conditional (`If: sampleRate`)
convolution paths select per-rate IRs.

### 9.15 VST hosting
APO hosts the plugin; RUMBLE's `vst_registry` audits paths library-wide,
relinks moved DLLs everywhere at once, and scans folders for candidates.
`VSTPlugin Chunks:` blobs are opaque and carried untouched.

### 9.16 Blind A/B/X testing
Level-matches candidate configs first (section 5.4's estimator; only ever
reducing the louder ones to the quietest) so loudness cannot masquerade as
quality, then runs the blind trial via the preview pipeline.

### 9.17 Spectrum analyzer and level meters
Display-only entities fed by loopback capture. `SpectrumShaper`:
interpolation to the display grid, pink-noise tilt compensation (+3 dB/oct
so pink noise reads flat), exponential smoothing, peak-hold decay;
"Source (estimated)" subtracts the known EQ response to estimate the
pre-EQ signal. `LevelMeter`: VU ballistics from the standard 300 ms/99%
convention (time constant `0.3/ln(100)` ~= 65 ms, symmetric attack/release),
peak mode with instant attack and 20 dB/s decay, K-System reference modes
(K-12/-14/-20 at -12/-14/-20 dBFS), clip light at -0.1 dBFS, -60 dBFS
floor, green/yellow/red bands at -10/-3 dBFS. Every constant is derived
from a named standard, never hand-picked.

### 9.18 Devices, formats and switching
`device_format.py` reads the true endpoint mix format (WASAPI shared-mode);
mismatch warnings cover IR rates and fitted profiles. Per-device dispatch
and auto-switch: section 5.6. Channel-layout presets (stereo through 7.1)
drive the editor's per-channel UI and the layout warnings (e.g. bass
filters on LFE only).

### 9.19 Peace import
Reads Peace's saved presets and converts them to canonical profiles
(gains/frequencies/Q mapped onto standard bands).

### 9.20 Preview, bypass, solo/AB
Preview writes the composed candidate to `rumble_preview.txt` and points
config.txt at it temporarily; bypass renders a flat config; solo/AB flips
between candidates through the same pipeline, so what you hear is always
exactly what activation would produce.

### 9.21 Backup / restore / trash
Full-tree zip of `data/` plus the APO `rumble/` tree (zip-slip guarded,
per-member decompression byte caps against zip bombs: 8 MiB text, ~234 MiB
IR -- both derived from RUMBLE's own ceilings). Deleted profiles go to a
25-entry recycle bin (`data/trash/`) before purge.

---

## 10. Quick constants table

| Constant | Value | Where |
|---|---|---|
| Max filter bands per section | 32 | `apo_config.MAX_BANDS` |
| Default sample rate | 48000 Hz | `apo_config.DEFAULT_SAMPLE_RATE` |
| Graph grid | 512 pts, 10 Hz - 24 kHz | `eq_math` |
| Preamp margin | 0.5 dB (1.0 escalated) | `preamp_calc` |
| Margin escalation | boost > ~6 dB, or 3+ boosts >= 4 dB | `preamp_calc` |
| Preamp/level quantum | 0.1 dB | APO rendering grid |
| Listener correction cap | +6 dB boost, 0 cut | `listener_correction` |
| Loudness layer caps | +6 dB boost, -12 dB cut | `loudness_layer` |
| Reference calibration | 75 phon | `loudness_layer` |
| ISO 226 valid range | 0-90 phon, 29 freqs | `iso226` |
| Tone trim shelves | 250 Hz / 4 kHz, +-6 dB, +-3 dB/oct tilt | `tone_trim` |
| Crossfeed delay cap | 690 us (Woodworth ITD ceiling) | settings UI |
| Speed of sound | 343 m/s (34.3 cm/ms) | `bass_management` |
| Coherent-sum headroom | 20*log10(2) = 6.02 dB | `bass_management` |
| VU time constant | 0.3/ln(100) ~= 65 ms | `level_meter` |
| Peak decay | 20 dB/s | `level_meter` |
| K-System refs | -12/-14/-20 dBFS | `level_meter` |
| Hearing test freqs | 125 Hz - 16 kHz (11) | `hearing_test` |
| Imbalance flag threshold | 6 dB | `hearing_test` |
| Loudness match anchor/tolerance | 0 dB / 0.1 dB | `profile_manager` |
| Trash retention | 25 entries | `profile_manager` |
| Listener run retention | 10 runs per slot | `listener_slots` |
