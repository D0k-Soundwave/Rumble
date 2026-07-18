"""Manual, human-in-the-loop verification of DeviceFormatWatcher against a
real Windows audio stack.

Starts a watcher, prints the current default-device format, then listens for
30 seconds. While it runs, go to Windows Settings > System > Sound > (your
playback device) > Device properties > Additional device properties >
Advanced tab, and change the "Default Format" sample rate/bit depth. Each
Windows notification burst should collapse into a single re-query print
within ~0.5s (the debounce window) of the change settling.

Not part of the automated gate (tests/run_all_tests.py) — it blocks for 30s
waiting for human interaction, so it is listed in that script's EXCLUDE set.

Run directly:  python tests/verify_device_format_live.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import device_format as df

CHANGE_COUNT = 0


def on_change():
    global CHANGE_COUNT
    CHANGE_COUNT += 1
    fmt = df.get_default_device_format()
    label = fmt.label if fmt is not None else "(no default device / query failed)"
    print(f"[{time.strftime('%H:%M:%S')}] on_change() fired (#{CHANGE_COUNT}) -> {label}")


def main() -> int:
    print("DeviceFormatWatcher live verification")
    print("=" * 60)

    fmt = df.get_default_device_format()
    print(f"Current default device format: {fmt.label if fmt else '(none / query failed)'}")

    watcher = df.DeviceFormatWatcher(on_change)
    watcher.start()
    print("Watcher started (registration failure, if any, was printed above as a WARNING).")
    print()
    print("You have 30 seconds. To exercise this manually:")
    print("  Windows Settings > System > Sound > <device> > Device properties")
    print("  > Additional device properties > Advanced tab > Default Format")
    print("  -> change the sample rate / bit depth, click Apply.")
    print("Each change should print exactly one on_change() line within ~1s")
    print("of the change settling (debounced), not one per Windows notification.")
    print()

    for remaining in range(30, 0, -1):
        print(f"\rWaiting... {remaining:2d}s remaining ", end="", flush=True)
        time.sleep(1)
    print("\rDone waiting.                        ")

    watcher.stop()
    print()
    print(f"Total on_change() calls observed: {CHANGE_COUNT}")
    print("Watcher stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
