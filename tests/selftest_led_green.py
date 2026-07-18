"""Self-test for the profile-status LED's fixed green.

The LED answers one question — is what I'm editing what I'm hearing? — so its
colour has to survive every theme. It used to be painted with the theme's
`success` token, which is only green in 3 of the 7 shipped themes (grey in
Malice, blue in Prime, blue-grey in Panther, teal in Bludgeon), so the "active"
LED lit up grey and never appeared to change colour at all. It is now pinned to
theme_engine.LED_GREEN.

This re-derives the properties that made that value the right one, so a future
theme edit that moves a colour next to it fails here instead of quietly making
the LED ambiguous again:

  (a) LED_GREEN is not used verbatim by any theme;
  (b) it stays perceptually distinct (CIEDE2000 >= 10) from every colour in
      every theme — above all from each theme's `success`, the green the
      Pre-Amp toggle paints one row below the LED;
  (c) it clears WCAG 2.2 SC 1.4.11's 3:1 non-text contrast on every surface it
      is drawn on (the header bar is QFrame[class="surface"] -> bg_surface);
  (d) the LED is lit only while you are actually hearing the profile — off is
      off, on is on, and bypassed is dark rather than a colour of its own;
  (e) it reserves the same width lit or dark, so toggling never reflows the
      header's buttons.

Run:  python tests/selftest_led_green.py
Uses Qt's offscreen platform plugin — no window is shown."""
from __future__ import annotations

import math
import os
import re
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

app = QApplication([])

from rumble.ui.theme.theme_engine import LED_GREEN, ThemeEngine
from rumble.ui.theme.tokens import FALLBACK_THEME, THEMES, get_theme

FAILURES: list[str] = []

# CIEDE2000 distance at which two colours read as plainly different rather than
# as two shades of one colour. 1.0 is the classic "just noticeable difference";
# 10 is an order of magnitude beyond it, i.e. nobody could mistake one for the
# other.
_MIN_DISTINCT_DE = 10.0
# WCAG 2.2 SC 1.4.11 Non-text Contrast: graphical objects and UI components
# that convey state need 3:1 against their background. The LED is exactly that.
_MIN_CONTRAST = 3.0

_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")


def step(label: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception:
        FAILURES.append(label)
        print(f"  FAIL  {label}\n{traceback.format_exc()}")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def to_rgb(value: str):
    """'#RRGGBB' (or '#RRGGBBAA') -> (r, g, b) floats, or None if not a colour."""
    if not isinstance(value, str):
        return None
    m = _HEX.match(value.strip())
    if not m:
        return None
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb):
    r, g, b = (_linear(c) for c in rgb)
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x / 0.95047), f(y / 1.0), f(z / 1.08883)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def contrast(a, b) -> float:
    def lum(rgb):
        r, g, bb = (_linear(c) for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bb

    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def ciede2000(lab1, lab2) -> float:
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    avg_L = (L1 + L2) / 2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    avg_C = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(avg_C ** 7 / (avg_C ** 7 + 25 ** 7))) if avg_C > 0 else 0
    a1p, a2p = a1 * (1 + G), a2 * (1 + G)
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    avg_Cp = (C1p + C2p) / 2
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp) / 2)
    if C1p * C2p == 0:
        avg_hp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        avg_hp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        avg_hp = (h1p + h2p + 360) / 2
    else:
        avg_hp = (h1p + h2p - 360) / 2
    T = (1 - 0.17 * math.cos(math.radians(avg_hp - 30))
         + 0.24 * math.cos(math.radians(2 * avg_hp))
         + 0.32 * math.cos(math.radians(3 * avg_hp + 6))
         - 0.20 * math.cos(math.radians(4 * avg_hp - 63)))
    Rc = 2 * math.sqrt(avg_Cp ** 7 / (avg_Cp ** 7 + 25 ** 7)) if avg_Cp > 0 else 0
    Sl = 1 + (0.015 * (avg_L - 50) ** 2) / math.sqrt(20 + (avg_L - 50) ** 2)
    Sc = 1 + 0.045 * avg_Cp
    Sh = 1 + 0.015 * avg_Cp * T
    Rt = -math.sin(math.radians(2 * (30 * math.exp(-(((avg_hp - 275) / 25) ** 2))))) * Rc
    return math.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
                     + Rt * (dCp / Sc) * (dHp / Sh))


