"""Turn-by-turn navigation for the MAP page, fully offline.

- Destinations: spoken (voice.py -> Whisper) or typed (screens.Keyboard), looked
  up in places.py's index of OSM addresses / places / streets, nearest first.
  Recent picks are kept in ~/.config/datalogger/destinations.json; places Ian
  names himself (HOME, WORK, ...) in saved-places.json (GO > SAVED).
- Routes: Valhalla (pyvalhalla in .venv) on tiles built from the same OSM
  extracts (/var/lib/datalogger/valhalla, see CLAUDE.md "Routing").
- Guidance: the next maneuver and distance to it come from where the GPS puts us
  along the route line; more than OFF_ROUTE_M off it for a few fixes = reroute.

`Navigator` holds the state; `DestinationScreen` is the full-screen picker.
"""
import json
import math
import os
import threading
import time

import places
from pixels import AMBER, DIM, FAINT, GREEN, LH, LW, RED, blink, button, inside, text
import screens

VALHALLA_CONFIG = os.environ.get("DATALOGGER_VALHALLA", "/var/lib/datalogger/valhalla/valhalla.json")
RECENT = os.path.expanduser("~/.config/datalogger/destinations.json")
SAVED = os.path.expanduser("~/.config/datalogger/saved-places.json")
OFF_ROUTE_M = 50                # this far from the route line ...
OFF_ROUTE_FIXES = 4             # ... for this many fixes in a row = reroute
REROUTE_GAP_SECS = 10
ARRIVED_M = 40
SPEAK_PROMPT = "Navigate to an address or a place, for example 2300 SE Hawthorne Blvd, or Powell's Books."
M_PER_DEG = 111320


def log(msg):
    print(time.strftime("%H:%M:%S"), "nav:", msg, flush=True)


def decode_polyline(s, precision=6):
    """Valhalla's encoded shape (polyline6) -> [(lat, lon)]."""
    coords, index, lat, lon = [], 0, 0, 0
    factor = 10 ** precision
    while index < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def metres(a, b):
    kx = M_PER_DEG * math.cos(math.radians(a[0]))
    return math.hypot((b[1] - a[1]) * kx, (b[0] - a[0]) * M_PER_DEG)


# Valhalla maneuver type -> how the screen draws it: turn angle (degrees, + = right),
# or "arrive" / "start"
TURNS = {1: "start", 2: "start", 3: "start", 4: "arrive", 5: "arrive", 6: "arrive",
         7: 0, 8: 0, 9: 45, 10: 90, 11: 135, 12: 180, 13: -180, 14: -135, 15: -90, 16: -45,
         17: 0, 18: 45, 19: -45, 20: 45, 21: -45, 22: 0, 23: 20, 24: -20, 25: 0,
         26: 90, 27: 90, 37: 20, 38: -20}


def nice_distance(m, mph=True):
    """'0.3 mi' / '500 ft' / '1.2 km' / '300 m' for the banner."""
    if mph:
        ft = m / 0.3048
        if ft < 1000:
            return f"{int(round(ft / 50) * 50)} ft"
        mi = m / 1609.344
        return f"{mi:.1f} mi" if mi < 10 else f"{mi:.0f} mi"
    if m < 1000:
        return f"{int(round(m / 10) * 10)} m"
    return f"{m / 1000:.1f} km" if m < 10000 else f"{m / 1000:.0f} km"


