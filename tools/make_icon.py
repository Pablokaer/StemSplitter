"""Generate assets/icon.png, icon.ico and icon.icns from the official logo (run once; outputs are committed).

The logo (assets/logo.png, a copy of docs/images/Glossy Red Octopus Equalizer Logo.png) is red on pure black,
so it is added onto a rounded near-black square: the black adds nothing and the octopus is left untouched.
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 1024
logo = Image.open(ROOT / "assets" / "logo.png").convert("RGB").resize((S - 120, S - 120), Image.LANCZOS)

plate = Image.new("RGB", (S, S), (8, 8, 8))
plate.paste(ImageChops.add(Image.new("RGB", logo.size, (8, 8, 8)), logo), (60, 60))

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(plate, (0, 0), mask)

out = ROOT / "assets"
img.save(out / "icon.png")
img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(out / "icon.icns")
print("icons written to", out)
