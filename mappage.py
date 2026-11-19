"""MAP page: live moving map in the retro vector style.

Roads come from roadmap.py's offline database (OpenStreetMap, OR + WA). The map
turns so the way we're driving is up (north-up until we've moved), the car is
an arrow low in the middle, this trip's track trails behind it in amber. Zoom
follows speed until you zoom yourself: +/- buttons or two-finger pinch (A puts
it back on auto). One finger drags the map; ME (or 30 s untouched while
driving) brings it back to the car. Smaller streets fade out as the map widens
rather than vanishing in one go (`max_class`, `style`). GO opens the
destination picker (nav.py); while navigating, the route is a thick amber line,
the next turn shows in a banner across the top (X ends it) and distance / time /
arrival replace the scale bar. Fetching roads
runs in a thread, so a slow SD card never stalls the screen: it keeps drawing
from the last batch until the new one is ready.
"""
import math
import threading
import time

import numpy as np
from PIL import Image, ImageDraw

import roadmap
from pixels import AMBER, BG, DIM, FAINT, GREEN, LH, LW, RED, blink, text, text_width

TOP = 19                        # same as display.TOP (below the tab bar)
LEVELS_M = [300, 600, 1200, 2400, 4800, 9600, 19200, 38400]   # road fetches are per level
MIN_WIDTH, MAX_WIDTH = 150, 40000                         # map width in metres, either way
CAR_Y = TOP + round((LH - TOP) * 0.68)                   # car sits low: more road ahead
MID = (55, 185, 90)             # between DIM and GREEN, for middling roads
RECENTER_SECS = 30              # dragged away while driving: back to the car after this


def max_class(width):
    """Smallest road class worth drawing at this map width. Residential streets stay
    until ~3 km across (still ~9 px apart in a city grid), tertiary to 6 km, ..."""
    return 5 if width <= 3200 else 4 if width <= 6500 else 3 if width <= 13000 else 2 if width <= 26000 else 1


def style(cls, width):
    """(colour, line width) for a road class, fading the small ones as the map widens."""
    if cls <= 1:
        return GREEN, 2 if width <= 13000 else 1
    if cls <= 3:
        return (GREEN if width <= 6500 else MID), 1
    if cls == 4:
        return (MID if width <= 2500 else DIM), 1
    return (DIM if width <= 1500 else FAINT), 1


def level(width):
    """Fetch level for a width: the first LEVELS_M at least that wide."""
    return next((i for i, w in enumerate(LEVELS_M) if w >= width), len(LEVELS_M) - 1)
ZOOM_IN = (LW - 21, TOP + 18, LW - 3, TOP + 36)
ZOOM_OUT = (LW - 21, TOP + 39, LW - 3, TOP + 57)
ZOOM_AUTO = (LW - 21, TOP + 60, LW - 3, TOP + 72)
GO_BTN = (LW - 25, TOP + 78, LW - 3, TOP + 98)           # navigate: opens the destination picker
ME_BTN = (LW - 25, TOP + 102, LW - 3, TOP + 118)         # after dragging: back to the car
END_BTN = (LW - 21, TOP + 4, LW - 3, TOP + 22)           # in the nav banner: stop navigating
BANNER_H = 26
M_PER_DEG = 111320


ABBREV = {"north": "N", "south": "S", "east": "E", "west": "W", "northeast": "NE",
          "northwest": "NW", "southeast": "SE", "southwest": "SW", "avenue": "Ave",
          "street": "St", "boulevard": "Blvd", "road": "Rd", "drive": "Dr", "highway": "Hwy",
          "parkway": "Pkwy", "place": "Pl", "court": "Ct", "lane": "Ln", "terrace": "Ter",
          "circle": "Cir", "freeway": "Fwy", "expressway": "Expy", "way": "Way"}


def short_name(name):
    """'Southeast 12th Avenue' -> 'SE 12th Ave' (fits the screen; OSM spells it all out)."""
    return " ".join(ABBREV.get(w.lower(), w) for w in name.split())


def metres(a, b):
    """Rough distance between two (lat, lon) points, fine for short hops."""
    kx = M_PER_DEG * math.cos(math.radians(a[0]))
    return math.hypot((b[1] - a[1]) * kx, (b[0] - a[0]) * M_PER_DEG)


