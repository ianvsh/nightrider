"""Which gear the 924S is in, from engine RPM and GPS road speed.

In gear, RPM / speed is fixed by the gearbox, final drive and tyre size, so each
gear has its own "RPM per km/h". The starting values below are estimates
(944-type 5-speed ratios 3.50 / 2.06 / 1.40 / 1.03 / 0.83, final drive 3.89,
195/65 R15 tyres) - not checked against Ian's car. The finder nudges each gear's
value towards what it sees while cruising steadily and saves what it learned to
~/.config/datalogger/gears.json (rarely: the SD card).

Gear 0 = not in a gear: stopped, crawling, clutch in, coasting in neutral or
mid-shift.

    python3 gear.py            # learned values vs. estimates
    python3 gear.py --reset    # forget what was learned
"""
import collections
import json
import math
import os
import sys
import time

ESTIMATE = {1: 116.3, 2: 68.4, 3: 46.5, 4: 34.4, 5: 27.6}   # RPM per km/h
PATH = os.path.expanduser("~/.config/datalogger/gears.json")
MIN_KMH = 5               # slower than this: gear 0 (GPS speed is too rough)
MIN_RPM = 1000            # under this while moving = clutch in / neutral
MAX_OFF = 0.12            # a gear matches within 12% (also at most half way to the next)
CONFIRM_SECS = 0.25       # a new gear must match this long before it's shown
STEADY_SECS = 2.0         # RPM must be flat this long to learn from a sample
HOLD_SECS = 1.0           # keep showing a gear this long through a brief mismatch
LEARN_PER_SEC = 0.08      # how far a second of steady cruising moves a gear's value
SAVE_SECS = 300


class GearFinder:
    def __init__(self, path=PATH, log=print):
        self.path = path
        self.log = log
        self.ratios = dict(ESTIMATE)
        self._load()
        self.gear = 0             # what we report
        self.candidate, self.since = 0, 0
        self.last_match = 0
        self.last_learn = None
        self.rpms = collections.deque()             # (time, rpm) over the last STEADY_SECS
        self.dirty_since = None
        self.saved = dict(self.ratios)

    def _load(self):
        try:
            with open(self.path) as f:
                saved = json.load(f)
            ratios = {int(g): float(r) for g, r in saved["rpm_per_kmh"].items()}
            if set(ratios) == set(ESTIMATE) and all(ratios[g] > ratios[g + 1] for g in range(1, 5)):
                self.ratios = ratios
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def match(self, rpm, kmh):
        """The gear RPM/speed fits, or 0."""
        if kmh is None or rpm is None or kmh < MIN_KMH or rpm < MIN_RPM:
            return 0
        r = math.log(rpm / kmh)
        g = min(self.ratios, key=lambda g: abs(r - math.log(self.ratios[g])))
        off = abs(r - math.log(self.ratios[g]))
        gaps = [abs(math.log(self.ratios[g] / self.ratios[n])) / 2 for n in (g - 1, g + 1) if n in self.ratios]
        return g if off <= min([MAX_OFF] + gaps) else 0

    def update(self, now, rpm, kmh, accel=0.0):
        """One sample (any rate). accel = GPS km/h per second. Returns the gear, 0-5."""
        if rpm is not None:
            self.rpms.append((now, rpm))
        while self.rpms and now - self.rpms[0][0] > STEADY_SECS:
            self.rpms.popleft()
        g = self.match(rpm, kmh)
        if g != self.candidate:
            self.candidate, self.since = g, now
        settled = now - self.since >= CONFIRM_SECS
        if g:
            self.last_match = now
            if settled:
                self.gear = g
                if self._steady(accel, kmh, now):
                    self._learn(g, rpm / kmh, now)
                    return self.gear
        elif settled and now - self.last_match >= HOLD_SECS:
            self.gear = 0
        self.last_learn = None
        return self.gear

    def _steady(self, accel, kmh, now):
        """Cruising: speed and RPM both flat, so the 1 Hz GPS isn't lagging RPM."""
        if abs(accel) > 1.5 or kmh < 15 or not self.rpms or now - self.rpms[0][0] < STEADY_SECS * 0.9:
            return False
        rpms = [r for _, r in self.rpms]
        return max(rpms) / max(min(rpms), 1) < 1.04

    def _learn(self, g, ratio, now):
        dt = min(now - self.last_learn, 1.0) if self.last_learn is not None else 0.0
        self.last_learn = now
        new = self.ratios[g] * (ratio / self.ratios[g]) ** (LEARN_PER_SEC * dt)
        neighbours_ok = (g == 1 or new < self.ratios[g - 1]) and (g == 5 or new > self.ratios[g + 1])
        if neighbours_ok:
            self.ratios[g] = new
            if abs(new / self.saved[g] - 1) > 0.005:
                self.dirty_since = self.dirty_since or now
        if self.dirty_since and now - self.dirty_since >= SAVE_SECS:
            self.save()

    def save(self):
        if not self.dirty_since:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"rpm_per_kmh": {g: round(r, 2) for g, r in self.ratios.items()},
                           "updated": time.strftime("%Y-%m-%d %H:%M")}, f, indent=1)
            os.replace(tmp, self.path)
            self.saved = dict(self.ratios)
            self.dirty_since = None
            self.log("gears: learned " + ", ".join(f"{g}: {r:.1f}" for g, r in self.ratios.items()) + " rpm per km/h")
        except OSError as e:
            self.log(f"gears: can't save: {e}")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        try:
            os.remove(PATH)
        except FileNotFoundError:
            pass
        print("gears: back to the estimates")
    f = GearFinder()
    print("gear  rpm per km/h  (estimate)  rpm at 100 km/h")
    for g, r in f.ratios.items():
        print(f"{g:>4}  {r:12.1f}  ({ESTIMATE[g]:8.1f})  {r * 100:9.0f}")
