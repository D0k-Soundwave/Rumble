"""Per-listener Test A ("Ear Calibration") storage: identity, 10-run
retention, and the single averaged view every consumer (live correction,
plain-text export) reads from.

Qt-free, mirrors rumble.core.hearing_test's data_dir-argument style (no
ProfileManager dependency) so it stays unit-testable in isolation. See
docs/superpowers/specs/2026-07-09-listener-correction-design.md §3.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from rumble.core import hearing_test
from rumble.core.autoeq_merge import Point, average_curves

#: Exactly 4 fixed slots (design spec §2 — "Listener Slot", never "Profile").
SLOT_IDS: tuple[int, ...] = (1, 2, 3, 4)

#: A slot keeps at most this many saved Test A runs; saving an 11th deletes
#: the oldest outright (design spec §3.2 — normal end-user runtime retention,
#: not subject to this repo's dev-side archive-don't-delete convention).
MAX_SAVED_RUNS = 10

LISTENERS_SUBDIR = "listeners"
NAMES_FILENAME = "names.json"

#: The single progressive calibration's persistent working state -- one
#: file per slot holding the CURRENT per-point thresholds across sessions
#: (measure some points today, the rest tomorrow, re-test any point any
#: time). Saved runs (save_slot_result) stay as immutable history snapshots
#: for the results-page overlay; this file is what the dashboard shows and
#: what live correction reads (see effective_imbalance_curve).
STATE_FILENAME = "calibration_state.json"


def _slot_root(data_dir: Path, slot_id: int) -> Path:
    return Path(data_dir) / LISTENERS_SUBDIR / str(slot_id)


def _slot_hearing_dir(data_dir: Path, slot_id: int) -> Path:
    return _slot_root(data_dir, slot_id) / "hearing" / "frequency"


def _state_path(data_dir: Path, slot_id: int) -> Path:
    return _slot_root(data_dir, slot_id) / "hearing" / STATE_FILENAME


def _load_state_file(data_dir: Path, slot_id: int) -> dict | None:
    """The raw state file if one exists and parses, else None. Split from
    load_calibration_state() so consumers can distinguish "slot has adopted
    the working-state model" (file exists) from "legacy slot with only
    saved runs" -- effective_imbalance_curve keeps its historical
    average-of-runs behavior for the latter."""
    path = _state_path(data_dir, slot_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    points = payload.get("points", {})
    payload["points"] = {
        ear: {int(freq): entry for freq, entry in points.get(ear, {}).items()}
        for ear in ("left", "right")
    }
    return payload


def load_calibration_state(data_dir: Path, slot_id: int) -> dict:
    """The slot's working calibration state:
    ``{"points": {"left": {freq: {"threshold": float|None, "gave_up": bool,
    "measured_utc": str|None}}, "right": {...}},
    "volume_reference": dict|None}``.

    ``threshold`` None = the point was explicitly skipped. A slot with no
    state file yet is seeded (in memory only -- nothing is written until
    save_calibration_state) from its LATEST saved run, so a listener with
    pre-existing history opens the dashboard to their last calibration
    rather than a blank slate."""
    state = _load_state_file(data_dir, slot_id)
    if state is not None:
        return state
    state = {"points": {"left": {}, "right": {}}, "volume_reference": None}
    runs = load_slot_runs(data_dir, slot_id)
    if runs:
        latest = runs[-1]
        for ear in ("left", "right"):
            for freq, value in latest.get("raw", {}).get(ear, {}).items():
                state["points"][ear][int(freq)] = {
                    "threshold": value, "gave_up": False, "measured_utc": None}
        state["volume_reference"] = latest.get("volume_reference")
    return state


def save_calibration_state(data_dir: Path, slot_id: int, state: dict) -> Path:
    path = _state_path(data_dir, slot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def record_calibration_point(state: dict, ear: str, freq: int,
                              threshold: float | None,
                              gave_up: bool = False) -> None:
    """Merge one completed (or skipped: threshold None) point into *state*
    in place, stamping when it was measured."""
    from datetime import datetime, timezone
    state.setdefault("points", {}).setdefault(ear, {})[int(freq)] = {
        "threshold": threshold,
        "gave_up": bool(gave_up),
        "measured_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def calibration_raw(state: dict, *, measured_only: bool = False) -> dict:
    """*state* flattened to hearing_test's raw-results shape
    ({"left": {freq: threshold|None}, ...}) for normalise()/export_text().

    ``measured_only=True`` also maps a **gave-up** point to None. A gave-up
    point is a *censored* observation -- the tone was not heard even at the
    ceiling, so the true threshold is "at least the ceiling, possibly far
    above it" -- and its stored value is that ceiling, i.e. a LOWER BOUND, not
    a measurement. Anything doing MATH on these numbers must pass True:

    * it understates the loss (the real threshold is higher than the ceiling
      it records), and
    * far worse, it hands the correction a large positive imbalance at a
      frequency the ear cannot hear at any testable level -- so correction
      spends its capped boost, and makes every OTHER frequency pay for it
      through the global anti-clipping preamp, pushing energy into a dead
      band where it can achieve nothing.

    The default (False) keeps the ceiling value, for callers that want the
    unfiltered record. Either way the point stays in the dict (as a key), so
    "tested" vs "never tested" is still distinguishable, and the `gave_up`
    flag itself lives on in *state* for the dashboard chips and
    no_response_freqs()."""
    def value(entry: dict) -> float | None:
        if measured_only and entry.get("gave_up"):
            return None
        return entry.get("threshold")

    return {
        ear: {freq: value(entry)
              for freq, entry in state.get("points", {}).get(ear, {}).items()}
        for ear in ("left", "right")
    }


def no_response_freqs(state: dict) -> dict[str, list[int]]:
    """Per ear, the frequencies that were tested but never heard, even at the
    ceiling. calibration_raw(measured_only=True) drops these from the math;
    this is how a consumer that should still REPORT them (the plain-text
    export, the dashboard) gets them back."""
    return {
        ear: sorted(freq for freq, entry in state.get("points", {}).get(ear, {}).items()
                    if entry.get("gave_up"))
        for ear in ("left", "right")
    }


def clear_calibration_state(data_dir: Path, slot_id: int) -> None:
    _state_path(data_dir, slot_id).unlink(missing_ok=True)


def save_slot_result(data_dir: Path, slot_id: int, norm: dict, raw: dict,
                      volume_reference: dict | None = None) -> Path:
    """Persist one Test A run for *slot_id* (same JSON shape
    hearing_test.save_results() already produces, just rooted per-slot),
    then enforce the 10-run cap."""
    path = hearing_test.save_results(
        norm, raw, _slot_root(data_dir, slot_id), volume_reference=volume_reference)
    _enforce_retention(data_dir, slot_id)
    return path


def load_slot_runs(data_dir: Path, slot_id: int) -> list[dict]:
    """Every saved run for *slot_id*, oldest to newest."""
    return hearing_test.load_all_results(_slot_root(data_dir, slot_id))


def slot_run_files(data_dir: Path, slot_id: int) -> list[Path]:
    """Every saved-run JSON file for *slot_id*, oldest to newest -- same
    selection/ordering as load_slot_runs(), but returning paths (a
    filename doubles as a stable ID for the history-management dialog's
    delete UI) instead of parsed payloads."""
    out_dir = _slot_hearing_dir(data_dir, slot_id)
    if not out_dir.exists():
        return []
    return sorted(out_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))


def delete_slot_runs(data_dir: Path, slot_id: int, filenames) -> None:
    """Delete specific saved runs by filename (Path.name, as returned by
    slot_run_files()). Does not touch the working calibration state --
    call recompile_calibration_from_runs() afterward to resync it to
    whatever history remains."""
    names = set(filenames)
    for path in slot_run_files(data_dir, slot_id):
        if path.name in names:
            path.unlink(missing_ok=True)


def recompile_calibration_from_runs(data_dir: Path, slot_id: int) -> None:
    """Resync the working calibration state to whatever saved-run history
    remains -- call right after delete_slot_runs() removes one or more
    runs, so a deleted run stops contributing to live correction instead
    of lingering in state a prior pass already wrote there. Mirrors
    load_calibration_state()'s from-history seeding: the latest surviving
    run's raw thresholds become the new working state (a run snapshot
    doesn't carry the gave-up/measured_utc detail a live-recorded point
    does, same caveat as that seeding path). No runs left -> clears the
    state outright, so the slot reads exactly like a never-calibrated one
    until the listener runs the test again."""
    runs = load_slot_runs(data_dir, slot_id)
    if not runs:
        clear_calibration_state(data_dir, slot_id)
        return
    latest = runs[-1]
    state = {"points": {"left": {}, "right": {}},
             "volume_reference": latest.get("volume_reference")}
    for ear in ("left", "right"):
        for freq, value in latest.get("raw", {}).get(ear, {}).items():
            state["points"][ear][int(freq)] = {
                "threshold": value, "gave_up": False, "measured_utc": None}
    save_calibration_state(data_dir, slot_id, state)


def latest_volume_reference(data_dir: Path, slot_id: int) -> dict | None:
    """The most recent saved run's volume_reference for *slot_id*, or None
    if there are no runs yet or none of them captured one (older runs saved
    before this existed, or every capture attempt failed). Lets the intro
    page show "here's what you set last time" before the listener starts
    adjusting their volume again -- see hearing_test.export_text's
    docstring for the dict's shape and scope.
    """
    runs = load_slot_runs(data_dir, slot_id)
    for run in reversed(runs):
        ref = run.get("volume_reference")
        if ref:
            return ref
    return None


def _enforce_retention(data_dir: Path, slot_id: int) -> None:
    out_dir = _slot_hearing_dir(data_dir, slot_id)
    if not out_dir.exists():
        return
    files = sorted(out_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    excess = len(files) - MAX_SAVED_RUNS
    for path in files[:max(excess, 0)]:
        path.unlink(missing_ok=True)


def effective_imbalance_curve(data_dir: Path, slot_id: int) -> list[Point]:
    """The single imbalance view every consumer (live correction,
    plain-text export) reads.

    A slot with a working calibration state file reads its imbalance from
    that state directly -- the dashboard's per-point model means "what the
    list shows is what corrects": a re-tested point takes full effect
    immediately, instead of being diluted through a history average.
    Legacy slots (saved runs only, dashboard never used) keep the original
    design-spec behavior: the average of every saved run's imbalance curve
    via autoeq_merge.average_curves() (design spec §3.3). Empty when the
    slot has nothing measured."""
    state = _load_state_file(data_dir, slot_id)
    if state is not None:
        # measured_only: a point the ear never heard, even at the ceiling, is a
        # lower bound rather than a measurement -- correcting from it would
        # boost a dead band at every other frequency's expense. See
        # calibration_raw().
        norm = hearing_test.normalise(calibration_raw(state, measured_only=True))
        imbalance = norm.get("imbalance", {})
        return sorted((float(f), float(v)) for f, v in imbalance.items())
    curves: list[list[Point]] = []
    for run in load_slot_runs(data_dir, slot_id):
        imbalance = run.get("norm", {}).get("imbalance", {})
        if imbalance:
            curves.append(sorted((float(f), float(v)) for f, v in imbalance.items()))
    return average_curves(curves)


def load_slot_names(data_dir: Path) -> dict[int, str]:
    """Custom display names, keyed by slot id. A slot with no saved name is
    absent from the returned dict -- callers fall back to
    f"Listener {slot_id}"."""
    path = Path(data_dir) / LISTENERS_SUBDIR / NAMES_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {int(k): v for k, v in raw.items() if isinstance(v, str) and v.strip()}


def save_slot_names(data_dir: Path, names: dict[int, str]) -> None:
    """Overwrite the whole names file with *names* (blank/whitespace-only
    names are dropped so the default "Listener N" label applies)."""
    root = Path(data_dir) / LISTENERS_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    cleaned = {str(k): v.strip() for k, v in names.items() if v and v.strip()}
    (root / NAMES_FILENAME).write_text(json.dumps(cleaned, indent=2), encoding="utf-8")


def migrate_legacy_hearing_history(data_dir: Path) -> int:
    """One-time migration: copy any pre-existing global Test A history
    (data_dir/hearing/frequency/*.json, saved before Listener Slots existed)
    into Slot 1, then apply the retention cap. Copies rather than moves
    (non-destructive) and is a no-op if Slot 1 already has any saved runs, so
    it is safe to call unconditionally -- callers (ProfileManager) still gate
    repeat calls behind a settings flag to avoid re-copying files a user has
    since deleted from the legacy location. Returns the number of runs
    migrated."""
    legacy_dir = Path(data_dir) / "hearing" / "frequency"
    if not legacy_dir.is_dir():
        return 0
    dest_dir = _slot_hearing_dir(data_dir, 1)
    if dest_dir.is_dir() and any(dest_dir.glob("*.json")):
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    migrated = 0
    for src in sorted(legacy_dir.glob("*.json")):
        shutil.copy2(src, dest_dir / src.name)
        migrated += 1
    if migrated:
        _enforce_retention(data_dir, 1)
    return migrated
