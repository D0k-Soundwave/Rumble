"""Theme token dictionaries for RUMBLE's built-in themes.

Every theme is a flat dict of named colour tokens (plus the optional structural
levers) that ThemeEngine expands into a full QSS stylesheet. Each one is built
from a single Transformers character's palette, mapped through one consistent
rule so no theme is an arbitrary recolour of another:

    body colour   → the surface ladder (bg_deep … bg_raised)
    highlight     → the interactive accent (buttons, EQ curve, focus, selection)
    supporting    → the minor pops: EQ node dots + warning, and success +
                    selected band + GraphicEQ
    font colour   → the text ramp

  • Soundwave — the default: dark navy surfaces under a Decepticon-purple
    accent, glossy and lightly squared.
  • Panther (Ravage) — near-black velvet under a shadow-purple accent; the
    matte, sharpest, lowest-glow theme of the set.
  • Screamer (Starscream) — the lightest theme: a medium grey lifted to the
    limit of white-text legibility, under a fully saturated signal red.
  • Malice (Scorponok) — violet surfaces cut by a teal accent; rounded and glowy.
  • Bludgeon — near-black green under an amber accent; a tactical/night-ops look.
  • Prime (Optimus) — deep commander-red surfaces under a chrome-silver accent,
    with Optimus blue for selection and gold for the EQ nodes.
  • Sage (Kup) — dark teal surfaces under a burnt-orange rust accent.
  • System — generated live from the Windows DWM accent + light/dark setting;
    structurally neutral so it always mirrors Windows.

The set carries no light theme: every built-in is dark or mid-tone and takes
white `text_primary`. (System is the exception — it follows Windows.) Contrast
is derived, not guessed: each theme's text colour and any lifted accent were
chosen against the surface's luminance, so nothing ships unreadable.

Structural levers (`radius_scale`, `sheen`, `bevel`, `glow`, `glass_alpha`) give
each theme its own shape and material on top of colour — corner roundness,
gradient gloss, raised-edge strength, and glow/elevation intensity. They are
optional and default to 1.0 (0.90 for glass_alpha), so System, imported and
custom themes that omit them are unaffected.
"""
from __future__ import annotations

import colorsys


# ── Colour maths ──────────────────────────────────────────────────────────

def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _to_rgb(c: str) -> tuple[int, int, int]:
    s = c.strip().lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _to_hex(r: float, g: float, b: float) -> str:
    return f"#{_clamp(r):02X}{_clamp(g):02X}{_clamp(b):02X}"


def _mix(c1: str, c2: str, t: float) -> str:
    """Linear blend from c1 toward c2 by fraction t (0..1)."""
    r1, g1, b1 = _to_rgb(c1)
    r2, g2, b2 = _to_rgb(c2)
    return _to_hex(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t)


def _lum(c: str) -> float:
    """Perceived luminance (0..1) of a colour token.

    The single guarded luminance helper for the whole theme layer — both
    ``theme_engine`` and ``effects`` delegate here. Malformed or short input
    (fewer than 6 hex digits) returns 0.0 rather than raising, so a custom or
    imported theme with a bad colour can never crash QSS generation.
    """
    s = str(c).strip().lstrip("#")
    if len(s) < 6:
        return 0.0
    try:
        r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return 0.0
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def theme_float(tokens: dict, key: str, default: float = 1.0) -> float:
    """Read a numeric structural lever (glow/sheen/bevel/radius_scale/…) from a
    theme dict, falling back to ``default`` on a missing or non-numeric value.

    The single shared reader for the theme layer — ``theme_engine``,
    ``effects`` and ``HudFrame`` all delegate here so the parsing rule lives in
    exactly one place.
    """
    try:
        return float(tokens.get(key, default))
    except (TypeError, ValueError):
        return default


# ── Dark theme generator ──────────────────────────────────────────────────

# A deliberately near-black, faintly cool charcoal ladder. Each step is spaced
# so adjacent surfaces are distinguishable without the UI ever feeling grey.
_NEUTRAL: dict[str, str] = {
    "bg_deep":        "#050506",
    "bg_sunken":      "#08080B",
    "bg_base":        "#0C0C10",
    "bg_surface":     "#15151B",
    "bg_raised":      "#1F1F28",
    "border_subtle":  "#1C1C24",
    "border_default": "#2D2D38",
}

