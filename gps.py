"""GPS from the SIM7670G cellular + GNSS HAT on the GPIO UART (/dev/ttyS0).

The module talks AT commands at 115200. GNSS is off after every power-up, so
this turns it on (AT+CGNSSPWR=1) and asks for NMEA on the same port
(AT+CGNSSTST=1, AT+CGNSSPORTSWITCH=0,1). It only does 1 fix a second
(AT+CGNSSNMEARATE accepts only 1), so `speed_at` extrapolates between fixes.
Run `python3 gps.py` to print fixes.
"""
import collections
import os
import sys
import threading
import time

import serial

PORT = os.environ.get("DATALOGGER_GPS", "/dev/ttyS0")    # "0" = no GPS
BAUD = 115200
STALE_SECS = 3.0          # no fix for this long = no GPS readings
LATENCY_SECS = 0.3        # a fix reaches us roughly this long after its epoch
STILL_KMH = 1.5           # GPS speed wanders a little when parked: below this is 0
KNOTS_TO_KMH = 1.852
INIT = ["AT", "ATE0", "AT+CGNSSPWR=1", "AT+CGNSSTST=1", "AT+CGNSSPORTSWITCH=0,1"]


def checksum_ok(line):
    """'$GNRMC,...*2E' -> True if the XOR checksum matches."""
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, cs = line[1:].partition("*")
    x = 0
    for c in body:
        x ^= ord(c)
    try:
        return x == int(cs[:2], 16)
    except ValueError:
        return False


def degrees(value, hemi):
    """NMEA ddmm.mmmm / dddmm.mmmm + N/S/E/W -> signed decimal degrees."""
    if not value:
        return None
    d, m = divmod(float(value), 100)
    deg = d + m / 60
    return -deg if hemi in ("S", "W") else deg


class GPS:
    def __init__(self, port=PORT, log=print):
        self.port = port
        self.log = log
        self.lock = threading.Lock()
        self.fix = {}             # latest fix readings, see snapshot()
        self.fix_time = 0         # time.time() the latest fix arrived
        self.speeds = collections.deque(maxlen=2)   # (time, km/h) of the last two fixes
        self.connected = False
        self._closing = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="gps").start()
        return self

    def close(self):
        self._closing.set()

    # ---- reading ----

    def _run(self):
        said_fix = False
        while not self._closing.is_set():
            try:
                with serial.Serial(self.port, BAUD, timeout=1) as s:
                    self._init(s)
                    self.connected = True
                    quiet_since = time.time()
                    while not self._closing.is_set():
                        line = s.readline().decode("ascii", "replace").strip()
                        if line.startswith("$"):
                            quiet_since = time.time()
                            if self.handle(line) and not said_fix:
                                f = self.fix
                                self.log(f"gps: fix, {f.get('gps_sats')} satellites")
                                said_fix = True
                        elif time.time() - quiet_since > 10:   # module reset: set it up again
                            self.log("gps: no NMEA for 10 s, re-initialising")
                            self._init(s)
                            quiet_since = time.time()
            except (serial.SerialException, OSError) as e:
                if self.connected:
                    self.log(f"gps: {e}")
                self.connected = False
            self._closing.wait(5)

    def _init(self, s):
        for cmd in INIT:
            s.write((cmd + "\r").encode())
            end = time.time() + 2
            while time.time() < end:
                r = s.readline()
                if b"OK" in r or b"ERROR" in r:   # CGNSSPWR=1 when already on may say ERROR
                    break

    def handle(self, line, now=None):
        """Take one NMEA sentence. Returns True when it completed a valid fix (RMC)."""
        if not checksum_ok(line):
            return False
        now = now or time.time()
        f = line.split("*")[0].split(",")
        kind = f[0][3:]
        if kind == "GGA" and len(f) > 9:
            with self.lock:
                self._gga = {"gps_sats": int(f[7] or 0),
                             "alt_m": float(f[9]) if f[9] else None,
                             "quality": int(f[6] or 0)}
        elif kind == "RMC" and len(f) > 8:
            if f[2] != "A":       # V = no fix
                return False
            speed = float(f[7] or 0) * KNOTS_TO_KMH
            if speed < STILL_KMH:
                speed = 0.0
            fix = {"lat": round(degrees(f[3], f[4]), 6),
                   "lon": round(degrees(f[5], f[6]), 6),
                   "speed_kmh": round(speed, 1)}
            if f[8] and speed:
                fix["course_deg"] = round(float(f[8]))
            gga = getattr(self, "_gga", {})
            if gga.get("quality"):
                fix["gps_sats"] = gga["gps_sats"]
                if gga["alt_m"] is not None:
                    fix["alt_m"] = round(gga["alt_m"])
            with self.lock:
                self.fix, self.fix_time = fix, now
                self.speeds.append((now - LATENCY_SECS, speed))
            return True
        return False

    # ---- results ----

    def snapshot(self, now=None):
        """Latest fix as {lat, lon, speed_kmh, course_deg, gps_sats, alt_m, age_s};
        empty with no recent fix. speed_kmh is extrapolated to `now`."""
        now = now or time.time()
        with self.lock:
            if now - self.fix_time > STALE_SECS:
                return {}
            out = dict(self.fix)
            out["age_s"] = round(now - self.fix_time, 2)     # display carries the car on from here
        out["speed_kmh"] = self.speed_at(now)
        return out

    def speed_at(self, now):
        """Speed now, carried on from the last two fixes' acceleration (at most
        1.5 s ahead, so a lost fix doesn't run away)."""
        with self.lock:
            sp = list(self.speeds)
        if not sp:
            return None
        t1, v1 = sp[-1]
        if len(sp) < 2 or v1 == 0:
            return v1
        t0, v0 = sp[0]
        accel = (v1 - v0) / max(t1 - t0, 0.5)
        return round(max(0.0, v1 + accel * min(now - t1, 1.5)), 1)

    def accel(self):
        """km/h per second between the last two fixes (0 if unknown)."""
        with self.lock:
            sp = list(self.speeds)
        if len(sp) < 2:
            return 0.0
        (t0, v0), (t1, v1) = sp
        return (v1 - v0) / max(t1 - t0, 0.5)


if __name__ == "__main__":
    g = GPS(sys.argv[1] if len(sys.argv) > 1 else PORT).start()
    try:
        while True:
            time.sleep(1)
            print(g.snapshot() or "no fix")
    except KeyboardInterrupt:
        pass
