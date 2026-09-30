"""Renders goat.ico — v7 (2026-09-30): the goat mark from the window's rail,
in its electric-blue gradient, on a midnight rounded square. The mark is
painted by ui_qt.paint_goat_mark, so the icon, the rail, the greeting and
the collapsed bubble are always the same face.

Run:  cd C:/Users/user/goat-standalone/python && py -3.13 make_icon.py
"""
import io
import os
import sys

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import (QColor, QGuiApplication, QImage, QLinearGradient,
                           QPainter, QPainterPath, QRadialGradient)

S = 1024  # master size, downscaled for the .ico


def render() -> Image.Image:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    import ui_qt
    t = ui_qt.THEMES["midnight"]
    img = QImage(S, S, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    square = QPainterPath()
    square.addRoundedRect(QRectF(0, 0, S, S), S * 0.22, S * 0.22)
    p.setClipPath(square)
    bg = QLinearGradient(0, 0, 0, S)
    bg.setColorAt(0.0, QColor("#14213d"))
    bg.setColorAt(1.0, QColor(t["bg_bot"]))
    p.fillRect(0, 0, S, S, bg)
    glow = QRadialGradient(S / 2, S * 0.45, S * 0.5)
    c = QColor(t["accent"])
    c.setAlpha(70)
    glow.setColorAt(0.0, c)
    c.setAlpha(0)
    glow.setColorAt(1.0, c)
    p.fillRect(0, 0, S, S, glow)
    pad = S * 0.12
    ui_qt.paint_goat_mark(p, QRectF(pad, pad, S - 2 * pad, S - 2 * pad), t, glow=True)
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icon = render()
    icon.save(os.path.join(root, "goat.ico"), format="ICO",
              sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                     (32, 32), (24, 24), (16, 16)])
    icon.resize((256, 256), Image.LANCZOS).save(
        os.path.join(root, "python", "goat-icon-preview.png"))
    print("written")
