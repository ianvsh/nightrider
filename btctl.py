"""Bluetooth devices for the voice Claude (`bluetooth` tool in approve_mcp.py):
list, scan, pair, remove, trust, connect... through `bluetoothctl`, so Ian can
pair and unpair things like the EX1 keyboard by voice without ALLOW taps.
(Claude's Bash sandbox can't reach D-Bus, so this runs in the MCP server.)

Pairing drives one interactive bluetoothctl: own agent (NoInputNoOutput, so
keyboards pair "just works" with no code to type; yes/no prompts answered yes),
LE + classic scan until a not-yet-paired device whose name matches turns up,
then pair, trust, connect. A multi-channel keyboard shows up under a different
address per channel (EX1: D1:03... = channel 1, D2:03... = channel 2).

`python3 btctl.py list` / `pair EX1` / `remove D1:03:FF:30:0C:FD` to try it.
"""
import queue
import re
import subprocess
import sys
import threading
import time

MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02")
PAIR_WAIT = 90          # default seconds to look for a device in pairing mode


def run(*args, timeout=20):
    try:
        r = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "bluetoothctl timed out"
    except OSError as e:
        return f"bluetoothctl failed: {e}"
    return ANSI.sub("", r.stdout + r.stderr).strip()


def devices(which=""):
    """-> [(mac, name)]; which = "" (all known), "Paired" or "Connected"."""
    out = []
    for line in run("devices", *([which] if which else [])).splitlines():
        p = line.split(" ", 2)
        if len(p) >= 2 and p[0] == "Device" and MAC.match(p[1]):
            out.append((p[1].upper(), p[2] if len(p) > 2 else ""))
    return out


def info(mac):
    d = {}
    for line in run("info", mac).splitlines():
        k, _, v = line.strip().partition(": ")
        if k in ("Name", "Alias", "Icon", "Paired", "Trusted", "Blocked", "Connected", "Battery Percentage"):
            d[k] = v
    return d


def describe(mac, name=""):
    i = info(mac)
    flags = [k.lower() for k in ("Paired", "Trusted", "Connected", "Blocked") if i.get(k) == "yes"]
    kind = i.get("Icon", "")
    batt = i.get("Battery Percentage", "")
    batt = f", battery {batt.split('(')[-1].rstrip(')')}%" if "(" in batt else ""
    return f"{mac} {i.get('Alias') or name or '?'}" + (f" [{kind}]" if kind else "") + \
        f": {', '.join(flags) or 'not paired'}{batt}"


def listing():
    paired = devices("Paired")
    if not paired:
        return "No paired Bluetooth devices."
    return "Paired devices:\n" + "\n".join(describe(m, n) for m, n in paired)


def resolve(target):
    """A MAC, or part of a known device's name -> [macs] (all that match)."""
    t = (target or "").strip()
    if MAC.match(t):
        return [t.upper()]
    return [m for m, n in devices() if t and t.lower() in n.lower()]


class Session:
    """One interactive bluetoothctl; lines read on a thread."""

    def __init__(self):
        self.p = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.lines = queue.Queue()
        self.log = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = ""
        while True:
            ch = self.p.stdout.read(1)
            if not ch:
                break
            buf += ch
            # agent questions end in "(yes/no):" with no newline
            if ch == "\n" or buf.endswith("(yes/no): "):
                line = ANSI.sub("", buf).strip()
                buf = ""
                if line:
                    self.log.append(line)
                    if "(yes/no)" in line:
                        self.send("yes")
                    self.lines.put(line)

    def send(self, cmd):
        try:
            self.p.stdin.write(cmd + "\n")
            self.p.stdin.flush()
        except (OSError, ValueError):
            pass

    def wait_for(self, patterns, secs):
        """-> the first line matching any regex, or None after secs."""
        end = time.time() + secs
        while time.time() < end:
            try:
                line = self.lines.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            for pat in patterns:
                if re.search(pat, line, re.I):
                    return line
        return None

    def close(self):
        self.send("scan off")
        self.send("quit")
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()


