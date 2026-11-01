"""Renders goat.ico, v8 (2026-11-01): the goat mark inside the v8 presence
ring. The ring runs from the voice's blue into Claude's work amber (the two
brains), on a deep midnight squircle with a soft top sheen. Tiny sizes
(<=32 px) drop the ring and enlarge the mark so it stays readable in the
taskbar and tray. The mark itself is ui_qt.paint_goat_mark, so the icon, the
rail and the bubble are still the same face.

Run:  cd C:/Users/giorg/projects/goat-standalone-master/python && py -3.13 make_icon.py
"""
import io
import os
import sys

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QConicalGradient, QGuiApplication, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen,
                           QRadialGradient)

S = 1024  # master size, downscaled for the .ico
BLUE, CYAN, AMBER = "#5aa2ff", "#7fe3ff", "#f2b36b"


def _qcol(hexs: str, a: int = 255) -> QColor:
    c = QColor(hexs)
    c.setAlpha(a)
    return c


def _to_pil(img: QImage) -> Image.Image:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def render(small: bool = False) -> Image.Image:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    import ui_qt
    t = ui_qt.THEMES["midnight"]
    img = QImage(S, S, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    # squircle body
    square = QPainterPath()
    square.addRoundedRect(QRectF(0, 0, S, S), S * 0.23, S * 0.23)
    p.setClipPath(square)
    bg = QRadialGradient(S * 0.5, S * 0.32, S * 0.85)
    bg.setColorAt(0.0, QColor("#1d3060"))
    bg.setColorAt(0.55, QColor("#0c1630"))
    bg.setColorAt(1.0, QColor("#05080f"))
    p.fillRect(0, 0, S, S, bg)
    # warm glow low right (Claude), cool glow top left (voice)
    for cx, cy, col, a in ((0.82, 0.86, AMBER, 46), (0.2, 0.15, BLUE, 40)):
        g = QRadialGradient(S * cx, S * cy, S * 0.55)
        g.setColorAt(0.0, _qcol(col, a))
        g.setColorAt(1.0, _qcol(col, 0))
        p.fillRect(0, 0, S, S, g)

    c = QPointF(S / 2, S / 2)
    if not small:
        # presence ring: blue -> cyan -> amber -> blue, with a halo
        r = S * 0.385
        ring = QConicalGradient(c, 90)
        ring.setColorAt(0.0, QColor(BLUE))
        ring.setColorAt(0.30, QColor(CYAN))
        ring.setColorAt(0.55, QColor(AMBER))
        ring.setColorAt(0.80, QColor("#8f8cff"))
        ring.setColorAt(1.0, QColor(BLUE))
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        p.setBrush(Qt.NoBrush)
        for w, a in ((S * 0.11, 22), (S * 0.07, 40), (S * 0.045, 90)):
            halo = QConicalGradient(ring)
            stops = []
            for pos, col in ring.stops():
                col = QColor(col)
                col.setAlpha(a)
                stops.append((pos, col))
            halo.setStops(stops)
            p.setPen(QPen(halo, w, Qt.SolidLine, Qt.RoundCap))
            p.drawEllipse(rect)
        p.setPen(QPen(ring, S * 0.026, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(rect)
        # bright highlight dot on the ring (the "alive" spark)
        spark = QRadialGradient(c.x() + r * 0.707, c.y() - r * 0.707, S * 0.05)
        spark.setColorAt(0.0, _qcol("#ffffff", 235))
        spark.setColorAt(0.35, _qcol(CYAN, 150))
        spark.setColorAt(1.0, _qcol(CYAN, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(spark)
        p.drawEllipse(QPointF(c.x() + r * 0.707, c.y() - r * 0.707), S * 0.05, S * 0.05)
        mark = S * 0.52
    else:
        mark = S * 0.80

    ui_qt.paint_goat_mark(
        p, QRectF(c.x() - mark / 2, c.y() - mark / 2 + S * 0.01, mark, mark),
        t, glow=True)

    # glass sheen + hairline rim
    sheen = QLinearGradient(0, 0, 0, S * 0.5)
    sheen.setColorAt(0.0, _qcol("#ffffff", 26))
    sheen.setColorAt(1.0, _qcol("#ffffff", 0))
    p.fillRect(0, 0, S, int(S * 0.5), sheen)
    p.setClipping(False)
    rim = QLinearGradient(0, 0, 0, S)
    rim.setColorAt(0.0, _qcol("#ffffff", 70))
    rim.setColorAt(1.0, _qcol("#ffffff", 8))
    p.setPen(QPen(rim, S * 0.006))
    p.setBrush(Qt.NoBrush)
    inset = S * 0.003
    p.drawRoundedRect(QRectF(inset, inset, S - 2 * inset, S - 2 * inset),
                      S * 0.23, S * 0.23)
    p.end()
    return _to_pil(img)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    big, small = render(), render(small=True)
    frames = {}
    for n in (256, 128, 64, 48):
        frames[n] = big.resize((n, n), Image.LANCZOS)
    for n in (32, 24, 16):
        frames[n] = small.resize((n, n), Image.LANCZOS)
    frames[256].save(os.path.join(root, "goat.ico"), format="ICO",
                     sizes=[(n, n) for n in frames],
                     append_images=[frames[n] for n in frames if n != 256])
    big.resize((512, 512), Image.LANCZOS).save(
        os.path.join(root, "python", "goat-icon-preview.png"))
    small.resize((64, 64), Image.LANCZOS).save(
        os.path.join(root, "python", "goat-icon-preview-small.png"))
    print("written")
