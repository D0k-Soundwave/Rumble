"""AutoEQ provider — headphones/IEMs, wrapping :mod:`rumble.core.autoeq`.

A thin adapter: AutoEQ already publishes finished corrections (GraphicEQ) and
exact APO PEQ text, so the flat/unmodified single-import path still uses
:meth:`fetch_parametric` for byte-for-byte parity with the old dialog. A
non-zero preference tilt (:attr:`CorrectionOptions.slope_db_per_oct`) instead
re-derives the correction from the device's raw/target CSV columns, adding the
tilt on top of AutoEQ's own baked-in target.
"""
from __future__ import annotations

from typing import Any

from rumble.core import autoeq, autoeq_merge, eq_target, target_curves
from rumble.core.measurements.base import (
    CorrectionOptions, MeasurementProvider, Point,
)


class AutoEqProvider(MeasurementProvider):
    id = "autoeq"
    name = "AutoEQ"
    transducer = "headphone"
    license = "MIT"
    attribution = "AutoEQ (jaakkopasanen)"
    commercial_safe = True
    enabled_by_default = True
    supports_tilt = True
    supports_merge = True
    supports_fixed_band = True
    supports_convolution = True

    def load_index(self, *, force_refresh: bool = False) -> list[Any]:
        return autoeq.load_index(self.data_dir, force_refresh=force_refresh)

    def search(self, entries, query="", *, source=None, device_type=None,
               limit=None):
        return autoeq.search(entries, query, source=source,
                             device_type=device_type, limit=limit)

    def sources(self, entries) -> list[str]:
        return autoeq.sources(entries)

    def device_types(self) -> list[str]:
        return ["Over-ear", "In-ear", "Earbud"]

    def fetch_curve(self, entry, *, options: CorrectionOptions | None = None
                    ) -> tuple[list[Point], eq_target.ClampSummary]:
        # AutoEQ's flat/unmodified fetch below is already a finished
        # correction, so it reports the default no-clamp summary. But a
        # preference tilt or a named-target retarget re-derives the curve as
        # a bare `target - measured` delta — the same operation
        # autoeq_merge's gear-simulation delta performs, which has no
        # inherent gain bound of its own (Rumble 2.6: two arbitrary measured
        # curves can differ by more than either curve's own range). Apply the
        # same soft boost/cut cap gear-sim uses so this path can't silently
        # produce an unbounded correction either.
        opt = options or CorrectionOptions()
        if opt.target_curve_name is None and opt.slope_db_per_oct == 0.0:
            return autoeq_merge.parse_graphiceq(autoeq.fetch_graphic(entry)), eq_target.ClampSummary()
        data = autoeq.parse_csv(autoeq.fetch_csv(entry))
        if not data.raw or not data.target:
            return autoeq_merge.parse_graphiceq(autoeq.fetch_graphic(entry)), eq_target.ClampSummary()  # graceful fallback
        grid = [f for f, _ in data.target]
        if opt.target_curve_name:
            # Rumble 3.4: a named curve replaces AutoEQ's own baked-in target
            # entirely (rather than adding a tilt on top of it), resampled
            # onto AutoEQ's own CSV grid via the project's usual log-linear
            # interpolation.
            named_vals = autoeq_merge.interp_log(
                target_curves.get_curve(opt.target_curve_name), grid)
            new_target = list(zip(grid, named_vals))
        else:
            tilt = eq_target.tilt_target(grid, slope_db_per_oct=opt.slope_db_per_oct, pivot_hz=opt.pivot_hz)
            new_target = [(f, t + dt) for (f, t), (_, dt) in zip(data.target, tilt)]
        delta = autoeq_merge.delta_curve(new_target, data.raw)
        return eq_target.clamp_gain(delta)

    def fetch_parametric(self, entry, *, options: CorrectionOptions | None = None) -> str | None:
        if options is not None and (options.slope_db_per_oct != 0.0 or options.target_curve_name):
            return None  # force the fetch_curve path above so the tilt/named target actually applies
        return autoeq.fetch_parametric(entry)

    def fetch_fixed_band(self, entry) -> str | None:
        return autoeq.fetch_fixed(entry)

    def fetch_convolution(self, entry) -> dict[int, bytes] | None:
        out: dict[int, bytes] = {}
        for rate in (44100, 48000):
            try:
                out[rate] = autoeq.fetch_ir(entry, rate)
            except Exception:
                continue
        return out or None

    def _customized(self, opt: CorrectionOptions) -> bool:
        return bool(opt.slope_db_per_oct != 0.0 or opt.target_curve_name)

    def import_name(self, entry, options=None) -> str:
        opt = options or CorrectionOptions()
        return f"{entry.name} ({opt.label})" if self._customized(opt) else entry.name

    def import_tags(self, entry, options=None) -> list[str]:
        tags = ["AutoEQ", entry.device_type, entry.name, entry.source]
        return [t for t in tags if t and t != "Other"]

    def import_description(self, entry, options=None) -> str:
        opt = options or CorrectionOptions()
        base = f"AutoEQ tuning · measured by {entry.location}"
        if not self._customized(opt):
            return base
        if opt.target_curve_name:
            return f"{base}. Targeted toward {target_curves.label(opt.target_curve_name)} instead of AutoEQ's own baked-in target."
        return f"{base}. Preference tilt on top of AutoEQ's Harman target: {opt.slope_db_per_oct:+.2f} dB/oct."
