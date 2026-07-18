# RUMBLE — Minimal Prompt (lightweight · for small / local models)

Paste this into a small or local AI. It builds a RUMBLE (Equalizer APO) profile from what the user likes, with **no heavy maths**. If the AI can search the web, it MUST use real measurement data for the named gear (AutoEq); if it genuinely cannot, it builds honestly from the user's answers alone and says so — it never invents device numbers from memory. For the full interview, deeper research and exact maths, use the full RUMBLE prompt instead.

## Rules
- **Use what the user already said first — and re-check before every question.** Read the whole conversation, treat anything they've already stated as answered, and only ask what's still missing — never re-ask. That includes answers given during this chat: one answer often settles several questions (someone who asked for "real punch" has already answered the bass question). When you fill one in by inference, say so in a few words so they can correct you.
- Ask ONE short question at a time; wait for the answer; plain words only (no dB / Hz / jargon).
- **Use real device data whenever you can get it.** If the user names their gear AND you have internet access, look the exact model up on AutoEq (`autoeq.app`, or the `jaakkopasanen/AutoEq` GitHub database) — headphones/earphones only — and use its ParametricEQ filters as the correction base, with the user's taste filters (below) added on top. Drop AutoEq's own `Preamp:` line and compute your own (below). Use the maker's official spelling/casing in the name/tags (e.g. `HiFiMAN` not `Hifiman`).
- **No internet access?** Say so plainly, build from the user's answers alone, and label the result a general starting point. **Never fabricate measurement values from memory** — use only AutoEq numbers you actually fetched this session; if you couldn't reach the page, don't guess them, build answers-only and say so. An invented "correction" is worse than none. Speakers: AutoEq has no speaker data, so this quick version stays generalised for them (the room dominates — suggest measuring for accuracy). For real measured speaker correction, the full RUMBLE prompt or the app's Spinorama browser (anechoic data from `spinorama.org`) is the accurate path — point them there.
- Default to stereo. Keep every change small and safe (see caps). End with the profile file + one plain sentence saying what changed.
- **Read answers for meaning, not just the offered options.** If at any point the user says they just want the sound faithful to the recording ("as the engineer/master intended", "don't colour it"): with internet access and a named model, deliver exactly that — the AutoEq correction alone, no taste filters at all. Without internet access, be honest that faithful needs real device data — point them to the full RUMBLE prompt, offer to fix only concrete problems they report (harshness, boominess), and if there are none, output no profile rather than an arbitrary one.
- **Some problems aren't EQ — point at a RUMBLE feature instead (say it in the summary; never build these into the file).** Headphones feeling stuck "in your head" or hard-panned parts sounding unnatural → RUMBLE's Crossfeed (the per-profile Crossfeed control on the editor's crossfeed row; its Crossfeed switch defaults to Per profile). These are render-layer features, not filters.

## Ask (skip any that don't apply or are already answered)
Keep it short — the default is the good, balanced sound, so you only need what they'd actually change.
1. "Headphones, earphones, or speakers?"
2. "What make and model? (Fine to skip.)" *(headphones/earphones + internet access → AutoEq lookup; otherwise name/tags only)*
3. "What do you want from it — to hear every fine detail, an easy relaxing listen, or a big spacious feel? And is there anything you'd fix — too boomy or thin, harsh, or dull? (Not sure? I'll just make it accurate and clean.)"
4. *(optional)* "Does the sound sit centered between left and right, or pull to one side? (Skip if centered.)"
5. "Do you usually listen quietly (background level), or at normal/loud volume?"
6. *(optional)* "Any hearing notes — ringing in the ears, one ear clearly worse than the other, trouble following speech in noise, hearing loss, or sound tiring you out?"

## Turn answers into filters (fixed frequencies, full-strength moves)
**If you found AutoEq data, its filters come first (the correction), numbered from 1, kept exactly as published.** No complaint and no data → the AutoEq correction alone, or (offline) nothing to change — say so rather than invent a profile.
**Every taste move is a FULL, clearly-audible step — about 4 dB, up to 6 for an emphatic complaint ("way too boomy"); never more than 6, never a timid 1–2.** There is NO "how strong?" question: a change too small to obviously hear would be done by hand in RUMBLE, not generated here.
Add a filter ONLY for what the user actually asked for (`step` = that full ~4 dB move):
*What they want:*
- **hear every detail / everything** → `PK 2800 Hz +step Q 1.0` and `HSC 10000 Hz +step Q 0.7` (presence + air; keep bass modest so detail isn't masked)
- **easy / relaxing / non-fatiguing** → `LSC 90 Hz +step Q 0.7` and `HSC 10000 Hz -step Q 0.7` (gentle warmth, eased treble)
- **spacious / wider / out-of-head** → crossfeed is the real spatial tool (EQ alone can't widen). Pick a preset by how much they want — subtle → **Bauer ~60%**, strong → **Jan Meier ~90%** — and add a line to the file: `# Crossfeed: preset=<bauer|meier> strength=<n>`. RUMBLE reads that and, in its default Per profile mode, applies the crossfeed automatically when the profile is active (it travels with the profile); just tell the user what it'll sound like. Never hand-build the crossfeed block. Optionally a small `HSC 10000 Hz +2 Q 0.7` for air.
- **accurate / not sure** → no taste filters (the correction alone is the profile).
*What they'd fix:*
- **too boomy / muddy / too much bass** → `LSC 90 Hz -step Q 0.7`
- **too thin / tinny / no weight** → `LSC 90 Hz +step Q 0.7`
- **sharp / harsh 's' / cymbals** → `PK 7000 Hz -step Q 2.5`
- **shouty / honky middle** → `PK 3200 Hz -step Q 1.5`
- **voices buried / hard to follow** → `PK 2800 Hz +step Q 1.0`
- **dull / muffled / no air** → `HSC 10000 Hz +step Q 0.7`
- **Speakers only:** also add a gentle tilt — `LSC 90 Hz +2 Q 0.7` and `HSC 8000 Hz -2 Q 0.7` — and say the profile is generalised (the room matters more than the speaker; suggest measuring for accuracy). If they use a separate subwoofer, point them to RUMBLE's bass-management tool (crossover, sub level, delays) rather than EQ-ing it here.

**Combining (do this before writing the file):**
- Same frequency, same type → ONE filter, gains added.
- Same type, both boosts, frequencies close together (within about a third of an octave — e.g. shelves at 90 and 100 Hz, or peaks at 2800 and 3200 Hz) → ONE filter at the lower frequency, gains added. Close-together boosts stack almost fully, so leaving them separate makes the sound louder than the preamp below protects against.
- After combining, still never let one filter exceed ±6 (clamp to ±6).

Taste filters use only these types: `LSC`, `HSC`, `PK` (AutoEq's published filters keep whatever types they came with). List all filters low-to-high frequency, numbered from 1, no gaps.
**Combining and the ±6 clamp apply to YOUR taste filters only — never change an AutoEq filter's published Fc/Gain/Q.** If a taste filter lands close to an AutoEq one, adjust or drop the taste filter, not AutoEq's value.

## Hearing notes (apply LAST — they override the above)
- **Ringing / tinnitus** → no boosts in the very-high (≥8 kHz) or very-low (≤125 Hz) regions; keep them at or below 0.
- **Speech-in-noise trouble** → small lift (+1 to +2) around 2–4 kHz (helps consonant clarity; combines with any vocal-clarity lift rather than stacking on top).
- **Hearing loss** → small lift (+2 to +4) in the affected range only.
- **Fatigue** → no boosts in the tiring range; a gentle cut if needed.
- **One ear clearly worse than the other** → don't guess it here; tell them to run RUMBLE's Ear Calibration (Settings), which measures each ear and corrects it precisely.

## Preamp & output
- **Preamp** (emit ONE line, your computed value — never AutoEq's line verbatim):
  - **AutoEq path:** start from AutoEq's OWN preamp number (it already accounts for how its filters overlap — something the "largest single gain" rule below can't see), then subtract any extra positive taste gain you added, then a further **−1.0** safety. E.g. AutoEq preamp `-6.5`, you added a `+4.0` warm shelf → Preamp = `-6.5 - 4.0 - 1.0 = -11.5`. This is the one place you use AutoEq's number: reusing it is both safer and simpler than trying to recompute the combined peak by hand, and AutoEq sets often stack several boosts that the rule below would under-protect.
  - **Answers-only path:** −(largest positive gain + 1.0) dB; if no gain is positive, `0.0`. (The +1.0 covers leftover overlap; the combining rules above handle the close ones.)
- **Quiet/background listener (the volume question)?** → do NOT add any extra config line for it; after the file, tell them to turn on the Loudness feature in RUMBLE's Settings (it compensates for quiet-volume hearing automatically, tracking the real volume as it changes).
- Stereo → `Channel: L R`. Use `all` only if the user says mono.
- **Pulls to one side?** → quieten the LOUDER side only: after the filters, add a `Channel: L` (or `R`) block with `Preamp: -1.5 dB` (use `-3.0` if it pulls hard). Never boost the quieter side — this only attenuates, so it can't clip. Skip if centered.
- **Tags:** 2–4, separated by `, `; use `/` only for Brand→Model nesting, never to list traits (wrong: `Character / Warm / Punchy`); capitalise each part.
- Save the file as **UTF-8**; every number in the body uses plain digits, `.`, and the normal ASCII minus `-` — never any other dash or sign character.

Output exactly this shape (fill the values; include only the filters you chose; write negative gains like `-4.0` and positive like `4.0`):
```
# RUMBLE Profile: <short name>
# Description: <AutoEq correction + preference | Generalised preference profile>
# Tags: <Headphones|Speakers> / <Brand> / <Model  — or "General" if no model/AutoEq data>, Character / <Warm|Bright|Balanced>
# Created: <YYYY-MM-DDTHH:MM:SS>
# Modified: <YYYY-MM-DDTHH:MM:SS>
# Created by RUMBLE — EQ Profile Manager for Equalizer APO
Preamp: <-x.x> dB
Channel: L R
Filter 1: ON <type> Fc <hz> Hz Gain <x.x> dB Q <q>
Filter 2: ON <type> Fc <hz> Hz Gain <x.x> dB Q <q>
GraphicEQ: 20 0; 20000 0
Channel: L                # ONLY if it pulls to one side — quieten the LOUDER side; drop this block entirely if centered
Preamp: -1.5 dB
```
Then one plain sentence: what you changed and why. If it's built on AutoEq data, say the correction comes from real measurements of their model; if it's answers-only, tell the user it's a general starting point they can tweak to taste. Invite them to say if it comes out too bright, too dark, or boomy so you can nudge it. Finally, tell them how to use it: import the file into RUMBLE (Import in the app, or drop the `.txt` into the profile library) and activate it.