# How strongly each layer is pulled toward the theme's ambient hue. Lighter,
# nearer surfaces carry more tint so the colour reads where the eye lands while
# the deepest backgrounds stay true black.
_TINT_W: dict[str, float] = {
    "bg_deep":        0.05,
    "bg_sunken":      0.05,
    "bg_base":        0.08,
    "bg_surface":     0.14,
    "bg_raised":      0.18,
    "border_subtle":  0.12,
    "border_default": 0.16,
}

# Shared semantic palette — kept constant across the dark family for muscle
# memory (success always reads the same green, etc.).
_SUCCESS = "#2BD4A0"
_WARNING = "#FFB13C"
_ERROR   = "#FF5C66"


def _dark_theme(
    name: str,
    accent_name: str,
    accent: str,
    accent_hover: str,
    accent_active: str,
    *,
    tint: str | None = None,
    tint_scale: float = 1.0,
    contrast: str = _WARNING,
    points: str = _SUCCESS,
    on_accent: str | None = None,
    base_override: dict[str, str] | None = None,
    radius_scale: float = 1.0,
    sheen: float = 1.0,
    bevel: float = 1.0,
    glow: float = 1.0,
    glass_alpha: float = 0.90,
) -> dict[str, str]:
    """Build a complete dark theme dict from an accent family + ambient hue.

    `tint` is the colour the near-black surfaces lean toward (defaults to the
    accent); `tint_scale` dials that lean up or down. `contrast`/`points` are
    the complementary hues used for the selected EQ band and node markers.

    `radius_scale`/`sheen`/`bevel`/`glow` are the structural levers that give each
    theme its own *shape and material* on top of its colours — corner roundness,
    gradient gloss, raised-edge strength, and the accent-glow/elevation intensity
    (read by rumble.ui.theme.effects for the soft glass drop-shadows). All default
    to 1.0, which reproduces the engine's baseline look.
    """
    hue = tint or accent
    th: dict[str, str] = {}

    for key, base in _NEUTRAL.items():
        th[key] = _mix(base, hue, _TINT_W[key] * tint_scale)
    if base_override:
        th.update(base_override)

    th["border_strong"] = accent

    th["accent"]        = accent
    th["accent_hover"]  = accent_hover
    th["accent_active"] = accent_active
    th["accent_muted"]  = accent + "26"   # ~15% — selection fills, chip bg
    th["accent_glow"]   = accent + "55"   # ~33% — focus halos

    th["text_primary"]   = "#ECECF3"
    th["text_secondary"] = _mix("#8C8C9E", hue, 0.10 * tint_scale)
    th["text_muted"]     = _mix("#4C4C58", hue, 0.10 * tint_scale)
    th["text_on_accent"] = on_accent or ("#0B0B0E" if _lum(accent) > 0.60 else "#FFFFFF")

    th["success"] = _SUCCESS
    th["warning"] = _WARNING
    th["error"]   = _ERROR
    th["info"]    = accent

    th["graph_bg"]       = _mix("#040405", hue, 0.04 * tint_scale)
    th["graph_grid"]     = _mix("#101017", hue, 0.10 * tint_scale)
    th["graph_zero"]     = _mix("#242432", hue, 0.10 * tint_scale)
    th["graph_curve"]    = accent
    th["graph_band"]     = accent + "55"
    th["graph_band_sel"] = contrast
    th["graph_points"]   = points
    th["graph_handle"]   = accent
    th["graph_text"]     = _mix("#34343F", hue, 0.10 * tint_scale)

    th["scrollbar_track"]       = th["bg_base"]
    th["scrollbar_thumb"]       = th["border_default"]
    th["scrollbar_thumb_hover"] = _mix(th["border_default"], accent, 0.40)

    th["radius_scale"] = radius_scale
    th["sheen"] = sheen
    th["bevel"] = bevel
    th["glow"] = glow
    th["glass_alpha"] = glass_alpha

    th["name"] = name
    th["accent_name"] = accent_name
    return th


