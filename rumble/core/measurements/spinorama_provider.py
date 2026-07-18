"""Spinorama provider — speakers, wrapping :mod:`rumble.core.spinorama`.

Speaker data is a raw anechoic Listening Window, not a finished correction, so
:meth:`fetch_curve` runs it through :mod:`rumble.core.eq_target` toward a tilt
target (see :class:`CorrectionOptions`), band-limited at the speaker's own -6 dB
extension so its natural roll-off isn't boosted. Opt-in / off by default with
``commercial_safe = False`` because the underlying measurements are owner-held.
"""
from __future__ import annotations

from typing import Any

from rumble.core import apo_config, eq_target, spinorama, target_curves
from rumble.core.measurements.base import (
    CorrectionOptions, MeasurementProvider, Point,
)

_BAND_HIGH = 16000.0
# Rumble 3.6: companion curve for eq_target.directivity_gap_suppression()'s
# real-data-derived thresholds (Sound Power specifically, not Early
# Reflections — that's the curve the reference-speaker calibration used).
_GAP_SECONDARY_CURVE = "Sound Power"


class SpinoramaProvider(MeasurementProvider):
    id = "spinorama"
    name = "Spinorama (speakers)"
    transducer = "speaker"
    license = "data: owner-held (ASR/EAC)"
    attribution = "spinorama.org"
    commercial_safe = False
    enabled_by_default = True
    supports_tilt = True
    supports_merge = True

    def load_index(self, *, force_refresh: bool = False) -> list[Any]:
        return spinorama.load_index(self.data_dir, force_refresh=force_refresh)

    def search(self, entries, query="", *, source=None, device_type=None,
               limit=None):
        return spinorama.search(entries, query, source=source, limit=limit)

    def sources(self, entries) -> list[str]:
        return spinorama.sources(entries)

    def device_types(self) -> list[str]:
        return []

    def fetch_curve(self, entry, *, options: CorrectionOptions | None = None
                    ) -> tuple[list[Point], eq_target.ClampSummary]:
        opt = options or CorrectionOptions()
        basis = opt.curve_basis or spinorama.DEFAULT_CURVE
        lw = spinorama.fetch_curve(entry, curve=basis, data_dir=self.data_dir)
        if not lw:
            return [], eq_target.ClampSummary()
        # Prefer -3 dB (a more conservative, higher-frequency estimate of the
        # driver's true usable extension) and fall back to -6 dB, the same
        # combining logic suggested_filters() below already uses for the
        # highpass suggestion -- rather than reading ref_6db alone, which is
        # 0.0/"unknown" whenever a measurement only published -3 dB.
        band_low = eq_target.suggested_highpass_hz(
            getattr(entry, "ref_3db", 0.0), getattr(entry, "ref_6db", 0.0)) or 20.0
        cap = opt.max_boost_db
        if opt.directivity_limiting:
            cap = eq_target.directivity_boost_cap(
                cap,
                horizontal_avg=getattr(entry, "directivity_horizontal_avg", 0.0),
                vertical_avg=getattr(entry, "directivity_vertical_avg", 0.0))
        named_curve = (target_curves.get_curve(opt.target_curve_name)
                      if opt.target_curve_name else None)
        secondary = None
        if opt.directivity_gap_suppression and basis != _GAP_SECONDARY_CURVE:
            secondary = spinorama.fetch_curve(
                entry, curve=_GAP_SECONDARY_CURVE, data_dir=self.data_dir) or None
        return eq_target.make_correction(
            lw, slope_db_per_oct=opt.slope_db_per_oct, pivot_hz=opt.pivot_hz,
            smooth_oct=opt.smooth_oct, max_boost_db=cap,
            band=(band_low, _BAND_HIGH),
            adaptive_smoothing=opt.adaptive_smoothing,
            target_curve=named_curve,
            directivity_secondary=secondary)

    def suggested_filters(self, entry) -> list:
        fc = eq_target.suggested_highpass_hz(getattr(entry, "ref_3db", 0.0),
                                              getattr(entry, "ref_6db", 0.0))
        return [apo_config.Filter(type="HPQ", fc=fc, q=0.707)] if fc else []

    def entry_weight(self, entry) -> float:
        return spinorama.quality_weight(getattr(entry, "quality", ""))

    def import_name(self, entry, options=None) -> str:
        opt = options or CorrectionOptions()
        return f"{entry.name} ({opt.label})"

    def import_tags(self, entry, options=None) -> list[str]:
        tags = ["Spinorama", "Speaker", entry.name, entry.source]
        return [t for t in tags if t]

    def import_description(self, entry, options=None) -> str:
        opt = options or CorrectionOptions()
        if opt.target_curve_name:
            tilt = target_curves.label(opt.target_curve_name)
        elif opt.slope_db_per_oct == 0:
            tilt = "flat (anechoic)"
        else:
            tilt = f"{opt.slope_db_per_oct:+.2f} dB/oct tilt"
        return (f"Spinorama anechoic correction · measured by {entry.source}. "
                f"Listening Window toward a {tilt} target — an anechoic starting "
                "point; refine with a room measurement (REW) if you can.")