class Navigator:
    def __init__(self, speech=None, finder=None):
        self.voice = speech                 # voice.Voice, shared with the AI page
        self.places = finder or places.Places()
        self.actor = None
        self.actor_error = None
        self.lock = threading.Lock()
        self.dest = None                    # {"name", "detail", "lat", "lon"}
        self.route = None                   # see _parse
        self.status = ""                    # "ROUTING...", "REROUTING...", errors
        self.progress = None                # see update()
        self.off_count = 0
        self.last_route_at = 0
        self.arrived_at = None

    @property
    def active(self):
        return self.dest is not None

    @property
    def available(self):
        return os.path.exists(VALHALLA_CONFIG) and self.places.available

    # ---- routing ----

    def _get_actor(self):
        if self.actor is None and self.actor_error is None:
            try:
                from valhalla import Actor
                self.actor = Actor(VALHALLA_CONFIG)
            except Exception as e:
                self.actor_error = str(e)
                log(f"router unavailable: {e}")
        return self.actor

    def go(self, dest, fix):
        """Start navigating to dest from the current fix."""
        self.dest = dest
        self.route, self.progress, self.arrived_at = None, None, None
        remember(dest)
        log(f"to {dest['name']} ({dest['lat']:.5f}, {dest['lon']:.5f})")
        self._route_async(fix, "ROUTING...")

    def stop(self):
        self.dest = self.route = self.progress = self.arrived_at = None
        self.status = ""

    def _route_async(self, fix, status):
        if not fix:
            self.status = "WAITING FOR GPS"
            return
        self.status = status
        self.last_route_at = time.time()
        threading.Thread(target=self._route, args=(dict(fix), self.dest), daemon=True).start()

    def _route(self, fix, dest):
        actor = self._get_actor()
        if not actor:
            self.status = "NO ROUTING DATA"
            return
        start = {"lat": fix["lat"], "lon": fix["lon"]}
        if fix.get("course_deg") is not None and (fix.get("speed_kmh") or 0) > 8:
            start.update(heading=fix["course_deg"], heading_tolerance=45)   # don't route a U-turn
        req = {"locations": [start, {"lat": dest["lat"], "lon": dest["lon"]}],
               "costing": "auto", "directions_options": {"units": "miles"}}
        t0 = time.time()
        try:
            reply = json.loads(actor.route(json.dumps(req)))
            route = parse(reply)
        except Exception as e:
            msg = str(e)
            log(f"route failed: {msg}")
            if self.dest is dest:
                self.status = "NO ROUTE FOUND" if "No path" in msg or "442" in msg else "ROUTE ERROR"
            return
        with self.lock:
            if self.dest is not dest:      # cancelled / changed while we worked
                return
            # a new route has its own points: the place on the old one means nothing here
            # (keeping it crashed the display twice after reroutes near home, 2026-10-17)
            self.route, self.status, self.off_count, self.progress = route, "", 0, None
        log(f"route {route['metres'] / 1609.344:.1f} mi, {route['secs'] / 60:.0f} min, "
            f"{len(route['steps'])} steps in {time.time() - t0:.1f}s")

    # ---- following it ----

    def update(self, fix):
        """Call with each GPS fix: where we are on the route, the next step, reroutes."""
        r = self.route
        if not self.dest or not fix:
            return
        here = (fix["lat"], fix["lon"])
        if metres(here, (self.dest["lat"], self.dest["lon"])) < ARRIVED_M and r:
            self.arrived_at = self.arrived_at or time.time()
        if self.arrived_at:
            self.progress = {"arrived": True}
            if time.time() - self.arrived_at > 60:
                self.stop()
            return
        if not r:
            return
        i, off = nearest_on_route(r["shape"], here, self.progress["index"] if self.progress else 0)
        if off > OFF_ROUTE_M:
            self.off_count += 1
            if self.off_count >= OFF_ROUTE_FIXES and time.time() - self.last_route_at > REROUTE_GAP_SECS:
                log(f"off route by {off:.0f} m: rerouting")
                self._route_async(fix, "REROUTING...")
        else:
            self.off_count = 0
        step = next((s for s in r["steps"] if s["at"] > i), r["steps"][-1])
        to_step = r["cum"][step["at"]] - r["cum"][i]
        left = r["metres"] - r["cum"][i]
        secs = r["secs"] * left / max(r["metres"], 1)
        self.progress = {"index": i, "off": off, "step": step, "to_step": to_step,
                         "left_m": left, "left_s": secs}


