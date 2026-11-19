"""Live engine data from the F9 (ftech9) OBD+ Sport DME over its USB port.

Protocol details are in dme_protocol.md. Run `python dme.py` to print live
values, or `python dme.py --sim` to try it against the built-in simulator.
"""
import os
import re
import sys
import threading
import time

import serial
from serial.tools import list_ports

USB_PRODUCT = "F9T OBD Interface"
BAUD = 2_000_000
REPLY_TIMEOUT = 0.1       # seconds to wait for each reply before moving on
CYCLE_SECS = 0.25         # one pass over the fast commands (default; DME(cycle_secs=...))
STATS_SECS = 300          # log poll timing this often while connected
STALE_SECS = 3.0          # older readings are dropped from snapshots
DME_ON_VOLTS = 5.0        # below this the DME is unpowered; ignore readings

# (command, poll every N cycles). Read-only commands only: never send 16, 42 or 72.
POLL = [
    ("61", 1),  # RPM
    ("35", 1),  # coolant temp
    ("22", 1),  # DME supply voltage
    ("64", 1),  # ignition advance
    ("63", 1),  # injector pulse width
    ("27", 1),  # idle / full-load switch
    ("2C", 1),  # narrowband O2 volts
    ("56", 1),  # narrowband O2 rich/lean
    ("34", 2),  # intake air temp
    ("32", 2),  # airflow CFM
    ("33", 2),  # airflow g/s
    ("6E", 2),  # idle control valve
    ("62", 2),  # ignition dwell
    ("2D", 2),  # wideband O2
    ("2B", 8),  # barometric pressure
    ("53", 8),  # fuel pump
    ("54", 8),  # A/C compressor
    ("68", 8),  # flywheel tooth count
    ("66", 8),  # RPM sensor errors
    ("67", 8),  # mark sensor errors
    ("69", 8),  # flywheel signal status
    ("17", 8),   # DME reset count
    ("11", 8),   # firmware version
]

# Wideband gauge selected in FocusOBD's settings: name -> volts -> (AFR, lambda)
WIDEBANDS = {
    "aem": lambda v: (2.375 * v + 7.3125, 0.1621 * v + 0.499),
    "autometer": lambda v: (2.0 * v + 10.0, (2.0 * v + 10.0) / 14.68),
    "autometer-2650": lambda v: (1.6 * v + 10.0, (1.6 * v + 10.0) / 14.68),
    "prosport": lambda v: ((0.136 * v + 0.683) * 14.68, 0.136 * v + 0.683),
    "innovate": lambda v: (3.008 * v + 7.35, 0.2 * v + 0.5),
    "plx": lambda v: (2.0 * v + 10.0, (2.0 * v + 10.0) / 14.68),
    "zeitronix": lambda v: (2.0 * v + 9.6, (2.0 * v + 9.6) / 14.68),
}
WIDEBAND = os.environ.get("GOLDY_WIDEBAND", "")   # "" = no wideband fitted


def _num(p):
    return float(p)


def _volts(p):
    v = float(p)
    if v > 5.2:                 # same sanity limit FocusOBD applies to ADC readings
        raise ValueError(p)
    return v


def _advance(p):
    m = re.fullmatch(r"(-?[\d.]+)-(-?[\d.]+)", p)
    dme, klr = float(m[1]), float(m[2])
    if klr == 0:                  # no knock control (924S / 944 NA): KLR is turbo-only
        return {"advance_deg": dme}
    return {"advance_deg": dme, "knock_retard_deg": round(dme - klr, 1)}


def _throttle(p):
    v = _volts(p)
    return {"throttle": "idle" if v < 3.3 else "full load" if v < 4.1 else "part throttle"}


def _o2_state(p):
    return {"o2_state": {"0": "rich", "1": "switching", "3": "lean"}.get(p, p)}


def _wideband(p):
    if WIDEBAND not in WIDEBANDS:
        return {}
    v = _volts(p)
    if WIDEBAND == "aem" and (v < 0.3 or v > 4.5):
        return {}                 # heating up, or out of range
    afr, lam = WIDEBANDS[WIDEBAND](v)
    return {"afr": round(afr, 2), "lambda": round(lam, 3)}


