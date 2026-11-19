"""Bluetooth audio to the car stereo, and the AUDIO setting (AUX or BLUETOOTH).

Pairing and connecting go through BlueZ on D-Bus; the sound itself goes
through bluez-alsa (bluealsa.service, A2DP source only). spotify_player plays to
ALSA "default" and the AI page's voice to "voicebus": two channels on the Loopback
card that never go away, so the player's sound device is always there. The Bridge
adds them and plays the sum to the output: the headphone jack
(AUX, "tap") or the stereo ("tap_bt"). A switch only moves the Bridge - Spotify is
never restarted for it (2026-11-15: restarts fire bursts of Web API calls; 42 in a
day got Ian's Spotify app blocked for 21 hours).

The Router only routes to Bluetooth while the stereo is actually connected
(bluez-alsa refuses to open with no device). With BLUETOOTH chosen and the stereo
gone (car switched off, out of range), the Bridge stops and the music is PAUSED and
kept paused - nothing streams to an empty car
(Ian 2026-11-14: it used to play on through AUX, over 4G, all night). When the stereo
comes back, music carries on if it was playing when it went.

Spotify's streaming quality follows the link the internet uses (audio.json
"quality_cell" / "quality_wifi", 96 / 160 / 320 kbps; SET > AUDIO > SPOTIFY QUALITY).
It's the bitrate in spotify_player's app.toml, read at start-up, so a change restarts
the player (a few seconds' gap); only once the link has been the same for QUALITY_SETTLE.
"""
import json
import os
import re
import subprocess
import threading
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

import cell
import spotify

SETTINGS = os.path.expanduser("~/.config/nightrider/audio.json")
ASOUNDRC = os.path.expanduser("~/.asoundrc")      # the old way to route (pointed "default" at tap_bt): removed
OUTPUTS = {"bluetooth": "tap_bt", "aux": "tap"}     # /etc/asound.conf PCM the Bridge plays to, per route
MIX_CAPTURE = "hw:Loopback,1,1"                     # Spotify's channel ("default" = hw:Loopback,0,1)
VOICE_CAPTURE = "hw:Loopback,1,2"                   # the voice's channel ("voicebus" = hw:Loopback,0,2)
BLUEZ = "org.bluez"
AGENT = "/nightrider/agent"
A2DP_SINK = "0000110b-0000-1000-8000-00805f9b34fb"
SCAN_SECS = 12
RETRY = 20            # seconds between tries to reconnect the stereo


STUCK_SECS = 25                        # s of pure silence from a "playing" Spotify before it's restarted
QUALITIES = (96, 160, 320)            # kbps spotify_player can stream at
QUALITY_SETTLE = 15                    # s the internet link must stay put before the quality follows it
QUALITY_GAP = 60                       # s at least between quality restarts


def load():
    """-> {"output": "aux" | "bluetooth", "stereo": MAC or None, "name": str,
    "quality_cell": kbps on 4G, "quality_wifi": kbps on Wi-Fi}"""
    s = {"output": "aux", "stereo": None, "name": "", "quality_cell": 160, "quality_wifi": 320}
    try:
        with open(SETTINGS) as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save(s):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, SETTINGS)


def friendly(e):
    """A D-Bus error -> a short message for the screen."""
    name = getattr(e, "get_dbus_name", lambda: "")() or ""
    msg = {
        "org.bluez.Error.AuthenticationFailed": "PAIRING REFUSED: TRY AGAIN",
        "org.bluez.Error.AuthenticationCanceled": "PAIRING CANCELLED",
        "org.bluez.Error.AuthenticationTimeout": "PAIRING TIMED OUT",
        "org.bluez.Error.ConnectionAttemptFailed": "STEREO DIDN'T ANSWER",
        "org.bluez.Error.InProgress": "ALREADY TRYING...",
        "org.freedesktop.DBus.Error.NoReply": "STEREO DIDN'T ANSWER",
    }.get(name)
    return msg or str(getattr(e, "get_dbus_message", lambda: e)() or name or e).upper()


