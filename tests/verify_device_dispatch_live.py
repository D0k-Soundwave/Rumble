"""Manual, human-in-the-loop hardware spike (Session S18, RUMBLE adaptive
front-end campaign, roadmap section D1) establishing Equalizer APO's real
`Device:` dispatch semantics before D1a builds per-device profile dispatch
on top of an assumption.

D1's design assumes: a config file can carry several `Device: <pattern>`
scoped blocks (one per assigned endpoint) followed by an UNSCOPED trailing
block as the fallback for every endpoint with no explicit assignment, and
that APO's own "a Device: line's scope runs until the next Device: line"
rule means each block only ever touches its own endpoint. This script
builds exactly that three-block shape on the REAL Equalizer APO config and
listens with the user on two real APO-hooked outputs to confirm or refute
it, per protocol 1.3's hardware-spike rule ("confirm on real hardware
before building UI").

Structural note: `apo_config.ProfileConfig` models exactly ONE `device`
field -- there is no way to express three separate Device: blocks in a
single ProfileConfig (the same "the data model doesn't have a slot for
this shape" situation S6's verify_crossfeed_live.py hit with its
post-Channel Copy: line). So this script renders three independent
ProfileConfig objects with `apo_config.render_config_body()` and
concatenates their text directly -- the raw-text route the S6 KB entry
flagged as the fallback when ProfileConfig can't express something.

Since `ProfileManager.preview_config()` only accepts a single ProfileConfig
(same one-`device`-field limitation), this script cannot use it either. It
instead writes the concatenated text to a throwaway scratch file under
`ProfileManager.rumble_dir` and points the real active-pointer file
(`rumble_active.txt`) at it via `apo_config.render_include()` -- exactly the
Include-file indirection `preview_config()`/`_write_active_pointer()` use
internally, just built by hand for a shape they don't support. The
pointer's original bytes are read before the swap and restored verbatim in
a `finally` block (every exit path, including an exception mid-listening),
and the scratch files are deleted on the way out -- the same restore
guarantee `ABXRunner`/`preview_config()` give, applied manually because the
three-block shape can't go through them.

Test signal: pink noise (`tones.generate_pink_noise`), not a pure sine --
a Low-Pass/High-Pass/Peak filter's effect on a single sine tone is nearly
inaudible unless the tone sits exactly at the filter's corner, whereas the
same filter reshapes broadband noise into an unmistakable timbre change
(muffled / thin / bright) any listener can identify without training.

Three distinct markers, chosen so any of "scoped-only", "scoped+fallback
stacked", or "fallback superseded the scoped block" is identifiable by ear:
  Device A block:  LP  500 Hz  -> "muffled, no highs"
  Device B block:  HP 4000 Hz  -> "thin, no bass/mids"
  Fallback block:  PK +12 dB @ 8000 Hz -> "bright treble sizzle"
A stacked result sounds like the device's own marker PLUS a treble sizzle
on top; a superseded result sounds like the fallback's sizzle alone with
none of the device's own muffled/thin character. A second config (fallback
marker alone, `Device: all`, no per-device blocks) is played on the same
two devices afterward as a direct reference for "what does the fallback
marker sound like by itself" -- without that reference, "stacked" and
"superseded" can be hard to tell apart from a single listen.

What this script can and cannot confirm on its own: it can confirm the
process doesn't crash, that the pointer/Include indirection round-trips
(if nothing audibly changes when it should, that itself means Include:
indirection or `Device:` dispatch isn't working -- worth calling out
explicitly), and it narrates exactly what should be audible at each phase.
It CANNOT confirm what was actually heard -- that needs the user, present
at real hardware, listening and reporting back.

Findings from running this with the user go into
docs/RUMBLE_APP_SPECIFIC_KB.md as a new dated subsection, and into
docs/superpowers/plans/2026-07-12-adaptive-frontend-progress.md.

Not part of the automated gate (tests/run_all_tests.py) -- it plays audible
sound, requires two real APO-hooked playback endpoints, and needs a human
to interpret the result, so it is listed in that script's EXCLUDE set.

Pacing (revised live during the S18 run itself, twice): the first version
used a fixed timed countdown, which the user could not react to in time
(no live view of a background tool's console output, so a 3s -- then 10s --
timer was already running before they could see it). This version instead
waits for a real Enter keypress before every single tone, with the exact
output device and exact listening cue printed immediately before the
prompt, and lets the user replay any single tone or any whole pair of
phases on demand until satisfied. Because that needs a live stdin the user
types into directly, THIS SCRIPT MUST BE RUN BY THE USER DIRECTLY IN THEIR
OWN TERMINAL WINDOW -- not invoked through an agent's tool call, which has
no live keyboard channel to the user.

Run directly:  python tests/verify_device_dispatch_live.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, apo_path, audio_devices
from rumble.core.audio import tones
from rumble.core.audio.engine import AudioError, ToneEngine, find_output_device
from rumble.core.crossover import BUTTERWORTH_Q
from rumble.core.profile_manager import ProfileManager, ACTIVE_POINTER_FILENAME, RUMBLE_SUBDIR

TONE_LEVEL_DBFS = -20.0
# 10s (revised live during the S18 spike run itself, at the user's explicit
# request): pink noise's spectral shape is judgeable almost immediately once
# actually listening, but 10s gives comfortable margin to characterize
# timbre rather than a tight minimum.
NOISE_DURATION_S = 10.0
GAP_S = 1.0

SCRATCH_DISPATCH = "rumble_verify_device_dispatch.txt"
SCRATCH_FALLBACK_ONLY = "rumble_verify_device_dispatch_fallback_only.txt"

# Device A marker: Low-Pass, comfortably below pink noise's midband -- an
# unmistakable "muffled, bass-only" character with no highs at all.
DEVICE_A_LP_HZ = 500.0

# Device B marker: High-Pass, comfortably above pink noise's midband -- an
# unmistakable "thin, tinny" character with no bass/mids at all.
DEVICE_B_HP_HZ = 4000.0

# Fallback marker: a Peak boost far from both A's and B's corners, so it
# reads as a distinct "bright treble sizzle" whether heard alone, stacked
# on top of A's muffle, or stacked on top of B's thinness.
FALLBACK_PK_HZ = 8000.0
FALLBACK_PK_GAIN_DB = 12.0
FALLBACK_PK_Q = 1.0


def build_device_block(pattern: str, filt: apo_config.Filter) -> apo_config.ProfileConfig:
    section = apo_config.ChannelSection(channel="all", filters=[filt])
    return apo_config.ProfileConfig(device=pattern, channel_sections=[section])


def render_dispatch(pattern_a: str, pattern_b: str) -> str:
    block_a = build_device_block(
        pattern_a, apo_config.Filter(type="LP", fc=DEVICE_A_LP_HZ, q=BUTTERWORTH_Q))
    block_b = build_device_block(
        pattern_b, apo_config.Filter(type="HP", fc=DEVICE_B_HP_HZ, q=BUTTERWORTH_Q))
    block_fallback = build_device_block(
        "all", apo_config.Filter(type="PK", fc=FALLBACK_PK_HZ,
                                  gain=FALLBACK_PK_GAIN_DB, q=FALLBACK_PK_Q))
    return "\n".join([
        apo_config.render_config_body(block_a),
        apo_config.render_config_body(block_b),
        apo_config.render_config_body(block_fallback),
    ])


def render_fallback_only() -> str:
    block_fallback = build_device_block(
        "all", apo_config.Filter(type="PK", fc=FALLBACK_PK_HZ,
                                  gain=FALLBACK_PK_GAIN_DB, q=FALLBACK_PK_Q))
    return apo_config.render_config_body(block_fallback)


def resolve_playback_index(device: audio_devices.ApoDevice) -> tuple[int | None, str]:
    """Resolve an ApoDevice to a sounddevice output index for ToneEngine.play().

    Tries the full deviceName+connectionName pattern first (mirrors real APO
    matching exactly); if sounddevice's own name field doesn't happen to
    contain the connection-name word, falls back to deviceName alone. Returns
    (index_or_None, how) where `how` documents which fragment matched, or
    "default" if neither resolved (caller falls back to system default).
    """
    idx = find_output_device(device.pattern)
    if idx is not None:
        return idx, f"full pattern {device.pattern!r}"
    if device.device_name:
        idx = find_output_device(device.device_name)
        if idx is not None:
            return idx, f"device name only {device.device_name!r}"
    return None, "default (no sounddevice name match found)"


def write_pointer_with_retry(pointer_path: Path, data: bytes | str, *, retries: int = 5) -> None:
    """Write the real rumble_active.txt, retrying briefly on PermissionError.

    Found live during the S18 spike run: a write to this file failed once
    with WinError 5 (access denied) immediately after 12s of continuous
    playback through it -- most likely Equalizer APO's own hot-reload
    watcher (or antivirus) holding a brief read lock right after a change is
    detected, not a real permissions problem (the very same path had just
    been written successfully seconds earlier). A short retry-with-backoff
    absorbs that without weakening the restore guarantee -- if it never
    clears, this still raises after `retries` attempts and the `finally`
    block's own restore attempt gets the same treatment.
    """
    mode = "w" if isinstance(data, str) else "wb"
    kwargs = {"encoding": "utf-8"} if isinstance(data, str) else {}
    for attempt in range(retries):
        try:
            with open(pointer_path, mode, **kwargs) as f:
                f.write(data)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.3)


def play_and_wait(engine: ToneEngine, buffer, device_index: int | None, label: str) -> None:
    print(f"  Playing {label}...")
    done = {"finished": False}

    def on_done():
        done["finished"] = True

    try:
        engine.play(buffer, SAMPLE_RATE, device=device_index, on_done=on_done)
    except AudioError as exc:
        print(f"    FAILED to start playback ({exc.kind}): {exc}")
        return

    deadline = time.monotonic() + NOISE_DURATION_S + 2.0
    while not done["finished"] and time.monotonic() < deadline:
        time.sleep(0.05)
    if not done["finished"]:
        print("    WARNING: playback did not report finished in time, stopping it now.")
        engine.stop()
    time.sleep(GAP_S)


def run_phase(engine: ToneEngine, noise, device_index: int | None, device_label: str,
              phase_name: str, listen_for: list[str], play_label: str) -> None:
    """One interactive phase: state the output, state what to listen for,
    wait for a real keypress, play, then offer an on-demand replay loop.
    Keypress-gated (not timed) because a fixed countdown proved useless when
    the user has no live view of a background process's console output --
    the countdown was already running before they could see it started."""
    while True:
        print()
        print(f"{phase_name}")
        print(f"  LISTEN ON: {device_label}")
        print("  LISTEN FOR:")
        for line in listen_for:
            print(f"    - {line}")
        input("  Press Enter when you are ready to listen... ")
        play_and_wait(engine, noise, device_index, play_label)
        again = input("  Type 'r' + Enter to replay this tone, or press Enter to continue: ")
        if again.strip().lower() != "r":
            break


def run_pair(pair_name: str, phase_a, phase_b) -> None:
    """Run two phases back to back, then offer to redo the WHOLE pair before
    moving on -- gives a clean pause point to report both results before the
    config changes again."""
    while True:
        phase_a()
        phase_b()
        print()
        print(f"{pair_name} complete.")
        again = input("  Type 'r' + Enter to redo the WHOLE pair, or press Enter to continue: ")
        if again.strip().lower() != "r":
            break


def main() -> int:
    global SAMPLE_RATE

    print("RUMBLE S18 hardware spike: APO Device: dispatch semantics")
    print("=" * 68)

    if not apo_path.is_apo_installed():
        print("Equalizer APO does not appear to be installed on this machine --")
        print("this spike needs the real config path. Aborting.")
        return 1

    devices = [d for d in audio_devices.list_apo_devices()
               if d.flow == "Render" and d.is_active]
    if len(devices) < 2:
        print(f"Found only {len(devices)} active APO-hooked playback endpoint(s).")
        print("This spike needs at least two -- hook a second output device")
        print("(Windows Sound settings + Equalizer APO's Configurator/")
        print("DeviceSelector) and re-run. Aborting.")
        return 1

    device_a, device_b = devices[0], devices[1]
    print(f"Device A: {device_a.label}")
    print(f"  pattern: {device_a.pattern!r}")
    print(f"Device B: {device_b.label}")
    print(f"  pattern: {device_b.pattern!r}")
    print()

    idx_a, how_a = resolve_playback_index(device_a)
    idx_b, how_b = resolve_playback_index(device_b)
    print(f"Playback resolution for A: index={idx_a!r} ({how_a})")
    print(f"Playback resolution for B: index={idx_b!r} ({how_b})")
    if idx_a is None or idx_b is None:
        print()
        print("WARNING: at least one device could not be resolved to a distinct")
        print("sounddevice output index -- playback for it will fall back to the")
        print("system default device, which may not be the intended endpoint.")
        print("Results for that device should be treated with caution.")
    print()

    root_dir = Path(__file__).resolve().parents[1]
    manager = ProfileManager(data_dir=root_dir / "data")
    SAMPLE_RATE = manager.effective_sample_rate()
    print(f"Equalizer APO config path: {manager.config_path}")
    print(f"Sample rate used for test noise: {SAMPLE_RATE} Hz")
    print()

    dispatch_text = render_dispatch(device_a.pattern, device_b.pattern)
    fallback_only_text = render_fallback_only()

    print("Dispatch config (three Device: blocks, before any render-layer")
    print("composition -- this spike bypasses render layers deliberately, to")
    print("isolate raw Device: dispatch behavior):")
    print("-" * 68)
    print(dispatch_text)
    print("-" * 68)
    print()

    pointer_path = manager.config_path / ACTIVE_POINTER_FILENAME
    scratch_dispatch_path = manager.rumble_dir / SCRATCH_DISPATCH
    scratch_fallback_path = manager.rumble_dir / SCRATCH_FALLBACK_ONLY
    original_pointer_bytes = pointer_path.read_bytes() if pointer_path.exists() else None

    engine = ToneEngine()
    try:
        manager.rumble_dir.mkdir(parents=True, exist_ok=True)

        noise = tones.generate_pink_noise(NOISE_DURATION_S, TONE_LEVEL_DBFS, SAMPLE_RATE)

        # ── Stage 1: full three-block dispatch ──────────────────────────
        scratch_dispatch_path.write_text(
            "# RUMBLE S18 spike scratch -- temporary, safe to delete\n\n"
            + dispatch_text, encoding="utf-8")
        write_pointer_with_retry(
            pointer_path, apo_config.render_include(f"{RUMBLE_SUBDIR}/{SCRATCH_DISPATCH}"))
        print("Dispatch config is now live on the real APO config (via")
        print("Include: indirection through rumble_active.txt).")

        run_pair(
            "PAIR 1 (full dispatch: A's block, B's block, unscoped fallback)",
            lambda: run_phase(
                engine, noise, idx_a, device_a.label,
                "PHASE 1 -- Device A under full dispatch",
                [f"Muffled, bass-only, no highs -> device A's own {DEVICE_A_LP_HZ:g} Hz "
                 "low-pass applied, fallback did NOT also apply (scoped-only).",
                 "Muffled bass PLUS a bright treble sizzle on top -> BOTH A's block and "
                 f"the fallback's {FALLBACK_PK_HZ:g} Hz +{FALLBACK_PK_GAIN_DB:g} dB peak "
                 "applied (stacked).",
                 "ONLY a bright treble sizzle, no muffle -> A's own block did NOT apply, "
                 "only the fallback did (superseded)."],
                "device A dispatch noise"),
            lambda: run_phase(
                engine, noise, idx_b, device_b.label,
                "PHASE 2 -- Device B under full dispatch",
                [f"Thin, no bass/mids -> device B's own {DEVICE_B_HP_HZ:g} Hz high-pass "
                 "applied, fallback did NOT also apply (scoped-only).",
                 "Thin sound PLUS extra bright sizzle on top -> BOTH B's block and the "
                 "fallback applied (stacked).",
                 "ONLY a bright treble sizzle, real bass/mids still present -> B's own "
                 "block did NOT apply, only the fallback did (superseded)."],
                "device B dispatch noise"),
        )

        # ── Stage 2: fallback marker alone, as a direct reference ───────
        print()
        print("Swapping to the fallback-marker-alone reference config (no per-device")
        print("blocks at all -- this is what the fallback sounds like by itself, to")
        print("compare against Pair 1's results)...")
        scratch_fallback_path.write_text(
            "# RUMBLE S18 spike scratch -- temporary, safe to delete\n\n"
            + fallback_only_text, encoding="utf-8")
        write_pointer_with_retry(
            pointer_path, apo_config.render_include(f"{RUMBLE_SUBDIR}/{SCRATCH_FALLBACK_ONLY}"))

        run_pair(
            "PAIR 2 (fallback marker alone, reference for Pair 1)",
            lambda: run_phase(
                engine, noise, idx_a, device_a.label,
                "PHASE 3 -- fallback marker alone, device A output",
                [f"Bright treble sizzle only, no muffle -> compare this directly against "
                 "Phase 1's result: if Phase 1 sounded THE SAME as this, A's own block "
                 "never applied at all."],
                "fallback-only reference noise (device A)"),
            lambda: run_phase(
                engine, noise, idx_b, device_b.label,
                "PHASE 4 -- fallback marker alone, device B output",
                [f"Bright treble sizzle only, real bass/mids present -> compare this "
                 "directly against Phase 2's result: if Phase 2 sounded THE SAME as "
                 "this, B's own block never applied at all."],
                "fallback-only reference noise (device B)"),
        )

        print()
        print("Both pairs played. Restoring the real active profile now...")
    finally:
        if original_pointer_bytes is not None:
            write_pointer_with_retry(pointer_path, original_pointer_bytes)
        elif pointer_path.exists():
            pointer_path.unlink()
        for scratch in (scratch_dispatch_path, scratch_fallback_path):
            if scratch.exists():
                scratch.unlink()
        engine.stop()

    print("Restored.")
    print()
    print("Pattern-matching reliability (c): compare Phase 1 vs Phase 2 above --")
    print("if A consistently sounded like A's own marker (muffled) and B")
    print("consistently sounded like B's own marker (thin), deviceName+")
    print("connectionName pattern matching correctly discriminated between the")
    print("two endpoints from one shared config file. If either device sounded")
    print("like the OTHER device's marker, pattern matching cross-matched --")
    print("a serious finding, note the exact device names/patterns above.")
    print()
    print("Include: indirection (d): this entire spike ran through")
    print("rumble_active.txt's Include: line pointing at a hand-built scratch")
    print("file -- if nothing audible changed at all during Phases 1-2, that is")
    print("itself evidence Include: indirection or Device: dispatch did not")
    print("apply, not just that the markers were subtle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
