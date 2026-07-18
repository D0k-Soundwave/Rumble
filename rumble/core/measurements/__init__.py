"""Unified measurement-data layer.

A small provider abstraction over the online measurement databases RUMBLE can
import from. Each :class:`~rumble.core.measurements.base.MeasurementProvider`
wraps one source's I/O module (AutoEQ for headphones, Spinorama for speakers)
behind a common interface, so one browse UI can federate them and new sources
slot in without bolting onto the UI.

Providers fetch *data* over HTTP only (never linking foreign code); sources
whose data is owner-held ship opt-in / off by default. See
:func:`~rumble.core.measurements.registry.enabled_providers`.
"""
from rumble.core.measurements.base import (
    CorrectionOptions, MeasurementEntry, MeasurementProvider,
)
from rumble.core.measurements.federated import FederatedCatalog
from rumble.core.measurements.registry import all_providers, enabled_providers

__all__ = [
    "CorrectionOptions", "MeasurementEntry", "MeasurementProvider",
    "FederatedCatalog", "all_providers", "enabled_providers",
]
