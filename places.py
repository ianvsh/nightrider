"""Offline destination search for navigation: addresses, named places (shops,
restaurants, parks...), towns/neighbourhoods and streets from OpenStreetMap,
in SQLite full-text search (FTS5).

Build (once, after downloading the extracts; ~20-40 min on the Pi 4):

    python3 places.py build /var/lib/nightrider/osm/*.osm.pbf
    python3 places.py search "2300 se hawthorne"          # try it
    python3 places.py search "nearest costco" 45.5 -122.6

Names are matched word by word with abbreviations folded together, so
"SE Hawthorne Blvd", "southeast hawthorne boulevard" and "hawthorne" all find
the same street. Results nearest to the car come first.

OSM is missing many house numbers (2026-10-15: "4814 NE 107th" - OSM only has
4809 NE 107th Ave). When the number isn't there, the address is estimated from
the nearest known numbers on that street, like a car GPS does ("approx.").
"""
import math
import os
import re
import sqlite3
import sys
import time

import config

PLACES_DB = os.environ.get("NIGHTRIDER_PLACES", "/var/lib/nightrider/places.db")

# One spelling per word (the short one), so typed, spoken and map forms match:
# "Southeast 12th Avenue", "SE 12th Ave" -> "se 12th ave". Street and Saint both
# become "st", so "St Johns" and "Saint Johns" agree.
FOLD = {"north": "n", "south": "s", "east": "e", "west": "w", "northeast": "ne",
        "northwest": "nw", "southeast": "se", "southwest": "sw", "street": "st",
        "saint": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd", "road": "rd",
        "drive": "dr", "highway": "hwy", "parkway": "pkwy", "place": "pl", "court": "ct",
        "lane": "ln", "terrace": "ter", "circle": "cir", "freeway": "fwy", "mount": "mt",
        "fort": "ft", "&": "and"}
FILLER = re.compile(r"^(please\s+)?(navigate|take me|directions|drive|go|route|get me|"
                    r"head|show me the way|find)(\s+(to|me to))?\s+|^(hey claude,?\s+)", re.I)
NEAREST = re.compile(r"\b(nearest|closest|nearby|near me)\b", re.I)
NUMBERED = re.compile(r"^(\d+)\s+(.+)$")
STATE = re.compile(r",?\s+(washington|oregon|idaho|california|wa|or|usa|united states)\s*$", re.I)
# departments inside a bigger place: shown after it unless asked for ("costco gas")
SIDE = {"pharmacy", "fuel", "optician", "parking", "atm", "car wash", "bank", "garden centre",
        "tyres", "vending machine", "bicycle parking", "parking entrance", "charging station",
        "car repair", "toilets", "post box", "bench", "waste basket", "photo booth"}
SIDE_WORDS = {"fuel": {"gas", "fuel", "petrol", "station"}, "pharmacy": {"pharmacy", "drugstore"},
              "car wash": {"wash"}, "charging station": {"charging", "charger", "ev"}}
APPROX_MAX_GAP = 600              # house numbers: don't guess further than ~6 blocks from a known one
APPROX_EXACT_M = 3000           # the exact address this close (any town name): no estimate for that street

# kinds, best first when equally good matches
KINDS = {"address": 0, "poi": 1, "place": 2, "street": 3}
PLACE_TYPES = {"city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter"}
POI_KEYS = ("amenity", "shop", "tourism", "leisure", "aeroway", "railway", "office",
            "craft", "historic", "healthcare", "sport", "natural", "waterway", "building")
# named natural features worth driving to (not every named wood, stream or lake shore)
NATURAL_OK = {"peak", "volcano", "waterfall", "beach", "bay", "spring", "cave_entrance",
              "arch", "glacier", "hot_spring", "cape", "rock", "water"}
