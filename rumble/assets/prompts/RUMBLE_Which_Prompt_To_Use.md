# Which RUMBLE Prompt Should I Use?
### A plain-English guide to matching the two RUMBLE prompts to your AI

RUMBLE comes with two versions of the profile-builder prompt. They both do the same job — interview you and hand back an Equalizer APO profile — but they're sized for different kinds of AI. **Rule of thumb: use Universal unless your AI is a small or offline model.**

**Two terms, briefly:**
- **Context window** — how much the AI can read and keep "in mind" at once (prompt + your answers + any research). Bigger is better. It's usually measured in *tokens* (a token ≈ ¾ of a word).
- **Web access** — whether the AI can search the internet. The Universal prompt uses this to look up your gear's measurements.

## Quick pick

| If your AI is… | Use this prompt |
|---|---|
| A web-connected assistant (e.g. ChatGPT, Gemini, Copilot), including free tiers | **Universal** |
| A small model running on your own PC / offline (e.g. Llama, Mistral, Qwen, Gemma, Phi, DeepSeek) | **Minimal** |

When in doubt, use **Universal** — it's the best all-round fit.

## The two prompts

**Universal — `RUMBLE_Universal_AI_Prompt.md`**
Everything RUMBLE can do: device-accurate correction, online gear research, measurement fitting, speaker time-alignment, the optional tone-control layer, and the 10-band export.
- *Best on:* any capable web-connected assistant, including lighter and free tiers.
- *Needs:* web access (to research your gear) and good maths — ideally the ability to run code.
- *Rough size:* ~6,000-token prompt; a full run with research uses more, so you want a mid-size context window ("16k and up").
- *You get:* the most accurate, fully-featured profile.

**Minimal — `RUMBLE_Minimal_Prompt.md`**
A stripped-down, preference-only builder. No internet, no measurements, no heavy maths — it asks a handful of simple questions and produces a sensible general-purpose profile from presets.
- *Best on:* small models you run yourself or offline, or any AI without web access.
- *Needs:* nothing special — basic question-answering only.
- *Rough size:* ~1,000-token prompt; fits a tiny context window ("4k" is plenty).
- *You get:* a good general starting point — **not** a measured per-device correction. The file says so, and you can fine-tune by ear afterwards.

## How to tell if your AI can handle Universal

Use Universal if your AI can do all three:
1. **Search the web** — so it can find your gear's published measurements.
2. **Do careful maths** — ideally run code; otherwise be a strong reasoner. This is what gets the loudness/anti-clipping value and the filter numbers right.
3. **Hold a long conversation** without forgetting your earlier answers.

If it can't browse, or it's a small offline model, use **Minimal**. (A capable model *without* web access can still run Universal if you paste in a measurement file — otherwise it'll build from your description rather than exact data, which is less precise.)

## Quick tips
- **Give the AI web access** if it has the option — it makes Universal noticeably more accurate.
- **Turn on code-running** if your AI offers it; the maths becomes reliable even on smaller models.
- **If a profile sounds wrong** — too quiet, or distorting on loud parts — the model probably fumbled the maths. Switch to an AI that can run code, or use Minimal for a simpler result.
- **Not sure?** Use **Universal**.
