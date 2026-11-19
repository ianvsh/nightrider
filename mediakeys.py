"""Media keys -> Spotify. The car stereo's buttons arrive over Bluetooth (AVRCP),
and BlueZ turns them into key presses on an input device named after the stereo,
"TR7412UB-OR (AVRCP)"; a keyboard's media keys work the same way. Any input device
with media keys is read (found any time, rescanned every 2 s, like terminal.Keyboards)
and its keys drive the MUSIC page's player: fast-forward and next = next track (Ian's
ask), rewind and previous = back, play/pause toggles. Typing keyboards are left to
terminal.Keyboards, which grabs them."""
import threading
import time

from terminal import is_keyboard

ACTIONS = {"PLAYPAUSE": "toggle", "PLAY": "play", "PLAYCD": "play", "PAUSE": "pause", "PAUSECD": "pause",
           "STOP": "pause", "STOPCD": "pause", "NEXTSONG": "next", "FASTFORWARD": "next",
           "PREVIOUSSONG": "previous", "REWIND": "previous"}
REPEAT_S = 0.4                 # the same action again this soon is one press sent twice (some stereos do)


def has_media_keys(dev, ec):
    keys = dev.capabilities().get(ec.EV_KEY, [])
    return any(getattr(ec, "KEY_" + k, None) in keys for k in ACTIONS)


class MediaKeys:
    def __init__(self, player, log=print, scan=True):
        self.player, self.log = player, log
        self.open = set()
        self.last = (None, 0.0)                       # (action, monotonic time) of the last one done
        if scan:
            threading.Thread(target=self._scan, daemon=True).start()

    def act(self, key, source=""):
        """One key press (evdev name without KEY_) -> the player."""
        action = ACTIONS.get(key)
        p = self.player
        if not action or not p:
            return None
        now = time.monotonic()
        if self.last[0] == action and now - self.last[1] < REPEAT_S:
            return None
        self.last = (action, now)
        self.log(f"music: {key} from {source or 'a media key'} -> {action}")
        if action == "next":
            p.next()
        elif action == "previous":
            p.previous()
        elif action == "toggle" or (action == "play") != p.playing:    # play when paused, pause when playing
            p.toggle()
        return action

    def _scan(self):
        try:
            import evdev
        except ImportError:
            return
        ec = evdev.ecodes
        while True:
            for path in evdev.list_devices():
                if path in self.open:
                    continue
                try:
                    dev = evdev.InputDevice(path)
                    if has_media_keys(dev, ec) and not is_keyboard(dev, ec):
                        self.open.add(path)
                        threading.Thread(target=self._read, args=(dev, ec), daemon=True).start()
                    else:
                        dev.close()
                except OSError:
                    pass
            time.sleep(2)

    def _read(self, dev, ec):
        self.log(f"music: media keys on {dev.name} ({dev.path})")
        try:
            for ev in dev.read_loop():
                if ev.type != ec.EV_KEY or ev.value != 1:         # presses only: no releases or repeats
                    continue
                name = ec.KEY.get(ev.code)
                for n in (name if isinstance(name, list) else [name]):
                    if n and n.startswith("KEY_") and n[4:] in ACTIONS:
                        try:
                            self.act(n[4:], dev.name)
                        except Exception as e:                  # one bad press mustn't stop the buttons
                            self.log(f"music: {n} failed: {e!r}")
                        break
        except OSError:
            pass
        finally:
            self.log(f"music: media keys gone: {dev.name}")
            self.open.discard(dev.path)
