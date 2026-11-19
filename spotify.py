"""Spotify for the touchscreen and for Claude: a thin wrapper round the
spotify_player CLI (stdlib only, so approve_mcp.py can use it too).

The player itself runs as spotify.service: spotify_player in a detached tmux
session, a Spotify Connect device called "924S" that plays through the Pi's
audio out. Its CLI talks to that running instance. Logged in as Ian's account
with his own Spotify developer app key (client_id in
~/.config/spotify-player/app.toml); tokens in ~/.cache/spotify-player.

Note: the CLI exits 0 even when Spotify refuses a command, so the screen
trusts the next playback poll, not the exit code.

Spotify Radio doesn't work with a personal developer key (spotify_player's radio
needs the bulk GET /v1/tracks?ids=..., which Spotify answers 403 for dev-mode
apps, and Song Radio playlists can't be played through the API either). So a
"radio" is Claude's own pick of similar songs, played as one list with
play_songs(), which calls the Web API directly with spotify_player's token.
"""
import concurrent.futures
import glob
import json
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

import config
import taste

SP = shutil.which("spotify_player") or os.path.expanduser("~/.local/bin/spotify_player")
CONFIG = os.path.expanduser("~/.config/spotify-player/app.toml")


def _device_name(path=CONFIG):
    """This Pi's Spotify Connect name: [device] name in app.toml."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f).get("device", {}).get("name") or "nightrider"
    except (OSError, tomllib.TOMLDecodeError):
        return "nightrider"


DEVICE = _device_name()
CACHE = os.path.expanduser("~/.cache/spotify-player")
PORT = 8080                        # client_port in app.toml: the running player listens here (UDP)
LOGIN_PORT = 8989                  # login_redirect_uri: only open while it waits for a new login
REACH = ("accounts.spotify.com", 443)   # what the player needs first (token refresh)


def _listening(port, tables, state=None):
    want = f"{port:04X}"
    for path in tables:
        try:
            with open(path) as f:
                for line in list(f)[1:]:
                    cols = line.split()
                    if cols[1].rsplit(":", 1)[1] == want and (state is None or cols[3] == state):
                        return True
        except (OSError, IndexError):
            pass
    return False


def player_up():
    """The spotify.service player is listening. If it isn't, the CLI starts a
    whole player of its own, which takes the port and stops the real one
    from starting (seen 2026-10-15), so run() won't call the CLI then."""
    return _listening(PORT, ("/proc/net/udp", "/proc/net/udp6"))


def waiting_for_login():
    """The player couldn't refresh its token and is waiting for an interactive
    login. It does that when it starts with no internet, and never retries."""
    return _listening(LOGIN_PORT, ("/proc/net/tcp", "/proc/net/tcp6"), state="0A")     # 0A = LISTEN


def online(timeout=4):
    """Spotify is reachable (DNS + a TCP connection). False on the Nightrider
    hotspot, or a phone hotspot that has no data yet."""
    try:
        socket.create_connection(REACH, timeout=timeout).close()
        return True
    except OSError:
        return False


def network_id():
    """The default route as "interface:gateway", "" if there's none (the Nightrider
    hotspot has none). Changes when the Pi moves to another network: a free check
    (/proc), so the Watchdog can look at the player as soon as that happens."""
    try:
        with open("/proc/net/route") as f:
            for line in list(f)[1:]:
                cols = line.split()
                if cols[1] == "00000000" and int(cols[3], 16) & 2:        # destination 0, RTF_GATEWAY
                    return f"{cols[0]}:{cols[2]}"
    except (OSError, IndexError, ValueError):
        pass
    return ""


last_restart = 0.0                 # monotonic time of the last restart()
hold_until = 0.0                   # spotify.service stopped on purpose (the AI page's voice) until then


# Every player start fires a burst of Web API calls (profile, playlists, library) on
# Ian's developer app. 2026-11-15: 42 restarts in a day (the AI voice stopped and started
# it for every answer, plus testing) and Spotify blocked the app for 21 hours. So restarts
# are rationed - RESTARTS_PER_HOUR, counted across processes in RESTART_FILE - and none
# while Spotify's block lasts (a start then only makes the block longer).
RESTART_FILE = os.path.expanduser("~/.cache/nightrider/spotify-restarts")
RESTARTS_PER_HOUR = 4


def _restarts_last_hour(now):
    try:
        with open(RESTART_FILE) as f:
            times = [float(t) for t in f.read().split()]
    except (OSError, ValueError):
        times = []
    return [t for t in times if now - 3600 < t <= now]


def restart_allowed():
    """-> "" if a restart may go ahead now, else why not (for the log / screen)."""
    until = blocked_until()
    if until:
        return f"Spotify asks us to wait until {time.strftime('%H:%M', time.localtime(until))}"
    if len(_restarts_last_hour(time.time())) >= RESTARTS_PER_HOUR:
        return f"already restarted {RESTARTS_PER_HOUR} times in the last hour"
    return ""


