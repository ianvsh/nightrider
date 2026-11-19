"""Boot logo: PORSCHE over 924S in wide Porsche-style vector letters (a look-alike of
the logo lettering, not the real font). One still frame, shown the moment the screen
is up and replaced by the dashboard as soon as that's ready: it never adds to start-up
time (Ian: "fast startup as possible"). The same picture goes on the screen even
earlier in boot (system/splash.sh reads the copy save_splash writes).
`python3 display.py --png out.png intro` saves it."""
import math
import os

import numpy as np
from PIL import Image, ImageDraw

from pixels import BG, GREEN, H, W, overlay

CW, CH = W // 2, H // 2                   # 400 x 240: drawn at 2x for finer letters


def rounded(points, r=0.22, closed=False):
    """A polyline with its corners rounded off (in glyph units)."""
    pts = points + [points[0], points[1]] if closed else points
    out = [pts[0]] if not closed else []
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        ab, bc = math.dist(a, b), math.dist(b, c)
        if not ab or not bc:
            continue
        k1, k2 = min(r, ab / 2) / ab, min(r, bc / 2) / bc
        p1 = (b[0] + (a[0] - b[0]) * k1, b[1] + (a[1] - b[1]) * k1)
        p2 = (b[0] + (c[0] - b[0]) * k2, b[1] + (c[1] - b[1]) * k2)
        for t in np.linspace(0, 1, 6):                                  # a quadratic curve round b
            out.append(((1 - t) ** 2 * p1[0] + 2 * (1 - t) * t * b[0] + t * t * p2[0],
                        (1 - t) ** 2 * p1[1] + 2 * (1 - t) * t * b[1] + t * t * p2[1]))
    out.append(pts[-1] if not closed else out[0])
    return out


W_, M_ = 1.5, 0.5                         # glyph width (height 1), middle bar height
GLYPHS = {
    "P": [rounded([(0, 1), (0, 0), (W_, 0), (W_, M_), (0, M_)])],
    "O": [rounded([(0, 0), (W_, 0), (W_, 1), (0, 1)], closed=True)],
    "R": [rounded([(0, 1), (0, 0), (W_, 0), (W_, M_), (0, M_)]), [(W_ * 0.62, M_), (W_, 1)]],
    "S": [rounded([(W_, 0), (0, 0), (0, M_), (W_, M_), (W_, 1), (0, 1)])],
    "C": [rounded([(W_, 0), (0, 0), (0, 1), (W_, 1)])],
    "H": [[(0, 0), (0, 1)], [(W_, 0), (W_, 1)], [(0, M_), (W_, M_)]],
    "E": [rounded([(W_, 0), (0, 0), (0, 1), (W_, 1)]), [(0, M_), (W_ * 0.8, M_)]],
    "9": [rounded([(W_, M_), (0, M_), (0, 0), (W_, 0), (W_, 1), (0, 1)])],
    "2": [rounded([(0, 0), (W_, 0), (W_, M_), (0, M_), (0, 1), (W_, 1)])],
    "4": [[(0, 0), (0, 0.62), (W_, 0.62)], [(W_ * 0.75, 0.25), (W_ * 0.75, 1)]],
}
GAP = 0.55                                # between letters, in letter heights


def word_width(word, size):
    return len(word) * (W_ + GAP) * size - GAP * size


def wordmark(word, y0, size):
    """Strokes (lists of canvas points) for word, centred, top y0, letters size px high."""
    strokes, x = [], (CW - word_width(word, size)) / 2
    for ch in word:
        for s in GLYPHS[ch]:
            strokes.append([(x + px * size, y0 + py * size) for px, py in s])
        x += (W_ + GAP) * size
    return strokes


# Two lines, centred on the screen, the same width (~300 of 400 px): PORSCHE over a taller 924S
LINES = [("PORSCHE", 22, 3), ("924S", 39, 4)]       # word, letter height, stroke width (canvas px)
LINE_GAP = 20


def still():
    """The logo, panel-sized (800x480) for the framebuffer."""
    img = Image.new("RGB", (CW, CH), BG)
    d = ImageDraw.Draw(img)
    y = (CH - sum(size for _, size, _ in LINES) - LINE_GAP) / 2
    for word, size, width in LINES:
        for s in wordmark(word, y, size):
            d.line(s, fill=GREEN, width=width, joint="curve")
        y += size + LINE_GAP
    panel = np.zeros((H, W, 3), dtype=np.uint8)
    return overlay(panel, img, 0, 0, scale=2)


render = still                            # (display.py --png)

SPLASH = os.path.expanduser("~/.cache/nightrider/splash.rgb565")


def save_splash(path=SPLASH, panel=None):
    """The logo as raw RGB565 (800x480, the panel's own format), for system/splash.sh to
    copy onto the screen early in boot. Only rewritten when it changed (SD card)."""
    a = (still() if panel is None else panel).astype(np.uint16)
    raw = (((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)).astype("<u2").tobytes()
    try:
        with open(path, "rb") as f:
            if f.read() == raw:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(raw)
    os.replace(path + ".tmp", path)
    return True
