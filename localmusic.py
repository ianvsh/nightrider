"""Music from a USB stick: a second way to play music, next to Spotify (MUSIC > LIBRARY >
LOCALLY SAVED MUSIC).

- The stick is mounted read-only at /media/usb-<uuid> by system/90-nightrider-usb.rules
  (read-only: a car's power can go at any moment).
- Library: an index of the MP3s (artist, album, track, title, length) in MUSIC_DB, built
  by `python localmusic.py scan <dir>` in a low-priority process (100 GB of MP3s takes some
  minutes the first time; after that only new or changed files are read). Browsing works
  from memory, so lists open at once.
- LocalPlayer: one long-lived mpg123 in remote mode, playing to its own Loopback channel
  ("localbus" in /etc/asound.conf), which the audio bridge (bt.Bridge) mixes with Spotify
  and the voice. A new song is one line to mpg123: no process starts, nothing waits.
  Its shuffle is its own: Spotify's shuffle setting doesn't touch it, and vice versa.
- Music: which of the two the MUSIC page, the stereo's buttons and the voice control.
  Starting one pauses the other.
"""
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata

MEDIA = "/media"
MOUNT_PREFIX = "usb-"                       # the udev rule's mount points: /media/usb-<uuid>
MUSIC_DB = "/var/lib/nightrider/music.db"
STATE = os.path.expanduser("~/.config/nightrider/local-music.json")
OUT = "localbus"                            # ALSA PCM: Loopback 0,3 (bridge reads 1,3)
EXTS = (".mp3",)
SKIP_DIRS = {"system volume information", "$recycle.bin", ".trashes", ".spotlight-v100", ".fseventsd"}
BACK_RESTARTS = 3.0                         # BACK after this many seconds = start of the song again


# ---------------------------------------------------------------- where the music is

def mounted(media=MEDIA, mounts="/proc/mounts"):
    """USB sticks mounted now (their mount points), from the kernel's mount table."""
    out = []
    try:
        with open(mounts) as f:
            for line in f:
                parts = line.split()
                if len(parts) > 1:
                    mp = parts[1].replace("\\040", " ")
                    if os.path.dirname(mp) == media and os.path.basename(mp).startswith(MOUNT_PREFIX):
                        out.append(mp)
    except OSError:
        pass
    return sorted(out)


def sort_key(name):
    """'The Beatles' -> 'beatles', so it files under B, like a record shop; 'Ángela' under A."""
    s = "".join(c for c in unicodedata.normalize("NFKD", name.casefold().strip()) if not unicodedata.combining(c))
    for article in ("the ", "a "):
        if s.startswith(article) and len(s) > len(article):
            s = s[len(article):]
            break
    s = re.sub(r"^[^0-9a-z]+", "", s) or s
    return s


def letter(name):
    """A-Z, or '#' for numbers and anything else."""
    k = sort_key(name)[:1].upper()
    return k if "A" <= k <= "Z" else "#"


