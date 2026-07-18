"""AutoEQ online database access.

AutoEQ (https://github.com/jaakkopasanen/AutoEq) publishes ready-to-use
parametric EQ files for ~4,000 headphones and IEMs. Each device's
``<Name> ParametricEQ.txt`` is already valid Equalizer APO syntax
(``Preamp:`` + ``Filter N: ON PK/LSC/HSC Fc … Gain … Q …``), so RUMBLE can
ingest it through the normal profile parser with no translation step.

This module is pure I/O + parsing (stdlib ``urllib`` only — no extra
dependencies and no Qt). It downloads and caches the master ``INDEX.md``
(a flat markdown list of every result) and fetches an individual device's
parametric file on demand. The UI layer runs these calls off the GUI thread.

INDEX.md line format::

    - [Device Name](./<source>/<rig>/<Device Name>) by <source> on <rig>

where the path is URL-encoded and relative to ``results/``. The device's
parametric file lives at ``<path>/<Device Name> ParametricEQ.txt``.
"""
from __future__ import annotations

import csv
import io
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

Point = tuple[float, float]

RAW_BASE = "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master"
RESULTS_BASE = f"{RAW_BASE}/results"
INDEX_URL = f"{RESULTS_BASE}/INDEX.md"

CACHE_FILENAME = "autoeq_index.md"
DEFAULT_MAX_AGE_DAYS = 7
_USER_AGENT = "RUMBLE-EQ-Manager (+https://github.com/jaakkopasanen/AutoEq)"

# "- [Name](./path) by Source[ on Rig]"
_INDEX_LINE_RE = re.compile(
    r"^-\s*\[(?P<name>.+?)\]\(\.?/?(?P<path>[^)]+)\)\s*"
    r"by\s+(?P<source>.+?)(?:\s+on\s+(?P<rig>.+?))?\s*$"
)


@dataclass(frozen=True)
class AutoEqEntry:
    """One AutoEQ result: a device measured by a given source on a given rig."""

    name: str
    source: str
    rig: str
    path: str          # URL-encoded, relative to results/, no leading "./"
    device_type: str   # "Over-ear" | "In-ear" | "Earbud" | "Other"

    @property
    def folder(self) -> str:
        """The (encoded) last path segment — the device folder name."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def location(self) -> str:
        return f"{self.source} · {self.rig}" if self.rig else self.source

    @property
    def display(self) -> str:
        return f"{self.name}  —  {self.location}"

    def parametric_url(self) -> str:
        return f"{RESULTS_BASE}/{self.path}/{self.folder}%20ParametricEQ.txt"

    def graphic_url(self) -> str:
        return f"{RESULTS_BASE}/{self.path}/{self.folder}%20GraphicEQ.txt"

    def fixed_url(self) -> str:
        return f"{RESULTS_BASE}/{self.path}/{self.folder}%20FixedBandEQ.txt"

    def ir_url(self, sample_rate: int = 44100) -> str:
        return f"{RESULTS_BASE}/{self.path}/{self.folder}%20minimum%20phase%20{sample_rate}Hz.wav"

    def csv_url(self) -> str:
        return f"{RESULTS_BASE}/{self.path}/{self.folder}.csv"


def _classify_device_type(path: str) -> str:
    p = path.lower()
    if "over-ear" in p:
        return "Over-ear"
    if "in-ear" in p:
        return "In-ear"
    if "earbud" in p:
        return "Earbud"
    return "Other"


def _http_get(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _http_get_bytes(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_index(text: str) -> list[AutoEqEntry]:
    """Parse INDEX.md markdown into AutoEqEntry objects (malformed lines skipped)."""
    entries: list[AutoEqEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("- ["):
            continue
        m = _INDEX_LINE_RE.match(line)
        if not m:
            continue
        path = m.group("path").strip().strip("/")
        entries.append(AutoEqEntry(
            name=m.group("name").strip(),
            source=(m.group("source") or "").strip(),
            rig=(m.group("rig") or "").strip(),
            path=path,
            device_type=_classify_device_type(path),
        ))
    return entries


def load_index(data_dir: Path, *, force_refresh: bool = False,
               max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> list[AutoEqEntry]:
    """Return the full AutoEQ index, served from a local cache when fresh.

    The raw INDEX.md is cached in ``data_dir`` and reused while younger than
    ``max_age_days``. A forced or expired refresh re-downloads it; if the
    network fails but a cache exists (even a stale one), the cache is used so
    the browser still works offline. Raises on the first run with no network.
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
            text = _http_get(INDEX_URL)
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


def fetch_parametric(entry: AutoEqEntry) -> str:
    """Download a device's ParametricEQ.txt (valid APO config text)."""
    return _http_get(entry.parametric_url())


def fetch_graphic(entry: AutoEqEntry) -> str:
    """Download a device's GraphicEQ.txt.

    The file is a single ``GraphicEQ: <f> <g>; <f> <g>; …`` line on AutoEQ's
    standard 127-point frequency grid — the format RUMBLE merges/averages.
    """
    return _http_get(entry.graphic_url())


def fetch_fixed(entry: AutoEqEntry) -> str:
    """Download a device's FixedBandEQ.txt (valid APO PEQ text, e.g. 10-band)."""
    return _http_get(entry.fixed_url())


def fetch_ir(entry: AutoEqEntry, sample_rate: int = 44100) -> bytes:
    """Download a device's minimum-phase impulse response WAV (44100 or 48000 Hz)."""
    return _http_get_bytes(entry.ir_url(sample_rate))


@dataclass(frozen=True)
class AutoEqCsvData:
    raw: list[Point]
    target: list[Point]


def fetch_csv(entry: AutoEqEntry) -> str:
    """Download a device's <Name>.csv (raw/target/equalization columns, among others)."""
    return _http_get(entry.csv_url())


def parse_csv(text: str) -> AutoEqCsvData:
    """Parse <Name>.csv into (raw, target) point lists — the two columns
    needed for a preference re-target. Rows with a non-positive or
    unparsable frequency are skipped."""
    raw, target = [], []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            f = float(row["frequency"])
        except (KeyError, ValueError, TypeError):
            continue
        if f <= 0:
            continue
        for col, bucket in (("raw", raw), ("target", target)):
            try:
                bucket.append((f, float(row[col])))
            except (KeyError, ValueError, TypeError):
                pass
    raw.sort(key=lambda p: p[0])
    target.sort(key=lambda p: p[0])
    return AutoEqCsvData(raw=raw, target=target)


def search(entries: list[AutoEqEntry], query: str = "", *,
           source: str | None = None, device_type: str | None = None,
           limit: int | None = None) -> list[AutoEqEntry]:
    """Filter entries by free-text query plus optional source/type facets.

    Every whitespace-separated token must appear (case-insensitively) in the
    device name or source. Results are ranked: name-prefix matches first, then
    name-substring, then the rest — alphabetised within each tier.
    """
    tokens = [t for t in query.lower().split() if t]
    q = query.strip().lower()

    def matches(e: AutoEqEntry) -> bool:
        if source and e.source != source:
            return False
        if device_type and e.device_type != device_type:
            return False
        if not tokens:
            return True
        hay = f"{e.name} {e.source}".lower()
        return all(tok in hay for tok in tokens)

    def rank(e: AutoEqEntry) -> tuple:
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


def sources(entries: list[AutoEqEntry]) -> list[str]:
    return sorted({e.source for e in entries if e.source})
