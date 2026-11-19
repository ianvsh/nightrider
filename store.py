"""SQLite storage for logged samples, grouped into sessions (drives).

A session starts when engine data appears and ends after SESSION_GAP_SECS
without any. Samples keep their readings as JSON so new fields need no schema
change. Writes are batched (commit every COMMIT_SECS) to spare the SD card.
Each session also keeps the distance driven (`km`, from odometer.py); sessions
logged before that existed are worked out from their samples once (backfill_km).
"""
import json
import sqlite3
import threading
import time

from odometer import Odometer, fix_of

SESSION_GAP_SECS = 30
COMMIT_SECS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id      INTEGER PRIMARY KEY,
    start   REAL NOT NULL,
    end     REAL NOT NULL,
    samples INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS samples (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    session INTEGER,            -- NULL = heartbeat while the engine is off
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_session ON samples(session, id);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        if "km" not in [r[1] for r in self.db.execute("PRAGMA table_info(sessions)")]:
            self.db.execute("ALTER TABLE sessions ADD COLUMN km REAL")     # NULL = not worked out yet
        self.db.commit()
        self.lock = threading.Lock()
        self.session = None       # id of the open session
        self.last_engine_ts = 0
        self.last_commit = time.time()
        self.latest = None        # most recent sample as a dict
        self.odo = Odometer(self._last_fix())

    def _last_fix(self):
        """(ts, (lat, lon)) of the newest logged position: where the car was parked, so
        a drive that starts before the GPS has a fix still counts from there."""
        rows = self.db.execute("SELECT ts, data FROM samples WHERE id > (SELECT COALESCE(MAX(id), 0) - 50000 "
                               "FROM samples) AND data LIKE '%\"lat\":%' ORDER BY id DESC LIMIT 20")
        for ts, data in rows:
            pos = fix_of(json.loads(data))
            if pos:
                return ts, pos
        return None

    def add(self, data, engine, ts=None):
        """Record one sample. `engine` = it contains engine data (opens/extends a session).
        -> the sample as a dict, with `km` = distance driven since the one before."""
        ts = ts or time.time()
        with self.lock:
            session, km = None, 0.0
            if engine:
                if self.session is None or ts - self.last_engine_ts > SESSION_GAP_SECS:
                    cur = self.db.execute(
                        "INSERT INTO sessions (start, end, km) VALUES (?, ?, 0)", (ts, ts))
                    self.session = cur.lastrowid
                self.last_engine_ts = ts
                session = self.session
                km = self.odo.add(ts, data)
                self.db.execute(
                    "UPDATE sessions SET end = ?, samples = samples + 1, km = COALESCE(km, 0) + ? WHERE id = ?",
                    (ts, km, session))
            else:
                self.odo.parked(ts, data)
                if self.session is not None and ts - self.last_engine_ts > SESSION_GAP_SECS:
                    self.session = None
            cur = self.db.execute(
                "INSERT INTO samples (ts, session, data) VALUES (?, ?, ?)",
                (ts, session, json.dumps(data, separators=(",", ":"))))
            self.latest = {"id": cur.lastrowid, "ts": ts, "session": session, "data": data, "km": km}
            if ts - self.last_commit >= COMMIT_SECS:
                self.db.commit()
                self.last_commit = ts
            return self.latest

    def flush(self):
        with self.lock:
            self.db.commit()

    # --- queries (used by the Wi-Fi and Bluetooth servers) -----------------

    def sessions(self, limit=50, before=None):
        """Newest first. `before` = only sessions with id < before (paging)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM sessions WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before or 2**62, limit)).fetchall()
        return [dict(r) for r in rows]

    def samples(self, session=None, since_id=0, limit=500, since_ts=None):
        """Oldest first, id > since_id. session=None = all samples (incl. heartbeats)."""
        q, args = "SELECT * FROM samples WHERE id > ?", [since_id]
        if session is not None:
            q += " AND session = ?"
            args.append(session)
        if since_ts is not None:
            q += " AND ts >= ?"
            args.append(since_ts)
        q += " ORDER BY id LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "session": r["session"],
                 "data": json.loads(r["data"])} for r in rows]

    def counts(self):
        with self.lock:
            n = self.db.execute("SELECT COUNT(*), MAX(id) FROM samples").fetchone()
            s = self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return {"samples": n[0], "last_id": n[1] or 0, "sessions": s[0]}

    def set_km(self, km):
        """{session id: km} for sessions that don't have a distance yet (backfill_km)."""
        with self.lock:
            for sid, v in km.items():
                self.db.execute("UPDATE sessions SET km = ? WHERE id = ? AND km IS NULL", (v, sid))
            self.db.commit()
            self.last_commit = time.time()

    def close(self):
        with self.lock:
            self.db.commit()
            self.db.close()


def backfill_km(path, log=print, save=None):
    """Distance for sessions logged before sessions had one (km NULL): the same
    odometer run over every sample in order, heartbeats included (where it was
    parked). Reads on its own connection, so it can run in a thread beside the
    logger; save = the logger's Store.set_km, because the logger's connection holds
    the write lock between commits (a minute at a time with the engine off)."""
    db = sqlite3.connect(path, timeout=30)
    todo = {r[0] for r in db.execute("SELECT id FROM sessions WHERE km IS NULL")}
    if not todo:
        db.close()
        return 0
    t0, odo, km, last = time.time(), Odometer(), {}, 0
    while True:
        rows = db.execute("SELECT id, ts, session, data FROM samples WHERE id > ? ORDER BY id LIMIT 20000",
                          (last,)).fetchall()
        if not rows:
            break
        for last, ts, session, data in rows:
            if session is None:
                odo.parked(ts, json.loads(data))
            else:
                if session not in km:
                    odo.prev = None                          # a new drive
                km[session] = km.get(session, 0.0) + odo.add(ts, json.loads(data))
    db.close()
    result = {sid: round(km.get(sid, 0.0), 4) for sid in todo}
    if save:
        save(result)
    else:
        db = sqlite3.connect(path, timeout=30)
        for sid, v in result.items():
            db.execute("UPDATE sessions SET km = ? WHERE id = ? AND km IS NULL", (v, sid))
        db.commit()
        db.close()
    log(f"distance worked out for {len(todo)} earlier drives ({sum(km.get(s, 0) for s in todo) / 1.609344:.1f} mi) "
        f"in {time.time() - t0:.0f}s")
    return len(todo)