def all_themes():
    return list(THEMES.items()) + [("FALLBACK", FALLBACK_THEME)]


def led_is_valid_hex() -> None:
    expect(to_rgb(LED_GREEN) is not None, f"LED_GREEN {LED_GREEN!r} is not a #RRGGBB colour")


def led_is_unused_by_every_theme() -> None:
    for name, theme in all_themes():
        for key, value in theme.items():
            if isinstance(value, str) and value.strip().upper()[:7] == LED_GREEN.upper():
                raise AssertionError(
                    f"{name}.{key} is {value} — the same colour as the LED. The LED "
                    f"must stay unique so it cannot be read as a themed accent.")


def led_is_distinct_from_every_theme_colour() -> None:
    led_lab = rgb_to_lab(to_rgb(LED_GREEN))
    worst = (999.0, "")
    for name, theme in all_themes():
        for key, value in theme.items():
            rgb = to_rgb(value)
            if rgb is None:
                continue
            de = ciede2000(led_lab, rgb_to_lab(rgb))
            if de < worst[0]:
                worst = (de, f"{name}.{key} ({value})")
    expect(worst[0] >= _MIN_DISTINCT_DE,
           f"LED {LED_GREEN} is only dE {worst[0]:.1f} from {worst[1]} — too close to "
           f"tell apart (need >= {_MIN_DISTINCT_DE}).")


def led_is_distinct_from_every_success_colour() -> None:
    """The Pre-Amp toggle paints itself `success` one row under the LED, so this
    is the pair a user is most likely to see side by side and conflate."""
    led_lab = rgb_to_lab(to_rgb(LED_GREEN))
    for name, theme in all_themes():
        rgb = to_rgb(theme.get("success", ""))
        if rgb is None:
            continue
        de = ciede2000(led_lab, rgb_to_lab(rgb))
        expect(de >= _MIN_DISTINCT_DE,
               f"LED {LED_GREEN} is only dE {de:.1f} from {name}.success "
               f"({theme['success']}), which the Pre-Amp toggle paints right below it.")


def led_is_legible_on_every_surface() -> None:
    """The header bar is QFrame[class='surface'] -> bg_surface."""
    led = to_rgb(LED_GREEN)
    for name, theme in all_themes():
        rgb = to_rgb(theme.get("bg_surface", ""))
        if rgb is None:
            continue
        c = contrast(led, rgb)
        expect(c >= _MIN_CONTRAST,
               f"LED {LED_GREEN} has only {c:.2f}:1 contrast on {name}.bg_surface "
               f"({theme['bg_surface']}) — WCAG 2.2 SC 1.4.11 needs {_MIN_CONTRAST}:1.")


def every_theme_paints_the_led_green() -> None:
    """The regression that started this: `success` is not green in 4 of 7 themes."""
    for name, _ in all_themes():
        qss = ThemeEngine(get_theme(name))._generate_qss()
        m = re.search(r'QLabel\[class="badge-live"\]\s*\{([^}]*)\}', qss)
        expect(m is not None, f"{name}: no badge-live rule in the generated QSS")
        expect(LED_GREEN.lower() in m.group(1).lower(),
               f"{name}: the LED's base rule is not {LED_GREEN} — it reads {m.group(1)!r}")