# ── The RUMBLE theme collection ──────────────────────────────────────────────
# Seven hand-authored themes, each a fully-specified flat token dict (colours +
# the five structural levers), each sourced from one Transformers character's
# palette. They diverge on base material, temperature, saturation, accent hue
# AND shape/gloss at once, so no two read alike:
#
#   • Soundwave — the signature default: dark navy surfaces under a
#     Decepticon-purple accent, glossy and lightly squared.
#   • Panther   — Ravage: near-black velvet with a shadow-purple accent; the
#     matte, sharpest, lowest-glow theme of the set.
#   • Screamer  — Starscream: the lightest theme, a medium grey pushed to the
#     limit of white-text legibility under a fully saturated signal red.
#   • Malice    — Scorponok: violet surfaces cut by a teal accent; rounded, glowy.
#   • Bludgeon  — near-black green under an amber accent; tactical/night-ops.
#   • Prime     — Optimus: deep commander-red under a chrome-silver accent,
#     Optimus blue for selection, gold for the EQ nodes.
#   • Sage      — Kup: dark teal under a burnt-orange rust accent.
#
# Every theme is a complete dict — nothing is generated — so what you read here
# is exactly what ships. (The generator helpers above are retained only for the
# live Windows "System" theme further down.)

SOUNDWAVE: dict[str, str] = {
    "name": "Soundwave", "accent_name": "Decepticon Purple",

    "bg_deep":        "#00091A",
    "bg_sunken":      "#011026",
    "bg_base":        "#011833",
    "bg_surface":     "#052548",
    "bg_raised":      "#0A335E",

    "border_subtle":  "#123A5E",
    "border_default": "#1E4E7A",
    "border_strong":  "#A64DBF",

    "accent":         "#A64DBF",
    "accent_hover":   "#BE6FD1",
    "accent_active":  "#7A2E9E",
    "accent_muted":   "#A64DBF26",
    "accent_glow":    "#A64DBF55",

    "text_primary":   "#FFFFFF",
    "text_secondary": "#CCD3E0",
    "text_muted":     "#8A97AB",
    "text_on_accent": "#FFFFFF",

    "success":        "#009933",
    "warning":        "#E6A020",
    "error":          "#FF3333",
    "info":           "#3A6EA5",

    "graph_bg":       "#010F26",
    "graph_grid":     "#052548",
    "graph_zero":     "#1E4E7A",
    "graph_curve":    "#A64DBF",
    "graph_band":     "#A64DBF26",
    "graph_band_sel": "#009933",
    "graph_handle":   "#A64DBF",
    "graph_text":     "#CCD3E0",
    "graph_points":   "#3A6EA5",
    "graph_geq":      "#009933",

    "scrollbar_track": "#011026",
    "scrollbar_thumb": "#1E4E7A",
    "scrollbar_thumb_hover": "#7A3FA0",

    "radius_scale": 0.9, "sheen": 1.2, "bevel": 1.1, "glow": 0.8, "glass_alpha": 0.87,
}

PANTHER: dict[str, str] = {
    "name": "Panther", "accent_name": "Shadow Purple",

    "bg_deep":        "#050308",
    "bg_sunken":      "#0A0710",
    "bg_base":        "#100A18",
    "bg_surface":     "#191125",
    "bg_raised":      "#241933",

    "border_subtle":  "#1E1430",
    "border_default": "#2E2047",
    "border_strong":  "#8A46B8",

    "accent":         "#8A46B8",
    "accent_hover":   "#A366D1",
    "accent_active":  "#6B3494",
    "accent_muted":   "#8A46B826",
    "accent_glow":    "#8A46B855",

    "text_primary":   "#FFFFFF",
    "text_secondary": "#CFC6D9",
    "text_muted":     "#8E85A0",
    "text_on_accent": "#FFFFFF",

    "success":        "#8FA0C4",
    "warning":        "#E0A82E",
    "error":          "#E23B3B",
    "info":           "#8A46B8",

    "graph_bg":       "#0A0710",
    "graph_grid":     "#191125",
    "graph_zero":     "#2E2047",
    "graph_curve":    "#8A46B8",
    "graph_band":     "#8A46B826",
    "graph_band_sel": "#8FA0C4",
    "graph_handle":   "#8A46B8",
    "graph_text":     "#CFC6D9",
    "graph_points":   "#E0A82E",
    "graph_geq":      "#8FA0C4",

    "scrollbar_track": "#0A0710",
    "scrollbar_thumb": "#2E2047",
    "scrollbar_thumb_hover": "#6B3494",

    "radius_scale": 0.6, "sheen": 0.7, "bevel": 0.8, "glow": 0.2, "glass_alpha": 0.95,
}

