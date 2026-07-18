"""Provider registry — which measurement sources are available/enabled.

Every shipped source is enabled by default (``enabled_by_default = True``); the
flag exists so a source we must *never* auto-enable (e.g. Crinacle) can ship
off. A ``<id>_enabled`` setting can still override per source if one is ever set,
but there is no UI to disable a source — all sources are available at all times.
"""
from __future__ import annotations

from pathlib import Path

from rumble.core.measurements.autoeq_provider import AutoEqProvider
from rumble.core.measurements.base import MeasurementProvider
from rumble.core.measurements.spinorama_provider import SpinoramaProvider

#: Registration order = display order in the browser.
PROVIDER_CLASSES: list[type[MeasurementProvider]] = [
    AutoEqProvider,
    SpinoramaProvider,
]


def all_providers(data_dir: Path) -> list[MeasurementProvider]:
    """Every known provider, instantiated against *data_dir* (ignores settings)."""
    return [cls(data_dir) for cls in PROVIDER_CLASSES]


def enabled_providers(data_dir: Path, settings=None) -> list[MeasurementProvider]:
    """Providers the user has enabled (or that are on by default)."""
    settings = settings or {}
    out: list[MeasurementProvider] = []
    for cls in PROVIDER_CLASSES:
        if settings.get(f"{cls.id}_enabled", cls.enabled_by_default):
            out.append(cls(data_dir))
    return out
