"""Spinorama.org online speaker-measurement database access.

Spinorama (https://www.spinorama.org, code GPL-3) aggregates anechoic speaker
measurements (Klippel NFS) from Audio Science Review, Erin's Audio Corner and
others, presented as CEA-2034 "spinorama" plots. We never link or vendor their
GPL-3 *code*; this module only fetches published *data* over HTTP — exactly as
:mod:`rumble.core.autoeq` does for AutoEQ — so the source must ship opt-in, off by
default, with per-result attribution (the underlying measurements are owner-held).

This module is pure I/O + parsing (stdlib only — no Qt, no extra deps). It mirrors
:mod:`rumble.core.autoeq`: a cached, sharded JSON *index* of every speaker plus an
on-demand fetch of a single measurement's curves.

Endpoints (pinned by inspecting the site's network traffic)::

    index   {BASE}/json/metadata-<shard>.json   shard in {head, 1970, 2019..now}
    curves  {BASE}/speakers/<name>/<origin>/<key>/CEA2034.json

The index maps ``"<Brand Model>" -> {brand, model, type, shape,
default_measurement, measurements: {<key>: {origin, quality, review,
estimates: {ref_6dB, ...}}}}``. A speaker may have several measurements; we list
one :class:`SpinoramaEntry` per measurement (parallel to AutoEQ's per-source rows).

The curves file is a Plotly figure ``{"data": [traces], "layout": {...}}``; the
traces named "On Axis" / "Listening Window" / "Early Reflections" / "Sound Power"
carry the response. Each trace's ``x``/``y`` is either a plain list or a Plotly
typed array ``{"dtype": "f8", "bdata": "<base64 little-endian>"}``.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import struct
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE = "https://www.spinorama.org"
META_BASE = f"{BASE}/json"
SPEAKERS_BASE = f"{BASE}/speakers"

CACHE_FILENAME = "spinorama_index.json"
MEAS_CACHE_DIR = "spinorama_cache"
DEFAULT_MAX_AGE_DAYS = 7
_USER_AGENT = "RUMBLE-EQ-Manager (+https://www.spinorama.org)"

#: Trace we base a correction on (smoother than raw On Axis).
DEFAULT_CURVE = "Listening Window"

# Rumble 3.2: Spinorama's own `quality` field is categorical — real index
# data (data/spinorama_index.json, checked 2026-07-10) has exactly four
# values in this frequency: high=729, medium=318, low=313, unknown=44 — no
# numeric score is published, so there is nothing to derive a weight *from*
# beyond a monotonic ranking. "unknown" is treated identically to "low"
# (weight 1.0) rather than given the benefit of the doubt, since it carries
# strictly less confidence information than an explicit "low" rating, not
# more. The 3/2/1 spacing is a plain linear ranking (no claim of a precise
# ratio between tiers — Spinorama doesn't publish one).
QUALITY_WEIGHTS = {"high": 3.0, "medium": 2.0, "low": 1.0, "unknown": 1.0}
DEFAULT_QUALITY_WEIGHT = 1.0


def quality_weight(quality: str) -> float:
    """Merge weight for a measurement's quality tier (Rumble 3.2). Falls back
    to the lowest known weight for blank/unrecognized values."""
    return QUALITY_WEIGHTS.get((quality or "").strip().lower(), DEFAULT_QUALITY_WEIGHT)

#: Which measurement file each curve basis lives in. On Axis, Listening
#: Window, Early Reflections and Sound Power are all traces inside the same
#: CEA2034 Plotly figure; Estimated In-Room Response is a separate file.
_CURVE_TO_FILE = {
    "On Axis": "CEA2034",
    "Listening Window": "CEA2034",
    "Early Reflections": "CEA2034",
    "Sound Power": "CEA2034",
    "Estimated In-Room Response": "Estimated In-Room Response",
}

# Plotly typed-array dtype -> struct format char (little-endian).
_DTYPE_FMT = {
    "f8": "d", "f4": "f", "i8": "q", "i4": "i", "i2": "h", "i1": "b",
    "u8": "Q", "u4": "I", "u2": "H", "u1": "B",
}


@dataclass(frozen=True)
class SpinoramaEntry:
    """One speaker measurement: a speaker measured by a given origin/reviewer."""

    name: str            # "<Brand> <Model>" — the index key
    brand: str
    model: str
    source: str          # measurement origin, e.g. "ASR", "ErinsAudioCorner"
    measurement_key: str  # measurement id within the speaker, e.g. "asr"
    quality: str = ""
    review: str = ""
    shape: str = ""
    speaker_type: str = ""  # "active" | "passive"
    ref_6db: float = 0.0    # -6 dB low-frequency extension (Hz), 0 if unknown
    ref_3db: float = 0.0    # -3 dB low-frequency extension (Hz), 0 if unknown
    # Angular window (degrees) within which horizontal/vertical directivity
    # stays even, per Spinorama's own estimate; 0 if not published for this
    # measurement. See eq_target.directivity_boost_cap() for how these are
    # used (Phase F of the RUMBLE measurement-integration plan).
    directivity_horizontal_avg: float = 0.0
    directivity_vertical_avg: float = 0.0

    device_type: str = "Speaker"

    @property
    def location(self) -> str:
        return f"{self.source} · {self.quality}" if self.quality else self.source

    @property
    def display(self) -> str:
        return f"{self.name}  —  {self.location}"

    def measurement_url(self, curve_file: str = "CEA2034") -> str:
        from urllib.parse import quote
        return (f"{SPEAKERS_BASE}/{quote(self.name)}/{quote(self.source)}/"
                f"{quote(self.measurement_key)}/{curve_file}.json")


def _http_get(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _index_shards() -> list[str]:
    """Metadata shard names. Year shards auto-extend, so new years need no change."""
    year = datetime.datetime.now().year
    return ["head", "1970"] + [str(y) for y in range(2019, year + 1)]


def _fetch_index_raw() -> dict:
    """Download and merge every metadata shard into one ``{name: info}`` dict.

    Missing/!200 shards are skipped (a future year that doesn't exist yet, etc.).
    Raises only if *every* shard fails (no data at all).
    """
    merged: dict = {}
    for shard in _index_shards():
        try:
            text = _http_get(f"{META_BASE}/metadata-{shard}.json")
            merged.update(json.loads(text))
        except Exception:
            continue
    if not merged:
        raise RuntimeError("Spinorama index unavailable (no shards fetched)")
    return merged


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_index(raw: dict | str) -> list[SpinoramaEntry]:
    """Flatten the merged metadata mapping into one entry per measurement."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    entries: list[SpinoramaEntry] = []
    for name, info in data.items():
        if not isinstance(info, dict):
            continue
        measurements = info.get("measurements") or {}
        for key, meas in measurements.items():
            if not isinstance(meas, dict):
                continue
            est = meas.get("estimates") or {}
            # Kept separate and NOT cross-substituted: a driver's -3 dB point
            # is physically higher-frequency than its -6 dB point, so filling
            # a missing ref_6dB from ref_3dB would mislabel the field with a
            # value that reads as more extension than the speaker actually
            # has. ref_6db stays 0.0 ("unknown", per its own docstring) when
            # only -3 dB was published; callers that want "whichever estimate
            # is available" should combine both fields themselves (see
            # eq_target.suggested_highpass_hz(), which already does this
            # correctly for both the highpass suggestion and the correction
            # curve's low-frequency taper edge).
            ref6 = _num(est.get("ref_6dB"))
            ref3 = _num(est.get("ref_3dB"))
            dir_h = _num(est.get("directivity_horizontal_avg"))
            dir_v = _num(est.get("directivity_vertical_avg"))
            entries.append(SpinoramaEntry(
                name=name,
                brand=str(info.get("brand", "")),
                model=str(info.get("model", "")),
                source=str(meas.get("origin", "")),
                measurement_key=str(key),
                quality=str(meas.get("quality", "")),
                review=str(meas.get("review", "")),
                shape=str(info.get("shape", "")),
                speaker_type=str(info.get("type", "")),
                ref_6db=ref6,
                ref_3db=ref3,
                directivity_horizontal_avg=dir_h,
                directivity_vertical_avg=dir_v,
            ))
    entries.sort(key=lambda e: (e.name.lower(), e.source.lower()))
    return entries


def load_index(data_dir: Path, *, force_refresh: bool = False,
               max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> list[SpinoramaEntry]:
    """Return the full speaker index, served from a fresh local cache when able.

    The merged index JSON is cached in ``data_dir`` and reused while younger than
    ``max_age_days``. A forced/expired refresh re-downloads; on network failure a
    stale cache (if any) is still used so the browser works offline. Raises only
    on the first run with no network and no cache.
    """
    cache = Path(data_dir) / CACHE_FILENAME
    text: str | None = None

    if not force_refresh and cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < max_age_days * 86400:
            try:
                text = cache.read_text(encoding="utf-8")
            except OSError:
                text = None

    if text is None:
        try:
            raw = _fetch_index_raw()
            text = json.dumps(raw)
            try:
                cache.write_text(text, encoding="utf-8")
            except OSError:
                pass
        except Exception:
            if cache.exists():
                text = cache.read_text(encoding="utf-8")  # stale fallback
            else:
                raise

    return parse_index(text)


def _decode_array(obj) -> list[float]:
    """Decode a Plotly axis array: plain list or ``{dtype, bdata}`` typed array."""
    if isinstance(obj, list):
        return [float(v) for v in obj]
    if isinstance(obj, dict) and "bdata" in obj:
        fmt = _DTYPE_FMT.get(str(obj.get("dtype", "f8")), "d")
        raw = base64.b64decode(obj["bdata"])
        size = struct.calcsize(fmt)
        n = len(raw) // size
        return list(struct.unpack(f"<{n}{fmt}", raw[:n * size]))
    return []


def extract_curve(figure: dict, name: str = DEFAULT_CURVE) -> list[tuple[float, float]]:
    """Pull one named trace from a CEA2034 figure as sorted ``(freq, gain)`` points."""
    for trace in figure.get("data", []):
        if trace.get("name") == name:
            xs = _decode_array(trace.get("x"))
            ys = _decode_array(trace.get("y"))
            pts = [(float(f), float(g)) for f, g in zip(xs, ys) if f > 0]
            pts.sort(key=lambda p: p[0])
            return pts
    return []


def fetch_measurement(entry: SpinoramaEntry, *, curve_file: str = "CEA2034",
                      data_dir: Path | None = None,
                      max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict:
    """Fetch one measurement file's Plotly figure (cached on disk when *data_dir*).

    ``curve_file`` picks which figure to fetch — most curve bases share the
    ``CEA2034`` figure (it's a multi-trace spinorama plot); Estimated In-Room
    Response lives in its own file. The payload is ~40 KB, so unlike AutoEQ's
    one-line files it's worth caching (keyed by URL hash, so different files
    naturally get separate cache entries).
    """
    url = entry.measurement_url(curve_file)
    cache: Path | None = None
    if data_dir is not None:
        cdir = Path(data_dir) / MEAS_CACHE_DIR
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        cache = cdir / f"{digest}.json"
        if cache.exists() and (time.time() - cache.stat().st_mtime) < max_age_days * 86400:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    text = _http_get(url)
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return json.loads(text)


def fetch_curve(entry: SpinoramaEntry, *, curve: str = DEFAULT_CURVE,
                data_dir: Path | None = None) -> list[tuple[float, float]]:
    """Fetch one measurement and return its named response curve."""
    figure = fetch_measurement(entry, curve_file=_CURVE_TO_FILE.get(curve, "CEA2034"),
                               data_dir=data_dir)
    return extract_curve(figure, curve)


def search(entries: list[SpinoramaEntry], query: str = "", *,
           source: str | None = None,
           limit: int | None = None) -> list[SpinoramaEntry]:
    """Filter by free-text query plus optional source facet, ranked like AutoEQ.

    Every whitespace token must appear (case-insensitively) in name or source;
    results are ranked name-prefix > name-substring > rest, alphabetised within.
    """
    tokens = [t for t in query.lower().split() if t]
    q = query.strip().lower()

    def matches(e: SpinoramaEntry) -> bool:
        if source and e.source != source:
            return False
        if not tokens:
            return True
        hay = f"{e.name} {e.source}".lower()
        return all(tok in hay for tok in tokens)

    def rank(e: SpinoramaEntry) -> tuple:
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


def sources(entries: list[SpinoramaEntry]) -> list[str]:
    return sorted({e.source for e in entries if e.source})
