"""Screen settings (SET > SCREEN): brightness, auto-dim by daylight, colour scheme.

- Brightness drives the DSI panel's real backlight (/sys/class/backlight/*,
  0-255, writable by the video group), so dimming saves light, not just pixels.
- Auto-dim works out the sun's height from the GPS position and the clock (no
  internet): full DAY level in daylight, NIGHT level after dusk, blended through
  twilight (sun between +3 and -6 degrees) so it never jumps.
- Colour scheme: GREEN (the original) or AMBER. pixels.to_panel recolours each
  frame (pixels.set_scheme), so no page needs to know.

Settings live in ~/.config/nightrider/screen.json.
"""
import glob
import json
import math
import os
import time

import config
import pixels
import screens
from pixels import AMBER, DIM, FAINT, GREEN, LH, LW, RED, button, inside, text

SETTINGS = os.path.expanduser("~/.config/nightrider/screen.json")
DEFAULTS = {"brightness": 100, "auto": False, "night": 25, "scheme": "green",
            "layout": "classic",
            "speed_unit": "mph",          # DRIVE page speed: "mph" or "kmh" (tap the speed)
            "fuel_units": "imperial"}     # GAS screen + DRIVE fuel column: "imperial" or "metric"         # the DRIVE page layout (display.LAYOUTS), chosen under SET
HOME = config.HOME                 # until there's been a GPS fix (config.toml [map] home)
# Backlight floor = 1%. 2026-10-16: raw 16 left Ian's panel completely dark; 2026-10-18 he
# watched raw 64 down to 16 step by step and chose 48 as the lowest still readable at night.
# 1-100 % maps linearly onto MIN_RAW..max.
MIN_RAW = 48
STEP = 5                           # % per +/- tap; 1 % at a time at and below 5 %
SUN_EVERY = 10                     # s between sun-height checks: auto-dim moves in small steps


def step(value, up, lo=1):
    """The next brightness setting: 5 % at a time above 5 %, 1 % at a time below."""
    if up:
        return min(100, value + (1 if value < 5 else STEP))
    return max(lo, value - (1 if value <= 5 else STEP))


def log(msg):
    print(time.strftime("%H:%M:%S"), "screen:", msg, flush=True)


def sun_elevation(lat, lon, t=None):
    """Sun's height above the horizon in degrees (NOAA-style approximation, ~0.5 deg)."""
    t = time.time() if t is None else t
    d = t / 86400 - 10957.5                      # days since 2000-01-01 12:00 UTC
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians((gmst * 15 + lon - ra) % 360)
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)))


def daylight(elevation):
    """0 (night) .. 1 (day): sun above +3 deg = day, below -6 (end of civil twilight) = night,
    eased in and out (smoothstep) so the fade starts and finishes gently."""
    x = max(0.0, min(1.0, (elevation + 6) / 9))
    return x * x * (3 - 2 * x)


def next_change(lat, lon, now=None, step=300):
    """('SUNSET' / 'SUNRISE', unix time) of the next day/night switch, to the 5 minutes."""
    now = time.time() if now is None else now
    day = sun_elevation(lat, lon, now) > -0.833
    for i in range(1, 2 * 24 * 12):
        t = now + i * step
        if (sun_elevation(lat, lon, t) > -0.833) != day:
            return ("SUNSET" if day else "SUNRISE"), t
    return None, None


class Backlight:
    def __init__(self):
        found = sorted(glob.glob("/sys/class/backlight/*/brightness"))
        self.path = found[0] if found else None
        try:
            with open(os.path.join(os.path.dirname(self.path), "max_brightness")) as f:
                self.max = int(f.read())
        except (OSError, ValueError, TypeError):
            self.max = 255
        self.last = None

    def set(self, pct):
        """1-100 % (fractions too, for smooth fades) -> MIN_RAW..max, so 1 % is the dimmest
        Ian can still read and it never looks off."""
        if not self.path:
            return
        raw = round(MIN_RAW + (self.max - MIN_RAW) * (max(1.0, min(100.0, pct)) - 1) / 99)
        if raw == self.last:
            return
        try:
            with open(self.path, "w") as f:
                f.write(str(raw))
            self.last = raw
        except OSError as e:
            if self.last is not None or not getattr(self, "warned", False):
                log(f"can't set backlight: {e}")
                self.warned = True