# Auto zoom: (km/h, metres across) - town streets slow, more road ahead on the
# highway. In between it slides smoothly (evenly in zoom steps), not in jumps.
AUTO_ZOOM = ((25, 600), (50, 1200), (80, 2400), (115, 4800))
ZOOM_HOLD = 1.2                 # ignore speed changes that would zoom by less than this factor
ZOOM_EASE_SECS = 1.5            # then glide to the new width (~4 s to settle)


def auto_width(kmh):
    """Map width for a speed: town streets slow, more road ahead on the highway."""
    kmh = kmh or 0
    pts = AUTO_ZOOM
    if kmh <= pts[0][0]:
        return pts[0][1]
    for (k0, w0), (k1, w1) in zip(pts, pts[1:]):
        if kmh <= k1:
            return w0 * (w1 / w0) ** ((kmh - k0) / (k1 - k0))
    return pts[-1][1]


def scale_bar(width_m, mph):
    """(bar length in metres, label) about a quarter of the map wide."""
    want = width_m / 4
    if mph:
        feet = want / 0.3048
        if feet < 1000:
            n = min((100, 200, 300, 500), key=lambda v: abs(v - feet))
            return n * 0.3048, f"{n} ft"
        miles = want / 1609.344
        n = min((0.25, 0.5, 1, 2, 5), key=lambda v: abs(v - miles))
        return n * 1609.344, f"{n:g} mi"
    n = min((50, 100, 200, 500, 1000, 2000, 5000), key=lambda v: abs(v - want))
    return n, f"{n} m" if n < 1000 else f"{n // 1000} km"


HEADING_EASE_SECS = 0.3


