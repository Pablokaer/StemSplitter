"""Generate assets/icon.png, icon.ico and icon.icns (run once; outputs are committed)."""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=(28, 24, 48, 255))
colors = [(124, 92, 255), (255, 94, 135), (255, 184, 64), (64, 210, 170)]  # vocals, drums, bass, other
heights = [[0.35, 0.6, 0.9, 0.5, 0.3], [0.7, 0.4, 0.8, 0.45, 0.65], [0.5, 0.75, 0.35, 0.6, 0.4], [0.3, 0.55, 0.7, 0.85, 0.5]]
lane_h, top = 170, 150
for r, (c, hs) in enumerate(zip(colors, heights)):
    cy = top + r * (lane_h + 10) + lane_h / 2
    for i, h in enumerate(hs):
        x = 190 + i * 136
        half = h * lane_h / 2
        d.rounded_rectangle([x, cy - half, x + 90, cy + half], radius=40, fill=c + (255,))
out = Path(__file__).resolve().parent.parent / "assets"
img.save(out / "icon.png")
img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(out / "icon.icns")
print("icons written to", out)
