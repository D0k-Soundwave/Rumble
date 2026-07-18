"""Provider interface for the measurement-data layer.

A *provider* wraps one online measurement source (AutoEQ, Spinorama, …) behind a
common interface. The browse UI talks only to providers, so adding a source is a
new provider plus its I/O module — no UI surgery.

Two layers of detail:

* The UI's reusable search panel works with each provider's **native entry**
  objects (``AutoEqEntry`` / ``SpinoramaEntry``). They are duck-typed: every
  entry exposes ``name``, ``source``, ``location``, ``display`` and
  ``device_type`` (already true of both today). Providers expose
  :meth:`load_index`, :meth:`search`, :meth:`sources` and :meth:`device_types`
  over those entries.
* Importing converges on a single **correction curve** (a list of
  ``(freq_hz, gain_db)`` points, ready to store as a GraphicEQ) plus a
  :class:`~rumble.core.eq_target.ClampSummary` of any boost/cut clamping that
  was applied (all-zero when none), returned together by :meth:`fetch_curve`.
  AutoEQ results are already corrections, so they always report a no-clamp
  summary; a speaker's raw measurement is turned into one via
  :mod:`rumble.core.eq_target` using :class:`CorrectionOptions`, whose clamp
  summary reflects whatever :func:`~rumble.core.eq_target.make_correction`
  actually capped. AutoEQ additionally offers :meth:`fetch_parametric`
  (exact APO PEQ text) so its single-import path stays byte-for-byte unchanged.

:class:`MeasurementEntry` is a light, source-agnostic wrapper used where code
prefers not to depend on a concrete entry type (e.g. tests, future callers).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rumble.core.eq_target import ClampSummary

Point = tuple[float, float]


@dataclass
class CorrectionOptions:
    """Knobs for turning a raw speaker measurement into a correction.

    ``slope_db_per_oct`` is the target tilt: ``0`` = flat ("anechoic starting
    point"), a mild negative (~ -0.9) = room-preference tilt for users who won't
    measure their room. ``label`` is folded into the generated profile name.
    Ignored by providers whose data is already a finished correction (AutoEQ).
    """

    slope_db_per_oct: float = 0.0
    pivot_hz: float = 1000.0
    smooth_oct: float = 1 / 3
    max_boost_db: float = 6.0
    label: str = "anechoic"
    curve_basis: str = "Listening Window"
    directivity_limiting: bool = True
    # Rumble 3.6: per-frequency directivity-gap suppression — distinct from
    # directivity_limiting above (a single per-speaker cap-damping scalar).
    # This fetches the speaker's Sound Power curve alongside its Listening
    # Window and suppresses correction wherever their gap deviates sharply
    # from its own local trend (see eq_target.directivity_gap_suppression's
    # module comment for the real-data derivation). Default True: like
    # adaptive_smoothing, this only ever reaches auto-generated corrections.
    directivity_gap_suppression: bool = True
    # Rumble 2.2/2.3: frequency-dependent smoothing + soft no-boost-above-
    # ~8kHz guardrail (AutoEQ's own published treble convention — see
    # rumble.core.eq_target's module comment). Default True: this option only
    # ever reaches auto-generated corrections (make_correction()), never
    # manual GEQ/PEQ editing.
    adaptive_smoothing: bool = True
    # Rumble 3.4: a name from rumble.core.target_curves, or None (default) to
    # keep the existing slope_db_per_oct/pivot_hz linear tilt behavior.
    target_curve_name: str | None = None


@dataclass(frozen=True)
class MeasurementEntry:
    """Source-agnostic view of one measurement (light wrapper over a native entry)."""

    provider_id: str
    name: str
    source: str
    transducer: str
    device_type: str
    license: str
    attribution: str
    raw: Any  # the native AutoEqEntry / SpinoramaEntry

    @property
    def location(self) -> str:
        return getattr(self.raw, "location", self.source)

    @property
    def display(self) -> str:
        return getattr(self.raw, "display", f"{self.name}  —  {self.location}")


class MeasurementProvider(ABC):
    """One online measurement source behind a common interface.

    Subclasses set the class attributes and implement the index/fetch methods.
    Methods operate on the provider's native entry objects (see module docstring).
    """

    id: str = ""
    name: str = ""
    transducer: str = "headphone"      # "headphone" | "speaker"
    license: str = ""
    attribution: str = ""
    commercial_safe: bool = False
    enabled_by_default: bool = False
    #: Whether this source's data needs a target/tilt to become a correction
    #: (speakers) — gates the tilt control in the UI.
    supports_tilt: bool = False
    #: Whether the multi-measurement merge workflow applies (AutoEQ).
    supports_merge: bool = False
    #: Whether this source publishes a fixed-band EQ variant (AutoEQ).
    supports_fixed_band: bool = False
    #: Whether this source publishes convolution/impulse-response data (AutoEQ).
    supports_convolution: bool = False

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    # ── index / search (over native entries) ─────────────────────────
    @abstractmethod
    def load_index(self, *, force_refresh: bool = False) -> list[Any]:
        """Return all entries, cached on disk and reused while fresh."""

    @abstractmethod
    def search(self, entries: list[Any], query: str = "", *,
               source: str | None = None, device_type: str | None = None,
               limit: int | None = None) -> list[Any]:
        """Filter/rank entries by free-text query plus optional facets."""

    @abstractmethod
    def sources(self, entries: list[Any]) -> list[str]:
        """Distinct source-facet values present in *entries*."""

    def device_types(self) -> list[str]:
        """Device-type facet values, or ``[]`` for no type facet (speakers)."""
        return []

    # ── import (converges on a correction curve / APO text) ──────────
    @abstractmethod
    def fetch_curve(self, entry: Any, *,
                    options: CorrectionOptions | None = None
                    ) -> tuple[list[Point], ClampSummary]:
        """Fetch *entry* and return a ready-to-store correction curve plus a
        summary of any boost/cut clamping applied (all-zero when none)."""

    def fetch_parametric(self, entry: Any, *,
                         options: CorrectionOptions | None = None) -> str | None:
        """Exact APO ParametricEQ text, if the source ships one (AutoEQ)."""
        return None

    def fetch_fixed_band(self, entry: Any) -> str | None:
        """Exact APO fixed-band PEQ text, if the source ships one (AutoEQ)."""
        return None

    def fetch_convolution(self, entry: Any) -> dict[int, bytes] | None:
        """{sample_rate_hz: wav_bytes} for every rate the source publishes, or
        None when unsupported (default)."""
        return None

    def suggested_filters(self, entry: Any) -> list:
        """Extra APO Filter objects worth adding alongside the correction curve
        for this entry (e.g. a subsonic high-pass). [] by default."""
        return []

    def entry_weight(self, entry: Any) -> float:
        """Relative confidence weight for *entry* when averaging it with other
        measurements of the same device (Rumble 3.2) — e.g. Spinorama's
        quality tier. ``1.0`` (flat) by default."""
        return 1.0

    # ── profile metadata for an imported entry ───────────────────────
    @abstractmethod
    def import_name(self, entry: Any, options: CorrectionOptions | None = None) -> str:
        """Profile name for a single-entry import."""

    @abstractmethod
    def import_tags(self, entry: Any,
                    options: CorrectionOptions | None = None) -> list[str]:
        """Tags for a single-entry import."""

    @abstractmethod
    def import_description(self, entry: Any,
                          options: CorrectionOptions | None = None) -> str:
        """Description (profile hover text) for a single-entry import."""

    def badge(self, entry: Any) -> str:
        """Short attribution badge: ``source · license``."""
        loc = getattr(entry, "location", getattr(entry, "source", ""))
        return f"{loc} · {self.license}" if self.license else loc
