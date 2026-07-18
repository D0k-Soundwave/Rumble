"""Manual, human-in-the-loop verification of SessionWatcher against a real
Windows audio stack.

Starts a watcher and prints the set of actively-playing executable names for
20 seconds. While it runs, play/pause audio in a couple of apps (e.g. a
browser tab, Spotify) to see the set change.

Not part of the automated gate (tests/run_all_tests.py) — it blocks for 20s
waiting for human interaction, so it is listed in that script's EXCLUDE set.

Run directly:  python tests/verify_auto_switch_live.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core.auto_switch import SessionWatcher

CHANGE_COUNT = 0


def on_state(exes):
    global CHANGE_COUNT
    CHANGE_COUNT += 1
    label = ", ".join(sorted(exes)) if exes else "(nothing playing)"
    print(f"[{time.strftime('%H:%M:%S')}] on_state() fired (#{CHANGE_COUNT}) -> {label}")


def main() -> int:
    print("SessionWatcher live verification")
    print("=" * 60)

    watcher = SessionWatcher(on_state, poll_secs=1.0)
    watcher.start()
    print("Watcher started, polling every 1s.")
    print()
    print("You have 20 seconds. Play/pause audio in a browser tab or app")
    print("(e.g. a YouTube video, Spotify) to see the playing-set change.")
    print()

    for remaining in range(20, 0, -1):
        print(f"\rWaiting... {remaining:2d}s remaining ", end="", flush=True)
        time.sleep(1)
    print("\rDone waiting.                        ")

    watcher.stop()
    print()
    print(f"Total on_state() calls observed: {CHANGE_COUNT}")
    print("Watcher stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
