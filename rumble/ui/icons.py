"""SVG icon loading with runtime colour tinting (Material Symbols subset)."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICONS_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"

_FILL_RE = re.compile(r'fill="(?:#[0-9A-Fa-f]{3,8}|currentColor|none)"')


@lru_cache(maxsize=256)
def _tinted_pixmap(name: str, color: str, size: int) -> QPixmap:
    svg_path = ICONS_DIR / f"{name}.svg"
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    if not svg_path.exists():
        return pixmap

    try:
        raw = svg_path.read_text(encoding="utf-8")
    except OSError:
        return pixmap

    tinted, count = _FILL_RE.subn(f'fill="{color}"', raw)
    if count == 0:
        # No fill attribute found (path-only SVGs) — inject one on <path>/<svg>.
        tinted = raw.replace("<path ", f'<path fill="{color}" ', 1)

    renderer = QSvgRenderer(QByteArray(tinted.encode("utf-8")))
    if not renderer.isValid():
        return pixmap

    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap


def icon_pixmap(name: str, color: str, size: int = 16) -> QPixmap:
    return _tinted_pixmap(name, color, size)


def icon(name: str, color: str, size: int = 16) -> QIcon:
    return QIcon(icon_pixmap(name, color, size))