def nearest_on_route(shape, here, start=0):
    """(index of the closest route point at/after start-20, distance in m). Searching
    forward from where we were stops a route that doubles back from jumping ahead."""
    lo = max(0, min(start - 20, len(shape) - 1))     # (a start past this route's end: from near its end)
    best, best_d = lo, float("inf")
    kx = M_PER_DEG * math.cos(math.radians(here[0]))
    for j in range(lo, len(shape)):
        lat, lon = shape[j]
        d = math.hypot((lon - here[1]) * kx, (lat - here[0]) * M_PER_DEG)
        if d < best_d:
            best, best_d = j, d
        elif d > best_d + 2000:            # well past the closest stretch: stop looking
            break
    return best, best_d


def parse(reply):
    """Valhalla /route reply -> {"shape", "cum" (metres along), "steps", "metres", "secs"}."""
    trip = reply["trip"]
    shape, steps = [], []
    for leg in trip["legs"]:
        base = len(shape)
        pts = decode_polyline(leg["shape"])
        shape.extend(pts)
        for m in leg["maneuvers"]:
            steps.append({"at": base + m["begin_shape_index"], "type": m["type"],
                          "text": m.get("verbal_pre_transition_instruction") or m["instruction"],
                          "instruction": m["instruction"],
                          "street": ", ".join(m.get("street_names", [])[:1]) or "",
                          "turn": TURNS.get(m["type"], 0)})
    cum = [0.0]
    for a, b in zip(shape, shape[1:]):
        cum.append(cum[-1] + metres(a, b))
    return {"shape": shape, "cum": cum, "steps": steps[1:] or steps,    # skip "Drive east on ..."
            "metres": cum[-1], "secs": trip["summary"]["time"]}


# ---- recent destinations ----

def recent():
    try:
        with open(RECENT) as f:
            return json.load(f)[:6]
    except (OSError, ValueError):
        return []


def remember(dest):
    keep = [d for d in recent() if (d["name"], d.get("detail")) != (dest["name"], dest.get("detail"))]
    keep.insert(0, {k: dest[k] for k in ("name", "detail", "lat", "lon")})
    try:
        os.makedirs(os.path.dirname(RECENT), exist_ok=True)
        with open(RECENT + ".tmp", "w") as f:
            json.dump(keep[:6], f, indent=1)
        os.replace(RECENT + ".tmp", RECENT)
    except OSError as e:
        log(f"can't save recent destinations: {e}")


# ---- saved places (HOME, WORK, MOM'S HOUSE...) ----

def saved_places():
    try:
        with open(SAVED) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _write_saved(rows):
    try:
        os.makedirs(os.path.dirname(SAVED), exist_ok=True)
        with open(SAVED + ".tmp", "w") as f:
            json.dump(rows, f, indent=1)
        os.replace(SAVED + ".tmp", SAVED)
    except OSError as e:
        log(f"can't save places: {e}")


def save_place(name, place):
    """Save place under name (the same name again moves it). -> the saved list."""
    name = " ".join(name.split())[:30]
    row = {"name": name, "detail": place.get("detail") or place.get("name", ""),
           "lat": place["lat"], "lon": place["lon"]}
    if place.get("name") and place.get("name") != name and place.get("detail"):
        row["detail"] = f"{place['name']}, {place['detail']}"
    rows = saved_places()
    same = [i for i, r in enumerate(rows) if r["name"].lower() == name.lower()]
    if same:                                            # moved: keeps its place in the list
        rows[same[0]] = row
    else:
        rows.append(row)
    _write_saved(rows)
    return rows


def rename_place(old, new):
    new = " ".join(new.split())[:30]
    rows = [r for r in saved_places() if r["name"] == old or r["name"].lower() != new.lower()]
    for r in rows:
        if r["name"] == old:
            r["name"] = new
    _write_saved(rows)
    return rows


