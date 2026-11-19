"""Offline road map for the MAP page: OpenStreetMap roads + rivers in SQLite.

Build (once, ~30-60 min on the Pi 4; needs osmium-tool + python3-pyosmium):

    python3 roadmap.py build /var/lib/nightrider/osm/*.osm.pbf

Extracts come from download.geofabrik.de (OR + WA so far). The build keeps
roads (motorway..residential) and big rivers, cuts each way into short pieces
so the R-tree boxes stay tight, and writes ROADS_DB. Queries: Roads.near().

    python3 roadmap.py query 45.5189 -122.6793     # roads within ~300 m
"""
import array
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

ROADS_DB = os.environ.get("NIGHTRIDER_ROADS", "/var/lib/nightrider/roads.db")

# road class: 0 = biggest. Display brightness/zoom filtering go by this.
CLASSES = {
    "motorway": 0, "motorway_link": 0,
    "trunk": 1, "trunk_link": 1,
    "primary": 2, "primary_link": 2,
    "secondary": 3, "secondary_link": 3,
    "tertiary": 4, "tertiary_link": 4,
    "residential": 5, "unclassified": 5, "living_street": 5, "road": 5,
}
RIVER = 9                       # waterway=river (lines only; no lakes/areas)
PIECE = 24                      # points per stored piece
SCALE = 1e6                     # coordinates stored as int micro-degrees

