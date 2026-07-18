"""Self-test: the right-side VU/Peak meter panel (rumble/ui/vu_meter.py),
wired into the real editor layout (rumble/ui/editor_panel.py) and driven
through the real Analyzer toggle (rumble/ui/eq_graph.py).

Builds a real offscreen MainWindow, no real audio hardware -- levels are fed
directly to the widget the same way the live capture pipeline would.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication

from rumble.core import eq_math, level_meter
from rumble.core.audio.metering import StereoLevels
from rumble.core.profile_manager import ProfileManager
from rumble.ui.main_window import MainWindow
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

failures: list[str] = []


def expect(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        failures.append(label)


def main() -> int:
    app = QApplication([])
    for font_file in (Path(__file__).resolve().parents[1] / "assets" / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(font_file))
    engine = ThemeEngine(get_theme(DEFAULT_THEME_NAME))
    engine.apply(app)

    tmp = Path(tempfile.mkdtemp(prefix="rumble_selftest_"))
    m = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    m.ensure_first_run()
    m.settings["show_safety_warning"] = False

    w = MainWindow(m, engine, QIcon())
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    w.move(-4000, 0)
    w.show()
    app.processEvents()

    panel = w.editor_panel
    meter = panel.vu_meter

    expect(meter is not None, "editor_panel.vu_meter exists")
    expect(meter.isVisible(), "meter panel is visible")
    expect(meter.x() > panel._graph_frame.x(),
           "meter panel sits to the right of the graph frame")

    # ── on/off follows the Analyzer toggle ────────────────────────────────

    expect(meter._active is False, "meter starts inactive (Analyzer off by default)")
    panel.graph._btn_live.setChecked(True)
    app.processEvents()
    expect(meter._active is True, "checking the Analyzer toggle activates the meter")
    panel.graph._btn_live.setChecked(False)
    app.processEvents()
    expect(meter._active is False, "unchecking the Analyzer toggle deactivates the meter")
    expect(meter.level_meter.bar_l == meter.level_meter.bar_r == -60.0,
           "deactivating resets both bars to the floor")

    # ── feeding levels updates the underlying LevelMeter ──────────────────

    panel.graph._btn_live.setChecked(True)
    app.processEvents()
    levels = StereoLevels(peak_l=-3.0, peak_r=-9.0, rms_l=-10.0, rms_r=-15.0)
    meter.set_mode("peak")
    meter.on_stereo_level(levels)
    expect(meter.level_meter.bar_l == -3.0 and meter.level_meter.bar_r == -9.0,
           "Peak mode's bars read the fed levels directly")

    # Inactive meter ignores fed levels.
    panel.graph._btn_live.setChecked(False)
    app.processEvents()
    before_l = meter.level_meter.bar_l
    meter.on_stereo_level(StereoLevels(peak_l=0.0, peak_r=0.0, rms_l=0.0, rms_r=0.0))
    expect(meter.level_meter.bar_l == before_l,
           "an inactive meter ignores on_stereo_level updates")

    # ── VU/Peak mode toggle + settings round-trip ─────────────────────────

    received: list[dict] = []
    panel.meter_settings_changed.connect(lambda s: received.append(s))
    meter.btn_vu.setChecked(False)
    meter.btn_peak.setChecked(True)
    meter._on_mode_clicked("peak")
    expect(meter.level_meter.mode == "peak", "clicking Peak flips the LevelMeter's mode")
    expect(received and received[-1] == {"meter_mode": "peak"},
           f"mode click emits meter_settings_changed({{'meter_mode': 'peak'}}) (got {received})")

    meter._on_mode_clicked("vu")
    expect(meter.level_meter.mode == "vu", "clicking VU flips back")
    expect(received[-1] == {"meter_mode": "vu"}, "second click emits the updated mode")

    # ── estimated INPUT bars: graph -> widget wiring ──────────────────────
    # White-box: drives EQGraph's own tick handlers directly rather than a
    # fake capture, since there's no capture_factory injection seam threaded
    # through EditorPanel/MainWindow (only EQGraph's own constructor has one).

    panel.graph._btn_live.setChecked(True)
    app.processEvents()
    meter.set_mode("peak")

    graph = panel.graph
    n = len(eq_math.frequency_array())
    graph._spectrum_enabled = True
    graph._spectrum_response_db = np.full(n, 6.0)  # flat +6 dB EQ boost
    graph._on_spectrum_ready(np.full(n, -20.0))     # measured spectrum this tick
    expect(graph._last_input_offset_db is not None
           and abs(graph._last_input_offset_db - (-6.0)) < 1e-6,
           f"a flat +6 dB EQ response yields a -6 dB input offset (got {graph._last_input_offset_db})")

    captured_input: list = []
    graph.estimated_input_level_ready.connect(lambda lv: captured_input.append(lv))
    out_levels = StereoLevels(peak_l=-3.0, peak_r=-9.0, rms_l=-10.0, rms_r=-15.0)
    graph._on_spectrum_stereo_level(out_levels)
    expect(len(captured_input) == 1 and captured_input[0] is not None,
           "estimated_input_level_ready fires with a real StereoLevels once a response is known")
    expect(abs(captured_input[0].peak_l - (out_levels.peak_l - 6.0)) < 1e-6,
           f"estimated input peak_l is the measured peak offset by -6 dB (got {captured_input[0].peak_l})")
    expect(meter.input_level_meter.bar_l == out_levels.peak_l - 6.0,
           "the widget's input meter reflects the synthesized estimate (Peak mode = instant attack)")

    # No calculated response known -> None, input meter idles rather than
    # showing a stale/misleading reading.
    graph._spectrum_response_db = None
    graph._on_spectrum_ready(np.full(n, -20.0))
    captured_input.clear()
    graph._on_spectrum_stereo_level(out_levels)
    expect(captured_input == [None],
           "no calculated response -> estimated_input_level_ready fires with None")
    meter.on_estimated_input_level(None)
    expect(meter.input_level_meter.bar_l == level_meter.FLOOR_DBFS,
           "on_estimated_input_level(None) resets the input meter to the floor")

    panel.graph._btn_live.setChecked(False)
    app.processEvents()

    # ── Reference mode: K-row visibility + K-level round-trip ─────────────

    expect(not meter._k_row.isVisible(), "K-level row is hidden outside Reference mode")
    meter._on_mode_clicked("ref")
    app.processEvents()
    expect(meter.level_meter.mode == "ref", "clicking Ref switches to Reference mode")
    expect(meter._k_row.isVisible(), "K-level row appears once Reference mode is selected")
    expect(meter.k_reference == "k14", "K-reference defaults to K-14 (Katz's general recommendation)")

    meter._on_k_clicked("k20")
    expect(meter.k_reference == "k20", "clicking K20 updates the widget's reference level")
    expect(received[-1] == {"meter_k_reference": "k20"},
           f"K-level click emits meter_settings_changed({{'meter_k_reference': 'k20'}}) (got {received})")

    meter._on_mode_clicked("peak")
    app.processEvents()
    expect(not meter._k_row.isVisible(), "K-level row hides again when leaving Reference mode")

    meter._on_mode_clicked("ref")
    app.processEvents()
    meter.set_mode("vu")
    expect(meter.level_meter.mode == "vu" and not meter._k_row.isVisible(),
           "programmatic set_mode() also hides the K-row when leaving Reference mode")

    meter.grab().save(str(Path(__file__).parent / "selftest_vu_meter_panel.png"))
    print("meter grab saved", flush=True)

    print(("ALL PASS" if not failures else f"{len(failures)} FAILURE(S)"), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