def the_led_is_lit_only_while_you_hear_the_profile() -> None:
    """Off is off, on is on. Bypassed is dark, not a colour of its own: dark
    already means "you are not hearing this profile", and while bypassed you
    are hearing none of it. The LED claiming ACTIVE while bypassed was the bug.
    """
    from rumble.ui.editor_panel import EditorPanel

    panel = EditorPanel(get_theme("Soundwave"))
    led = panel.badge_live
    cases = {
        (False, False): "",           # editing something that isn't running
        (False, True): "",            # not running, and bypassed
        (True, True): "",             # the running profile, but EQ switched off
        (True, False): "●  ACTIVE",   # running, and you can hear it
    }
    for (is_active, is_bypassed), want in cases.items():
        panel._set_led_state(is_active=is_active, is_bypassed=is_bypassed)
        got = led.text()
        expect(got == want,
               f"is_active={is_active}, is_bypassed={is_bypassed}: LED reads {got!r}, "
               f"expected {want!r}")


def the_led_never_reflows_the_header() -> None:
    """The LED must hold one width whether lit or dark.

    It is measured on real laid-out geometry, not on minimumWidth: an earlier
    attempt set a minimum from fontMetrics (44px) that was narrower than the
    label's actual laid-out width (50px), so the minimum never bound, the label
    still sized itself to its text, and lighting up grew it 6px into the
    profile name column beside it — which takes the leftover space via a
    stretch factor and so gets re-elided on every toggle.

    Measured in a real MainWindow, whose header is width-constrained the way
    the shipped one is. A bare EditorPanel has so much slack that the layout
    absorbs the difference and the check passes on a broken LED.
    """
    import tempfile

    from PySide6.QtGui import QIcon

    from rumble.core.profile_manager import ProfileManager, make_flat_config
    from rumble.ui.main_window import MainWindow
    from rumble.ui.theme.theme_engine import ThemeEngine

    tmp = Path(tempfile.mkdtemp(prefix="rumble_led_"))
    mgr = ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")
    mgr.ensure_first_run()
    mgr.settings["show_safety_warning"] = False
    mgr.save()
    mgr.create_profile("A Profile With A Name Long Enough To Fill The Header Bar",
                       config=make_flat_config(10, channels=["L", "R"]))
    engine = ThemeEngine(get_theme("Soundwave"))
    engine.apply(app)
    w = MainWindow(mgr, engine, QIcon())
    w.show()
    app.processEvents()
    ep = w.editor_panel
    pid = mgr.profiles[0].id
    w._on_profile_selected(pid)
    app.processEvents()

    seen = {}
    for tag, (is_active, is_bypassed) in {
            "lit": (True, False),
            "dark": (False, False),
            "bypassed": (True, True),
    }.items():
        ep.set_active_state(is_active=is_active, is_bypassed=is_bypassed)
        ep._header_bar.layout().activate()
        app.processEvents()
        seen[tag] = (ep.badge_live.width(), ep.badge_live.x(),
                     ep.name_edit.width(), ep.btn_save.x())
    w.close()

    expect(len(set(seen.values())) == 1,
           "the LED changes the header's layout as it toggles — "
           "(LED width, LED x, name width, Save x) per state: " + repr(seen))
    expect(seen["lit"][0] > 0, f"the LED reserves no width at all: {seen['lit']}")


def main() -> int:
    step("LED_GREEN is a valid colour", led_is_valid_hex)
    step("no theme uses the LED's green", led_is_unused_by_every_theme)
    step("the LED is distinct from every colour in every theme",
         led_is_distinct_from_every_theme_colour)
    step("the LED cannot be confused with any theme's success green",
         led_is_distinct_from_every_success_colour)
    step("the LED is legible on every theme's header surface",
         led_is_legible_on_every_surface)
    step("every theme paints the active LED green (not success)",
         every_theme_paints_the_led_green)
    step("the LED is lit only while you are hearing the profile",
         the_led_is_lit_only_while_you_hear_the_profile)
    step("lighting the LED never reflows the header row",
         the_led_never_reflows_the_header)
    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
