"""Fuel used and MPG, estimated from the DME's airflow (GET /api/fuel, the
DRIVE page's MPG tile and the GAS screen).

There's no fuel flow sensor. Fuel = air mass / air-fuel ratio. Air mass comes
from the DME's airflow volume (airflow_cfm) and the air's density (baro_kpa,
intake_c): the DME's own g/s reading (airflow_gs) is ~1.9x what the CFM gives
and more than a 2.5 L engine can breathe, so it's only the fallback. Ratio:
the wideband reading (afr) when there is one, else 14.7 (stoichiometric; the
narrowband O2 keeps it there in closed loop, so cold starts and full throttle,
which run richer, read a little low). Overrun fuel cut-off (injector pulse ~0
at speed) counts as no fuel. Distance comes from the store's odometer (odometer.py).

The airflow reading may be off by some percent, so fill-ups calibrate it: FILL
TANK (always to the pump's click) records the gallons pumped. Between two
fill-ups the pump says how much was really used, and the correction factor
becomes (all gallons pumped) / (all gallons estimated) over every full tank so
far. The first fill-up only starts counting.

Fed live by the logger (a listener on each sample, so no lag from the
database's 5 s commits). Totals are saved to FILE every minute and on fill-ups;
a crash loses at most a minute of counting.
"""
import collections
import json
import os
import threading
import time

FILE = os.path.expanduser("~/.config/datalogger/fuel.json")
STOICH = 14.7
GRAMS_PER_GALLON = 745.0 * 3.785        # petrol ~745 g/L
KM_PER_MILE = 1.609344
L_PER_CF = 28.3168
WINDOW_SECS = 10.0                      # live MPG: rolling average over this long
MOVING_MPH = 3.0                        # slower than this: show gal/h, not MPG
MAX_GAP = 2.0                           # s between samples beyond which nothing is counted
CUT_MS, CUT_RPM = 0.3, 1200             # injector pulse below this above this rpm = fuel cut
MIN_CAL_MILES = 20                      # a tank shorter than this doesn't change the calibration
CAL_RANGE = (0.5, 2.0)
# Range left (GAS screen). The 924S tank is 66 L = 17.4 US gal (spec sheets agree);
# Ian has never put in more than 13, so filling at the light leaves a few gallons.
# The low-fuel light comes on with roughly 2 gal (~8 L) left, by owners' accounts.
# "tank_gallons" in fuel.json overrides the size.
TANK_GALLONS = 17.4
LIGHT_GALLONS = 2.0
RANGE_MIN_MILES = 10                    # this tank's MPG once it has this many miles, before that recent fills'
SAVE_EVERY = 60
FILLS_KEPT = 20


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def air_gs(data):
    """Air mass flow, g/s (None = no airflow reading)."""
    cfm = data.get("airflow_cfm")
    if num(cfm):
        kpa = data.get("baro_kpa") if num(data.get("baro_kpa")) else 101.3
        c = data.get("intake_c") if num(data.get("intake_c")) else 20.0
        density = kpa * 1000 / (287.05 * (c + 273.15))          # kg/m3 = g/L
        return max(cfm, 0.0) * L_PER_CF / 60 * density
    air = data.get("airflow_gs")
    return max(air, 0.0) if num(air) else None


def fuel_grams(data, dt):
    """Fuel burnt over dt seconds at this sample's readings (None = can't tell)."""
    air = air_gs(data)
    if air is None:
        return None
    pw, rpm = data.get("injector_ms"), data.get("rpm") or 0
    if isinstance(pw, (int, float)) and pw < CUT_MS and rpm > CUT_RPM:
        return 0.0
    afr = data.get("afr")
    afr = afr if isinstance(afr, (int, float)) and 9 <= afr <= 20 else STOICH
    return air / afr * dt


def mpg(miles, gallons):
    return round(miles / gallons, 1) if gallons > 0.001 and miles > 0 else None