def _flywheel(p):
    if len(p) != 4:
        raise ValueError(p)
    names = ("no_mark_signal", "no_ref_signal", "mark_swapped", "ref_swapped")
    return {n: c == "1" for n, c in zip(names, p)}


# command -> payload -> {name: value}
DECODE = {
    "11": lambda p: {"firmware": p},
    "17": lambda p: {"dme_resets": int(p)},
    "22": lambda p: {"dme_volts": round(_volts(p) * 3.26, 1)},
    "27": _throttle,
    "2B": lambda p: {"baro_kpa": round(18.75 * _volts(p) + 30.625, 1)},
    "2C": lambda p: {"o2_volts": _volts(p)},
    "2D": _wideband,
    "32": lambda p: {"airflow_cfm": _num(p)},
    "33": lambda p: {"airflow_gs": _num(p)},
    "34": lambda p: {"intake_c": _num(p)},
    "35": lambda p: {"coolant_c": _num(p)},
    "53": lambda p: {"fuel_pump": p.lower()},
    "54": lambda p: {"ac": {"1": "on", "0": "off"}.get(p, p)},
    "56": _o2_state,
    "61": lambda p: {"rpm": int(p)},
    "62": lambda p: {"dwell_ms": _num(p)},
    "63": lambda p: {"injector_ms": _num(p)},
    "64": _advance,
    "66": lambda p: {"rpm_sensor_errors": int(p)},
    "67": lambda p: {"mark_sensor_errors": int(p)},
    "68": lambda p: {"flywheel_teeth": int(p)},
    "69": _flywheel,
    "6E": lambda p: {"idle_valve_pct": round(14.9254 * _num(p) - 34.3284, 1)},
}


def parse_frames(buf):
    """Split raw bytes into (command, payload) replies. Returns (replies, leftover)."""
    *frames, rest = buf.split(b"\r")
    replies = []
    for f in frames:
        if b"#" not in f:
            continue
        text = re.sub(r"[^A-Za-z0-9. -]", "", f[f.rindex(b"#") + 1:].decode("ascii", "replace"))
        parts = text.split(" ")
        if len(parts) == 2:
            replies.append((parts[0].upper(), parts[1]))
    return replies, rest


def latency_ms(port):
    """The FTDI chip's USB latency timer (it holds short replies this long;
    the driver default is 16). system/99-dme-latency.rules sets it to 1."""
    try:
        name = os.path.basename(os.path.realpath(port))
        with open(f"/sys/bus/usb-serial/devices/{name}/latency_timer") as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def find_port():
    for p in list_ports.comports():
        if p.product == USB_PRODUCT or USB_PRODUCT in (p.description or ""):
            return p.device
    return None


def c_to_f(c):
    return c * 9 / 5 + 32


