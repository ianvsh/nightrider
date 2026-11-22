"""Terminal page: a login shell as ian, drawn in the pixel font, typed on with a
USB keyboard. The shell runs in a pty and pyte keeps the screen; keyboards are
read with evdev and grabbed, so keys don't also reach the Linux console."""

import fcntl
import os
import pty
import pwd
import select
import signal
import struct
import subprocess
import termios
import threading
import time

import pyte

from pixels import AMBER, BG, DIM, GREEN, RED, blink, glyph, plain_char

COLS, ROWS = 66, 26               # fills the page drawn at 2x (display.py TERM_SCALE), not 3x
CELL_W, CELL_H = 6, 8
WHEEL_LINES = 2                   # finger travel (in text lines) per mouse-wheel step in tmux
STUCK_S = 3                       # typed bytes the tmux client hasn't taken for this long: reconnect it
DECCKM = 1 << 5                   # pyte's private mode 1: arrows send ESC O x (less, vim)
SUBST = {"│": "|", "─": "-", "┌": "+", "┐": "+", "└": "+", "┘": "+", "├": "+", "┤": "+",
         "┬": "+", "┴": "+", "┼": "+", "█": "#", "▒": "#", "░": ".", "•": "*", "→": ">", "←": "<"}
COLOURS = {"red": RED, "brown": AMBER, "yellow": AMBER, "blue": AMBER, "magenta": AMBER,
           "black": DIM, "default": GREEN}


