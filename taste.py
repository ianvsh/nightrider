"""Ian's music taste, for Claude to pick songs with (the `music_taste` tool in
approve_mcp.py, and a short version in the Claude page's system prompt).

Sources:
- notes Claude saves when Ian says what he likes or doesn't (NOTES),
- songs he hearts (saves to Liked Songs) on the MUSIC page or by asking Claude (LIKES),
- songs played through to the end (PLAYS), the quiet sign he's happy with them,
- songs skipped early, from the screen's NEXT button or by asking Claude (SKIPS),
- what the MUSIC page's SUGGEST queued (SUGGESTED); plays and skips of those are
  marked "suggested", so how the picks went feeds the next ones,
- Spotify's own view: top artists/tracks, recently liked songs (cached in
  SPOTIFY_CACHE for a day so it still works with no internet in the car).
All small files in ~/.config/nightrider; written rarely (SD card)."""

import json
import os
import time

import config

DIR = os.environ.get("NIGHTRIDER_CONFIG", os.path.expanduser("~/.config/nightrider"))
NOTES = os.path.join(DIR, "music-taste.json")
SKIPS = os.path.join(DIR, "music-skips.jsonl")
LIKES = os.path.join(DIR, "music-likes.jsonl")
PLAYS = os.path.join(DIR, "music-plays.jsonl")
SUGGESTED = os.path.join(DIR, "music-suggested.jsonl")
SPOTIFY_CACHE = os.path.join(DIR, "music-spotify.json")
KINDS = ("likes", "dislikes", "notes")
SKIP_BEFORE = 0.6           # NEXT before 60% of the song (and before its last 30 s) = a skip
PLAYED_TO = 0.9             # still going at 90% of the song (or its last 30 s) when it changed = played through


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    os.makedirs(DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def notes():
    n = _load(NOTES, {})
    return {k: list(n.get(k, [])) for k in KINDS}


def remember(kind, text):
    """kind: like / dislike / note (plural ok). -> message for Claude."""
    kind = {"like": "likes", "dislike": "dislikes", "note": "notes"}.get(kind, kind)
    text = " ".join(str(text or "").split())
    if kind not in KINDS or not text:
        return "Error: give kind (like, dislike or note) and text."
    n = notes()
    for k in KINDS:                                      # a new like replaces an old dislike of it, etc.
        n[k] = [e for e in n[k] if e["text"].lower() != text.lower()]
    n[kind].append({"text": text, "date": time.strftime("%Y-%m-%d")})
    _save(NOTES, n)
    return f"Saved under {kind}: {text}"


def forget(text):
    text = str(text or "").strip().lower()
    if not text:
        return "Error: say what to forget."
    n = notes()
    gone = [e["text"] for k in KINDS for e in n[k] if text in e["text"].lower()]
    if not gone:
        return f"Nothing saved matches '{text}'."
    for k in KINDS:
        n[k] = [e for e in n[k] if text not in e["text"].lower()]
    _save(NOTES, n)
    return "Forgot: " + "; ".join(gone)


def _song(item):
    row = {"ts": round(time.time()), "track": item["name"],
           "artist": ", ".join(a.get("name", "") for a in item.get("artists") or [])}
    if item.get("id"):
        row["id"] = item["id"]
        if item["id"] in {r.get("id") for r in _rows(SUGGESTED, 14)}:
            row["suggested"] = True                         # SUGGEST queued it: how did it go?
    return row


def _append(path, row, keep=500):
    try:
        os.makedirs(DIR, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 200_000:      # keep it small
            with open(path) as f:
                rows = f.readlines()[-keep:]
            with open(path, "w") as f:
                f.writelines(rows)
        with open(path, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def record_skip(playback, by):
    """playback: spotify_player's playback JSON just before NEXT. Logs it if
    it was an early skip. by: "screen" or "claude"."""
    item = (playback or {}).get("item") or {}
    pos, length = (playback or {}).get("progress_ms") or 0, item.get("duration_ms") or 0
    if not item.get("name") or not length or pos > length * SKIP_BEFORE or pos > length - 30000:
        return False
    return _append(SKIPS, dict(_song(item), at_s=pos // 1000, of_s=length // 1000, by=by))


def record_like(item, by):
    """A song saved to Liked Songs. item: Spotify track JSON; by: "screen" or "claude"."""
    if not (item or {}).get("name"):
        return False
    return _append(LIKES, dict(_song(item), album=(item.get("album") or {}).get("name", ""), by=by), keep=2000)


def record_play(item, pos):
    """The song changed with pos ms of item played: logs it if it was played through."""
    length = (item or {}).get("duration_ms") or 0
    if not (item or {}).get("name") or not length or (pos < length * PLAYED_TO and pos < length - 30000):
        return False
    return _append(PLAYS, _song(item))


def record_suggested(seed, picks):
    """SUGGEST queued picks [(spotify id, "title - artist")] for the seed song."""
    seed = f"{seed.get('name')} - " + ", ".join(a.get("name", "") for a in seed.get("artists") or [])
    for sid, name in picks:
        _append(SUGGESTED, {"ts": round(time.time()), "id": sid, "song": name, "seed": seed}, keep=1000)


def suggested(days=30):
    return _rows(SUGGESTED, days)


def recent_ids(days=7):
    """Track ids played, skipped or suggested lately: not to be suggested again."""
    return {r["id"] for path in (PLAYS, SKIPS, SUGGESTED) for r in _rows(path, days) if r.get("id")}


def skipped_artists(days=60, times=2):
    """Artists skipped early at least `times` times lately (not ones he's hearted: a heart wins)."""
    counts, hearted = {}, {r["artist"] for r in likes()}
    for r in skips(days):
        counts[r["artist"]] = counts.get(r["artist"], 0) + 1
    return [a for a, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= times and a not in hearted]


def suggestion_brief():
    """Compact taste for the quick SUGGEST model (the full profile is for the AI page)."""
    n, out = notes(), []
    for k, title in (("likes", "Likes"), ("dislikes", "Dislikes"), ("notes", "Notes")):
        if n[k]:
            out.append(f"{title}: " + "; ".join(e["text"] for e in n[k][-20:]))
    lk = likes()
    if lk:
        out.append("Hearted (strongest signal): " + "; ".join(f"{r['track']} - {r['artist']}" for r in lk[-25:][::-1]))
    sk = skips(60)
    often = skipped_artists()[:10]
    if often:
        out.append("Keeps skipping: " + ", ".join(often))
    picked = [r for r in plays(30) + sk if r.get("suggested")]
    if picked:
        out.append("Earlier suggestions they " + "; ".join(
            f"{'played through' if 'at_s' not in r else 'skipped'}: {r['track']} - {r['artist']}" for r in picked[-15:]))
    recent = [f"{r['track']} - {r['artist']}" for r in plays(3)[-20:]] + [r["song"] for r in suggested(3)[-20:]]
    if recent:
        out.append("Played or suggested lately (don't repeat): " + "; ".join(recent))
    sv = spotify_view() or {}
    for key, title in (("top_artists_recent", "Top artists lately"), ("top_artists_year", "Top artists this year")):
        if sv.get(key):
            out.append(f"{title}: " + ", ".join(sv[key][:15]))
    return "\n".join(out) or "Nothing known about their taste yet."


def likes(days=None):
    return _rows(LIKES, days)


def plays(days=90):
    return _rows(PLAYS, days)


def skips(days=90):
    return _rows(SKIPS, days)


def _rows(path, days):
    since = time.time() - days * 86400 if days else 0
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def spotify_view(fetch=None, max_age=86400):
    """Spotify's top artists/tracks and recent likes. fetch() -> fresh dict, or
    raises (no internet): then the last saved copy is used."""
    cached = _load(SPOTIFY_CACHE, None)
    if fetch and (not cached or time.time() - cached.get("fetched", 0) > max_age):
        try:
            cached = dict(fetch(), fetched=round(time.time()))
            _save(SPOTIFY_CACHE, cached)
        except Exception:
            pass
    return cached


def profile(fetch=None):
    """Everything known, as text for Claude."""
    n = notes()
    out = []
    for k, title in (("likes", "Likes"), ("dislikes", "Dislikes"), ("notes", "Other notes")):
        if n[k]:
            out.append(title + ":\n" + "\n".join(f"- {e['text']} ({e['date']})" for e in n[k]))
    if not any(n.values()):
        out.append("No likes or dislikes saved yet.")
    lk = likes()
    if lk:
        out.append("Songs they hearted (saved to Liked Songs), newest first - the strongest signal:\n" + "\n".join(
            f"- {r['track']} - {r['artist']} ({time.strftime('%Y-%m-%d', time.localtime(r['ts']))})"
            for r in lk[-40:][::-1]))
    pl = plays()
    if pl:
        by_artist = {}
        for r in pl:
            by_artist[r["artist"]] = by_artist.get(r["artist"], 0) + 1
        out.append("Artists they play through most (last 90 days): " + ", ".join(
            f"{a} ({c})" for a, c in sorted(by_artist.items(), key=lambda x: -x[1])[:15]))
        out.append("Recently played through (avoid repeating these): " + "; ".join(
            f"{r['track']} - {r['artist']}" for r in pl[-15:][::-1]))
    tried = [r for r in pl + skips() if r.get("suggested")]
    if tried:
        good = [f"{r['track']} - {r['artist']}" for r in tried if "at_s" not in r]
        bad = [f"{r['track']} - {r['artist']}" for r in tried if "at_s" in r]
        out.append(f"SUGGEST picks: {len(good)} played through, {len(bad)} skipped early"
                   + (f". Liked: {'; '.join(good[-10:])}" if good else "")
                   + (f". Skipped: {'; '.join(bad[-10:])}" if bad else ""))
    sk = skips()
    if sk:
        by_artist = {}
        for r in sk:
            by_artist[r["artist"]] = by_artist.get(r["artist"], 0) + 1
        often = [f"{a} ({c})" for a, c in sorted(by_artist.items(), key=lambda x: -x[1]) if c >= 2][:10]
        if often:
            out.append("Artists they often skip (last 90 days): " + ", ".join(often))
        out.append("Recently skipped early: " + "; ".join(
            f"{r['track']} - {r['artist']} at {r['at_s']}s" for r in sk[-15:][::-1]))
    sv = spotify_view(fetch)
    if sv:
        age = (time.time() - sv.get("fetched", 0)) / 86400
        out.append(f"From Spotify ({'today' if age < 1 else f'{age:.0f} days ago'}):")
        for key, title in (("top_artists_recent", "Top artists, last 4 weeks"),
                           ("top_artists_year", "Top artists, last 6 months"),
                           ("top_artists_all", "Top artists, all time"),
                           ("top_tracks_recent", "Top tracks, last 4 weeks"),
                           ("liked_recent", "Recently liked songs")):
            if sv.get(key):
                out.append(f"{title}: " + "; ".join(sv[key]))
    return "\n\n".join(out)


def prompt_note():
    """Short version for the system prompt: saved likes/dislikes and the latest hearts."""
    n = notes()
    parts = []
    for k, title in (("likes", "likes"), ("dislikes", "dislikes"), ("notes", "music notes")):
        if n[k]:
            parts.append(f"{config.DRIVER or 'The driver'} {title}: " + "; ".join(e["text"] for e in n[k][-30:]) + ".")
    lk = likes()
    if lk:
        parts.append("Songs they hearted lately: " + "; ".join(
            f"{r['track']} - {r['artist']}" for r in lk[-10:][::-1]) + ".")
    return " ".join(parts)
