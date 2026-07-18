# tests/selftest_foreign_tool_detection.py
"""Selftest: foreign-tool coexistence dashboard backend (Rumble 4.8,
docs/PLANNED_WORK.md #2) — detects (1) foreign `Include:` targets currently
active in config.txt, (2) raw foreign directives written directly into
config.txt alongside RUMBLE's managed block, (3) existing drift snapshots
(reusing Rumble 4.1's ProfileManager.list_drift_snapshots()), and (4) known
competing APO-client GUI processes (Peace.exe, Equalizer APO's own
Editor.exe) currently running, via `tasklist` parsing."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import foreign_tool_detection as ftd
from rumble.core.profile_manager import (
    CONFIG_DRIFT_SUFFIX, CONFIG_FILENAME, MANAGED_MARKER, MANAGED_CONFIG_VERSION,
    ProfileManager,
)


# ── _parse_image_names ───────────────────────────────────────────────────

def test_parse_image_names_from_tasklist_csv():
    csv_text = (
        '"Peace.exe","4821","Console","1","12,345 K"\r\n'
        '"chrome.exe","9911","Console","1","210,000 K"\r\n'
        '"System Idle Process","0","Services","0","8 K"\r\n'
    )
    names = ftd._parse_image_names(csv_text)
    assert "peace.exe" in names
    assert "chrome.exe" in names
    assert "system idle process" in names


def test_parse_image_names_tolerates_malformed_lines():
    csv_text = '"Peace.exe","4821","Console","1","12,345 K"\r\n\r\n,,,\r\n'
    names = ftd._parse_image_names(csv_text)
    assert "peace.exe" in names


# ── detect_running_tools ─────────────────────────────────────────────────

def test_detect_running_tools_matches_peace():
    assert ftd.detect_running_tools(image_names={"peace.exe", "chrome.exe"}) == ["Peace"]


def test_detect_running_tools_matches_apo_editor():
    assert ftd.detect_running_tools(image_names={"editor.exe"}) == ["Equalizer APO Editor"]


def test_detect_running_tools_empty_when_no_match():
    assert ftd.detect_running_tools(image_names={"notepad.exe", "chrome.exe"}) == []


def test_detect_running_tools_falls_back_to_real_tasklist_when_unavailable():
    # Simulate tasklist being unavailable (non-Windows, or the call failing) —
    # must never raise, just report no known tools running.
    real_run = ftd._run_tasklist
    ftd._run_tasklist = lambda: None
    try:
        assert ftd.detect_running_tools() == []
    finally:
        ftd._run_tasklist = real_run


# ── ProfileManager.coexistence_report() ──────────────────────────────────

def _write_owned_config(apo_dir: Path, extra: str = "") -> None:
    apo_dir.mkdir(parents=True, exist_ok=True)
    (apo_dir / CONFIG_FILENAME).write_text(
        "# ----------------------------------------------\n"
        "# This Equalizer APO config is managed by RUMBLE.\n"
        "# Do not edit by hand - use the RUMBLE app.\n"
        "# Your original config was saved as: config.txt.rumble_backup\n"
        f"{MANAGED_MARKER} {MANAGED_CONFIG_VERSION}\n"
        "# ----------------------------------------------\n"
        "Include: rumble_active.txt\n"
        f"{extra}",
        encoding="utf-8")


def _patched_no_running_tools(fn):
    """Run fn() with foreign_tool_detection.detect_running_tools() forced to
    return [], so process-list nondeterminism (whatever happens to be running
    on the machine executing the test) never affects these assertions."""
    real = ftd.detect_running_tools
    ftd.detect_running_tools = lambda: []
    try:
        fn()
    finally:
        ftd.detect_running_tools = real


def test_coexistence_report_clean_when_nothing_foreign():
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            apo_dir = Path(tmp) / "apo"
            mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
            _write_owned_config(apo_dir)

            report = mgr.coexistence_report()
            assert report.foreign_includes == []
            assert report.foreign_raw_directives == []
            assert report.drift_snapshot_count == 0
            assert report.latest_drift_label is None
            assert report.has_any_signal is False
    _patched_no_running_tools(run)


def test_coexistence_report_detects_foreign_include():
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            apo_dir = Path(tmp) / "apo"
            mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
            _write_owned_config(apo_dir, extra="Include: peace.txt\n")

            report = mgr.coexistence_report()
            assert report.foreign_includes == ["peace.txt"]
            assert report.has_any_signal is True
    _patched_no_running_tools(run)


def test_coexistence_report_detects_foreign_raw_directive():
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            apo_dir = Path(tmp) / "apo"
            mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
            _write_owned_config(apo_dir, extra="Filter: ON PK Fc 100 Hz Gain 3 dB Q 1\n")

            report = mgr.coexistence_report()
            assert report.foreign_includes == []
            assert len(report.foreign_raw_directives) == 1
            assert "Fc 100" in report.foreign_raw_directives[0]
            assert report.has_any_signal is True
    _patched_no_running_tools(run)


def test_coexistence_report_reflects_drift_snapshots():
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            apo_dir = Path(tmp) / "apo"
            mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
            _write_owned_config(apo_dir)
            (apo_dir / f"{CONFIG_FILENAME}{CONFIG_DRIFT_SUFFIX}-20260301-120000").write_text(
                "Preamp: -6 dB\n", encoding="utf-8")

            report = mgr.coexistence_report()
            assert report.drift_snapshot_count == 1
            assert report.latest_drift_label is not None
            assert report.has_any_signal is True
    _patched_no_running_tools(run)


def test_coexistence_report_includes_running_tools():
    with tempfile.TemporaryDirectory() as tmp:
        apo_dir = Path(tmp) / "apo"
        mgr = ProfileManager(Path(tmp) / "data", config_path=apo_dir)
        _write_owned_config(apo_dir)

        real = ftd.detect_running_tools
        ftd.detect_running_tools = lambda: ["Peace"]
        try:
            report = mgr.coexistence_report()
            assert report.running_tools == ["Peace"]
            assert report.has_any_signal is True
        finally:
            ftd.detect_running_tools = real


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
