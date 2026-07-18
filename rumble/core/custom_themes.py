"""Custom theme storage, import, export and validation."""
from __future__ import annotations

import json
import re
from pathlib import Path

from rumble.ui.theme.tokens import THEMES, FALLBACK_THEME

# A colour token is exactly 6 (#RRGGBB) or 8 (#RRGGBBAA) hex digits — the two
# forms RUMBLE emits. Anything else (3-digit shorthand, named colours, junk) is
# rejected so it can never reach QSS generation or the luminance maths.
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

CUSTOM_THEMES_FILE = "custom_themes.json"

# Tokens every theme must define (name + accent_name are also required)
REQUIRED_TOKENS: list[str] = [
    "bg_deep", "bg_base", "bg_surface", "bg_raised", "bg_sunken",
    "border_subtle", "border_default", "border_strong",
    "accent", "accent_hover", "accent_active", "accent_muted", "accent_glow",
    "text_primary", "text_secondary", "text_muted", "text_on_accent",
    "success", "warning", "error", "info",
    "graph_bg", "graph_grid", "graph_zero", "graph_curve",
    "graph_band", "graph_band_sel", "graph_handle", "graph_text",
    "scrollbar_track", "scrollbar_thumb", "scrollbar_thumb_hover",
    "name", "accent_name",
]


def _custom_themes_path(data_dir: Path) -> Path:
    return data_dir / CUSTOM_THEMES_FILE


def validate_theme(data: dict) -> list[str]:
    """Return a list of missing/invalid token names, or [] if valid."""
    errors = []
    for key in REQUIRED_TOKENS:
        if key not in data:
            errors.append(f"Missing token: '{key}'")
        elif key not in ("name", "accent_name") and not _HEX_RE.match(str(data[key])):
            errors.append(f"Token '{key}' must be a 6- or 8-digit hex colour (e.g. #1A2B3C)")
    if "name" in data and data["name"] in THEMES:
        errors.append(f"A built-in theme named '{data['name']}' already exists")
    return errors


def load_custom_themes(data_dir: Path) -> dict[str, dict]:
    """Load custom themes from data_dir and merge them into the runtime THEMES dict.

    Returns the dict of just the custom themes (name → tokens).
    """
    path = _custom_themes_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    loaded: dict[str, dict] = {}
    for t in raw:
        name = t.get("name", "").strip()
        if name and name not in THEMES:
            THEMES[name] = t
            loaded[name] = t
    return loaded


def save_custom_theme(data_dir: Path, theme: dict) -> None:
    """Persist a new or updated custom theme to data_dir/custom_themes.json."""
    path = _custom_themes_path(data_dir)
    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = []
    # Replace if same name exists, otherwise append
    names = [t.get("name") for t in existing]
    if theme["name"] in names:
        idx = names.index(theme["name"])
        existing[idx] = theme
    else:
        existing.append(theme)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    # Also update the live THEMES dict
    THEMES[theme["name"]] = theme


def export_theme(theme: dict, path: Path) -> None:
    """Write a theme dict to a .rumble-theme JSON file."""
    path.write_text(json.dumps(theme, indent=2), encoding="utf-8")


def import_theme_file(path: Path) -> tuple[dict, list[str]]:
    """Load and validate a .rumble-theme JSON file.

    Returns (theme_dict, errors). If errors is empty the theme is valid.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"Could not read file: {exc}"]
    if not isinstance(data, dict):
        return {}, ["File does not contain a theme object"]
    # Backfill ONLY missing REQUIRED colour tokens from the fallback. The
    # fallback (RUMBLE) also carries structural levers — radius_scale,
    # sheen, bevel, glow, glass_alpha — which are NOT required tokens; copying
    # the whole fallback would silently stamp RUMBLE's shape/gloss onto an
    # imported colour-only theme. Filling per-key leaves the levers absent so
    # they default to the engine's neutral 1.0 baseline.
    filled = dict(data)
    for key in REQUIRED_TOKENS:
        if key in ("name", "accent_name"):
            # Identity, not a colour: never borrow the fallback's name, or a
            # nameless import would inherit "RUMBLE" and fail validation with
            # a misleading "built-in already exists" instead of "missing name".
            continue
        filled.setdefault(key, FALLBACK_THEME[key])
    errors = validate_theme(filled)
    return filled, errors