def colour(name, default=GREEN):
    """pyte colour (a name or 'rrggbb') -> one of the screen's few colours."""
    if name in COLOURS:
        return COLOURS[name] if name != "default" else default
    if len(name) == 6:
        try:
            r, g, b = (int(name[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default
        if max(r, g, b) < 60:
            return DIM
        if r > 150 and g < 100:
            return RED
        if r > 150 or b > max(r, g):
            return AMBER
    return default


class Terminal:
    def __init__(self, wake=None, rows=ROWS, cols=COLS, tmux="term", log=print):
        self.wake = wake
        self.tmux = tmux
        self.log = log
        self.screen = pyte.HistoryScreen(cols, rows, history=1000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        self.fd = self.proc = None
        self.ended = False
        self.in_tmux = False           # attached to nightrider-term's tmux: scroll back with its copy mode
        self._tmux_back = False        # we put tmux in copy mode (cancelled before the next key)
        self._pending = 0              # finger travel not yet turned into scrolling, in lines (+ = back)
        self._out = b""                # typed bytes the program hasn't taken yet (the pty is non-blocking)
        self._out_since = None         # monotonic time they started waiting
        self._wlock = threading.Lock()
        self._revive = False           # reconnect as soon as the stuck client is gone
        self._unread_since = None      # typed bytes sitting unread by the program since then

    @property
    def alive(self):
        return self.fd is not None

    def start(self):
        """Attach to the tmux session (again, if it ended), else a plain login shell."""
        if self.alive:
            return
        user = pwd.getpwuid(os.getuid())
        with self.lock:
            self.screen.reset()
        self.ended = False
        fd, tty = pty.openpty()
        fcntl.ioctl(tty, termios.TIOCSWINSZ, struct.pack("HHHH", self.screen.lines, self.screen.columns, 0, 0))
        env = {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name,
               "SHELL": user.pw_shell, "TERM": "linux", "LANG": "C.UTF-8",
               "PATH": f"{user.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"}
        # The shell lives in nightrider-term.service's tmux, so it outlives display restarts;
        # if that isn't running, a login shell of our own (which dies with the display).
        # The tmux client runs straight under setsid (not inside `sh -c`): a session leader
        # whose parent is in another session is an orphaned process group, so the kernel
        # ignores stop signals for it and tmux can never suspend itself and stop reading keys.
        self.in_tmux = self._tmux_cmd("has-session", "-t", "term")
        cmd = ["tmux", "-L", self.tmux, "attach", "-t", "term"] if self.in_tmux else [user.pw_shell or "/bin/bash", "-l"]
        # setsid -c: a new session with the pty as its terminal, so ctrl-c and job control work
        self.proc = subprocess.Popen(["setsid", "-c", *cmd],
                                     stdin=tty, stdout=tty, stderr=tty, cwd=user.pw_dir, env=env)
        os.set_blocking(fd, False)         # typing must never hang the keyboard thread (see _send)
        with self._wlock:
            self._out, self._out_since = b"", None
        self._unread_since = None
        self.fd = fd
        threading.Thread(target=self._read, args=(tty,), daemon=True).start()

    def _read(self, tty):
        """Screen output in; also keeps an eye on the tmux client. tty: the program's
        end of the pty, kept open to see how much typing it hasn't read yet."""
        fd, proc = self.fd, self.proc
        while proc.poll() is None:         # (our tty copy means no EOF when it exits)
            try:
                r, w, _ = select.select([fd], [fd] if self._out else [], [], 0.5)
                if w:
                    self._flush()
                if self.in_tmux and self._stuck(tty):
                    self.log(f"terminal: tmux took no keys for {STUCK_S}s: reconnecting")
                    self._revive = True
                    os.killpg(proc.pid, signal.SIGKILL)      # tmux and Claude live on in the server
                    break
                if not r:
                    continue
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if not data:
                break
            with self.lock:
                self.stream.feed(data)
            if self.wake:
                self.wake.set()
        os.close(tty)
        os.close(fd)
        proc.wait()
        self.fd = self.proc = None
        self.ended = True
        if self._revive:
            self._revive = False
            self.start()
        if self.wake:
            self.wake.set()

    def write(self, data):
        """Keyboard bytes to the shell. Any key restarts a shell that ended."""
        if not self.alive:
            self.start()
            return
        self.bottom()
        self._send(data)

    def _send(self, data):
        """Queue bytes for the program and push what the pty takes now. Never blocks:
        what's left goes when the program reads it (_read), and if it stops reading
        altogether, _read reconnects it."""
        with self._wlock:
            if not self._out:
                self._out_since = time.monotonic()
            self._out += data
        self._flush()

    def _flush(self):
        with self._wlock:
            fd = self.fd
            if not self._out or fd is None:
                return
            try:
                n = os.write(fd, self._out)
            except BlockingIOError:
                return
            except OSError:
                self._out, self._out_since = b"", None
                return
            self._out = self._out[n:]
            self._out_since = time.monotonic() if self._out else None

    def _stuck(self, tty):
        """tmux has stopped taking keys for STUCK_S. The tmux *server* reads the
        client's terminal itself, with it in raw mode, so typing never waits there.
        Stuck = typing left unread (in our buffer or the pty), or the terminal back in
        normal line mode, which is how a suspended client leaves it: typing then just
        echoes on the TERM page and never reaches Claude."""
        try:
            unread = struct.unpack("i", fcntl.ioctl(tty, termios.FIONREAD, b"\0" * 4))[0]
            cooked = bool(termios.tcgetattr(tty)[3] & termios.ICANON)
        except (OSError, termios.error):
            unread, cooked = 0, False
        now = time.monotonic()
        if unread or self._out or cooked:
            self._unread_since = self._unread_since or now
        else:
            self._unread_since = None
        return self._unread_since is not None and now - self._unread_since > STUCK_S

    @property
    def app_cursor(self):
        return DECCKM in self.screen.mode

    def scroll(self, up):
        """A page back (up) or forward: Shift+PgUp / PgDn."""
        if self.in_tmux:
            self.scroll_lines(self.screen.lines // 2 if up else -(self.screen.lines // 2))
            return
        with self.lock:
            (self.screen.prev_page if up else self.screen.next_page)()

    def scroll_lines(self, n):
        """Finger drag: n lines back (n > 0) or forward. In tmux: mouse-wheel events
        (tmux has mouse on), so tmux does the right thing - a full-screen program that
        takes the mouse (Claude Code, whose alternate screen tmux keeps no history for)
        scrolls itself; a plain shell goes into tmux's copy mode ([line/total] in the
        corner; scrolling back to the bottom leaves it). One wheel step per WHEEL_LINES
        of finger travel (tmux steps 5 lines, Claude Code 1-3). Without tmux, pyte's
        history in half pages."""
        if not n:
            return
        if not self.in_tmux:
            self._pending += n
            half = max(1, self.screen.lines // 2)
            while abs(self._pending) >= half:
                up = self._pending > 0
                self._pending -= half if up else -half
                with self.lock:
                    (self.screen.prev_page if up else self.screen.next_page)()
            return
        self._pending += n
        steps = int(self._pending / WHEEL_LINES)
        if not steps or not self.alive:
            return
        self._pending -= steps * WHEEL_LINES
        if steps > 0:
            self._tmux_back = True
        # SGR mouse report: button 64 = wheel up, 65 = down, at the middle of the screen
        at = f";{self.screen.columns // 2};{self.screen.lines // 2}M".encode()
        self._send(((b"\x1b[<64" if steps > 0 else b"\x1b[<65") + at) * abs(steps))

    def _tmux_cmd(self, *args):
        try:
            return subprocess.run(["tmux", "-L", self.tmux, *args], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def bottom(self):
        if self._tmux_back:                # leave copy mode first, or the key would go to it
            self._tmux_back = False
            self._pending = 0
            self._tmux_cmd("send-keys", "-t", "term", "-X", "cancel")
        with self.lock:
            while self.scrolled:
                self.screen.next_page()

    @property
    def scrolled(self):
        h = self.screen.history
        return h.position < h.size and bool(h.bottom)

    def draw(self, img, d, x0, y0):
        with self.lock:
            s = self.screen
            cx, cy = s.cursor.x, s.cursor.y
            show_cursor = not s.cursor.hidden and not self.scrolled
            for y in range(s.lines):
                line = s.buffer[y]
                py = y0 + y * CELL_H
                for x in range(s.columns):
                    ch = line[x]
                    fg, bg = colour(ch.fg), None
                    if ch.bg != "default":
                        bg = colour(ch.bg, DIM)
                    if ch.reverse:
                        fg, bg = bg or BG, fg
                    if show_cursor and (x, y) == (cx, cy) and blink():
                        fg, bg = BG, GREEN
                    px = x0 + x * CELL_W
                    if bg:
                        d.rectangle([px, py - 1, px + CELL_W - 1, py + CELL_H - 2], fill=bg)
                    c = ch.data
                    if c and c != " ":
                        c = plain_char(SUBST.get(c, c))[:1] or "?"
                        g = glyph(c, 1)
                        img.paste(fg, (px, py, px + g.width, py + g.height), g)
        if self.ended:
            msg = "SHELL ENDED - PRESS A KEY"
            rows, cols = self.screen.lines, self.screen.columns
            d.rectangle([x0, y0 + (rows - 1) * CELL_H - 1, x0 + cols * CELL_W - 1, y0 + rows * CELL_H - 2], fill=BG)
            for i, c in enumerate(msg):
                if c != " ":
                    g = glyph(c, 1)
                    img.paste(AMBER, (x0 + i * CELL_W, y0 + (rows - 1) * CELL_H), g)


# ---- keyboards ----

# evdev key name (without KEY_) -> (plain, shifted), US layout
CHARS = {**{c: (c.lower(), c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
         "1": ("1", "!"), "2": ("2", "@"), "3": ("3", "#"), "4": ("4", "$"), "5": ("5", "%"),
         "6": ("6", "^"), "7": ("7", "&"), "8": ("8", "*"), "9": ("9", "("), "0": ("0", ")"),
         "MINUS": ("-", "_"), "EQUAL": ("=", "+"), "LEFTBRACE": ("[", "{"), "RIGHTBRACE": ("]", "}"),
         "SEMICOLON": (";", ":"), "APOSTROPHE": ("'", '"'), "GRAVE": ("`", "~"),
         "BACKSLASH": ("\\", "|"), "COMMA": (",", "<"), "DOT": (".", ">"), "SLASH": ("/", "?"),
         "SPACE": (" ", " "), "102ND": ("\\", "|"),
         "KP0": ("0", "0"), "KP1": ("1", "1"), "KP2": ("2", "2"), "KP3": ("3", "3"), "KP4": ("4", "4"),
         "KP5": ("5", "5"), "KP6": ("6", "6"), "KP7": ("7", "7"), "KP8": ("8", "8"), "KP9": ("9", "9"),
         "KPDOT": (".", "."), "KPSLASH": ("/", "/"), "KPASTERISK": ("*", "*"), "KPMINUS": ("-", "-"),
         "KPPLUS": ("+", "+")}
SPECIAL = {"ENTER": "\r", "KPENTER": "\r", "BACKSPACE": "\x7f", "TAB": "\t", "ESC": "\x1b",
           "INSERT": "\x1b[2~", "DELETE": "\x1b[3~", "HOME": "\x1b[1~", "END": "\x1b[4~",
           "PAGEUP": "\x1b[5~", "PAGEDOWN": "\x1b[6~",
           "F1": "\x1b[[A", "F2": "\x1b[[B", "F3": "\x1b[[C", "F4": "\x1b[[D", "F5": "\x1b[[E",
           "F6": "\x1b[17~", "F7": "\x1b[18~", "F8": "\x1b[19~", "F9": "\x1b[20~", "F10": "\x1b[21~",
           "F11": "\x1b[23~", "F12": "\x1b[24~"}
ARROWS = {"UP": "A", "DOWN": "B", "RIGHT": "C", "LEFT": "D"}
KEYLOG = os.environ.get("NIGHTRIDER_KEYLOG") == "1"   # debug: log every key press
MODS = {"LEFTSHIFT": "shift", "RIGHTSHIFT": "shift", "LEFTCTRL": "ctrl", "RIGHTCTRL": "ctrl",
        "LEFTALT": "alt", "RIGHTALT": "alt"}


def key_bytes(key, mods, caps=False, app_cursor=False):
    """A key press -> bytes for the shell, ("scroll", up) for shift+PgUp/PgDn, or None.
    key: evdev name without KEY_; mods: set of "shift"/"ctrl"/"alt"."""
    shift = "shift" in mods
    if shift and key in ("PAGEUP", "PAGEDOWN"):
        return ("scroll", key == "PAGEUP")
    if key in ARROWS:
        s = ("\x1bO" if app_cursor else "\x1b[") + ARROWS[key]
    elif key in SPECIAL:
        s = SPECIAL[key]
        if key == "TAB" and shift:
            s = "\x1b[Z"
    elif key in CHARS:
        plain, shifted = CHARS[key]
        upper = shift != (caps and plain.isalpha())
        s = shifted if upper else plain
        if "ctrl" in mods:
            if s.isalpha() or s in "@[\\]^_":
                s = chr(ord(s.upper()) & 31)
            elif s == " ":
                s = "\x00"
            elif s == "/":
                s = "\x1f"
            else:
                return None
    else:
        return None
    if "alt" in mods:
        s = "\x1b" + s
    return s.encode()


def is_keyboard(dev, ec):
    keys = dev.capabilities().get(ec.EV_KEY, [])
    return all(k in keys for k in (ec.KEY_A, ec.KEY_Z, ec.KEY_ENTER, ec.KEY_SPACE))


def is_escape(name, mods):
    """Ctrl + Alt + Delete (either Ctrl/Alt key): the way back to the text console."""
    mods = set(mods)
    return name == "DELETE" and "ctrl" in mods and "alt" in mods


class Keyboards:
    """Finds USB/Bluetooth keyboards (plugged in any time) and sends their keys
    to on_key(name, mods, caps). .count = keyboards connected now.

    One keyboard can be several devices: Ian's MINI 60 HE dongle has two with
    letters (event1 and event5), and a modifier can go down on one and up on the
    other. So Shift/Ctrl/Alt are one shared set, and before each key it's checked
    against what the kernel says is held on any of them: a lost release can't leave
    Ctrl or Alt stuck down, turning every letter into an invisible shortcut."""

    def __init__(self, on_key, log=print):
        self.on_key = on_key
        self.log = log
        self.open = set()
        self.devs = {}                 # path -> evdev device, while it's being read
        self.mods = {}                 # held modifier key name (LEFTCTRL...) -> "ctrl"...
        self.caps = False
        self._lock = threading.Lock()
        threading.Thread(target=self._scan, daemon=True).start()

    @property
    def count(self):
        return len(self.open)

    def _scan(self):
        try:
            import evdev
        except ImportError:
            self.log("python3-evdev missing: no keyboard")
            return
        while True:
            for path in evdev.list_devices():
                if path in self.open:
                    continue
                try:
                    dev = evdev.InputDevice(path)
                    if is_keyboard(dev, evdev.ecodes):
                        self.open.add(path)
                        threading.Thread(target=self._read, args=(dev, evdev.ecodes), daemon=True).start()
                    else:
                        dev.close()
                except OSError:
                    pass
            time.sleep(2)

    def escape(self, dev):
        """Ctrl-Alt-Del: let go of the keyboards and stop the screen (nightrider-console.service).
        Done here, not in on_key, so it works even when the rest of the screen is broken."""
        self.log("keyboard: Ctrl-Alt-Del: back to the text console")
        for d in list(self.devs.values()):
            try:
                d.ungrab()
            except OSError:
                pass
        subprocess.Popen(["sudo", "-n", "systemctl", "start", "--no-block", "nightrider-console.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def held_mods(self, ec):
        """The shared modifiers, minus any no keyboard device has down right now."""
        with self._lock:
            if self.mods:
                down = set()
                for d in list(self.devs.values()):
                    try:
                        down.update(d.active_keys())
                    except OSError:
                        return frozenset(self.mods.values())      # can't check: trust the events
                for key in [k for k in self.mods if getattr(ec, "KEY_" + k, None) not in down]:
                    self.log(f"keyboard: {key} was stuck down (its release got lost): let go")
                    del self.mods[key]
            return frozenset(self.mods.values())

    def _read(self, dev, ec):
        self.log(f"keyboard: {dev.name} on {dev.path}")
        self.devs[dev.path] = dev
        try:
            try:
                dev.grab()                     # keys go to the screen only, not the console
            except OSError:
                pass
            for ev in dev.read_loop():
                if ev.type != ec.EV_KEY:
                    continue
                name = ec.KEY.get(ev.code)
                if isinstance(name, list):
                    name = name[0]
                if not name or not name.startswith("KEY_"):
                    continue
                name = name[4:]
                if name in MODS:
                    with self._lock:
                        if ev.value:
                            self.mods[name] = MODS[name]
                        else:
                            self.mods.pop(name, None)
                elif ev.value == 1 and is_escape(name, self.mods.values()):
                    self.escape(dev)           # Ctrl-Alt-Del: back to the text console
                elif ev.value:                 # 1 = press, 2 = auto-repeat
                    if KEYLOG:
                        self.log(f"keyboard: key {name} on {dev.path}")
                    if name == "CAPSLOCK" and ev.value == 1:
                        self.caps = not self.caps
                    else:
                        try:
                            self.on_key(name, self.held_mods(ec), self.caps)
                        except Exception as e:        # one bad key must not kill this keyboard
                            self.log(f"keyboard: {name} failed: {e!r}")
        except OSError:
            pass
        finally:
            self.log(f"keyboard gone: {dev.path}")
            self.devs.pop(dev.path, None)
            self.open.discard(dev.path)
