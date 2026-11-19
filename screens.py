"""Full-screen touch screens opened from the Status page: touch calibration,
the Wi-Fi network list, AUDIO (AUX / Bluetooth stereo), 4G (cell.py), and the on-screen keyboard.
All drawn at 266x160.

Each screen has render(img, d) and tap(x, y); tap returns None (stay), "close",
or another screen to open on top.
"""
import json
import os
import threading
import time

import numpy as np

import bt
import cell
import wifi
from pixels import OX, SCALE, AMBER, BG, DIM, FAINT, GREEN, LH, LW, RED, blink, button, inside, text, text_width

CAL_FILE = os.path.expanduser("~/.config/datalogger/touch.json")
FLASH = 0.15          # seconds a pressed button stays lit


class Calibration:
    """Maps raw touch readings to screen positions (affine fit over 4 taps).
    Works whatever way round the panel's touch layer is wired."""

    def __init__(self, matrix):
        self.m = np.asarray(matrix, dtype=float)       # 3x2

    def map(self, raw):
        x, y = np.array([raw[0], raw[1], 1.0]) @ self.m
        return int(round(x)), int(round(y))

    @staticmethod
    def load():
        """The saved calibration, else the capacitive panel's own mapping: it
        reports panel pixels, so it just needs scaling down to drawing pixels."""
        try:
            with open(CAL_FILE) as f:
                return Calibration(json.load(f)["matrix"])
        except (OSError, ValueError, KeyError):
            return Calibration([[1 / SCALE, 0], [0, 1 / SCALE], [-OX / SCALE, 0]])

    def save(self):
        os.makedirs(os.path.dirname(CAL_FILE), exist_ok=True)
        tmp = CAL_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"matrix": self.m.tolist()}, f)
        os.replace(tmp, CAL_FILE)

    @staticmethod
    def fit(raws, targets):
        """-> (Calibration, worst error in screen pixels)"""
        a = np.hstack([np.asarray(raws, float), np.ones((len(raws), 1))])
        t = np.asarray(targets, float)
        m, *_ = np.linalg.lstsq(a, t, rcond=None)
        err = np.abs(a @ m - t).max()
        return Calibration(m), float(err)