class Agent(dbus.service.Object):
    """Answers the Pi's side of pairing: accept everything, legacy PIN 0000.
    Only used for pairings we start (it isn't the default agent, and the
    adapter is only pairable while we pair)."""
    IFACE = "org.bluez.Agent1"

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method(IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method(IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        pass

    @dbus.service.method(IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        pass

    @dbus.service.method(IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass


class Bluetooth:
    """BlueZ calls. A GLib loop runs in its own thread so the agent can answer
    while a pairing is in progress; the slow calls (Pair, Connect) are started
    on that loop and waited for here."""

    _shared = {}                               # loop + agent, set up once even if BlueZ isn't ready yet

    def __init__(self):
        sh = Bluetooth._shared
        if not sh:
            dbus.mainloop.glib.threads_init()
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            sh["bus"] = dbus.SystemBus()
            threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
            sh["agent"] = Agent(sh["bus"], AGENT)
        self.bus, self.agent = sh["bus"], sh["agent"]
        mgr = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1")
        mgr.RegisterAgent(AGENT, "DisplayYesNo")
        self.adapter_path = next(p for p, i in self._objects().items() if "org.bluez.Adapter1" in i)

    def _objects(self):
        om = dbus.Interface(self.bus.get_object(BLUEZ, "/"), "org.freedesktop.DBus.ObjectManager")
        return om.GetManagedObjects()

    def _iface(self, path, iface):
        return dbus.Interface(self.bus.get_object(BLUEZ, path), iface)

    def _set(self, path, iface, prop, value):
        self._iface(path, "org.freedesktop.DBus.Properties").Set(iface, prop, value)

    def _path(self, mac):
        return f"{self.adapter_path}/dev_{mac.replace(':', '_')}"

    def _wait(self, path, method, timeout=60):
        """Call Device1.<method> on the GLib thread; raise its error, if any."""
        done, out = threading.Event(), []

        def start():
            fn = getattr(self._iface(path, "org.bluez.Device1"), method)
            fn(reply_handler=lambda: done.set(),
               error_handler=lambda e: (out.append(e), done.set()),
               timeout=timeout)
            return False
        GLib.idle_add(start)
        if not done.wait(timeout + 5):
            raise RuntimeError("STEREO DIDN'T ANSWER")
        if out:
            raise out[0]

    def devices(self):
        """Classic (non-BLE) Bluetooth devices with a name: paired first, then
        audio ones -> [{"mac", "name", "paired", "connected", "audio"}]"""
        out = []
        for path, ifaces in self._objects().items():
            d = ifaces.get("org.bluez.Device1")
            if not d or not path.startswith(self.adapter_path + "/") or "Name" not in d:
                continue
            uuids = [str(u) for u in d.get("UUIDs", [])]
            cls = int(d.get("Class", 0))
            if not cls and not uuids:               # BLE-only gadgets: no use for music
                continue
            out.append({"mac": str(d["Address"]), "name": str(d.get("Alias", d["Name"])),
                        "paired": bool(d.get("Paired")), "connected": bool(d.get("Connected")),
                        "audio": A2DP_SINK in uuids or (cls >> 8) & 0x1F == 4})
        out.sort(key=lambda x: (not x["paired"], not x["audio"], x["name"].lower()))
        return out

    def scan(self, secs=SCAN_SECS):
        a = self._iface(self.adapter_path, "org.bluez.Adapter1")
        a.SetDiscoveryFilter({"Transport": "bredr"})
        try:
            a.StartDiscovery()
        except dbus.DBusException as e:
            if "InProgress" not in (e.get_dbus_name() or ""):
                raise
        try:
            time.sleep(secs)
        finally:
            try:
                a.StopDiscovery()
            except dbus.DBusException:
                pass
        return self.devices()

    def connected(self, mac):
        try:
            p = self._iface(self._path(mac), "org.freedesktop.DBus.Properties")
            return bool(p.Get("org.bluez.Device1", "Connected"))
        except dbus.DBusException:
            return False

    def pair_and_connect(self, mac):
        """Pair (if needed), trust and connect. Raises on failure."""
        path = self._path(mac)
        props = self._iface(path, "org.freedesktop.DBus.Properties")
        if not props.Get("org.bluez.Device1", "Paired"):
            self._set(self.adapter_path, "org.bluez.Adapter1", "Pairable", True)
            try:
                self._wait(path, "Pair", timeout=60)
            finally:
                self._set(self.adapter_path, "org.bluez.Adapter1", "Pairable", False)
        self._set(path, "org.bluez.Device1", "Trusted", True)
        self.connect(mac)

    def connect(self, mac):
        self._wait(self._path(mac), "Connect", timeout=30)

    def forget(self, mac):
        try:
            self._iface(self.adapter_path, "org.bluez.Adapter1").RemoveDevice(self._path(mac))
        except dbus.DBusException:
            pass


def _cli(*args):
    try:
        return subprocess.run(["bluealsa-cli", *args], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def bt_pcm():
    """bluez-alsa's playback PCM for the connected stereo (D-Bus path), or None."""
    return next((p for p in _cli("list-pcms").split() if p.endswith("a2dpsrc/sink")), None)


def fix_rate(pcm):
    """The TR7412UB stereo sometimes reconnects at 16 kHz SBC (sounds like a phone
    call; seen 2026-10-15). Move it to 44.1 kHz -> True if it had to (Spotify's
    open stream goes stale then: restart it). --a2dp-force-audio-cd in the
    bluealsa drop-in only covers connections the Pi sets up."""
    m = re.search(r"^Sampling: (\d+)", _cli("info", pcm), re.M)
    if not m or int(m.group(1)) >= 44100:
        return False
    _cli("codec", "-c", "2", "-s", "44100", pcm, "SBC")
    return True


class Bridge:
    """Adds the music channel (Spotify, hw:Loopback,1,1) and the voice channel (speech.py,
    hw:Loopback,1,2) and plays the sum to the output: two arecords -> numpy -> aplay, in a
    thread. Loopback channels nobody plays into read as silence, so both always flow.
    Restarted by the Router when it dies (stereo link dropped) - cheap, nothing to do with
    Spotify. aplay keeps ~0.5 s buffered against a busy CPU. (A dmix for this broke
    Spotify's audio - see /etc/asound.conf.)"""
    FMT = ["-q", "-t", "raw", "-f", "S16_LE", "-r", "44100", "-c", "2"]
    CHUNK = 1024 * 4                       # bytes: 1024 stereo frames, ~23 ms

    def __init__(self, log):
        self.log = log
        self.out = None                    # PCM name it plays to, None = stopped
        self._procs = []
        self._thread = None
        self._stop = threading.Event()
        self.started_at = 0.0
        self.heard_at = 0.0                # monotonic time Spotify's channel last carried sound

    def start(self, out):
        self.stop()
        self.out = out
        self._stop = threading.Event()
        self.started_at = self.heard_at = time.monotonic()
        try:
            music = subprocess.Popen(["arecord", *self.FMT, "-D", MIX_CAPTURE, "--buffer-time=200000"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            voice = subprocess.Popen(["arecord", *self.FMT, "-D", VOICE_CAPTURE, "--buffer-time=200000"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            play = subprocess.Popen(["aplay", *self.FMT, "-D", out, "--buffer-time=500000"],
                                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.log(f"audio: bridge to {out} failed: {e}")
            self.stop()
            self.out = out                         # still wanted: alive() is False, the Router retries
            return
        self._procs = [music, voice, play]
        self._thread = threading.Thread(target=self._mix, args=(music, voice, play, self._stop), daemon=True)
        self._thread.start()

    def _mix(self, music, voice, play, stop):
        import numpy as np
        voice_ok = True
        try:
            while not stop.is_set():
                a = music.stdout.read(self.CHUNK)
                if len(a) < self.CHUNK:
                    return                         # music capture gone: alive() goes False
                if a.strip(b"\x00"):                # any non-zero sample (cheap: no numpy)
                    self.heard_at = time.monotonic()
                if voice_ok:
                    b = voice.stdout.read(self.CHUNK)
                    if len(b) == self.CHUNK:
                        x = np.frombuffer(a, np.int16).astype(np.int32) + np.frombuffer(b, np.int16)
                        a = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
                    else:
                        voice_ok = False           # voice channel broke: music on alone
                        self.log("audio: voice channel stopped; music carries on")
                play.stdin.write(a)
        except (OSError, ValueError):
            pass                                   # output gone (stereo dropped)

    def stop(self):
        self._stop.set()
        for p in reversed(self._procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
        self._procs = []
        self.out = None

    def alive(self):
        return bool(self._procs and self._thread and self._thread.is_alive()
                    and self._procs[0].poll() is None and self._procs[2].poll() is None)


def player_bitrate(path=None):
    """The bitrate in spotify_player's app.toml ([device] bitrate = N), None if not found."""
    try:
        with open(path or spotify.CONFIG) as f:
            m = re.search(r"^bitrate\s*=\s*(\d+)", f.read(), re.M)
        return int(m.group(1)) if m else None
    except OSError:
        return None


def set_player_bitrate(kbps, path=None):
    path = path or spotify.CONFIG
    with open(path) as f:
        conf = f.read()
    new, n = re.subn(r"^bitrate\s*=\s*\d+", f"bitrate = {kbps}", conf, flags=re.M)
    if not n:
        new = re.sub(r"^\[device\]\s*$", f"[device]\nbitrate = {kbps}", conf, count=1, flags=re.M)
    with open(path + ".tmp", "w") as f:
        f.write(new)
    os.replace(path + ".tmp", path)




class Router:
    """Keeps the sound going where the AUDIO setting says: tries to reconnect
    the stereo while it's away, and moves Spotify between AUX and Bluetooth."""

    def __init__(self, log, player=None):
        self.log = log
        self.player = player
        self.bt = None
        self.error = ""
        self._rate_pcm, self._rate_checks, self._rate_at = None, 0, 0.0    # sample-rate checks (_step)
        self._held = False                 # music was playing when the stereo went: resume when it's back
        self._via, self._via_at, self._quality_at = None, 0.0, -1e9     # internet link + since when
        self._start_bt()
        self.settings = load()
        try:
            os.remove(ASOUNDRC)                # left from the old routing: it bypassed the mixer
        except FileNotFoundError:
            pass
        except OSError as e:
            self.log(f"audio: couldn't remove {ASOUNDRC}: {e}")
        self.bridge = Bridge(log)
        self._bridge_pcm = None                # stereo PCM the bridge was started for
        self._bridge_fails = 0
        self._stuck_since = None               # _check_stuck: when it first looked stuck
        self._playing_since = None             # ...and since when it's been playing
        self.route = None                      # set by the first _step
        self.switching = False
        self._kick = threading.Event()
        self._tried = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def set_quality(self, link, kbps):
        """link "cell" or "wifi": the kbps Spotify streams at over it."""
        self.settings = dict(self.settings, **{f"quality_{link}": kbps})
        save(self.settings)
        self._quality_at = -1e9                # apply soon, not after the usual gap
        self._kick.set()

    def quality_now(self):
        """-> (link "cell"/"wifi"/None, kbps wanted for it)."""
        via = cell.internet_via()
        link = "cell" if via == "4G" else "wifi" if via else None
        return link, self.settings.get(f"quality_{link}") if link else None

    def set_output(self, output, stereo=None, name=None):
        s = dict(self.settings, output=output)
        if stereo is not None:
            s["stereo"], s["name"] = stereo, name or stereo
        self.settings = s
        save(s)
        self._tried = 0.0
        self._kick.set()

    def forget_stereo(self):
        mac = self.settings.get("stereo")
        if mac and self.bt:
            self.bt.forget(mac)
        self.settings = dict(self.settings, output="aux", stereo=None, name="")
        save(self.settings)
        self._kick.set()

    @property
    def stereo_connected(self):
        mac = self.settings.get("stereo")
        return bool(mac and self.bt and self.bt.connected(mac))

    def _start_bt(self):
        """BlueZ can still be starting at boot, so this is retried from _run."""
        try:
            self.bt = Bluetooth()
            if self.error:
                self.log("bluetooth: ready")
            self.error = ""
        except Exception as e:                 # no Bluetooth (yet): AUX only
            if not self.error:
                self.log(f"bluetooth: {e}")
            self.error = f"BLUETOOTH UNAVAILABLE: {e}"

    def _run(self):
        while True:
            if not self.bt:
                self._start_bt()
            try:
                self._step()
            except Exception as e:
                self.log(f"audio router: {e}")
            self._kick.wait(5)
            self._kick.clear()

    def _step(self):
        s = self.settings
        want_bt = s["output"] == "bluetooth" and s.get("stereo") and self.bt
        up = bool(want_bt and self.bt.connected(s["stereo"]))
        if want_bt and not up and time.monotonic() - self._tried > RETRY:
            self._tried = time.monotonic()
            try:
                self.bt.connect(s["stereo"])
                up = True
                self.log(f"bluetooth: connected {s['name']}")
            except Exception:
                pass                           # out of range / stereo off: try again later
        pcm = None
        if up:                                 # bluez-alsa takes a moment after Connect
            for _ in range(10):
                pcm = bt_pcm()
                if pcm:
                    break
                time.sleep(0.5)
            else:
                up = False
        route = "bluetooth" if up else "aux"
        lost = bool(want_bt and not up)        # BLUETOOTH chosen, stereo gone: AUX, but paused
        # The sample-rate check talks to bluealsa (GetCodecs) on the stereo's link, so only
        # right after it connects and once more ~20 s on - not every 5 s (2026-10-18: audio
        # hiccups on a link already flooded with empty frames from the stereo).
        if pcm != self._rate_pcm:
            self._rate_pcm, self._rate_checks = pcm, 0
        due = pcm and self._rate_checks < 2 and time.monotonic() - self._rate_at > (0 if not self._rate_checks else 20)
        if due:
            self._rate_checks += 1
            self._rate_at = time.monotonic()
        if due and fix_rate(pcm):
            self.log("bluetooth: stereo was at low quality (under 44.1 kHz), fixed")
            if route == self.route:            # the bridge's stream went stale: open it again
                self._switch(route, resume=False)
        if route != self.route and self.route is None:      # first step: just set up
            self._switch(route, resume=False, lost=lost)
        elif route != self.route:
            if lost:
                p = self.player
                self._held = bool(p and p.playing and p.device == spotify.DEVICE)
                self.log("bluetooth: stereo gone: pausing the music" + (" (resumes when it's back)" if self._held else ""))
                self._pause()
                self._switch(route, resume=False, lost=True)
            else:
                self._switch(route, resume=True if route == "bluetooth" and self._held else None)
                self._held = False
        elif lost:
            if self.bridge.out:                # BLUETOOTH chosen with the stereo away: no sound anywhere
                self.bridge.stop()
            self._keep_paused()
        else:
            self._check_bridge(pcm)
            self._check_stuck()
        self._follow_quality()

    def _pause(self):
        try:
            spotify.run("playback", "pause", timeout=10)
        except (RuntimeError, OSError):
            pass

    def _keep_paused(self):
        """Stereo gone: anything that starts playing (a resume, a tap) is paused again."""
        p = self.player
        if p and p.playing and p.device == spotify.DEVICE and not p.error and time.monotonic() - p.at < 10:
            self.log("bluetooth: stereo not connected: pausing")
            self._pause()
            p.refresh()

    def _follow_quality(self):
        """Stream quality for the link in use: restart the player when it should change."""
        link, kbps = self.quality_now()
        now = time.monotonic()
        if link != self._via:
            self._via, self._via_at = link, now
            return
        if not link or not kbps or now - self._via_at < QUALITY_SETTLE or now - self._quality_at < QUALITY_GAP:
            return
        have = player_bitrate()
        if have is None or have == kbps:
            return
        self._quality_at = now
        self.log(f"audio: spotify quality {have} -> {kbps} kbps (internet over {'4G' if link == 'cell' else 'Wi-Fi'})")
        try:
            set_player_bitrate(kbps)
        except OSError as e:
            self.log(f"audio: couldn't set the quality: {e}")
            return
        p = self.player
        was_playing = bool(p and p.playing)        # (stereo gone: it's paused anyway)
        if not spotify.restart(f"stream quality {kbps} kbps", self.log):
            return
        if was_playing:
            spotify.resume_here(wait=60)

    def _check_bridge(self, pcm):
        """The bridge died (the stereo's stream went away: a quick reconnect, a dropped
        link) or the stereo came back on a new stream: open it again. Never restarts
        Spotify (it plays into the mixer, which never goes away). Backs off if it keeps
        dying at once, and logs only the first few."""
        b = self.bridge
        stale = self.route == "bluetooth" and pcm and self._bridge_pcm and pcm != self._bridge_pcm
        if b.alive() and not stale:
            if time.monotonic() - b.started_at > 30:
                self._bridge_fails = 0
            return
        wait = min(5 * 2 ** self._bridge_fails, 60) if self._bridge_fails else 0
        if time.monotonic() - b.started_at < wait:
            return
        self._bridge_fails += 1
        if self._bridge_fails <= 3:
            self.log(f"audio: sound to the {self.route} {'moved to a new stream' if stale else 'stopped'}: reopening")
        b.start(OUTPUTS[self.route])
        self._bridge_pcm = pcm if self.route == "bluetooth" else None

    def _check_stuck(self):
        """Spotify says it's playing here but the player is making no sound: restart it.
        2026-11-16 17:58: the player's connection to Spotify died (websocket closed); it
        played out the loaded song, then silence, skips ignored - while the Watchdog saw
        "924S" in the device list (a stale second "924S" was there) and called it healthy.
        Two signs, either is enough, both from things already at hand (no extra API calls):
        - the bridge has had nothing but digital silence from Spotify's channel for STUCK_SECS;
        - the reported position has run past the end of the song (a frozen, stale state).
        Restarts go through spotify.restart (rationed, none while Spotify blocks us)."""
        p = self.player
        now = time.monotonic()
        fresh = bool(p and not p.error and now - p.at < 10)
        playing = bool(fresh and p.playing and p.device == spotify.DEVICE)
        if not playing or self.switching:
            self._stuck_since = self._playing_since = None
            return
        if self._playing_since is None:
            self._playing_since = now              # silence only counts from when it (re)started
        state = p.state or {}
        dur = ((state.get("item") or {}).get("duration_ms")) or 0
        past_end = bool(dur and (state.get("progress_ms") or 0) > dur + 15000)
        b = self.bridge
        since = max(b.started_at, self._playing_since)
        silent = b.alive() and now - since > STUCK_SECS and now - max(b.heard_at, since) > STUCK_SECS
        if not (past_end or silent):
            self._stuck_since = None
            return
        if self._stuck_since is None:
            self._stuck_since = now                # once more (~5 s) before acting
            return
        if not spotify.online():
            return
        self._stuck_since = None
        why = "its position is past the end of the song" if past_end else f"no sound for {STUCK_SECS} s"
        self.log(f"spotify: says it's playing but {why}: restarting the player")
        if spotify.restart("playing but silent", self.log):
            threading.Thread(target=spotify.resume_here, kwargs={"wait": 60}, daemon=True).start()
            self.bridge.heard_at = now + 60        # give it a minute to come back before judging again

    def _switch(self, route, resume=None, lost=False):
        """Send the sound to route ("aux"/"bluetooth"; lost = BLUETOOTH chosen and the
        stereo gone: nowhere). resume: None = carry on if it was playing, True = play,
        False = don't. Spotify keeps running: it plays into the mixer either way."""
        self.switching = True
        try:
            self.log(f"audio: switching to {'nothing (stereo gone)' if lost else route}")
            p = self.player
            was_playing = bool(p and p.playing) if resume is None else resume
            self.route = route
            self._bridge_fails = 0
            if lost:
                self.bridge.stop()
            else:
                self.bridge.start(OUTPUTS[route])
                self._bridge_pcm = bt_pcm() if route == "bluetooth" else None
            if was_playing:                    # pick the music back up
                try:
                    spotify.run("playback", "play", timeout=10)
                except (RuntimeError, OSError) as e:
                    self.log(f"audio: couldn't resume the music: {e}")
            if p:
                p.refresh()
        finally:
            self.switching = False