class MapView:
    def __init__(self, roads=None):
        self.roads = roads or roadmap.Roads()
        self.manual_width = None      # metres across, once zoomed by hand; None = by speed
        self.width = auto_width(0)    # what the last frame used
        self.zoom_target = None       # auto zoom: the width it's gliding to
        self.zoomed_at = None         # monotonic time of the last auto-zoom step
        self.pan = (0.0, 0.0)         # view centre, metres east/north of the car (dragging)
        self.touched_at = 0.0
        self.gesture_pts = None       # finger positions last frame, while a gesture is on
        self.heading = 0.0            # degrees, map turned so this is up
        self.cache = None             # roads near the last fetch point, see _load
        self.loading = False
        self.lock = threading.Lock()
        self.road_name = None
        self.name_at = 0
        self.nav = None               # nav.Navigator (display.py sets it)
        self.pos = None

    # ---- controls ----

    def zoom(self, steps):
        """+1 = twice as wide (out), -1 = half (in)."""
        self.manual_width = min(MAX_WIDTH, max(MIN_WIDTH, self.width * 2 ** steps))
        self.width = self.manual_width

    def auto(self):
        self.manual_width = None

    def recenter(self):
        self.pan = (0.0, 0.0)

    @property
    def panned(self):
        return self.pan != (0.0, 0.0)

    def width_for(self, kmh):
        return self.manual_width or auto_width(kmh)

    def eased_width(self, kmh, now=None):
        """This frame's width. By hand: exactly that. Auto: a new target only once the
        speed asks for ZOOM_HOLD x more or less (so a wobbling speed never pumps the
        zoom), then an even glide towards it in zoom terms, as after tapping A."""
        now = time.monotonic() if now is None else now
        dt = 0.0 if self.zoomed_at is None else min(now - self.zoomed_at, 1.0)
        self.zoomed_at = now
        if self.manual_width:
            self.zoom_target = None
            return self.manual_width
        want = auto_width(kmh)
        if self.zoom_target is None or abs(math.log(want / self.zoom_target)) > math.log(ZOOM_HOLD):
            self.zoom_target = want
        step = math.log(self.zoom_target / self.width) * (1 - math.exp(-dt / ZOOM_EASE_SECS))
        width = self.width * math.exp(step)
        return self.zoom_target if abs(math.log(self.zoom_target / width)) < 0.002 else width

    def to_world(self, dx, dy, width=None):
        """A screen offset (drawing pixels) -> metres east/north, in the turned map."""
        mpp = (width or self.width) / LW
        rx, ry = dx * mpp, -dy * mpp
        h = math.radians(self.heading)
        return rx * math.cos(h) + ry * math.sin(h), -rx * math.sin(h) + ry * math.cos(h)

    def gesture(self, points, now=None):
        """Fingers on the map (drawing coords; [] = none). One drags, two pinch-zoom
        around the point between them. Called every frame while touching."""
        now = now or time.time()
        if not points:
            self.gesture_pts = None
            return
        prev, self.gesture_pts = self.gesture_pts, list(points)
        self.touched_at = now
        if prev is None or len(prev) != len(points):     # a finger came or went: start over
            return
        anchor = (LW / 2, CAR_Y)
        if len(points) == 1:
            e, n = self.to_world(points[0][0] - prev[0][0], points[0][1] - prev[0][1])
            self.pan = (self.pan[0] - e, self.pan[1] - n)    # the map follows the finger
            return
        (a0, b0), (a1, b1) = prev[:2], points[:2]
        d0 = math.dist(a0, b0)
        d1 = math.dist(a1, b1)
        if d0 < 3 or d1 < 3:
            return
        mid0 = ((a0[0] + b0[0]) / 2, (a0[1] + b0[1]) / 2)
        mid1 = ((a1[0] + b1[0]) / 2, (a1[1] + b1[1]) / 2)
        # the spot under the fingers' midpoint stays under it
        e0, n0 = self.to_world(mid0[0] - anchor[0], mid0[1] - anchor[1])
        spot = (self.pan[0] + e0, self.pan[1] + n0)
        new = min(MAX_WIDTH, max(MIN_WIDTH, self.width * d0 / d1))
        self.manual_width = self.width = new
        e1, n1 = self.to_world(mid1[0] - anchor[0], mid1[1] - anchor[1], new)
        self.pan = (spot[0] - e1, spot[1] - n1)

    # ---- road fetching ----

    def _want(self, lat, lon, lvl, cls):
        """Fetch if we have nothing, the detail changed, or the view left the cached patch."""
        c = self.cache
        if c is None or c["level"] != lvl or c["max_cls"] != cls:
            return True
        dx = (lon - c["lon"]) * M_PER_DEG * math.cos(math.radians(lat))
        dy = (lat - c["lat"]) * M_PER_DEG
        return math.hypot(dx, dy) > LEVELS_M[lvl] * 0.5

    def _load(self, lat, lon, lvl, cls):
        try:
            found = self.roads.near(lat, lon, LEVELS_M[lvl] * 1.3, cls)
            kx = M_PER_DEG * math.cos(math.radians(lat))
            # one array of points (metres east/north of the fetch point) + where each road starts
            found.sort(key=lambda r: -r[0])          # small roads first, big ones on top
            pts = [np.array(p, dtype=np.float64) for _, _, p in found]
            if pts:
                allp = np.concatenate(pts)
                en = np.column_stack(((allp[:, 1] - lon) * kx, (allp[:, 0] - lat) * M_PER_DEG))
                starts = np.cumsum([0] + [len(p) for p in pts[:-1]])
            else:
                en, starts = np.zeros((0, 2)), np.zeros(0, dtype=int)
            cache = {"lat": lat, "lon": lon, "kx": kx, "level": lvl, "max_cls": cls, "en": en,
                     "starts": starts, "cls": [r[0] for r in found], "n": len(found)}
            with self.lock:
                self.cache = cache
        finally:
            self.loading = False

    def _fetch(self, lat, lon, width):
        lvl, cls = level(width), max_class(width)
        if not self.loading and self._want(lat, lon, lvl, cls):
            self.loading = True
            threading.Thread(target=self._load, args=(lat, lon, lvl, cls), daemon=True).start()

    def _name(self, lat, lon, course):
        """Road we're on, looked up at most once a second (small R-tree query)."""
        now = time.time()
        if now - self.name_at >= 1:
            self.name_at = now
            try:
                self.road_name = roadmap.nearest_name(self.roads.near(lat, lon, 40), lat, lon, course)
            except Exception:
                self.road_name = None
        return self.road_name

    # ---- drawing ----

    def draw(self, img, d, fix, track, mph=True, lit=lambda name: False):
        """fix = latest GPS ({} = none); track = [(lat, lon), ...] oldest first."""
        layer = Image.new("RGB", (LW, LH), BG)
        if self._draw_map(layer, ImageDraw.Draw(layer), fix, track):
            img.paste(layer.crop((0, TOP + 1, LW, LH)), (0, TOP + 1))
            self._overlay(img, d, fix, self.pos[0], self.pos[1], self.width, mph, lit)
        else:
            text(img, LW // 2, 80, "WAITING FOR GPS", DIM, align="c")

    def _turn_to(self, course):
        """Swing the map towards the GPS course over ~0.3 s instead of jumping
        once a second when a new fix comes in."""
        now = time.monotonic()
        dt = min(now - getattr(self, "turned_at", now), 1.0)
        self.turned_at = now
        diff = (course - self.heading + 180) % 360 - 180
        self.heading = (self.heading + diff * (1 - math.exp(-dt / HEADING_EASE_SECS))) % 360

    def _draw_map(self, img, d, fix, track):
        """Roads, track and car onto img. False if we don't know where we are."""
        if fix:
            lat, lon = fix["lat"], fix["lon"]
            kmh = fix.get("speed_kmh") or 0
            if kmh >= 5 and fix.get("course_deg") is not None:
                self._turn_to(float(fix["course_deg"]))
        elif track:
            lat, lon = track[-1]
            kmh = 0
        else:
            return False
        now = time.time()
        if self.panned and kmh > 10 and self.gesture_pts is None and now - self.touched_at > RECENTER_SECS:
            self.recenter()
        width = self.eased_width(kmh)
        self.pos, self.width = (lat, lon), width
        mpp = width / LW
        h = math.radians(self.heading)
        cos_h, sin_h = math.cos(h), math.sin(h)
        cx = LW / 2
        pe, pn = self.pan
        kx = M_PER_DEG * math.cos(math.radians(lat))

        def to_screen(east, north):
            """Metres east/north of the car -> drawing pixels (view centre at (cx, CAR_Y))."""
            east, north = east - pe, north - pn
            rx = east * cos_h - north * sin_h
            ry = east * sin_h + north * cos_h
            return cx + rx / mpp, CAR_Y - ry / mpp

        if self.roads.available:
            self._fetch(lat + pn / M_PER_DEG, lon + pe / kx, width)     # around what's on screen
        with self.lock:
            c = self.cache
        if c is not None and c["n"]:
            # car relative to the fetch point
            ce = (lon - c["lon"]) * c["kx"]
            cn = (lat - c["lat"]) * M_PER_DEG
            sx, sy = to_screen(c["en"][:, 0] - ce, c["en"][:, 1] - cn)
            st = c["starts"]
            # skip pieces entirely off screen without looking at them one by one
            on = ((np.maximum.reduceat(sx, st) >= 0) & (np.minimum.reduceat(sx, st) <= LW)
                  & (np.maximum.reduceat(sy, st) >= TOP) & (np.minimum.reduceat(sy, st) <= LH))
            ends = np.append(st[1:], len(sx))
            limit = max_class(width)
            for i in np.flatnonzero(on):
                cls = c["cls"][i]
                if cls > limit and cls != roadmap.RIVER:  # a wider cache from before zooming out
                    continue
                x, y = sx[st[i]:ends[i]], sy[st[i]:ends[i]]
                if cls == roadmap.RIVER:              # rivers: dots at their points
                    for px, py in zip(x.tolist(), y.tolist()):
                        d.point((px, py), fill=DIM)
                    continue
                col, w = style(cls, width)
                d.line(list(zip(x.tolist(), y.tolist())), fill=col, width=w)
        # the route ahead (amber, thick); this trip's track (amber, or dim under a route)
        route = self.nav.route if self.nav and self.nav.active else None
        if len(track) > 1:
            tr = np.array(track)
            tx, ty = to_screen((tr[:, 1] - lon) * kx, (tr[:, 0] - lat) * M_PER_DEG)
            d.line(list(zip(tx.tolist(), ty.tolist())), fill=DIM if route else AMBER, width=1)
        if route:
            start = self.nav.progress["index"] if self.nav.progress and "index" in self.nav.progress else 0
            sh = np.array(route["shape"][start:])
            if len(sh) > 1:
                rx, ry = to_screen((sh[:, 1] - lon) * kx, (sh[:, 0] - lat) * M_PER_DEG)
                d.line(list(zip(rx.tolist(), ry.tolist())), fill=AMBER, width=3)
            dx, dy = to_screen((self.nav.dest["lon"] - lon) * kx, (self.nav.dest["lat"] - lat) * M_PER_DEG)
            d.ellipse([dx - 3, dy - 3, dx + 3, dy + 3], outline=RED)        # the destination
        # the car: an arrow pointing the way we're going (the middle unless dragged away)
        col = AMBER if fix else RED
        ax, ay = to_screen(0, 0)
        d.polygon([(ax, ay - 6), (ax + 4, ay + 4), (ax, ay + 1), (ax - 4, ay + 4)], fill=col)
        return True

    # ---- overlay: banner, buttons, scale ----

    def buttons(self):
        """[(rect, label, name)] for the right-hand buttons (lower while the nav banner shows)."""
        navigating = bool(self.nav and self.nav.active)
        dy = BANNER_H if navigating else 0
        out = [(_shift(ZOOM_IN, dy), "+", "zoom_in"), (_shift(ZOOM_OUT, dy), "-", "zoom_out")]
        if self.manual_width:
            out.append((_shift(ZOOM_AUTO, dy), "A", "zoom_auto"))
        if self.nav and not navigating:
            out.append((GO_BTN, "GO", "go"))
        if navigating:
            out.append((END_BTN, "X", "end"))
        if self.panned:
            out.append((ME_BTN, "ME", "me"))
        return out

    def _overlay(self, img, d, fix, lat, lon, width, mph, lit):
        navigating = bool(self.nav and self.nav.active)
        if navigating:
            self._banner(img, d, mph)
        else:
            # road name, top left on a dark strip
            name = self._name(lat, lon, fix.get("course_deg") if fix else None) if self.roads.available else None
            if name:
                s = short_name(name)[:38]
                d.rectangle([0, TOP + 1, text_width(s) + 5, TOP + 10], fill=BG)
                text(img, 3, TOP + 2, s, GREEN)
        # north pointer, top right of the map (under the banner when navigating)
        nx, ny = LW - 12, TOP + 9 + (BANNER_H if navigating else 0)
        a = math.radians(-self.heading)
        tip = (nx + 6 * math.sin(a), ny - 6 * math.cos(a))
        if not navigating:
            d.line([(nx, ny), tip], fill=DIM)
            text(img, round(tip[0]) - 2, round(tip[1]) - 3, "N", DIM)
        for rect, label, name in self.buttons():
            on = lit(name)
            col = AMBER if name in ("zoom_auto", "go", "me") else RED if name == "end" else GREEN
            d.rectangle(rect, fill=col if on else BG, outline=DIM if name.startswith("zoom") else col)
            text(img, (rect[0] + rect[2] + 1) // 2, (rect[1] + rect[3]) // 2 - 3, label,
                 BG if on else col, align="c")
        if navigating and self.nav.progress and "left_m" in self.nav.progress:
            p = self.nav.progress                 # bottom left: distance, time, arrival
            eta = time.strftime("%H:%M", time.localtime(time.time() + p["left_s"]))
            s = f"{nav_distance(p['left_m'], mph)}  {round(p['left_s'] / 60)} min  {eta}"
            d.rectangle([0, LH - 10, text_width(s) + 5, LH - 1], fill=BG)
            text(img, 3, LH - 8, s, AMBER, upper=False)
        else:
            # scale bar, bottom left
            metres, label = scale_bar(width, mph)
            px = metres / (width / LW)
            y = LH - 4
            d.rectangle([2, y - 9, max(px, text_width(label)) + 6, LH - 1], fill=BG)
            d.line([(4, y), (4 + px, y)], fill=DIM)
            d.line([(4, y - 2), (4, y)], fill=DIM)
            d.line([(4 + px, y - 2), (4 + px, y)], fill=DIM)
            text(img, 6, y - 9, label, DIM, upper=False)
        if not self.roads.available:
            text(img, LW // 2, LH - 20, "NO MAP DATA", RED, align="c")
        elif not fix:
            text(img, LW // 2, LH - 20, "NO GPS FIX", RED, align="c")

    def _banner(self, img, d, mph):
        """Next turn across the top: arrow, distance, street. Or routing status / arrival."""
        nav = self.nav
        y0, y1 = TOP + 1, TOP + BANNER_H
        d.rectangle([0, y0, LW, y1], fill=BG)
        d.line([0, y1, LW, y1], fill=DIM)
        p = nav.progress or {}
        if p.get("arrived"):
            turn_icon(d, 13, (y0 + y1) // 2, "arrive", AMBER)
            text(img, 28, y0 + 3, "ARRIVED", AMBER, scale=2)
            text(img, 28, y0 + 19, nav.dest["name"][:36], GREEN, upper=False)
            return
        if nav.status or "step" not in p:
            text(img, 6, y0 + 5, nav.status or "ROUTING...", RED if "NO " in nav.status or "ERROR" in nav.status
                 else AMBER if blink() else DIM, scale=2)
            text(img, 6, y0 + 20, f"to {nav.dest['name']}"[:44], DIM, upper=False)
            return
        step = p["step"]
        turn_icon(d, 13, (y0 + y1) // 2, step["turn"], AMBER)
        text(img, 28, y0 + 3, nav_distance(p["to_step"], mph), AMBER, scale=2, upper=False)
        street = step["street"] or step["instruction"]
        text(img, 28, y0 + 19, short_name(street)[:38], GREEN)

    def button_at(self, x, y):
        return next((name for rect, _, name in self.buttons()
                     if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]), None)

    def tap(self, x, y):
        """Returns the button name hit (for the press flash), or None. "go" = open the
        destination picker (display.py does that)."""
        for rect, _, name in self.buttons():
            if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                if name == "zoom_in":
                    self.zoom(-1)
                elif name == "zoom_out":
                    self.zoom(1)
                elif name == "zoom_auto":
                    self.auto()
                elif name == "end":
                    self.nav.stop()
                elif name == "me":
                    self.recenter()
                return name
        return None


def _shift(rect, dy):
    return rect[0], rect[1] + dy, rect[2], rect[3] + dy


def nav_distance(m, mph):
    from nav import nice_distance
    return nice_distance(m, mph)


def turn_icon(d, cx, cy, turn, col):
    """Arrow for the next maneuver: turn = degrees (+ right), or "arrive" / "start"."""
    if turn == "arrive":
        d.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], outline=col)
        d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=col)
        return
    if turn == "start":
        turn = 0
    if abs(turn) >= 180:                     # U-turn: up, over, down
        s = 1 if turn > 0 else -1
        d.line([(cx - 4 * s, cy + 9), (cx - 4 * s, cy - 4)], fill=col, width=3)
        d.arc([cx - 4 * s - 4 if s > 0 else cx, cy - 9, cx + 4 if s > 0 else cx + 8, cy + 1], 180, 360, fill=col, width=3)
        hx, hy = cx + 4 * s, cy + 2
        d.polygon([(hx - 4, hy), (hx + 4, hy), (hx, hy + 6)], fill=col)
        return
    d.line([(cx, cy + 10), (cx, cy)], fill=col, width=3)            # where we come from
    a = math.radians(turn)
    ex, ey = cx + 8 * math.sin(a), cy - 8 * math.cos(a)
    d.line([(cx, cy), (ex, ey)], fill=col, width=3)
    # arrow head at the end, pointing along the new direction
    hx, hy = ex + 4 * math.sin(a), ey - 4 * math.cos(a)
    lx, ly = ex + 4 * math.sin(a - math.pi / 2), ey - 4 * math.cos(a - math.pi / 2)
    rx, ry = ex + 4 * math.sin(a + math.pi / 2), ey - 4 * math.cos(a + math.pi / 2)
    d.polygon([(hx, hy), (lx, ly), (rx, ry)], fill=col)
