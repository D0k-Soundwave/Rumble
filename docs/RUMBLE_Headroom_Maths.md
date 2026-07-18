# RUMBLE — Headroom (Preamp) Maths
### How the prompts calculate the anti-clipping preamp value

**The idea.** Digital clipping happens at whatever frequency the *combined* boost of all filters is highest. So the preamp attenuates everything by that peak (plus a small safety margin), bringing the loudest point back to or below 0 dBFS. A positive preamp is never used — it's `0.0` or negative.

The **Full** and **Compact** prompts compute this from the true combined response (below). The **Minimal** prompt uses a simpler approximation because small/local models can't reliably run the full curve.

---

## Full / Compact — combined-curve method

### Step 1 — one filter's gain in dB at a frequency `f`
Build the biquad (Robert Bristow-Johnson "Audio EQ Cookbook") from the filter's `Fc`, `Q`, `gain_dB`, and the playback sample rate `Fs` (default 48000 Hz):

```
w0    = 2*pi*Fc/Fs
cosw0 = cos(w0)
sinw0 = sin(w0)
alpha = sinw0 / (2*Q)
A     = 10^(gain_dB/40)      # peaking & shelves only
sqrtA = sqrt(A)
```

Coefficients by filter type (`b` = numerator, `a` = denominator):

```
PK : b0=1+alpha*A   b1=-2*cosw0   b2=1-alpha*A    a0=1+alpha/A  a1=-2*cosw0  a2=1-alpha/A

LSC: b0=A*((A+1)-(A-1)*cosw0+2*sqrtA*alpha)   b1=2*A*((A-1)-(A+1)*cosw0)   b2=A*((A+1)-(A-1)*cosw0-2*sqrtA*alpha)
     a0=(A+1)+(A-1)*cosw0+2*sqrtA*alpha        a1=-2*((A-1)+(A+1)*cosw0)     a2=(A+1)+(A-1)*cosw0-2*sqrtA*alpha

HSC: b0=A*((A+1)+(A-1)*cosw0+2*sqrtA*alpha)   b1=-2*A*((A-1)+(A+1)*cosw0)  b2=A*((A+1)+(A-1)*cosw0-2*sqrtA*alpha)
     a0=(A+1)-(A-1)*cosw0+2*sqrtA*alpha        a1=2*((A-1)-(A+1)*cosw0)      a2=(A+1)-(A-1)*cosw0-2*sqrtA*alpha

LP : b0=(1-cosw0)/2  b1=1-cosw0     b2=(1-cosw0)/2   a0=1+alpha a1=-2*cosw0 a2=1-alpha
HP : b0=(1+cosw0)/2  b1=-(1+cosw0)  b2=(1+cosw0)/2   a0=1+alpha a1=-2*cosw0 a2=1-alpha
BP : b0=alpha        b1=0           b2=-alpha        a0=1+alpha a1=-2*cosw0 a2=1-alpha
NO : b0=1            b1=-2*cosw0    b2=1             a0=1+alpha a1=-2*cosw0 a2=1-alpha
AP : b0=1-alpha      b1=-2*cosw0    b2=1+alpha       a0=1+alpha a1=-2*cosw0 a2=1-alpha
```

The squared magnitude of `c0 + c1*z^-1 + c2*z^-2` at `f` (with `phi = 2*pi*f/Fs`):

```
mag2(c0,c1,c2) = c0^2 + c1^2 + c2^2
               + 2*(c0*c1 + c1*c2)*cos(phi)
               + 2*(c0*c2)*cos(2*phi)

gain_dB(f) = 10 * log10( mag2(b0,b1,b2) / mag2(a0,a1,a2) )
```

