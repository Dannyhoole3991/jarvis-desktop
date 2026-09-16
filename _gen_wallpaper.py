"""
Generates a desktop wallpaper: a radial glow in the orb's own colors
(white -> cyan -> blue -> deep navy/black) fading outward, with a
scattered starfield -- denser and more visible out toward the dark
edges, sparser near the bright core where a real glow would wash faint
stars out. Native resolution, no stock-photo compression artifacts.
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
import random

random.seed(7)
np.random.seed(7)

WIDTH, HEIGHT = 2560, 1440
OUT_PATH = r"C:\Users\danny\Desktop\Jarvis\jarvis_sky_background.jpg"

# Color stops, matching the orb's own gradient (see jarvis_hud.html):
# white core -> cyan -> blue -> deep navy -> near-black edges.
STOPS = [
    (0.00, (255, 255, 255)),
    (0.10, (200, 245, 255)),
    (0.22, (52, 229, 255)),
    (0.42, (47, 141, 255)),
    (0.65, (20, 45, 90)),
    (0.85, (6, 12, 26)),
    (1.00, (2, 4, 10)),
]

cx, cy = WIDTH * 0.5, HEIGHT * 0.46

# Low-frequency noise field to warp the glow's shape so it reads as
# organic gas rather than a perfect UI gradient -- generated small and
# upscaled+blurred, which is what makes it "low frequency" (soft, large
# blobs) instead of static-like.
small = np.random.rand(10, 6).astype(np.float32)
warp = np.array(Image.fromarray((small * 255).astype(np.uint8)).resize((WIDTH, HEIGHT), Image.BICUBIC)) / 255.0
warp = np.array(Image.fromarray((warp * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius=WIDTH * 0.07))) / 255.0

yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
dist = np.sqrt(((xx - cx) / (WIDTH * 0.85)) ** 2 + ((yy - cy) / (HEIGHT * 0.85)) ** 2)
dist = dist + (warp - 0.5) * 0.12  # subtle organic irregularity, not a perfect ellipse
dist = np.clip(dist, 0, 1)
# Ease the falloff (smootherstep) so colour bands blend rather than ring.
dist = dist * dist * dist * (dist * (dist * 6 - 15) + 10)

r = np.zeros((HEIGHT, WIDTH), dtype=np.float64)
g = np.zeros((HEIGHT, WIDTH), dtype=np.float64)
b = np.zeros((HEIGHT, WIDTH), dtype=np.float64)

for i in range(len(STOPS) - 1):
    t0, c0 = STOPS[i]
    t1, c1 = STOPS[i + 1]
    mask = (dist >= t0) & (dist <= t1)
    local_t = np.clip((dist - t0) / (t1 - t0 + 1e-9), 0, 1)
    local_t = local_t * local_t * (3 - 2 * local_t)  # smoothstep per band too
    r[mask] = c0[0] + (c1[0] - c0[0]) * local_t[mask]
    g[mask] = c0[1] + (c1[1] - c0[1]) * local_t[mask]
    b[mask] = c0[2] + (c1[2] - c0[2]) * local_t[mask]

# Fine grain dithering noise -- breaks up JPEG/gradient banding so the
# smooth ramp doesn't show visible rings.
grain = (np.random.rand(HEIGHT, WIDTH) - 0.5) * 7.0
r = np.clip(r + grain, 0, 255)
g = np.clip(g + grain, 0, 255)
b = np.clip(b + grain, 0, 255)

rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
img = Image.fromarray(rgb, mode="RGB")
img = img.filter(ImageFilter.GaussianBlur(radius=1.1))  # settle the grain into soft texture, not speckle

# Starfield -- denser and brighter further from the glowing core.
draw = ImageDraw.Draw(img, "RGBA")
num_stars = 2200
for _ in range(num_stars):
    x = random.uniform(0, WIDTH)
    y = random.uniform(0, HEIGHT)
    d = min(1.0, ((x - cx) / (WIDTH * 0.62)) ** 2 + ((y - cy) / (HEIGHT * 0.62)) ** 2)
    if random.random() > (0.15 + 0.85 * d):
        continue  # thin out stars near the bright core
    size = random.choice([0.6, 0.8, 1.0, 1.0, 1.3, 1.8])
    brightness = int(160 + 95 * random.random())
    alpha = int(120 + 135 * random.random())
    draw.ellipse([x - size, y - size, x + size, y + size], fill=(brightness, brightness, 255, alpha))
    if size > 1.4 and random.random() < 0.3:
        # A handful of brighter stars get a tiny soft cross-glow.
        draw.line([x - size * 3, y, x + size * 3, y], fill=(brightness, brightness, 255, alpha // 3), width=1)
        draw.line([x, y - size * 3, x, y + size * 3], fill=(brightness, brightness, 255, alpha // 3), width=1)

img.save(OUT_PATH, quality=95)
print("saved", img.size, "->", OUT_PATH)
