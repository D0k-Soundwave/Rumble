"""The canonical RUMBLE profile file format.

A RUMBLE profile is a single, portable, self-contained UTF-8 text file.
All profile metadata (name, description, tags, timestamps, readonly flag) is
embedded as `#` comment lines at the top of the file, followed by the
Equalizer APO command body. Because APO ignores comment lines, the canonical
file is *also* a valid APO config file — the very same file is stored in
`<apo_config_dir>/rumble/<id>.txt`, referenced by the activation Include
pointer, exported, imported, and round-trip edited. No translation step is
required between "RUMBLE profile" and "APO config"; they are one artifact.

Canonical header:

    # RUMBLE Profile: <Name>
    # Description: <single line, may be empty>
    # Tags: <comma-separated, may be empty>
    # Created: YYYY-MM-DDTHH:MM:SS
    # Modified: YYYY-MM-DDTHH:MM:SS
    # Readonly: true                (only present when user-locked)
    # Builtin: true                 (only present on system-protected built-ins)
    # Fit Sample Rate: 48000        (only on curve-fitted profiles — see below)
    # Created by RUMBLE — EQ Profile Manager for Equalizer APO

Files with partial headers and foreign APO config files (no header at all)
parse fine: absent fields are reported as None so the caller can fall back
to other sources and migrate the file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rumble.core import apo_config
from rumble.core.apo_config import ProfileConfig

ATTRIBUTION_LINE = "# Created by RUMBLE — EQ Profile Manager for Equalizer APO"

# Keys that a fully canonical header must carry (Readonly and Builtin are
# optional — they are only written when true).
REQUIRED_HEADER_KEYS = ("name", "description", "tags", "created", "modified")

_HEADER_KEY_RE = re.compile(
    r"^#\s*(RUMBLE\s+Profile|Description|Tags|Created|Modified|Readonly|Builtin"
    r"|APO\s+Source\s+Hash|APO\s+Source|Import\s+Signature|Fit\s+Sample\s+Rate)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_ATTRIBUTION_RE = re.compile(r"^#\s*Created by RUMBLE\b", re.IGNORECASE)

_KEY_TO_FIELD = {
    "rumble profile": "name",
    "description": "description",
    "tags": "tags",
    "created": "created",
    "modified": "modified",
    "readonly": "readonly",
    "builtin": "builtin",
    # Provenance for APO-config imports (see profile_manager.sync_apo_imports):
    "apo source": "apo_source",
    "apo source hash": "apo_source_hash",
    "import signature": "import_signature",
    # Sample rate a curve-fitted profile's bands/residual were solved at:
    "fit sample rate": "fit_sample_rate",
}

_LEGACY_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$")


@dataclass
class ProfileDocument:
    """A parsed canonical profile file: metadata header + APO config body.

    Metadata fields are None when the corresponding header line was absent,
    letting callers distinguish "not stated" from "stated empty".
    """

    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    created: str | None = None
    modified: str | None = None
    readonly: bool | None = None
    builtin: bool | None = None
    # Provenance for profiles imported from an existing Equalizer APO config.
    # None on hand-made/built-in profiles; set only on APO imports so the boot
    # reconcile can keep them in sync without duplicating (see profile_manager).
    apo_source: str | None = None        # source path, relative to the config dir
    apo_source_hash: str | None = None   # hash of the source's effective content
    import_signature: str | None = None  # hash of the body we generated at import
    # Set ONLY on profiles whose bands were fitted to a measured curve
    # (autoeq_merge.build_curve_config): the sample rate those bands — and the
    # GraphicEQ residual that completes them — were solved at. Biquads warp with
    # sample rate while a GraphicEQ line does not, so the residual only cancels
    # exactly at this rate; its presence is what lets the manager re-solve the
    # residual when the device rate changes. Absent on hand-authored profiles,
    # which is what keeps their GraphicEQ layer (a deliberate part of the tuning,
    # not a residual) from ever being rewritten.
    fit_sample_rate: int | None = None
    config: ProfileConfig = field(default_factory=ProfileConfig)

    # Header keys actually found while parsing (field names).
    found_keys: set = field(default_factory=set)

    def has_full_header(self) -> bool:
        return all(key in self.found_keys for key in REQUIRED_HEADER_KEYS)


def _normalise_datetime(value: str) -> str:
    """Accept both ISO `T` and legacy space-separated datetimes."""
    value = value.strip()
    m = _LEGACY_DATETIME_RE.match(value)
    return f"{m.group(1)}T{m.group(2)}" if m else value


def parse_profile_text(text: str) -> ProfileDocument:
    """Parse canonical profile text (or any APO config text) into a document.

    Only the leading comment block is scanned for metadata; the APO command
    body is parsed with apo_config.parse_config (which itself skips comments,
    so the header never leaks into the config).
    """
    doc = ProfileDocument()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            break  # end of the leading comment block
        m = _HEADER_KEY_RE.match(line)
        if not m:
            continue
        key = re.sub(r"\s+", " ", m.group(1).lower().replace(".", ""))
        fieldname = _KEY_TO_FIELD[key]
        value = m.group(2).strip()
        if fieldname == "name":
            if value:
                doc.name = value
        elif fieldname == "description":
            doc.description = value
        elif fieldname == "tags":
            doc.tags = [t.strip() for t in value.split(",") if t.strip()]
        elif fieldname in ("created", "modified"):
            if value:
                setattr(doc, fieldname, _normalise_datetime(value))
        elif fieldname in ("readonly", "builtin"):
            setattr(doc, fieldname, value.lower() in ("true", "yes", "1"))
        elif fieldname in ("apo_source", "apo_source_hash", "import_signature"):
            if value:
                setattr(doc, fieldname, value)
        elif fieldname == "fit_sample_rate":
            try:
                doc.fit_sample_rate = int(value)
            except ValueError:
                doc.fit_sample_rate = None
        doc.found_keys.add(fieldname)

    doc.config = apo_config.parse_config(text)
    return doc


def _header_lines(doc: ProfileDocument) -> list[str]:
    lines = [
        f"# RUMBLE Profile: {doc.name or ''}".rstrip(),
        f"# Description: {doc.description or ''}".rstrip(),
        f"# Tags: {', '.join(doc.tags or [])}".rstrip(),
        f"# Created: {doc.created or ''}".rstrip(),
        f"# Modified: {doc.modified or ''}".rstrip(),
    ]
    if doc.readonly:
        lines.append("# Readonly: true")
    if doc.builtin:
        lines.append("# Builtin: true")
    if doc.apo_source:
        lines.append(f"# APO Source: {doc.apo_source}")
    if doc.apo_source_hash:
        lines.append(f"# APO Source Hash: {doc.apo_source_hash}")
    if doc.import_signature:
        lines.append(f"# Import Signature: {doc.import_signature}")
    if doc.fit_sample_rate:
        lines.append(f"# Fit Sample Rate: {doc.fit_sample_rate}")
    lines.append(ATTRIBUTION_LINE)
    return lines


def render_profile_text(doc: ProfileDocument) -> str:
    """Render a full canonical profile file: metadata header + APO body."""
    text = "\n".join(_header_lines(doc)) + "\n\n" + apo_config.render_config_body(doc.config)
    return re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"


def replace_header(text: str, doc: ProfileDocument) -> str:
    """Rewrite only the metadata header of existing profile text.

    The APO command body is preserved byte-for-byte (no reparse/re-render),
    so hand-edited or foreign-tool formatting survives metadata updates.
    Leading comment lines that are not RUMBLE metadata (e.g. REW headers)
    are kept below the new header.
    """
    lines = text.splitlines()
    body_start = len(lines)
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            body_start = i
            break

    preserved = [
        raw for raw in lines[:body_start]
        if raw.strip().startswith("#")
        and not _HEADER_KEY_RE.match(raw.strip())
        and not _ATTRIBUTION_RE.match(raw.strip())
    ]
    body = "\n".join(lines[body_start:]).strip("\n")

    out_lines = _header_lines(doc) + preserved + [""]
    out = "\n".join(out_lines) + "\n" + (body + "\n" if body else "")
    return re.sub(r"\n{3,}", "\n\n", out).rstrip() + "\n"
