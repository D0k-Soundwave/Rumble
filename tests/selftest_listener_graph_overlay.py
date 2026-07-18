# tests/selftest_listener_graph_overlay.py
"""Selftest: EQGraph's optional L/R-effective listener-correction overlay."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtWidgets import QApplication

from rumble.core import eq_math
from rumble.ui.eq_graph import EQGraph
from rumble.ui.theme.tokens import THEMES, DEFAULT_THEME_NAME

_app = QApplication.instance() or QApplication([])


def test_listener_overlay_hidden_by_default():
    graph = EQGraph(THEMES[DEFAULT_THEME_NAME])
    assert graph._listener_l_curve.isVisible() is False
    assert graph._listener_r_curve.isVisible() is False


def test_set_listener_correction_shows_and_hides_overlay():
    graph = EQGraph(THEMES[DEFAULT_THEME_NAME])
    freqs = eq_math.frequency_array()
    left = np.full_like(freqs, 3.0)
    right = np.zeros_like(freqs)
    graph.set_listener_correction(left, right)
    assert graph._listener_l_curve.isVisible() is True
    assert graph._listener_r_curve.isVisible() is True

    graph.set_listener_correction(None, None)
    assert graph._listener_l_curve.isVisible() is False
    assert graph._listener_r_curve.isVisible() is False


def test_listener_overlay_curve_is_base_response_plus_correction():
    graph = EQGraph(THEMES[DEFAULT_THEME_NAME])
    freqs = eq_math.frequency_array()
    left = np.full_like(freqs, 3.0)
    right = np.full_like(freqs, -1.0)  # never happens in practice (never-cut
                                        # invariant lives in listener_correction,
                                        # not the graph) -- exercises the math only
    graph.set_listener_correction(left, right)
    l_data = graph._listener_l_curve.yData
    r_data = graph._listener_r_curve.yData
    # base combined response is 0 (no filters/GEQ/IR set)
    assert np.allclose(l_data, 3.0)
    assert np.allclose(r_data, -1.0)


# append to tests/selftest_listener_graph_overlay.py
from rumble.ui.editor_panel import EditorPanel
from rumble.core.listener_correction import EarCorrection


def test_editor_panel_refreshes_overlay_from_provider():
    panel = EditorPanel(THEMES[DEFAULT_THEME_NAME])
    corr = EarCorrection(left=[(1000.0, 3.0)], right=[])
    panel.set_listener_correction_provider(lambda: corr)
    panel.refresh_listener_overlay()
    assert panel.graph._listener_l_curve.isVisible() is True


def test_editor_panel_hides_overlay_when_correction_flat():
    panel = EditorPanel(THEMES[DEFAULT_THEME_NAME])
    panel.set_listener_correction_provider(lambda: EarCorrection())
    panel.refresh_listener_overlay()
    assert panel.graph._listener_l_curve.isVisible() is False
