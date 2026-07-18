"""Self-test for the glass-HUD theme engine's robustness.

Covers the Session-1 theme-layer fixes:

  (a) every registered theme (built-in, custom, imported, System) expands into a
      full QSS stylesheet without raising — guards the centralized luminance
      helper across all palettes (finding #1);
  (b) validate_theme rejects a colour token that is not 6/8-digit hex, e.g. the
      3-digit shorthand "#FFF" (defense in depth for #1);
  (c) a colour-only imported theme carries NO structural lever keys
      (radius_scale/sheen/bevel/glow/glass_alpha) so it renders at the engine's
      neutral 1.0 baseline rather than inheriting RUMBLE's shape/gloss
      (finding #3), and still generates QSS.

Run:  python tests/selftest_theme_engine.py
Uses Qt's offscreen platform plugin — no window is shown."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

app = QApplication([])

from rumble.core.custom_themes import REQUIRED_TOKENS, import_theme_file, validate_theme
from rumble.ui.theme.theme_engine import ThemeEngine
from rumble.ui.theme.tokens import (
    FALLBACK_THEME,
    get_theme,
    ordered_theme_names,
    theme_float,
)

FAILURES: list[str] = []

# Structural levers that must NOT be back-filled into a colour-only theme.
_LEVERS = ("radius_scale", "sheen", "bevel", "glow", "glass_alpha")


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


def _colour_only_theme(name: str) -> dict:
    """A theme with every REQUIRED colour token (valid hex) and no levers."""
    t = {k: FALLBACK_THEME[k] for k in REQUIRED_TOKENS}
    t["name"] = name
    t["accent_name"] = "Test"
    return t


def all_themes_generate_qss() -> None:
    names = ordered_theme_names(include_system=True)
    expect("System" in names, "System should be in the ordered theme list")
    for name in names:
        tokens = get_theme(name)
        engine = ThemeEngine(tokens)
        qss = engine._generate_qss()
        expect(isinstance(qss, str) and len(qss) > 0,
               f"theme '{name}' produced empty QSS")
        # Exercise the full apply path too (effects.refresh_tree etc.).
        engine.switch(app, tokens)


def short_hex_rejected() -> None:
    bad = _colour_only_theme("BadHexTheme")
    bad["accent"] = "#FFF"  # 3-digit shorthand — must be rejected
    errors = validate_theme(bad)
    expect(any("accent" in e for e in errors),
           f"3-digit hex accent should be rejected, got errors={errors}")

    good = _colour_only_theme("GoodHexTheme")
    expect(validate_theme(good) == [],
           f"a fully-specified 6/8-digit theme should validate, got {validate_theme(good)}")


def colour_only_import_has_no_levers(tmp_path: Path) -> None:
    src = _colour_only_theme("ImportedColourOnly")
    import json
    f = tmp_path / "theme.rumble-theme"
    f.write_text(json.dumps(src), encoding="utf-8")

    filled, errors = import_theme_file(f)
    expect(errors == [], f"colour-only import should be valid, got {errors}")
    for lever in _LEVERS:
        expect(lever not in filled,
               f"imported colour-only theme must not inherit lever '{lever}'")
        # …and the shared reader returns the neutral default for the missing key.
        expect(theme_float(filled, lever, 1.0) == 1.0,
               f"missing lever '{lever}' should read as neutral 1.0")
    # It must still render.
    qss = ThemeEngine(filled)._generate_qss()
    expect(len(qss) > 0, "colour-only imported theme failed to generate QSS")


def nameless_import_reports_missing_name(tmp_path: Path) -> None:
    # A hand-authored theme that omits its identity tokens must NOT inherit the
    # fallback's name ("RUMBLE") on import — that would mask the real problem
    # behind a misleading "a built-in named 'RUMBLE' already exists" error.
    import json
    src = {k: FALLBACK_THEME[k] for k in REQUIRED_TOKENS
           if k not in ("name", "accent_name")}
    f = tmp_path / "nameless.rumble-theme"
    f.write_text(json.dumps(src), encoding="utf-8")
    filled, errors = import_theme_file(f)
    expect("name" not in filled, "import must not backfill an identity 'name'")
    expect(any("name" in e and "Missing" in e for e in errors),
           f"a nameless import should report a missing-name error, got {errors}")


def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="rumble_themetest_"))
    step("every theme generates QSS without raising (#1)", all_themes_generate_qss)
    step("validate_theme rejects 3-digit hex colour tokens (#1)", short_hex_rejected)
    step("colour-only import carries no structural levers (#3)",
         lambda: colour_only_import_has_no_levers(tmp))
    step("nameless import reports missing name, not a fallback-name clash",
         lambda: nameless_import_reports_missing_name(tmp))
    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
