"""Discover and run every standalone test/audit script in tools/, printing a
PASS/FAIL matrix and exiting non-zero if any fails.

Each script is run as its own subprocess with QT_QPA_PLATFORM=offscreen and a
timeout, so a hang or crash in one cannot take down the rest. This is the
regression gate: run it before and after a change set and compare.

Run:  python tools/run_all_tests.py
      python tools/run_all_tests.py -v        # stream each script's output
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent

# Scripts that require a real display / human interaction, or that drive a live
# QApplication.exec() event loop with screen capture. These are visual/manual
# verification tools, not headless regression tests — under the offscreen
# platform they never terminate, so they are not part of the automated gate.
EXCLUDE = {
    "run_all_tests.py",       # this driver
    "verify_device_ui.py",    # interactive (prints for human review, may block)
    "test_live_rescale.py",   # drives real main()+exec(), self-captures window
    "verify_tab_order.py",    # patches exec() and runs a live event loop
    "verify_device_format_live.py",  # 30s human-in-the-loop watcher demo, blocks by design
    "verify_auto_switch_live.py",  # 20s human-in-the-loop session-watcher demo, blocks by design
    "verify_audio_live.py",   # real hardware, human-in-the-loop (plays audible tones/noise)
    "verify_spectrum_live.py",  # real hardware, human-in-the-loop (plays/observes real audio)
    "verify_hearing_live.py",  # real hardware, human-in-the-loop (plays audible tones/triplets)
    "verify_crossfeed_live.py",  # real hardware, human-in-the-loop (S6 spike, plays hard-panned tones)
    "verify_device_dispatch_live.py",  # real hardware, human-in-the-loop (S18 spike, plays pink noise on two devices)
    "verify_capture_dispatch_live.py",  # real hardware (plays probe tones, records an APO-hooked mic)
}

# Prefixes that mark a runnable test script.
PREFIXES = ("selftest_", "test_", "verify_", "audit_")

TIMEOUT = 180  # seconds per script


def discover() -> list[Path]:
    found: list[Path] = []
    for p in sorted(TOOLS.glob("*.py")):
        if p.name in EXCLUDE:
            continue
        if p.name.startswith(PREFIXES):
            found.append(p)
    return found


def run_one(script: Path, verbose: bool) -> tuple[str, float, str]:
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        elapsed = time.monotonic() - start
        output = (proc.stdout or "") + (proc.stderr or "")
        if verbose:
            print(output)
        status = "PASS" if proc.returncode == 0 else "FAIL"
        return status, elapsed, output
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.monotonic() - start, "(exceeded %ds)" % TIMEOUT


def main() -> int:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    scripts = discover()
    print(f"Running {len(scripts)} test scripts (offscreen, {TIMEOUT}s timeout each)\n")

    results: list[tuple[str, str, float]] = []
    failed_output: dict[str, str] = {}
    for script in scripts:
        status, elapsed, output = run_one(script, verbose)
        results.append((script.name, status, elapsed))
        marker = {"PASS": "PASS ", "FAIL": "FAIL ", "TIMEOUT": "TIME "}[status]
        print(f"  {marker} {script.name:<40} {elapsed:6.1f}s")
        if status != "PASS":
            failed_output[script.name] = output

    passes = sum(1 for _, s, _ in results if s == "PASS")
    fails = [name for name, s, _ in results if s != "PASS"]

    print(f"\n{'=' * 60}")
    print(f"{passes}/{len(results)} passed")
    if fails:
        print(f"\nFAILED: {', '.join(fails)}")
        for name in fails:
            print(f"\n----- {name} -----")
            print(failed_output[name][-3000:])  # tail of the output
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
