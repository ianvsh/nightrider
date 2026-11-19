"""GAS screen (tap the MPG row on DRIVE): live MPG, this drive, this tank, and
FILL TANK -> a number pad for the gallons pumped. The figures come from the
logger's /api/fuel (fuel.py); a fill-up is POSTed back there."""
import threading
import time

import screens
from pixels import AMBER, DIM, FAINT, GREEN, LW, RED, button, inside, text


def fmt_mpg(v):
    return "--" if v is None else f"{min(v, 99.9):.1f}"


def ago(ts, now=None):
    secs = max(0, (now or time.time()) - ts)
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


class GasScreen(screens.Pressable):
    BACK = (0, 0, 56, 18)
    FILL = (4, 132, LW - 5, 156)

    def __init__(self, feed):
        super().__init__()
        self.feed = feed
        self.note, self.note_colour = "", DIM

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        if inside(self.FILL, x, y):
            self.press("fill")
            return NumPad("GALLONS PUMPED (FILLED TO THE CLICK)", self._fill)
        return None

    def _fill(self, gallons):
        self.note, self.note_colour = "SAVING...", DIM

        def run():
            r = self.feed.fill(gallons)
            if r is None:
                self.note, self.note_colour = "COULDN'T SAVE: LOGGER NOT ANSWERING", RED
                return
            last = (r.get("fills") or [{}])[0]
            if last.get("mpg") is not None:
                self.note = f"{gallons:g} GAL SAVED: THAT TANK DID {fmt_mpg(last['mpg'])} MPG"
            elif last.get("miles") is None:
                self.note = f"{gallons:g} GAL SAVED: COUNTING STARTS NOW"
            else:
                self.note = f"{gallons:g} GAL SAVED"
            self.note_colour = AMBER
        threading.Thread(target=run, daemon=True).start()

    def render(self, img, d):
        f = getattr(self.feed, "fuel", None) or {}
        button(img, d, self.BACK, "BACK", GREEN, self.lit("back"))
        text(img, LW - 4, 6, "GAS", DIM, align="r")
        # now: big
        y = 24
        if f.get("now_mpg") is not None:
            lab, val, unit = "NOW", fmt_mpg(f["now_mpg"]), "MPG"
        elif f.get("now_gph") is not None:
            lab, val, unit = "NOW (STOPPED)", f"{f['now_gph']:.2f}", "GAL/H"
        else:
            lab, val, unit = "NOW", "--", "MPG"
        text(img, 4, y + 9, lab, DIM)
        text(img, LW - 4, y + 14, unit, DIM, align="r", upper=False)
        text(img, LW - 8 - len(unit) * 6, y, val, GREEN if val != "--" else DIM, scale=3, align="r")
        d.line([0, 50, LW, 50], fill=FAINT)
        # this drive / this tank
        cols = (110, 160, LW - 4)                  # right edges: MILES, GAL, MPG
        y = 55
        for x, h in zip(cols, ("MILES", "GAL", "MPG")):
            text(img, x, y, h, FAINT, align="r")
        for i, (lab, key) in enumerate((("THIS DRIVE", "drive"), ("THIS TANK", "tank"))):
            row = f.get(key) or {}
            yy = y + 12 + i * 14
            text(img, 4, yy, lab, DIM)
            vals = ("--" if row.get("miles") is None else f"{row['miles']:.1f}",
                    "--" if row.get("gallons") is None else f"{row['gallons']:.2f}",
                    fmt_mpg(row.get("mpg")))
            for x, v in zip(cols, vals):
                text(img, x, yy, v, AMBER if x == cols[-1] and v != "--" else GREEN if v != "--" else DIM,
                     align="r")
        # range left: what's in the tank (full at the last fill-up, minus what's burnt) x MPG
        rng, yy = f.get("range"), 96
        text(img, 4, yy, "RANGE LEFT", DIM)
        if rng and rng.get("miles") is not None:
            miles, light = rng["miles"], rng["to_light"]
            col = RED if light <= 0 else AMBER if miles < 60 else GREEN
            text(img, 110, yy, f"~{miles} MI", col, align="r")
            text(img, LW - 4, yy, f"{rng['gallons_left']:.1f} GAL LEFT" if light > 0 else "AT THE LIGHT",
                 col if light <= 0 else DIM, align="r")
        else:
            text(img, LW - 4, yy, "AFTER THE FIRST FILL-UP" if not rng else "--", DIM, align="r")
        d.line([0, 106, LW, 106], fill=FAINT)
        # fill-ups and calibration
        fills = f.get("fills") or []
        if fills:
            last = fills[0]
            s = f"LAST FILL {last['gallons']:g} GAL " + ago(last["ts"])
            if last.get("mpg") is not None:
                s += f", {fmt_mpg(last['mpg'])} MPG"
            text(img, 4, 109, s, DIM, upper=False)
        else:
            text(img, 4, 109, "NO FILL-UPS YET", DIM)
        if f.get("calibrated"):
            cal = f"ESTIMATE CORRECTED x{f['factor']:.2f} BY FILL-UPS"
        elif f:
            cal = "ESTIMATE FROM AIRFLOW. 2 FILL-UPS CALIBRATE"
        else:
            cal = "NO FUEL DATA FROM THE LOGGER"
        text(img, 4, 117, cal, FAINT)
        if self.note:
            text(img, 4, 125, self.note[:43], self.note_colour)
        button(img, d, self.FILL, "FILL TANK", AMBER, self.lit("fill"), scale=2)


