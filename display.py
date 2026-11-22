"""Shows the logger's data on the 5" DSI touchscreen (800x480, capacitive ft5x06).

Runs as its own service (nightrider-display.service) and reads the logger's HTTP
API on this Pi, so a screen problem can never stop logging. A row of tabs is
always along the top: DRIVE (gauges; tap the page or DRIVE again for the other
readings), MUSIC (spotify.py), AI (assistant.py), SET (Wi-Fi
switcher, touch calibration: screens.py, wifi.py) and TERM (terminal.py). Hold the SHUT DOWN box
on the SET page for 4 s to shut the Pi down cleanly (do it before switching the car off).

Look: early-80s vector/CRT ("Escape from New York"). Everything is drawn at
266x160 with a built-in 5x7 pixel font, then tripled to 800x480 with scanlines.

    python3 display.py                          # draw on the LCD
    python3 display.py --png out.png [page]     # render one frame to a file (testing)

Settings (env vars): NIGHTRIDER_URL (default http://127.0.0.1),
NIGHTRIDER_DISPLAY_F=1 for °F, NIGHTRIDER_DISPLAY_MPH=0 for km/h.
"""
import collections
import getpass
import glob
import json
import math
import mmap
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw

import bt
import cell
import gas
import mappage
import nav
import screen
import screens
import spotify
import terminal
import wifi
from pixels import (AMBER, BG, DIM, FAINT, GREEN, H, LH, LW, RED, W, WHITE, YELLOW,  # noqa: F401
                    GLYPHS, SCALE, blink, button, inside, overlay, text, text_width, to_panel)

API = os.environ.get("NIGHTRIDER_URL", "http://127.0.0.1")
FAHRENHEIT = os.environ.get("NIGHTRIDER_DISPLAY_F", "0") == "1"
MPH = os.environ.get("NIGHTRIDER_DISPLAY_MPH", "1") == "1"   # then screen.json "speed_unit" (tap the speed)
METRIC_FUEL = False                # screen.json "fuel_units": L/h and km/L instead of gal/h and MPG
FPS = 4                           # pages that only change now and then
LIVE_FPS = 15                     # gauges and map (the logger samples at 10 Hz, GPS at 1 Hz)
LIVE_PAGES = {"gauges", "more", "map"}
POLL_HZ = 10                      # Feed polls /api/latest and /api/gps this often
AHEAD_SECS = 1.5                  # map: carry the car on from the last fix at most this far
HOLD_FIRE = 4.0                   # seconds holding SHUT DOWN (SET page) before the Pi shuts down
RPM_MAX, RPM_RED = 7000, 6200     # 924S tach


# Same readings and warning limits as the phone app (READINGS in app/app.js) -
# keep the two in step. Limits use raw values (°C, V, ...). Labels are short:
# the screen fits 20 characters per column.
READINGS = {
    "rpm": dict(label="RPM", dp=0, warn=lambda v: v > RPM_RED),
    "speed_kmh": dict(label="SPEED", speed=True, dp=0),
    "gear": dict(label="GEAR", dp=0),
    "coolant_c": dict(label="COOLANT", temp=True, dp=0, warn=lambda v: v > 104, cold=lambda v: v < 60),
    "afr": dict(label="AIR/FUEL", dp=1, warn=lambda v: v < 10.5 or v > 16.5),
    "advance_deg": dict(label="TIMING", unit="°", dp=1),
    "knock_retard_deg": dict(label="KNOCK", unit="°", dp=1, warn=lambda v: v > 0),
    "dme_volts": dict(label="BATTERY", unit="V", dp=1, warn=lambda v: v < 12.3 or v > 15),
    "intake_c": dict(label="INTAKE", temp=True, dp=0),
    "injector_ms": dict(label="INJECTOR", unit="ms", dp=2),
    "throttle": dict(label="THROTTLE"),
    "o2_state": dict(label="O2 SENSOR"),
    "idle_valve_pct": dict(label="IDLE VALVE", unit="%", dp=0),
    "airflow_gs": dict(label="AIRFLOW", unit="g/s", dp=1),
    "lambda": dict(label="LAMBDA", dp=2),
    "o2_volts": dict(label="O2 VOLTS", unit="V", dp=2),
    "dwell_ms": dict(label="DWELL", unit="ms", dp=1),
    "baro_kpa": dict(label="BARO", unit="KPA", dp=0),
    "fuel_pump": dict(label="FUEL PUMP"),
    "ac": dict(label="A/C"),
    "pi_temp_c": dict(label="PI TEMP", temp=True, dp=0, warn=lambda v: v > 75),
    "course_deg": dict(label="HEADING", unit="°", dp=0),
    "alt_m": dict(label="ALTITUDE", unit="m", dp=0),
    "gps_sats": dict(label="SATELLITES", dp=0),
    "lat": dict(label="LATITUDE", dp=5),
    "lon": dict(label="LONGITUDE", dp=5),
    "airflow_cfm": dict(label="AIRFLOW", unit="CFM", dp=1),
    "dme_resets": dict(label="DME RESETS", dp=0),
    "rpm_sensor_errors": dict(label="RPM SNS ERR", dp=0),
    "mark_sensor_errors": dict(label="MARK SNS ERR", dp=0),
    "flywheel_teeth": dict(label="FLYWHEEL", dp=0),
    "no_mark_signal": dict(label="NO MARK SIG"),
    "no_ref_signal": dict(label="NO REF SIG"),
    "mark_swapped": dict(label="MARK SWAP"),
    "ref_swapped": dict(label="REF SWAP"),
}
FAULTS = {"no_mark_signal", "no_ref_signal", "mark_swapped", "ref_swapped"}   # True = sensor problem
ORDER = {k: i for i, k in enumerate(READINGS)}
MAIN_TILES = ["coolant_c", "dme_volts", "advance_deg", "o2_volts", "injector_ms", "intake_c"]
TRACK_POINTS = 20000              # MAP page track: ~200 km at TRACK_STEP_M
TRACK_STEP_M = 10
TRACK_GAP_SECS = 30 * 60          # parked this long = a new trip, track starts again
TRACE_SECS = 60                   # the airflow trace shows the last minute
TRACE_HISTORY = 1200              # samples kept for it (a minute at up to 20 Hz)
PAGES = ["gauges", "more", "map", "spotify", "claude", "status", "terminal"]
# The tabs along the top: (label, the pages it covers). DRIVE flips between its two.
TABS = [("DRIVE", ("gauges", "more")), ("MAP", ("map",)), ("MUSIC", ("spotify",)), ("AI", ("claude",)),
        ("SET", ("status",)), ("TERM", ("terminal",))]
TAB_H = 16                        # tab bar height; taps above TAB_TOUCH count as tabs
TAB_TOUCH = 19
TOP = 19                          # pages draw from here down

# ---- values ----

def label(k):
    return READINGS.get(k, {}).get("label") or k.replace("_", " ")


def unit(k):
    m = READINGS.get(k, {})
    if m.get("temp"):
        return "°F" if FAHRENHEIT else "°C"
    if m.get("speed"):
        return "mph" if MPH else "km/h"
    return m.get("unit", "")


SHORT_VALUES = {"part throttle": "PART", "full load": "FULL", "switching": "SWITCH"}


def fmt(k, v):
    if v is None:
        return "--"
    if isinstance(v, str):
        return SHORT_VALUES.get(v, v)
    if isinstance(v, bool):
        return "YES" if v else "NO"
    if not isinstance(v, (int, float)):
        return str(v)
    m = READINGS.get(k, {})
    if k == "gear" and not v:
        return "-"                # not in a gear (stopped, clutch in, neutral)
    if m.get("temp") and FAHRENHEIT:
        v = v * 9 / 5 + 32
    if m.get("speed") and MPH:
        v = v / 1.609344
    return f"{v:.{m.get('dp', 0 if float(v).is_integer() else 2)}f}"


def colour(k, v, default=GREEN):
    """Warnings blink red; too-cold shows amber."""
    m = READINGS.get(k, {})
    if isinstance(v, bool):
        bad = v and k in FAULTS
    elif isinstance(v, (int, float)):
        if m.get("cold") and m["cold"](v):
            return AMBER
        bad = bool(m.get("warn") and m["warn"](v))
    else:
        bad = False
    if bad:
        return RED if blink() else DIM
    return default


# ---- data from the logger ----

