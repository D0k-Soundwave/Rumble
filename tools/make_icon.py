"""Render the RUMBLE glyph and pack it into rumble/rumble.ico
(modern PNG-in-ICO format, supported by Windows Vista+).

The glyph is a large triangular rumble filling the canvas on a *fully
transparent* background, with the visible spectrum fanning out from the apex
down through the glass — dispersion contained inside the rumble, no incoming
beam. On-theme for the name and for an EQ tool. Run:

    python tools/make_icon.py            # writes rumble/rumble.ico
    python tools/make_icon.py --preview  # also writes preview + 1024px master PNGs
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QImage, QPainter, QPainterPath, QTransform,
)
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "rumble" / "rumble.ico"
# Windows .ico tops out at 256×256 per image; every size is rendered natively
# (not downscaled) so it stays crisp from the 16px tray glyph up.
SIZES = (16, 24, 32, 48, 64, 128, 256)
# The transparent letter punched out of the rumble ("" / None = plain triangle).
LETTER = "P"
# A high-resolution master beyond the .ico's 256px cap (for store art, etc.).
MASTER_SIZE = 1024
MASTER_PATH = ROOT / "packaging" / "rumble_icon_1024.png"

# Refracted spectrum, apex (red) → base spread (violet); vivid but not neon.
SPECTRUM = [
    QColor("#FF453A"),  # red
    QColor("#FF9F0A"),  # orange
    QColor("#FFD60A"),  # yellow
    QColor("#32D74B"),  # green
    QColor("#0A84FF"),  # blue
    QColor("#5E5CE6"),  # violet
]


def _spectrum_color(t: float) -> QColor:
    """Linearly interpolate the spectrum at position ``t`` in 0..1."""
    t = max(0.0, min(1.0, t))
    span = len(SPECTRUM) - 1
    pos = t * span
    i = min(int(pos), span - 1)
    f = pos - i
    c0, c1 = SPECTRUM[i], SPECTRUM[i + 1]
    return QColor(
        round(c0.red() + (c1.red() - c0.red()) * f),
        round(c0.green() + (c1.green() - c0.green()) * f),
        round(c0.blue() + (c1.blue() - c0.blue()) * f),
    )


def _lerp(a: QPointF, b: QPointF, t: float) -> QPointF:
    return QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)


def _tri_path(a: QPointF, b: QPointF, c: QPointF) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(a)
    path.lineTo(b)
    path.lineTo(c)
    path.closeSubpath()
    return path


def _inset_triangle(a: QPointF, b: QPointF, c: QPointF, d: float):
    """The triangle whose edges are all moved inward by ``d`` (a similar
    triangle scaled about the incenter, so edges stay parallel)."""
    import math
    la = math.dist((b.x(), b.y()), (c.x(), c.y()))
    lb = math.dist((a.x(), a.y()), (c.x(), c.y()))
    lc = math.dist((a.x(), a.y()), (b.x(), b.y()))
    per = la + lb + lc
    ix = (la * a.x() + lb * b.x() + lc * c.x()) / per
    iy = (la * a.y() + lb * b.y() + lc * c.y()) / per
    area = abs((b.x() - a.x()) * (c.y() - a.y()) - (c.x() - a.x()) * (b.y() - a.y())) / 2.0
    r = area / (per / 2.0)
    s = (r - d) / r
    return (QPointF(ix + (a.x() - ix) * s, iy + (a.y() - iy) * s),
            QPointF(ix + (b.x() - ix) * s, iy + (b.y() - iy) * s),
            QPointF(ix + (c.x() - ix) * s, iy + (c.y() - iy) * s))


_LETTER_FAMILY: str | None = None
_LETTER_CX: dict[str, float] = {}


def _ink_centroid_x(glyph: QPainterPath, gb: QRectF) -> float:
    """Fraction 0..1 across the glyph's bounding box where its filled ink's
    horizontal centre of mass sits — used to optically centre asymmetric
    letters (a 'P' is stem-heavy on the left, so its ink centroid is left of
    the bbox centre)."""
    res = 256
    mask = QImage(res, res, QImage.Format.Format_ARGB32_Premultiplied)
    mask.fill(Qt.GlobalColor.transparent)
    mp = QPainter(mask)
    t = QTransform()
    t.scale(res / gb.width(), res / gb.height())
    t.translate(-gb.x(), -gb.y())
    mp.setTransform(t)
    mp.fillPath(glyph, QColor(255, 255, 255))
    mp.end()
    total = 0
    weighted = 0
    for x in range(res):
        col = sum(1 for y in range(res) if (mask.pixel(x, y) >> 24) & 0xFF > 128)
        weighted += col * x
        total += col
    return 0.5 if total == 0 else (weighted / total) / res


def _letter_family() -> str:
    """Brand font (Inter SemiBold) for the cut-out letter, with a safe fallback."""
    global _LETTER_FAMILY
    if _LETTER_FAMILY is None:
        font_file = ROOT / "rumble" / "assets" / "fonts" / "Inter-SemiBold.ttf"
        fam = ""
        if font_file.exists():
            fid = QFontDatabase.addApplicationFont(str(font_file))
            fams = QFontDatabase.applicationFontFamilies(fid) if fid != -1 else []
            fam = fams[0] if fams else ""
        _LETTER_FAMILY = fam or "Arial"
    return _LETTER_FAMILY


def _cut_letter(p: QPainter, letter: str, apex: QPointF, br: QPointF,
                bl: QPointF, size: float) -> None:
    """Punch ``letter`` out of the already-painted triangle as fully transparent
    pixels (so the user's background shows through), sized as large as it fits
    inside the rumble with a small margin to every edge."""
    gap = max(2.0, size * 0.03)             # small margin between letter and rumble
    ia, ib, ic = _inset_triangle(apex, br, bl, gap)

    font = QFont(_letter_family())
    font.setPixelSize(1000)                 # large for an accurate outline
    glyph = QPainterPath()
    glyph.addText(0.0, 0.0, font, letter)
    gb = glyph.boundingRect()
    if gb.width() <= 0 or gb.height() <= 0:
        return

    # Optically centre on the ink centroid (not the bbox) so a stem-heavy 'P'
    # doesn't look left-shoved.
    cx = _LETTER_CX.get(letter)
    if cx is None:
        cx = _ink_centroid_x(glyph, gb)
        _LETTER_CX[letter] = cx
    centroid_gx = gb.x() + cx * gb.width()
    y_base = ic.y()

    # Grow the glyph (centroid pinned to the centre axis, bottom on the inset
    # base) until its actual OUTLINE — not its bounding box — touches a slanted
    # rumble edge. Fitting the real outline lets a 'P' fill far more of the
    # triangle, since its empty top-right corner no longer limits the size.
    samples = [glyph.pointAtPercent(i / 600.0) for i in range(601)]
    g = QPointF((ia.x() + ib.x() + ic.x()) / 3.0, (ia.y() + ib.y() + ic.y()) / 3.0)
    scale = float("inf")
    for e1, e2 in ((ia, ib), (ia, ic)):
        dx, dy = e2.x() - e1.x(), e2.y() - e1.y()
        inside = dx * (g.y() - e1.y()) - dy * (g.x() - e1.x())   # interior sign
        for pt in samples:
            bx = pt.x() - centroid_gx
            by = pt.y() - gb.bottom()
            c0 = dx * (y_base - e1.y()) - dy * (size / 2.0 - e1.x())
            c1 = dx * by - dy * bx
            # Increasing scale moves this point toward the edge only when c1
            # opposes the interior sign; the limit is where it reaches the edge.
            if c1 * inside < 0 < c0 * inside:
                scale = min(scale, -c0 / c1)
    if not (scale < float("inf")):
        return

    t = QTransform()
    t.translate(size / 2.0, y_base)
    t.scale(scale, scale)
    t.translate(-centroid_gx, -gb.bottom())
    placed = t.map(glyph)

    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    p.setPen(Qt.PenStyle.NoPen)
    p.fillPath(placed, QColor(0, 0, 0, 255))
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)


def render(size: int, letter: str | None = None) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)  # entirely transparent background

    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # The triangle fills the whole canvas: apex at the top-centre edge, base
    # corners in the bottom corners — no surrounding margin.
    apex = QPointF(size / 2.0, 0.0)
    bl = QPointF(0.0, size)
    br = QPointF(size, size)

    # Spectrum fan: tile the triangle with thin wedges that all share the apex
    # and step across the base bl→br, so the colours radiate from the apex and
    # spread through the glass. Adjacent wedges overlap by a hair to avoid
    # antialiasing seams; the union's outer edges stay smooth (no hard clip).
    n = max(96, int(size * 2))
    overlap = 1.6 / n
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(n):
        t0 = i / n
        t1 = min(1.0, (i + 1) / n + overlap)
        wedge = _tri_path(apex, _lerp(bl, br, t0), _lerp(bl, br, t1))
        p.fillPath(wedge, _spectrum_color((t0 + t1) / 2.0))

    if letter:
        _cut_letter(p, letter, apex, br, bl, float(size))

    p.end()
    return image


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def write_ico(images: list[QImage], out_path: Path) -> None:
    entries = []
    payloads = []
    offset = 6 + 16 * len(images)
    for image in images:
        data = png_bytes(image)
        size = image.width()
        width_byte = 0 if size >= 256 else size
        height_byte = 0 if size >= 256 else size
        entries.append(struct.pack("<BBBBHHII", width_byte, height_byte, 0, 0, 1, 32, len(data), offset))
        payloads.append(data)
        offset += len(data)

    header = struct.pack("<HHH", 0, 1, len(images))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(header)
        for entry in entries:
            fh.write(entry)
        for payload in payloads:
            fh.write(payload)


def main() -> None:
    QApplication.instance() or QApplication(sys.argv)
    letter = None if "--plain" in sys.argv else LETTER

    images = [render(s, letter=letter) for s in SIZES]
    write_ico(images, OUT_PATH)
    print(f"Wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")

    # High-resolution PNG master (beyond the .ico's 256px ceiling).
    render(MASTER_SIZE, letter=letter).save(str(MASTER_PATH), "PNG")
    print(f"Wrote {MASTER_PATH} ({MASTER_SIZE}px)")

    if "--preview" in sys.argv:
        out = ROOT / "_iconpreview"
        out.mkdir(exist_ok=True)
        for s in (256, 64, 32, 16):
            render(s, letter=letter).save(str(out / f"rumble_icon_{s}.png"), "PNG")
        print(f"Wrote preview PNGs to {out}")


if __name__ == "__main__":
    main()