SCHEMA = """
CREATE TABLE IF NOT EXISTS roads (id INTEGER PRIMARY KEY, cls INTEGER NOT NULL,
                                  name TEXT, pts BLOB NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS roads_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def label(tags):
    """What to call a road: its name, else its ref ("I 5", "OR 99E")."""
    name, ref = tags.get("name"), tags.get("ref")
    if tags.get("highway", "").startswith("motorway") and ref:
        return ref.split(";")[0]
    return name or (ref.split(";")[0] if ref else None)


# ---- build ----

def build(pbfs, out=ROADS_DB, log=print):
    import osmium

    t0 = time.time()
    tmp_db = out + ".building"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    db = sqlite3.connect(tmp_db)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    seen = set()                    # ways in both extracts (border roads)
    count = [0, 0]

    class Handler(osmium.SimpleHandler):
        def way(self, w):
            if w.id in seen:
                return
            tags = {t.k: t.v for t in w.tags}
            cls = CLASSES.get(tags.get("highway"))
            if cls is None:
                if tags.get("waterway") != "river":
                    return
                cls = RIVER
            try:
                pts = [(round(n.lat * SCALE), round(n.lon * SCALE)) for n in w.nodes]
            except osmium.InvalidLocationError:
                return
            if len(pts) < 2:
                return
            seen.add(w.id)
            name = label(tags) if cls != RIVER else tags.get("name")
            for i, start in enumerate(range(0, len(pts) - 1, PIECE - 1)):
                piece = pts[start:start + PIECE]
                rid = w.id * 1000 + i
                lats = [p[0] for p in piece]
                lons = [p[1] for p in piece]
                db.execute("INSERT INTO roads VALUES (?, ?, ?, ?)",
                           (rid, cls, name, array.array("i", [v for p in piece for v in p]).tobytes()))
                db.execute("INSERT INTO roads_idx VALUES (?, ?, ?, ?, ?)",
                           (rid, min(lats) / SCALE, max(lats) / SCALE, min(lons) / SCALE, max(lons) / SCALE))
                count[1] += 1
            count[0] += 1
            if count[0] % 100000 == 0:
                log(f"  {count[0]:,} ways, {count[1]:,} pieces, {time.time() - t0:.0f} s")

    with tempfile.TemporaryDirectory(dir=os.path.dirname(out) or ".") as tmp:
        for pbf in pbfs:
            small = os.path.join(tmp, os.path.basename(pbf) + ".roads.pbf")
            log(f"filtering {pbf}")
            subprocess.run(["osmium", "tags-filter", "--overwrite", "-o", small, pbf,
                            "w/highway=" + ",".join(CLASSES), "w/waterway=river"], check=True)
            log(f"reading {small}")
            Handler().apply_file(small, locations=True, idx="flex_mem")
            db.commit()
    db.execute("INSERT OR REPLACE INTO meta VALUES ('built', ?)", (time.strftime("%Y-%m-%d %H:%M"),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('sources', ?)",
               (", ".join(os.path.basename(p) for p in pbfs),))
    db.commit()
    db.close()
    os.replace(tmp_db, out)
    log(f"done: {count[0]:,} ways, {count[1]:,} pieces in {(time.time() - t0) / 60:.1f} min -> {out}")


# ---- queries ----

class Roads:
    """Read-only access for the display. Missing database = no roads (the map
    page still shows the track)."""

    def __init__(self, path=ROADS_DB):
        self.path = path
        self.db = None

    def _open(self):
        if self.db is None and os.path.exists(self.path):
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        return self.db

    @property
    def available(self):
        return self._open() is not None

    def box(self, lat0, lat1, lon0, lon1, max_cls=9):
        """Roads touching the box: [(cls, name, [(lat, lon), ...]), ...]."""
        db = self._open()
        if not db:
            return []
        rows = db.execute(
            "SELECT r.cls, r.name, r.pts FROM roads_idx i JOIN roads r ON r.id = i.id "
            "WHERE i.maxlat >= ? AND i.minlat <= ? AND i.maxlon >= ? AND i.minlon <= ? AND r.cls <= ?",
            (lat0, lat1, lon0, lon1, max_cls)).fetchall()
        out = []
        for cls, name, blob in rows:
            a = array.array("i")
            a.frombytes(blob)
            out.append((cls, name, [(a[i] / SCALE, a[i + 1] / SCALE) for i in range(0, len(a), 2)]))
        return out

    def near(self, lat, lon, metres, max_cls=9):
        dlat = metres / 111320
        dlon = metres / (111320 * max(0.1, math.cos(math.radians(lat))))
        return self.box(lat - dlat, lat + dlat, lon - dlon, lon + dlon, max_cls)


def nearest_name(roads, lat, lon, course=None, max_m=30):
    """Name of the road the car is on: the closest named piece within max_m,
    preferring ones running the way we're heading."""
    kx = 111320 * math.cos(math.radians(lat))
    best, best_score = None, max_m
    for cls, name, pts in roads:
        if not name or cls == RIVER:
            continue
        for (a_lat, a_lon), (b_lat, b_lon) in zip(pts, pts[1:]):
            ax, ay = (a_lon - lon) * kx, (a_lat - lat) * 111320
            bx, by = (b_lon - lon) * kx, (b_lat - lat) * 111320
            dx, dy = bx - ax, by - ay
            seg = dx * dx + dy * dy
            t = 0 if seg == 0 else max(0, min(1, -(ax * dx + ay * dy) / seg))
            dist = math.hypot(ax + t * dx, ay + t * dy)
            if course is not None and seg > 1:
                bearing = math.degrees(math.atan2(dx, dy)) % 180
                off = abs((course % 180) - bearing)
                dist += 10 * min(off, 180 - off) / 90       # up to +10 m across our heading
            if dist < best_score:
                best, best_score = name, dist
    return best


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "build":
        build(sys.argv[2:])
    elif len(sys.argv) == 4 and sys.argv[1] == "query":
        r = Roads()
        lat, lon = float(sys.argv[2]), float(sys.argv[3])
        found = r.near(lat, lon, 300)
        print(f"{len(found)} pieces; on: {nearest_name(found, lat, lon)}")
        for cls, name, pts in sorted(found, key=lambda r: (r[0], r[1] or ""))[:15]:
            print(f"  class {cls}  {name or '-'}  {len(pts)} points")
    else:
        print(__doc__)