def _num(v):
    """'3/12' -> 3, junk -> 0."""
    m = re.match(r"\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else 0


def from_path(path, root):
    """Artist / album / title guessed from the folders and file name, for files without tags:
    <root>/Artist/Album/03 - Title.mp3."""
    rel = os.path.relpath(path, root).split(os.sep)
    base = os.path.splitext(rel[-1])[0]
    m = re.match(r"^\s*(\d{1,3})[\s.\-_]+(.+)$", base)
    track, title = (int(m.group(1)), m.group(2)) if m else (0, base)
    artist = rel[-3] if len(rel) >= 3 else (rel[-2] if len(rel) == 2 else "")
    album = rel[-2] if len(rel) >= 3 else ""
    if " - " in title and not artist:                 # "Artist - Title.mp3" loose in the root
        artist, title = title.split(" - ", 1)
    return {"artist": artist.strip(), "album": album.strip(), "title": title.strip(), "track": track}


def read_tags(path, root):
    """-> dict for one file: tags where there are some, the folders where there aren't."""
    guess = from_path(path, root)
    row = dict(guess, year=0, disc=0, secs=0.0)
    try:
        from mutagen.mp3 import MP3, HeaderNotFoundError  # noqa: F401
        from mutagen.easyid3 import EasyID3
        f = MP3(path, ID3=EasyID3)
        row["secs"] = float(getattr(f.info, "length", 0) or 0)
        t = f.tags or {}

        def tag(k):
            v = t.get(k)
            return (v[0] if v else "").strip()
        row["artist"] = tag("albumartist") or tag("artist") or guess["artist"]
        row["album"] = tag("album") or guess["album"]
        row["title"] = tag("title") or guess["title"]
        row["track"] = _num(tag("tracknumber")) or guess["track"]
        row["disc"] = _num(tag("discnumber"))
        row["year"] = _num(tag("date") or tag("originaldate"))
    except Exception:                                  # unreadable tags: the folders will do
        pass
    if not row["artist"]:
        row["artist"] = "Unknown artist"
    return row


# ---------------------------------------------------------------- the index

SCHEMA = """CREATE TABLE IF NOT EXISTS songs (
    path TEXT PRIMARY KEY, root TEXT, mtime INTEGER, size INTEGER,
    artist TEXT, album TEXT, year INTEGER, disc INTEGER, track INTEGER, title TEXT, secs REAL)"""


def connect(db=MUSIC_DB):
    os.makedirs(os.path.dirname(db), exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(SCHEMA)
    return con


def music_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.casefold() not in SKIP_DIRS]
        for name in files:
            if name.lower().endswith(EXTS) and not name.startswith("._"):     # ._x = a Mac's sidecar file
                yield os.path.join(dirpath, name)


def scan(root, db=MUSIC_DB, log=print, batch=300):
    """Bring the index up to date with one stick: read new/changed files, drop gone ones.
    -> (songs on the stick, files read)."""
    root = os.path.abspath(root)
    con = connect(db)
    known = {p: (m, s) for p, m, s in con.execute("SELECT path, mtime, size FROM songs WHERE root = ?", (root,))}
    seen, todo, read = set(), [], 0
    for path in music_files(root):
        seen.add(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if known.get(path) == (int(st.st_mtime), st.st_size):
            continue
        r = read_tags(path, root)
        todo.append((path, root, int(st.st_mtime), st.st_size, r["artist"], r["album"], r["year"],
                     r["disc"], r["track"], r["title"], r["secs"]))
        read += 1
        if len(todo) >= batch:
            con.executemany("INSERT OR REPLACE INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?)", todo)
            con.commit()
            todo = []
    if todo:
        con.executemany("INSERT OR REPLACE INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?)", todo)
    gone = [(p,) for p in known if p not in seen]
    if seen or not os.listdir(root):      # (a stick that read as empty by mistake keeps its index)
        con.executemany("DELETE FROM songs WHERE path = ?", gone)
    con.commit()
    con.close()
    log(f"music: {root}: {len(seen)} songs, {read} read, {len(gone)} gone")
    return len(seen), read


class Library:
    """The songs on the sticks plugged in now, in memory, ready to browse:
    artists A-Z, each artist's songs album by album. Rebuilt (in a thread) when a stick
    comes or goes and as an index scan goes along; the screens always see a complete one."""
    CHECK_EVERY = 5                    # s between looks for sticks coming and going
    RELOAD_WHILE_SCANNING = 20         # s: show what's indexed so far

    def __init__(self, db=MUSIC_DB, log=print, wake=None, mounts=mounted, watch=True):
        self.db, self.log, self.wake, self.mounts = db, log, wake, mounts
        self.roots = []
        self.artists = []              # [(name, letter, n songs)] A-Z
        self.songs = {}                # artist -> [song dict] album by album, track by track
        self.by_path = {}              # path -> song dict
        self.total = 0
        self.loaded = False            # first load done
        self.scanning = None           # root being indexed, or None
        self._scanned = set()          # roots indexed since they were plugged in
        self._lock = threading.Lock()
        if watch:
            threading.Thread(target=self._watch, daemon=True).start()

    # -- the songs, from the index --

    def load(self):
        roots = self.mounts()
        rows = []
        if roots and os.path.exists(self.db):
            try:
                con = sqlite3.connect(self.db, timeout=10)
                q = ",".join("?" * len(roots))
                rows = con.execute(f"SELECT path, root, artist, album, year, disc, track, title, secs "
                                   f"FROM songs WHERE root IN ({q})", roots).fetchall()
                con.close()
            except sqlite3.Error as e:
                self.log(f"music: index unreadable: {e}")
        self.build(rows, roots)

    def build(self, rows, roots=()):
        songs, by_path = {}, {}
        for path, root, artist, album, year, disc, track, title, secs in rows:
            s = {"path": path, "artist": artist, "album": album or "", "year": year or 0, "disc": disc or 0,
                 "track": track or 0, "title": title or os.path.basename(path), "secs": secs or 0.0}
            songs.setdefault(artist, []).append(s)
            by_path[path] = s
        for artist, lst in songs.items():
            first_year = {}
            for s in lst:
                a = s["album"].casefold()
                if s["year"] and (a not in first_year or s["year"] < first_year[a]):
                    first_year[a] = s["year"]
            # albums oldest first (no year: after), then disc, track, title
            lst.sort(key=lambda s: (first_year.get(s["album"].casefold(), 9999), s["album"].casefold(),
                                    s["disc"], s["track"] or 999, s["title"].casefold()))
        names = sorted(songs, key=lambda a: (letter(a) != "#", sort_key(a), a))
        names = [n for n in names if letter(n) != "#"] + [n for n in names if letter(n) == "#"]  # A-Z, then #
        artists = [(n, letter(n), len(songs[n])) for n in names]
        with self._lock:
            self.songs, self.by_path, self.artists = songs, by_path, artists
            self.total, self.roots, self.loaded = len(by_path), list(roots), True
        if self.wake:
            self.wake.set()

    def albums(self, artist):
        """-> [(album, year, [songs])] in order, for the artist's screen."""
        out = []
        for s in self.songs.get(artist, []):
            if not out or out[-1][0] != s["album"]:
                out.append((s["album"], s["year"], []))
            out[-1][2].append(s)
        return out

    def first_of(self, ch):
        """Index in artists of the first artist under a letter (None if there's none)."""
        return next((i for i, a in enumerate(self.artists) if a[1] == ch), None)

    def all_songs(self):
        return [s for a, _, _ in self.artists for s in self.songs[a]]

    # -- sticks coming and going --

    def _watch(self):
        last = None
        while True:
            try:
                roots = self.mounts()
                if roots != last:
                    if last is not None:
                        self.log(f"music: USB sticks now: {', '.join(roots) or 'none'}")
                    self._scanned &= set(roots)
                    last = roots
                    self.load()
                todo = [r for r in roots if r not in self._scanned]
                if todo and not self.scanning:
                    self._scan(todo[0])
            except Exception as e:                     # never let the watcher die
                self.log(f"music: watcher error: {e!r}")
            time.sleep(self.CHECK_EVERY)

    def _scan(self, root):
        """Index one stick in a low-priority process; reload as it goes and when done."""
        self.scanning = root
        if self.wake:
            self.wake.set()
        cmd = ["nice", "-n", "15", "ionice", "-c", "3", sys.executable, os.path.abspath(__file__), "scan", root, self.db]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError as e:
            self.log(f"music: couldn't index {root}: {e}")
            self._scanned.add(root)
            self.scanning = None
            return
        last = time.monotonic()
        while proc.poll() is None:
            time.sleep(1)
            if time.monotonic() - last > self.RELOAD_WHILE_SCANNING:
                last = time.monotonic()
                self.load()
        out = (proc.stdout.read() or "").strip()
        if out:
            self.log(out.splitlines()[-1][:300])
        self._scanned.add(root)
        self.scanning = None
        self.load()


# ---------------------------------------------------------------- playing

def _load_state(path=STATE):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state, path=STATE):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(state, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


class LocalPlayer:
    """Plays songs from the Library through mpg123, with its own queue and shuffle.
    Looks like spotify.Player to the MUSIC page (title, artist, progress, toggle, next...).
    Everything a tap does is one line written to mpg123: instant."""
    show_heart = False
    heart = None
    device = ""
    polled = True
    doing = ""
    visible = False

    def __init__(self, library, wake=None, log=print, out=OUT, state_path=STATE, cmd=None):
        self.library, self.wake, self.log, self.out = library, wake, log, out
        self.state_path = state_path
        self.cmd = cmd or ["mpg123", "-R", "-o", "alsa", "-a", out]
        st = _load_state(state_path)
        self.shuffle = bool(st.get("shuffle"))
        self.context = st.get("context")       # ["all"] / ["artist", name] / ["album", artist, album]
        self._resume_path = st.get("path")     # last song, until the library has it again
        self._resume_at = float(st.get("at") or 0)
        self.queue = []                        # songs in context order
        self.order = []                        # play order: indexes into queue
        self.pos = 0                           # where in order
        self.song = None                       # song dict playing / paused
        self.playing = False
        self.loaded = False                    # mpg123 has the song (paused = resumable in place)
        self._start = 0.0                      # monotonic time progress was _offset
        self._offset = 0.0                     # s into the song at _start
        self.error = ""
        self.song_seq = 0
        self._proc = None
        self._lock = threading.Lock()
        self._gen = 0                          # bumped per load: a stale "song ended" is ignored

    # -- what the MUSIC page shows --

    @property
    def item(self):
        self._restore()
        return self.song

    @property
    def title(self):
        return (self.song or {}).get("title", "")

    @property
    def artist(self):
        return (self.song or {}).get("artist", "")

    @property
    def album(self):
        return (self.song or {}).get("album", "")

    @property
    def duration(self):
        return int(((self.song or {}).get("secs") or 0) * 1000)

    @property
    def progress(self):
        """ms into the song."""
        s = self._offset + (time.monotonic() - self._start if self.playing else 0)
        ms = int(s * 1000)
        return min(ms, self.duration) if self.duration else ms

    @property
    def problem(self):
        return self.error

    def refresh(self):
        pass

    # -- mpg123 --

    def _send(self, line):
        """One command to mpg123 (started on first use; restarted if it died)."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                try:
                    self._proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
                except OSError as e:
                    self.error = f"CAN'T PLAY: {e.strerror or e}".upper()[:39]
                    return False
                threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()
                self._proc.stdin.write("SILENCE\n")        # no per-frame progress lines
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
                return True
            except (OSError, ValueError):
                self._proc = None
                return False

    def _read(self, proc):
        for line in proc.stdout:
            if line.startswith("@P 3"):                    # the song played to its end
                gen = self._gen
                threading.Thread(target=self._ended, args=(gen,), daemon=True).start()
            elif line.startswith("@E"):
                msg = line[2:].strip()
                self.log(f"music: mpg123: {msg}")
                if self.song and not os.path.exists(self.song["path"]):
                    self.error = "USB STICK NOT FOUND"
                    self.playing, self.loaded = False, False
                    self._changed()

    def _ended(self, gen):
        if gen == self._gen and self.playing:
            self._advance(1, natural=True)

    def _changed(self):
        if self.wake:
            self.wake.set()

    def _load(self, song, at=0.0, play=True):
        """Put a song on (at = s into it)."""
        if self.song is None or song["path"] != self.song["path"]:
            self.song_seq += 1
        self.song = song
        self.error = ""
        self._gen += 1
        self._offset, self._start = at, time.monotonic()
        if not os.path.exists(song["path"]):
            self.error = "USB STICK NOT FOUND"
            self.playing = self.loaded = False
            self._changed()
            return
        ok = self._send(f"L {song['path']}")
        if ok and at > 1:
            self._send(f"J {int(at)}s")
        if ok and not play:
            self._send("P")
        self.playing, self.loaded = bool(ok and play), bool(ok)
        self._changed()
        self._save()

    def _save(self):
        st = _load_state(self.state_path)          # (keeps Music's "source")
        st.update({"shuffle": self.shuffle, "context": self.context,
                   "path": (self.song or {}).get("path"), "at": round(self.progress / 1000, 1)})
        _save_state(st, self.state_path)

    # -- the queue --

    def _context_songs(self, context):
        lib = self.library
        if not context:
            return []
        if context[0] == "all":
            return lib.all_songs()
        if context[0] == "artist":
            return list(lib.songs.get(context[1], []))
        if context[0] == "album":
            return [s for s in lib.songs.get(context[1], []) if s["album"] == context[2]]
        return []

    def _make_order(self, first):
        n = len(self.queue)
        if self.shuffle:
            rest = [i for i in range(n) if i != first]
            random.shuffle(rest)
            self.order, self.pos = [first] + rest, 0
        else:
            self.order, self.pos = list(range(n)), first

    def play(self, context, first=None):
        """Play from a context (["all"], ["artist", a], ["album", a, album]); first = index in it
        of the song to start with (None = the start, or a random one when shuffling)."""
        songs = self._context_songs(context)
        if not songs:
            self.error = "NO SONGS FOUND"
            self._changed()
            return
        self.context, self.queue = list(context), songs
        if first is None:
            first = random.randrange(len(songs)) if self.shuffle else 0
        self._make_order(max(0, min(first, len(songs) - 1)))
        self._load(self.queue[self.order[self.pos]])

    def play_song(self, context, song):
        songs = self._context_songs(context)
        i = next((n for n, s in enumerate(songs) if s["path"] == song["path"]), 0)
        self.play(context, i)

    def _advance(self, step, natural=False):
        if not self.queue:
            self._restore()
            if not self.queue:
                return
        p = self.pos + step
        if p >= len(self.order):
            if self.shuffle:                           # shuffling: deal again, carry on
                self._make_order(random.randrange(len(self.queue)))
                p = 0
            elif natural:                              # end of the album / artist: stop there
                self.playing = self.loaded = False
                self._offset, self._start = 0.0, time.monotonic()
                self._changed()
                self._save()
                return
            else:
                p = 0
        self.pos = max(0, p)
        self._load(self.queue[self.order[self.pos]])

    def _restore(self):
        """After a restart: the last song back on screen (paused), once the library has it."""
        if self._resume_path and self.library.loaded:
            path, at = self._resume_path, self._resume_at
            self._resume_path = None
            song = self.library.by_path.get(path)
            if not song:
                return
            self.queue = self._context_songs(self.context) or [song]
            i = next((n for n, s in enumerate(self.queue) if s["path"] == path), 0)
            self._make_order(i)
            self.song, self._offset, self._start = song, at, time.monotonic()
            self.playing = self.loaded = False

    # -- buttons --

    def toggle(self):
        self._restore()
        if self.playing:
            self.pause()
        elif self.song and self.loaded:
            self._offset, self._start = self._offset, time.monotonic()
            if self._send("P"):
                self.playing = True
            self._changed()
        elif self.song:                                 # stopped / after a restart: from where it was
            self._load(self.song, at=self._offset if self._offset < (self.song.get("secs") or 0) - 2 else 0)
        else:                                           # nothing yet: the whole library, shuffled
            if not self.shuffle:
                self.shuffle = True
            self.play(["all"])

    def pause(self):
        if self.playing:
            self._offset = self.progress / 1000
            self._start = time.monotonic()
            self.playing = False
            self._send("P")
            self._changed()
            self._save()

    def resume(self):
        if not self.playing:
            self.toggle()

    def next(self):
        self._advance(1)

    def previous(self):
        if self.song and self.progress > BACK_RESTARTS * 1000:
            self._load(self.song)
        else:
            self._advance(-1)

    def toggle_shuffle(self):
        self._restore()
        self.shuffle = not self.shuffle
        if self.queue and self.order:
            cur = self.order[self.pos]
            self._make_order(cur)
        self._changed()
        self._save()

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._send("Q")


class Music:
    """Which player the MUSIC page, the stereo's buttons and the voice work: Spotify or
    the USB stick. Starting one pauses the other; Spotify starting by itself (from the
    phone, say) takes over from the stick."""
    STATE_KEY = "source"

    def __init__(self, spotify_player, local, log=print, state_path=STATE, device=None):
        self.spotify, self.local, self.log, self.device = spotify_player, local, log, device
        self.state_path = state_path
        self.source = "local" if local and _load_state(state_path).get(self.STATE_KEY) == "local" else "spotify"
        self._sp_playing = None                # Spotify's playing state at the last tick
        self._paused_sp_at = None              # monotonic time we last paused Spotify for the stick
        if spotify_player is not None:
            spotify_player.before_play = self.use_spotify
            spotify_player.quiet = lambda: self.source == "local"

    @property
    def active(self):
        return self.local if self.source == "local" and self.local else self.spotify

    def _set(self, source):
        if source != self.source:
            self.source = source
            st = _load_state(self.state_path)
            st[self.STATE_KEY] = source
            _save_state(st, self.state_path)

    def use_local(self):
        """About to play from the stick: Spotify pauses."""
        self._set("local")
        sp = self.spotify
        if sp is not None:
            sp._wanted = None                 # a play tapped earlier mustn't start later
            if sp.playing:
                self._paused_sp_at = time.monotonic()
                sp.pause()

    def use_spotify(self):
        """About to play Spotify (any play from Spotify's side): the stick pauses."""
        if self.local:
            self.local.pause()
        self._set("spotify")

    def play_local(self, context, song=None):
        self.use_local()
        if song is None:
            self.local.play(context)
        else:
            self.local.play_song(context, song)

    def tick(self):
        """Each frame (cheap): Spotify starting to play on its own while the stick is on
        -> Spotify takes over. Spotify still playing a while after we paused it (the pause
        didn't take) -> pause it once more."""
        sp = self.spotify
        if sp is None:
            return
        now_playing = bool(sp.playing and (not sp.device or not self.device or sp.device == self.device))
        was, self._sp_playing = self._sp_playing, now_playing
        if self.source != "local" or was is None:
            return
        if now_playing and not was:
            recent = self._paused_sp_at and time.monotonic() - self._paused_sp_at < 8
            if not recent:
                self.log("music: Spotify started playing: USB music paused")
                self.use_spotify()
        elif now_playing and self._paused_sp_at and 8 < time.monotonic() - self._paused_sp_at < 9 \
                and self.local and self.local.playing:
            self._paused_sp_at = time.monotonic() - 10       # once
            sp.pause()

    # what the stereo's buttons and the voice's music hold call
    @property
    def playing(self):
        return self.active.playing

    def toggle(self):
        if self.source == "local" and not self.local.playing:
            self.use_local()                           # about to play the stick: Spotify pauses
        self.active.toggle()

    def next(self):
        self.active.next()

    def previous(self):
        self.active.previous()

    def toggle_shuffle(self):
        self.active.toggle_shuffle()

    def refresh(self):
        self.active.refresh()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "scan":
        scan(sys.argv[2], *(sys.argv[3:4]))
    else:
        print("usage: localmusic.py scan <dir> [db]")