def pair(target, secs=PAIR_WAIT):
    """Wait (up to secs) for a device in pairing mode whose name contains target
    (or with that MAC), pair, trust and connect it -> text."""
    target = (target or "").strip()
    if not target:
        return "Error: say which device (part of its name, or its address)."
    by_mac = bool(MAC.match(target))
    already = {m for m, _ in devices("Paired")}
    s = Session()
    try:
        s.wait_for([r"Agent registered|\]>|Controller"], 3)
        s.send("agent NoInputNoOutput")
        s.wait_for([r"Agent registered|already registered"], 3)
        s.send("default-agent")
        s.send("pairable on")
        s.send("scan on")
        mac, name, end = None, "", time.time() + secs
        while time.time() < end and not mac:
            time.sleep(1)
            for m, n in devices():
                hit = m == target.upper() if by_mac else target.lower() in n.lower()
                if hit and (by_mac or m not in already):
                    mac, name = m, n
                    break
        s.send("scan off")
        if not mac:
            return (f"No '{target}' in pairing mode showed up in {secs} s. Put it in pairing mode "
                    "(light flashing fast) and try again." +
                    (f" Already paired: {', '.join(sorted(already))}." if already else ""))
        time.sleep(1)
        s.send(f"pair {mac}")
        r = s.wait_for([r"Pairing successful", r"Failed to pair", r"AlreadyExists", r"not available"], 40)
        if not r or "successful" not in r and "AlreadyExists" not in r:
            return f"Pairing {name} ({mac}) failed: {r or 'no answer in 40 s'}"
        s.send(f"trust {mac}")
        s.wait_for([r"trust succeeded", r"Trusted: yes"], 5)
        s.send(f"connect {mac}")
        c = s.wait_for([r"Connection successful", r"Failed to connect"], 15)
        passkey = next((ln for ln in s.log if "Passkey" in ln or "PIN" in ln), "")
        return (f"Paired and trusted {name} ({mac})." +
                (" Connected." if c and "successful" in c else " Not connected yet; a key press or "
                 "switching it on reconnects it.") + (f" ({passkey})" if passkey else ""))
    finally:
        s.send("pairable off")
        s.close()


def simple(action, target):
    """remove / trust / untrust / connect / disconnect / block / unblock / info."""
    macs = resolve(target)
    if not macs:
        return f"No known device matches '{target}'.\n" + listing()
    out = []
    for mac in macs:
        if action == "info":
            out.append(describe(mac))
            continue
        name = info(mac).get("Alias", "")
        r = run(action, mac, timeout=30 if action == "connect" else 15)
        ok = re.search(r"succe|removed|Successful", r, re.I)
        out.append(f"{action} {name} ({mac}): " + ("done" if ok else r.splitlines()[-1] if r else "no answer"))
    return "\n".join(out)


def scan(secs=15):
    known = {m for m, _ in devices("Paired")}
    s = Session()
    try:
        s.send("scan on")
        time.sleep(secs)
    finally:
        s.close()
    near = [(m, n) for m, n in devices() if m not in known and n and n.replace("-", ":").upper() != m]
    return ("Nearby, not paired:\n" + "\n".join(f"{m} {n}" for m, n in near)) if near else \
        "Nothing new with a name nearby."


def command(action, target="", secs=None):
    action = (action or "").lower()
    if action == "list":
        return listing()
    if action == "scan":
        return scan(min(int(secs or 15), 60))
    if action == "pair":
        return pair(target, min(int(secs or PAIR_WAIT), 180))
    if action in ("remove", "trust", "untrust", "connect", "disconnect", "block", "unblock", "info"):
        return simple(action, target)
    return "Error: action is list, scan, pair, remove, trust, untrust, connect, disconnect, block, unblock or info."


if __name__ == "__main__":
    print(command(sys.argv[1] if len(sys.argv) > 1 else "list", " ".join(sys.argv[2:])))
