"""Data logger: records DME engine data to SQLite and serves it to a phone
over Wi-Fi (HTTP, web.py) and Bluetooth LE (ble.py).

    python3 logger.py           # real DME on USB
    python3 logger.py --sim     # built-in DME simulator, for testing

Settings come from environment variables (see the systemd unit):
NIGHTRIDER_DB, NIGHTRIDER_HZ, NIGHTRIDER_HTTP_PORT, NIGHTRIDER_BLE (0 = off),
NIGHTRIDER_GPS (serial port, default /dev/serial0; 0 = off).

Each sample also gets GPS readings (lat, lon, speed_kmh, course_deg, gps_sats,
alt_m) when there's a fix, and `gear` (0-5, gear.py) while the engine runs.
"""
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import battery
import fuel
import dme
import gps
from gear import GearFinder
from store import Store

VERSION = "1.0"
DB_PATH = os.environ.get("NIGHTRIDER_DB", "/var/lib/nightrider/log.db")
SAMPLE_HZ = float(os.environ.get("NIGHTRIDER_HZ", "4"))   # while the engine is on
HEARTBEAT_SECS = 60                                         # while it's off
HTTP_PORT = int(os.environ.get("NIGHTRIDER_HTTP_PORT", "80"))
BLE_ON = os.environ.get("NIGHTRIDER_BLE", "1") != "0"
STARTED = time.monotonic()      # immune to clock jumps (no RTC; NTP sets the time late)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def pi_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000, 1)
    except OSError:
        return None


def ip_addresses():
    """IPv4 addresses other than loopback, e.g. ["192.168.0.60"]."""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             timeout=2).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    return [a for a in out if "." in a]


def settled(readings):
    """False while the DME is booting at key-on (first ~2 s of cranking): it
    reports nonsense then (barometer ~31 kPa, dwell 24000 ms, intake 141 C,
    flywheel teeth 0). The very last reply at key-off can be garbage too, which
    is why run() holds each sample back one tick and drops the one before the
    data stops."""
    return readings.get("flywheel_teeth", 1) != 0 and readings.get("baro_kpa", 100) > 50


class Logger:
    def __init__(self, store, engine, gps=None):
        self.store = store
        self.engine = engine          # dme.DME
        self.gps = gps                # gps.GPS, or None
        self.gears = GearFinder(log=log) if gps else None
        self.stop = threading.Event()
        self.listeners = []           # callables(sample), e.g. BLE live notify
        self.battery = None           # battery.Battery: charge estimate for /api/battery
        self.fuel = None              # fuel.Fuel: MPG / fuel used for /api/fuel (also a listener)

    def info(self):
        """Short status, served at /api/info and on the Bluetooth info characteristic."""
        latest = self.store.latest
        return {
            "name": socket.gethostname(),
            "version": VERSION,
            "time": round(time.time(), 1),
            "uptime": round(time.monotonic() - STARTED),
            "ip": ip_addresses(),
            "dme_connected": self.engine.connected,
            "gps_connected": bool(self.gps and self.gps.connected),
            "gps_fix": bool(self.gps and self.gps.snapshot()),
            "engine_on": bool(latest and latest["session"] is not None
                              and time.time() - latest["ts"] < 5),
            "session": self.store.session,
            "sample_hz": SAMPLE_HZ,
            "pi_temp_c": pi_temp_c(),
            **self.store.counts(),
        }

    def run(self):
        period = 1 / SAMPLE_HZ
        last_heartbeat = 0
        pending = None              # (ts, readings) held back one tick, see settled()
        was_on = False
        while not self.stop.is_set():
            t0 = time.time()
            readings = self.engine.snapshot()
            if readings and not settled(readings):
                readings = {}
            if readings:
                readings = self._with_gps(t0, readings)
                if pending:
                    self._record(*pending, engine=True)
                elif not was_on:
                    log(f"engine data: rpm {readings.get('rpm')}, coolant {readings.get('coolant_c')} C")
                    was_on = True
                pending = (t0, readings)
            else:
                if was_on:
                    log(f"engine data stopped (samples so far: {self.store.counts()['samples']})")
                    was_on = False
                pending = None          # the last sample before key-off: drop it
                if t0 - last_heartbeat >= HEARTBEAT_SECS:
                    self._record(t0, self._with_gps(t0, {}), engine=False)
                    last_heartbeat = t0
            self.stop.wait(max(0, period - (time.time() - t0)))
        if pending:
            self._record(*pending, engine=True)
        self.store.flush()
        if self.gears:
            self.gears.save()

    def _with_gps(self, now, readings):
        """Readings plus the GPS fix and, with the engine running, the gear."""
        if not self.gps:
            return readings
        fix = self.gps.snapshot(now)
        fix.pop("age_s", None)          # only the display's map needs it
        out = {**readings, **fix}
        if fix and "rpm" in readings:
            out["gear"] = self.gears.update(now, readings["rpm"], fix["speed_kmh"], self.gps.accel())
        return out

    def _record(self, ts, readings, engine):
        data = dict(readings)
        data["pi_temp_c"] = pi_temp_c()
        sample = self.store.add(data, engine=engine, ts=ts)
        for fn in self.listeners:
            try:
                fn(sample)
            except Exception as e:     # a broken listener mustn't stop logging
                log(f"listener error: {e}")


def main():
    sim = None
    port = None
    if "--sim" in sys.argv:
        from dme_sim import Simulator
        sim = Simulator().start()
        port = sim.port
        log("using DME simulator")

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    store = Store(DB_PATH)
    import store as store_mod
    threading.Thread(target=store_mod.backfill_km, args=(DB_PATH, log, store.set_km), daemon=True).start()
    engine = dme.DME(port, log=log, cycle_secs=1 / SAMPLE_HZ).start()
    receiver = None
    if gps.PORT != "0":
        receiver = gps.GPS(log=log).start()
        log(f"gps on {gps.PORT}")
    logger = Logger(store, engine, receiver)
    logger.battery = battery.Battery(DB_PATH, log=log)
    logger.fuel = fuel.Fuel(log=log)
    logger.listeners.append(logger.fuel.add)

    import web
    web.start(logger, HTTP_PORT, log)
    log(f"wifi: http on port {HTTP_PORT}")

    if BLE_ON:
        try:
            import ble
            ble.start(logger, log)
        except Exception as e:      # no adapter / no BlueZ: keep logging anyway
            log(f"bluetooth unavailable: {e}")

    def shutdown(*_):
        logger.stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"logging to {DB_PATH} at {SAMPLE_HZ:g} Hz")
    logger.run()
    logger.fuel.close()
    engine.close()
    if receiver:
        receiver.close()
    store.close()
    if sim:
        sim.close()
    log("stopped")


if __name__ == "__main__":
    main()
