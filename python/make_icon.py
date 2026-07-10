"""Renders goat.ico — the app's string-of-light on a warm near-black
rounded square, same design language as the window (v5 'instrument')."""
import math
from PIL import Image, ImageDraw, ImageFilter

S = 1024  # master size, downscaled for the .ico

AMBER = (255, 169, 77)
BG_TOP = (15, 14, 12)
BG_BOT = (11, 10, 9)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def string_points(w, mid, margin, amp):
    pts = []
    span = w - margin * 2
    for i in range(241):
        u = i / 240
        pin = math.sin(math.pi * u) ** 1.5
        y = mid + pin * amp * (
            math.sin(u * math.tau * 1.5 + 0.6) * 0.7
            + math.sin(u * math.tau * 3.0 - 0.8) * 0.3)
        pts.append((margin + span * u, y))
    return pts


def render():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # vertical gradient background
    bg = Image.new("RGB", (S, S))
    for y in range(S):
        t = y / (S - 1)
        bg.putpixel((0, y), tuple(
            int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOT)))
    bg = bg.resize((S, S))
    img.paste(bg, (0, 0))

    pts = string_points(S, S * 0.50, S * 0.09, S * 0.24)

    # halo pass: fat amber line, heavily blurred
    halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(halo).line(pts, fill=AMBER + (200,), width=int(S * 0.07),
                              joint="curve")
    halo = halo.filter(ImageFilter.GaussianBlur(S * 0.035))
    img.alpha_composite(halo)

    # the string itself: bright core
    core = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(core).line(pts, fill=(255, 205, 140, 255),
                              width=int(S * 0.026), joint="curve")
    core = core.filter(ImageFilter.GaussianBlur(S * 0.002))
    img.alpha_composite(core)

    # rounded-square silhouette (Windows 11 style)
    img.putalpha(rounded_mask(S, int(S * 0.22)))
    return img


if __name__ == "__main__":
    icon = render()
    icon.save("C:/Users/user/goat/goat.ico", format="ICO",
              sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                     (32, 32), (24, 24), (16, 16)])
    icon.resize((256, 256), Image.LANCZOS).save(
        "C:/Users/user/goat/goat-icon-preview.png")
    print("written")
