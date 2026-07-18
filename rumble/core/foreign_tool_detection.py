"""Detect other Equalizer-APO-consuming tools running or writing alongside
RUMBLE (docs/PLANNED_WORK.md #2 / RUMBLE_ACTION_GUIDE.md 4.8) — backend for
Tools -> Foreign-Tool Coexistence… dashboard.

Three independent, honestly-scoped signals — this is deliberately narrower
than the guide's aspirational framing (driver-level EQ enhancements are not
generically identifiable, so they are left out rather than guessing a tool
name RUMBLE can't actually know):

1. Foreign `Include:` targets currently active in config.txt (e.g. Peace's
   own peace.txt) and raw foreign directives written directly into
   config.txt alongside RUMBLE's managed block — both computed on demand from
   `ProfileManager`'s existing ownership-detection helpers
   (`_foreign_includes`/`_foreign_config_body`), which already run at boot
   inside `ensure_first_run()`. This dashboard is mostly a UI surface over
   data RUMBLE already collects, not new detection logic.
2. Existing preserved drift snapshots (`ProfileManager.list_drift_snapshots()`,
   Rumble 4.1) — evidence that config.txt was clobbered or co-written by
   something else at some point in the past, even if nothing foreign is
   present right now.
3. Known competing APO-client GUI processes currently running: Peace.exe
   (Peace, interface Equalizer APO — executable name confirmed against its
   SourceForge distribution, fetched 2026-07-10:
   sourceforge.net/projects/peace-equalizer-apo-extension/) and Equalizer
   APO's own bundled Editor.exe (confirmed filename, see
   docs/PROFILE_FILE_CONSTRUCTION.md / phase1-grammar-notes.md, both from
   binary-strings/on-disk verification against a real install). Detected via
   `tasklist` (Windows built-in, no new dependency — matches
   auto_switch.SessionWatcher's "poll on demand, never raise" resilience
   contract, just via a process list instead of a pycaw audio-session list,
   since neither of these GUIs necessarily plays audio). NOTE: "Editor.exe"
   is a generic filename shared by unrelated software; a false positive from
   some other app named Editor.exe is possible and the dashboard says so
   rather than presenting this as certain. A process that ran once and wrote
   to config.txt but has since closed is invisible to this signal — only
   signal 1/2 catch that case.
"""
from __future__ import annotations

import csv
import io
import subprocess
import sys
from dataclasses import dataclass

# Known competing APO-client GUI executables -> display name. Matched
# case-insensitively against tasklist's Image Name column, in this order.
KNOWN_APO_CLIENT_PROCESSES: dict[str, str] = {
    "peace.exe": "Peace",
    "editor.exe": "Equalizer APO Editor",
}


def _run_tasklist() -> str | None:
    """Raw `tasklist /fo csv /nh` output, or None on any failure. Never
    raises — matches every other watcher module's resilience contract."""
    if sys.platform != "win32":
        return None
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=5,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _parse_image_names(tasklist_csv: str) -> set[str]:
    """Pure parse: lowercase Image Name column (first field) from tasklist's
    CSV output. Tolerant of blank/malformed rows — skips rather than
    raising."""
    names: set[str] = set()
    for row in csv.reader(io.StringIO(tasklist_csv)):
        if not row or not row[0].strip():
            continue
        names.add(row[0].strip().lower())
    return names


def detect_running_tools(image_names: set[str] | None = None) -> list[str]:
    """Display names of known competing APO-client processes currently
    running, in `KNOWN_APO_CLIENT_PROCESSES` order.

    Pass `image_names` (a set of lowercase exe names) directly in tests;
    left as None, this calls the real `tasklist` and returns [] on any
    failure (non-Windows, tasklist missing, timeout, ...).
    """
    if image_names is None:
        raw = _run_tasklist()
        image_names = _parse_image_names(raw) if raw is not None else set()
    return [display for exe, display in KNOWN_APO_CLIENT_PROCESSES.items()
            if exe in image_names]


@dataclass
class CoexistenceReport:
    """Snapshot for the foreign-tool coexistence dashboard — on-demand, like
    the config-health viewer (Rumble 4.6), not live-refreshing."""
    foreign_includes: list[str]        # foreign Include: targets active in config.txt right now
    foreign_raw_directives: list[str]  # foreign APO directive lines written directly (not via Include:)
    drift_snapshot_count: int          # preserved config.txt.rumble_drift-<ts> files on disk
    latest_drift_label: str | None     # human label of the most recent one, if any
    running_tools: list[str]           # known competing APO-client GUIs currently running

    @property
    def has_any_signal(self) -> bool:
        return bool(self.foreign_includes or self.foreign_raw_directives
                     or self.drift_snapshot_count or self.running_tools)