MALICE: dict[str, str] = {
    "name": "Malice", "accent_name": "Scorponok Teal",

    "bg_deep":        "#33194F",
    "bg_sunken":      "#472371",
    "bg_base":        "#5C2D91",
    "bg_surface":     "#6C429C",
    "bg_raised":      "#7C57A7",

    "border_subtle":  "#6B44A0",
    "border_default": "#8259B8",
    "border_strong":  "#00828A",

    "accent":         "#00828A",
    "accent_hover":   "#14A5AE",
    "accent_active":  "#005F66",
    "accent_muted":   "#00828A26",
    "accent_glow":    "#00828A55",

    "text_primary":   "#FFFFFF",
    "text_secondary": "#DAD3E8",
    "text_muted":     "#A79EC2",
    "text_on_accent": "#FFFFFF",

    "success":        "#7A7B7D",
    "warning":        "#FF6600",
    "error":          "#FF3333",
    "info":           "#00828A",

    "graph_bg":       "#3A1F5C",
    "graph_grid":     "#5A3785",
    "graph_zero":     "#8259B8",
    "graph_curve":    "#00828A",
    "graph_band":     "#00828A26",
    "graph_band_sel": "#7A7B7D",
    "graph_handle":   "#00828A",
    "graph_text":     "#DAD3E8",
    "graph_points":   "#FF6600",
    "graph_geq":      "#7A7B7D",

    "scrollbar_track": "#3A1F5C",
    "scrollbar_thumb": "#6B44A0",
    "scrollbar_thumb_hover": "#147A85",

    "radius_scale": 1.2, "sheen": 1.1, "bevel": 0.9, "glow": 1.2, "glass_alpha": 0.89,
}

PRIME: dict[str, str] = {
    "name": "Prime", "accent_name": "Commander Chrome",

    "bg_deep":        "#3D0000",
    "bg_sunken":      "#520000",
    "bg_base":        "#660000",
    "bg_surface":     "#7A1010",
    "bg_raised":      "#941F1F",

    "border_subtle":  "#6E1515",
    "border_default": "#8A2323",
    "border_strong":  "#C6CBD4",

    "accent":         "#C6CBD4",
    "accent_hover":   "#DCE0E8",
    "accent_active":  "#A3A9B5",
    "accent_muted":   "#C6CBD426",
    "accent_glow":    "#C6CBD455",

    "text_primary":   "#FFFFFF",
    "text_secondary": "#F0D6D6",
    "text_muted":     "#C99A9A",
    "text_on_accent": "#000000",

    "success":        "#2A72D6",
    "warning":        "#E6C21E",
    "error":          "#FF6B6B",
    "info":           "#2A72D6",

    "graph_bg":       "#4F0000",
    "graph_grid":     "#6E1515",
    "graph_zero":     "#8A2323",
    "graph_curve":    "#C6CBD4",
    "graph_band":     "#C6CBD426",
    "graph_band_sel": "#2A72D6",
    "graph_handle":   "#C6CBD4",
    "graph_text":     "#F0D6D6",
    "graph_points":   "#E6C21E",
    "graph_geq":      "#2A72D6",

    "scrollbar_track": "#4F0000",
    "scrollbar_thumb": "#8A2323",
    "scrollbar_thumb_hover": "#C6CBD4",

    "radius_scale": 1.1, "sheen": 1.2, "bevel": 1.0, "glow": 0.6, "glass_alpha": 0.90,
}

