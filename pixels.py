"""Retro pixel drawing shared by the touchscreen code: colours, a 5x7 font,
buttons, and the 266x160 -> 800x480 CRT scale-up."""
import time
import unicodedata

import numpy as np
from PIL import Image

W, H = 800, 480                   # the panel (5" DSI)
SCALE = 3                         # each drawn pixel becomes 3x3
LW, LH = W // SCALE, H // SCALE   # what we draw at: 266x160
OX = (W - LW * SCALE) // 2        # 1 px of black either side (800 isn't a multiple of 3)

BG = (0, 5, 2)
FAINT = (10, 42, 20)
DIM = (28, 110, 52)
GREEN = (90, 255, 130)
AMBER = (255, 184, 40)
RED = (255, 52, 36)

# ---- 5x7 pixel font: 35 characters of . and # per glyph, row by row ----

GLYPHS = {
    "0": ".###.#...##..###.#.###..##...#.###.", "1": "..#...##....#....#....#....#...###.",
    "2": ".###.#...#....#...#...#...#...#####", "3": "#####...#...#.....#.....##...#.###.",
    "4": "...#...##..#.#.#..#.#####...#....#.", "5": "######....####.....#....##...#.###.",
    "6": "..##..#...#....####.#...##...#.###.", "7": "#####....#...#...#...#....#....#...",
    "8": ".###.#...##...#.###.#...##...#.###.", "9": ".###.#...##...#.####....#...#..##..",
    "A": ".###.#...##...#######...##...##...#", "B": "####.#...##...#####.#...##...#####.",
    "C": ".###.#...##....#....#....#...#.###.", "D": "###..#..#.#...##...##...##..#.###..",
    "E": "######....#....####.#....#....#####", "F": "######....#....####.#....#....#....",
    "G": ".###.#...##....#.####...##...#.####", "H": "#...##...##...#######...##...##...#",
    "I": ".###...#....#....#....#....#...###.", "J": "..###...#....#....#....#.#..#..##..",
    "K": "#...##..#.#.#..##...#.#..#..#.#...#", "L": "#....#....#....#....#....#....#####",
    "M": "#...###.###.#.##.#.##...##...##...#", "N": "#...##...###..##.#.##..###...##...#",
    "O": ".###.#...##...##...##...##...#.###.", "P": "####.#...##...#####.#....#....#....",
    "Q": ".###.#...##...##...##.#.##..#..##.#", "R": "####.#...##...#####.#.#..#..#.#...#",
    "S": ".#####....#.....###.....#....#####.", "T": "#####..#....#....#....#....#....#..",
    "U": "#...##...##...##...##...##...#.###.", "V": "#...##...##...##...##...#.#.#...#..",
    "W": "#...##...##...##.#.##.#.##.#..#.#..", "X": "#...##...#.#.#...#...#.#.#...##...#",
    "Y": "#...##...#.#.#...#....#....#....#..", "Z": "#####....#...#...#...#...#....#####",
    " ": "." * 35, ".": "." * 25 + ".##...##..", ",": "." * 20 + ".##....#...#...",
    ":": "......##...##........##...##.......", "-": "...............#####...............",
    "/": ".........#...#...#...#...#.........", "%": "##...##..#...#...#...#...#..##...##",
    "°": ".##..#..#.#..#..##.." + "." * 15, "(": "...#...#...#....#....#.....#.....#.",
    ")": ".#.....#.....#....#....#...#...#...", '"': ".#.#..#.#." + "." * 25,
    "'": "..#....#.." + "." * 25, "!": "..#....#....#....#....#.........#..",
    "?": ".###.#...#....#...#...#.........#..", "+": ".......#....#..#####..#....#.......",
    "=": "..........#####.....#####..........", ">": ".#.....#.....#.....#...#...#...#...",
    "<": "...#...#...#...#.....#.....#.....#.", "_": "." * 30 + "#####",
    "[": ".###..#....#....#....#....#....###.", "]": ".###....#....#....#....#....#..###.",
}
GLYPHS.update({
    'a': '...........###.....#.#####...#.####',
    'b': '#....#....#.##.##..##...##...#####.',
    'c': '...........###.#....#....#...#.###.',
    'd': '....#....#.##.##..###...##...#.####',
    'e': '...........###.#...#######.....###.',
    'f': '..##..#..#.#...###...#....#....#...',
    'g': '......#####...##...#.####....#.###.',
    'h': '#....#....#.##.##..##...##...##...#',
    'i': '..#........##....#....#....#...###.',
    'j': '...#........##....#....#.#..#..##..',
    'k': '#....#....#..#.#.#..##...#.#..#..#.',
    'l': '.##....#....#....#....#....#...###.',
    'm': '..........##.#.#.#.##.#.##...##...#',
    'n': '..........#.##.##..##...##...##...#',
    'o': '...........###.#...##...##...#.###.',
    'p': '..........####.#...#####.#....#....',
    'q': '...........##.##..##.####....#....#',
    'r': '..........#.##.##..##....#....#....',
    's': '...........###.#.....###.....#####.',
    't': '.#....#...###...#....#....#..#..##.',
    'u': '..........#...##...##...##..##.##.#',
    'v': '..........#...##...##...#.#.#...#..',
    'w': '..........#...##...##.#.##.#.#.#.#.',
    'x': '..........#...#.#.#...#...#.#.#...#',
    'y': '..........#...##...#.####....#.###.',
    'z': '..........#####...#...#...#...#####',
    '@': '.###.#...##.####.#.##.####.....###.',
    '#': '.#.#..#.#.#####.#.#.#####.#.#..#.#.',
    '$': '..#...#####.#...###...#.#####...#..',
    '^': '..#...#.#.#...#....................',
    '&': '.##..#..#.#.#...#...#.#.##..#..##.#',
    '*': '.......#..#.#.#.###.#.#.#..#.......',
    '{': '...#...#....#...#.....#....#.....#.',
    '}': '.#.....#....#.....#...#....#...#...',
    ';': '......##...##........##....#...#...',
    '\\': '.....#.....#.....#.....#.....#.....',
    '|': '..#....#....#....#....#....#....#..',
    '~': '...........#...#.#.#...#...........',
    '`': '.#.....#...........................',
})
SUBST = {"–": "-", "—": "-", "…": "...", "“": '"', "”": '"', "‘": "'", "’": "'", "·": "-"}
_glyphs = {}