def restart(reason="", log=print):
    """Restart spotify.service without waiting (it holds off starting the player until
    Spotify is reachable, which can take minutes). Rationed: -> False (and logs why)
    if it isn't allowed now."""
    global last_restart
    why_not = restart_allowed()
    if why_not:
        log(f"spotify: not restarting the player{' for ' + reason if reason else ''}: {why_not}")
        return False
    now = time.time()
    times = [*_restarts_last_hour(now), now]       # read before "w" empties the file
    try:
        os.makedirs(os.path.dirname(RESTART_FILE), exist_ok=True)
        with open(RESTART_FILE, "w") as f:
            f.write(" ".join(f"{t:.0f}" for t in times))
    except OSError:
        pass
    last_restart = time.monotonic()
    subprocess.run(["sudo", "-n", "systemctl", "restart", "--no-block", "spotify"],
                   capture_output=True, timeout=30)
    return True


def service_state():
    """spotify.service: 'active', 'activating' (waiting for internet), 'failed', ..."""
    try:
        return subprocess.run(["systemctl", "is-active", "spotify"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def service_age():
    """Seconds since spotify.service last became active (the player started), None if unknown."""
    try:
        out = subprocess.run(["systemctl", "show", "spotify", "-p", "ActiveEnterTimestampMonotonic", "--value"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return time.monotonic() - int(out) / 1e6 if out and out != "0" else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def resume_here(wait=60):
    """After a restart: play on this Pi again, once the player is back.
    The CLI exits 0 even when refused, so check. -> True if it's playing."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(2)
        try:
            play_here()
            run("playback", "play")
            time.sleep(1)
            now = get("playback") or {}
            if now.get("is_playing") and (now.get("device") or {}).get("name") == DEVICE:
                return True
        except (RuntimeError, ValueError):
            pass
    return False


def run(*args, timeout=15):
    """Run one spotify_player CLI command -> stdout. RuntimeError if it can't run."""
    if not player_up():
        raise RuntimeError("Spotify player isn't running (starting up?)")
    try:
        r = subprocess.run([SP, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("spotify_player is not installed")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Spotify didn't answer (no internet?)")
    if r.returncode:
        err = r.stderr.strip().splitlines()
        raise RuntimeError(err[-1] if err else f"spotify_player exited {r.returncode}")
    return r.stdout.strip()


def get(key, timeout=15):
    """get key playback / devices / user-playlists / queue / ... -> parsed JSON (None if empty)."""
    out = run("get", "key", key, timeout=timeout)
    return json.loads(out) if out else None


def play_here():
    """Make this Pi the active Spotify device (if it isn't already), carrying
    on whatever was playing elsewhere. Web API transfer: the CLI's `connect`
    fails (librespot "Invalid state", HTTP 500) when nothing is loaded yet,
    e.g. after the player restarts (2026-10-15)."""
    p = get("playback")
    if not p or (p.get("device") or {}).get("name") != DEVICE:
        web("PUT", "me/player", _token(), {"device_ids": [_device()], "play": True})
        time.sleep(1.0)


def start(uris=None, context=None, shuffle=False):
    """Start playing on this Pi straight away (Web API, device_id), whatever
    state the player is in. uris = track URIs in order, or context = a
    playlist/album/artist/collection URI."""
    dev = _device()
    token = _token()
    web("PUT", "me/player/play", token, {"uris": uris} if uris else {"context_uri": context}, device_id=dev)
    if shuffle:
        try:
            web("PUT", "me/player/shuffle", token, state="true", device_id=dev)
        except RuntimeError:
            pass
    else:
        _shuffle_off(token, dev)


PAGE = 50                                  # songs per request when listing a playlist


def _song(t):
    """A Web API track -> (uri, "title", "artists"), or None (local files, podcasts)."""
    if not t or t.get("type", "track") != "track" or not t.get("uri", "").startswith("spotify:track:"):
        return None
    return t["uri"], t.get("name", ""), ", ".join(a.get("name", "") for a in t.get("artists") or [])


def playlist_songs(pid, offset=0):
    """One page of a playlist (pid "liked" = Liked Songs) -> (total, [(uri, title, artists)]).
    Fetched only when the list screen needs them: one request per PAGE songs."""
    token = _token()
    if pid == "liked":
        r = web("GET", "me/tracks", token, limit=PAGE, offset=offset) or {}
    else:
        try:
            r = web("GET", f"playlists/{pid}/items", token, limit=PAGE, offset=offset) or {}
        except Blocked:
            raise
        except RuntimeError:                       # older name of the same call
            r = web("GET", f"playlists/{pid}/tracks", token, limit=PAGE, offset=offset) or {}
    songs = [_song(i.get("item") or i.get("track")) for i in r.get("items") or []]
    return r.get("total", 0), [s for s in songs if s]


_me = None


def play_song_in(pid, uri, then=()):
    """Start one song inside its playlist (pid "liked" = Liked Songs), so the rest of
    the playlist follows it; the shuffle setting is left as it is. then = the uris
    after it, played instead if Spotify won't start the playlist at that song."""
    global _me
    dev, token = _device(), _token()
    if pid == "liked":
        _me = _me or web("GET", "me", token)["id"]
        context = f"spotify:user:{_me}:collection"
    else:
        context = f"spotify:playlist:{pid}"
    try:
        web("PUT", "me/player/play", token, {"context_uri": context, "offset": {"uri": uri}}, device_id=dev)
    except Blocked:
        raise
    except RuntimeError:
        web("PUT", "me/player/play", token, {"uris": [uri, *then][:100]}, device_id=dev)


def start_liked():
    """Liked songs, shuffled."""
    token = _token()
    try:
        start(context=f"spotify:user:{web('GET', 'me', token)['id']}:collection", shuffle=True)
    except RuntimeError:                               # no collection context: a random 50 of them
        rows = (web("GET", "me/tracks", token, limit=50, offset=random.randrange(0, 200)) or {}).get("items") \
            or (web("GET", "me/tracks", token, limit=50) or {}).get("items") or []
        uris = [r["track"]["uri"] for r in rows if r.get("track")]
        if not uris:
            raise RuntimeError("no liked songs found")
        random.shuffle(uris)
        start(uris=uris)


def is_liked(track_id):
    """In Liked Songs? me/library: the older me/tracks/contains is 403 for this app (2026-10-16)."""
    return bool((web("GET", "me/library/contains", _token(), uris=f"spotify:track:{track_id}") or [False])[0])


def save_liked(track_id):
    """Add a track to Liked Songs (me/library, as spotify_player's own `like` does)."""
    web("PUT", "me/library", _token(), uris=f"spotify:track:{track_id}")


def start_item(kind, item_id="", name=""):
    """Track / playlist / album / artist by id, or by name (searched)."""
    if not item_id:
        hits = ((web("GET", "search", _token(), q=name, type=kind, limit=1) or {}).get(kind + "s") or {}).get("items")
        if not hits:
            raise RuntimeError(f"no {kind} called {name}")
        item_id = hits[0]["id"]
    uri = f"spotify:{kind}:{item_id}"
    start(uris=[uri]) if kind == "track" else start(context=uri)


def _token():
    """spotify_player's Web API access token (it refreshes the file itself)."""
    try:
        cid = re.search(r'^client_id\s*=\s*"([^"]+)"', open(CONFIG).read(), re.M).group(1)
        with open(os.path.join(CACHE, f"{cid}_token.json")) as f:
            return json.load(f)["access_token"]
    except (OSError, AttributeError, KeyError, ValueError):
        files = glob.glob(os.path.join(CACHE, "*_token.json"))
        if not files:
            raise RuntimeError("not logged in to Spotify")
        with open(max(files, key=os.path.getmtime)) as f:
            return json.load(f)["access_token"]


# Spotify's "429 Too Many Requests" comes with how long to stay away (2026-10-17: 17792 s,
# ~5 h, after SUGGEST's search bursts). Calling during it can make it longer, so no call
# goes out until it's over - in any process (the AI page's spotify tool too): the end time
# is kept in BLOCK_FILE.
BLOCK_FILE = os.path.expanduser("~/.cache/nightrider/spotify-blocked")


class Blocked(RuntimeError):
    """Spotify told us to stay away until .until (unix time)."""
    def __init__(self, until):
        self.until = until
        super().__init__(f"Spotify asks us to wait until {time.strftime('%H:%M', time.localtime(until))}")


def blocked_until():
    """Unix time Spotify's limit ends (0 = not limited)."""
    try:
        with open(BLOCK_FILE) as f:
            until = float(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0
    return until if until > time.time() else 0.0


def web(method, path, token, body=None, **params):
    """One Web API call -> parsed JSON (or None). Raises Blocked while Spotify's limit lasts."""
    until = blocked_until()
    if until:
        raise Blocked(until)
    url = "https://api.spotify.com/v1/" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        msg = e.read()[:200].decode(errors="replace")
        if e.code == 429:
            try:
                wait = max(1.0, float(e.headers.get("Retry-After") or 60))
            except ValueError:
                wait = 60.0
            until = time.time() + wait
            try:
                os.makedirs(os.path.dirname(BLOCK_FILE), exist_ok=True)
                with open(BLOCK_FILE, "w") as f:
                    f.write(f"{until:.0f}")
            except OSError:
                pass
            raise Blocked(until)
        raise RuntimeError(f"Spotify said {e.code}: {msg}")
    except OSError as e:
        raise RuntimeError(f"Spotify didn't answer ({e})")
    try:
        return json.loads(raw) if raw.strip() else None
    except ValueError:                                 # player commands may answer with plain text
        return None


def _device():
    devices = get("devices") or []                     # also gets spotify_player to refresh its token
    dev = next((d["id"] for d in devices if d.get("name") == DEVICE), None)
    if not dev:
        raise RuntimeError("the car's Spotify player isn't running (spotify.service)")
    return dev


def find_tracks(songs, token):
    """Track ids or "title - artist" texts -> [(id or None, name)], searched in parallel."""
    def find(song):
        song = str(song).strip()
        if re.fullmatch(r"[0-9A-Za-z]{22}", song):
            return song, song
        items = []
        if " - " in song:                              # "title - artist": exact fields first
            title, artist = song.rsplit(" - ", 1)
            q = f'track:"{title.strip()}" artist:"{artist.strip()}"'
            items = ((web("GET", "search", token, q=q, type="track", limit=1) or {}).get("tracks") or {}).get("items")
        if not items:
            q = song.replace(" - ", " ")
            items = ((web("GET", "search", token, q=q, type="track", limit=1) or {}).get("tracks") or {}).get("items")
        if not items:
            return None, song
        t = items[0]
        return t["id"], f"{t['name']} - {', '.join(a['name'] for a in t['artists'])}"

    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        return list(ex.map(find, [x for x in songs if str(x).strip()][:100]))


def play_songs(songs):
    """Play a list of songs in the car, in order. Each is a track id or
    "title - artist" text (looked up with a search). -> (played names, not found)."""
    dev = _device()
    token = _token()
    found = find_tracks(songs[:50], token)
    ids = list(dict.fromkeys(i for i, _ in found if i))      # no repeats, order kept
    if not ids:
        raise RuntimeError("none of those songs were found")
    web("PUT", "me/player/play", token, {"uris": [f"spotify:track:{i}" for i in ids]}, device_id=dev)
    _shuffle_off(token, dev)
    return [n for i, n in found if i], [n for i, n in found if not i]


def _shuffle_off(token, dev):
    try:                                               # in order; Spotify refuses this if it's already off
        web("PUT", "me/player/shuffle", token, state="false", device_id=dev)
    except RuntimeError:
        pass


def make_playlist(name, songs, play=True):
    """Save songs as a new private playlist in Ian's account (and play it).
    -> (playlist name, added names, not found)."""
    token = _token()
    found = find_tracks(songs, token)
    uris = [f"spotify:track:{i}" for i in dict.fromkeys(i for i, _ in found if i)]
    if not uris:
        raise RuntimeError("none of those songs were found")
    body = {"name": name, "public": False, "description": f"Made by Claude in the {config.CAR}"}
    try:
        pl = web("POST", "me/playlists", token, body)
    except RuntimeError:                               # older form of the same call
        pl = web("POST", f"users/{web('GET', 'me', token)['id']}/playlists", token, body)
    for n in range(0, len(uris), 100):
        try:
            web("POST", f"playlists/{pl['id']}/items", token, {"uris": uris[n:n + 100]})
        except RuntimeError:
            web("POST", f"playlists/{pl['id']}/tracks", token, {"uris": uris[n:n + 100]})
    if play:
        dev = _device()
        web("PUT", "me/player/play", token, {"context_uri": pl["uri"]}, device_id=dev)
        _shuffle_off(token, dev)
    return pl["name"], [n for i, n in found if i], [n for i, n in found if not i]


def fetch_taste():
    """Spotify's view of Ian's taste (for taste.py): top artists over three
    spans, recent top tracks, recently liked songs."""
    token = _token()
    track = lambda t: f"{t['name']} - {', '.join(a['name'] for a in t['artists'])}"      # noqa: E731
    top = lambda kind, span, n: (web("GET", f"me/top/{kind}", token, time_range=span, limit=n) or {}).get("items") or []   # noqa: E731
    return {"top_artists_recent": [a["name"] for a in top("artists", "short_term", 20)],
            "top_artists_year": [a["name"] for a in top("artists", "medium_term", 20)],
            "top_artists_all": [a["name"] for a in top("artists", "long_term", 20)],
            "top_tracks_recent": [track(t) for t in top("tracks", "short_term", 20)],
            "liked_recent": [track(i["track"]) for i in
                             (web("GET", "me/tracks", token, limit=30) or {}).get("items") or []]}


def summary(p):
    """Playback JSON -> one line for Claude."""
    if not p or not p.get("item"):
        return "Nothing is playing."
    it = p["item"]
    who = ", ".join(a["name"] for a in it.get("artists") or []) or (it.get("show") or {}).get("name", "")
    dev = p.get("device") or {}
    return (f"{'Playing' if p.get('is_playing') else 'Paused'}: {it.get('name')} - {who} "
            f"({(it.get('album') or {}).get('name', '')}) on {dev.get('name')} at volume "
            f"{dev.get('volume_percent')}%, {mmss(p.get('progress_ms'))} of {mmss(it.get('duration_ms'))}, "
            f"shuffle {'on' if p.get('shuffle_state') else 'off'}, repeat {p.get('repeat_state')}.")


def mmss(ms):
    if not isinstance(ms, (int, float)):
        return "--:--"
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


class Watchdog:
    """Restarts spotify.service when the player is running but useless. It
    never recovers by itself from these (journal, 2026-10-15/16, all when
    getting in the car):
    - started with no internet (Nightrider hotspot, or the phone's before it
      has data): the token refresh fails and it waits for a login forever;
    - lost the network (home Wi-Fi -> hotspot -> phone): one reconnect try,
      then never again. The CLI still answers, with no devices, no playback;
    - rodio panic (audio device changed under it): player thread dead.
    Healthy = "924S" is in the player's device list. Only acts when Spotify is
    reachable, since a restart can't help before that.
    Joining a new network (getting in the car: home Wi-Fi -> phone hotspot) is when
    the player dies, so then it checks at once and every few seconds for a while,
    restarts on the first sick check, and earlier backoff doesn't hold it up
    (2026-11-08: it used to take a minute or more to notice)."""
    GRACE = 45                     # s after a restart before judging it
    MAX_BACKOFF = 600
    STRIKES = 2                    # sick checks in a row before a restart
    LOGINS = 3                     # login waits with internet up = the login really expired
    NEW_NET_SECS = 90              # after joining a network: checks this long...
    NEW_NET_EVERY = 5              # ...this often
    WARMUP = 15                    # s a just-started player gets to show up (it takes ~3.5)

    def __init__(self, log=print):
        self.log = log
        self.checked = 0.0
        self.strikes = 0
        self.backoff = self.GRACE
        self.logins = 0
        self.status = ""           # for the screen: what's wrong, "" if fine
        self.net = None            # network_id() at the last check (None = not looked yet)
        self.net_at = -1e9         # monotonic time it last changed to a real network
        self.healthy_at = -1e9     # monotonic time of the last healthy check

    def playing_here(self):
        """A poll shows music playing on this Pi: whatever was wrong, it's fine now (don't
        leave "RECONNECTING..." up until the next check, up to 90 s after a restart)."""
        if self.status:
            self.log("spotify: player is healthy (playing)")
        self.strikes, self.logins, self.status = 0, 0, ""
        self.healthy_at = time.monotonic()

    def diagnose(self):
        """-> what's wrong, or "" if healthy, or None if it's still starting."""
        if waiting_for_login():
            return "waiting for a login"
        if not player_up():
            return None if service_state() == "activating" else "not running"
        try:
            devices = get("devices", timeout=10) or []
        except (RuntimeError, ValueError):
            devices = None
        if devices and any(d.get("name") == DEVICE for d in devices):
            return ""
        age = service_age()
        if age is not None and age < self.WARMUP:        # started just now (e.g. the network came up)
            return None
        return "not answering" if devices is None else "not connected to Spotify"

    def check(self, fine):
        """After each playback poll (fine = it showed something). -> True if it
        just restarted the player."""
        now = time.monotonic()
        if now < hold_until:                       # stopped for the voice: not a fault
            return False
        net = network_id()
        if net != self.net:
            if self.net is not None:
                self.log(f"spotify: network changed ({self.net or 'none'} -> {net or 'none'})")
            if net and self.net is not None:
                self.net_at, self.checked, self.strikes, self.backoff = now, 0.0, 0, self.GRACE
            self.net = net
        new_net = now - self.net_at < self.NEW_NET_SECS
        # a restart from before the move was on the old network: no need to wait for it
        settling = now - last_restart < self.backoff and not (new_net and last_restart < self.net_at)
        every = self.NEW_NET_EVERY if new_net else 10 if self.strikes else 20 if not fine else 60
        if settling or now - self.checked < every:
            return False
        self.checked = now
        sick = self.diagnose()
        if sick is None:
            self.status = "STARTING (WAITING FOR INTERNET?)"
            return False
        if not sick:
            if self.strikes or self.status:
                self.log("spotify: player is healthy")
            self.strikes, self.backoff, self.logins, self.status = 0, self.GRACE, 0, ""
            self.healthy_at = now
            return False
        if not online():
            self.strikes, self.status = 0, "NO INTERNET"
            return False
        self.strikes += 1
        if self.strikes < (1 if new_net else self.STRIKES) and sick != "waiting for a login":
            return False
        if sick == "waiting for a login":
            self.logins += 1
            if self.logins > self.LOGINS:
                self.status = "NEEDS A NEW LOGIN (SEE CLAUDE.MD)"
                return False
        self.strikes = 0
        if not restart(f"player {sick}", self.log):
            self.status = "PLAYER STUCK: WAITING TO RESTART"
            self.checked = now + 60                # don't ask again every few seconds
            return False
        self.log(f"spotify: player {sick} with internet up: restarted it")
        self.status = "RECONNECTING..."
        self.backoff = min(self.backoff * 2, self.MAX_BACKOFF)
        return True


EXPECT_SECS = 3                    # how long a picked song is shown before Spotify reports it
WANT_SECS = 180                    # a play tapped before the player was ready is kept this long...
WANT_EVERY = 5                     # ...and tried again this often (one device check each time)
NOT_READY = ("isn't running", "didn't answer", "not logged in")     # errors a later try can get past


def play_now():
    """Play on this Pi: carry on what's loaded, or liked songs if nothing is."""
    p = get("playback")
    if not p or not p.get("item"):
        start_liked()
        return
    play_here()
    run("playback", "play")


class _Stale(Exception):
    """A poll that still shows the song from before a pick."""


class Player:
    """What the Spotify page shows, kept fresh by a background poll (every 2 s
    while the page is showing, 15 s otherwise). Buttons run in a worker thread
    so a slow network never stalls the screen. With watch=True (the display)
    a Watchdog restarts the player when it's stuck."""

    def __init__(self, wake=None, log=print, watch=False):
        self.wake = wake
        self.log = log
        self.watchdog = Watchdog(log) if watch else None
        self.played_at = -1e9          # monotonic time a poll last saw it playing here
        self.state = None              # last `get key playback`
        self.at = 0.0                  # monotonic time of that poll
        self.error = ""
        self.doing = ""                # e.g. "STARTING LIKED SONGS..."
        self.visible = False
        self.polled = False            # at least one answer (or error) so far
        self.liked = {}                # track id -> in Liked Songs (True/False); missing = not known yet
        self._like_tried = {}          # track id -> monotonic time of the last failed lookup
        self._flushed_at = -1e9        # last try at saving hearts that waited out a block
        self._expect = (None, 0)       # (track id, until): a picked song shown before Spotify reports it
        self._wanted = None            # (monotonic, label, fn): a play tapped while the player wasn't ready
        self._want_tried = -1e9
        self._kick = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._poll, daemon=True).start()

    def _changed(self):
        if self.wake:
            self.wake.set()

    def _poll(self):
        while True:
            fine = False
            try:
                before = self.state and dict(self.state, progress_ms=self.progress)
                polled = get("playback", timeout=10)
                tid, until = self._expect
                if tid and time.monotonic() < until and ((polled or {}).get("item") or {}).get("id") != tid:
                    raise _Stale()                       # still the old song: keep showing the picked one
                self._expect = (None, 0)
                self.state, self.at, self.error = polled, time.monotonic(), ""
                fine = bool(self.state and self.state.get("item"))
                self._song_changed(before)
                if self.playing and self.device == DEVICE:
                    self.played_at = self.at
                    if self.watchdog:
                        self.watchdog.playing_here()
                self._play_wanted()
            except _Stale:
                pass
            except (RuntimeError, ValueError) as e:
                self.error = str(e)
            self.polled = True
            if self.watchdog:
                self._watch(fine)
                if time.monotonic() - self._flushed_at > 60 and not blocked_until() and pending_likes():
                    self._flushed_at = time.monotonic()
                    flush_pending_likes(self.log)
            self._changed()
            self._kick.wait(2 if self.visible else 15)
            self._kick.clear()

    def _song_changed(self, before):
        """After each poll: a song that ended note for taste.py, and look up whether the new one is liked."""
        old, new = (before or {}).get("item") or {}, self.item or {}
        if old.get("id") and old.get("id") != new.get("id") and old.get("type", "track") == "track":
            taste.record_play(old, before.get("progress_ms") or 0)
        tid = new.get("id") if new.get("type", "track") == "track" else None
        if tid and tid not in self.liked and time.monotonic() - self._like_tried.get(tid, -1e9) > 60:
            try:
                self.liked[tid] = is_liked(tid)
            except RuntimeError:                         # offline: try again in a minute
                self._like_tried[tid] = time.monotonic()

    def _play_wanted(self):
        """A play tapped while the player was starting or offline: start it once 924S is
        up (getting in the car: tap play, music comes as soon as it can)."""
        w = self._wanted
        if not w or self._lock.locked():
            return
        now = time.monotonic()
        if now - w[0] > WANT_SECS:
            self._wanted = None
            return
        if now - self._want_tried < WANT_EVERY:
            return
        self._want_tried = now
        try:
            if not any(d.get("name") == DEVICE for d in get("devices", timeout=10) or []):
                return
        except (RuntimeError, ValueError):
            return
        self.log("spotify: player ready: playing what was asked for")
        self._do(w[1], w[2], play=True, retry=True)

    def _watch(self, fine):
        try:
            was_playing = time.monotonic() - self.played_at < 600
            if self.watchdog.check(fine) and was_playing:        # music stopped by the fault: bring it back
                threading.Thread(target=self._resume, daemon=True).start()
        except Exception as e:                                   # never let the poll thread die
            self.log(f"spotify: watchdog error: {e!r}")

    def _resume(self):
        if resume_here(wait=180):
            self.log("spotify: playing again after the restart")
        self.refresh()

    @property
    def problem(self):
        """What's wrong with the player for the screen, '' if nothing."""
        until = blocked_until()
        if until:                                       # music plays on; only the Web API waits
            n = len(pending_likes())
            return f"SPOTIFY BLOCKED TILL {time.strftime('%H:%M', time.localtime(until))}" + \
                (f" +{n} HEART{'S' if n > 1 else ''}" if n else "")
        return (self.watchdog.status if self.watchdog else "") or self.error

    def refresh(self):
        self._kick.set()

    # ---- what's playing ----

    @property
    def item(self):
        return (self.state or {}).get("item") or None

    @property
    def playing(self):
        return bool(self.state and self.state.get("is_playing"))

    @property
    def title(self):
        return (self.item or {}).get("name", "")

    @property
    def artist(self):
        it = self.item or {}
        return ", ".join(a["name"] for a in it.get("artists") or []) or (it.get("show") or {}).get("name", "")

    @property
    def album(self):
        return ((self.item or {}).get("album") or {}).get("name", "")

    @property
    def device(self):
        return ((self.state or {}).get("device") or {}).get("name", "")

    @property
    def volume(self):
        return ((self.state or {}).get("device") or {}).get("volume_percent")

    @property
    def shuffle(self):
        return bool(self.state and self.state.get("shuffle_state"))

    @property
    def heart(self):
        """The playing song is in Liked Songs: True / False / None (not known yet)."""
        return self.liked.get((self.item or {}).get("id"))

    HEART_SHOWN_SECS = 3               # a just-tapped heart stays up filled this long, then goes

    @property
    def show_heart(self):
        """The heart is only for songs NOT in Liked Songs (Ian 2026-11-15: "if it's on liked
        songs, it's hearted"): hidden when playing from Liked Songs or when the song is liked,
        except for a moment after it was tapped so the fill is seen."""
        it = self.item or {}
        if it.get("type", "track") != "track":
            return False
        if time.monotonic() - getattr(self, "_hearted_at", -1e9) < self.HEART_SHOWN_SECS:
            return True
        context = ((self.state or {}).get("context") or {}).get("uri", "")
        return not context.endswith(":collection") and self.heart is not True

    @property
    def duration(self):
        return (self.item or {}).get("duration_ms") or 0

    @property
    def progress(self):
        """ms, moving on smoothly between polls while playing."""
        if not self.state:
            return 0
        ms = self.state.get("progress_ms") or 0
        if self.playing:
            ms += (time.monotonic() - self.at) * 1000
        return min(ms, self.duration) if self.duration else ms

    # ---- buttons ----

    def _do(self, label, fn, play=False, retry=False):
        """Run a button's command in a thread. play = it starts music: if the player isn't
        ready yet, it's kept and done once it is (_play_wanted)."""
        if not self._lock.acquire(blocking=False):      # one command at a time
            return
        if not retry:
            self._wanted = None                         # a new tap replaces an older wish
        self.doing = label
        self._changed()

        def work():
            try:
                fn()
                self.error = ""
                if play:
                    self._wanted = None
            except RuntimeError as e:
                self.error = str(e)
                if play and not retry and not isinstance(e, Blocked) and any(m in str(e) for m in NOT_READY):
                    self._wanted = (time.monotonic(), label, fn)
                    self._want_tried = time.monotonic()
            finally:
                self.doing = ""
                self._lock.release()
                self.refresh()                          # show the result at once...
                time.sleep(0.4)
                self.refresh()                          # ...and again once Spotify has caught up
        threading.Thread(target=work, daemon=True).start()

    def toggle(self):
        if not self.item:                               # nothing to resume: put on liked songs
            self.play_liked()
            return
        to_play = not self.playing
        if self.state:                                  # show it at once; the poll confirms
            self.state["progress_ms"] = self.progress
            self.state["is_playing"] = to_play
            self.at = time.monotonic()
        if to_play and (not player_up() or (self.watchdog and self.watchdog.status)):   # starting / offline:
            self._do("", play_now, play=True)          # (and plays once ready if it can't yet)
        else:
            self._do("", lambda: run("playback", "play-pause"), play=to_play)

    def next(self):
        if self.state:                                  # an early NEXT is a skip: note it for Claude
            taste.record_skip(dict(self.state, progress_ms=self.progress), "screen")
        self._do("NEXT...", lambda: run("playback", "next"))

    def previous(self):
        self._do("BACK...", lambda: run("playback", "previous"))

    def toggle_shuffle(self):
        if self.state:
            self.state["shuffle_state"] = not self.shuffle
        self._do("", lambda: run("playback", "shuffle"))

    def like(self):
        """The heart: save the playing song to Liked Songs (filled at once; un-filled if it fails).
        Already liked: nothing, so a stray tap while driving can't unlike anything."""
        item = self.item
        if not item or not item.get("id") or item.get("type", "track") != "track" or self.heart \
                or self._lock.locked():                  # another button's command is still going
            return
        tid = item["id"]
        self.liked[tid] = True
        self._hearted_at = time.monotonic()
        self._changed()

        def save():
            try:
                save_liked(tid)
            except Blocked:                             # stays filled: saved once Spotify lets us
                rows = [r for r in pending_likes() if r["id"] != tid]
                _save_pending(rows + [{"id": tid, "name": item.get("name", "")}])
            except RuntimeError:
                self.liked.pop(tid, None)
                raise
            taste.record_like(item, "screen")
        self._do("", save)

    def play_liked(self):
        self._do("STARTING LIKED SONGS...", start_liked, play=True)

    def play_playlist(self, pid, name="", shuffle=False):
        if pid == "liked":
            self.play_liked()
            return
        self._do(f"STARTING {name.upper()[:24]}...",
                 lambda: start(context=f"spotify:playlist:{pid}", shuffle=shuffle), play=True)

    def play_song(self, pid, uri, title="", then=(), artists=""):
        """A song picked from a playlist's list: it plays, the playlist carries on after it.
        The MUSIC page shows it at once (from the list's title and artists) rather than
        after the command and the next poll, ~1.5 s later; the poll fills in the rest."""
        tid = uri.rsplit(":", 1)[-1]
        self.state = dict(self.state or {}, progress_ms=0, is_playing=True,
                          item={"name": title, "id": tid, "type": "track", "duration_ms": 0,
                                "artists": [{"name": artists}] if artists else []})
        self.at = time.monotonic()
        self._expect = (tid, time.monotonic() + EXPECT_SECS)
        self._changed()
        self._do("", lambda: play_song_in(pid, uri, then), play=True)


# ---- hearts that have to wait (Spotify limiting us) ----

PENDING_LIKES = os.path.expanduser("~/.config/nightrider/spotify-pending-likes.json")


def pending_likes():
    try:
        with open(PENDING_LIKES) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_pending(rows):
    try:
        os.makedirs(os.path.dirname(PENDING_LIKES), exist_ok=True)
        with open(PENDING_LIKES + ".tmp", "w") as f:
            json.dump(rows, f)
        os.replace(PENDING_LIKES + ".tmp", PENDING_LIKES)
    except OSError:
        pass


def flush_pending_likes(log=print):
    """Save the hearts that waited out a block. -> how many are still waiting."""
    rows = pending_likes()
    while rows and not blocked_until():
        try:
            save_liked(rows[0]["id"])
        except Blocked:
            break
        except RuntimeError as e:                 # this one won't go: drop it rather than loop
            log(f"spotify: couldn't save heart for {rows[0].get('name')}: {e}")
        else:
            log(f"spotify: saved waiting heart: {rows[0].get('name')}")
        rows = rows[1:]
        _save_pending(rows)
    return len(rows)


# ---- Claude's spotify tool (approve_mcp.py) ----

def _items(kind, rows, n=8):
    out = []
    for r in (rows or [])[:n]:
        who = ", ".join(a["name"] for a in r.get("artists") or []) if r.get("artists") else ""
        out.append(f"- {r.get('name')}{' - ' + who if who else ''} (id {r.get('id')})")
    return f"{kind}:\n" + ("\n".join(out) if out else "- none")


def command(action, query="", item_type="", item_id="", songs=None, name=""):
    """One Spotify action for Claude -> text reply. Starting something always
    plays it on the car (this Pi)."""
    a = (action or "").strip().lower()
    if a == "status":
        return summary(get("playback"))
    if a in ("play", "resume"):
        p = get("playback")
        if not p or not p.get("item"):
            start_liked()
            time.sleep(1.0)
            return "Nothing was loaded, so started liked songs on shuffle. " + summary(get("playback"))
        play_here()
        run("playback", "play")
    elif a == "pause":
        run("playback", "pause")
    elif a in ("next", "skip"):
        taste.record_skip(get("playback"), "claude")
        run("playback", "next")
    elif a in ("previous", "back"):
        run("playback", "previous")
    elif a == "shuffle":
        run("playback", "shuffle")
    elif a == "repeat":
        run("playback", "repeat")
    elif a == "like":
        item = (get("playback") or {}).get("item") or {}
        if not item.get("id"):
            return "Nothing is playing to like."
        save_liked(item["id"])
        taste.record_like(item, "claude")
        return f"Saved {item.get('name')} to Liked Songs."
    elif a == "search":
        if not query:
            return "Error: search needs query."
        d = json.loads(run("search", query, timeout=20) or "{}")
        return "\n\n".join([_items("Tracks", d.get("tracks")), _items("Artists", d.get("artists"), 5),
                            _items("Albums", d.get("albums"), 5), _items("Playlists", d.get("playlists"), 5)])
    elif a == "playlists":
        return _items("Ian's playlists", get("user-playlists"), 60)
    elif a == "queue":                                 # straight from Spotify: the CLI's copy goes stale
        q = web("GET", "me/player/queue", _token()) or {}
        return _items("Up next", q.get("queue"), 10)
    elif a == "liked":
        start_liked()
    elif a == "play_songs":
        if not songs:
            return "Error: play_songs needs songs (a list)."
        played, missing = play_songs(songs)
        time.sleep(1.0)
        return (f"Playing {len(played)} songs, starting with {played[0]}."
                + (f" Not found: {'; '.join(missing)}." if missing else "") + " " + summary(get("playback")))
    elif a == "make_playlist":
        if not songs:
            return "Error: make_playlist needs songs (a list) and name."
        made, added, missing = make_playlist(name or query or "Made by Claude", songs)
        time.sleep(1.0)
        return (f"Saved playlist '{made}' with {len(added)} songs to Ian's Spotify and started it."
                + (f" Not found: {'; '.join(missing)}." if missing else "") + " " + summary(get("playback")))
    elif a == "start":
        t = (item_type or "").lower()
        if t == "radio":
            return ("Error: Spotify Radio isn't available with this app key. Make the radio yourself: "
                    "use play_songs with the seed song first, then 20-30 similar songs.")
        if t not in ("track", "playlist", "album", "artist"):
            return "Error: start needs item_type track, playlist, album or artist."
        if not (item_id or query):
            return "Error: start needs item_id (from search/playlists) or query (a name)."
        start_item(t, item_id, query)
    else:
        return ("Error: unknown action. Use status, play, pause, next, previous, shuffle, "
                "repeat, like, search, playlists, queue, liked, start, play_songs, make_playlist.")
    time.sleep(1.0)
    return "Done. " + summary(get("playback"))
