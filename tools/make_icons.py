"""Draws the installed app's icons into app/ (run once; the PNGs are committed):
an orange tile with a snowy peak and a low sun - the dashboard's accent colour and its mountain art.

  icon-192.png / icon-512.png    rounded tile on transparent (the "any" icon: tabs, app switcher)
  icon-maskable-512.png          full-bleed tile, art inside the 80% safe circle (Android's adaptive icon)
  apple-touch-icon.png           180 px full-bleed (iOS home screen, if ever used)
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "app")
ORANGE, CREAM, SUN, SHADE, RIDGE = "#FE5000", "#FFFFFF", "#FFE7A3", "#FFD6C0", "#C43E00"
SS = 4   # supersampling, for smooth edges


def art(d, s, k):
    """The peak and sun, scaled by k (1 = full size, smaller keeps it in the maskable safe zone)."""
    c = 256 * s

    def P(x, y):
        return (c + (x - 256) * k * s, c + (y - 256) * k * s)

    d.ellipse([*P(318, 112), *P(410, 204)], fill=SUN)                                   # the sun, low behind
    d.polygon([P(86, 380), P(222, 150), P(286, 252), P(334, 196), P(426, 380)], fill=CREAM)  # the peaks
    d.polygon([P(222, 150), P(286, 252), P(268, 380), P(232, 380)], fill=SHADE)          # shaded east faces
    d.polygon([P(334, 196), P(426, 380), P(372, 380)], fill=SHADE)
    d.line([P(86, 380), P(426, 380)], fill=RIDGE, width=int(10 * k * s))                 # the ground line


def tile(size, rounded, k):
    s = size * SS / 512
    im = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if rounded:
        d.rounded_rectangle([0, 0, size * SS - 1, size * SS - 1], radius=int(112 * s), fill=ORANGE)
    else:
        d.rectangle([0, 0, size * SS, size * SS], fill=ORANGE)
    art(d, s, k)
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    tile(192, True, 1).save(os.path.join(OUT, "icon-192.png"))
    tile(512, True, 1).save(os.path.join(OUT, "icon-512.png"))
    tile(512, False, 0.72).save(os.path.join(OUT, "icon-maskable-512.png"))
    tile(180, False, 0.86).convert("RGB").save(os.path.join(OUT, "apple-touch-icon.png"))
    print("icons written to", OUT)