class NumPad(screens.Pressable):
    """Digits, a point and DEL; OK calls on_done(float) when the number is sensible."""
    KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "0", "DEL"]
    TOP, KEY_H, KEY_W = 27, 31, 58

    def __init__(self, title, on_done, lo=0.5, hi=40):
        super().__init__()
        self.title, self.on_done, self.lo, self.hi = title, on_done, lo, hi
        self.value = ""
        self.bad = False

    def keys(self):
        out = []
        for i, k in enumerate(self.KEYS):
            r, c = divmod(i, 3)
            x0, y0 = 4 + c * (self.KEY_W + 4), self.TOP + r * (self.KEY_H + 2)
            out.append(((x0, y0, x0 + self.KEY_W, y0 + self.KEY_H), k))
        x0 = 4 + 3 * (self.KEY_W + 4)
        half = 2 * (self.KEY_H + 2)
        out.append(((x0, self.TOP, LW - 5, self.TOP + half - 3), "CANCEL"))
        out.append(((x0, self.TOP + half, LW - 5, self.TOP + 2 * half - 3), "OK"))
        return out

    def number(self):
        try:
            v = float(self.value)
        except ValueError:
            return None
        return v if self.lo <= v <= self.hi else None

    def tap(self, x, y):
        for rect, k in self.keys():
            if not inside(rect, x, y):
                continue
            self.press(k)
            self.bad = False
            if k == "CANCEL":
                return "close"
            if k == "OK":
                v = self.number()
                if v is None:
                    self.bad = True
                    return None
                self.on_done(v)
                return "close"
            if k == "DEL":
                self.value = self.value[:-1]
            elif len(self.value) < 5 and not (k == "." and "." in self.value):
                self.value += k
            return None
        return None

    def render(self, img, d):
        text(img, 2, 1, self.title, DIM)
        shown = self.value or ""
        text(img, 4, 10, shown + " GAL" if shown else "", AMBER, scale=2)
        if not shown and int(time.time() * 2) % 2 == 0:
            d.rectangle([4, 22, 13, 23], fill=AMBER)
        if self.bad:
            text(img, LW - 4, 14, f"{self.lo:g}-{self.hi:g} GAL", RED, align="r")
        d.line([0, 25, LW, 25], fill=FAINT)
        for rect, k in self.keys():
            col = AMBER if k == "OK" else RED if k == "CANCEL" else GREEN
            button(img, d, rect, k, col, self.lit(k), scale=1 if len(k) > 1 else 2)
