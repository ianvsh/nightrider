"""Wi-Fi control for the touchscreen, via NetworkManager (nmcli).

Changes go through `sudo -n nmcli` (ian has passwordless sudo). wifi-fallback
keeps working alongside: once we're on a network it leaves it alone; if a
connection fails it brings the DataLogger hotspot back after ~45 s.
"""
import subprocess

HOTSPOT = "datalogger-hotspot"
HOTSPOT_SSID = "DataLogger"


def nmcli(*args, sudo=False, timeout=40):
    """-> (ok, output or error text)"""
    cmd = (["sudo", "-n"] if sudo else []) + ["nmcli"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr or r.stdout).strip()


def fields(line):
    """Split an `nmcli -t` line on ':' (nmcli escapes literal ':' and '\\' with '\\')."""
    out, cur, esc = [], "", False
    for c in line:
        if esc:
            cur += c
            esc = False
        elif c == "\\":
            esc = True
        elif c == ":":
            out.append(cur)
            cur = ""
        else:
            cur += c
    out.append(cur)
    return out


def current():
    """Name of the active Wi-Fi connection ('' if none), e.g. 'Testing1' or HOTSPOT."""
    ok, out = nmcli("-t", "-f", "NAME,TYPE", "c", "show", "--active")
    for line in out.splitlines() if ok else []:
        f = fields(line)
        if len(f) >= 2 and "wireless" in f[1]:
            return f[0]
    return ""


def saved():
    """Saved client networks: {ssid: connection name} (the hotspot is left out)."""
    ok, out = nmcli("-t", "-f", "NAME,TYPE", "c", "show")
    result = {}
    for line in out.splitlines() if ok else []:
        name, kind = (fields(line) + [""])[:2]
        if "wireless" not in kind or name == HOTSPOT:
            continue
        ok2, info = nmcli("-t", "-g", "802-11-wireless.ssid,802-11-wireless.mode", "c", "show", name)
        if ok2:
            ssid, mode = (info.splitlines() + ["", ""])[:2]
            if mode != "ap":
                result[ssid or name] = name
    return result


def scan(rescan=True):
    """Networks in range, best first: [{ssid, signal, secure, active, saved}]."""
    ok, out = nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
                    "--rescan", "yes" if rescan else "no", sudo=True, timeout=30)
    if not ok:
        raise RuntimeError(out or "scan failed")
    known = saved()
    best = {}
    for line in out.splitlines():
        f = fields(line)
        if len(f) < 4 or not f[1] or f[1] == HOTSPOT_SSID:
            continue
        ssid = f[1]
        net = {"ssid": ssid, "signal": int(f[2] or 0), "secure": f[3] not in ("", "--"),
               "active": f[0].strip() == "*", "saved": ssid in known, "profile": known.get(ssid)}
        old = best.get(ssid)
        if not old or net["signal"] > old["signal"] or net["active"]:
            net["active"] = net["active"] or bool(old and old["active"])
            best[ssid] = net
    return sorted(best.values(), key=lambda n: (not n["active"], not n["saved"], -n["signal"]))


def friendly(err):
    e = err.lower()
    if "secrets were required" in e or "802-1x" in e or "psk" in e or "password" in e:
        return "WRONG PASSWORD?"
    if "no network with ssid" in e:
        return "NETWORK NOT FOUND"
    if "timed out" in e or "timeout" in e:
        return "TIMED OUT"
    return "FAILED: " + err.replace("Error: ", "")[:60]


def connect(ssid, password=None, profile=None):
    """Join a network. Uses the saved profile if there is one. -> (ok, message)"""
    if profile and password is None:
        ok, out = nmcli("c", "up", "id", profile, sudo=True, timeout=60)
    else:
        args = ["dev", "wifi", "connect", ssid] + (["password", password] if password else [])
        ok, out = nmcli(*args, sudo=True, timeout=60)
        if not ok and not profile:
            # don't leave a half-made profile with a wrong password behind
            nmcli("c", "delete", "id", ssid, sudo=True)
    return ok, ("CONNECTED TO " + ssid) if ok else friendly(out)


def start_hotspot():
    ok, out = nmcli("c", "up", "id", HOTSPOT, sudo=True, timeout=60)
    return ok, "HOTSPOT ON: JOIN \"DATALOGGER\", THEN 10.42.0.1" if ok else friendly(out)
