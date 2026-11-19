"""4G on the SIM7670G (SET > 4G). The modem is on USB (2026-11-12): a network card
`usb0` (NetworkManager profile "cellular") and AT ports ttyACM0/ttyACM2; the GPS keeps
the header UART (/dev/ttyS0), so nothing here touches it.

- `Modem`: reads signal, network and SIM state over AT every POLL_SECS in a thread
  (local to the modem: no mobile data used).
- `Usage`: mobile data used since the SIM was last topped up (SET > 4G: used / plan,
  REFILLED resets it), counted from usb0's byte counters, so it includes phones on the
  hotspot. Saved in data-usage.json every SAVE_SECS (SD card) or each SAVE_BYTES.
- Settings in ~/.config/datalogger/network.json:
  prefer  "cell" (default, Ian 2026-11-12) or "wifi": which one carries internet when
          both are up, by the route metric of the "cellular" profile (Wi-Fi is 600).
  hotspot True = the Pi's Wi-Fi is always the DataLogger hotspot, sharing 4G with
          phones (NetworkManager "shared" = NAT); wifi-fallback reads this too and
          then never goes looking for other networks.
"""
import glob
import json
import os
import re
import subprocess
import threading
import time

import wifi

FILE = os.path.expanduser("~/.config/datalogger/network.json")
DEFAULTS = {"prefer": "cell", "hotspot": False}
PROFILE = "cellular"
IFACE = "usb0"
METRIC = {"cell": 50, "wifi": 700}          # Wi-Fi's own route metric is 600
PORTS = ("/dev/serial/by-id/usb-QualComm_QualComm_Compo_*-if06", "/dev/serial/by-id/usb-QualComm_QualComm_Compo_*-if02")
POLL_SECS = 15
OPERATORS = {"310260": "T-MOBILE", "310410": "AT&T", "311480": "VERIZON", "310120": "SPRINT"}
REG = {0: "NOT REGISTERED", 1: "HOME", 2: "SEARCHING", 3: "REFUSED", 4: "UNKNOWN", 5: "ROAMING"}


USAGE_FILE = os.path.expanduser("~/.config/datalogger/data-usage.json")
PLANS_MB = (1000, 2000, 3000, 5000, 10000)
SAVE_SECS = 600
SAVE_BYTES = 5_000_000


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


