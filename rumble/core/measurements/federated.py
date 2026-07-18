"""Federated catalogue — every enabled provider searched as one unified source.

Wraps a list of :class:`MeasurementProvider` so the browse UI sees a *single*
catalogue: one search box, one results list spanning headphones (AutoEQ) and
speakers (Spinorama), with the transducer exposed as a device-type **facet**
("Over-ear", "In-ear", "Earbud", "Speaker") rather than a mode switch.

It deliberately mirrors the provider method surface the UI already calls
(``search``/``sources``/``device_types``/``badge``/``fetch_curve``/…) so the
existing ``_MeasurementPanel`` works against it unchanged. Per-entry operations
(import, fetch, badge) dispatch back to the owning provider via
``MeasurementEntry.provider_id``.
"""
from __future__ import annotations

from rumble.core import autoeq_merge
from rumble.core.eq_target import ClampSummary
from rumble.core.measurements.base import (
    CorrectionOptions, MeasurementEntry, MeasurementProvider, Point,
)

# Facet display order: headphone types first, then speakers, then anything else.
_TYPE_ORDER = {"Over-ear": 0, "In-ear": 1, "Earbud": 2, "Speaker": 3, "Other": 8}


class FederatedCatalog:
    """A single browseable catalogue federating several providers."""

    name = "Measurement Databases"
    transducer = "mixed"

    def __init__(self, providers: list[MeasurementProvider]):
        self.providers = list(providers)
        self._by_id = {p.id: p for p in self.providers}
        self.supports_merge = any(p.supports_merge for p in self.providers)
        self.supports_tilt = any(p.supports_tilt for p in self.providers)
        self.supports_fixed_band = any(p.supports_fixed_band for p in self.providers)
        self.supports_convolution = any(p.supports_convolution for p in self.providers)

    # ── index ────────────────────────────────────────────────────────
    def load_index(self, *, force_refresh: bool = False) -> list[MeasurementEntry]:
        """Load every provider's index and return one merged, wrapped list.

        A provider that fails (e.g. offline with no cache) is skipped; only if
        *every* provider fails is an error raised, so one source being down never
        blanks the whole browser.
        """
        entries: list[MeasurementEntry] = []
        errors: list[str] = []
        for provider in self.providers:
            try:
                native = provider.load_index(force_refresh=force_refresh)
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                errors.append(f"{provider.name}: {exc}")
                continue
            entries.extend(self._wrap(provider, e) for e in native)
        if not entries and errors:
            raise RuntimeError("; ".join(errors))
        return entries

    def _wrap(self, provider: MeasurementProvider, entry) -> MeasurementEntry:
        device_type = getattr(entry, "device_type", "") or (
            "Speaker" if provider.transducer == "speaker" else "Other")
        return MeasurementEntry(
            provider_id=provider.id, name=entry.name,
            source=getattr(entry, "source", ""), transducer=provider.transducer,
            device_type=device_type, license=provider.license,
            attribution=provider.attribution, raw=entry)

    # ── facets / search (panel-compatible) ───────────────────────────
    def device_types(self) -> list[str]:
        seen: list[str] = []
        for provider in self.providers:
            types = provider.device_types() or (
                ["Speaker"] if provider.transducer == "speaker" else [])
            for t in types:
                if t not in seen:
                    seen.append(t)
        return sorted(seen, key=lambda t: _TYPE_ORDER.get(t, 7))

    def sources(self, entries: list[MeasurementEntry]) -> list[str]:
        return sorted({e.source for e in entries if e.source})

    def search(self, entries: list[MeasurementEntry], query: str = "", *,
               source: str | None = None, device_type: str | None = None,
               limit: int | None = None) -> list[MeasurementEntry]:
        """Unified filter/rank across all providers' entries.

        Same ranking both providers use individually (name-prefix > name-substring
        > rest), so a federated search reads identically to a single-source one.
        """
        tokens = [t for t in query.lower().split() if t]
        q = query.strip().lower()

        def matches(e: MeasurementEntry) -> bool:
            if device_type and e.device_type != device_type:
                return False
            if source and e.source != source:
                return False
            if not tokens:
                return True
            hay = f"{e.name} {e.source}".lower()
            return all(tok in hay for tok in tokens)

        def rank(e: MeasurementEntry) -> tuple:
            name = e.name.lower()
            if q and name.startswith(q):
                tier = 0
            elif q and q in name:
                tier = 1
            else:
                tier = 2
            return (tier, name, e.source.lower())

        out = sorted((e for e in entries if matches(e)), key=rank)
        return out[:limit] if limit is not None else out

    # ── per-entry dispatch to the owning provider ────────────────────
    def _provider(self, entry: MeasurementEntry) -> MeasurementProvider:
        return self._by_id[entry.provider_id]

    def fetch_curve(self, entry: MeasurementEntry, *,
                    options: CorrectionOptions | None = None
                    ) -> tuple[list[Point], ClampSummary]:
        return self._provider(entry).fetch_curve(entry.raw, options=options)

    def fetch_parametric(self, entry: MeasurementEntry, *,
                    options: CorrectionOptions | None = None) -> str | None:
        return self._provider(entry).fetch_parametric(entry.raw, options=options)

    def fetch_fixed_band(self, entry: MeasurementEntry) -> str | None:
        return self._provider(entry).fetch_fixed_band(entry.raw)

    def fetch_convolution(self, entry: MeasurementEntry) -> dict[int, bytes] | None:
        return self._provider(entry).fetch_convolution(entry.raw)

    def suggested_filters(self, entry: MeasurementEntry) -> list:
        return self._provider(entry).suggested_filters(entry.raw)

    # ── multi-measurement consensus (Rumble 3.1) ─────────────────────
    def same_device_entries(self, entries: list[MeasurementEntry],
                            entry: MeasurementEntry) -> list[MeasurementEntry]:
        """Every entry in *entries* that is "the same device" as *entry*:
        same provider (never group a headphone with a speaker), same
        normalized name (see :func:`autoeq_merge.normalize_device_key`).
        Includes *entry* itself, so a device with only one measurement
        returns a list of one."""
        key = autoeq_merge.normalize_device_key(entry.name)
        return [e for e in entries if e.provider_id == entry.provider_id
                and autoeq_merge.normalize_device_key(e.name) == key]

    def fetch_consensus_curve(self, entries: list[MeasurementEntry], *,
                              options: CorrectionOptions | None = None
                              ) -> tuple[list[Point], list[tuple[float, float, float]],
                                        list[str], ClampSummary]:
        """Average every entry in *entries* (same device, several
        sources/rigs — see :meth:`same_device_entries`) into one consensus
        curve, plus the per-frequency min/max spread across them and the
        combined clamp summary. Entries that fail to fetch are skipped
        rather than failing the whole consensus."""
        curves: list[list[Point]] = []
        sources: list[str] = []
        weights: list[float] = []
        clamped_boost = clamped_cut = 0
        max_delta = 0.0
        for entry in entries:
            provider = self._provider(entry)
            points, clamp = provider.fetch_curve(entry.raw, options=options)
            if points:
                curves.append(points)
                sources.append(entry.source or entry.location)
                weights.append(provider.entry_weight(entry.raw))
                clamped_boost += clamp.points_clamped_boost
                clamped_cut += clamp.points_clamped_cut
                max_delta = max(max_delta, clamp.max_clamped_delta_db)
        # Quality-weighted where a provider distinguishes measurement quality
        # (Spinorama); every weight is 1.0 (flat average) otherwise (AutoEQ) —
        # Rumble 3.1's consensus averaging and Rumble 3.2's quality weighting
        # share this one code path.
        consensus = autoeq_merge.average_curves(curves, weights=weights)
        spread = autoeq_merge.spread_curves(curves)
        summary = ClampSummary(clamped_boost, clamped_cut, max_delta)
        return consensus, spread, sources, summary

    def import_name(self, entry: MeasurementEntry,
                    options: CorrectionOptions | None = None) -> str:
        return self._provider(entry).import_name(entry.raw, options)

    def import_tags(self, entry: MeasurementEntry,
                    options: CorrectionOptions | None = None) -> list[str]:
        return self._provider(entry).import_tags(entry.raw, options)

    def import_description(self, entry: MeasurementEntry,
                          options: CorrectionOptions | None = None) -> str:
        return self._provider(entry).import_description(entry.raw, options)

    def badge(self, entry: MeasurementEntry) -> str:
        return self._provider(entry).badge(entry.raw)
