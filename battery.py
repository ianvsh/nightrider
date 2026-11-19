"""Car battery charge estimate, for when the engine is off (the DRIVE page's
BATTERY row and GET /api/battery).

The battery: Wuldnmar G47 (Amazon B0F213L8XB), LiFePO4, BCI group 47 / H5,
12.8 V nominal, 600 CCA (850 tested), 11 lb, built-in BMS (over-charge,
over-discharge, over-current), 1-year warranty. Wuldnmar doesn't publish its
amp-hours; ~30 Ah is a guess from the weight (Antigravity's 40 Ah H5 is
14.25 lb). The real figure may be on the battery's label: set capacity_ah.

Nothing measures the battery while the car is off: the DME (our only
voltmeter) is powered only once the key turns, and its first reading is
already during cranking. LiFePO4 voltage is also nearly flat from ~20% to
~90% (13.0-13.4 V at rest), so even a reading wouldn't say much. So this
counts charge in and out over time from the logged samples:
- Pi running, engine off (a heartbeat each minute): the Pi + screen + SIM
  HAT draw from the battery (pi_draw_a), plus the car's own parasitic draw.
- Pi off (no samples for over 3 minutes): only the car's parasitic draw.
- Engine running: the DME's battery volts say whether the alternator is
  charging (>= 13.6 V: charge_a, tapering above 90%), holding, or losing
  (< 13.3 V, idling with lights/fan on: idle_drain_a). Each start costs start_ah.
All the rates are estimates, not measurements, so it drifts: `python3 battery.py
set 100` after a full charge on a charger corrects it.
"""
import json
import os
import sqlite3
import sys
import threading
import time

DB_PATH = os.environ.get("NIGHTRIDER_DB", "/var/lib/nightrider/log.db")
FILE = os.path.expanduser("~/.config/nightrider/battery.json")

DEFAULTS = {
    "capacity_ah": 30.0,     # not published: estimated from the weight (see above)
    "pi_draw_a": 0.55,       # Pi 4 + 5" screen + SIM7670G + 12->5 V converter (~7 W), estimated
    "parasitic_a": 0.03,     # the car itself with the key out (clock, radio memory)
    "charge_a": 25.0,        # alternator into the battery while it reads >= CHARGING_V
    "idle_drain_a": 5.0,     # engine running below HOLDING_V: the battery is helping
    "start_ah": 0.1,         # one start: ~200 A for ~2 s
}
CHARGING_V = 13.6
HOLDING_V = 13.3
TAPER_FROM = 90.0            # % where charging current starts to fall off
PI_OFF_GAP = 180             # s without any sample = the Pi was off
SAVE_EVERY = 3600            # s between checkpoint writes (SD card)


def rate_a(settings, session, volts, soc):
    """Battery current in amps (+ = charging) for one stretch of time."""
    if session is None:                                   # engine off, Pi on
        return -(settings["pi_draw_a"] + settings["parasitic_a"])
    if volts is None:
        return 0.0
    if volts >= CHARGING_V:
        return settings["charge_a"] * min(1.0, max(0.0, (100.0 - soc) / (100.0 - TAPER_FROM)))
    if volts < HOLDING_V:
        return -settings["idle_drain_a"]
    return 0.0


class Battery:
    """State of charge, replayed from the samples since the last checkpoint
    (kept in FILE with the settings). Thread-safe; cheap after the first call."""

    def __init__(self, db_path=DB_PATH, path=FILE, log=print):
        self.db_path, self.path, self.log = db_path, path, log
        self.lock = threading.Lock()
        self.saved_at = 0.0
        self.conf = self._load()
        self.settings = {k: float(self.conf.get(k, v)) for k, v in DEFAULTS.items()}
        self.soc = float(self.conf.get("soc", 100.0))
        self.ts = self.conf.get("ts")                     # time the soc is for (None = from the first sample)
        self.last_id = int(self.conf.get("sample_id", 0))
        self.session = self.conf.get("session")
        self.volts = None
        self.parked_since = None                          # ts of the last engine sample

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        conf = dict(self._load(), soc=round(self.soc, 3), ts=self.ts, sample_id=self.last_id,
                    session=self.session)
        for k, v in DEFAULTS.items():
            conf.setdefault(k, v)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(conf, f, indent=1)
        os.replace(tmp, self.path)
        self.saved_at = time.time()

    def _step(self, until, session, volts):
        """Move self.soc on from self.ts to `until`."""
        if self.ts is None:
            self.ts = until
            return
        dt = until - self.ts
        if dt <= 0:
            return
        if dt > PI_OFF_GAP:                               # Pi off: only the car's own draw
            amps = -self.settings["parasitic_a"]
        else:
            amps = rate_a(self.settings, session, volts, self.soc)
        self.soc += amps * dt / 3600 / self.settings["capacity_ah"] * 100
        self.soc = min(100.0, max(0.0, self.soc))
        self.ts = until

    def update(self, now=None):
        """Catch up with new samples -> the estimate as a dict."""
        now = time.time() if now is None else now
        with self.lock:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
            try:
                rows = db.execute("SELECT id, ts, session, json_extract(data, '$.dme_volts') FROM samples "
                                  "WHERE id > ? ORDER BY id", (self.last_id,)).fetchall()
            finally:
                db.close()
            for sid, ts, session, volts in rows:
                self._step(ts, self.session, self.volts)
                if session is not None and session != self.session:     # a new drive: the start
                    self.soc = max(0.0, self.soc - self.settings["start_ah"] / self.settings["capacity_ah"] * 100)
                if session is not None:
                    self.parked_since = ts
                self.last_id, self.session, self.volts = sid, session, volts
            if rows and time.time() - self.saved_at > SAVE_EVERY:
                self._save()
            soc = self.soc                                # now: carry on from the last sample, unsaved
            if self.ts is not None and now > self.ts:
                gap = now - self.ts
                amps = (-self.settings["parasitic_a"] if gap > PI_OFF_GAP
                        else rate_a(self.settings, self.session, self.volts, soc))
                soc = min(100.0, max(0.0, soc + amps * gap / 3600 / self.settings["capacity_ah"] * 100))
            pi_hours = (self.settings["capacity_ah"] * soc / 100
                        / (self.settings["pi_draw_a"] + self.settings["parasitic_a"]))
            if self.parked_since is None:
                db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
                try:
                    self.parked_since = db.execute("SELECT max(end) FROM sessions").fetchone()[0]
                finally:
                    db.close()
            return {"soc_pct": round(soc, 1), "estimated": True,
                    "capacity_ah": self.settings["capacity_ah"],
                    "pi_draw_a": self.settings["pi_draw_a"],
                    "hours_left_with_pi_on": round(pi_hours, 1),
                    "parked_since": self.parked_since}

    def set(self, pct, now=None):
        """Correct the estimate: the battery is at pct% now (e.g. 100 after a charger)."""
        now = time.time() if now is None else now
        self.update(now)
        with self.lock:
            self.soc, self.ts = float(pct), now
            self._save()


def main():
    b = Battery()
    if len(sys.argv) >= 3 and sys.argv[1] == "set":
        b.set(float(sys.argv[2]))
        print(f"battery set to {float(sys.argv[2]):.0f}%")
        return
    est = b.update()
    b._save()
    print(json.dumps(est, indent=1))
    print(f"settings: {b.settings}  (change them in {b.path})")


if __name__ == "__main__":
    main()