class Usage:
    """Bytes through the 4G link since the last REFILLED, kept across reboots. The
    kernel's counters start at 0 each boot (or when the modem is plugged in again), so
    only what they grew by since the last look is added."""

    def __init__(self, path=USAGE_FILE, counter=None, boot=None, now=time.time):
        self.path, self.counter, self.boot, self.now = path, counter or iface_bytes, boot or boot_id, now
        self.state = {"since": now(), "bytes": 0, "plan_mb": 2000, "boot": None, "seen": 0}
        try:
            with open(path) as f:
                self.state.update(json.load(f))
        except (OSError, ValueError):
            pass
        self._saved_at, self._saved_bytes = time.monotonic(), self.state["bytes"]

    def update(self):
        c = self.counter()
        if c is None:
            return
        b = self.boot()
        st = self.state
        if st["boot"] != b or c < st["seen"]:          # new boot / modem replugged: counting from 0
            st["boot"], st["seen"] = b, 0
        st["bytes"] += c - st["seen"]
        st["seen"] = c
        if st["bytes"] - self._saved_bytes >= SAVE_BYTES or time.monotonic() - self._saved_at >= SAVE_SECS:
            self.save()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                json.dump(self.state, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass
        self._saved_at, self._saved_bytes = time.monotonic(), self.state["bytes"]

    def refilled(self):
        self.state.update(since=self.now(), bytes=0)
        self.save()

    def next_plan(self):
        plans = list(PLANS_MB)
        cur = self.state["plan_mb"]
        self.state["plan_mb"] = plans[(plans.index(cur) + 1) % len(plans)] if cur in plans else plans[1]
        self.save()

    @property
    def used_mb(self):
        return self.state["bytes"] / 1e6

    @property
    def fraction(self):
        return self.used_mb / self.state["plan_mb"] if self.state["plan_mb"] else 0.0

    def plan_text(self):
        mb = self.state["plan_mb"]
        return f"{mb / 1000:g} GB" if mb >= 1000 else f"{mb} MB"

    def used_text(self):
        mb = self.used_mb
        return f"{mb / 1000:.2f} GB" if mb >= 1000 else f"{mb:.0f} MB"


def load():
    try:
        with open(FILE) as f:
            return dict(DEFAULTS, **json.load(f))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(s):
    try:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        with open(FILE + ".tmp", "w") as f:
            json.dump(s, f)
        os.replace(FILE + ".tmp", FILE)
    except OSError:
        pass


def bars(rsrp):
    """LTE RSRP (dBm) -> 0..4 bars."""
    if rsrp is None:
        return 0
    return 4 if rsrp >= -90 else 3 if rsrp >= -100 else 2 if rsrp >= -110 else 1 if rsrp >= -120 else 0


def parse(replies):
    """{AT command: reply text} -> what the screen shows."""
    out = {}
    m = re.search(r"\+CPIN: (.+)", replies.get("AT+CPIN?", ""))
    out["sim"] = m.group(1).strip() if m else ("NO SIM" if "ERROR" in replies.get("AT+CPIN?", "") else None)
    m = re.search(r"\+CEREG: \d+,(\d+)", replies.get("AT+CEREG?", ""))
    out["reg"] = REG.get(int(m.group(1)), "?") if m else None
    m = re.search(r'\+COPS: \d+,\d+,"([^"]*)"(?:,(\d+))?', replies.get("AT+COPS?", ""))
    if m:
        out["operator"] = OPERATORS.get(m.group(1), m.group(1).upper())
    m = re.search(r"\+CSQ: (\d+),", replies.get("AT+CSQ", ""))
    if m and m.group(1) != "99":
        out["rssi"] = -113 + 2 * int(m.group(1))
    cpsi = re.search(r"\+CPSI: (.+)", replies.get("AT+CPSI?", ""))
    if cpsi:
        f = [x.strip() for x in cpsi.group(1).split(",")]
        out["tech"] = f[0]
        if f[0] == "LTE" and len(f) >= 14:
            out["band"] = f[6].replace("EUTRAN-", "")
            for k, i in (("rsrq", 10), ("rsrp", 11), ("rssi", 12), ("snr", 13)):
                try:
                    out[k] = int(f[i])
                except ValueError:
                    pass
        elif f[0] == "NO SERVICE":
            out["tech"] = "NO SERVICE"
    m = re.search(r"\+CGPADDR: 1,([\d.]+)", replies.get("AT+CGPADDR", ""))
    out["modem_ip"] = m.group(1) if m else None
    out["bars"] = bars(out.get("rsrp"))
    return out


def iface_bytes():
    try:
        return sum(int(open(f"/sys/class/net/{IFACE}/statistics/{k}_bytes").read()) for k in ("rx", "tx"))
    except (OSError, ValueError):
        return None


def internet_via():
    """Which link the internet goes out on now: "4G", "WI-FI" or ""."""
    try:
        best = None
        with open("/proc/net/route") as f:
            for line in list(f)[1:]:
                c = line.split()
                if c[1] == "00000000" and (best is None or int(c[6]) < best[1]):
                    best = (c[0], int(c[6]))
        return {"usb0": "4G", "wlan0": "WI-FI"}.get(best[0], best[0].upper()) if best else ""
    except (OSError, IndexError, ValueError):
        return ""


def apply_prefer(prefer):
    """Route metric of the cellular profile, applied at once (no reconnect). -> (ok, message)"""
    m = str(METRIC[prefer])
    ok, out = wifi.nmcli("c", "modify", PROFILE, "ipv4.route-metric", m, "ipv6.route-metric", m, sudo=True)
    if not ok:
        return False, wifi.friendly(out)
    ok, out = wifi.nmcli("d", "reapply", IFACE, sudo=True)
    if not ok:                                  # no modem plugged in: it applies when it is
        return True, "SAVED (4G NOT CONNECTED NOW)"
    return True, "INTERNET: 4G FIRST" if prefer == "cell" else "INTERNET: WI-FI FIRST, 4G WHEN NONE"


def apply_hotspot(on):
    if on:
        ok, msg = wifi.start_hotspot()
        return ok, "HOTSPOT ON: DATALOGGER, SHARING 4G" if ok else msg
    ok, out = wifi.nmcli("c", "down", "id", wifi.HOTSPOT, sudo=True)
    return True, "HOTSPOT OFF: JOINING SAVED WI-FI"


def hotspot_password():
    ok, out = wifi.nmcli("-s", "-g", "802-11-wireless-security.psk", "c", "show", wifi.HOTSPOT, sudo=True)
    return out.strip() if ok else ""


class Modem:
    """Modem state for the screen, refreshed in a thread (fast while visible)."""

    def __init__(self, log=print, start=True):
        self.log = log
        self.info = {}              # parse() of the last poll
        self.at = 0.0               # monotonic time of it
        self.present = False
        self.visible = False
        self.usage = Usage()
        self._kick = threading.Event()
        if start:
            threading.Thread(target=self._run, daemon=True).start()

    def port(self):
        for pattern in PORTS:
            hits = glob.glob(pattern)
            if hits:
                return hits[0]
        return None

    def ask(self, cmds, port):
        import serial
        replies = {}
        with serial.Serial(port, 115200, timeout=0.2) as s:
            s.reset_input_buffer()
            for c in cmds:
                s.write((c + "\r\n").encode())
                buf, end = b"", time.monotonic() + 1.5
                while time.monotonic() < end and not re.search(rb"\r\n(OK|ERROR|\+CME ERROR[^\r]*)\r\n", buf):
                    buf += s.read(256)
                replies[c] = buf.decode(errors="replace")
        return replies

    def poll(self):
        port = self.port()
        self.present = bool(port)
        if not port:
            self.info = {}
            return
        try:
            self.info = parse(self.ask(["ATE0", "AT+CPIN?", "AT+CEREG?", "AT+COPS?", "AT+CSQ", "AT+CPSI?",
                                        "AT+CGPADDR"], port))
            self.at = time.monotonic()
        except (OSError, ImportError) as e:
            self.info = {"error": str(e)[:40]}

    def refresh(self):
        self._kick.set()

    def _run(self):
        while True:
            try:
                self.poll()
            except Exception as e:                      # never let the thread die
                self.log(f"cell: {e!r}")
            try:
                self.usage.update()
            except Exception as e:
                self.log(f"cell: usage {e!r}")
            self._kick.wait(5 if self.visible else POLL_SECS * 4)
            self._kick.clear()

    def summary(self):
        """Short text for the SET page button."""
        if not self.present:
            return "NO MODEM"
        i = self.info
        if i.get("sim") and i["sim"] != "READY":
            return "SIM " + i["sim"][:8]
        if not i.get("reg") or i["reg"] in ("NOT REGISTERED", "SEARCHING", "REFUSED"):
            return (i.get("reg") or "...")[:12]
        return f"{i.get('bars', 0)}/4"                    # signal bars