class Fuel:
    def __init__(self, path=FILE, log=print):
        self.path, self.log = path, log
        self.lock = threading.Lock()
        s = self._load()
        self.tank_g = float(s.get("tank_g", 0.0))         # raw (uncalibrated) grams since the last fill-up
        self.tank_km = float(s.get("tank_km", 0.0))
        self.tank_since = s.get("tank_since")             # ts of the last fill-up (None = never)
        self.cal_pumped = float(s.get("cal_pumped", 0.0))  # gallons over full tanks used for calibration
        self.cal_est = float(s.get("cal_est", 0.0))        # raw estimated gallons over the same tanks
        self.fills = s.get("fills", [])
        self.drive = s.get("drive", {"session": None, "g": 0.0, "km": 0.0})
        self.tank_gallons = float(s.get("tank_gallons", TANK_GALLONS))
        self.prev = None                                   # (ts, data) of the last engine sample
        self.window = collections.deque()                  # (ts, grams, km) for the live figure
        self.saved_at = time.time()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        s = {"tank_g": round(self.tank_g, 2), "tank_km": round(self.tank_km, 4), "tank_since": self.tank_since,
             "cal_pumped": self.cal_pumped, "cal_est": round(self.cal_est, 4), "fills": self.fills[-FILLS_KEPT:],
             "drive": self.drive}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, self.path)
        self.saved_at = time.time()

    @property
    def factor(self):
        if self.cal_est <= 0:
            return 1.0
        return min(max(self.cal_pumped / self.cal_est, CAL_RANGE[0]), CAL_RANGE[1])

    def gallons(self, grams):
        return grams / GRAMS_PER_GALLON * self.factor

    # ---- counting (logger listener) ----

    def add(self, sample):
        """One logged sample ({ts, session, data}); only engine samples count."""
        ts, session, data = sample["ts"], sample.get("session"), sample.get("data") or {}
        with self.lock:
            if session is None:
                self.prev = None
                return
            if session != self.drive.get("session"):
                self.drive = {"session": session, "g": 0.0, "km": 0.0}
            # distance: the store's odometer (odometer.py: GPS speed, gaps bridged) when
            # the sample carries it, so drive / tank miles match the drive list exactly
            km = sample.get("km")
            g, dt, steady = 0.0, None, False
            if self.prev is not None:
                dt = ts - self.prev[0]
                if 0 < dt <= MAX_GAP:
                    p = self.prev[1]
                    g = fuel_grams(p, dt) or 0.0
                    steady = p.get("speed_kmh") is not None      # not the lump a GPS gap is bridged with
                    if km is None:
                        km = (p.get("speed_kmh") or 0) * dt / 3600
                else:
                    dt = None
            km = km or 0.0
            if g or km:
                self.tank_g += g
                self.tank_km += km
                self.drive["g"] += g
                self.drive["km"] += km
            if dt:                             # the live figure: only the steady part
                self.window.append((ts, g, km if steady else 0.0, dt))
            self.prev = (ts, data)
            while self.window and ts - self.window[0][0] > WINDOW_SECS:
                self.window.popleft()
            if time.time() - self.saved_at > SAVE_EVERY:
                self._save()

    def close(self):
        with self.lock:
            self._save()

    # ---- fill-ups ----

    def fill(self, gallons, now=None):
        """Filled to the click with this many gallons -> the new summary."""
        gallons = float(gallons)
        if not 0 < gallons < 40:
            raise ValueError("gallons out of range")
        now = time.time() if now is None else now
        with self.lock:
            miles = self.tank_km / KM_PER_MILE
            est = self.tank_g / GRAMS_PER_GALLON                 # raw gallons
            entry = {"ts": now, "gallons": gallons}
            if self.tank_since is not None:                      # a full tank measured end to end
                entry.update(miles=round(miles, 1), estimated=round(est * self.factor, 2),
                             mpg=mpg(miles, gallons))
                if miles >= MIN_CAL_MILES and est > 0:
                    self.cal_pumped += gallons
                    self.cal_est += est
            self.fills.append(entry)
            self.fills = self.fills[-FILLS_KEPT:]
            self.tank_g, self.tank_km, self.tank_since = 0.0, 0.0, now
            self._save()
            self.log(f"fuel: filled {gallons:.2f} gal ({entry.get('mpg')} mpg), factor now {self.factor:.3f}")
        return self.summary(now)

    # ---- results ----

    def _range(self, t_mi, t_gal):
        """Estimated fuel and miles left: a full tank (every fill-up is to the click)
        minus what's been burnt since; miles at this tank's MPG, or before it has
        RANGE_MIN_MILES, the MPG measured at the last fill-ups. None before any fill-up."""
        if self.tank_since is None:
            return None
        left = max(self.tank_gallons - t_gal, 0.0)
        measured = [f["mpg"] for f in self.fills[-3:] if f.get("mpg")]
        this_tank = mpg(t_mi, t_gal)
        if this_tank and t_mi >= RANGE_MIN_MILES:
            basis, how = this_tank, "this tank"
        elif measured:
            basis, how = sum(measured) / len(measured), "last fill-ups"
        else:
            basis, how = this_tank, "this tank"
        out = {"gallons_left": round(left, 1), "tank_gallons": self.tank_gallons, "mpg": basis, "basis": how,
               "miles": None, "to_light": None}
        if basis:
            out.update(miles=round(left * basis), to_light=round(max(left - LIGHT_GALLONS, 0.0) * basis))
        return out

    def summary(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            live = [w for w in self.window if now - w[0] <= WINDOW_SECS + 1]
            secs = sum(w[3] for w in live)
            g, km = sum(w[1] for w in live), sum(w[2] for w in live)
            now_mpg = now_gph = None
            if secs >= 1:
                miles, gal = km / KM_PER_MILE, self.gallons(g)
                if miles / secs * 3600 >= MOVING_MPH:
                    now_mpg = mpg(miles, gal) or 99.9          # coasting on fuel cut-off
                else:
                    now_gph = round(gal / secs * 3600, 2)
            d_mi, d_gal = self.drive["km"] / KM_PER_MILE, self.gallons(self.drive["g"])
            t_mi, t_gal = self.tank_km / KM_PER_MILE, self.gallons(self.tank_g)
            return {
                "now_mpg": now_mpg, "now_gph": now_gph,
                "drive": {"session": self.drive.get("session"), "miles": round(d_mi, 2),
                          "gallons": round(d_gal, 3), "mpg": mpg(d_mi, d_gal)},
                "tank": {"since": self.tank_since, "miles": round(t_mi, 1),
                         "gallons": round(t_gal, 2), "mpg": mpg(t_mi, t_gal)},
                "range": self._range(t_mi, t_gal),
                "factor": round(self.factor, 3),
                "calibrated": self.cal_est > 0,
                "fills": list(reversed(self.fills[-5:])),        # newest first
            }