def delete_place(name):
    rows = [r for r in saved_places() if r["name"] != name]
    _write_saved(rows)
    return rows


# ---- the destination picker ----

class DestinationScreen(screens.Pressable):
    """SPEAK / TYPE a destination, SAVED places, or pick a recent one; then pick from the
    matches. With on_pick it chooses a place for something else (saving one: the third
    button is then HERE, where the car is) instead of navigating."""
    BACK = (0, 0, 56, 18)
    SPEAK = (0, 21, LW // 3 - 2, 55)
    TYPE = (LW // 3 + 1, 21, 2 * LW // 3 - 2, 55)
    SAVED = (2 * LW // 3 + 1, 21, LW - 1, 55)          # HERE when choosing a place to save
    ROWS_Y0, ROW_H = 60, 16

    def __init__(self, nav, feed, wake=None, on_pick=None, title="NAVIGATE"):
        super().__init__()
        self.nav = nav
        self.feed = feed
        self.wake = wake
        self.on_pick = on_pick
        self.title = title
        self.state = "pick"                 # pick listening thinking results
        self.heard = ""
        self.results = []
        self.rows = recent()                # what the list shows: recent, or results
        self.message = "" if nav.available else "MAP DATA FOR ROUTING NOT BUILT YET"
        self.stop = threading.Event()

    def _changed(self):
        if self.wake:
            self.wake.set()

    def _near(self):
        fix = self.feed.gps or {}
        if fix:
            return fix["lat"], fix["lon"]
        track = list(getattr(self.feed, "track", []))
        return track[-1] if track else None

    def _search(self, query):
        self.state, self.heard, self.message = "thinking", query, "SEARCHING..."
        self._changed()
        if not self.nav.places.available:
            self.rows, self.state = [], "results"
            self.message = "ADDRESS SEARCH NOT READY YET (BUILDING)"
            self._changed()
            return
        found = self.nav.places.search(query, self._near())
        self.rows = found
        self.state = "results"
        self.message = "" if found else f"NOTHING FOUND FOR \"{query.upper()[:24]}\""
        self._changed()

    def _listen(self):
        try:
            rec = self.nav.voice.record(self.stop, self._changed, "nav")
            if rec is None:
                self.state, self.message = "pick", "DIDN'T HEAR ANYTHING - TRY AGAIN"
                return
            self.state, self.message = "thinking", "WORKING OUT WHAT YOU SAID..."
            self._changed()
            text_ = self.nav.voice.transcribe(rec, SPEAK_PROMPT)
            if text_:
                self._search(text_)
            if not self.rows_found() and self.nav.places.available \
                    and not self.nav.voice.uses_accurate(rec):
                # the fast model's words found nothing: listen again with the accurate one
                self.state, self.message = "thinking", "LISTENING MORE CAREFULLY..."
                self._changed()
                text_ = self.nav.voice.transcribe(rec, SPEAK_PROMPT, accurate=True) or text_
                if text_:
                    self._search(text_)
            if not text_:
                self.state, self.message = "pick", "COULDN'T MAKE THAT OUT - TRY AGAIN"
        except Exception as e:
            self.state, self.message = "pick", str(e)[:38].upper()
        finally:
            self._changed()

    def rows_found(self):
        return self.state == "results" and bool(self.rows)

    def _typed(self, value):
        if value.strip():
            threading.Thread(target=self._search, args=(value,), daemon=True).start()

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            self.stop.set()
            return "close"
        if self.state in ("listening",):
            if inside(self.SPEAK, x, y):        # tap again = done talking
                self.press("speak")
                self.stop.set()
            return None
        if self.state == "thinking":
            return None
        if inside(self.SPEAK, x, y) and self.nav.voice:
            self.press("speak")
            self.stop.clear()
            self.state, self.message, self.heard = "listening", "", ""
            threading.Thread(target=self._listen, daemon=True).start()
            return None
        if inside(self.TYPE, x, y):
            self.press("type")
            return screens.Keyboard("TYPE A DESTINATION (ADDRESS OR PLACE)" if not self.on_pick
                                    else "TYPE ITS ADDRESS (OR A PLACE NAME)", self._typed)
        if inside(self.SAVED, x, y):
            self.press("saved")
            if not self.on_pick:
                return SavedScreen(self.nav, self.feed, self.wake)
            near = self._near()
            if not near:
                self.message = "NO GPS POSITION YET"
                return None
            self.on_pick({"name": "", "detail": "where the car was", "lat": near[0], "lon": near[1]})
            return "close"
        top = self.rows_top()
        for i, row in enumerate(self.rows[:max(1, (LH - top) // self.ROW_H)]):
            y0 = top + i * self.ROW_H
            if y0 <= y < y0 + self.ROW_H:
                self.press(("row", i))
                if self.on_pick:
                    self.on_pick(row)
                    return "close"
                go_to(self.nav, self.feed, row, self._near())
                return "close"
        return None

    def rows_top(self):
        """Where the list starts (lower when "heard" or RECENT is shown above it)."""
        y = self.ROWS_Y0 + (8 if self.heard else 0)
        return y + 8 if self.state == "pick" else y

    def render(self, img, d):
        button(img, d, self.BACK, "BACK", GREEN, self.lit("back"))
        text(img, LW - 2, 6, self.title[:34], DIM, align="r")
        listening = self.state == "listening"
        speak_label = "DONE" if listening else "SPEAK"
        button(img, d, self.SPEAK, speak_label, RED if listening else AMBER, self.lit("speak"), scale=2)
        button(img, d, self.TYPE, "TYPE", GREEN, self.lit("type"), scale=2)
        button(img, d, self.SAVED, "HERE" if self.on_pick else "SAVED", GREEN, self.lit("saved"), scale=2)
        if listening:                          # mic level under SPEAK
            x0, _, x1, y1 = self.SPEAK
            lvl = self.nav.voice.level if self.nav.voice else 0
            d.rectangle([x0 + 3, y1 - 5, x0 + 3 + int((x1 - x0 - 6) * lvl), y1 - 3], fill=RED)
        y = self.ROWS_Y0
        if listening:
            text(img, LW // 2, y + 10, "SAY WHERE TO" if blink() else "", AMBER, align="c")
            text(img, LW // 2, y + 24, "e.g. \"2300 SE Hawthorne Blvd\"", DIM, align="c", upper=False)
            text(img, LW // 2, y + 34, "or \"nearest gas station\"", DIM, align="c", upper=False)
            return
        if self.heard:
            text(img, 2, y - 3, f"\"{self.heard}\""[:44], DIM, upper=False)
            y += 8
        if self.message:
            text(img, LW // 2, y + 10, self.message[:44], RED if "NOTHING" in self.message or "DIDN" in self.message
                 or "COULDN" in self.message or "NOT BUILT" in self.message else DIM, align="c")
            if self.state != "results":
                return
        if not self.rows:
            if self.state == "pick" and not self.message:
                text(img, LW // 2, y + 10, "NO RECENT DESTINATIONS", FAINT, align="c")
            return
        if self.state == "pick":
            text(img, 2, y - 1, "RECENT", FAINT)
        y = self.rows_top()
        near = self._near()
        for i, row in enumerate(self.rows[:max(1, (LH - y) // self.ROW_H)]):
            ry = y + i * self.ROW_H
            on = self.lit(("row", i))
            if on:
                d.rectangle([0, ry, LW - 1, ry + self.ROW_H - 2], fill=DIM)
            km = places.distance_m(near[0], near[1], row["lat"], row["lon"]) / 1000 if near else None
            dist = nice_distance(km * 1000) if km is not None else ""
            text(img, 2, ry + 1, row["name"][:34], AMBER, upper=False)
            text(img, LW - 2, ry + 1, dist, GREEN, align="r", upper=False)
            text(img, 6, ry + 8, (row.get("detail") or "")[:42], DIM, upper=False)
            d.line([0, ry + self.ROW_H - 1, LW, ry + self.ROW_H - 1], fill=FAINT)


def go_to(nav, feed, place, near):
    """Start navigating to place from the car's fix (or its last known spot)."""
    fix = feed.gps or ({"lat": near[0], "lon": near[1]} if near else {})
    nav.go(place, fix)


class SavedScreen(screens.Pressable):
    """GO > SAVED: Ian's named places. Tap one = navigate there; its EDIT button (right) =
    rename / move / delete it. ADD = name a place, then say, type or pick where it is
    (or HERE). Saving under the same name again moves that place."""
    BACK = (0, 0, 56, 18)
    ADD = (0, 21, LW // 2 - 2, 39)
    MORE = (LW // 2 + 1, 21, LW - 1, 39)              # next page, when they don't all fit
    ROWS_Y0, ROW_H = 43, 23
    PER_PAGE = (LH - 43) // 23
    EDIT_W = 46                                        # the EDIT button at the right of each row

    def __init__(self, nav, feed, wake=None):
        super().__init__()
        self.nav, self.feed, self.wake = nav, feed, wake
        self.rows = saved_places()
        self.page = 0

    def _near(self):
        fix = self.feed.gps or {}
        if fix:
            return fix["lat"], fix["lon"]
        track = list(getattr(self.feed, "track", []))
        return track[-1] if track else None

    def reload(self, rows=None):
        self.rows = saved_places() if rows is None else rows
        self.page = min(self.page, max(0, (len(self.rows) - 1) // self.PER_PAGE))

    def _named(self, name):
        name = " ".join(name.split())
        if not name:
            return None

        def picked(place):
            self.reload(save_place(name, place))
            i = next((n for n, r in enumerate(self.rows) if r["name"].lower() == name.lower()), 0)
            self.page = i // self.PER_PAGE                                 # show it
        return DestinationScreen(self.nav, self.feed, self.wake, on_pick=picked,
                                 title=f"WHERE IS {name.upper()}?")

    def shown(self):
        return self.rows[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]

    def edit_rect(self, i):
        y0 = self.ROWS_Y0 + i * self.ROW_H
        return (LW - self.EDIT_W, y0 + 2, LW - 1, y0 + self.ROW_H - 3)

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        if inside(self.ADD, x, y):
            self.press("add")
            return screens.Keyboard("NAME THIS PLACE (E.G. HOME, WORK, MOM'S HOUSE)", self._named)
        if inside(self.MORE, x, y) and len(self.rows) > self.PER_PAGE:
            self.press("more")
            self.page = (self.page + 1) % ((len(self.rows) + self.PER_PAGE - 1) // self.PER_PAGE)
            return None
        for i, row in enumerate(self.shown()):
            y0 = self.ROWS_Y0 + i * self.ROW_H
            if not y0 <= y < y0 + self.ROW_H:
                continue
            if x >= LW - self.EDIT_W - 4:                   # EDIT (a little extra room for a finger)
                self.press(("edit", i))
                return PlaceScreen(self, row)
            self.press(("row", i))
            go_to(self.nav, self.feed, row, self._near())
            return "done"                                   # straight back to the map
        return None

    def render(self, img, d):
        button(img, d, self.BACK, "BACK", GREEN, self.lit("back"))
        text(img, LW - 2, 6, "SAVED PLACES", DIM, align="r")
        button(img, d, self.ADD, "ADD", AMBER, self.lit("add"))
        if len(self.rows) > self.PER_PAGE:
            pages = (len(self.rows) + self.PER_PAGE - 1) // self.PER_PAGE
            button(img, d, self.MORE, f"MORE {self.page + 1}/{pages}", GREEN, self.lit("more"))
        if not self.rows:
            text(img, LW // 2, 70, "NO SAVED PLACES YET", FAINT, align="c")
            text(img, LW // 2, 84, "TAP ADD TO SAVE HOME, WORK...", DIM, align="c")
            return
        near = self._near()
        for i, row in enumerate(self.shown()):
            ry = self.ROWS_Y0 + i * self.ROW_H
            if self.lit(("row", i)):
                d.rectangle([0, ry, LW - self.EDIT_W - 3, ry + self.ROW_H - 2], fill=DIM)
            text(img, 2, ry + 3, row["name"][:24], AMBER, upper=False)
            if near:
                m = places.distance_m(near[0], near[1], row["lat"], row["lon"])
                text(img, LW - self.EDIT_W - 5, ry + 3, nice_distance(m), GREEN, align="r", upper=False)
            text(img, 6, ry + 13, (row.get("detail") or "")[:34], DIM, upper=False)
            button(img, d, self.edit_rect(i), "EDIT", GREEN, self.lit(("edit", i)))
            d.line([0, ry + self.ROW_H - 1, LW, ry + self.ROW_H - 1], fill=FAINT)


class PlaceScreen(screens.Pressable):
    """One saved place: RENAME, MOVE (choose where it is again), DELETE (tap twice)."""
    BACK = (0, 0, 56, 18)
    RENAME = (0, 62, LW // 3 - 2, 96)
    MOVE = (LW // 3 + 1, 62, 2 * LW // 3 - 2, 96)
    DELETE = (2 * LW // 3 + 1, 62, LW - 1, 96)
    CONFIRM_SECS = 3

    def __init__(self, saved, row):
        super().__init__()
        self.saved, self.row = saved, row
        self.armed = 0.0                    # monotonic time of DELETE's first tap

    def _renamed(self, new):
        new = " ".join(new.split())
        if new and new != self.row["name"]:
            self.saved.reload(rename_place(self.row["name"], new))
            self.row = dict(self.row, name=new)

    def _moved(self, place):
        self.saved.reload(save_place(self.row["name"], place))
        self.row = next((r for r in self.saved.rows if r["name"] == self.row["name"]), self.row)

    def tap(self, x, y):
        if inside(self.BACK, x, y):
            self.press("back")
            return "close"
        if inside(self.RENAME, x, y):
            self.press("rename")
            kb = screens.Keyboard(f"NEW NAME FOR {self.row['name'].upper()}", self._renamed)
            kb.value = self.row["name"]
            return kb
        if inside(self.MOVE, x, y):
            self.press("move")
            s = self.saved
            return DestinationScreen(s.nav, s.feed, s.wake, on_pick=self._moved,
                                     title=f"WHERE IS {self.row['name'].upper()}?")
        if inside(self.DELETE, x, y):
            self.press("delete")
            if time.monotonic() - self.armed < self.CONFIRM_SECS:
                self.saved.reload(delete_place(self.row["name"]))
                return "close"
            self.armed = time.monotonic()
        return None

    def render(self, img, d):
        button(img, d, self.BACK, "BACK", GREEN, self.lit("back"))
        text(img, LW - 2, 6, "EDIT PLACE", DIM, align="r")
        text(img, 4, 26, self.row["name"][:20], AMBER, scale=2, upper=False)
        text(img, 4, 46, (self.row.get("detail") or "")[:44], DIM, upper=False)
        button(img, d, self.RENAME, "RENAME", GREEN, self.lit("rename"))
        button(img, d, self.MOVE, "MOVE", GREEN, self.lit("move"))
        armed = time.monotonic() - self.armed < self.CONFIRM_SECS
        button(img, d, self.DELETE, "SURE?" if armed else "DELETE", RED, armed or self.lit("delete"))
        if armed:
            text(img, LW // 2, 108, "TAP DELETE AGAIN TO DELETE IT", RED, align="c")
        else:
            text(img, LW // 2, 108, "MOVE = SAY, TYPE OR PICK WHERE IT IS (OR HERE)", FAINT, align="c")
