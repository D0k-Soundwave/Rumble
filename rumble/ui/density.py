"""UI density: compact metrics are the app's only metrics. Widgets call
dp(standard, compact) for any fixed metric (heights, widths, margins) at
construction time; the theme engine uses it to emit the QSS. The first
argument is the retired roomier "Standard" value, kept at every call site
as documentation and as an easy way back if a looser layout ever returns;
only the compact value is used. The Double interface size scales this
compact layout 2x via QT_SCALE_FACTOR (see main._apply_ui_scale)."""
from __future__ import annotations


def dp(standard: int, compact: int) -> int:
    """The metric for the app's (compact) density."""
    return compact
