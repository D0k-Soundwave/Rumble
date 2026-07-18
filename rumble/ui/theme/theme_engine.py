"""Generates a complete QSS stylesheet from a theme token dict and applies it.

Switching themes at runtime is just: regenerate the stylesheet string from the
new token dict and reapply it to the QApplication instance. No widget-level
restyling is needed — Qt re-cascades the stylesheet through the whole tree.
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

from rumble.ui.density import dp
from rumble.ui.theme import effects
from rumble.ui.theme.tokens import _lum, qss_color, theme_float

# The profile-status LED's "you are hearing this" green — CSS/X11 `springgreen`.
# Fixed across every theme on purpose (see the badge-live rules below), so it
# cannot be a themed token and has to be pinned to a value here.
#
# Chosen by searching the sRGB gamut for the vivid green (HSV sat >= 90%,
# value >= 90%, hue <= 150 so it stays green rather than mint) that maximises
# the minimum CIEDE2000 distance to every colour in every shipped theme, then
# taking the named CSS colour at the top of that set. Results:
#   - dE >= 15.2 from the nearest colour in ANY theme (dE > 10 reads as a
#     plainly different colour). Its closest neighbour is Screamer's success
#     #66CC00 — the green the Pre-Amp toggle paints one row below the LED,
#     which is the confusion that matters most here.
#   - 3.96:1 contrast on the worst surface it is ever drawn on (Screamer's
#     grey #6B6B6B header), clearing WCAG 2.2 SC 1.4.11's 3:1 for a status
#     indicator. Every other theme is 4.2:1 or better.
#   - Matches no colour in any theme exactly.
# tests/selftest_led_green.py re-derives all of this and fails if a theme
# change ever moves a colour close to it.
LED_GREEN = "#00FF7F"


def _alpha(hex_color: str, alpha_hex: str) -> str:
    """Build an 'rgba(r, g, b, a)' string from a '#RRGGBB' base colour and a
    two-digit hex alpha byte (e.g. _alpha('#5E81F4', '26'))."""
    text = hex_color.strip().lstrip("#")
    r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    a = int(alpha_hex, 16)
    return f"rgba({r}, {g}, {b}, {a})"


def _lighten(hex_color: str, amount: int) -> str:
    """Return a hex colour with each RGB channel increased by `amount`."""
    text = hex_color.strip().lstrip("#")
    r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = min(255, r + amount), min(255, g + amount), min(255, b + amount)
    return f"#{r:02X}{g:02X}{b:02X}"


def _darken(hex_color: str, amount: int) -> str:
    """Return a hex colour with each RGB channel decreased by `amount`."""
    text = hex_color.strip().lstrip("#")
    r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = max(0, r - amount), max(0, g - amount), max(0, b - amount)
    return f"#{r:02X}{g:02X}{b:02X}"


class ThemeEngine:
    def __init__(self, token_dict: dict[str, str]):
        self.t: dict[str, str] = token_dict

    def apply(self, app: QApplication) -> None:
        app.setStyle("Fusion")
        app.setStyleSheet(self._generate_qss())
        # Refresh the soft glass shadows/glow on every open window to match the
        # new palette (reused in place, so switching never flickers).
        effects.set_current_theme(self.t)
        for w in app.topLevelWidgets():
            effects.refresh_tree(w, self.t)

    def switch(self, app: QApplication, new_token_dict: dict[str, str]) -> None:
        self.t = new_token_dict
        self.apply(app)

    # ------------------------------------------------------------------

    def _generate_qss(self) -> str:
        t = dict(self.t)
        for key in ("accent_muted", "accent_glow"):
            if key in t:
                t[key] = qss_color(t[key])

        # ── Structural levers ─────────────────────────────────────────────
        # Optional per-theme knobs that let two themes with similar colours
        # still read as different materials. All default to 1.0, which
        # reproduces the original QSS byte-for-byte, so System, imported and
        # custom themes that omit them are unaffected.
        #   sheen  — gloss: scales the lighten/darken delta of gradient stops
        #            (0 = flat/matte, >1 = glossier).
        #   bevel  — raised edge: scales the top-lit / bottom-dark border lines.
        #   rscale — corner roundness: scales every border-radius.
        #   glow   — luminance of the accent halo; read by rumble.ui.theme.effects
        #            to size the soft accent glow/elevation drop-shadows.
        def _f(key: str) -> float:
            return theme_float(t, key)
        sheen  = _f("sheen")
        bevel  = _f("bevel")
        rscale = _f("radius_scale")

        def _r(px: int) -> int:
            return max(0, round(px * rscale))

        def lg(c: str, n: int) -> str:
            return _lighten(c, round(n * sheen))

        def dk(c: str, n: int) -> str:
            return _darken(c, round(n * sheen))

        def bvd(c: str, n: int) -> str:
            return _darken(c, round(n * bevel))

        def bvl(c: str, n: int) -> str:
            return _lighten(c, round(n * bevel))

        # Catch-light: the bright top edge where light grazes a glass surface.
        # Tied to sheen so glossy themes catch more light, matte themes less.
        def cl(c: str, n: int) -> str:
            return _lighten(c, round(n * sheen))

        # Named corner radii — generous, glass-friendly bases scaled per theme.
        r_xs = _r(3)
        r_sm = _r(4)
        r_md = _r(8)
        r_lg = _r(12)

        # ── Glass-HUD treatment ───────────────────────────────────────────
        # Frosted glass surfaces (translucent fills + a bright near-white rim
        # catch-light) under a subtle backdrop gradient, so panes read as lit
        # glass. Qt can't blur the backdrop, so the depth comes from the rim,
        # the backdrop gradient and the HudFrame corner ticks/glow.
        glow = _f("glow")

        is_light = _lum(t["bg_base"]) > 0.5

        glass_alpha = max(0.0, min(1.0, theme_float(t, "glass_alpha", 0.90)))
        _ga = f"{round(glass_alpha * 255):02X}"

        def tr(c: str) -> str:
            """Translucent glass fill of a #RRGGBB colour at glass_alpha."""
            return _alpha(c, _ga)

        # Bright near-white rim along the top edge of each glass pane.
        _rim_a = max(0, min(255, round((20 if is_light else 34) * sheen)))
        rim = _alpha("#FFFFFF", f"{_rim_a:02X}")

        # Subtle diagonal backdrop so translucent panes show depth (window/dialog).
        backdrop = (
            f"qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            f"stop:0 {t['bg_deep']}, stop:0.55 {t['bg_base']}, "
            f"stop:1 {dk(t['bg_base'], 3)})"
        )

        # Derived gradient stops — computed once, used repeatedly. The surface
        # gradients are 3-stop (bright catch-light → base → faint shade) so they
        # read as curved glass rather than a flat fill.
        btn_top    = cl(t["bg_raised"], 16)
        btn_mid    = t["bg_raised"]
        btn_bot    = dk(t["bg_raised"], 4)
        btn_edge   = bvl(t["bg_raised"], 22)            # catch-light top border
        btn_h_top  = cl(t["bg_raised"], 30)
        btn_h_bot  = lg(t["bg_raised"], 8)
        btn_p_top  = dk(t["bg_raised"], 6)
        btn_p_bot  = dk(t["bg_raised"], 10)

        acc_top    = cl(t["accent"], 22)
        acc_mid    = t["accent"]
        acc_bot    = dk(t["accent"], 10)
        acc_h_top  = cl(t["accent"], 34)
        acc_h_bot  = lg(t["accent"], 10)

        suc_top    = lg(t["success"], 12)
        suc_bot    = t["success"]

        panel_top  = cl(t["bg_surface"], 12)
        panel_mid  = t["bg_surface"]
        panel_bot  = dk(t["bg_surface"], 4)
        panel_edge = bvl(t["bg_surface"], 18)           # catch-light top border

        card_top   = cl(t["bg_raised"], 14)
        card_mid   = t["bg_raised"]
        card_bot   = dk(t["bg_raised"], 5)
        card_edge  = bvl(t["bg_raised"], 20)

        field_top  = lg(t["bg_sunken"], 5)
        field_bot  = dk(t["bg_sunken"], 4)

        # Density-dependent metrics: the Compact interface keeps the same
        # colours and structure but trims paddings, control heights and a
        # point of font size so the smaller fixed window stays uncluttered.
        font_base      = dp(12, 11)
        font_small     = dp(11, 10)
        font_display   = dp(18, 14)
        btn_pad        = dp(5, 2)
        btn_pad_h      = dp(16, 10)
        ctl_min_h      = dp(22, 18)
        field_pad      = dp(5, 2)
        pill_pad_h     = dp(12, 8)
        pill_radius    = _r(dp(11, 9))
        section_h      = dp(40, 28)
        menubar_h      = dp(32, 24)
        menu_item_pad  = dp(6, 3)
        tab_pad_v      = dp(7, 3)
        tab_pad_h      = dp(16, 10)
        header_pad     = dp(6, 3)
        check_size     = dp(16, 14)
        raw_pad        = dp(12, 6)

        # ── Type system ──────────────────────────────────────────────────
        # Three roles, each on its own family (registered at startup from
        # rumble/assets/fonts). The fallbacks keep the app legible if a font
        # ever fails to load. See rumble/__main__._load_fonts.
        ui_font      = '"Inter", "Segoe UI", Arial, sans-serif'      # body / UI
        display_font = '"Space Grotesk", "Inter", "Segoe UI", sans-serif'  # headers / wordmark
        mono_font    = '"IBM Plex Mono", "Cascadia Code", "Consolas", monospace'  # numeric readouts

        return f"""
/* ─── Base ─── */
QWidget {{
    background-color: {t['bg_base']};
    color: {t['text_primary']};
    font-family: {ui_font};
    font-size: {font_base}px;
    border: none;
}}
/* Labels never paint their own opaque backdrop — they sit on whatever
   panel/card/glass surface contains them, including translucent ones
   (About/AI Profiles hero & info cards). Classed labels that *want* a
   background (chips, badges) still get it: their [class=...] selector
   is more specific and overrides this. */
QLabel {{
    background: transparent;
}}

/* ─── Main Window ─── */
/* Subtle diagonal backdrop so translucent glass panes read with depth. */
QMainWindow {{
    background: {backdrop};
}}

/* ─── Panels / Frames ─── */
/* Frosted glass: a translucent 3-stop gradient (catch-light → base → faint
   shade) with a bright near-white rim along the top edge, so panes read as lit
   panes of glass floating over the backdrop. */
QFrame[class="panel"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {tr(panel_top)}, stop:0.14 {tr(panel_mid)}, stop:1 {tr(panel_bot)});
    border: 1px solid {t['border_default']};
    border-top: 1px solid {rim};
    border-radius: {r_md}px;
}}
QFrame[class="surface"] {{
    background-color: {t['bg_surface']};
}}
QFrame[class="card"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {tr(card_top)}, stop:0.16 {tr(card_mid)}, stop:1 {tr(card_bot)});
    border: 1px solid {t['border_default']};
    border-top: 1px solid {rim};
    border-radius: {r_md}px;
}}
QFrame[class="card-selected"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {tr(card_top)}, stop:0.16 {tr(card_mid)}, stop:1 {tr(card_bot)});
    border: 1px solid {t['accent']};
    border-top: 1px solid {t['accent']};
    border-radius: {r_md}px;
}}
QFrame[class="hline"] {{
    background-color: {t['border_subtle']};
    max-height: 1px;
    min-height: 1px;
}}
QFrame[class="warning-banner"] {{
    background-color: {_alpha(t['warning'], '26')};
    border: none;
    border-left: 3px solid {t['warning']};
    border-radius: {r_sm}px;
}}
QFrame[class="info-banner"] {{
    background-color: {_alpha(t['info'], '1A')};
    border: none;
    border-left: 3px solid {t['info']};
    border-radius: {r_sm}px;
}}
/* Hero header band for dialogs (About, AI Profiles). */
QFrame[class="hero"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {tr(cl(t['bg_surface'], 14))}, stop:0.5 {tr(t['bg_surface'])},
        stop:1 {tr(dk(t['bg_surface'], 4))});
    border: 1px solid {t['border_default']};
    border-top: 1px solid {rim};
    border-bottom: 2px solid {t['accent']};
    border-radius: {r_lg}px;
}}

/* ─── Band column strip ─── */
QWidget[class="band-column"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_surface'], 6)}, stop:1 {t['bg_surface']});
    border-right: 1px solid {t['border_subtle']};
}}
QWidget[class="band-column"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_raised'], 8)}, stop:1 {t['bg_raised']});
}}
QWidget[class="band-column-selected"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_raised'], 12)}, stop:1 {t['bg_raised']});
    border-right: 1px solid {t['border_subtle']};
    border-top: 2px solid {t['accent']};
}}

/* ─── Buttons ─── */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {btn_top}, stop:0.5 {btn_mid}, stop:1 {btn_bot});
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-top: 1px solid {btn_edge};
    border-bottom: 1px solid {bvd(t['border_default'], 10)};
    border-radius: {r_sm}px;
    padding: {btn_pad}px {btn_pad_h}px;
    min-height: {ctl_min_h}px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {btn_h_top}, stop:1 {btn_h_bot});
    border-color: {t['border_strong']};
}}
QPushButton:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {btn_p_top}, stop:1 {btn_p_bot});
    border-color: {t['border_strong']};
}}
QPushButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {acc_top}, stop:0.5 {acc_mid}, stop:1 {acc_bot});
    color: {t['text_on_accent']};
    border: 1px solid {cl(t['accent'], 30)};
}}
QPushButton:checked:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {acc_h_top}, stop:1 {acc_h_bot});
}}
QPushButton[class="primary"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {acc_top}, stop:0.5 {acc_mid}, stop:1 {acc_bot});
    color: {t['text_on_accent']};
    border: 1px solid {cl(t['accent'], 30)};
    border-bottom: 1px solid {bvd(t['accent_active'], 15)};
}}
QPushButton[class="primary"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {acc_h_top}, stop:1 {acc_h_bot});
}}
QPushButton[class="primary"]:pressed {{
    background: {t['accent_active']};
}}
QPushButton[class="success"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {suc_top}, stop:1 {suc_bot});
    color: {t['bg_deep']};
    border: 1px solid {_darken(t['success'], 20)};
    border-bottom: 1px solid {bvd(t['success'], 35)};
}}
QPushButton[class="success"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['success'], 30)}, stop:1 {lg(t['success'], 15)});
}}
QPushButton[class="warning-outline"] {{
    background: transparent;
    color: {t['warning']};
    border: 1px solid {t['warning']};
}}
QPushButton[class="warning-outline"]:hover {{
    background: {_alpha(t['warning'], '26')};
}}
QPushButton[class="warning-outline"]:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['warning'], 20)}, stop:1 {t['warning']});
    color: {t['bg_deep']};
    border: 1px solid {_darken(t['warning'], 20)};
}}
/* Pre-Amp toggle — colour carries the state: engaged (checked) reads as the
   theme's success colour, disabled (unchecked, preamp 0 dB / clipping risk)
   reads as the theme's warning colour. */
QPushButton[class="preamp-toggle"] {{
    background: transparent;
    color: {t['warning']};
    border: 1px solid {t['warning']};
}}
QPushButton[class="preamp-toggle"]:hover {{
    background: {_alpha(t['warning'], '26')};
}}
QPushButton[class="preamp-toggle"]:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['success'], 30)}, stop:1 {t['success']});
    color: {t['bg_deep']};
    border: 1px solid {_darken(t['success'], 20)};
}}
QPushButton[class="preamp-toggle"]:checked:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['success'], 40)}, stop:1 {lg(t['success'], 10)});
}}
QPushButton[class="danger"] {{
    background: transparent;
    color: {t['error']};
    border: 1px solid {t['error']};
}}
QPushButton[class="danger"]:hover {{
    background: {_alpha(t['error'], '26')};
}}
QPushButton[class="ghost"] {{
    background: transparent;
    border: none;
    color: {t['text_secondary']};
    padding: {btn_pad}px 8px;
}}
QPushButton[class="ghost"]:hover {{
    background: {t['bg_raised']};
    color: {t['text_primary']};
}}
QPushButton[class="ghost"]:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {acc_top}, stop:0.5 {acc_mid}, stop:1 {acc_bot});
    color: {t['text_on_accent']};
}}
QPushButton[class="ghost"]:checked:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {acc_h_top}, stop:1 {acc_h_bot});
}}
/* Ghost with an outline — for a control that opens something (a popover, a
   panel) and so has to read as a button at rest, where a plain ghost reads as
   a label until you hover it. */
QPushButton[class="ghost-outline"] {{
    background: transparent;
    border: 1px solid {t['border_subtle']};
    color: {t['text_secondary']};
    padding: {btn_pad}px 8px;
}}
QPushButton[class="ghost-outline"]:hover {{
    background: {t['bg_raised']};
    border: 1px solid {t['border_strong']};
    color: {t['text_primary']};
}}
QPushButton[class="ghost-outline"]:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {acc_top}, stop:0.5 {acc_mid}, stop:1 {acc_bot});
    border: 1px solid {cl(t['accent'], 30)};
    color: {t['text_on_accent']};
}}
QPushButton[class="ghost-outline"]:checked:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {acc_h_top}, stop:1 {acc_h_bot});
}}
QPushButton[class="section-header"] {{
    background: transparent;
    border: none;
    border-bottom: 1px solid {t['border_subtle']};
    border-radius: 0px;
    color: {t['text_secondary']};
    font-weight: 600;
    text-align: left;
    padding: 0px 12px;
    min-height: {section_h}px;
}}
QPushButton[class="section-header"]:hover {{
    background: {_alpha(t['bg_raised'], '60')};
    color: {t['text_primary']};
}}
QPushButton[class="icon"] {{
    background: transparent;
    border: none;
    border-radius: {r_sm}px;
    padding: 0;
}}
QPushButton[class="icon"]:hover {{
    background: {t['bg_raised']};
}}
QPushButton[class="pill"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_raised'], 10)}, stop:1 {t['bg_raised']});
    border: 1px solid {t['border_default']};
    color: {t['text_secondary']};
    border-radius: {pill_radius}px;
    padding: 3px {pill_pad_h}px;
    min-height: {dp(16, 12)}px;
    font-size: {font_small}px;
}}
QPushButton[class="pill-active"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['accent'], 15)}, stop:1 {t['accent']});
    border: 1px solid {t['accent_active']};
    color: {t['text_on_accent']};
    border-radius: {pill_radius}px;
    padding: 3px {pill_pad_h}px;
    min-height: {dp(16, 12)}px;
    font-size: {font_small}px;
}}
QPushButton:disabled {{
    color: {t['text_muted']};
    border-color: {t['border_subtle']};
    background: {t['bg_deep']};
}}

/* ─── Input Fields ─── */
QLineEdit, QDoubleSpinBox, QSpinBox {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {field_top}, stop:1 {field_bot});
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-top: 1px solid {bvd(t['border_default'], 12)};
    border-radius: {r_sm}px;
    padding: {field_pad}px 6px;
    min-height: {ctl_min_h}px;
    font-size: {font_base}px;
}}
/* Numeric value fields read like an instrument panel (fixed-width digits). */
QDoubleSpinBox, QSpinBox {{
    font-family: {mono_font};
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {t['accent']};
    border-top-color: {bvd(t['accent'], 20)};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_sunken'], 4)}, stop:1 {field_bot});
}}
QLineEdit:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {{
    background: {t['bg_deep']};
    color: {t['text_muted']};
    border-color: {t['border_subtle']};
}}
QLineEdit[class="display"] {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    font-family: {display_font};
    font-size: {font_display}px;
    font-weight: 600;
    border-radius: 0;
    padding: 0 2px;
}}
QLineEdit[class="display"]:focus {{
    border-bottom: 2px solid {t['accent']};
}}
QLineEdit[class="ghost-input"] {{
    background: transparent;
    border: none;
    color: {t['text_secondary']};
    font-size: {font_small}px;
    padding: 0 2px;
}}
QLineEdit[class="error"], QDoubleSpinBox[class="error"] {{
    border: 1px solid {t['error']};
    background: {_alpha(t['error'], '14')};
}}
QLineEdit[class="warning"], QDoubleSpinBox[class="warning"] {{
    border: 1px solid {t['warning']};
    background: {_alpha(t['warning'], '14')};
}}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QSpinBox::up-button, QSpinBox::down-button {{
    width: 0px;
    border: none;
}}

/* ─── ComboBox ─── */
QComboBox {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {field_top}, stop:1 {field_bot});
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-top: 1px solid {bvd(t['border_default'], 12)};
    border-radius: {r_sm}px;
    padding: {field_pad}px 8px;
    min-height: {ctl_min_h}px;
}}
QComboBox:focus {{
    border-color: {t['accent']};
}}
QComboBox:disabled {{
    color: {t['text_muted']};
    background: {t['bg_deep']};
}}
QComboBox::drop-down {{
    width: 20px;
    border: none;
}}
QComboBox QAbstractItemView {{
    background: {t['bg_raised']};
    color: {t['text_primary']};
    border: 1px solid {t['border_strong']};
    border-radius: {r_md}px;
    selection-background-color: {t['accent_muted']};
    selection-color: {t['text_primary']};
    outline: none;
}}

/* ─── Labels ─── */
QLabel[class="heading"] {{
    color: {t['text_secondary']};
    font-family: {display_font};
    font-size: {font_small}px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel[class="display"] {{
    color: {t['text_primary']};
    font-family: {display_font};
    font-size: {font_display}px;
    font-weight: 600;
}}
QLabel[class="muted"] {{
    color: {t['text_muted']};
    font-size: {dp(10, 9)}px;
    letter-spacing: 0.5px;
}}
QLabel[class="secondary"] {{
    color: {t['text_secondary']};
    font-size: {font_small}px;
}}
/* Profile-status LED: lit green while you are hearing this profile, dark
   otherwise (see EditorPanel._set_led_state). One colour, because off is off
   and on is on.

   The green is NOT the theme's `success` token, deliberately. `success` is
   only green in 3 of the 7 themes — it is grey in Malice (#7A7B7D), blue in
   Prime, blue-grey in Panther and teal in Bludgeon — so the LED used to light
   up grey when active, i.e. it never changed colour at all. It is a physical
   indicator, not a themed accent, so it holds one fixed green everywhere. */
QLabel[class="badge-live"] {{
    color: {LED_GREEN};
    font-size: {font_small}px;
    font-weight: 600;
}}
QLabel[class="value-success"] {{
    color: {t['success']};
    font-family: {mono_font};
    font-weight: 500;
}}
QLabel[class="value-warning"] {{
    color: {t['warning']};
    font-family: {mono_font};
    font-weight: 500;
}}
QLabel[class="value-error"] {{
    color: {t['error']};
    font-family: {mono_font};
    font-weight: 500;
}}
QLabel[class="mono"] {{
    font-family: {mono_font};
    font-size: 11px;
    color: {t['text_secondary']};
}}
QLabel[class="app-title"] {{
    color: {t['text_primary']};
    font-family: {display_font};
    font-size: {dp(30, 24)}px;
    font-weight: 700;
    letter-spacing: 4px;
}}
QLabel[class="subheading"] {{
    color: {t['accent']};
    font-family: {display_font};
    font-size: {dp(13, 12)}px;
    font-weight: 700;
    letter-spacing: 0.5px;
}}
QLabel[class="feature-title"] {{
    color: {t['text_primary']};
    font-size: {font_base}px;
    font-weight: 600;
}}
/* Small rounded status/version chips. */
QLabel[class="chip"] {{
    color: {t['accent']};
    background: {t['accent_muted']};
    border: 1px solid {_alpha(t['accent'], '66')};
    border-radius: {_r(dp(11, 9))}px;
    padding: {dp(2, 1)}px {dp(10, 8)}px;
    font-size: {font_small}px;
    font-weight: 600;
}}
QLabel[class="chip-accent"] {{
    color: {t['text_on_accent']};
    background: {t['accent']};
    border: 1px solid {t['accent_active']};
    border-radius: {_r(dp(11, 9))}px;
    padding: {dp(2, 1)}px {dp(10, 8)}px;
    font-size: {font_small}px;
    font-weight: 700;
}}
QLabel[class="chip-muted"] {{
    color: {t['text_secondary']};
    background: {_alpha(t['text_secondary'], '22')};
    border: 1px solid {t['border_default']};
    border-radius: {_r(dp(11, 9))}px;
    padding: {dp(2, 1)}px {dp(10, 8)}px;
    font-size: {font_small}px;
    font-weight: 600;
}}

/* ─── Scrollbars ─── */
QScrollBar:vertical {{
    background: {t['scrollbar_track']};
    width: 6px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t['scrollbar_thumb']};
    border-radius: {r_sm}px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t['scrollbar_thumb_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: {t['scrollbar_track']};
    height: 6px;
}}
QScrollBar::handle:horizontal {{
    background: {t['scrollbar_thumb']};
    border-radius: {r_sm}px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {t['scrollbar_thumb_hover']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* ─── Tooltips ─── */
QToolTip {{
    background: {t['bg_raised']};
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-radius: {r_sm}px;
    padding: 2px 5px;
    font-size: 13px;
}}

/* ─── Splitter ─── */
QSplitter::handle {{
    background-color: {t['border_subtle']};
    width: 1px;
}}

/* ─── Tab Widget ─── */
QTabWidget::pane {{
    background: {t['bg_surface']};
    border: 1px solid {t['border_subtle']};
    border-radius: {r_md}px;
}}
QTabBar::tab {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_deep'], 8)}, stop:1 {t['bg_deep']});
    color: {t['text_muted']};
    padding: {tab_pad_v}px {tab_pad_h}px;
    /* The selected tab expands a few px upward when painted — keep a top
       margin so it never overlaps the divider line above the tab bar. */
    margin-top: 4px;
    min-height: {dp(22, 16)}px;
    font-size: {font_small}px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_surface'], 10)}, stop:1 {t['bg_surface']});
    color: {t['text_primary']};
    border-bottom: 2px solid {t['accent']};
}}
QTabBar::tab:hover {{
    color: {t['text_secondary']};
    background: {lg(t['bg_deep'], 12)};
}}

/* ─── Table / List ─── */
QTableWidget, QListWidget, QTreeWidget {{
    background-color: {t['bg_surface']};
    alternate-background-color: {t['bg_sunken']};
    border: 1px solid {t['border_subtle']};
    gridline-color: {t['border_subtle']};
    outline: none;
}}
QListWidget::item, QTreeWidget::item {{
    border: none;
}}
QListWidget::item:hover {{
    background-color: {t['bg_raised']};
}}
QListWidget::item:selected {{
    background-color: {t['accent_muted']};
    color: {t['text_primary']};
}}
QHeaderView::section {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_deep'], 10)}, stop:1 {t['bg_deep']});
    color: {t['text_muted']};
    border: none;
    border-bottom: 1px solid {t['border_default']};
    padding: {header_pad}px 8px;
    min-height: {dp(20, 16)}px;
    font-size: {font_small}px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}

/* ─── Plain Text Edit (raw config view) ─── */
QPlainTextEdit[class="raw-config"] {{
    background-color: {t['graph_bg']};
    color: {t['text_secondary']};
    border: 1px solid {t['border_subtle']};
    border-radius: {r_sm}px;
    padding: {raw_pad}px;
    font-family: {mono_font};
    font-size: {font_small}px;
}}
QPlainTextEdit[class="raw-text-edit"] {{
    background-color: {t['bg_sunken']};
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-radius: {r_sm}px;
    padding: {dp(8, 5)}px;
    font-family: {mono_font};
    font-size: {font_small}px;
}}

/* ─── Menu ─── */
QMenu {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_raised'], 8)}, stop:1 {t['bg_raised']});
    border: 1px solid {t['border_default']};
    border-radius: {r_md}px;
    padding: 4px 0;
}}
QMenu::item {{
    padding: {menu_item_pad}px 12px;
    min-height: {dp(20, 16)}px;
    color: {t['text_primary']};
    font-size: {font_base}px;
}}
QMenu::item:selected {{
    background-color: {t['accent_muted']};
    color: {t['text_primary']};
}}
QMenu::item:disabled {{
    color: {t['text_muted']};
}}
QMenu::separator {{
    height: 1px;
    background: {t['border_subtle']};
    margin: 4px 0;
}}

/* ─── Menu Bar ─── */
QMenuBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_deep'], 8)}, stop:1 {t['bg_deep']});
    color: {t['text_secondary']};
    min-height: {menubar_h}px;
    border-bottom: 1px solid {t['border_subtle']};
}}
QMenuBar::item {{
    background: transparent;
    padding: {menu_item_pad}px 12px;
}}
QMenuBar::item:selected {{
    background-color: {t['accent_muted']};
    color: {t['text_primary']};
    border-radius: {r_sm}px;
}}
QMenuBar::item:hover {{
    background-color: {t['bg_surface']};
    color: {t['text_primary']};
}}

/* ─── Status Bar ─── */
QStatusBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {dk(t['bg_deep'], 4)}, stop:1 {t['bg_deep']});
    color: {t['text_muted']};
    font-size: {font_small}px;
    border-top: 1px solid {t['border_subtle']};
}}

/* ─── Window Chrome (frameless drag strip + close/minimize) ─── */
QWidget[class="chrome-strip"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_deep'], 8)}, stop:1 {t['bg_deep']});
    border-bottom: 1px solid {t['border_subtle']};
}}
QLabel[class="chrome-title"] {{
    color: {t['text_secondary']};
    font-size: {font_small}px;
}}
QPushButton[class="chrome-close"], QPushButton[class="chrome-minimize"] {{
    background: transparent;
    border: none;
    border-radius: {r_sm}px;
    padding: 0;
}}
QPushButton[class="chrome-close"]:hover {{
    background: {t['error']};
}}
QPushButton[class="chrome-minimize"]:hover {{
    background: {t['bg_raised']};
}}

/* ─── Checkboxes ─── */
/* Transparent for the same reason QLabel is: the base QWidget rule paints
   every widget with bg_base, which a checkbox on a card (Settings) would
   otherwise drag along as an opaque bar the full width of the row. Only the
   indicator paints. */
QCheckBox {{
    background: transparent;
    color: {t['text_primary']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: {check_size}px;
    height: {check_size}px;
    border: 1px solid {t['border_default']};
    border-radius: {r_sm}px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['bg_sunken'], 6)}, stop:1 {t['bg_sunken']});
}}
QCheckBox::indicator:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['accent'], 20)}, stop:1 {t['accent']});
    border-color: {t['accent_active']};
}}

/* ─── Progress Bar ─── */
QProgressBar {{
    background-color: {t['bg_sunken']};
    border: none;
    border-radius: {r_xs}px;
    height: 4px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {t['accent']}, stop:1 {lg(t['accent'], 30)});
    border-radius: {r_xs}px;
}}

/* ─── Slider ─── */
QSlider::groove:horizontal {{
    height: 4px;
    background: {t['bg_sunken']};
    border: 1px solid {t['border_subtle']};
    border-radius: {r_xs}px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['accent'], 25)}, stop:1 {t['accent']});
    border: 1px solid {t['accent_active']};
    border-radius: 7px;
    margin: -5px 0;
}}
QSlider::handle:horizontal:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {lg(t['accent_hover'], 20)}, stop:1 {t['accent_hover']});
}}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {t['accent']}, stop:1 {lg(t['accent'], 20)});
    border-radius: {r_xs}px;
}}

/* ─── Dialogs ─── */
QDialog {{
    background: {backdrop};
}}

/* Keyboard focus frame for the interactive EQ response graph only — it is the
   one keyboard-navigable control that needs a visible focus indicator. Other
   controls keep the baseline look (no accent focus outline). The graph also
   paints its own accent frame in paintEvent; this marks any default frame too. */
QWidget[class="eq-graph-focus"]:focus {{
    outline: 2px solid {t['accent']};
    outline-offset: -2px;
}}
"""
