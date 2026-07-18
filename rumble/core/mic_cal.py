"""Microphone calibration import + confidence tiers (RUMBLE Campaign C, S10).

Two independent pieces, per roadmap C1 (docs/superpowers/plans/
2026-07-11-adaptive-frontend-roadmap.md):

1. Parsing/storage of calibration files. The miniDSP/UMIK text format (an
   optional "Sens Factor" header line, comment lines, then freq/dB data
   pairs) is parsed into the same ``(freq, db)`` Point-list shape every
   other curve consumer (:mod:`rumble.core.autoeq_merge`) already uses.
   Saved under ``data_dir/mic_cal/`` as a GraphicEQ-format curve file
   (reusing :func:`autoeq_merge.format_graphiceq`/
   :func:`autoeq_merge.parse_graphiceq` rather than inventing a new curve
   serialization) plus a small JSON sidecar carrying display metadata
   (name/source/band) -- rides into backups for free, same as every other
   data_dir subtree.

2. The two-tier correction-band model (user decision 2026-07-11: standard
   mics are first-class, not an afterthought). A mic with no calibration
   can only be trusted below the Schroeder transition region for typical
   domestic rooms (the standard acoustics result cited in roadmap C1:
   below Schroeder, room modal behavior dominates and its narrow +-10-20 dB
   features are captured correctly by any smooth-response capsule; above
   it, an unknown mic's own coloration is the same order of magnitude as
   real speaker differences, so "correcting" it would bake the mic's own
   errors into the speakers). A calibrated mic (file or transfer cal) is
   trusted across the full audible range. An "estimated" cal (S17,
   Spinorama-referenced mic estimation) sits between the two: its own
   sidecar records the confidence band that estimation produced.

S16 (C4a) adds :func:`derive_transfer_cal` -- deriving a calibration for an
uncalibrated mic by comparing it against ANY calibrated mic borrowed once
(roadmap C4's "transfer calibration"), reusing :func:`subtract_cal` for both
subtraction steps and :func:`eq_target.octave_smooth_adaptive` for the same
smoothing every other auto-derived curve gets, then saving the result via
:func:`save_cal` with ``source="transfer"``.

S17 (C4b) adds :func:`derive_estimated_cal` -- estimating a calibration from
a single speaker the user owns, IF it has a published measurement in the
Spinorama database (roadmap C4's "Spinorama-referenced estimation"): a
quasi-anechoic (time-gated) measurement of that speaker, compared against
its published anechoic on-axis curve, stands in for the transfer-calibration
comparison since the speaker's own published data plays the role a borrowed
reference mic's cal file would. Reuses :func:`subtract_cal` for the
comparison (same "measurement minus ground truth" shape as
:func:`derive_transfer_cal`'s own second step) but smooths much more heavily
(see the function's own docstring), since this estimate also carries the
reference speaker's unit-to-unit variance and the user's own positioning
error on top of the mic's response -- honesty tracked via the confidence
:func:`estimated_band` computes from the measurement's own gate length, and
:data:`ESTIMATED_TIER_MAX_BOOST_DB`, a tightened correction boost cap for
profiles fit into that band.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rumble.core import autoeq_merge, eq_target
from rumble.core.autoeq_merge import Point

CAL_SUBDIR = "mic_cal"

#: Full audible range -- the band a genuine calibration (file or transfer)
#: is trusted across.
FULL_RANGE_HZ: tuple[float, float] = (20.0, 20000.0)

#: Default cap for an uncalibrated ("standard/unknown") mic: the upper end
#: of the Schroeder transition frequency for typical domestic rooms (roadmap
#: C1). User-adjustable within MIN/MAX below.
DEFAULT_UNCALIBRATED_CAP_HZ = 300.0
MIN_UNCALIBRATED_CAP_HZ = 200.0
MAX_UNCALIBRATED_CAP_HZ = 500.0

_VALID_SOURCES = ("file", "transfer", "estimated")

_SENS_FACTOR_RE = re.compile(r"sens\s*factor\s*=\s*([+-]?\d+\.?\d*)", re.IGNORECASE)
# Same "a data line starts with a number, anything else is header/comment"
# rule rew_import.py applies to REW's own export format -- miniDSP/UMIK
# files use the identical convention (comment lines prefixed with `*`, a
# quoted "Sens Factor ..." header line, then bare freq/dB rows).
_NUMERIC_LEAD_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)")


@dataclass
class MicCalParseResult:
    """A parsed miniDSP/UMIK calibration text file."""

    curve: list[Point] = field(default_factory=list)
    sens_factor_db: float | None = None
    warnings: list[str] = field(default_factory=list)


def parse_cal_text(text: str) -> MicCalParseResult:
    """Parse miniDSP/UMIK calibration text into a sorted ``(freq, db)``
    curve. Tolerates comment lines and an optional "Sens Factor" header line
    (captured, not treated as data). Malformed data lines are skipped and
    reported in ``.warnings`` rather than raising, the same honesty
    convention :func:`rumble.core.rew_import.parse_measurement_text` uses --
    a result with an empty ``.curve`` means the file was rejected as
    unusable and the reason is the last warning.
    """
    points: list[Point] = []
    warnings: list[str] = []
    sens_factor_db: float | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        sens_match = _SENS_FACTOR_RE.search(line)
        if sens_match:
            sens_factor_db = float(sens_match.group(1))
            continue
        if not _NUMERIC_LEAD_RE.match(line):
            continue  # comment/header line -- not a data row
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            warnings.append(f"Skipped malformed line: {line!r}")
            continue
        try:
            freq = float(parts[0])
            gain = float(parts[1])
        except ValueError:
            warnings.append(f"Skipped malformed line: {line!r}")
            continue
        if freq <= 0:
            warnings.append(f"Skipped non-positive frequency: {line!r}")
            continue
        points.append((freq, gain))

    points.sort(key=lambda p: p[0])
    if not points:
        warnings.append("No usable calibration points found in file")
    return MicCalParseResult(curve=points, sens_factor_db=sens_factor_db, warnings=warnings)


@dataclass
class MicCalEntry:
    """Metadata for one saved calibration (the JSON sidecar's contents)."""

    id: str
    name: str
    source: str  # "file" | "transfer" | "estimated"
    band: tuple[float, float] | None = None  # only meaningful for "estimated"


def _cal_dir(data_dir: Path) -> Path:
    return Path(data_dir) / CAL_SUBDIR


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "mic_cal"


def save_cal(data_dir: Path, name: str, curve: list[Point], source: str = "file",
             band: tuple[float, float] | None = None) -> str:
    """Persist a calibration curve under ``data_dir/mic_cal/``, returning the
    generated id. The curve itself is stored as a GraphicEQ-format ``.txt``
    file (reusing :func:`autoeq_merge.format_graphiceq` -- no new curve
    serialization); ``name``/``source``/``band`` go in a small JSON sidecar
    of the same stem. *band* is only meaningful for ``source="estimated"``
    (the confidence range that estimation produced); ``None`` for
    ``"file"``/``"transfer"`` (their band is always the full range, derived,
    not stored).
    """
    if source not in _VALID_SOURCES:
        raise ValueError(f"Unknown calibration source: {source!r}")
    root = _cal_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)

    base = _slugify(name)
    candidate = base
    existing = {p.stem for p in root.glob("*.json")}
    n = 2
    while candidate in existing:
        candidate = f"{base}_{n}"
        n += 1
    cal_id = candidate

    (root / f"{cal_id}.txt").write_text(
        autoeq_merge.format_graphiceq(curve), encoding="utf-8")
    sidecar = {
        "name": name,
        "source": source,
        "band": list(band) if band else None,
    }
    (root / f"{cal_id}.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return cal_id


def _entry_from_sidecar(cal_id: str, sidecar: dict) -> MicCalEntry:
    band = sidecar.get("band")
    return MicCalEntry(
        id=cal_id,
        name=sidecar.get("name", cal_id),
        source=sidecar.get("source", "file"),
        band=tuple(band) if band else None,
    )


def list_cals(data_dir: Path) -> list[MicCalEntry]:
    """Every saved calibration's metadata, sorted by id."""
    root = _cal_dir(data_dir)
    if not root.exists():
        return []
    entries: list[MicCalEntry] = []
    for json_path in sorted(root.glob("*.json")):
        try:
            sidecar = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries.append(_entry_from_sidecar(json_path.stem, sidecar))
    return entries


def load_cal(data_dir: Path, cal_id: str) -> tuple[MicCalEntry, list[Point]] | None:
    """Load one saved calibration's metadata + curve, or ``None`` if either
    half of the pair is missing/unreadable."""
    root = _cal_dir(data_dir)
    json_path = root / f"{cal_id}.json"
    txt_path = root / f"{cal_id}.txt"
    if not json_path.exists() or not txt_path.exists():
        return None
    try:
        sidecar = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = _entry_from_sidecar(cal_id, sidecar)
    curve = autoeq_merge.parse_graphiceq(txt_path.read_text(encoding="utf-8"))
    return entry, curve


def subtract_cal(measurement: list[Point], cal: list[Point]) -> list[Point]:
    """Remove a microphone's own response from a raw measurement:
    ``measurement - cal`` at each point of their reconciled common grid, via
    :func:`autoeq_merge.delta_curve` (no new interpolation code -- the same
    log-linear grid reconciliation every other curve subtraction in the
    codebase already uses)."""
    return autoeq_merge.delta_curve(measurement, cal)


def derive_transfer_cal(reference_curve: list[Point], reference_cal: list[Point],
                         unknown_curve: list[Point]) -> list[Point]:
    """Derive an unknown mic's calibration curve from a transfer measurement
    against a known, already-calibrated reference mic (roadmap C4, "transfer
    calibration"): both mics measure the SAME speaker from the SAME fixed
    position, so the speaker+room response cancels out of the comparison,
    leaving only the difference between the two mics' own capsule responses.

    ``reference_curve - reference_cal`` (:func:`subtract_cal`) removes the
    reference mic's own known error, giving ground truth -- the true
    speaker+room response at the measurement position. ``unknown_curve -
    ground_truth`` is then exactly the unknown mic's own response error --
    the same "measurement minus true response" shape :func:`subtract_cal`
    already applies everywhere else, so it's reused for both steps rather
    than duplicating the arithmetic. Smoothed with
    :func:`eq_target.octave_smooth_adaptive` (the same smoothing every other
    auto-derived correction curve in the codebase uses) before being handed
    to :func:`save_cal` with ``source="transfer"``.
    """
    ground_truth = subtract_cal(reference_curve, reference_cal)
    unknown_response = subtract_cal(unknown_curve, ground_truth)
    return eq_target.octave_smooth_adaptive(unknown_response)


#: Upper bound of an "estimated" calibration's confidence band: above here,
#: the reference speaker's own unit-to-unit variance and the user's
#: positioning error become comparable in size to a typical mic's own
#: response error, so the comparison stops being informative about the mic
#: specifically (roadmap C4: "honest to ~+-2-3 dB over ~500 Hz-10 kHz").
#: Adopted directly from the roadmap's own stated ceiling, not derived from
#: a formula the way the lower bound (below) is.
ESTIMATED_BAND_HIGH_HZ = 10000.0

# S17 (roadmap C4): correcting into an "estimated" tier's band with the
# standard 6 dB boost cap (eq_target.make_correction's own default) would
# compound the estimate's own +-2-3 dB uncertainty with a full-strength
# correction on top of it. The project's existing precedent for "how much to
# damp a boost cap under measurement uncertainty" is
# eq_target.directivity_boost_cap()'s own worst-case damping fraction
# (_DIRECTIVITY_MIN_CAP_FRACTION = 0.5, "maximum reduction is half the
# cap") -- reused here verbatim against the standard 6.0 dB cap rather than
# inventing a new fraction, since an estimated mic calibration's uncertainty
# is exactly the kind of case that precedent exists for.
ESTIMATED_TIER_MAX_BOOST_DB = 3.0  # 6.0 * 0.5


def estimated_gate_f_lo_hz(gate_ms: float) -> float:
    """Lowest frequency a time-gated (quasi-anechoic) measurement can
    resolve: ``1 / gate_window_s`` -- the standard windowing-resolution
    limit for any gated impulse-response measurement (roadmap C4: "the gate
    limit is 1/T_window, typically ~300-500 Hz domestically"). A caller
    chooses ``gate_ms`` from the measurement's own first-reflection arrival
    (see :func:`rumble.core.measurement.detect_first_reflection_gate_ms`),
    which for a ~1 m domestic placement naturally lands in the
    few-millisecond range this formula expects.
    """
    gate_s = max(gate_ms, 1e-6) / 1000.0
    return 1.0 / gate_s


def estimated_band(gate_ms: float) -> tuple[float, float]:
    """The ``(f_lo, f_hi)`` confidence band an "estimated" calibration
    derived from a gate of *gate_ms* is honest across (roadmap C4):
    ``f_lo`` from the gate's own resolution limit (:func:`estimated_gate_f_lo_hz`),
    ``f_hi`` fixed at :data:`ESTIMATED_BAND_HIGH_HZ`. Clamped so an
    unusually short gate (an implausibly close first reflection) can never
    invert the band -- both ends collapse to the ceiling rather than
    reporting ``f_lo > f_hi``.
    """
    f_lo = min(estimated_gate_f_lo_hz(gate_ms), ESTIMATED_BAND_HIGH_HZ)
    return (f_lo, ESTIMATED_BAND_HIGH_HZ)


def derive_estimated_cal(gated_measurement: list[Point],
                          anechoic_on_axis: list[Point]) -> list[Point]:
    """Estimate a microphone's own response by comparing a quasi-anechoic
    (time-gated) measurement of a reference speaker the user owns against
    that speaker's published anechoic on-axis curve (roadmap C4,
    "Spinorama-referenced estimation"): gating removes the room above the
    gate's own resolution floor (see :func:`estimated_gate_f_lo_hz`), so
    what remains in ``gated_measurement - anechoic_on_axis`` is the mic's
    own coloration plus the reference speaker's positioning/unit variance
    (both folded into the same "estimated" honesty label, not separated).
    Reuses :func:`subtract_cal` -- the published curve stands in for a
    reference mic's own ground-truth measurement, the same "measurement
    minus ground truth" shape :func:`derive_transfer_cal`'s second step
    already applies.

    Smoothed much more heavily than every other auto-derived correction
    curve: uniformly at :data:`eq_target.ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT`
    (2.0 octaves -- the widest window :func:`eq_target.octave_smooth_adaptive`
    documents anywhere, reused here as a flat window rather than its usual
    narrow-to-wide taper), since this comparison carries meaningfully more
    noise than a controlled transfer-calibration measurement (one extra
    unknown: the reference speaker's own unit-to-unit variance).
    """
    diff = subtract_cal(gated_measurement, anechoic_on_axis)
    return eq_target.octave_smooth_adaptive(
        diff,
        window_oct=eq_target.ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT,
        treble_window_oct=eq_target.ADAPTIVE_SMOOTH_TREBLE_WINDOW_OCT,
    )


def clamp_uncalibrated_cap(cap_hz: float) -> float:
    """Clamp a user-adjustable uncalibrated-mic cap to the documented
    200-500 Hz range."""
    return max(MIN_UNCALIBRATED_CAP_HZ, min(MAX_UNCALIBRATED_CAP_HZ, cap_hz))


def correction_band(cal: MicCalEntry | None, *,
                     uncalibrated_cap_hz: float = DEFAULT_UNCALIBRATED_CAP_HZ,
                     ) -> tuple[float, float]:
    """The ``(f_lo, f_hi)`` band a measurement through *cal* can be trusted
    to auto-correct against (roadmap C1's two-tier model):

    - ``cal is None`` (no calibration -- a standard/unknown mic): capped at
      *uncalibrated_cap_hz* (clamped to the documented 200-500 Hz range),
      default 300 Hz.
    - ``cal.source == "estimated"``: the band recorded in its own sidecar
      at estimation time (S17) -- falls back to the generic uncalibrated
      band if that sidecar somehow has none.
    - ``cal.source`` in ``{"file", "transfer"}``: the full audible range --
      both are genuine measured/derived calibrations (transfer calibration
      cancels speaker+room exactly the same way a purchased cal file does).
    """
    cap = clamp_uncalibrated_cap(uncalibrated_cap_hz)
    if cal is None:
        return (FULL_RANGE_HZ[0], cap)
    if cal.source == "estimated":
        return cal.band if cal.band else (FULL_RANGE_HZ[0], cap)
    return FULL_RANGE_HZ