class Screen:
    """Settings + applying them. tick() from the display loop (cheap; does the
    sun maths at most once a minute)."""

    def __init__(self, path=SETTINGS, backlight=None):
        self.path = path
        self.settings = dict(DEFAULTS)
        try:
            with open(path) as f:
                self.settings.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        self.backlight = backlight or Backlight()
        self.where = HOME
        self.level = None            # % the backlight is at now
        self.light = 1.0             # daylight 0..1 last worked out
        self._sun_at = 0
        pixels.set_scheme(self.settings["scheme"])

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                json.dump(self.settings, f, indent=1)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            log(f"can't save settings: {e}")

    def change(self, **kw):
        self.settings.update(kw)
        if "scheme" in kw:
            pixels.set_scheme(kw["scheme"])
        self.save()
        self.tick(force=True)

    def target(self):
        """Backlight % right now. Through twilight it fades between the day and night levels
        by ratio, not by difference, because the eye sees brightness that way: an even
        fade to the eye, slowing down as it gets dark instead of rushing the last bit."""
        s = self.settings
        if not s["auto"]:
            return s["brightness"]
        night = max(1, min(s["night"], s["brightness"]))
        day = max(1, s["brightness"])
        return round(night * (day / night) ** self.light, 2)

    def tick(self, fix=None, now=None, force=False):
        now = time.time() if now is None else now
        if fix:
            self.where = (fix["lat"], fix["lon"])
        if force or now - self._sun_at >= SUN_EVERY:
            self._sun_at = now
            self.light = daylight(sun_elevation(*self.where, now))
        level = self.target()
        if level != self.level:
            self.level = level
            self.backlight.set(level)


# ---- the settings screen ----

class CleanScreen(screens.Pressable):
    """CLEAN: the screen goes black for SECS, a small countdown in the corner, so it can
    be wiped. Every touch is ignored (taps, drags, long presses) and it closes itself
    (display.UI.render pops it once `expired`)."""
    SECS = 5
    no_hold = True                     # a finger held down while wiping is not a hold

    def __init__(self):
        super().__init__()
        self.started = time.monotonic()

    @property
    def left(self):
        return max(0.0, self.SECS - (time.monotonic() - self.started))

    @property
    def expired(self):
        return self.left <= 0

    def tap(self, x, y):
        return None

    def render(self, img, d):
        text(img, LW - 4, LH - 10, f"CLEAN {math.ceil(self.left)}", FAINT, align="r")


