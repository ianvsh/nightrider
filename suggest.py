"""SUGGEST on the MUSIC page: 5 songs like the one playing, put at the front of
the queue. Nothing happens until the button is tapped: no picking ahead of time, no
searches as songs change. Building picks ahead for every song got the app blocked by
Spotify for 5 hours (2026-10-17), and Ian said never again. A tap asks a model for
songs given the song and Ian's taste (taste.suggestion_brief: hearts, plays, skips, and
how earlier picks went), finds them on Spotify one search at a time, and queues them
(~10 s; the button stays filled meanwhile). What gets queued is logged
(taste.record_suggested) so later picks learn from it."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata

import spotify
import taste

N = 5
# Sonnet: Haiku answered as fast but made songs up and ignored the skip list (2026-10-17);
# the picks are worked out before the tap, so the stronger model costs nothing there.
MODEL = os.environ.get("NIGHTRIDER_SUGGEST_MODEL", "sonnet")
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
WORK = os.path.expanduser("~/.cache/nightrider/work")    # run outside the project: no CLAUDE.md to read
NOTE_SECS = 20                                            # how long the screen shows what happened


def prompt_for(item, brief):
    who = ", ".join(a.get("name", "") for a in item.get("artists") or [])
    album = (item.get("album") or {}).get("name", "")
    return (f"The car is playing \"{item.get('name')}\" by {who}" + (f" (album: {album})" if album else "") + ".\n"
            f"Pick {N + 5} real, existing songs (exact Spotify titles) to queue right after it, based mostly on this song - its sound, era, mood "
            "and energy - and on the driver's taste below. Favour songs they haven't saved yet that sit close to their "
            "hearts and to this song; a familiar one is fine now and then. Never this song, nothing by artists "
            "they dislike or keep skipping, nothing they played or was suggested lately, and at most two by this "
            "artist. Reply with ONLY a JSON list of \"title - artist\" strings, best first.\n\n" + brief)


def norm(s):
    """Lower case, no accents or punctuation, no "the": for comparing names."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]| - .*$", "", s)                     # "(Remastered)", "- 2004 Remaster"
    return " ".join(w for w in re.sub(r"[^a-z0-9 ]", " ", s).split() if w != "the")


def strict_find(songs, want=N):
    """"title - artist" -> [(id or None, name)]: only a search result really by that
    artist with that title counts (a loose search's first hit was often a random song).
    One search at a time, stopping once `want` are found: bursts of parallel searches
    are what got the app limited by Spotify."""
    token = spotify._token()

    def find(song):
        if " - " not in song:
            return None, song
        title, artist = (x.strip()[:90] for x in song.rsplit(" - ", 1))   # Spotify refuses queries > 250
        q = f'track:"{title}" artist:"{artist}"'
        items = ((spotify.web("GET", "search", token, q=q, type="track", limit=5) or {}).get("tracks") or {}).get("items") or []
        want_t, want_a = norm(title), norm(artist)
        for t in items:
            artists = [norm(a["name"]) for a in t.get("artists") or []]
            title_ok = norm(t["name"]).startswith(want_t) or want_t.startswith(norm(t["name"]))
            if title_ok and any(want_a in a or a in want_a for a in artists if a):
                return t["id"], f"{t['name']} - {', '.join(a['name'] for a in t['artists'])}"
        return None, song

    out = []
    for song in songs:
        out.append(find(song))
        if sum(1 for sid, _ in out if sid) >= want:
            break
    return out


def parse_songs(text):
    """Claude's reply -> ["title - artist", ...] (a JSON list, or one per line as a fallback)."""
    m = re.search(r"\[.*\]", text or "", re.S)
    if m:
        try:
            return [str(s).strip() for s in json.loads(m.group(0)) if str(s).strip()]
        except ValueError:
            pass
    lines = [re.sub(r"^\s*(\d+[.)]|[-*])\s*", "", l).strip().strip('"') for l in (text or "").splitlines()]
    return [l for l in lines if " - " in l]


class Suggester:
    def __init__(self, log=print, wake=None, ask=None, find=None, queue=None):
        self.log, self.wake = log, wake
        self.ask = ask or self._ask_claude                # prompt -> reply text (tests swap these)
        self.find = find or strict_find
        self.queue = queue or self._queue_tracks
        self.note, self.note_at = "", 0.0
        self.waiting = False       # a tap is being worked on

    # ---- for the screen ----

    @property
    def status(self):
        if self.waiting:
            return "PICKING 5 SONGS LIKE THIS ONE..."
        return self.note if time.monotonic() - self.note_at < NOTE_SECS else ""

    @property
    def busy(self):
        return self.waiting

    def _say(self, note):
        self.note, self.note_at = note, time.monotonic()
        if self.wake:
            self.wake.set()

    # ---- the button ----

    def suggest(self, item):
        """SUGGEST: pick 5 songs like this one and queue them (in a thread)."""
        tid = (item or {}).get("id")
        if not tid or (item.get("type") or "track") != "track" or self.waiting:
            return
        until = spotify.blocked_until()
        if until:
            self._say(f"SPOTIFY BLOCKED TILL {time.strftime('%H:%M', time.localtime(until))}")
            return
        self.waiting = True
        self._say("")

        def run():
            try:
                picks = self._pick(item)
                if not picks:
                    self._say("COULDN'T FIND SONGS - TRY AGAIN")
                    return
                self.queue([p[0] for p in picks])
                taste.record_suggested(item, picks)
                self._say(f"QUEUED {len(picks)}: " + "; ".join(p[1].split(" - ")[0] for p in picks))
            except RuntimeError as e:
                self._say(f"SPOTIFY: {e}")
            except Exception as e:
                self.log(f"suggest: {e!r}")
                self._say("SUGGEST FAILED - TRY AGAIN")
            finally:
                self.waiting = False
                if self.wake:
                    self.wake.set()
        threading.Thread(target=run, daemon=True).start()

    def _pick(self, item):
        """-> [(spotify id, "title - artist")] for up to N songs like item."""
        t0 = time.monotonic()
        songs = parse_songs(self.ask(prompt_for(item, taste.suggestion_brief())))
        avoid = taste.recent_ids() | {item["id"]}
        skipped = {norm(a) for a in taste.skipped_artists()}              # in code, not just asked for
        here = norm(item.get("name"))
        songs = [s for s in songs if not (" - " in s and (norm(s.rsplit(" - ", 1)[1]) in skipped
                                                           or norm(s.rsplit(" - ", 1)[0]) == here))]
        picks = []
        for sid, name in self.find(songs):
            if sid and sid not in avoid and sid not in {p[0] for p in picks}:
                picks.append((sid, name))
            if len(picks) == N:
                break
        self.log(f"suggest: {len(picks)} picks for {item.get('name')!r} in {time.monotonic() - t0:.1f}s")
        return picks

    def _ask_claude(self, prompt):
        os.makedirs(WORK, exist_ok=True)
        r = subprocess.run([CLAUDE, "-p", prompt, "--model", MODEL, "--tools", "", "--strict-mcp-config",
                            "--no-session-persistence", "--output-format", "text"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90, cwd=WORK)
        if r.returncode:
            raise RuntimeError((r.stderr.strip().splitlines() or [f"claude exited {r.returncode}"])[-1][:80])
        return r.stdout

    def _queue_tracks(self, ids):
        token, dev = spotify._token(), spotify._device()
        for sid in ids:                                    # in order: each lands after the one before
            spotify.web("POST", "me/player/queue", token, uri=f"spotify:track:{sid}", device_id=dev)
