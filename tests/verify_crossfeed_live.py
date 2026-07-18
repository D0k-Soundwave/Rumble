"""Manual, human-in-the-loop hardware spike (Session S6, RUMBLE adaptive
front-end campaign, roadmap section B2) validating the Equalizer APO
virtual-channel primitives a native crossfeed generator (S7) would rely on:

  Copy: XL=R XR=L        -- creates two virtual channels carrying the
                             opposite ear's content
  Channel: XL XR          -- filters/delays/attenuates the cross path
  Copy: L=L+XL R=R+XR     -- mixes the processed cross path back into L/R

This is NOT the crossfeed feature itself (that's S7/S8) -- it only confirms
whether APO accepts and correctly processes this pattern on real hardware,
per the roadmap's "Validation spike FIRST (half-session): confirm APO's
virtual-channel Copy:/Channel: behavior on real hardware before building UI."

Structural note on how the test config is built: `apo_config.render_config_body`
always renders ALL of a ProfileConfig's `copy_lines` before ALL of its
`channel_sections` (see its docstring's canonical order) -- there is no
field for a Copy: line that must render AFTER a Channel: section. The first
Copy: (creating XL/XR) belongs before the Channel: block and fits
`copy_lines` correctly; the second Copy: (mixing XL/XR back into L/R) must
render AFTER it, so this script places it in `extra_lines` (the verbatim/
"rendered last" field) as a workaround. Whether S7's real generator should
extend the data model with a proper "post-channel copy" field, or bypass
ProfileConfig entirely and emit raw text (as the roadmap's own "Compose"
note for B2 already suggests), is exactly the kind of thing this spike's
findings should inform -- not decided here.

What this script does:
  1. Builds the crossfeed test config above (Chu Moy preset values: ~700 Hz
     low-pass, -6 dB cross-path attenuation -- cited in the B2 roadmap
     section; ~300 microsecond Linkwitz-style ITD delay, same citation; LP
     Q uses rumble.core.crossover.BUTTERWORTH_Q, the codebase's existing
     standard "maximally flat" single-stage alignment for an APO `LP`
     filter, matching crossover.py's own convention).
  2. Previews it on the REAL Equalizer APO config via
     ProfileManager.preview_config(), using ABXRunner as a context manager
     so the true active profile is restored via end_preview() on every exit
     path (including an exception) -- the same guarantee ABXRunner gives
     ABX trials.
  3. Plays a sequence of hard-panned test tones through the default output
     device, narrating each phase so a human listening on headphones can
     judge what they hear.

IMPORTANT -- what this script can and cannot confirm on its own:
  It can confirm the process doesn't crash, that APO accepted the config
  file (Equalizer APO's own hot-reload either applies it or silently
  no-ops a rejected line -- this script cannot read APO's log itself), and
  it narrates exactly what should be audible at each phase. It CANNOT
  confirm what was actually heard -- that requires a human with working
  stereo headphones actually listening and reporting back. It also cannot
  meaningfully confirm the ~300 microsecond delay perceptually in isolation
  (well below the ear's threshold for a "later note", audible only as part
  of authentic interaural timing) -- that check leans on (c) below (APO
  accepting the Delay: line without erroring) rather than a direct listening
  test.

Findings from running this with the user go into
docs/RUMBLE_APP_SPECIFIC_KB.md as a new dated subsection, and into
docs/superpowers/plans/2026-07-12-adaptive-frontend-progress.md.

Not part of the automated gate (tests/run_all_tests.py) -- it plays audible
sound, requires real playback hardware, and needs a human to interpret the
result, so it is listed in that script's EXCLUDE set.

Run directly:  python tests/verify_crossfeed_live.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, apo_path, device_format
from rumble.core.abx import ABXRunner
from rumble.core.audio import tones
from rumble.core.audio.engine import AudioError, ToneEngine
from rumble.core.crossover import BUTTERWORTH_Q
from rumble.core.profile_manager import ProfileManager

TONE_LEVEL_DBFS = -20.0
# 9s (not the original 3s): during the live S6 spike, the user found 3s too
# short to confidently judge a quiet leak against a loud dry tone and asked
# for 3x the duration.
TONE_DURATION_S = 9.0
GAP_S = 0.75

# Chu Moy classic headphone-crossfeed preset, cited in the B2 roadmap design
# (docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md, "Design"):
# "Chu Moy (-6 dB, ~700 Hz)". Used verbatim here as the spike's test values.
CROSSFEED_LP_HZ = 700.0
CROSSFEED_GAIN_DB = -6.0

# Linkwitz-style ITD figure, same B2 roadmap section: "a Linkwitz-style
# (~300 microsecond ITD) variant".
CROSSFEED_DELAY_MS = 0.3

# Phase A/B source frequency: comfortably below CROSSFEED_LP_HZ so the leak
# should pass through the cross-path filter with little attenuation --
# isolates the "does audio mix back into L/R at all" question from the
# filter's own behavior.
LEAK_TEST_HZ = 300.0

# Phase C source frequency: comfortably above CROSSFEED_LP_HZ so a leak this
# high should be strongly attenuated if the low-pass is actually acting on
# the cross path -- isolates the "does the cross path obey the filter"
# question.
FILTER_TEST_HZ = 4000.0


def build_crossfeed_config() -> apo_config.ProfileConfig:
    cross_section = apo_config.ChannelSection(
        channel="XL XR",
        filters=[apo_config.Filter(type="LP", fc=CROSSFEED_LP_HZ, q=BUTTERWORTH_Q)],
        delay_ms=CROSSFEED_DELAY_MS,
        preamp_db=CROSSFEED_GAIN_DB,
    )
    return apo_config.ProfileConfig(
        device="all",
        copy_lines=["XL=R", "XR=L"],
        channel_sections=[cross_section],
        extra_lines=["Copy: L=L+XL R=R+XR"],
    )


def play_and_wait(engine: ToneEngine, buffer, label: str) -> None:
    print(f"  Playing {label}...")
    done = {"finished": False}

    def on_done():
        done["finished"] = True

    try:
        engine.play(buffer, SAMPLE_RATE, device=None, on_done=on_done)
    except AudioError as exc:
        print(f"    FAILED to start playback ({exc.kind}): {exc}")
        return

    deadline = time.monotonic() + TONE_DURATION_S + 2.0
    while not done["finished"] and time.monotonic() < deadline:
        time.sleep(0.05)
    if not done["finished"]:
        print("    WARNING: playback did not report finished in time, stopping it now.")
        engine.stop()
    time.sleep(GAP_S)


def main() -> int:
    global SAMPLE_RATE

    print("RUMBLE S6 hardware spike: APO virtual-channel crossfeed plumbing")
    print("=" * 68)

    if not apo_path.is_apo_installed():
        print("Equalizer APO does not appear to be installed on this machine --")
        print("this spike needs the real config path. Aborting.")
        return 1

    root_dir = Path(__file__).resolve().parents[1]
    manager = ProfileManager(data_dir=root_dir / "data")
    SAMPLE_RATE = manager.effective_sample_rate()

    fmt = device_format.get_default_device_format()
    dev_name = device_format.get_default_device_name()
    print(f"Default output device: {dev_name!r}")
    if fmt is not None:
        print(f"  {fmt.label}, {fmt.channels} channel(s)")
        if fmt.channels != 2:
            print("  NOTE: default device is not 2-channel stereo -- this spike's")
            print("  result may not generalize to this device's layout (see roadmap")
            print("  B2 decision: crossfeed is stereo-only, out of scope for 5.1 etc).")
    else:
        print("  (could not read the device's mix format)")
    print(f"Equalizer APO config path: {manager.config_path}")
    print(f"Sample rate used for test tones: {SAMPLE_RATE} Hz")
    print()

    config = build_crossfeed_config()
    print("Test config (before render-layer composition):")
    print("-" * 68)
    print(apo_config.render_config_body(config))
    print("-" * 68)
    print()
    print(f"Crossfeed test values: {CROSSFEED_LP_HZ:g} Hz low-pass (Q={BUTTERWORTH_Q:.3f}), "
          f"{CROSSFEED_GAIN_DB:g} dB cross-path gain, {CROSSFEED_DELAY_MS:g} ms delay.")
    print()

    engine = ToneEngine()
    runner = ABXRunner(manager, [config], SAMPLE_RATE)
    with runner:
        ok = runner.present(0)
        if not ok:
            print("preview_config() returned False (manager is bypassed) -- cannot")
            print("preview the crossfeed config. Un-bypass RUMBLE and re-run.")
            return 1
        print("Crossfeed config is now live on the real APO config. Listening")
        print("checks begin -- please listen on stereo headphones.")
        print()

        print("PHASE A -- leak audibility, LEFT source")
        print(f"  {LEAK_TEST_HZ:g} Hz tone, LEFT channel only, for {TONE_DURATION_S:g}s.")
        print("  Expect: clear tone in the left ear, PLUS a quieter, slightly")
        print("  delayed copy leaking into the right ear (checks (a): does audio")
        print("  from the virtual channels mix back into L/R at all, and (c): did")
        print("  APO accept the Channel: XL XR line for the virtual channels --")
        print("  if it silently rejected it, the leak would be silent).")
        left = tones.generate_sine(LEAK_TEST_HZ, TONE_DURATION_S, TONE_LEVEL_DBFS,
                                    SAMPLE_RATE, channel="left")
        play_and_wait(engine, left, "LEFT-only leak-test tone")

        print()
        print("PHASE B -- leak audibility, RIGHT source")
        print(f"  {LEAK_TEST_HZ:g} Hz tone, RIGHT channel only, for {TONE_DURATION_S:g}s.")
        print("  Expect: mirror of Phase A -- a quieter leak now in the left ear.")
        right = tones.generate_sine(LEAK_TEST_HZ, TONE_DURATION_S, TONE_LEVEL_DBFS,
                                     SAMPLE_RATE, channel="right")
        play_and_wait(engine, right, "RIGHT-only leak-test tone")

        print()
        print("PHASE C -- filter behavior")
        print(f"  {FILTER_TEST_HZ:g} Hz tone, LEFT channel only, for {TONE_DURATION_S:g}s.")
        print("  Expect: a much quieter/near-inaudible leak into the right ear")
        print("  compared to Phase A's leak -- this checks (b)'s filter half: does")
        print(f"  the cross path's {CROSSFEED_LP_HZ:g} Hz low-pass actually attenuate")
        print("  a source well above the cutoff.")
        bright = tones.generate_sine(FILTER_TEST_HZ, TONE_DURATION_S, TONE_LEVEL_DBFS,
                                      SAMPLE_RATE, channel="left")
        play_and_wait(engine, bright, "LEFT-only filter-test tone")

        print()
        print("All phases played. Restoring the real active profile now...")

    print("Restored.")
    print()
    print("(b)'s delay half is not independently checked above -- a ~300")
    print("microsecond difference is not perceptible as a distinct echo in")
    print("isolation, only as part of authentic interaural timing. Its")
    print("confirmation rests on APO not having errored/rejected the Delay:")
    print("line inside the Channel: XL XR section (i.e. Phase A/B producing a")
    print("leak at all is also evidence for this).")
    print()
    print("(d) channel-count/layout constraints: see the default-device report")
    print("printed above. Ask the user directly if their setup has anything")
    print("unusual (a surround receiver, a multichannel DAC/interface) that a")
    print("stereo-only crossfeed feature (per the B2 roadmap decision) should")
    print("know to hide/soft-disable against.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