class DME:
    """Polls the DME in a background thread; read values with snapshot()."""

    def __init__(self, port=None, log=print, cycle_secs=CYCLE_SECS):
        self.port = port          # None = find the DME by its USB name
        self.log = log
        self.cycle_secs = cycle_secs
        self._reset_stats()
        self.values = {}          # name -> (value, time received)
        self.lock = threading.Lock()
        self.connected = False
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)

    # --- reading -----------------------------------------------------------

    def snapshot(self):
        """Fresh readings as {name: value}; empty if the DME is off or unplugged."""
        now = time.time()
        with self.lock:
            vals = {k: v for k, (v, t) in self.values.items() if now - t < STALE_SECS}
        if vals.get("dme_volts", 0) < DME_ON_VOLTS:
            return {}
        return vals

    def summary(self):
        """One line of the key readings for Champ, or "" if there's no data."""
        v = self.snapshot()
        if not v:
            return ""
        parts = []
        if "rpm" in v:
            parts.append(f"{v['rpm']} rpm")
        if "coolant_c" in v:
            parts.append(f"coolant {c_to_f(v['coolant_c']):.0f}F")
        if "intake_c" in v:
            parts.append(f"intake air {c_to_f(v['intake_c']):.0f}F")
        if "dme_volts" in v:
            parts.append(f"battery {v['dme_volts']:.1f}V")
        if "advance_deg" in v:
            parts.append(f"timing {v['advance_deg']:.1f} deg BTDC")
        if "injector_ms" in v:
            parts.append(f"injector {v['injector_ms']:.1f}ms")
        if "throttle" in v:
            parts.append(f"throttle {v['throttle']}")
        if "afr" in v:
            parts.append(f"AFR {v['afr']:.1f}")
        elif "o2_state" in v:
            parts.append(f"O2 {v['o2_state']}")
        if "idle_valve_pct" in v:
            parts.append(f"idle valve {v['idle_valve_pct']:.0f}%")
        if "airflow_gs" in v:
            parts.append(f"airflow {v['airflow_gs']:.1f} g/s")
        if "fuel_pump" in v:
            parts.append(f"fuel pump {v['fuel_pump']}")
        errors = v.get("rpm_sensor_errors", 0) + v.get("mark_sensor_errors", 0)
        if errors:
            parts.append(f"flywheel sensor errors {errors}")
        return ", ".join(parts)

    # --- polling thread ----------------------------------------------------

    def _run(self):
        while not self.stop.is_set():
            port = self.port or find_port()
            if not port:
                self.stop.wait(2)
                continue
            try:
                with serial.Serial(port, BAUD, rtscts=True, timeout=REPLY_TIMEOUT,
                                   write_timeout=1) as ser:
                    ser.reset_input_buffer()
                    self.connected = True
                    self.log(f"dme connected on {port} (usb latency {latency_ms(port)} ms)")
                    self._reset_stats()
                    self._poll(ser)
            except (serial.SerialException, OSError) as e:
                if self.connected:
                    self.log(f"dme disconnected: {e}")
            self.connected = False
            self.stop.wait(2)

    def _poll(self, ser):
        cycle, buf = 0, b""
        while not self.stop.is_set():
            t0 = time.time()
            timeouts = polled = 0
            for cmd, every in POLL:
                if cycle % every:
                    continue
                polled += 1
                ser.write(cmd.encode() + b"\r")
                deadline = time.time() + REPLY_TIMEOUT
                got = False
                while not got and time.time() < deadline:
                    buf += ser.read(ser.in_waiting or 1)
                    replies, buf = parse_frames(buf)
                    for c, payload in replies:
                        self._store(c, payload)
                        got = got or c == cmd
                    if len(buf) > 256:      # garbage with no delimiter
                        buf = b""
                timeouts += not got
            cycle += 1
            if timeouts < polled:       # DME answering
                self._stats(time.time() - t0, timeouts)
            self.stop.wait(max(0, self.cycle_secs - (time.time() - t0)))

    def _store(self, cmd, payload):
        decode = DECODE.get(cmd)
        if not decode:
            return
        try:
            vals = decode(payload)
        except (ValueError, TypeError):
            return
        now = time.time()
        with self.lock:
            for k, v in vals.items():
                self.values[k] = (v, now)

    def _stats(self, cycle_s, timeouts):
        """Collect poll timing; log a line every STATS_SECS (are we keeping up?)."""
        st = self.timing
        st["n"] += 1
        st["sum"] += cycle_s
        st["max"] = max(st["max"], cycle_s)
        st["late"] += cycle_s > self.cycle_secs
        st["timeouts"] += timeouts
        if time.time() - st["since"] >= STATS_SECS:
            self.log(f"dme: {st['n']} cycles, avg {st['sum'] / st['n'] * 1000:.0f} ms, "
                     f"max {st['max'] * 1000:.0f} ms, {st['late']} over {self.cycle_secs * 1000:.0f} ms, "
                     f"{st['timeouts']} timeouts")
            self._reset_stats()

    def _reset_stats(self):
        self.timing = {"since": time.time(), "n": 0, "sum": 0.0, "max": 0.0, "late": 0, "timeouts": 0}


if __name__ == "__main__":
    sim = None
    port = None
    if "--sim" in sys.argv:
        from dme_sim import Simulator
        sim = Simulator().start()
        port = sim.port
    dme = DME(port).start()
    try:
        while True:
            time.sleep(1)
            print(time.strftime("%H:%M:%S"), dme.summary() or
                  ("connected, DME off" if dme.connected else "waiting for DME..."), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        dme.close()
        if sim:
            sim.close()