def glyph(ch, scale):
    key = (ch, scale)
    if key not in _glyphs:
        bits = GLYPHS.get(ch) or GLYPHS.get(ch.upper()) or GLYPHS["?"]
        g = Image.new("L", (5, 7))
        g.putdata([255 if b == "#" else 0 for b in bits])
        _glyphs[key] = g.resize((5 * scale, 7 * scale), Image.NEAREST)
    return _glyphs[key]


def text_width(s, scale=1):
    return max(0, len(s) * 6 * scale - scale)


def plain_char(c):
    """A character the font has: é -> e, ñ -> n (song titles), else as is ("?" box)."""
    c = SUBST.get(c, c)
    if c in GLYPHS or c.upper() in GLYPHS:
        return c
    base = unicodedata.normalize("NFKD", c)[:1]
    return base if base in GLYPHS or base.upper() in GLYPHS else c


def text(img, x, y, s, colour, scale=1, align="l", upper=True):
    """Pixel-font text, top-left at (x, y); align l/c/r around x.
    upper=False keeps case (network names, passwords, answers)."""
    s = "".join(plain_char(c) for c in str(s))
    if upper:
        s = s.upper()
    if align == "c":
        x -= text_width(s, scale) // 2
    elif align == "r":
        x -= text_width(s, scale)
    for c in s:
        if c != " ":
            g = glyph(c, scale)
            img.paste(colour, (int(x), int(y), int(x) + g.width, int(y) + g.height), g)
        x += 6 * scale


def blink():
    return int(time.time() * 2) % 2 == 0


def button(img, d, rect, label, colour=GREEN, pressed=False, scale=1, upper=True):
    """Outlined button; filled while pressed so a tap is visibly felt."""
    x0, y0, x1, y1 = rect
    if pressed:
        d.rectangle(rect, fill=colour)
    else:
        d.rectangle(rect, outline=colour)
    text(img, (x0 + x1 + 1) // 2, (y0 + y1 + 2) // 2 - 7 * scale // 2, label,
         BG if pressed else colour, scale=scale, align="c", upper=upper)


def inside(rect, x, y):
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


# Colour schemes: everything is drawn in green (all shades), amber and red. A scheme
# recolours the finished frame by colour family, keeping each pixel's brightness:
# greens -> the scheme's main colour, ambers -> its highlight, reds stay red (a
# plain 3x3 colour matrix turned the dim greens olive). screen.py sets it.
SCHEMES = {
    "green": None,
    "amber": {"main": (255, 168, 0), "highlight": (255, 236, 186), "red": (255, 48, 48)},
}
_scheme = None


def set_scheme(name):
    global _scheme
    s = SCHEMES.get(name)
    _scheme = {k: np.array(v, dtype=np.float32) / 255 for k, v in s.items()} if s else None


def recolour(a):
    """uint8 HxWx3 -> same, in the current scheme."""
    if _scheme is None:
        return a
    f = a.astype(np.float32)
    r, g = f[..., 0], f[..., 1]
    greenish = (g >= r)[..., None]
    # among the warm colours, amber has g/r ~0.72 and red ~0.2: blend between them
    t = np.clip((g / np.maximum(r, 1) - 0.2) / 0.52, 0, 1)[..., None]
    warm = r[..., None] * (t * _scheme["highlight"] + (1 - t) * _scheme["red"])
    out = np.where(greenish, g[..., None] * _scheme["main"], warm)
    return np.clip(out, 0, 255).astype(np.uint8)


def overlay(out, img, x, y, scale=2, scanline=13):
    """Lay a drawing over a finished panel array at panel pixel (x, y), each drawn pixel
    scale x scale: finer than the rest of the screen (the TERM page's small text).
    scanline/16 = how bright the last row of each pixel stays (CRT look, lighter than
    to_panel's 9/16 since at 2x every other row is one)."""
    a = recolour(np.asarray(img, dtype=np.uint8)).repeat(scale, axis=0).repeat(scale, axis=1)
    a[scale - 1::scale] = (a[scale - 1::scale].astype(np.uint16) * scanline // 16).astype(np.uint8)
    h, w = min(a.shape[0], H - y), min(a.shape[1], W - x)
    out[y:y + h, x:x + w] = a[:h, :w]
    return out


def to_panel(img):
    """266x160 drawing -> 800x480 RGB array with CRT scanlines."""
    a = recolour(np.asarray(img, dtype=np.uint8)).repeat(SCALE, axis=0).repeat(SCALE, axis=1)
    a[SCALE - 1::SCALE] = (a[SCALE - 1::SCALE].astype(np.uint16) * 9 // 16).astype(np.uint8)
    out = np.zeros((H, W, 3), dtype=np.uint8)
    out[:, OX:OX + a.shape[1]] = a
    return out