class Feed:
    """Polls the logger's API in the background."""

    def __init__(self):
        self.latest = None
        self.info = None
        self.ok = False
        self.wifi = ""
        self.airflow = collections.deque(maxlen=TRACE_HISTORY)   # (ts, g/s) for the trace
        self.drive_start = {}                              # session -> first ts seen
        self.gps = {}                                      # live fix from /api/gps ({} = none)
        self.gps_at = 0.0                                  # when that fix was taken
        self.track = collections.deque(maxlen=TRACK_POINTS)   # (lat, lon) for the MAP page
        self.track_ts = 0
        self.sessions = []                                 # drives this year, newest first
        self.battery = {}                                  # /api/battery: charge estimate
        self.fuel = {}                                     # /api/fuel: MPG, fuel used
        threading.Thread(target=self._run, daemon=True).start()

    def _get(self, path):
        with urllib.request.urlopen(API + path, timeout=2) as r:
            return json.load(r)

    def _run(self):
        n = 0
        due = time.monotonic()
        while True:
            try:
                latest = self._get("/api/latest")
                if latest and latest != self.latest:
                    self._remember(latest)
                self.latest = latest
                self._gps(self._get("/api/gps"))
                if n % (POLL_HZ // 2) == 0:            # twice a second: the rolling MPG
                    try:
                        self.fuel = self._get("/api/fuel") or {}
                    except Exception:
                        self.fuel = {}
                if n % (5 * POLL_HZ) == 0:
                    self.info = self._get("/api/info")
                    self.wifi = wifi.current()
                if n % (30 * POLL_HZ) == 0:
                    self.sessions = self._this_year()
                    try:
                        self.battery = self._get("/api/battery") or {}
                    except Exception:
                        self.battery = {}
                self.ok = True
            except Exception:
                self.ok = False
            n += 1
            due = max(due + 1 / POLL_HZ, time.monotonic())     # steady rate, no catching up
            time.sleep(max(0.0, due - time.monotonic()))

    def fill(self, gallons):
        """Tell the logger the tank was filled -> the new /api/fuel (None if it failed)."""
        req = urllib.request.Request(API + "/api/fuel/fill", data=json.dumps({"gallons": gallons}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                self.fuel = json.load(r)
            return self.fuel
        except Exception:
            return None

    def _gps(self, fix):
        """Keep the fix, and add to the track every TRACK_STEP_M (a new trip after a long stop)."""
        now = time.time()
        if fix:
            self.gps_at = now - fix.get("age_s", 0)
        self.gps = fix or {}
        if not fix:
            return
        if self.track and now - self.track_ts > TRACK_GAP_SECS:
            self.track.clear()
        p = (fix["lat"], fix["lon"])
        if not self.track or mappage.metres(self.track[-1], p) >= TRACK_STEP_M:
            self.track.append(p)
        if fix.get("speed_kmh"):
            self.track_ts = now

    def _this_year(self):
        """Drives since 1 January, paging back through /api/sessions."""
        year = period_start("year")
        out, before = [], ""
        while True:
            page = self._get(f"/api/sessions?limit=200{before}")
            out += [s for s in page if s["end"] >= year]
            if len(page) < 200 or page[-1]["start"] < year:
                return out
            before = f"&before={page[-1]['id']}"

    def _remember(self, s):
        sess, v = s.get("session"), s.get("data", {}).get("airflow_gs")
        if sess is None:
            return
        if sess not in self.drive_start:
            self.drive_start[sess] = s["ts"]
            self.airflow.clear()
        if isinstance(v, (int, float)):
            self.airflow.append((s["ts"], v))

    def gps_now(self):
        """The fix with the car carried on along its course since it was taken
        (GPS only updates once a second; the map draws much faster)."""
        fix = self.gps
        kmh, course = fix.get("speed_kmh"), fix.get("course_deg")
        if not fix or not kmh or course is None:
            return fix
        dt = min(max(time.time() - self.gps_at, 0.0), AHEAD_SECS)
        m = kmh / 3.6 * dt
        c = math.radians(course)
        out = dict(fix)
        out["lat"] = fix["lat"] + m * math.cos(c) / mappage.M_PER_DEG
        out["lon"] = fix["lon"] + m * math.sin(c) / (mappage.M_PER_DEG * math.cos(math.radians(fix["lat"])))
        return out

    @property
    def engine_on(self):
        s = self.latest
        return bool(s and s.get("session") is not None and time.time() - s["ts"] < 5)


# ---- drive time totals (the box left of the clock on the gauges page) ----

DRIVE_MODES = [("last", "LAST DRIVE"), ("today", "TODAY"), ("week", "THIS WEEK"),
               ("month", "THIS MONTH"), ("year", "THIS YEAR")]
# gauges page layout (drawing pixels)
SPLIT = 150                                # tach/speed left of here, readouts right
FUEL_COL = (94, 21, 148)                   # x0, y0, x1: gear, burn, trip, tank boxes right of the tach
FUEL_ROW_H = 26
SPEED_Y = 99                               # big speed under the tach
FOOTER_Y = LH - 9                          # one small line along the bottom
DRIVE_BOX = (122, FOOTER_Y - 4, 233, LH - 1)   # drawn round the drive time + miles while pressed
DRIVE_TAP = (122, FOOTER_Y - 14, 233, LH - 1)  # tap here to step DRIVE_MODES (taller: finger-sized)
MPG_TAP = (92, 20, SPLIT - 1, 127)         # the whole column opens the GAS screen
SPEED_TAP = (0, 94, 91, 127)               # tap the big speed: mph <-> km/h


def period_start(mode, now=None):
    """Unix time the period began (local time; weeks start on Monday)."""
    t = time.localtime(now)
    if mode == "today":
        day = (t.tm_year, t.tm_mon, t.tm_mday)
    elif mode == "week":
        day = (t.tm_year, t.tm_mon, t.tm_mday - t.tm_wday)        # mktime rolls back across months
    elif mode == "month":
        day = (t.tm_year, t.tm_mon, 1)
    else:
        day = (t.tm_year, 1, 1)
    return time.mktime((*day, 0, 0, 0, 0, 0, -1))


def drive_seconds(sessions, mode, now=None):
    """Last drive's length, or the total driven in the period (drives crossing
    its start count only from the start)."""
    if mode == "last":
        return sessions[0]["end"] - sessions[0]["start"] if sessions else None
    start = period_start(mode, now)
    return sum(max(0, s["end"] - max(s["start"], start)) for s in sessions)


def drive_km(sessions, mode, now=None):
    """Distance to go with drive_seconds: the last drive's, or the period's (a drive
    crossing its start counts the share of it driven after the start). None = unknown."""
    if mode == "last":
        return sessions[0].get("km") if sessions else None
    start = period_start(mode, now)
    total = 0.0
    for s in sessions:
        if s["end"] > start and s.get("km"):
            length = s["end"] - s["start"]
            total += s["km"] * (min(1.0, (s["end"] - start) / length) if length > 0 else 1.0)
    return total


def distance(km):
    """Short distance for the footer: 0.4mi, 12.5mi, 134mi (km/h setting: km)."""
    v = km / 1.609344 if MPH else km
    return (f"{v:.1f}" if v < 100 else f"{v:.0f}") + ("mi" if MPH else "km")


def duration(secs):
    """Lowercase units, biggest first: 45s, 3m, 4h2m, 2d4h1m."""
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return "".join(f"{n}{u}" for n, u in ((d, "d"), (h, "h"), (m, "m")) if n or (u == "m" and not (d or h)))


# ---- pages (all coordinates are in the 240x160 drawing) ----

def tab_of(page):
    return next(i for i, (_, pages) in enumerate(TABS) if PAGES[page] in pages)


def tab_rects():
    """Each tab sized to its label, sharing out the full width."""
    widths = [text_width(name) for name, _ in TABS]
    pad = (LW - (len(TABS) - 1) - sum(widths)) // len(TABS)
    rects, x = [], 0
    for w in widths:
        rects.append((x, 0, x + w + pad - 1, TAB_H))
        x += w + pad + 1
    rects[-1] = rects[-1][:2] + (LW - 1, TAB_H)        # the last one takes the leftover pixels
    return rects


def tab_bar(img, d, feed, page, ui=None):
    """DRIVE / MUSIC / AI / SET / TERM, always on screen."""
    on = tab_of(page)
    for i, ((name, _), rect) in enumerate(zip(TABS, tab_rects())):
        lit = bool(ui and ui.lit(("tab", i)))
        if i == on or lit:
            d.rectangle(rect, fill=GREEN if i == on else DIM)
            c = BG
        else:
            d.rectangle(rect, outline=DIM)
            c = GREEN
        if name == "DRIVE" and not feed.ok and i != on:  # logger not answering
            c = RED if blink() else DIM
        text(img, (rect[0] + rect[2] + 1) // 2, 5, name, c, align="c")


def polar(cx, cy, r, rpm):
    a = math.radians(210 - 240 * max(0, min(rpm, RPM_MAX)) / RPM_MAX)
    return cx + r * math.cos(a), cy - r * math.sin(a)


def tach(img, d, rpm, live, cx=46, cy=60, r=40, caption=True):
    """Analog tach, 0-7000 with a redline, like the 924S dash. caption=False
    leaves out "RPM" so the number can go under the hub."""
    big = r > 55
    main = GREEN if live else DIM
    # outer arc in short segments so it stays chunky after scaling
    for n in range(0, RPM_MAX, 100):
        col = RED if n >= RPM_RED else main
        d.line([polar(cx, cy, r, n), polar(cx, cy, r, n + 100)], fill=col)
        if n >= RPM_RED:
            d.line([polar(cx, cy, r - 2, n), polar(cx, cy, r - 2, n + 100)], fill=col)
    for n in range(0, RPM_MAX + 1, 250):
        col = RED if n >= RPM_RED else main
        if n % 1000 == 0:
            d.line([polar(cx, cy, r, n), polar(cx, cy, r - (8 if big else 6), n)], fill=col, width=2)
            x, y = polar(cx, cy, r - (15 if big else 12), n)
            text(img, round(x) - 2, round(y) - 3, str(n // 1000), col)
        elif n % 500 == 0:
            d.line([polar(cx, cy, r, n), polar(cx, cy, r - (5 if big else 4), n)], fill=col)
        else:
            d.line([polar(cx, cy, r, n), polar(cx, cy, r - 2, n)], fill=FAINT if not live else DIM)
    if caption:
        text(img, cx, cy + (14 if big else 9), "RPM", DIM, align="c")
    if big:
        text(img, cx, cy + 23, "X1000", FAINT if not live else DIM, align="c")
    # needle
    needle = (RED if rpm and rpm > RPM_RED else AMBER) if live else DIM
    d.line([polar(cx, cy, -5, rpm or 0), polar(cx, cy, r - 5, rpm or 0)], fill=needle, width=3 if big else 2)
    d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=needle)
    d.ellipse([cx - 1, cy - 1, cx + 1, cy + 1], fill=BG)


def tile_row(img, d, x0, x1, y, k, v, live, lab=None):
    """One readout row: small label on the left, big value right-aligned to x1
    with its unit after it."""
    text(img, x0, y + 7, lab or label(k), DIM)
    val = fmt(k, v if live else None)
    c = colour(k, v) if live and v is not None else DIM
    u = unit(k) if live and v is not None else ""
    if u:
        text(img, x1, y, u, DIM, align="r", upper=False)
        x1 -= text_width(u) + 1
    text(img, x1, y, val, c, scale=2, align="r")


def battery_row(img, x0, x1, y, pct):
    """Engine off: the battery's estimated charge (battery.py) in the BATTERY row."""
    text(img, x0, y + 7, "BATT EST", DIM)
    text(img, x1, y, "%", DIM, align="r")
    text(img, x1 - text_width("%") - 1, y, f"{pct:.0f}", GREEN if pct >= 50 else AMBER if pct >= 25 else RED,
         scale=2, align="r")


def airflow_trace(img, d, feed, live, data, y=131, x0=4, x1=LW - 5):
    """Airflow over the last minute, scaled to fit what it's seen."""
    by0, by1 = y + 9, y + 18
    v = data.get("airflow_gs") if live else None
    text(img, x0, y, "AIRFLOW", DIM)
    if v is not None:
        val = fmt("airflow_gs", v)
        text(img, x0 + 48, y, val, GREEN)
        text(img, x0 + 48 + text_width(val) + 3, y, "g/s", DIM, upper=False)
    span = TRACE_SECS
    now = feed.airflow[-1][0] if feed.airflow else 0
    recent = [(t, g) for t, g in feed.airflow if now - t <= span] if live else []
    top = max(20, math.ceil(max((g for _, g in recent), default=0) / 20) * 20)   # 20 g/s steps
    text(img, x1 + 1, y, f"0-{top} g/s  1 min", FAINT, align="r", upper=False)
    d.rectangle([x0, by0, x1, by1], outline=FAINT)
    for x in range(x0 + 2, x1, 4):                        # dotted half line
        d.point((x, (by0 + by1) // 2), fill=FAINT)
    pts = [(x1 - (now - t) / span * (x1 - x0), by1 - max(0, min(1, g / top)) * (by1 - by0)) for t, g in recent]
    if len(pts) > 1:
        d.line(pts, fill=GREEN)


GAL_TO_L, MPG_TO_KML = 3.785411784, 0.425143707


def set_units(settings, **kw):
    """speed_unit="mph"/"kmh", fuel_units="imperial"/"metric": change now and save (screen.json)."""
    global MPH, METRIC_FUEL
    if settings is not None:
        settings.change(**kw)
        s = settings.settings
    else:
        s = kw
    if "speed_unit" in s:
        MPH = s["speed_unit"] != "kmh"
    if "fuel_units" in s:
        METRIC_FUEL = s["fuel_units"] == "metric"


def fuel_values(feed, data, live):
    """(burn, trip, tank) as strings in the chosen units, "-" when not known. Burn = fuel
    per hour now: the logger's 10 s figure (older loggers only sent it when stopped)."""
    f = getattr(feed, "fuel", None) or {}
    gph = None
    if live and f.get("now_gph") is not None:
        gph = f["now_gph"]
    elif live and f.get("now_mpg") and data.get("speed_kmh") is not None:     # until the logger restarts
        gph = data["speed_kmh"] / 1.609344 / f["now_mpg"]
    sess = (feed.latest or {}).get("session")
    drive = f.get("drive") or {}
    trip = drive.get("mpg") if drive.get("session") == sess else None
    tank = (f.get("tank") or {}).get("mpg")
    if METRIC_FUEL:
        burn = "-" if gph is None else f"{gph * GAL_TO_L:.1f}"
        econ = lambda m: "-" if m is None else f"{min(m * MPG_TO_KML, 99):.1f}"
    else:
        burn = "-" if gph is None else f"{gph:.2f}"
        econ = lambda m: "-" if m is None else f"{min(m, 99):.0f}" if m >= 10 else f"{m:.1f}"
    return burn, econ(trip), econ(tank)


def fuel_column(img, d, feed, data, live, lit=False):
    """Right of the tach, four boxes: GEAR, fuel per hour, TRIP and TANK economy (yellow).
    Tap: GAS screen."""
    x0, y, x1 = FUEL_COL
    g = data.get("gear") if live and data.get("speed_kmh") is not None else None
    burn, trip, tank = fuel_values(feed, data, live)
    econ = "KM/L" if METRIC_FUEL else "MPG"
    rows = [("GEAR", fmt("gear", g) if g is not None else "-"),
            ("L/H" if METRIC_FUEL else "GAL/H", burn),
            ("TRIP " + econ, trip), ("TANK " + econ, tank)]
    cx = (x0 + x1 + 1) // 2
    for lab, val in rows:
        box = (x0, y, x1, y + FUEL_ROW_H - 1)
        d.rectangle(box, outline=DIM if val != "-" else FAINT, fill=FAINT if lit else None)
        text(img, cx, y + 3, lab, DIM, align="c")
        text(img, cx, y + 11, val, YELLOW if val != "-" else DIM, scale=2, align="c")
        y += FUEL_ROW_H


def big_speed(img, data, live, lit=False):
    """Big GPS speed under the tach, in white; tap toggles mph / km/h."""
    v = data.get("speed_kmh") if live else None
    val = fmt("speed_kmh", v)
    text(img, 4, SPEED_Y, val, WHITE if v is not None else DIM, scale=4)
    text(img, 4 + text_width(val, 4) + 3, SPEED_Y + 21, unit("speed_kmh"), WHITE if lit else DIM, upper=False)


def footer(img, d, feed, data, live, ui):
    """One small line along the bottom: throttle, idle valve, drive time + miles, clock."""
    y = FOOTER_Y
    def item(x, lab, val, col=GREEN, upper=True):
        text(img, x, y, lab, DIM)
        text(img, x + text_width(lab) + 4, y, val, col if val != "--" else DIM, upper=upper)
    item(4, "THR", fmt("throttle", data.get("throttle") if live else None))     # short: room for miles
    item(58, "VALVE", fmt("idle_valve_pct", data.get("idle_valve_pct") if live else None)
         + ("%" if live and data.get("idle_valve_pct") is not None else ""))
    sess = (feed.latest or {}).get("session")
    mode, name = DRIVE_MODES[ui.drive_mode if ui else 0]
    if ui and ui.lit("drive"):
        d.rectangle(DRIVE_BOX, outline=DIM)
    x = DRIVE_BOX[0] + 3
    name = name.replace("THIS ", "").replace(" DRIVE", "")             # short: LAST / TODAY / WEEK ...
    if mode == "last" and live and sess in feed.drive_start:          # driving: the live timer
        secs = int(feed.latest["ts"] - feed.drive_start[sess])
        val = f"{secs // 60}:{secs % 60:02d}"
        item(x, "DRIVE", val)
        drive = (getattr(feed, "fuel", None) or {}).get("drive") or {}   # live miles, twice a second
        km = drive.get("miles", 0) * 1.609344 if drive.get("session") == sess else None
        lab = "DRIVE"
    else:
        secs = drive_seconds(feed.sessions, mode)
        val = "--" if secs is None else duration(secs)
        item(x, name, val, GREEN if secs else DIM, upper=False)
        km = drive_km(feed.sessions, mode) if secs else None
        lab = name
    if km is not None:
        text(img, x + text_width(lab) + 4 + text_width(val) + 5, y, distance(km), AMBER, upper=False)
    text(img, LW - 1, y, time.strftime("%H:%M"), GREEN, align="r")


def layout_classic(img, d, feed, ui=None):
    """The original DRIVE page: tach, gear, MPG, big speed, six readouts, airflow, footer."""
    data = (feed.latest or {}).get("data", {})
    live = feed.engine_on
    rpm = data.get("rpm") if live else None
    tach(img, d, rpm, live, cx=50, cy=60, r=41, caption=False)
    if live and rpm is not None:
        text(img, 50, 79, fmt("rpm", rpm), colour("rpm", rpm), scale=2, align="c")
        text(img, 50 + text_width(fmt("rpm", rpm), 2) // 2 + 3, 86, "RPM", DIM)
    else:
        text(img, 50, 79, "OFF" if feed.ok else "----", DIM, scale=2, align="c")
    fuel_column(img, d, feed, data, live, bool(ui and ui.lit("mpg")))
    big_speed(img, data, live, bool(ui and ui.lit("speed")))
    # six readouts down the right
    d.line([SPLIT, TOP, SPLIT, 127], fill=FAINT)
    for i, k in enumerate(MAIN_TILES):
        y = TOP + 2 + i * 18
        lab = None
        if k == "o2_volts" and live and data.get("o2_state"):        # O2 RICH / O2 LEAN
            lab = f"O2 {fmt('o2_state', data['o2_state'])}"[:9]
        elif k == "injector_ms":
            lab = "INJ"
        if k == "dme_volts" and not live and feed.battery.get("soc_pct") is not None:
            battery_row(img, SPLIT + 5, LW - 3, y, feed.battery["soc_pct"])
        else:
            tile_row(img, d, SPLIT + 5, LW - 3, y, k, data.get(k), live, lab)
        if i < len(MAIN_TILES) - 1:
            d.line([SPLIT + 5, y + 16, LW - 3, y + 16], fill=FAINT)
    d.line([0, 128, LW, 128], fill=FAINT)
    airflow_trace(img, d, feed, live, data)
    footer(img, d, feed, data, live, ui)


# ---- DRIVE page layouts: SET > DRIVE LAYOUT (screen.json "layout") ----

LAYOUTS = [("classic", "CLASSIC", "TACH, GEAR, SPEED, SIX READINGS, AIRFLOW"),
           ("cockpit", "COCKPIT", "ONE BIG TACH, SPEED AND GEAR INSIDE IT"),
           ("bars", "BARS", "SHIFT-LIGHT REV BAR AND BAR METERS"),
           ("map", "MAP", "MOVING MAP WITH SPEED, GEAR AND REVS"),
           ("mapmusic", "MAP + MUSIC", "MAP, GAUGES AND WHAT'S PLAYING")]
# What a tap on each layout's areas does (UI.tap): drive = step the drive-time box,
# mpg = the GAS screen, map = the MAP tab, music = the MUSIC tab; anywhere else flips DRIVE.
MAP_RECT = (0, TOP + 1, 157, LH - 1)
MAPMUSIC_MAP = (0, TOP + 1, 157, 117)
MUSIC_RECT = (0, 119, LW - 1, LH - 1)
SIDE_X = 161                               # the gauge column beside a map
LAYOUT_TAPS = {
    "classic": {"drive": DRIVE_TAP, "mpg": MPG_TAP, "speed": SPEED_TAP},
    "cockpit": {"mpg": (LW - 70, 118, LW - 1, LH - 1)},
    "bars": {"drive": DRIVE_TAP, "mpg": (4, 113, 104, 141)},
    "map": {"map": MAP_RECT, "mpg": (SIDE_X, 118, LW - 1, 140)},
    "mapmusic": {"map": MAPMUSIC_MAP, "music": MUSIC_RECT},
}
# Bar meters: reading -> (empty, full) in raw units (°C, V, ...)
BAR_RANGE = {"coolant_c": (40, 120), "dme_volts": (11, 15), "intake_c": (0, 80),
             "advance_deg": (0, 40), "injector_ms": (0, 8), "o2_volts": (0, 1)}


def drive_layout(ui):
    s = getattr(ui, "screen", None) if ui else None
    key = (s.settings.get("layout") if s else None) or "classic"
    return key if key in LAYOUT_TAPS else "classic"


def rev_bar(img, d, x0, y0, x1, y1, rpm, live, labels=True):
    """Segmented rev counter, 250 rpm a segment: green, amber from 5000, red from the
    redline; the whole bar flashes red past the redline (shift light)."""
    n = RPM_MAX // 250
    w = (x1 - x0 + 2) / n
    shift = live and rpm and rpm > RPM_RED and blink()
    for i in range(n):
        a = x0 + round(i * w)
        b = x0 + round((i + 1) * w) - 2
        at = i * 250
        lit = live and rpm is not None and rpm > at
        col = RED if at >= RPM_RED else AMBER if at >= 5000 else GREEN
        if shift:
            d.rectangle([a, y0, b, y1], fill=RED)
        elif lit:
            d.rectangle([a, y0, b, y1], fill=col)
        else:
            d.rectangle([a, y0, b, y1], outline=FAINT if at < RPM_RED else RED)
    if labels:
        for k in range(1, RPM_MAX // 1000 + 1):
            x = x0 + round(k * 4 * w) - 2
            text(img, x, y1 + 2, str(k), RED if k * 1000 >= RPM_RED else DIM, align="c")


def bar_meter(img, d, x0, x1, y, k, v, live, lab=None, segs=14):
    """Label, a segmented bar for where the reading sits in BAR_RANGE, the value."""
    text(img, x0, y, lab or label(k), DIM)
    val = fmt(k, v if live else None)
    u = unit(k) if live and v is not None else ""
    text(img, x1, y, val + u, colour(k, v) if live and v is not None else DIM, align="r", upper=False)
    lo, hi = BAR_RANGE[k]
    frac = max(0.0, min(1.0, (v - lo) / (hi - lo))) if live and isinstance(v, (int, float)) else 0
    by, bw = y + 9, (x1 - x0 + 2) / segs
    c = colour(k, v) if live and v is not None else DIM
    for i in range(segs):
        a, b = x0 + round(i * bw), x0 + round((i + 1) * bw) - 2
        if (i + 0.5) / segs <= frac:
            d.rectangle([a, by, b, by + 3], fill=c)
        else:
            d.rectangle([a, by, b, by + 3], outline=FAINT)


def mpg_text(feed, live):
    """(label, value) for a compact MPG readout, like mpg_box."""
    f = getattr(feed, "fuel", None) or {}
    if live and f.get("now_mpg") is not None:
        return "MPG", f"{min(f['now_mpg'], 99):.0f}"
    if live and f.get("now_gph") is not None:
        return "GAL/H", f"{f['now_gph']:.1f}"
    tank = (f.get("tank") or {}).get("mpg")
    return "TANK MPG", "--" if tank is None else f"{min(tank, 99):.0f}"


def layout_cockpit(img, d, feed, ui=None):
    """One big tach filling the page; speed, gear and revs inside its ring, like a race dash."""
    data = (feed.latest or {}).get("data", {})
    live = feed.engine_on
    rpm = data.get("rpm") if live else None
    cx, cy, r = LW // 2, 96, 72
    main = GREEN if live else DIM
    for n in range(0, RPM_MAX, 100):                                  # the ring, chunky
        col = RED if n >= RPM_RED else main
        for rr in ((r, r - 1, r - 2) if n >= RPM_RED else (r, r - 1)):
            d.line([polar(cx, cy, rr, n), polar(cx, cy, rr, n + 100)], fill=col)
    for n in range(0, RPM_MAX + 1, 250):
        col = RED if n >= RPM_RED else main
        if n % 1000 == 0:
            d.line([polar(cx, cy, r, n), polar(cx, cy, r - 9, n)], fill=col, width=2)
            x, y = polar(cx, cy, r - 18, n)
            text(img, round(x), round(y) - 6, str(n // 1000), col, scale=2, align="c")
        else:
            d.line([polar(cx, cy, r, n), polar(cx, cy, r - (5 if n % 500 == 0 else 3), n)], fill=DIM)
    if live and rpm is not None:                                      # needle: the outer part only
        needle = RED if rpm > RPM_RED else AMBER
        d.line([polar(cx, cy, r * 0.62, rpm), polar(cx, cy, r - 3, rpm)], fill=needle, width=4)
    v = data.get("speed_kmh") if live else None
    val = fmt("speed_kmh", v)
    text(img, cx, cy - 34, val, GREEN if v is not None else DIM, scale=5, align="c")
    text(img, cx, cy + 3, unit("speed_kmh"), DIM, align="c", upper=False)
    g = data.get("gear") if v is not None else None
    gx0, gy0 = cx - 13, cy + 13
    d.rectangle([gx0, gy0, gx0 + 26, gy0 + 22], outline=DIM if g else FAINT)
    text(img, cx, gy0 + 4, fmt("gear", g) if g is not None else "-", AMBER if g else DIM, scale=2, align="c")
    text(img, cx, gy0 + 26, f"{fmt('rpm', rpm)} RPM" if rpm is not None else ("OFF" if feed.ok else "----"),
         colour("rpm", rpm) if rpm is not None else DIM, align="c")
    # corners (clear of the ring): temperature and battery top left, clock top right, MPG bottom right
    for i, k in enumerate(("coolant_c", "dme_volts")):
        y = TOP + 3 + i * 20
        v = data.get(k)
        text(img, 2, y, "TEMP" if k == "coolant_c" else "BATT", DIM)
        text(img, 2, y + 8, fmt(k, v if live else None) + (unit(k) if live and v is not None else ""),
             colour(k, v) if live and v is not None else DIM, upper=False)
    lab, val = mpg_text(feed, live)
    text(img, LW - 2, 124, lab, DIM, align="r")
    text(img, LW - 2, 133, val, AMBER if live and val != "--" else DIM, scale=2, align="r")
    text(img, LW - 2, TOP + 2, time.strftime("%H:%M"), DIM, align="r")


def layout_bars(img, d, feed, ui=None):
    """A shift-light rev bar across the top, huge speed and gear, bar meters for the rest."""
    data = (feed.latest or {}).get("data", {})
    live = feed.engine_on
    rpm = data.get("rpm") if live else None
    rev_bar(img, d, 4, TOP + 3, LW - 5, TOP + 17, rpm, live)
    text(img, 4, 47, f"{fmt('rpm', rpm)} RPM" if rpm is not None else ("ENGINE OFF" if feed.ok else "----"),
         colour("rpm", rpm) if rpm is not None else DIM)
    v = data.get("speed_kmh") if live else None
    val = fmt("speed_kmh", v)
    text(img, 4, 60, val, GREEN if v is not None else DIM, scale=6)
    text(img, 6 + text_width(val, 6), 95, unit("speed_kmh"), DIM, upper=False)
    g = data.get("gear") if v is not None else None
    d.rectangle([110, 56, 150, 104], outline=DIM if g else FAINT)
    text(img, 130, 59, "GEAR", DIM, align="c")
    text(img, 130, 70, fmt("gear", g) if g is not None else "-", AMBER if g else DIM, scale=4, align="c")
    lab, mv = mpg_text(feed, live)
    text(img, 4, 115, lab, DIM)
    text(img, 4, 124, mv, AMBER if live and mv != "--" else DIM, scale=2)
    d.line([156, 50, 156, 140], fill=FAINT)
    for i, k in enumerate(("coolant_c", "dme_volts", "intake_c", "advance_deg", "injector_ms", "o2_volts")):
        lab = {"advance_deg": "TIMING", "injector_ms": "INJ", "dme_volts": "BATTERY"}.get(k)
        bar_meter(img, d, 162, LW - 4, 50 + i * 15, k, data.get(k), live, lab)
    d.line([0, 143, LW, 143], fill=FAINT)
    footer(img, d, feed, data, live, ui)


def map_panel(img, d, feed, ui, rect):
    """The moving map (mappage, the MAP tab's view) cropped into rect, with the road
    name and, while navigating, the next turn."""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0 + 1, y1 - y0 + 1
    fix = feed.gps_now() if hasattr(feed, "gps_now") else getattr(feed, "gps", {}) or {}
    m = ui.map if ui else None
    layer = Image.new("RGB", (LW, LH), BG)
    if not m or not m._draw_map(layer, ImageDraw.Draw(layer), fix, list(getattr(feed, "track", []))):
        text(img, (x0 + x1) // 2, (y0 + y1) // 2, "WAITING FOR GPS", DIM, align="c")
    else:
        top = max(TOP + 1, min(LH - h, mappage.CAR_Y - round(h * 0.7)))   # the car low in the panel
        left = LW // 2 - w // 2
        img.paste(layer.crop((left, top, left + w, top + h)), (x0, y0))
        nav = m.nav
        p = (nav.progress or {}) if nav and nav.active else {}
        if "step" in p:
            d.rectangle([x0, y0, x1, y0 + 18], fill=BG)
            d.line([x0, y0 + 18, x1, y0 + 18], fill=DIM)
            mappage.turn_icon(d, x0 + 10, y0 + 9, p["step"]["turn"], AMBER)
            text(img, x0 + 22, y0 + 2, mappage.nav_distance(p["to_step"], MPH), AMBER, upper=False)
            text(img, x0 + 22, y0 + 10, clip(mappage.short_name(p["step"]["street"] or ""), 21), GREEN)
        elif m.road_name:
            text(img, x0 + 3, y0 + 2, clip(m.road_name, 24), GREEN, upper=False)
    d.line([x1 + 1, y0, x1 + 1, y1], fill=FAINT)


def side_gauges(img, d, feed, x0, y0, rows=("coolant_c", "dme_volts")):
    """Beside a map: speed, gear, a rev bar and a few readings."""
    data = (feed.latest or {}).get("data", {})
    live = feed.engine_on
    rpm = data.get("rpm") if live else None
    v = data.get("speed_kmh") if live else None
    val = fmt("speed_kmh", v)
    text(img, x0, y0, val, GREEN if v is not None else DIM, scale=4)
    text(img, x0 + 1 + text_width(val, 4), y0 + 21, unit("speed_kmh"), DIM, upper=False)
    g = data.get("gear") if v is not None else None
    d.rectangle([LW - 30, y0, LW - 2, y0 + 27], outline=DIM if g else FAINT)
    text(img, LW - 16, y0 + 5, fmt("gear", g) if g is not None else "-", AMBER if g else DIM, scale=3, align="c")
    rev_bar(img, d, x0, y0 + 33, LW - 3, y0 + 40, rpm, live, labels=False)
    text(img, x0, y0 + 44, f"{fmt('rpm', rpm)} RPM" if rpm is not None else ("ENGINE OFF" if feed.ok else "----"),
         colour("rpm", rpm) if rpm is not None else DIM)
    for i, k in enumerate(rows):
        lab = {"coolant_c": "TEMP", "dme_volts": "BATT", "advance_deg": "TIMING", "intake_c": "INTAKE"}.get(k)
        bar_meter(img, d, x0, LW - 3, y0 + 56 + i * 15, k, data.get(k), live, lab, segs=10)


def layout_map(img, d, feed, ui=None):
    """Map on the left, speed / gear / revs / readings and MPG on the right."""
    map_panel(img, d, feed, ui, MAP_RECT)
    side_gauges(img, d, feed, SIDE_X, TOP + 3, ("coolant_c", "dme_volts", "intake_c"))
    lab, val = mpg_text(feed, feed.engine_on)
    text(img, SIDE_X, 122, lab, DIM)
    text(img, SIDE_X, 131, val, AMBER if feed.engine_on and val != "--" else DIM, scale=2)
    text(img, LW - 2, 140, time.strftime("%H:%M"), GREEN, align="r")


def layout_mapmusic(img, d, feed, ui=None):
    """Map top left, gauges top right, what's playing along the bottom."""
    map_panel(img, d, feed, ui, MAPMUSIC_MAP)
    side_gauges(img, d, feed, SIDE_X, TOP + 3, ("coolant_c",))
    d.line([0, 118, LW, 118], fill=FAINT)
    p = ui.player if ui else None
    if not p or not p.item:
        text(img, LW // 2, 132, "NOTHING PLAYING - TAP FOR MUSIC" if p else "NO MUSIC", DIM, align="c")
    else:
        icon(d, "pause" if p.playing else "play", 10, 131, AMBER)
        text(img, 22, 122, clip(p.title, 32), GREEN, scale=1, upper=False)
        text(img, 22, 131, clip(p.artist, 32), AMBER, upper=False)
        text(img, LW - 3, 122, time.strftime("%H:%M"), DIM, align="r")
        bx0, bx1, by = 4, LW - 5, 146
        d.rectangle([bx0, by, bx1, by + 3], outline=FAINT)
        if p.duration:
            fw = int((bx1 - bx0 - 2) * p.progress / p.duration)
            if fw > 0:
                d.rectangle([bx0 + 1, by + 1, bx0 + 1 + fw, by + 2], fill=GREEN if p.playing else DIM)
        text(img, bx0, 151, spotify.mmss(p.progress), DIM)
        text(img, bx1, 151, spotify.mmss(p.duration), DIM, align="r")


DRAW_LAYOUT = {"classic": layout_classic, "cockpit": layout_cockpit, "bars": layout_bars,
               "map": layout_map, "mapmusic": layout_mapmusic}


def page_gauges(img, d, feed, ui=None):
    DRAW_LAYOUT[drive_layout(ui)](img, d, feed, ui)


def page_more(img, d, feed):
    data = (feed.latest or {}).get("data", {})
    keys = sorted((k for k in data if k not in MAIN_TILES and k not in ("rpm", "speed_kmh", "gear", "firmware", "o2_volts", "o2_state", "airflow_gs", "throttle", "idle_valve_pct")),
                  key=lambda k: (ORDER.get(k, 999), k))
    if not keys:
        text(img, LW // 2, 80, "NO READINGS YET", DIM, align="c")
        return
    rows = 12
    for i, k in enumerate(keys[:rows * 2]):
        x = 4 + (i // rows) * (LW // 2)
        y = TOP + 2 + (i % rows) * 11
        lab = label(k)
        val = f"{fmt(k, data[k]).upper()}{unit(k) if isinstance(data[k], (int, float)) and not isinstance(data[k], bool) else ''}"
        val = val[:max(3, 20 - len(lab))]
        text(img, x, y, lab, DIM)
        text(img, x + LW // 2 - 8, y, val, colour(k, data[k]), align="r", upper=False)
    d.line([LW // 2, TOP, LW // 2, LH - 2], fill=FAINT)


# SET page: one row per thing to change, each showing its state now and opening its screen
WIFI_BTN = (4, 40, LW - 5, 56)
CELL_BTN = (4, 59, LW - 5, 75)             # 4G: signal, which link first, hotspot (screens.CellScreen)
AUDIO_BTN = (4, 92, LW - 5, 108)
SCREEN_BTN = (4, 124, 108, 156)            # brightness, colours, DRIVE LAYOUT, TOUCH CAL (screen.py)
SHUTDOWN_BOX = (112, 124, LW - 5, 156)


def setting_row(img, d, rect, label, value, colour=GREEN, lit=False):
    """A full-width row button: LABEL, what it's set to now, ">"."""
    x0, y0, x1, y1 = rect
    if lit:
        d.rectangle(rect, fill=colour)
    else:
        d.rectangle(rect, outline=DIM)
    fg = BG if lit else None
    text(img, x0 + 4, y0 + 5, label, fg or DIM)
    text(img, x0 + 44, y0 + 5, value[:34], fg or colour, upper=False)
    text(img, x1 - 4, y0 + 5, ">", fg or GREEN, align="r")


def signal_bars(d, x, y, n, colour=GREEN):
    """Four rising bars, n of them filled; (x, y) = bottom left."""
    for k in range(4):
        h = 3 + k * 2
        box = [x + k * 5, y - h, x + k * 5 + 3, y]
        d.rectangle(box, fill=colour if k < n else None, outline=colour if k < n else FAINT)


def page_status(img, d, feed, ui=None):
    i = feed.info or {}
    ips = i.get("ip") or []
    hotspot = feed.wifi == wifi.HOTSPOT
    host = "10.42.0.1" if hotspot else ips[0] if ips else "--"
    text(img, 4, TOP + 3, "PHONE APP", DIM)
    text(img, 66, TOP + 3, f"http://{host}", AMBER, upper=False)
    dme = i.get("dme_connected")
    text(img, LW - 4, TOP + 3, "DME OK" if dme else "DME NOT CONNECTED", GREEN if dme else RED, align="r")
    if ui and not ui.cal:
        button(img, d, (4, 40, LW - 5, 75), "TAP HERE TO SET UP TOUCH", AMBER, bool(ui.lit("wifi")))
        return
    lit = lambda name: bool(ui and ui.lit(name))          # noqa: E731
    net = cell.load()
    wname = "OWN HOTSPOT (SHARING 4G)" if hotspot else feed.wifi or "NOT CONNECTED"
    setting_row(img, d, WIFI_BTN, "WI-FI", wname, AMBER if hotspot else GREEN if feed.wifi else RED, lit("wifi"))
    m = ui and ui.modem
    mi = (m.info if m else {}) or {}
    if not m or not m.present:
        cval, ccol = "NO MODEM", RED
    elif mi.get("reg") in ("HOME", "ROAMING"):
        cval, ccol = " ".join(x for x in (mi.get("operator"), mi.get("tech")) if x), GREEN
        u = m.usage
        cval += f"  {u.used_text()}/{u.plan_text()}"
        ccol = RED if u.fraction >= 0.9 else AMBER if u.fraction >= 0.75 else GREEN
    else:
        cval, ccol = (mi.get("reg") or "STARTING...").upper(), AMBER
    setting_row(img, d, CELL_BTN, "4G", cval, ccol, lit("cell"))
    if m and m.present and not lit("cell"):
        signal_bars(d, CELL_BTN[2] - 34, CELL_BTN[1] + 12, mi.get("bars", 0), ccol)
    via = cell.internet_via()
    first = "4G FIRST" if net["prefer"] == "cell" else "WI-FI FIRST"
    text(img, 8, 79, f"INTERNET VIA {via or 'NOTHING'}  ({first})", GREEN if via else RED)
    r = ui and ui.router
    if not r:
        aval, acol = "--", DIM
    elif r.route == "bluetooth":
        aval, acol = f"BLUETOOTH: {r.settings.get('name') or 'STEREO'}", GREEN
    elif r.settings["output"] == "bluetooth":
        aval, acol = "STEREO NOT CONNECTED - PAUSED", AMBER
    else:
        aval, acol = "AUX (HEADPHONE JACK)", GREEN
    setting_row(img, d, AUDIO_BTN, "AUDIO", aval, acol, lit("audio"))
    smid, on = (SCREEN_BTN[0] + SCREEN_BTN[2] + 1) // 2, lit("screen")
    d.rectangle(SCREEN_BTN, fill=GREEN if on else None, outline=GREEN)
    text(img, smid, 131, "SCREEN >", BG if on else GREEN, align="c")
    text(img, smid, 143, "LAYOUT, TOUCH", BG if on else DIM, align="c")
    d.rectangle(SHUTDOWN_BOX, outline=RED)
    mid = (SHUTDOWN_BOX[0] + SHUTDOWN_BOX[2] + 1) // 2
    text(img, mid, 131, "SHUT DOWN:", RED, align="c")
    text(img, mid, 143, "HOLD HERE 4 SEC", RED, align="c")


# ---- wireframe city for the shutdown screen (the glider approach) ----

_city = None


def city_boxes():
    global _city
    if _city is None:
        rng = random.Random(1981)
        _city = []
        for z in range(10, 120, 7):
            for side in (-1, 1):
                for _ in range(2):
                    x = side * rng.uniform(4, 30)
                    _city.append((x, z + rng.uniform(-2, 2), rng.uniform(2, 4.5),
                                  rng.uniform(2, 4.5), rng.uniform(3, 22 if abs(x) > 8 else 12)))
    return _city


def wire_city(d, progress, bright=GREEN, grid=DIM):
    horizon, f, cam_y = 58, 110, 6.0
    cam_z = -16 + progress * 22
    near = 0.6

    def seg(a, b, col):
        za, zb = a[2] - cam_z, b[2] - cam_z
        if za < near and zb < near:
            return
        if za < near or zb < near:                     # clip to the near plane
            t = (near - za) / (zb - za)
            p = tuple(a[i] + (b[i] - a[i]) * t for i in range(3))
            a, b = (p, b) if za < near else (a, p)
        pa = [LW / 2 + f * a[0] / (a[2] - cam_z), horizon + f * (cam_y - a[1]) / (a[2] - cam_z)]
        pb = [LW / 2 + f * b[0] / (b[2] - cam_z), horizon + f * (cam_y - b[1]) / (b[2] - cam_z)]
        d.line([tuple(pa), tuple(pb)], fill=col)

    far = 130
    for x in range(-40, 41, 5):
        seg((x, 0, cam_z + near), (x, 0, far), FAINT if x else grid)
    for z in range(int(cam_z) // 6 * 6 + 6, far, 6):
        seg((-40, 0, z), (40, 0, z), FAINT)
    for x, z, w, dp, h in sorted(city_boxes(), key=lambda b: -b[1]):
        if z + dp < cam_z + near:
            continue
        x0, x1, z0, z1 = x - w / 2, x + w / 2, z, z + dp
        col = bright if z - cam_z < 45 else grid
        for y in (0, h):
            seg((x0, y, z0), (x1, y, z0), col)
            seg((x1, y, z0), (x1, y, z1), col)
            seg((x1, y, z1), (x0, y, z1), col)
            seg((x0, y, z1), (x0, y, z0), col)
        for cx, cz in ((x0, z0), (x1, z0), (x1, z1), (x0, z1)):
            seg((cx, 0, cz), (cx, h, cz), col)


def render_shutdown(progress=None, message=None):
    """Hold-to-shut-down prompt (progress 0..1), or a final message. Physical-size image."""
    img = Image.new("RGB", (LW, LH), BG)
    d = ImageDraw.Draw(img)
    wire_city(d, 1.0 if message else progress, *((DIM, FAINT) if message else (GREEN, DIM)))
    if message:
        title, sub = message
        d.rectangle([0, 44, LW, 110], fill=BG)
        d.line([0, 44, LW, 44], fill=RED)
        d.line([0, 110, LW, 110], fill=RED)
        text(img, LW // 2, 52, title, RED, scale=2, align="c")
        for n, line in enumerate(sub.split("\n")):
            text(img, LW // 2, 76 + n * 12, line, GREEN, align="c")
        return to_panel(img)
    d.rectangle([0, 0, LW, 22], fill=BG)
    text(img, LW // 2, 4, "KEEP HOLDING", RED if blink() else AMBER, scale=2, align="c")
    d.rectangle([0, 124, LW, LH], fill=BG)
    text(img, LW // 2, 127, "SHUTTING DOWN THE PI", GREEN, align="c")
    d.rectangle([20, 138, LW - 21, 146], outline=RED)
    filled = int((LW - 44) * progress)
    if filled > 0:
        d.rectangle([22, 140, 22 + filled, 144], fill=RED)
    text(img, LW // 2, 151, "LET GO TO CANCEL", DIM, align="c")
    return to_panel(img)


def hold_progress(held, ui):
    """Shutdown countdown, 0..1, while a finger that came down on the SET page's
    SHUT DOWN box is still held; None otherwise. Holding anywhere else does nothing
    (Ian, 2026-10-15: it used to be any long press, which fired by accident)."""
    if held is None or not ui.shutdown_armed or ui.no_hold:
        return None
    return min(1.0, held / HOLD_FIRE)


def shutdown(log):
    """Clean power-off. True if it was started."""
    log("shutdown requested from the touchscreen")
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "poweroff"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"shutdown failed: {e}")
        return False
    if r.returncode:
        log(f"shutdown failed: {r.stderr.strip()}")
    return r.returncode == 0


# ---- Spotify page ----

# one row of buttons along the bottom (volume is on the amp)
PREV_BTN, PLAY_BTN, SKIP_BTN = (0, 128, 32, 157), (36, 128, 74, 157), (78, 128, 110, 157)
SHUF_BTN, SUGGEST_BTN, LISTS_BTN = (114, 128, 162, 157), (166, 128, 218, 157), (222, 128, LW - 1, 157)
VIS_TOP, VIS_BOTTOM, VIS_BARS = 62, 103, 16
HEART_BTN = (LW - 30, TOP + 18, LW - 3, TOP + 40)       # right of the artist/album lines
HEART_TAP = (LW - 44, TOP + 14, LW - 1, TOP + 42)       # a bigger target for a finger in a moving car
HEART = (".###...###.", "#####.#####", "###########", "###########", ".#########.",
         "..#######..", "...#####...", "....###....", ".....#.....")


def icon(d, kind, cx, cy, c):
    """Transport icons, drawn rather than lettered so they read at a glance."""
    if kind == "play":
        d.polygon([(cx - 5, cy - 7), (cx - 5, cy + 7), (cx + 7, cy)], fill=c)
    elif kind == "pause":
        d.rectangle([cx - 6, cy - 7, cx - 2, cy + 7], fill=c)
        d.rectangle([cx + 2, cy - 7, cx + 6, cy + 7], fill=c)
    elif kind == "next":
        d.polygon([(cx - 8, cy - 6), (cx - 8, cy + 6), (cx + 2, cy)], fill=c)
        d.rectangle([cx + 3, cy - 6, cx + 5, cy + 6], fill=c)
    elif kind == "prev":
        d.polygon([(cx + 8, cy - 6), (cx + 8, cy + 6), (cx - 2, cy)], fill=c)
        d.rectangle([cx - 5, cy - 6, cx - 3, cy + 6], fill=c)


def heart(d, cx, cy, colour, filled, scale=2):
    """Spotify's like heart: filled = in Liked Songs, else just its outline."""
    rows, w = len(HEART), len(HEART[0])
    on = lambda x, y: 0 <= y < rows and 0 <= x < w and HEART[y][x] == "#"      # noqa: E731
    x0, y0 = cx - w * scale // 2, cy - rows * scale // 2
    for y in range(rows):
        for x in range(w):
            if on(x, y) and (filled or not all(on(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))):
                px, py = x0 + x * scale, y0 + y * scale
                d.rectangle([px, py, px + scale - 1, py + scale - 1], fill=colour)


def icon_button(d, rect, kind, colour, pressed):
    if pressed:
        d.rectangle(rect, fill=colour)
    else:
        d.rectangle(rect, outline=colour)
    icon(d, kind, (rect[0] + rect[2] + 1) // 2, (rect[1] + rect[3] + 1) // 2, BG if pressed else colour)


def clip(s, n):
    return s if len(s) <= n else s[:n - 2].rstrip() + ".."


class AudioTap:
    """Hears what Spotify plays: ALSA copies all playback into the Loopback card
    (system/asound.conf), and arecord reads it back while the visualiser is up."""
    RATE, N = 44100, 2048

    def __init__(self, device="plughw:Loopback,1,0"):
        self.device = device
        self.buf = np.zeros(self.N, np.float32)
        self.fresh = 0.0          # when audio last arrived
        self.wanted = 0.0         # when the visualiser last asked; it stops 2 s after
        self.lock = threading.Lock()
        self.thread = None

    def want(self):
        self.wanted = time.monotonic()
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def window(self):
        """The last N samples (mono, -1..1), or None if nothing is coming in."""
        with self.lock:
            return self.buf.copy() if time.monotonic() - self.fresh < 0.5 else None

    def _run(self):
        chunk = 512
        while time.monotonic() - self.wanted < 2:
            proc = subprocess.Popen(["arecord", "-q", "-D", self.device, "-t", "raw", "-f", "S16_LE",
                                     "-r", str(self.RATE), "-c", "2"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                while time.monotonic() - self.wanted < 2:
                    data = proc.stdout.read(chunk * 4)
                    if len(data) < chunk * 4:
                        break
                    x = np.frombuffer(data, "<i2").reshape(-1, 2).mean(axis=1, dtype=np.float32) / 32768
                    with self.lock:
                        self.buf = np.concatenate((self.buf[chunk:], x))
                        self.fresh = time.monotonic()
            finally:
                proc.kill()
                proc.wait()
            if time.monotonic() - self.wanted < 2:
                time.sleep(2)              # no Loopback card? try again shortly


class Visualiser:
    """Spectrum bars from the music actually playing (see AudioTap)."""
    LO, HI = 40, 12000         # Hz covered by the bars
    RANGE = 36                 # dB from an empty bar to a full one
    QUIET = -60                # dB: below this the gain stops rising (silence stays empty)

    def __init__(self, bars=VIS_BARS, rate=AudioTap.RATE, n=AudioTap.N):
        self.h = [0.0] * bars
        self.ref = self.QUIET      # auto-gain: a full bar, in dB; jumps to peaks, eases down
        self.t = None
        freqs = np.fft.rfftfreq(n, 1 / rate)
        edges = np.geomspace(self.LO, self.HI, bars + 1)
        self.bins = [np.flatnonzero((freqs >= lo) & (freqs < hi)) for lo, hi in zip(edges, edges[1:])]
        self.bins = [b if len(b) else np.array([np.searchsorted(freqs, lo)]) for b, lo in zip(self.bins, edges)]
        self.tilt = 4.5 * np.log2(np.sqrt(edges[:-1] * edges[1:]) / 1000)   # music falls off with pitch
        self.win = np.hanning(n).astype(np.float32)

    def decibels(self, samples):
        """Loudness of each bar's band, in dB (treble lifted to match the bass)."""
        spec = np.abs(np.fft.rfft(samples * self.win)) / (len(samples) / 4)
        return np.array([20 * np.log10(np.sqrt(np.mean(spec[b] ** 2)) + 1e-9) for b in self.bins]) + self.tilt

    def step(self, samples, now=None):
        """samples: the latest audio window, or None when there's nothing to hear."""
        now = time.monotonic() if now is None else now
        dt = 0 if self.t is None else min(0.5, now - self.t)
        self.t = now
        if samples is None:
            target = np.zeros(len(self.h))
        else:
            db = self.decibels(samples)
            self.ref = max(self.ref - dt * 4, float(db.max()), self.QUIET)
            target = np.clip((db - self.ref + self.RANGE) / self.RANGE, 0, 1)
        for i, v in enumerate(target):
            if v > self.h[i]:
                self.h[i] = float(v)                                # jump up with the beat
            else:
                self.h[i] = max(float(v), self.h[i] - dt * 2.5)     # fall back smoothly

    def draw(self, d, x0, y0, x1, y1, colour):
        n = len(self.h)
        w = (x1 - x0 + 1 + 3) / n
        for i in range(n):
            bx = round(x0 + i * w)
            top = y1 - round(self.h[i] * (y1 - y0))
            if top < y1:
                d.rectangle([bx, top, round(x0 + (i + 1) * w) - 4, y1], fill=colour)


def page_spotify(img, d, ui):
    p = ui.player
    if p is None:
        text(img, LW // 2, 80, "SPOTIFY IS NOT AVAILABLE", DIM, align="c")
        return
    lit = ui.lit
    if p.item:
        t = p.title
        if text_width(t, 2) <= LW - 8:
            text(img, 4, TOP + 3, t, GREEN, scale=2, upper=False)
        else:
            lines = wrap(t, 39)
            text(img, 4, TOP + 3, lines[0], GREEN, upper=False)
            if len(lines) > 1:
                text(img, 4, TOP + 11, clip(" ".join(lines[1:]), 39), GREEN, upper=False)
        text(img, 4, TOP + 21, clip(p.artist, 38), AMBER, upper=False)
        album = p.album
        if p.device and p.device != spotify.DEVICE:              # only worth saying when it's elsewhere
            album = f"ON {p.device.upper()}"
        text(img, 4, TOP + 30, clip(album, 38), DIM, upper=album.startswith("ON "))
        if p.show_heart:                                         # only for songs not in Liked Songs
            liked, cx, cy = p.heart, (HEART_BTN[0] + HEART_BTN[2] + 1) // 2, (HEART_BTN[1] + HEART_BTN[3] + 1) // 2
            if lit("heart"):
                d.rectangle(HEART_BTN, fill=GREEN)
            # red = in Liked Songs; dim outline = not known yet (no internet)
            heart(d, cx, cy, BG if lit("heart") else RED if liked else GREEN if liked is False else DIM, bool(liked))
        if p.playing:
            ui.audio.want()
        ui.vis.step(ui.audio.window() if p.playing else None)
        ui.vis.draw(d, 4, VIS_TOP, LW - 5, VIS_BOTTOM, GREEN)
        x0, x1, y = 4, LW - 5, 109
        d.rectangle([x0, y, x1, y + 3], outline=FAINT)
        if p.duration:
            w = int((x1 - x0 - 2) * p.progress / p.duration)
            if w > 0:
                d.rectangle([x0 + 1, y + 1, x0 + 1 + w, y + 2], fill=GREEN if p.playing else DIM)
        text(img, x0, 116, spotify.mmss(p.progress), DIM)
        text(img, x1, 116, spotify.mmss(p.duration), DIM, align="r")
    elif not p.polled:
        text(img, LW // 2, 70, "CONNECTING TO SPOTIFY...", DIM, align="c")
    else:
        text(img, LW // 2, TOP + 20, "NOTHING PLAYING", GREEN, scale=2, align="c")
        text(img, LW // 2, TOP + 44, "PLAY = YOUR LIKED SONGS, SHUFFLED", DIM, align="c")
        text(img, LW // 2, TOP + 54, "OR PICK FROM PLAYLISTS", DIM, align="c")
    problem = p.problem
    if problem and not problem.startswith("SPOTIFY"):
        problem = "SPOTIFY: " + problem
    note = p.doing or problem or (ui.suggester.status if ui.suggester else "")
    if note:
        d.rectangle([0, 115, LW, 124], fill=BG)
        text(img, LW // 2, 116, clip(note.upper(), 39), RED if problem and not p.doing else AMBER, align="c")
    icon_button(d, PREV_BTN, "prev", GREEN, lit("prev"))
    icon_button(d, PLAY_BTN, "pause" if p.playing else "play", GREEN, lit("play"))
    icon_button(d, SKIP_BTN, "next", GREEN, lit("skip"))
    button(img, d, SHUF_BTN, "SHUFFLE", GREEN, p.shuffle != lit("shuf"))   # filled = shuffle on
    button(img, d, SUGGEST_BTN, "SUGGEST", GREEN if ui.suggester else FAINT,
           lit("suggest") or bool(ui.suggester and ui.suggester.busy))   # filled while the picks finish
    button(img, d, LISTS_BTN, "LISTS", GREEN, lit("lists"))


# Playlists and their songs, remembered for LIST_CACHE_SECS so opening LISTS or a list again is
# instant. Refreshed only when opened after that - never in the background (Spotify's limits).
LIST_CACHE_SECS = 600
_playlists = None                  # (monotonic time, [(id, name)])
_songs = {}                        # playlist id -> (monotonic time, [(uri, title, artists)], total)


class PlaylistScreen(screens.Pressable):
    """Liked songs + Ian's playlists; tap one to open its songs (SongsScreen)."""
    BACK = (0, 0, 56, 18)
    UP = (0, 138, 116, 159)
    DOWN = (LW - 118, 138, LW - 1, 159)
    ROW_Y, ROW_H, ROWS = 22, 23, 5

    def __init__(self, player):
        super().__init__()
        self.player = player
        self.rows = [("liked", "LIKED SONGS")]
        self.top = 0
        self.status = "LOADING..."
        if _playlists and time.monotonic() - _playlists[0] < LIST_CACHE_SECS:
            self._load()                               # remembered: no thread, no wait
        else:
            threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        global _playlists
        if _playlists and time.monotonic() - _playlists[0] < LIST_CACHE_SECS:
            self.rows += _playlists[1]                 # seen in the last 10 min: at once
            self.status = ""
            return
        try:
            rows = [(pl["id"], pl["name"]) for pl in spotify.get("user-playlists") or []]
            _playlists = (time.monotonic(), rows)
            self.rows += rows
            self.status = ""
        except (RuntimeError, ValueError) as e:
            self.status = f"ERROR: {e}"[:39]

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        if inside(self.UP, x, y):
            self.press("up")
            self.top = max(0, self.top - self.ROWS)
        elif inside(self.DOWN, x, y):
            self.press("down")
            if self.top + self.ROWS < len(self.rows):
                self.top += self.ROWS
        elif self.ROW_Y <= y < self.ROW_Y + self.ROWS * self.ROW_H:
            i = self.top + (y - self.ROW_Y) // self.ROW_H
            if i < len(self.rows):
                self.press(("row", i - self.top))
                return SongsScreen(self.player, *self.rows[i])
        return None

    def render(self, img, d):
        button(img, d, self.BACK, "< BACK", GREEN, self.lit("back"))
        text(img, 148, 6, "PLAYLISTS", GREEN, align="c")
        if self.status:
            text(img, LW // 2, 70, self.status, RED if self.status.startswith("ERROR") else DIM, align="c")
        for n, (pid, name) in enumerate(self.rows[self.top:self.top + self.ROWS]):
            y0 = self.ROW_Y + n * self.ROW_H
            d.rectangle((0, y0, LW - 1, y0 + self.ROW_H - 3), outline=FAINT)
            text(img, 4, y0 + 7, clip(name, 39), AMBER if pid == "liked" else GREEN,
                 upper=pid == "liked")
        more = self.top + self.ROWS < len(self.rows)
        button(img, d, self.UP, "UP", GREEN if self.top else FAINT, self.lit("up"))
        button(img, d, self.DOWN, f"DOWN  {min(len(self.rows), self.top + self.ROWS)}/{len(self.rows)}",
               GREEN if more else FAINT, self.lit("down"))


class LayoutScreen(screens.Pressable):
    """SET > DRIVE LAYOUT: the five ways the DRIVE page can look (LAYOUTS); tap one."""
    BACK = (0, 0, 56, 18)
    ROW_Y, ROW_H = 22, 27

    def __init__(self, settings):
        super().__init__()
        self.settings = settings           # screen.Screen: kept in screen.json with the other screen settings

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        i = (y - self.ROW_Y) // self.ROW_H
        if y >= self.ROW_Y and 0 <= i < len(LAYOUTS):
            self.press(("row", i))
            self.settings.change(layout=LAYOUTS[i][0])
            return "close"
        return None

    def render(self, img, d):
        button(img, d, self.BACK, "< BACK", GREEN, self.lit("back"))
        text(img, LW - 4, 6, "DRIVE LAYOUT", DIM, align="r")
        current = self.settings.settings.get("layout", "classic")
        for i, (key, name, about) in enumerate(LAYOUTS):
            y0 = self.ROW_Y + i * self.ROW_H
            on = key == current
            d.rectangle((0, y0, LW - 1, y0 + self.ROW_H - 3), outline=AMBER if on else FAINT,
                        fill=FAINT if self.lit(("row", i)) else None)
            text(img, 6, y0 + 4, name, AMBER if on else GREEN, scale=1)
            text(img, 6, y0 + 14, about, DIM)
            if on:
                text(img, LW - 6, y0 + 4, "IN USE", AMBER, align="r")


class SongsScreen(screens.Pressable):
    """One playlist's songs, a line each, with PAGE UP / PAGE DOWN; tap a song to play
    it there and then (the playlist carries on after it). Songs are fetched only as the
    pages are reached, spotify.PAGE at a time - no background Spotify calls (Spotify
    blocked the app for 5 hours over too many of those, 2026-10-17).

    The song playing now is highlighted, and the list opens on its page: if it's being
    played from this playlist, pages are fetched until it turns up (at most SEEK_PAGES,
    so a huge playlist can't flood Spotify). A picked song stays lit for PICK_SECS
    before the screens close, so the tap shows."""
    BACK = (0, 0, 56, 18)
    SHUFFLE = (LW - 70, 0, LW - 1, 18)
    UP = (0, 138, 116, 159)
    DOWN = (LW - 118, 138, LW - 1, 159)
    ROW_Y, ROW_H, ROWS = 21, 14, 8              # ~6.5 mm rows: fingers in a moving car
    SEEK_PAGES = 20                             # 1000 songs at spotify.PAGE = 50
    PICK_SECS = 0.25
    close_all = True                            # when expired: back to the MUSIC page

    def __init__(self, player, pid, name):
        super().__init__()
        self.player, self.pid, self.name = player, pid, name
        self.songs = []                # (uri, title, artists), in playlist order, as far as fetched
        self.total = None              # songs in the playlist (None until the first page answers)
        self.top = 0
        self.status = "LOADING..."
        self.loading = False
        self.moved = False             # paged by hand: don't jump to the playing song any more
        self.picked = None             # (row index, monotonic time) of the song just tapped
        context = ((getattr(player, "state", None) or {}).get("context") or {}).get("uri", "")
        self.seek = bool(self.playing_uri) and (
            context == f"spotify:playlist:{pid}" or (pid == "liked" and context.endswith(":collection")))
        cached = _songs.get(pid)
        if cached and time.monotonic() - cached[0] < LIST_CACHE_SECS:      # opened lately: at once
            self.songs, self.total = list(cached[1]), cached[2]
            self.status = "" if self.songs else "NO SONGS"
            if self._show_playing() or not self.seek or len(self.songs) >= self.total:
                self.seek = False                      # only playing from this list justifies more pages
                return
        self._fetch()

    @property
    def playing_uri(self):
        """The playing song as a spotify:track: uri. spotify_player's playback JSON gives the
        track's id but no uri (2026-10-18: Glycerine, no. 80 of Liked Songs, wasn't found)."""
        item = ((getattr(self.player, "item", None) or {}) if self.player else {})
        return item.get("uri") or (f"spotify:track:{item['id']}" if item.get("id") else None)

    @property
    def expired(self):
        return self.picked is not None and time.monotonic() - self.picked[1] >= self.PICK_SECS

    def _fetch(self):
        """Fetch the next page of songs (in a thread), if there's more and none is on its way;
        while seeking the playing song, carry on page by page until it's found."""
        if self.loading or (self.total is not None and len(self.songs) >= self.total):
            return
        self.loading = True

        def run():
            try:
                pages = 0
                while True:
                    total, songs = spotify.playlist_songs(self.pid, len(self.songs))
                    self.total = total
                    self.songs += songs
                    pages += 1
                    if not songs:                          # (unplayable items only): don't ask again
                        self.total = len(self.songs)
                    self.status = "" if self.songs else "NO SONGS"
                    if self._show_playing() or not self.seek or pages >= self.SEEK_PAGES \
                            or len(self.songs) >= self.total:
                        break
            except (RuntimeError, ValueError) as e:
                self.status = str(e).upper()[:39]
            finally:
                self.loading = False
                self.seek = False
                if self.songs:
                    _songs[self.pid] = (time.monotonic(), list(self.songs), self.total)
        threading.Thread(target=run, daemon=True).start()

    def _show_playing(self):
        """Open on the playing song's page (unless paged by hand). -> found it."""
        uri = self.playing_uri
        i = next((n for n, s in enumerate(self.songs) if s[0] == uri), None) if uri else None
        if i is not None and not self.moved:
            self.top = i // self.ROWS * self.ROWS
        return i is not None

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        if inside(self.SHUFFLE, x, y):
            self.press("shuffle")
            self.player.play_playlist(self.pid, self.name, shuffle=True)
            return "done"
        if self.picked:                                # on its way out: ignore further taps
            return None
        if inside(self.UP, x, y):
            self.press("up")
            self.moved = True
            self.top = max(0, self.top - self.ROWS)
        elif inside(self.DOWN, x, y):
            self.press("down")
            self.moved = True
            known = len(self.songs) if self.total is None else self.total
            if self.top + self.ROWS < known:
                self.top += self.ROWS
                if self.top + self.ROWS > len(self.songs):    # this page isn't fetched yet
                    self._fetch()
        elif self.ROW_Y <= y < self.ROW_Y + self.ROWS * self.ROW_H:
            i = self.top + (y - self.ROW_Y) // self.ROW_H
            if i < len(self.songs):
                uri, title, artists = self.songs[i]
                self.player.play_song(self.pid, uri, title, [s[0] for s in self.songs[i + 1:i + 100]], artists)
                self.picked = (i, time.monotonic())        # lit a moment, then back to MUSIC (expired)
        return None

    def render(self, img, d):
        button(img, d, self.BACK, "< BACK", GREEN, self.lit("back"))
        text(img, (60 + self.SHUFFLE[0]) // 2, 6, clip(self.name.upper(), 22), AMBER, align="c")
        button(img, d, self.SHUFFLE, "SHUFFLE", GREEN, self.lit("shuffle"))
        page = self.songs[self.top:self.top + self.ROWS]
        now = self.playing_uri
        for n, (uri, title, artists) in enumerate(page):
            y0 = self.ROW_Y + n * self.ROW_H
            row = (0, y0, LW - 1, y0 + self.ROW_H - 2)
            picked = self.picked and self.picked[0] == self.top + n
            playing = uri == now
            if picked:                                  # just tapped: lit solid
                d.rectangle(row, fill=AMBER)
            elif playing:                               # playing now: boxed in amber
                d.rectangle(row, outline=AMBER)
            if n and not picked:
                d.line((0, y0 - 1, LW - 1, y0 - 1), fill=FAINT)
            fg, dim = (BG, BG) if picked else (AMBER, AMBER) if playing else (GREEN, DIM)
            text(img, 2, y0 + 4, ">" if playing and not picked else f"{self.top + n + 1:>3}", dim if not playing else fg)
            t = clip(title, 24)
            text(img, 24, y0 + 4, t, fg, upper=False)
            text(img, 24 + text_width(t) + 6, y0 + 4, clip(artists, max(0, 38 - len(t) - 4)), dim, upper=False)
        if self.status or (self.loading and len(page) < self.ROWS):
            text(img, LW // 2, self.ROW_Y + max(len(page), 3) * self.ROW_H + 2, self.status or "LOADING...",
                 RED if self.status and not self.status.startswith(("LOADING", "NO SONGS")) else DIM, align="c")
        known = len(self.songs) if self.total is None else self.total
        more = self.top + self.ROWS < known
        last = min(known, self.top + self.ROWS)
        button(img, d, self.UP, "PAGE UP", GREEN if self.top else FAINT, self.lit("up"))
        button(img, d, self.DOWN, f"PAGE DOWN {self.top + 1 if known else 0}-{last}/{known}",
               GREEN if more else FAINT, self.lit("down"))


# ---- Claude page ----

NEW_BTN, MODE_BTN, ASK_BTN = (0, 134, 40, 159), (44, 134, 120, 159), (124, 134, LW - 1, 159)   # MODE: VOICE / TEXT
DENY_BTN, ALLOW_BTN = (4, 126, LW // 2 - 4, 157), (LW // 2 + 4, 126, LW - 5, 157)
BODY_LINES, BODY_TOP = 12, TOP + 1
_PLAIN = {"**": "", "__": "", "`": "", "•": "-", "→": "->", "≈": "~", "×": "x", "°": "°"}


def plain(s):
    """Markdown-ish answer -> plain screen text."""
    for a, b in _PLAIN.items():
        s = s.replace(a, b)
    return "\n".join(line.lstrip("#").strip() if line.startswith("#") else line for line in s.splitlines())


def wrap(s, width=38):
    out = []
    for para in s.split("\n"):
        line = ""
        for word in para.split(" "):
            while len(word) > width:                 # very long words / URLs
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if len(line) + len(word) + (1 if line else 0) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        out.append(line)
    return out


def claude_lines(a):
    """-> [(text, colour)] for the conversation area."""
    if a is None:
        return [(line, DIM) for line in wrap("CLAUDE IS NOT AVAILABLE ON THIS PI.")]
    lines = []
    if a.question:
        lines += [(line, DIM) for line in wrap("> " + a.question)] + [("", DIM)]
    if a.answer:
        lines += [(line, GREEN) for line in wrap(plain(a.answer))]
    if not a.question and not a.answer:
        if a.logged_in is False:
            lines = [(line, AMBER) for line in wrap("NOT LOGGED IN YET.")] + [("", DIM)] + [
                (line, GREEN) for line in wrap(f"From a computer on the same Wi-Fi: ssh {getpass.getuser()}@{socket.gethostname()}.local "
                                               "then run: claude auth login")]
        else:
            lines = [(line, GREEN) for line in wrap("Tap ASK, then speak.")] + [("", DIM)] + [
                (line, DIM) for line in wrap("Claude can read the logger, look things up and answer. "
                                             "Anything that changes the Pi shows ALLOW / DENY here first.")]
    if a.status:
        lines += [("", DIM)] + [(line, RED if a.status.startswith(("ERROR", "NOT LOGGED")) else AMBER)
                                for line in wrap(a.status)]
    return lines


def page_claude(img, d, ui):
    a = ui.assistant
    lines = claude_lines(a)
    top = max(0, len(lines) - BODY_LINES) if ui.follow else min(ui.scroll, max(0, len(lines) - BODY_LINES))
    for n, (line, c) in enumerate(lines[top:top + BODY_LINES]):
        text(img, 4, BODY_TOP + n * 9, line, c, upper=False)
    if len(lines) > BODY_LINES:                      # scroll position
        span = BODY_LINES * 9
        h = span * BODY_LINES // len(lines)
        y = BODY_TOP + (span - h) * top // max(1, len(lines) - BODY_LINES)
        d.line([LW - 2, y, LW - 2, y + h], fill=DIM)
    button(img, d, NEW_BTN, "NEW", DIM, ui.lit("new"))
    if a:                                            # answers spoken (VOICE) or on screen only (TEXT)
        can = bool(a.speaker and a.speaker.available)
        label = (a.speaker.short if a.speaker else "VOICE") if a.spoken else "TEXT"
        button(img, d, MODE_BTN, label + " >",
               (AMBER if a.spoken else GREEN) if can else FAINT, ui.lit("mode"))
    state = a.state if a else "idle"
    if state == "idle" and a and a.talking:
        button(img, d, ASK_BTN, "SPEAKING - TAP TO STOP", AMBER, ui.lit("ask"))
    elif state == "listening":
        d.rectangle(ASK_BTN, outline=RED)
        x0, y0, x1, y1 = ASK_BTN
        d.rectangle([x0 + 3, y1 - 6, x0 + 3 + int((x1 - x0 - 6) * a.level), y1 - 4], fill=RED)
        text(img, (x0 + x1) // 2, y0 + 6, "LISTENING - STOP", RED if blink() else AMBER, align="c")
    elif state == "transcribing":
        button(img, d, ASK_BTN, "HEARING YOU...", AMBER)
    elif state == "thinking":
        secs = int(time.monotonic() - a.started)
        button(img, d, ASK_BTN, f"THINKING {secs}s - CANCEL", AMBER, ui.lit("ask"), upper=False)
    else:
        button(img, d, ASK_BTN, "ASK", GREEN if a else FAINT, ui.lit("ask"), scale=2)


def render_approval(img, d, req, ui):
    text(img, LW // 2, 3, "CLAUDE WANTS TO", AMBER if blink() else RED, scale=2, align="c")
    what = {"Bash": "RUN A COMMAND:", "Edit": "EDIT A FILE:", "Write": "WRITE A FILE:",
            "MultiEdit": "EDIT A FILE:"}.get(req["tool"], f"USE {req['tool'].upper()}:")
    text(img, 4, 22, what, GREEN)
    for n, line in enumerate(wrap(req["detail"])[:10]):
        text(img, 4, 32 + n * 9, line, GREEN if n else AMBER, upper=False)
    left = max(0, int(req["deadline"] - time.monotonic()))
    text(img, LW // 2, 117, f"NO ANSWER = DENY IN {left}s", DIM, align="c", upper=False)
    button(img, d, DENY_BTN, "DENY", RED, ui.lit("deny"), scale=2)
    button(img, d, ALLOW_BTN, "ALLOW", GREEN, ui.lit("allow"), scale=2)


# The terminal is drawn at 2x instead of 3x like everything else (smaller text, 66x26),
# on its own picture laid over the finished frame below the tab bar.
TERM_SCALE = 2
TERM_Y = (TOP + 2) * SCALE                                            # panel pixels
TERM_X = (W - terminal.COLS * terminal.CELL_W * TERM_SCALE) // 2
TERM_W, TERM_H = (W - 2 * TERM_X) // TERM_SCALE, (H - TERM_Y) // TERM_SCALE
TERM_ROW = terminal.CELL_H * TERM_SCALE / SCALE                       # one text line, in drawing pixels


def page_terminal(img, d, ui):
    """Draws the 3x parts on img; returns the 2x terminal picture for render() to lay over."""
    if ui.term is None:
        text(img, LW // 2, 80, "TERMINAL IS NOT AVAILABLE", DIM, align="c")
        return None
    t = Image.new("RGB", (TERM_W, TERM_H), BG)
    td = ImageDraw.Draw(t)
    ui.term.draw(t, td, 0, 1)
    note = "SCROLLED BACK" if ui.term.scrolled else (
        "PLUG IN A KEYBOARD" if ui.keyboards is not None and not ui.keyboards.count else "")
    if note:
        td.rectangle([TERM_W - text_width(note) - 4, 0, TERM_W - 1, 9], fill=BG, outline=AMBER)
        text(t, TERM_W - 2, 2, note, AMBER, align="r")
    return t


def page_map(img, d, feed, ui):
    """Moving map (mappage.py), with speed and gear bottom right."""
    fix = feed.gps_now() if hasattr(feed, "gps_now") else getattr(feed, "gps", {}) or {}
    ui.map.draw(img, d, fix, list(getattr(feed, "track", [])), MPH, ui.lit)
    data = (feed.latest or {}).get("data", {})
    if fix.get("speed_kmh") is not None:
        val = fmt("speed_kmh", fix["speed_kmh"])
        g = data.get("gear") if feed.engine_on else None
        u = unit("speed_kmh")
        gw = text_width(fmt("gear", g), 2) + 6 if g is not None else 0
        x = LW - 3 - gw - text_width(u) - 2 - text_width(val, 2)
        d.rectangle([x - 3, LH - 17, LW, LH - 1], fill=BG)
        if g is not None:
            text(img, LW - 3, LH - 15, fmt("gear", g), AMBER if g else DIM, scale=2, align="r")
        right = LW - 3 - gw
        text(img, x, LH - 15, val, GREEN, scale=2)
        text(img, right - text_width(u), LH - 8, u, DIM, upper=False)


def render(feed, page, ui=None):
    """Page number -> 800x480 RGB array."""
    img = Image.new("RGB", (LW, LH), BG)
    d = ImageDraw.Draw(img)
    tab_bar(img, d, feed, page, ui)
    name = PAGES[page]
    term = None
    try:
        term = draw_page(img, d, feed, name, ui)
    except Exception as e:                 # a broken page must never take the whole screen down
        page_error(img, d, name, e)
    out = to_panel(img)
    return overlay(out, term, TERM_X, TERM_Y, TERM_SCALE) if term else out


_errors_logged = {}


def page_error(img, d, name, e):
    """Draw a short error in place of a page that raised, and log it (once a minute per page)."""
    import traceback
    now = time.monotonic()
    if now - _errors_logged.get(name, -99) > 60:
        _errors_logged[name] = now
        print(time.strftime("%H:%M:%S"), f"display: {name} page failed:", flush=True)
        traceback.print_exc()
    d.rectangle([0, TOP + 1, LW - 1, LH - 1], fill=BG)
    text(img, 4, TOP + 6, f"{name.upper()} PAGE ERROR", RED, scale=2)
    for i, line in enumerate(wrap(f"{type(e).__name__}: {e}", 42)[:6]):
        text(img, 4, TOP + 28 + i * 9, line, DIM, upper=False)
    text(img, 4, LH - 10, "OTHER TABS STILL WORK. DETAILS IN THE LOG.", DIM)


def draw_page(img, d, feed, name, ui):
    """Draw one page under the tab bar; returns the TERM page's overlay (or None)."""
    term = None
    if name == "status":
        page_status(img, d, feed, ui)
    elif name == "claude":
        page_claude(img, d, ui or UI(feed))
    elif name == "spotify":
        page_spotify(img, d, ui or UI(feed))
    elif name == "terminal":
        term = page_terminal(img, d, ui or UI(feed))
    elif name == "map":
        page_map(img, d, feed, ui or UI(feed))
    else:
        if name == "gauges":
            page_gauges(img, d, feed, ui)
        else:
            page_more(img, d, feed)
    return term


class UI(screens.Pressable):
    """Which page or full-screen touch screen is showing, and what taps do.
    The tab bar switches pages; gauges/readings: tap the page = the other one."""

    def __init__(self, feed, assistant=None, player=None, term=None, keyboards=None, router=None,
                 navigator=None, screen_settings=None):
        super().__init__()
        self.router = router
        self.modem = None                  # cell.Modem, set by main()
        self.feed = feed
        self.assistant = assistant
        self.player = player
        self.term = term
        self.keyboards = keyboards
        self.vis = Visualiser()
        self.audio = AudioTap()
        self.scroll, self.follow = 0, True      # Claude page: follow = stick to the newest line
        self.page = 0
        self.drive_mode = 0                # index into DRIVE_MODES
        self.map = mappage.MapView()
        self.screen = screen_settings      # screen.Screen: brightness / auto-dim / colours
        self.nav = navigator               # nav.Navigator: routes on the MAP page
        self.map.nav = navigator
        self._nav_fix = None
        self.stack = []                    # open touch screens, top last
        self.cal = screens.Calibration.load()
        self.shutdown_armed = False        # finger came down on SHUT DOWN: show the countdown at once
        self._term_y = None                # TERM page: where the dragging finger was, in drawing pixels
        self.suggester = None              # suggest.Suggester: the MUSIC page's SUGGEST

    def snapshot(self):
        return self.page, list(self.stack)

    def restore(self, snap):
        self.page, self.stack = snap[0], list(snap[1])

    @property
    def no_hold(self):
        return bool(self.stack) and getattr(self.stack[-1], "no_hold", False)

    def goto(self, name):
        self.page = PAGES.index(name)
        if name == "claude" and self.assistant:
            threading.Thread(target=self.assistant.check_login, args=(True,), daemon=True).start()
        if self.player:
            self.player.visible = name == "spotify"
            if name == "spotify":
                self.player.refresh()
        if name == "terminal" and self.term:
            self.term.start()

    def tab(self, i):
        """DRIVE again flips gauges <-> readings; other tabs open their page."""
        pages = TABS[i][1]
        if PAGES[self.page] in pages and len(pages) > 1:
            self.goto(pages[(pages.index(PAGES[self.page]) + 1) % len(pages)])
        else:
            self.goto(pages[0])

    def calibration_screen(self):
        def done(cal):
            self.cal = cal
        return screens.CalibrateScreen(done)

    def calibrate(self):
        self.stack.append(self.calibration_screen())

    def _result(self, r):
        if r == "close":
            self.stack.pop()
        elif r == "done":                  # finished with all of them (a song picked from a list)
            self.stack.clear()
        elif isinstance(r, tuple) and r[0] == "replace":     # this screen hands over to the next
            self.stack.pop()
            self.stack.append(r[1])
        elif r is not None:
            self.stack.append(r)

    def tap(self, raw):
        a = self.assistant
        if a and a.approval:                       # ALLOW / DENY comes first, on any page
            if self.cal:
                x, y = self.cal.map(raw)
                if inside(DENY_BTN, x, y):
                    self.press("deny")
                    a.decide(False)
                elif inside(ALLOW_BTN, x, y):
                    self.press("allow")
                    a.decide(True)
            return
        if self.stack:
            top = self.stack[-1]
            if not getattr(top, "wants_release", False) and self.cal:
                self._result(top.tap(*self.cal.map(raw)))
            return
        if not self.cal:                           # nothing can be aimed at yet
            self.calibrate()
            return
        x, y = self.cal.map(raw)
        name = PAGES[self.page]
        if y < TAB_TOUCH:
            i = next((i for i, r in enumerate(tab_rects()) if x <= r[2] + 1), len(TABS) - 1)
            self.press(("tab", i))
            self.tab(i)
        elif name == "gauges" and (hit := next((a for a, r in LAYOUT_TAPS[drive_layout(self)].items()
                                                if inside(r, x, y)), None)):
            self.press(hit)
            if hit == "drive":
                self.drive_mode = (self.drive_mode + 1) % len(DRIVE_MODES)
            elif hit == "mpg":
                self.stack.append(gas.GasScreen(self.feed, self.screen, set_units))
            elif hit == "speed":
                set_units(self.screen, speed_unit="kmh" if MPH else "mph")
            else:                                  # the map or music part of a layout: its tab
                self.goto("map" if hit == "map" else "spotify")
        elif name in ("gauges", "more"):
            self.tab(0)
        elif name == "map":
            hit = self.map.tap(x, y)
            if hit:
                self.press(hit)
            if hit == "go":
                self.stack.append(nav.DestinationScreen(self.nav, self.feed))
        elif name == "claude":
            self.claude_tap(x, y)
        elif name == "spotify":
            self.spotify_tap(x, y)
        elif name == "terminal":
            pass                                   # a finger drag scrolls (fingers()), so taps do nothing
        else:
            if inside(WIFI_BTN, x, y):
                self.press("wifi")
                self.stack.append(screens.WifiScreen())
            elif inside(CELL_BTN, x, y):
                self.press("cell")
                self.stack.append(screens.CellScreen(self.modem))
            elif inside(AUDIO_BTN, x, y) and self.router:
                self.press("audio")
                self.stack.append(screens.AudioScreen(self.router))
            elif inside(SCREEN_BTN, x, y) and self.screen:
                self.press("screen")
                self.stack.append(screen.ScreenSettings(
                    self.screen, open_layout=lambda: LayoutScreen(self.screen),
                    layout_name=lambda: dict((k, n) for k, n, _ in LAYOUTS)[drive_layout(self)],
                    open_cal=self.calibration_screen))
            elif inside(SHUTDOWN_BOX, x, y):
                self.shutdown_armed = True         # no page change; the hold does the rest

    def spotify_tap(self, x, y):
        p = self.player
        if not p:
            return
        for rect, name, act in ((PREV_BTN, "prev", p.previous), (PLAY_BTN, "play", p.toggle),
                                (SKIP_BTN, "skip", p.next), (SHUF_BTN, "shuf", p.toggle_shuffle)):
            if inside(rect, x, y):
                self.press(name)
                act()
                return
        if inside(LISTS_BTN, x, y):
            self.press("lists")
            self.stack.append(PlaylistScreen(p))
        elif inside(SUGGEST_BTN, x, y) and self.suggester:
            self.press("suggest")
            self.suggester.suggest(p.item)
        elif p.item and p.show_heart and inside(HEART_TAP, x, y):
            self.press("heart")
            p.like()

    def claude_tap(self, x, y):
        a = self.assistant
        if inside(ASK_BTN, x, y) and a:
            self.press("ask")
            if a.state == "idle":
                self.follow = True
            a.ask_button()
        elif inside(NEW_BTN, x, y) and a:
            self.press("new")
            a.new_chat()
            self.follow = True
        elif inside(MODE_BTN, x, y) and a:
            self.press("mode")
            self.stack.append(screens.VoiceScreen(a))
        elif BODY_TOP <= y < 132:                  # top half scrolls up, bottom half down
            total = len(claude_lines(a))
            top = max(0, total - BODY_LINES) if self.follow else self.scroll
            top = top - 6 if y < 75 else top + 6
            self.scroll = max(0, min(top, total - BODY_LINES))
            self.follow = self.scroll >= total - BODY_LINES

    def key(self, name, mods, caps):
        """A keyboard key: typed into the terminal when it's on screen, else ignored."""
        t = self.term
        if not t or PAGES[self.page] != "terminal" or self.stack or (self.assistant and self.assistant.approval):
            if terminal.KEYLOG:
                print(f"keyboard: {name} dropped: page {PAGES[self.page]}, stack {len(self.stack)}, "
                      f"approval {bool(self.assistant and self.assistant.approval)}, term {bool(t)}", flush=True)
            return
        out = terminal.key_bytes(name, mods, caps, t.app_cursor)
        if isinstance(out, tuple):
            t.scroll(out[1])
        elif out:
            t.write(out)
        if t.wake:
            t.wake.set()

    def fingers(self, raws):
        """Every finger on the screen, each frame. Only the MAP page uses them (drag, pinch);
        fingers that came down on a map button or the tab bar don't move the map."""
        free = not self.stack and self.cal and not (self.assistant and self.assistant.approval)
        if PAGES[self.page] == "terminal" and self.term:
            self.term_drag([self.cal.map(r) for r in raws] if free else [])
            return
        on_map = PAGES[self.page] == "map" and free
        pts = [self.cal.map(r) for r in raws] if on_map else []
        if pts and self.map.gesture_pts is None and (
                any(y < TAB_TOUCH for _, y in pts) or self.map.button_at(*pts[0])):
            pts = []                                   # started on a button: not a drag
        if pts or self.map.gesture_pts is not None:
            self.map.gesture(pts)

    def term_drag(self, pts):
        """TERM page: drag down = back through what scrolled off, like a phone; one line per
        line of text the finger moves."""
        if not pts or pts[0][1] < TAB_TOUCH:
            self._term_y = None
            return
        y = pts[0][1]
        if self._term_y is None:
            self._term_y = y
            return
        lines = int((y - self._term_y) / TERM_ROW)
        if lines:
            self.term.scroll_lines(lines)
            self._term_y += lines * TERM_ROW

    def release(self, raw):
        if self.stack and getattr(self.stack[-1], "wants_release", False):
            self._result(self.stack[-1].release(raw))

    def render(self):
        fix = getattr(self.feed, "gps", None)
        if self.nav and fix is not self._nav_fix:  # keep guidance going whatever page shows
            self._nav_fix = fix
            try:
                self.nav.update(fix)
            except Exception as e:             # a guidance bug must never take the screen down mid-drive
                log(f"nav: update failed: {e!r}")
        # one read: the assistant thread clears .approval the moment ALLOW/DENY is tapped
        req = self.assistant.approval if self.assistant else None
        if req:
            img = Image.new("RGB", (LW, LH), BG)
            render_approval(img, ImageDraw.Draw(img), req, self)
            return to_panel(img)
        if self.stack and getattr(self.stack[-1], "expired", False):    # e.g. CLEAN's 5 s are up
            if getattr(self.stack[-1], "close_all", False):              # (a song picked: back to MUSIC)
                self.stack.clear()
            else:
                self.stack.pop()
        if not self.stack:
            return render(self.feed, self.page, self)
        img = Image.new("RGB", (LW, LH), BG)
        d = ImageDraw.Draw(img)
        try:
            self.stack[-1].render(img, d)
        except Exception as e:             # same for a full-screen screen: close it next tap
            page_error(img, d, type(self.stack[-1]).__name__, e)
            text(img, LW - 4, 2, "TAP TO CLOSE", AMBER, align="r")
            self.stack[-1].tap = lambda x, y: "close"
        return to_panel(img)


# ---- hardware ----

class Framebuffer:
    """The 5" DSI panel's /dev/fbN (vc4drmfb, 800x480, RGB565).

    Drawing goes through mmap and only rewrites rows that changed."""

    def __init__(self):
        self.path = None
        for dev in sorted(glob.glob("/sys/class/graphics/fb[0-9]*")):
            name = open(f"{dev}/name").read().strip().lower()
            size = open(f"{dev}/virtual_size").read().strip()
            if size in (f"{W},{H}", f"{H},{W}"):
                self.path = "/dev/" + os.path.basename(dev)
                self.bpp = int(open(f"{dev}/bits_per_pixel").read())
                self.stride = int(open(f"{dev}/stride").read())
                self.size = tuple(int(n) for n in size.split(","))
                break
        if not self.path:
            raise RuntimeError(f"no {W}x{H} framebuffer (is the DSI screen connected?)")
        if self.bpp != 16:
            raise RuntimeError(f"{self.path}: expected 16 bpp, got {self.bpp}")
        self.f = open(self.path, "r+b", buffering=0)
        self.mem = mmap.mmap(self.f.fileno(), self.stride * self.size[1])
        self.prev = None

    def show(self, a):
        if self.size != (W, H):            # panel left in portrait: turn the picture
            a = np.rot90(a)
        a = a.astype(np.uint16)
        px = (((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)).astype("<u2")
        if self.prev is None:
            rows = np.arange(px.shape[0])
        else:
            rows = np.nonzero((px != self.prev).any(axis=1))[0]
        if rows.size:
            line = px.shape[1] * 2
            first, last = rows[0], rows[-1] + 1
            block = px[first:last]
            if self.stride == line:
                self.mem[first * line:last * line] = block.tobytes()
            else:
                for i, r in enumerate(range(first, last)):
                    self.mem[r * self.stride:r * self.stride + line] = block[i].tobytes()
        self.prev = px


def is_touch(dev, ec):
    """A touchscreen: reports BTN_TOUCH and absolute X/Y (the DSI panel's ft5x06)."""
    caps = dev.capabilities()
    abs_codes = [c[0] if isinstance(c, tuple) else c for c in caps.get(ec.EV_ABS, [])]
    return ec.BTN_TOUCH in caps.get(ec.EV_KEY, []) and ec.ABS_X in abs_codes


def watch_touch(touch, log):
    """Feeds finger down/up and raw positions from the touch controller to `touch`."""
    try:
        import evdev
    except ImportError:
        log("python3-evdev missing: touch disabled")
        return
    ec = evdev.ecodes
    while True:
        found = [d for d in map(evdev.InputDevice, evdev.list_devices()) if is_touch(d, ec)]
        found.sort(key=lambda d: d.absinfo(ec.ABS_X).max != W - 1)    # the panel's own first
        dev = found[0] if found else None
        if not dev:
            time.sleep(10)
            continue
        log(f"touch: {dev.name} on {dev.path}")
        x = y = 0
        change = None
        slot, fingers = 0, {}               # multi-touch: slot -> [tracking id, x, y]
        try:
            for ev in dev.read_loop():
                if ev.type == ec.EV_ABS:
                    if ev.code == ec.ABS_X:
                        x = ev.value
                    elif ev.code == ec.ABS_Y:
                        y = ev.value
                    elif ev.code == ec.ABS_MT_SLOT:
                        slot = ev.value
                    elif ev.code == ec.ABS_MT_TRACKING_ID:
                        if ev.value < 0:
                            fingers.pop(slot, None)
                        else:
                            fingers[slot] = [ev.value, x, y]
                    elif ev.code == ec.ABS_MT_POSITION_X and slot in fingers:
                        fingers[slot][1] = ev.value
                    elif ev.code == ec.ABS_MT_POSITION_Y and slot in fingers:
                        fingers[slot][2] = ev.value
                elif ev.type == ec.EV_KEY and ev.code == ec.BTN_TOUCH:
                    change = ev.value == 1
                elif ev.type == ec.EV_SYN and ev.code == ec.SYN_REPORT:
                    if change is not None:          # position arrives in the same report
                        touch.press(change, (x, y))
                        change = None
                    else:
                        touch.move((x, y))
                    touch.set_fingers([(f[1], f[2]) for _, f in sorted(fingers.items())])
        except OSError:
            time.sleep(2)


class Touch:
    """Turns finger down/up into events: ("tap", raw_pos) the moment a finger
    lands (that's what makes it feel quick) and ("release", averaged_pos) when
    it lifts. poll() also reports how long the finger has been held, for
    hold-to-shut-down. Resistive panels flicker 'up' for a moment during a long
    press, so a release only counts after DEBOUNCE."""
    DEBOUNCE = 0.12

    def __init__(self, wake):
        self.wake = wake
        self.lock = threading.Lock()
        self.down_at = None      # when the current press started
        self.up_at = None        # when the finger last lifted (pending debounce)
        self.samples = []
        self.events = collections.deque()
        self.fingers = []        # every finger's raw position right now (multi-touch)

    def press(self, down, pos=(0, 0)):
        now = time.monotonic()
        with self.lock:
            if down:
                continuing = self.down_at is not None and (
                    self.up_at is None or now - self.up_at < self.DEBOUNCE)
                if not continuing:
                    if self.down_at is not None:          # an unfinished release first
                        self._released()
                    self.down_at = now
                    self.samples = [pos]
                    self.events.append(("tap", pos))
                self.up_at = None
            elif self.down_at is not None:
                self.up_at = now
        self.wake.set()

    def set_fingers(self, positions):
        with self.lock:
            changed = positions != self.fingers
            self.fingers = positions
        if changed:
            self.wake.set()

    def contacts(self):
        with self.lock:
            return list(self.fingers)

    def move(self, pos):
        with self.lock:
            if self.down_at is not None and self.up_at is None and len(self.samples) < 200:
                self.samples.append(pos)

    def _released(self):
        xs, ys = zip(*self.samples)
        # drop the first and last readings: the edges of a press are the noisiest
        if len(xs) > 4:
            xs, ys = xs[1:-1], ys[1:-1]
        self.events.append(("release", (sum(xs) / len(xs), sum(ys) / len(ys))))
        self.down_at = self.up_at = None

    def poll(self):
        """-> seconds held so far, or None if not touching."""
        now = time.monotonic()
        with self.lock:
            if self.down_at is None:
                return None
            if self.up_at is not None and now - self.up_at >= self.DEBOUNCE:
                self._released()
                return None
            return (self.up_at or now) - self.down_at

    def drain(self):
        with self.lock:
            out = list(self.events)
            self.events.clear()
        return out


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def show_logo(fb):
    """The boot logo (intro.py) on the panel at once: one frame, no waiting - the
    dashboard replaces it as soon as it's ready. Its copy for the next boot's early
    picture (system/splash.sh) is refreshed in the background."""
    try:
        import intro
        logo = intro.still()
        fb.show(logo)
        threading.Thread(target=intro.save_splash, kwargs={"panel": logo}, daemon=True).start()
    except Exception as e:                 # never let it keep the dashboard from starting
        log(f"logo: {e!r}")


def main():
    if "--png" in sys.argv:
        args = sys.argv[sys.argv.index("--png") + 1:]
        page = args[1] if len(args) > 1 else "gauges"
        if page == "shutdown":
            a = render_shutdown(0.6)
        elif page == "intro":
            import intro
            a = intro.still()
        else:
            feed = Feed()
            player = spotify.Player() if page == "spotify" else None
            time.sleep(3 if player else 1.5)
            a = render(feed, PAGES.index(page), UI(feed, None, player))
        Image.fromarray(a).save(args[0])
        return

    warned = False
    while True:                            # wait quietly if the screen isn't fitted
        try:
            fb = Framebuffer()
            break
        except RuntimeError as e:
            if not warned:
                log(e)
                warned = True
            time.sleep(60)
    log(f"lcd: {fb.path} {fb.size[0]}x{fb.size[1]} {fb.bpp} bpp")
    display_settings = screen.Screen()     # first: its colour scheme (Ian's: amber) colours the intro too
    set_units(None, **display_settings.settings)   # mph/km/h and fuel units from screen.json
    show_logo(fb)                          # until the dashboard's first frame, a few seconds on
    feed = Feed()
    wake = threading.Event()
    try:
        import voice
        speech = voice.Voice()             # one speech model, for the AI page and navigation
    except Exception as e:
        log(f"voice unavailable: {e}")
        speech = None
    try:
        import assistant
        helper = assistant.Assistant(wake, speech)
    except Exception as e:                 # the dashboard must work without it
        log(f"claude page unavailable: {e}")
        helper = None
    try:
        player = spotify.Player(wake, log, watch=True)
    except Exception as e:                 # same: the dashboard comes first
        log(f"spotify page unavailable: {e}")
        player = None
    term = terminal.Terminal(wake, log=log)
    router = bt.Router(log, player)      # AUX / Bluetooth stereo; AUX-only if Bluetooth fails
    ui = UI(feed, helper, player, term, router=router, navigator=nav.Navigator(speech),
            screen_settings=display_settings)
    ui.keyboards = terminal.Keyboards(ui.key, log)
    ui.modem = cell.Modem(log)
    if helper:
        try:
            import speech as speech_out
            helper.speaker = speech_out.Speaker(speech_out.MusicHold(player), wake=wake)
            threading.Thread(target=helper.speaker.load, daemon=True).start()     # voice ready before the first answer
        except Exception as e:
            log(f"spoken answers unavailable: {e}")
        warm = threading.Timer(60, helper.prewarm)   # the first question's claude, once start-up is done (fast boot)
        warm.daemon = True
        warm.start()
    if player:
        import mediakeys
        import suggest
        ui.suggester = suggest.Suggester(log, wake)
        mediakeys.MediaKeys(player, log)   # the stereo's buttons (Bluetooth AVRCP) and media keys
    touch = Touch(wake)
    threading.Thread(target=watch_touch, args=(touch, log), daemon=True).start()

    snap, undone = ui.snapshot(), True
    while True:
        started = time.monotonic()
        held = touch.poll()
        for kind, pos in touch.drain():
            if kind == "tap":
                snap, undone = ui.snapshot(), False
                ui.shutdown_armed = False
                ui.tap(pos)
            else:
                ui.shutdown_armed = False
                ui.release(pos)
        ui.fingers(touch.contacts())       # drag / pinch on the MAP page
        display_settings.tick(feed.gps)    # backlight: settings + auto-dim by daylight
        progress = hold_progress(held, ui)
        if progress is not None:
            if not undone:                 # a hold, not a tap: take back what the tap did
                ui.restore(snap)
                undone = True
            if held >= HOLD_FIRE:
                fb.show(render_shutdown(message=("SHUTTING DOWN", "WAIT 15 SECONDS,\nTHEN SWITCH THE CAR OFF")))
                if shutdown(log):
                    while True:            # systemd stops us from here
                        time.sleep(60)
                fb.show(render_shutdown(message=("SHUTDOWN FAILED", "SEE JOURNALCTL\n-U NIGHTRIDER-DISPLAY")))
                time.sleep(5)
                while touch.poll() is not None:     # wait for the finger to lift
                    time.sleep(0.1)
                touch.drain()
                continue
            fb.show(render_shutdown(progress))
        else:
            fb.show(ui.render())
        busy = ui.stack or (helper and (helper.busy or helper.approval))
        dancing = player and player.visible and player.playing and not ui.stack   # the visualiser
        live = not ui.stack and PAGES[ui.page] in LIVE_PAGES
        period = 0.05 if held is not None or busy else 1 / LIVE_FPS if live or dancing else 1 / FPS
        wake.wait(max(0.0, period - (time.monotonic() - started)))   # frame time counts towards it
        wake.clear()


if __name__ == "__main__":
    main()