class ScreenSettings(screens.Pressable):
    BACK = (0, 0, 56, 18)
    CLEAN = (150, 0, 214, 18)          # black for 5 s, for wiping the screen
    ROW_Y = (22, 45, 68, 99)                     # brightness, auto-dim, night level, colours
    MINUS, PLUS = (138, 0, 168, 20), (LW - 34, 0, LW - 4, 20)
    AUTO = (138, 0, LW - 4, 20)
    GREEN_BTN = (4, 0, LW // 2 - 3, 20)
    AMBER_BTN = (LW // 2 + 2, 0, LW - 4, 20)
    LAYOUT = (4, 126, 176, 156)                  # DRIVE LAYOUT > (moved here from SET, Ian 2026-11-12)
    CAL = (180, 126, LW - 4, 156)                # TOUCH CAL

    def __init__(self, screen, open_layout=None, layout_name=None, open_cal=None):
        super().__init__()
        self.screen = screen
        self.open_layout, self.layout_name, self.open_cal = open_layout, layout_name, open_cal

    @staticmethod
    def at(rect, y):
        return rect[0], rect[1] + y, rect[2], rect[3] + y

    def tap(self, x, y):
        s, st = self.screen, self.screen.settings
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        if inside(self.CLEAN, x, y):
            self.press("clean")
            return CleanScreen()
        if inside(self.LAYOUT, x, y) and self.open_layout:
            self.press("layout")
            return self.open_layout()
        if inside(self.CAL, x, y) and self.open_cal:
            self.press("cal")
            return self.open_cal()
        for row, key in ((0, "brightness"), (2, "night")):
            for rect, delta, name in ((self.MINUS, -STEP, "minus"), (self.PLUS, STEP, "plus")):
                if inside(self.at(rect, self.ROW_Y[row]), x, y):
                    self.press((name, row))
                    s.change(**{key: step(st[key], delta > 0, lo=1 if key == "night" else 10)})
                    return None
        if inside(self.at(self.AUTO, self.ROW_Y[1]), x, y):
            self.press("auto")
            s.change(auto=not st["auto"])
            return None
        for rect, scheme in ((self.GREEN_BTN, "green"), (self.AMBER_BTN, "amber")):
            if inside(self.at(rect, self.ROW_Y[3]), x, y):
                self.press(scheme)
                s.change(scheme=scheme)
                return None
        return None

    def render(self, img, d):
        s, st = self.screen, self.screen.settings
        button(img, d, self.BACK, "BACK", GREEN, self.lit("back"))
        button(img, d, self.CLEAN, "CLEAN", GREEN, self.lit("clean"))
        text(img, LW - 4, 6, "SCREEN", DIM, align="r")
        rows = ((0, "BRIGHTNESS" if not st["auto"] else "DAY BRIGHTNESS", "brightness"),
                (2, "NIGHT BRIGHTNESS", "night"))
        for row, label, key in rows:
            y = self.ROW_Y[row]
            on = row == 0 or st["auto"]
            text(img, 4, y + 7, label, GREEN if on else FAINT)
            button(img, d, self.at(self.MINUS, y), "-", GREEN if on else FAINT, self.lit(("minus", row)), scale=2)
            text(img, (self.MINUS[2] + self.PLUS[0]) // 2 + 1, y + 4, f"{st[key]}%", AMBER if on else FAINT,
                 scale=2, align="c")
            button(img, d, self.at(self.PLUS, y), "+", GREEN if on else FAINT, self.lit(("plus", row)), scale=2)
        y = self.ROW_Y[1]
        text(img, 4, y + 7, "AUTO-DIM AT NIGHT", GREEN)
        button(img, d, self.at(self.AUTO, y), "ON" if st["auto"] else "OFF",
               AMBER if st["auto"] else DIM, self.lit("auto"), scale=2)
        # what auto-dim is doing now
        lat, lon = s.where
        what, when = next_change(lat, lon)
        state = "DAY" if s.light >= 1 else "NIGHT" if s.light <= 0 else "TWILIGHT"
        note = (f"NOW {state}, {s.level:.0f}%" if s.level is not None else f"NOW {state}") + \
            (f"  {what} {time.strftime('%H:%M', time.localtime(when))}" if what else "")
        text(img, 4, self.ROW_Y[2] + 22, note if st["auto"] else "BRIGHTNESS STAYS AS SET", DIM)
        y = self.ROW_Y[3]
        button(img, d, self.at(self.GREEN_BTN, y), "GREEN", GREEN, st["scheme"] == "green" or self.lit("green"), scale=2)
        button(img, d, self.at(self.AMBER_BTN, y), "AMBER", AMBER, st["scheme"] == "amber" or self.lit("amber"), scale=2)
        if not s.backlight.path:
            text(img, LW // 2, 116, "NO BACKLIGHT CONTROL FOUND", RED, align="c")
        if self.open_layout:
            name = self.layout_name() if self.layout_name else ""
            button(img, d, self.LAYOUT, f"DRIVE LAYOUT: {name} >", GREEN, self.lit("layout"))
        if self.open_cal:
            button(img, d, self.CAL, "TOUCH CAL", DIM, self.lit("cal"))