class CalibrateScreen:
    """Tap four crosses. Uses the averaged position of each press (on release)."""
    TARGETS = [(20, 20), (LW - 20, 20), (LW - 20, 140), (20, 140)]
    no_hold = True            # careful taps mustn't trigger hold-to-shut-down
    wants_release = True

    def __init__(self, on_done):
        self.on_done = on_done
        self.raws = []
        self.message = ""

    def release(self, raw):
        self.raws.append(raw)
        if len(self.raws) < len(self.TARGETS):
            return None
        cal, err = Calibration.fit(self.raws, self.TARGETS)
        self.raws = []
        if err > 10:
            self.message = "NOT QUITE - LET'S GO AGAIN"
            return None
        cal.save()
        self.on_done(cal)
        return "close"

    def tap(self, x, y):
        return None

    def render(self, img, d):
        n = len(self.raws)
        tx, ty = self.TARGETS[n]
        text(img, LW // 2, 50, "TOUCH SETUP", GREEN, scale=2, align="c")
        text(img, LW // 2, 72, f"TAP THE CENTRE OF THE CROSS  {n + 1}/4", GREEN, align="c")
        text(img, LW // 2, 84, "A FINGERNAIL WORKS BEST", DIM, align="c")
        if self.message:
            text(img, LW // 2, 100, self.message, AMBER, align="c")
        c = RED if blink() else AMBER
        d.line([tx - 9, ty, tx + 9, ty], fill=c)
        d.line([tx, ty - 9, tx, ty + 9], fill=c)
        d.rectangle([tx - 3, ty - 3, tx + 3, ty + 3], outline=c)


class Pressable:
    """Remembers the last pressed button so it can light up briefly."""

    def __init__(self):
        self._pressed = (None, 0)

    def press(self, name):
        self._pressed = (name, time.monotonic())

    def lit(self, name):
        return self._pressed[0] == name and time.monotonic() - self._pressed[1] < FLASH


class WifiScreen(Pressable):
    BACK = (0, 0, 56, 18)
    SCAN = (LW - 56, 0, LW - 1, 18)
    UP = (0, 138, 48, 159)
    DOWN = (52, 138, 100, 159)
    HOTSPOT = (LW - 136, 138, LW - 1, 159)
    ROW_Y, ROW_H, ROWS = 33, 21, 5

    def __init__(self):
        super().__init__()
        self.nets = []
        self.top = 0
        self.status, self.status_colour = "SCANNING...", DIM
        self.busy = False
        self.current = wifi.current()
        self._work(self._scan)

    def _work(self, fn, *args):
        self.busy = True
        threading.Thread(target=self._run, args=(fn,) + args, daemon=True).start()

    def _run(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:                 # show it, never crash the display
            self.status, self.status_colour = f"ERROR: {e}"[:39], RED
        finally:
            self.busy = False

    def _scan(self):
        self.status, self.status_colour = "SCANNING...", DIM
        self.current = wifi.current()
        try:
            self.nets = wifi.scan()
        except RuntimeError as e:
            if self.current == wifi.HOTSPOT:
                self.status, self.status_colour = "CAN'T SCAN WHILE HOTSPOT IS ON", AMBER
                return
            raise
        self.top = 0
        self._show_current()

    def _show_current(self):
        if self.current == wifi.HOTSPOT:
            self.status, self.status_colour = "ON OWN HOTSPOT (10.42.0.1)", AMBER
        elif self.current:
            self.status, self.status_colour = "CONNECTED: " + self.current, GREEN
        else:
            self.status, self.status_colour = "NOT CONNECTED", RED

    def _connect(self, net, password=None):
        self.status, self.status_colour = "CONNECTING TO " + net["ssid"] + "...", AMBER
        ok, msg = wifi.connect(net["ssid"], password, net.get("profile"))
        self.current = wifi.current()
        try:
            self.nets = wifi.scan(rescan=False)
        except RuntimeError:
            pass
        self.status, self.status_colour = msg, GREEN if ok else RED

    def _hotspot(self):
        self.status, self.status_colour = "STARTING HOTSPOT...", AMBER
        ok, msg = wifi.start_hotspot()
        self.current = wifi.current()
        self.status, self.status_colour = msg, AMBER if ok else RED

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        if self.busy:
            return None
        if inside(self.SCAN, x, y):
            self.press("scan")
            self._work(self._scan)
        elif inside(self.UP, x, y):
            self.press("up")
            self.top = max(0, self.top - self.ROWS)
        elif inside(self.DOWN, x, y):
            self.press("down")
            if self.top + self.ROWS < len(self.nets):
                self.top += self.ROWS
        elif inside(self.HOTSPOT, x, y):
            self.press("hotspot")
            self._work(self._hotspot)
        elif self.ROW_Y <= y < self.ROW_Y + self.ROWS * self.ROW_H:
            i = self.top + (y - self.ROW_Y) // self.ROW_H
            if i < len(self.nets):
                net = self.nets[i]
                self.press(("row", i))
                if net["active"]:
                    return None
                if net["secure"] and not net["saved"]:
                    return Keyboard("PASSWORD FOR " + net["ssid"],
                                    lambda pw, net=net: self._work(self._connect, net, pw))
                self._work(self._connect, net)
        return None

    def render(self, img, d):
        button(img, d, self.BACK, "< BACK", GREEN, self.lit("back"))
        text(img, LW // 2 + 2, 6, "WI-FI", GREEN, align="c")
        button(img, d, self.SCAN, "SCAN", DIM if self.busy else GREEN, self.lit("scan"))
        c = self.status_colour
        if self.busy and blink():
            c = DIM
        text(img, 2, 22, self.status[:39], c, upper=False if self.status.startswith(("CONNECTED: ",)) else True)
        d.line([0, 31, LW, 31], fill=FAINT)
        if not self.nets and not self.busy:
            text(img, LW // 2, 70, "NO NETWORKS FOUND", DIM, align="c")
        for n, net in enumerate(self.nets[self.top:self.top + self.ROWS]):
            i = self.top + n
            y0 = self.ROW_Y + n * self.ROW_H
            rect = (0, y0, LW - 1, y0 + self.ROW_H - 2)
            lit = self.lit(("row", i))
            col = AMBER if net["active"] else GREEN
            if lit:
                d.rectangle(rect, fill=col)
            else:
                d.rectangle(rect, outline=FAINT)
            fg = BG if lit else col
            text(img, 4, y0 + 3, net["ssid"][:26], fg, upper=False)
            tag = "CONNECTED" if net["active"] else "SAVED" if net["saved"] else \
                "PASSWORD" if net["secure"] else "OPEN"
            text(img, 4, y0 + 11, tag, BG if lit else DIM)
            bars = min(4, (net["signal"] + 12) // 25)
            for b in range(4):
                bx = LW - 30 + b * 6
                by = y0 + 15 - b * 3
                if b < bars:
                    d.rectangle([bx, by, bx + 3, y0 + 16], fill=fg)
                else:
                    d.rectangle([bx, by, bx + 3, y0 + 16], outline=BG if lit else FAINT)
        more = len(self.nets) > self.ROWS
        button(img, d, self.UP, "UP", GREEN if more and self.top else FAINT, self.lit("up"))
        button(img, d, self.DOWN, "DOWN", GREEN if self.top + self.ROWS < len(self.nets) else FAINT,
               self.lit("down"))
        button(img, d, self.HOTSPOT, "OWN HOTSPOT", AMBER, self.lit("hotspot"))


class VoiceScreen(Pressable):
    """AI page > voice: TEXT ONLY or one of the voices (speech.VOICES), on two tabs: MODERN
    (Piper neural voices) and VINTAGE (old-school synthesis, near-instant). Tapping a voice
    picks it and says a short line in it (the music steps aside for that, as for any answer)."""
    BACK = (0, 0, 56, 18)
    TABS = {"modern": (62, 0, 162, 18), "vintage": (166, 0, LW - 1, 18)}
    ROW_Y, ROW_H = 22, 27

    def __init__(self, assistant):
        super().__init__()
        self.a = assistant
        import speech
        self.speech = speech
        sp = assistant.speaker
        self.page = speech.VOICES[sp.choice].get("page", "modern") if sp else "modern"

    @property
    def rows(self):
        return [(None, "TEXT ONLY", "ANSWERS ON SCREEN, MUSIC KEEPS PLAYING")] + \
            [(k, v["label"], v["note"]) for k, v in self.speech.VOICES.items()
             if v.get("page", "modern") == self.page and self.speech.available(k)]

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        for page, rect in self.TABS.items():
            if inside(rect, x, y):
                self.press(("tab", page))
                self.page = page
                return None
        rows = self.rows
        i = (y - self.ROW_Y) // self.ROW_H
        if y >= self.ROW_Y and 0 <= i < len(rows):
            self.press(("row", i))
            self.a.set_voice(rows[i][0])
        return None

    def render(self, img, d):
        a = self.a
        sp = a.speaker
        button(img, d, self.BACK, "< BACK", GREEN, self.lit("back"))
        for page, rect in self.TABS.items():
            on = page == self.page
            if on or self.lit(("tab", page)):
                d.rectangle(rect, fill=GREEN)
            else:
                d.rectangle(rect, outline=FAINT)
            text(img, (rect[0] + rect[2]) // 2, 6, page.upper(), BG if on or self.lit(("tab", page)) else GREEN,
                 align="c")
        for i, (key, label, note) in enumerate(self.rows):
            y0 = self.ROW_Y + i * self.ROW_H
            rect = (0, y0, LW - 1, y0 + self.ROW_H - 3)
            chosen = (key is None and not a.spoken) or (key is not None and a.spoken and sp and sp.choice == key)
            lit = self.lit(("row", i))
            col = AMBER if chosen else GREEN
            if lit or chosen:
                d.rectangle(rect, fill=col)
            else:
                d.rectangle(rect, outline=FAINT)
            fg = BG if lit or chosen else None
            text(img, 6, y0 + 2, label, fg or col, scale=2)
            text(img, 6, y0 + 17, note, fg or DIM)
            if chosen and sp and sp.loading and blink():
                text(img, LW - 6, y0 + 8, "LOADING...", BG, align="r")
            elif chosen:
                text(img, LW - 6, y0 + 8, "IN USE", BG, align="r")


class QualityScreen(Pressable):
    """SET > AUDIO > SPOTIFY QUALITY: kbps on 4G and on Wi-Fi (bt.Router follows the link)."""
    BACK = (0, 0, 56, 18)
    ROWS = ((30, "cell", "ON 4G"), (62, "wifi", "ON WI-FI"))
    MB_PER_HOUR = {96: 45, 160: 70, 320: 145}

    def __init__(self, router):
        super().__init__()
        self.router = router

    def rect(self, row_y, n):
        x0 = 70 + n * 66
        return (x0, row_y, x0 + 62, row_y + 22)

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        for row_y, link, _ in self.ROWS:
            for n, kbps in enumerate(bt.QUALITIES):
                if inside(self.rect(row_y, n), x, y):
                    self.press((link, kbps))
                    self.router.set_quality(link, kbps)
        return None

    def render(self, img, d):
        r = self.router
        button(img, d, self.BACK, "< BACK", GREEN, self.lit("back"))
        text(img, LW - 2, 6, "SPOTIFY QUALITY", GREEN, align="r")
        for row_y, link, label in self.ROWS:
            text(img, 2, row_y + 8, label, DIM)
            want = r.settings.get(f"quality_{link}")
            for n, kbps in enumerate(bt.QUALITIES):
                button(img, d, self.rect(row_y, n), str(kbps), AMBER if link == "cell" else GREEN,
                       want == kbps or self.lit((link, kbps)), scale=2)
        text(img, LW // 2, 92, "KBPS. DATA PER HOUR OF MUSIC:", DIM, align="c")
        text(img, LW // 2, 102, "  ".join(f"{k} = {v} MB" for k, v in self.MB_PER_HOUR.items()), GREEN, align="c")
        link, kbps = r.quality_now()
        have = bt.player_bitrate()
        now = (f"NOW {have} KBPS" if have else "NOW ?") + \
            (f", INTERNET OVER {'4G' if link == 'cell' else 'WI-FI'}" if link else ", NO INTERNET")
        text(img, LW // 2, 118, now, AMBER if kbps and have and have != kbps else DIM, align="c")
        if kbps and have and have != kbps:
            text(img, LW // 2, 128, f"SWITCHING TO {kbps} SOON (SPOTIFY RESTARTS)", AMBER, align="c")
        text(img, LW // 2, 144, "A CHANGE RESTARTS SPOTIFY (SHORT GAP)", FAINT, align="c")


class AudioScreen(Pressable):
    """AUX / BLUETOOTH output, and picking + pairing the stereo (bt.Router)."""
    BACK = (0, 0, 56, 18)
    QUALITY = (60, 0, LW - 60, 18)
    SCAN = (LW - 56, 0, LW - 1, 18)
    AUX = (0, 21, LW - 123, 39)
    BLUE = (LW - 119, 21, LW - 1, 39)
    UP = (0, 138, 48, 159)
    DOWN = (52, 138, 100, 159)
    FORGET = (LW - 136, 138, LW - 1, 159)
    ROW_Y, ROW_H, ROWS = 54, 21, 4

    def __init__(self, router):
        super().__init__()
        self.router = router
        self.devs = []
        self.top = 0
        self.busy = False
        self.status, self.status_colour = "", DIM
        if router.bt:
            self._work(self._list)

    def _work(self, fn, *args):
        self.busy = True
        threading.Thread(target=self._run, args=(fn,) + args, daemon=True).start()

    def _run(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:                 # show it, never crash the display
            self.status, self.status_colour = bt.friendly(e)[:39], RED
        finally:
            self.busy = False

    def _list(self):
        self.devs = self.router.bt.devices()

    def _scan(self):
        self.status, self.status_colour = "SEARCHING: STEREO IN PAIRING MODE", AMBER
        self.devs = self.router.bt.scan()
        self.top = 0
        found = any(not d["paired"] for d in self.devs)
        self.status, self.status_colour = ("TAP YOUR STEREO", GREEN) if found else \
            ("NOTHING NEW FOUND: SCAN AGAIN", AMBER)

    def _use(self, dev):
        self.status, self.status_colour = "PAIRING WITH " + dev["name"] + "...", AMBER
        self.router.bt.pair_and_connect(dev["mac"])
        self.router.set_output("bluetooth", dev["mac"], dev["name"])
        self.devs = self.router.bt.devices()
        self.status, self.status_colour = "CONNECTED: " + dev["name"], GREEN

    def _forget(self):
        self.router.forget_stereo()
        self.devs = self.router.bt.devices()
        self.status, self.status_colour = "STEREO FORGOTTEN: AUDIO ON AUX", AMBER

    def tap(self, x, y):
        r = self.router
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        if inside(self.QUALITY, x, y):
            self.press("quality")
            return QualityScreen(r)
        if inside(self.AUX, x, y):
            self.press("aux")
            self.status = ""
            r.set_output("aux")
            return None
        if inside(self.BLUE, x, y):
            self.press("blue")
            if r.settings.get("stereo"):
                self.status = ""
                r.set_output("bluetooth")
            else:
                self.status, self.status_colour = "PICK A STEREO FIRST: TAP SCAN", AMBER
            return None
        if self.busy or not r.bt:
            return None
        if inside(self.SCAN, x, y):
            self.press("scan")
            self._work(self._scan)
        elif inside(self.UP, x, y):
            self.press("up")
            self.top = max(0, self.top - self.ROWS)
        elif inside(self.DOWN, x, y):
            self.press("down")
            if self.top + self.ROWS < len(self.devs):
                self.top += self.ROWS
        elif inside(self.FORGET, x, y):
            if r.settings.get("stereo"):
                self.press("forget")
                self._work(self._forget)
        elif self.ROW_Y <= y < self.ROW_Y + self.ROWS * self.ROW_H:
            i = self.top + (y - self.ROW_Y) // self.ROW_H
            if i < len(self.devs):
                self.press(("row", i))
                dev = self.devs[i]
                if not (dev["mac"] == r.settings.get("stereo") and dev["connected"]):
                    self._work(self._use, dev)
        return None

    def _state(self):
        """The status line when nothing else is being said."""
        r = self.router
        if r.error:
            return r.error, RED
        if r.switching:
            return "MOVING THE MUSIC...", AMBER
        if r.settings["output"] == "aux":
            return "PLAYING THROUGH AUX", GREEN
        if not r.settings.get("stereo"):
            return "PUT STEREO IN PAIRING MODE, TAP SCAN", AMBER
        if r.route == "bluetooth":
            return "PLAYING THROUGH " + r.settings["name"], GREEN
        return "STEREO NOT CONNECTED: MUSIC PAUSED", AMBER

    def render(self, img, d):
        r = self.router
        button(img, d, self.BACK, "< BACK", GREEN, self.lit("back"))
        link, kbps = r.quality_now()
        button(img, d, self.QUALITY, f"SPOTIFY QUALITY: {kbps or '?'} >", GREEN, self.lit("quality"))
        button(img, d, self.SCAN, "SCAN", DIM if self.busy or not r.bt else GREEN, self.lit("scan"))
        bt_on = r.settings["output"] == "bluetooth"
        button(img, d, self.AUX, "AUX", GREEN, not bt_on or self.lit("aux"))
        button(img, d, self.BLUE, "BLUETOOTH", GREEN, bt_on or self.lit("blue"))
        status, c = (self.status, self.status_colour) if self.status else self._state()
        if (self.busy or r.switching) and blink():
            c = DIM
        text(img, 2, 43, status[:39], c, upper=False)
        d.line([0, 52, LW, 52], fill=FAINT)
        if not self.devs and not self.busy and r.bt:
            text(img, LW // 2, 88, "NO STEREO YET: TAP SCAN", DIM, align="c")
        for n, dev in enumerate(self.devs[self.top:self.top + self.ROWS]):
            i = self.top + n
            y0 = self.ROW_Y + n * self.ROW_H
            rect = (0, y0, LW - 1, y0 + self.ROW_H - 2)
            lit = self.lit(("row", i))
            mine = dev["mac"] == r.settings.get("stereo")
            col = AMBER if mine else GREEN
            if lit:
                d.rectangle(rect, fill=col)
            else:
                d.rectangle(rect, outline=FAINT)
            text(img, 4, y0 + 3, dev["name"][:36], BG if lit else col, upper=False)
            tag = ("STEREO - " if mine else "") + ("CONNECTED" if dev["connected"] else
                                                   "PAIRED" if dev["paired"] else "TAP TO PAIR")
            text(img, 4, y0 + 11, tag, BG if lit else DIM)
        more = len(self.devs) > self.ROWS
        button(img, d, self.UP, "UP", GREEN if more and self.top else FAINT, self.lit("up"))
        button(img, d, self.DOWN, "DOWN", GREEN if self.top + self.ROWS < len(self.devs) else FAINT,
               self.lit("down"))
        button(img, d, self.FORGET, "FORGET STEREO", RED if r.settings.get("stereo") else FAINT,
               self.lit("forget"))


class CellScreen(Pressable):
    """SET > 4G: the modem's signal and network, data used since the SIM was topped up
    (PLAN size, REFILLED resets it), which link carries the internet first (4G or
    Wi-Fi), and the Wi-Fi hotspot (shares 4G with phones)."""
    BACK = (0, 0, 56, 18)
    CELL_FIRST = (90, 73, 176, 88)
    WIFI_FIRST = (180, 73, LW - 1, 88)
    HOT_OFF = (90, 92, 176, 107)
    HOT_ON = (180, 92, LW - 1, 107)
    PLAN = (0, 122, 86, 138)
    REFILL = (90, 122, LW - 1, 138)
    CONFIRM_SECS = 3

    def __init__(self, modem):
        super().__init__()
        self.modem = modem
        self.settings = cell.load()
        self.busy = False
        self.status, self.status_colour = "", DIM
        self.password = ""
        self.armed = 0.0                       # REFILLED's first tap
        if modem:
            modem.visible = True
            modem.refresh()
        if self.settings["hotspot"]:
            self._work(self._get_password)

    def close(self):
        if self.modem:
            self.modem.visible = False

    def _work(self, fn, *args):
        self.busy = True
        threading.Thread(target=self._run, args=(fn,) + args, daemon=True).start()

    def _run(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:                 # show it, never crash the display
            self.status, self.status_colour = f"ERROR: {e}"[:44], RED
        finally:
            self.busy = False

    def _get_password(self):
        self.password = cell.hotspot_password()

    def _prefer(self, prefer):
        self.status, self.status_colour = "CHANGING...", AMBER
        ok, msg = cell.apply_prefer(prefer)
        self.status, self.status_colour = msg, GREEN if ok else RED

    def _hotspot(self, on):
        self.status, self.status_colour = ("STARTING HOTSPOT..." if on else "STOPPING HOTSPOT..."), AMBER
        ok, msg = cell.apply_hotspot(on)
        if on:
            self.password = cell.hotspot_password()
        self.status, self.status_colour = msg, GREEN if ok else RED

    def _set(self, key, value, fn):
        if self.settings[key] == value:
            return
        self.settings = dict(self.settings, **{key: value})
        cell.save(self.settings)
        self._work(fn, value)

    @property
    def usage(self):
        return self.modem.usage if self.modem else None

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            self.close()
            return "close"
        u = self.usage
        if u and inside(self.PLAN, x, y):
            self.press("plan")
            u.next_plan()
            return None
        if u and inside(self.REFILL, x, y):
            self.press("refill")
            if time.monotonic() - self.armed < self.CONFIRM_SECS:
                u.refilled()
                self.armed = 0.0
                self.status, self.status_colour = "DATA COUNT RESET: SIM TOPPED UP", GREEN
            else:
                self.armed = time.monotonic()
            return None
        if self.busy:
            return None
        for rect, name, key, value, fn in ((self.CELL_FIRST, "cell", "prefer", "cell", self._prefer),
                                           (self.WIFI_FIRST, "wifi", "prefer", "wifi", self._prefer),
                                           (self.HOT_OFF, "hoff", "hotspot", False, self._hotspot),
                                           (self.HOT_ON, "hon", "hotspot", True, self._hotspot)):
            if inside(rect, x, y):
                self.press(name)
                self._set(key, value, fn)
        return None

    def render(self, img, d):
        m = self.modem
        i = (m.info if m else {}) or {}
        button(img, d, self.BACK, "< BACK", GREEN, self.lit("back"))
        text(img, LW - 2, 6, "4G / INTERNET", GREEN, align="r")
        y = 22
        if not m or not m.present:
            text(img, 2, y, "MODEM NOT FOUND ON USB", RED)
        elif i.get("sim") and i["sim"] != "READY":
            text(img, 2, y, "SIM", DIM)
            text(img, LW - 2, y, i["sim"][:30], RED, align="r")
        else:
            net = " ".join(x for x in (i.get("operator"), i.get("tech"), i.get("band")) if x) or "..."
            reg = i.get("reg") or ""
            text(img, 2, y, "NETWORK", DIM)
            text(img, LW - 2, y, f"{net} {reg}"[:34], GREEN if reg in ("HOME", "ROAMING") else AMBER, align="r")
        if m and m.present:
            text(img, 2, y + 10, "SIGNAL", DIM)
            b = i.get("bars", 0)
            for n in range(4):                         # four rising bars
                x0, h = 44 + n * 6, 3 + n * 2
                box = [x0, y + 17 - h, x0 + 3, y + 17]
                d.rectangle(box, fill=GREEN if n < b else None, outline=GREEN if n < b else FAINT)
            sig = f"{i['rsrp']} DBM" if i.get("rsrp") is not None else f"{i['rssi']} DBM" if i.get("rssi") else "--"
            text(img, LW - 2, y + 10, sig + (f"  SNR {i['snr']}" if i.get("snr") is not None else ""),
                 GREEN if b >= 2 else AMBER if b else RED, align="r")
        via = cell.internet_via()
        text(img, 2, y + 20, "INTERNET NOW", DIM)
        text(img, LW - 2, y + 20, via or "NONE", GREEN if via else RED, align="r")
        u = self.usage
        if u:                                          # data used since the SIM was topped up
            f = u.fraction
            col = RED if f >= 0.9 else AMBER if f >= 0.75 else GREEN
            since = time.strftime("%b %d", time.localtime(u.state["since"])).upper()
            text(img, 2, y + 30, f"DATA SINCE {since}", DIM)
            text(img, LW - 2, y + 30, f"{u.used_text()} OF {u.plan_text()} ({f * 100:.0f}%)", col, align="r")
            d.rectangle([2, y + 39, LW - 3, y + 44], outline=FAINT)
            if f > 0:
                d.rectangle([2, y + 39, 2 + int((LW - 5) * min(f, 1.0)), y + 44], fill=col)
        d.line([0, 70, LW, 70], fill=FAINT)
        text(img, 2, 77, "USE FIRST", DIM)
        cell_first = self.settings["prefer"] == "cell"
        button(img, d, self.CELL_FIRST, "4G", GREEN, cell_first or self.lit("cell"))
        button(img, d, self.WIFI_FIRST, "WI-FI", GREEN, not cell_first or self.lit("wifi"))
        text(img, 2, 96, "WI-FI HOTSPOT", DIM)
        hot = self.settings["hotspot"]
        button(img, d, self.HOT_OFF, "OFF", GREEN, not hot or self.lit("hoff"))
        button(img, d, self.HOT_ON, "ON", AMBER, hot or self.lit("hon"))
        if hot:
            text(img, 2, 111, f'JOIN "{wifi.HOTSPOT_SSID}"' + (f"  PASSWORD {self.password}" if self.password else ""),
                 AMBER, upper=False)
        else:
            text(img, 2, 111, "ON = PI'S WI-FI SHARES 4G WITH PHONES", FAINT)
        if u:
            button(img, d, self.PLAN, f"PLAN: {u.plan_text()}", GREEN, self.lit("plan"))
            armed = time.monotonic() - self.armed < self.CONFIRM_SECS
            button(img, d, self.REFILL, "TAP AGAIN TO RESET COUNT" if armed else "SIM TOPPED UP: RESET COUNT",
                   RED if armed else AMBER, armed or self.lit("refill"))
        if self.status:
            text(img, 2, 146, self.status[:44], DIM if self.busy and blink() else self.status_colour)


class Keyboard(Pressable):
    """Big-key keyboard. The text is shown as typed (no hidden password)."""
    ROWS_Y = [27, 54, 81, 108, 135]
    KEY_H = 24
    LAYOUTS = {
        "lower": ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"],
        "upper": ["1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"],
        "sym1": ["1234567890", "!@#$%^&*()", "-_=+:;'\"/", ",.?<>|\\"],
        "sym2": ["1234567890", "[]{}~`    ", "         ", "       "],
    }

    def __init__(self, title, on_done):
        super().__init__()
        self.title = title
        self.on_done = on_done
        self.value = ""
        self.mode = "lower"

    def keys(self):
        """-> [(rect, key)] where key is a character or a command name."""
        rows = self.LAYOUTS[self.mode]
        out = []
        for r, chars in enumerate(rows):
            y0 = self.ROWS_Y[r]
            rect = lambda x0, w: (x0, y0, x0 + w - 2, y0 + self.KEY_H - 1)   # noqa: E731
            if r == 3:
                out.append((rect(0, 36), "SHIFT"))
                for i, c in enumerate(chars):
                    out.append((rect(36 + i * 24, 24), c))
                out.append((rect(204, 36), "DEL"))
            else:
                x = 12 if len(chars) == 9 else 0
                for i, c in enumerate(chars):
                    out.append((rect(x + i * 24, 24), c))
        y0 = self.ROWS_Y[4]
        for x0, w, k in ((0, 36, "MODE"), (36, 84, "SPACE"), (120, 60, "CANCEL"), (180, 60, "OK")):
            out.append(((x0, y0, x0 + w - 2, y0 + self.KEY_H - 1), k))
        kx = (LW - 240) // 2                   # the keys span 240 px: centre them
        return [((x0 + kx, y0, x1 + kx, y1), k) for (x0, y0, x1, y1), k in out]

    def tap(self, x, y):
        for rect, k in self.keys():
            if not inside(rect, x, y) or k == " ":
                continue
            self.press(k)
            if k == "SHIFT":
                self.mode = {"lower": "upper", "upper": "lower", "sym1": "sym2", "sym2": "sym1"}[self.mode]
            elif k == "MODE":
                self.mode = "lower" if self.mode.startswith("sym") else "sym1"
            elif k == "DEL":
                self.value = self.value[:-1]
            elif k == "SPACE":
                self.value += " "
            elif k == "CANCEL":
                return "close"
            elif k == "OK":
                nxt = self.on_done(self.value)          # a next screen (e.g. naming a place, then where)
                return ("replace", nxt) if hasattr(nxt, "tap") else "close"
            else:
                self.value += k
            return None
        return None

    def label(self, k):
        if k == "SHIFT":
            return "MORE" if self.mode.startswith("sym") else "ABC" if self.mode == "lower" else "abc"
        if k == "MODE":
            return "abc" if self.mode.startswith("sym") else "#+="
        return k

    def render(self, img, d):
        text(img, 2, 1, self.title[:39], DIM, upper=False)
        shown = self.value[-18:]
        text(img, 4, 10, shown, AMBER, scale=2, upper=False)
        if blink():
            cx = 4 + len(shown) * 12
            d.rectangle([cx, 22, cx + 9, 23], fill=AMBER)
        d.line([0, 25, LW, 25], fill=FAINT)
        for rect, k in self.keys():
            if k == " ":
                continue
            col = AMBER if k in ("OK",) else RED if k == "CANCEL" else GREEN
            if k == "SHIFT" and self.mode in ("upper", "sym2"):
                col = AMBER
            lab = self.label(k)
            scale = 2 if len(lab) == 1 else 1
            button(img, d, rect, lab, col, self.lit(k), scale=scale, upper=False)