(The `a0` normalisation cancels in the ratio, so there's no need to divide the coefficients first.)

### Step 2 — the GraphicEQ contribution at `f`
APO interpolates GraphicEQ points **linearly in dB across a logarithmic frequency axis**. Between adjacent points `(f1,g1)` and `(f2,g2)`:

```
geq_dB(f) = g1 + (g2 - g1) * (log10(f) - log10(f1)) / (log10(f2) - log10(f1))
```

Below the first point hold `g_first`; above the last point hold `g_last`. A flat GEQ (`20 0; 20000 0`) returns 0 everywhere.

### Step 3 — combine and find the peak
Cascaded filters multiply in linear magnitude = **add in dB**. Evaluate the total on a dense log grid from 20 Hz to 20 kHz (≥ 480 points):

```
total_dB(f)   = ( Σ gain_dB_i(f) over all ENABLED filters )  +  geq_dB(f)
combined_peak = max( total_dB(f) over the grid )      # also record the frequency, for preamp_basis
```

### Step 4 — the preamp value
```
preamp = -(combined_peak + margin)   if combined_peak > 0
       =  0.0                          otherwise

margin = 0.5 dB   (default, sample-peak)
       = 1.0 dB   if the combined boost exceeds ~6 dB anywhere, or there are several
                  simultaneous sizeable boosts (guards inter-sample / "true" peaks)
```

Recompute the preamp whenever **any** filter or the GraphicEQ changes. Disabled (`OFF`) filters are excluded.

#### "Several simultaneous sizeable boosts" — where that trigger comes from

`combined_peak` is a steady-state sinusoidal magnitude (the gain the filter settles to if fed an infinite tone). The actual worst-case peak gain of an LTI filter over *any* signal bounded to ±1 is a standard linear-systems result — the L1 norm of the impulse response, `sum(|h[n]|)` — which is `>= combined_peak`'s linear equivalent, with equality only for a non-oscillating (e.g. plain low-pass) impulse response. A resonant peaking filter rings, so its L1 norm can exceed `combined_peak`; that gap is the real, provable version of "inter-sample risk."

Measured directly against RUMBLE's own RBJ biquad cascade (2026-07-07 audit):
- **A single boost's gap is driven by *gain*, not Q.** At a fixed 4 dB gain the gap sits ~0.85 dB fairly flat from Q=1 to Q=8 — a *wide* Q=0.3 boost measured a *bigger* gap (1.7 dB) than any narrow one tested. Q is not a useful trigger on its own.
- **The count of simultaneous sizeable boosts is what actually drives the worst case**, especially when they sit at different frequencies: 2 separate 4 dB/Q2 boosts measured a +2.5 dB gap, 3 measured +3.6 dB, 5 measured +4.8 dB (vs. only +1.9 dB for 5 boosts stacked at one frequency).
- Against **real** (not adversarial) program material — pink noise, RUMBLE's own stated proxy for music's long-term spectral density — none of this ever mattered: measured peaks fell 2–20 dB *short* of what `combined_peak` alone predicts, even for 5 stacked steep boosts, because the worst-case gap above only bites for a signal phase-aligned to the filter's ringing (an impulse/transient/hard edge), which incoherent broadband content essentially never produces.

Designing for the full adversarial worst case (up to ~4.8 dB for 5 separated boosts) would waste real loudness protecting against inputs that don't occur. The industry's actual practice for this exact tension — real transient outliers over long listening, without chasing the adversarial case — is broadcast/mastering "true peak" headroom (EBU R128 / ATSC A/85), which settles on ~1 dBTP. That's why the escalated margin stays at 1.0 dB; what changed is the *trigger*: **3 or more simultaneously enabled boosts of ≥4 dB, regardless of Q** (`_SIZEABLE_BOOST_GAIN_DB` / `_SIZEABLE_BOOST_COUNT` in `preamp_calc.py`) — matching what the data shows actually drives the risk, instead of a Q condition that measurably doesn't.

### Why the whole curve, not just the biggest filter
Two boosts that overlap in frequency stack on top of each other, so the real peak can be higher than any single filter alone.

*Example:* `+3 dB @ 2000 Hz` and `+2 dB @ 2200 Hz`, both fairly broad, overlap to roughly `+4.5 dB` where they meet. The correct preamp is `-(4.5 + 0.5) = -5.0 dB`. Reading only the largest single filter would have given `-3.5 dB` — and still clipped by ~1.5 dB.

---

## Minimal — simple approximation

The Minimal prompt does **not** build the combined curve. It takes the largest single positive filter gain and adds a fixed cushion:

```
preamp = -(largest positive filter gain + 1.0)   if any gain is positive
       =  0.0                                      otherwise
```

The `+1.0 dB` cushion (versus `0.5` in the full method) is deliberate: because it doesn't account for overlap, the extra half-decibel covers the case where broad presets stack. It is slightly conservative — occasionally a touch quieter than strictly necessary — but it will not clip, which is the right trade for a model that can't compute the full curve.

---

## Quick reference

| | Full / Compact | Minimal |
|---|---|---|
| Basis | combined response of all filters + GraphicEQ | largest single positive filter gain |
| Margin | 0.5 dB (1.0 if boost > ~6 dB or 3+ simultaneous ≥4 dB boosts) | 1.0 dB (fixed) |
| Formula | `preamp = -(combined_peak + margin)`, else 0.0 | `preamp = -(max_positive_gain + 1.0)`, else 0.0 |
| Accuracy | exact to the realised response | safe, slightly conservative |

*Source: biquad formulas — Robert Bristow-Johnson "Audio EQ Cookbook"; GraphicEQ interpolation — Equalizer APO (linear-in-dB over log-frequency).*

---

## Optional future enhancement: 4× oversampled true-peak detection

(RUMBLE_ACTION_GUIDE.md #2.4.) The escalating-margin approach above — a
steady-state combined-response peak plus a calibrated 0.5/1.0 dB margin,
justified against EBU R128 / ATSC A/85 true-peak practice in the "several
simultaneous sizeable boosts" section above — is a solid, pragmatic solution
and does **not** need to change urgently. This is a "nice to have," not a fix.

If more rigor is ever wanted: a true 4× oversampled true-peak detector —
matching broadcast-standard true-peak metering exactly (EBU R128 / ATSC A/85's
own method: interpolate the signal 4×, measure the *interpolated* peak, not
just the sample-rate peak) — would remove the empirical-margin approximation
entirely and replace it with a direct measurement of the actual worst-case
inter-sample peak, rather than the current L1-norm-adjacent heuristic. This
would need: (1) a real rendered audio buffer to run the oversampled peak
detector against, not just filter coefficients (the current method is pure
maths over the combined response curve, with no signal involved), and (2)
meaningfully more compute than the current steady-state evaluation, paid once
per preamp recalculation rather than continuously. Not scheduled — recorded
here so a future session can pick it up without re-deriving the option from
scratch.