SCREAMER: dict[str, str] = {
    "name": "Screamer", "accent_name": "Signal Red",

    "bg_deep":        "#454545",
    "bg_sunken":      "#525252",
    "bg_base":        "#5F5F5F",
    "bg_surface":     "#6B6B6B",
    "bg_raised":      "#767676",

    "border_subtle":  "#7D7D7D",
    "border_default": "#C40000",
    "border_strong":  "#E00000",

    "accent":         "#E00000",
    "accent_hover":   "#FF1A1A",
    "accent_active":  "#B00000",
    "accent_muted":   "#E0000026",
    "accent_glow":    "#E0000055",

    "text_primary":   "#FFFFFF",
    "text_secondary": "#E6E6E6",
    "text_muted":     "#CCCCCC",
    "text_on_accent": "#FFFFFF",

    "success":        "#66CC00",
    "warning":        "#FF9F1A",
    "error":          "#FF3333",
    "info":           "#4DA6FF",

    "graph_bg":       "#333333",
    "graph_grid":     "#474747",
    "graph_zero":     "#4DA6FF",
    "graph_curve":    "#E00000",
    "graph_band":     "#E0000026",
    "graph_band_sel": "#4DA6FF",
    "graph_handle":   "#E00000",
    "graph_text":     "#CCCCCC",
    "graph_points":   "#FFA000",
    "graph_geq":      "#4DA6FF",

    "scrollbar_track": "#4F4F4F",
    "scrollbar_thumb": "#C40000",
    "scrollbar_thumb_hover": "#E00000",

    "radius_scale": 1.0, "sheen": 0.8, "bevel": 0.9, "glow": 0.4, "glass_alpha": 0.92,
}

BLUDGEON: dict[str, str] = {
    "name": "Bludgeon", "accent_name": "Assault Amber",

    "bg_deep":        "#041804",
    "bg_sunken":      "#062406",
    "bg_base":        "#082E08",
    "bg_surface":     "#0E3D0E",
    "bg_raised":      "#164E16",

    "border_subtle":  "#123F12",
    "border_default": "#1E5C1E",
    "border_strong":  "#9E5508",

    "accent":         "#9E5508",
    "accent_hover":   "#C46E0A",
    "accent_active":  "#7A4106",
    "accent_muted":   "#9E550826",
    "accent_glow":    "#9E550855",

    "text_primary":   "#FFFFFF",
    "text_secondary": "#D2DED2",
    "text_muted":     "#8FA88F",
    "text_on_accent": "#FFFFFF",

    "success":        "#2FBAC7",
    "warning":        "#EAC200",
    "error":          "#F0463C",
    "info":           "#2FBAC7",

    "graph_bg":       "#062105",
    "graph_grid":     "#123F12",
    "graph_zero":     "#1E5C1E",
    "graph_curve":    "#9E5508",
    "graph_band":     "#9E550826",
    "graph_band_sel": "#2FBAC7",
    "graph_handle":   "#9E5508",
    "graph_text":     "#D2DED2",
    "graph_points":   "#EAC200",
    "graph_geq":      "#2FBAC7",

    "scrollbar_track": "#062406",
    "scrollbar_thumb": "#1E5C1E",
    "scrollbar_thumb_hover": "#7A4106",

    "radius_scale": 0.8, "sheen": 1.3, "bevel": 1.2, "glow": 0.7, "glass_alpha": 0.91,
}

SAGE: dict[str, str] = {
    "name": "Sage", "accent_name": "Veteran Rust",

    "bg_deep":        "#0D3538",
    "bg_sunken":      "#124B4F",
    "bg_base":        "#176065",
    "bg_surface":     "#2E7074",
    "bg_raised":      "#458084",

    "border_subtle":  "#22656A",
    "border_default": "#368085",
    "border_strong":  "#BC5E15",

    "accent":         "#BC5E15",
    "accent_hover":   "#DB7A28",
    "accent_active":  "#914510",
    "accent_muted":   "#BC5E1526",
    "accent_glow":    "#BC5E1555",

    "text_primary":   "#FFFFFF",
    "text_secondary": "#C9DEDB",
    "text_muted":     "#7FA3A4",
    "text_on_accent": "#000000",

    "success":        "#92C8AF",
    "warning":        "#E07A1E",
    "error":          "#E24A3A",
    "info":           "#A8DAC9",

    "graph_bg":       "#124B4F",
    "graph_grid":     "#22656A",
    "graph_zero":     "#368085",
    "graph_curve":    "#BC5E15",
    "graph_band":     "#BC5E1526",
    "graph_band_sel": "#92C8AF",
    "graph_handle":   "#BC5E15",
    "graph_text":     "#C9DEDB",
    "graph_points":   "#A8DAC9",
    "graph_geq":      "#92C8AF",

    "scrollbar_track": "#124B4F",
    "scrollbar_thumb": "#368085",
    "scrollbar_thumb_hover": "#BC5E15",

    "radius_scale": 1.1, "sheen": 1.0, "bevel": 0.9, "glow": 0.6, "glass_alpha": 0.88,
}