# everyday words for map categories: "nearest gas station" -> fuel stations
CATEGORY = {"gas station": "fuel", "gas": "fuel", "petrol station": "fuel", "petrol": "fuel",
            "grocery store": "supermarket", "grocery": "supermarket", "groceries": "supermarket",
            "coffee shop": "cafe", "coffee": "cafe", "drugstore": "pharmacy", "drug store": "pharmacy",
            "hardware store": "hardware", "ev charger": "charging station", "charger": "charging station",
            "auto parts": "car parts", "car parts store": "car parts", "mechanic": "car repair",
            "hotel": "hotel", "motel": "motel", "bathroom": "toilets", "restroom": "toilets",
            "atm": "atm", "bank": "bank", "hospital": "hospital", "urgent care": "clinic",
            "liquor store": "alcohol", "post office": "post office", "tire shop": "tyres"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (id INTEGER PRIMARY KEY, kind INTEGER, name TEXT,
                                   detail TEXT, lat REAL, lon REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(words, content='', tokenize='unicode61');
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def words(s):
    """Lowercase words with abbreviations folded: 'Southeast 12th Avenue' -> ['se', '12th', 'ave']."""
    out = []
    for w in re.findall(r"[\w&']+", s.lower().replace("'", "")):
        out.append(FOLD.get(w, w))
    return out


def clean_query(q):
    """'Navigate to the nearest Costco please' -> ('costco', True)."""
    q = q.strip().rstrip(".!?")
    while FILLER.search(q):                       # "Hey Claude, take me to ..."
        q = FILLER.sub("", q, count=1)
    nearest = bool(NEAREST.search(q))
    q = NEAREST.sub("", q)
    q = re.sub(r"^\s*(the|a|an)\s+", "", q, flags=re.I)
    q = re.sub(r"\s+(please|thanks|thank you)$", "", q, flags=re.I)
    q = STATE.sub("", q.strip(" ,"))            # "Seattle, Washington": the places have no state
    q = CATEGORY.get(q.lower().strip(" ,"), q)    # "gas station" -> "fuel"
    return q.strip(" ,"), nearest


def poi_kind(tags):
    """'Fred Meyer' + shop=supermarket -> 'supermarket'."""
    for k in POI_KEYS:
        v = tags.get(k)
        if v and v not in ("yes", "no"):
            return v.replace("_", " ")
    return ""


def is_poi(tags):
    """Named and something you'd drive to. Nature only for NATURAL_OK kinds (and
    waterfalls tagged as waterways); not plain named woods or streams."""
    if not tags.get("name"):
        return False
    if tags.get("natural") or tags.get("waterway"):
        return tags.get("natural") in NATURAL_OK or tags.get("waterway") == "waterfall" \
            or any(tags.get(k) for k in POI_KEYS if k not in ("natural", "waterway"))
    return any(tags.get(k) for k in POI_KEYS)


# ---- build ----

def build(pbfs, out=PLACES_DB, log=print):
    import osmium

    t0 = time.time()
    tmp = out + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    streets = {}                      # (name, ~1 km cell) -> (lat, lon): one entry per street per area
    n = [0]

    def add(kind, name, detail, lat, lon):
        cur = db.execute("INSERT INTO places (kind, name, detail, lat, lon) VALUES (?, ?, ?, ?, ?)",
                         (kind, name, detail, round(lat, 6), round(lon, 6)))
        db.execute("INSERT INTO places_fts (rowid, words) VALUES (?, ?)",
                   (cur.lastrowid, " ".join(words(f"{name} {detail}"))))
        n[0] += 1
        if n[0] % 200000 == 0:
            db.commit()
            log(f"  {n[0]:,} places, {time.time() - t0:.0f} s")

    def town(tags):
        return tags.get("addr:city") or tags.get("addr:suburb") or ""

    def take(tags, lat, lon):
        hn, street = tags.get("addr:housenumber"), tags.get("addr:street")
        name = tags.get("name")
        if name and tags.get("place") in PLACE_TYPES:
            add(KINDS["place"], name, tags["place"], lat, lon)
            return
        if is_poi(tags):
            where = " ".join(x for x in (f"{hn} {street}" if hn and street else street or "", town(tags)) if x)
            add(KINDS["poi"], name, f"{poi_kind(tags)} {where}".strip(), lat, lon)
        if hn and street:
            add(KINDS["address"], f"{hn} {street}", " ".join(x for x in (town(tags), tags.get("addr:postcode", "")) if x), lat, lon)

    class Handler(osmium.SimpleHandler):
        def node(self, nd):
            if not nd.tags:
                return
            tags = {t.k: t.v for t in nd.tags}
            take(tags, nd.location.lat, nd.location.lon)

        def way(self, w):
            tags = {t.k: t.v for t in w.tags}
            try:
                pts = [(x.lat, x.lon) for x in w.nodes]
            except osmium.InvalidLocationError:
                return
            if not pts:
                return
            if tags.get("highway") and tags.get("name"):
                mid = pts[len(pts) // 2]
                key = (tags["name"], round(mid[0] * 100), round(mid[1] * 100))
                streets.setdefault(key, mid)
                return
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
            take(tags, lat, lon)

    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(dir=os.path.dirname(out) or ".") as td:
        for pbf in pbfs:
            small = os.path.join(td, os.path.basename(pbf) + ".places.pbf")
            log(f"filtering {pbf}")
            subprocess.run(["osmium", "tags-filter", "--overwrite", "-o", small, pbf,
                            "addr:housenumber", "place", "w/highway", *POI_KEYS[:-1]], check=True)
            # (named natural=water lakes come in as areas: their centre is fine to aim at)
            log(f"reading {small}")
            Handler().apply_file(small, locations=True, idx="flex_mem")
            db.commit()
    for (name, _, _), (lat, lon) in streets.items():
        add(KINDS["street"], name, "street", lat, lon)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('built', ?)", (time.strftime("%Y-%m-%d %H:%M"),))
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, out)
    log(f"done: {n[0]:,} places in {(time.time() - t0) / 60:.1f} min -> {out}")


# ---- search ----

def distance_m(lat1, lon1, lat2, lon2):
    kx = 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * 111320)


class Places:
    def __init__(self, path=PLACES_DB):
        self.path = path
        self.db = None

    @property
    def available(self):
        if self.db is None and os.path.exists(self.path):
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        return self.db is not None

    def search(self, text, near=None, limit=8):
        """-> [{"name", "detail", "lat", "lon", "km"}], best first. near = (lat, lon) of the car."""
        if not self.available:
            return []
        q, nearest = clean_query(text)
        ws = words(q)
        if not ws:
            return []
        # every word must match; the last may be half-typed
        match = " ".join(f'"{w}"' for w in ws[:-1]) + f' "{ws[-1]}"*'
        rows = self.db.execute(
            "SELECT p.kind, p.name, p.detail, p.lat, p.lon, bm25(places_fts) FROM places_fts f "
            "JOIN places p ON p.id = f.rowid WHERE places_fts MATCH ? ORDER BY bm25(places_fts) LIMIT 400",
            (match,)).fetchall()
        out = []
        asked = NUMBERED.match(q)
        if asked:                                # a house number: estimate it where OSM lacks it
            hits = [(NUMBERED.match(n).group(2), town_of(d), lat, lon) for k, n, d, lat, lon, _ in rows
                    if k == KINDS["address"] and n.split()[0] == asked.group(1)]
            exact = {(street, town) for street, town, *_ in hits}
            for r in self._approx(int(asked.group(1)), words(asked.group(2)), exact):
                # the real address nearby under another town name (or none): no guessing.
                # a known house number exists, but houses on the same street with no town in OSM
                # gave a guess from 2510 - at Division, 0.2 mi off - and it was listed first
                if any(distance_m(lat, lon, r["lat"], r["lon"]) < APPROX_EXACT_M
                       and words(street) == words(r["name"].split(" ", 1)[1]) for street, _, lat, lon in hits):
                    continue
                km = distance_m(near[0], near[1], r["lat"], r["lon"]) / 1000 if near else None
                r["km"] = round(km, 1) if km is not None else None
                # behind any exact address (which scores ~1-2 nearby), ahead of other things
                # (the number asked for goes first anyway, below)
                out.append(((math.log1p(km) * 1.2 if km is not None else 0) + 2.5, r))
        best = rows[0][5] if rows else 0
        asked_ws = set(ws)
        for kind, name, detail, lat, lon, rank in rows:
            km = distance_m(near[0], near[1], lat, lon) / 1000 if near else None
            nw = words(name)
            cat = set(words(detail or "")[:2]) if kind == KINDS["poi"] else set()   # "cafe", "charging station"
            in_name = sum(1 for w in ws if w in nw or w in cat
                          or nw and w == ws[-1] and any(x.startswith(w) for x in nw))
            extra = len(set(nw) - asked_ws)            # "Costco Pharmacy" has 1 more word than "costco"
            category = (detail or "").split(" ")[0] if kind == KINDS["poi"] else ""
            side = next((c for c in SIDE if (detail or "").startswith(c)), None) if kind == KINDS["poi"] else None
            wanted_side = side and (asked_ws & SIDE_WORDS.get(side, {side}))
            # lower = better. Nearby matters most when driving; then how well the name matches.
            town = kind == KINDS["place"] and set(nw) == asked_ws   # "Eugene": the town, however far
            score = (math.log1p(km) * (2.5 if nearest else 0.3 if town else 1.2) if km is not None else 0) \
                + 0.05 * (rank - best) + 0.3 * kind - (1.5 if town else 0) \
                + 1.2 * (len(ws) - in_name) + 0.35 * min(extra, 4) \
                + (1.5 if side and not wanted_side else 0) \
                + (1.5 if kind == KINDS["address"] and not asked else 0) \
                - (1.0 if set(nw) == asked_ws else 0)   # the whole name, nothing more
            out.append((score, {"name": name, "detail": detail, "lat": lat, "lon": lon,
                                "km": round(km, 1) if km is not None else None}))
        out.sort(key=lambda s: s[0])
        seen, results = set(), []
        if asked and any(r["name"].split()[0] == asked.group(1) for _, r in out):
            out = [o for o in out if o[1]["name"].split()[0] == asked.group(1)] + \
                  [o for o in out if o[1]["name"].split()[0] != asked.group(1)]   # the number asked for first
        for _, r in out:                          # same name very close by = one result
            key = (r["name"].lower(), round(r["lat"], 3), round(r["lon"], 3))
            if key not in seen:
                seen.add(key)
                results.append(r)
            if len(results) >= limit:
                break
        return results

    def _approx(self, number, street_ws, skip):
        """Estimated spots for `number` on streets matching street_ws (one per street
        and town), from the nearest known house numbers either side."""
        if not street_ws:
            return []
        match = " ".join(f'"{w}"' for w in street_ws[:-1]) + f' "{street_ws[-1]}"*'
        rows = self.db.execute(
            "SELECT p.name, p.detail, p.lat, p.lon FROM places_fts f JOIN places p ON p.id = f.rowid "
            "WHERE places_fts MATCH ? AND p.kind = ? LIMIT 20000", (match, KINDS["address"])).fetchall()
        streets = {}
        n = len(street_ws)
        for name, detail, lat, lon in rows:
            m = NUMBERED.match(name)
            if not m or not m.group(1).isdigit():
                continue
            sw = words(m.group(2))
            # the street asked for, give or take a missing "Ave"/"St" at the end
            if sw[:n - 1] != street_ws[:-1] or len(sw) < n or not sw[n - 1].startswith(street_ws[-1]) \
                    or len(sw) > n + 1:
                continue
            streets.setdefault((m.group(2), town_of(detail)), []).append((int(m.group(1)), lat, lon))
        out = []
        for (street, town), known in streets.items():
            if (street, town) in skip:
                continue
            side = [k for k in known if k[0] % 2 == number % 2] or known    # same side of the street
            below = max((k for k in side if k[0] <= number), default=None)
            above = min((k for k in side if k[0] >= number), default=None)
            if below and above and above[0] != below[0]:
                f = (number - below[0]) / (above[0] - below[0])
                lat, lon = below[1] + f * (above[1] - below[1]), below[2] + f * (above[2] - below[2])
            else:
                _, lat, lon = below or above
            closest = min((k for k in (below, above) if k), key=lambda k: abs(k[0] - number))
            if abs(closest[0] - number) > APPROX_MAX_GAP:
                continue
            out.append({"name": f"{number} {street}", "lat": round(lat, 6), "lon": round(lon, 6),
                        "detail": f"approx. (near {closest[0]}) {town}".strip()})
        return out


def town_of(detail):
    """'Portland 97220' -> 'Portland' (an address's detail is town + postcode)."""
    return re.sub(r"\s*\d{5}(-\d{4})?\s*$", "", detail or "").strip()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "build":
        build(sys.argv[2:])
    elif len(sys.argv) >= 3 and sys.argv[1] == "search":
        near = (float(sys.argv[3]), float(sys.argv[4])) if len(sys.argv) >= 5 else config.HOME
        for r in Places().search(sys.argv[2], near):
            print(f"{r['km']:7.1f} km  {r['name']}  ({r['detail']})")
    else:
        print(__doc__)