# ── Registry ────────────────────────────────────────────────────────────────
# The registry order is incidental — the settings swatch grid and tray menu
# re-sort everything by colour family then brightness (see ordered_theme_names),
# so themes always lay out as a coherent dark→light ramp with System last.

THEMES: dict[str, dict[str, str]] = {
    "Soundwave": SOUNDWAVE,
    "Panther": PANTHER,
    "Malice": MALICE,
    "Prime": PRIME,
    "Screamer": SCREAMER,
    "Bludgeon": BLUDGEON,
    "Sage": SAGE,
}

DEFAULT_THEME_NAME = "Soundwave"

# Complete token dict used to fill gaps in custom/imported themes and as the
# fallback when an unknown theme name is requested.
FALLBACK_THEME = SOUNDWAVE


def get_theme(name: str) -> dict[str, str]:
    if name == "System":
        return build_system_theme()
    return THEMES.get(name, FALLBACK_THEME)


# ── Theme ordering ──────────────────────────────────────────────────────────
# Themes (built-in, custom and imported alike) auto-arrange by COLOUR FAMILY:
# all the reds together, all the blues together, etc. The families themselves
# run dark → light ("black, blue, … white") by their intrinsic brightness, and
# within each family the themes sort darkest → brightest. System always lands at
# the very bottom because it mirrors Windows rather than committing to a hue.

def _theme_brightness(tokens: dict[str, str]) -> float:
    """Perceived brightness of a theme: half its surface, half its accent.

    The background carries the large area, but the accent is what gives each
    dark theme its identity — blending both produces a smooth visible ramp
    within a family instead of every near-black theme tying.
    """
    return 0.5 * _lum(tokens.get("bg_base", "#000000")) + \
        0.5 * _lum(tokens.get("accent", "#000000"))


# Colour families in ascending brightness — black is the darkest, white the
# brightest, the hues in between ordered by their natural luminance (blue is the
# darkest pure hue, yellow the brightest).
_FAMILY_ORDER: dict[str, int] = {
    "black":  0,
    "blue":   1,
    "indigo": 2,
    "violet": 3,
    "red":    4,
    "pink":   5,
    "orange": 6,
    "green":  7,
    "teal":   8,
    "yellow": 9,
    "white":  10,
}


def _theme_family(tokens: dict[str, str]) -> str:
    """Classify a theme into a colour family from its accent hue.

    Light themes count as 'white' whatever their accent; desaturated (metal /
    grey) accents are 'black' when the theme is very dark, otherwise 'white'.
    Everything else is bucketed by accent hue angle.
    """
    if _lum(tokens.get("bg_base", "#000000")) > 0.5:
        return "white"  # a light theme reads as white regardless of accent

    r, g, b = _to_rgb(tokens.get("accent", "#000000"))
    h, _l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    if s < 0.42:  # neutral metal / greyscale — no real hue (chromatics are >0.6)
        return "black" if _theme_brightness(tokens) < 0.33 else "white"

    hue = h * 360
    if hue >= 345 or hue < 15:
        return "red"
    if hue < 45:
        return "orange"
    if hue < 90:
        return "yellow"   # includes lime
    if hue < 165:
        return "green"
    if hue < 200:
        return "teal"     # includes cyan
    if hue < 238:
        return "blue"
    if hue < 262:
        return "indigo"
    if hue < 300:
        return "violet"
    return "pink"          # magenta / pink


def theme_sort_key(name: str, tokens: dict[str, str]):
    """Sort key: colour family (dark→light), then brightness within the family.
    System sorts last."""
    if name == "System":
        return (99, 0.0)
    family = _theme_family(tokens)
    return (_FAMILY_ORDER.get(family, 50), round(_theme_brightness(tokens), 4))


def ordered_theme_names(include_system: bool = True) -> list[str]:
    """All registered theme names (incl. custom/imported) in display order."""
    names = sorted(THEMES.keys(), key=lambda n: theme_sort_key(n, THEMES[n]))
    if include_system:
        names.append("System")
    return names


# ── Windows system theme ──────────────────────────────────────────────────

def _h(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _parse(c: str) -> tuple[int, int, int]:
    s = c.strip().lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _tl(c: str, n: int) -> str:
    r, g, b = _parse(c)
    return _h(min(255, r + n), min(255, g + n), min(255, b + n))


def _td(c: str, n: int) -> str:
    r, g, b = _parse(c)
    return _h(max(0, r - n), max(0, g - n), max(0, b - n))


def _luminance(c: str) -> float:
    r, g, b = _parse(c)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _windows_accent() -> str:
    """Read the Windows DWM accent colour from the registry."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"SOFTWARE\Microsoft\Windows\DWM") as k:
            val = winreg.QueryValueEx(k, "ColorizationColor")[0]
            r = (val >> 16) & 0xFF
            g = (val >> 8) & 0xFF
            b = val & 0xFF
            return _h(r, g, b)
    except Exception:
        pass
    # Fallback: Qt palette highlight
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette
        app = QApplication.instance()
        if app:
            return app.palette().color(QPalette.ColorRole.Highlight).name().upper()
    except Exception:
        pass
    return "#0078D4"  # Windows default blue


def _windows_is_dark() -> bool:
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return True


def build_system_theme() -> dict[str, str]:
    """Generate a RUMBLE theme from live Windows colours.

    The accent is sourced from the Windows DWM accent colour and light/dark
    mode from the system setting — but the surfaces get RUMBLE's premium
    treatment: in dark mode the same hue-tinted near-black ladder the rest of
    the collection uses (tinted toward the system accent), and in light mode an
    airy cool-silver faintly washed with that accent. So it still tracks
    Windows, but never falls back to flat Windows greys.
    """
    accent = _windows_accent()
    is_dark = _windows_is_dark()
    accent_hover  = _tl(accent, 28)
    accent_active = _td(accent, 22)

    if is_dark:
        return _dark_theme(
            "System", "Windows",
            accent, accent_hover, accent_active,
            tint=accent, tint_scale=1.0,
        )

    # Light: airy cool-silver, faintly tinted toward the system accent so the
    # whole surface still reads as "lit" by the Windows colour.
    def mx(base: str, t: float) -> str:
        return _mix(base, accent, t)

    on_accent = "#0B0B0E" if _luminance(accent) > 0.60 else "#FFFFFF"
    return {
        "bg_deep":    mx("#C6C9DA", 0.05),
        "bg_base":    mx("#DBDDEC", 0.04),
        "bg_surface": mx("#F2F3FB", 0.03),
        "bg_raised":  mx("#FBFCFF", 0.02),
        "bg_sunken":  mx("#C1C4D6", 0.05),

        "border_subtle":  mx("#CDD0E0", 0.05),
        "border_default": mx("#ADB1C8", 0.06),
        "border_strong":  accent,

        "accent":        accent,
        "accent_hover":  accent_hover,
        "accent_active": accent_active,
        "accent_muted":  accent + "1F",
        "accent_glow":   accent + "3A",

        "text_primary":   "#13142A",
        "text_secondary": "#474A66",
        "text_muted":     "#8589A6",
        "text_on_accent": on_accent,

        "success": "#0E9C84",
        "warning": "#C76A0E",
        "error":   "#CC3340",
        "info":    accent,

        "graph_bg":       mx("#E6E7F4", 0.04),
        "graph_grid":     mx("#CFD1E2", 0.05),
        "graph_zero":     mx("#A9ADC6", 0.06),
        "graph_curve":    accent,
        "graph_band":     accent + "55",
        "graph_band_sel": "#C76A0E",
        "graph_points":   "#0E9C84",
        "graph_handle":   accent,
        "graph_text":     mx("#8589A6", 0.05),

        "scrollbar_track":       mx("#D3D5E6", 0.04),
        "scrollbar_thumb":       mx("#ADB1C8", 0.06),
        "scrollbar_thumb_hover": mx("#8589A8", 0.08),

        "name":        "System",
        "accent_name": "Windows",
    }


def qss_color(token: str) -> str:
    """Convert an '#RRGGBBAA' token into a QSS-safe 'rgba(r, g, b, a)' string.

    Qt's stylesheet hex parser expects '#AARRGGBB', not '#RRGGBBAA', so any
    8-digit hex token must go through this before landing in inline QSS.
    Plain 6-digit hex tokens pass through unchanged.
    """
    text = token.strip()
    if len(text) == 9 and text.startswith("#"):
        r, g, b, a = (int(text[i:i + 2], 16) for i in (1, 3, 5, 7))
        return f"rgba({r}, {g}, {b}, {a})"
    return text
