"""Navigation (routes, guidance, picker, places search) and engine-noise speech detection."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nav  # noqa: E402
import places  # noqa: E402
import voice  # noqa: E402

RATE = voice.RATE


def encode_polyline(coords, precision=6):
    out, plat, plon = [], 0, 0
    f = 10 ** precision
    for lat, lon in coords:
        for v, prev in ((round(lat * f), plat), (round(lon * f), plon)):
            d = v - prev
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1f)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        plat, plon = round(lat * f), round(lon * f)
    return "".join(out)


# an L-shaped route: 1 km north along 39th, then right (east) 500 m on Hawthorne
START = (45.5000, -122.6225)
CORNER = (45.5090, -122.6225)
END = (45.5090, -122.6161)
SHAPE = [START] + [(START[0] + i * 0.0009, START[1]) for i in range(1, 11)] + \
        [(CORNER[0], CORNER[1] + i * 0.00064) for i in range(1, 11)]
REPLY = {"trip": {"summary": {"time": 120, "length": 0.93}, "legs": [{
    "shape": encode_polyline(SHAPE),
    "maneuvers": [
        {"type": 1, "instruction": "Drive north on SE 39th Ave.", "begin_shape_index": 0, "street_names": ["SE 39th Ave"]},
        {"type": 10, "instruction": "Turn right onto SE Hawthorne Blvd.", "begin_shape_index": 10,
         "verbal_pre_transition_instruction": "Turn right onto Southeast Hawthorne Boulevard.",
         "street_names": ["SE Hawthorne Blvd"]},
        {"type": 4, "instruction": "You have arrived at your destination.", "begin_shape_index": 20}]}]}}


class FakeActor:
    def __init__(self):
        self.requests = []

    def route(self, req):
        self.requests.append(json.loads(req))
        return json.dumps(REPLY)


class FakePlaces:
    available = True

    def search(self, text, near=None, limit=8):
        return [{"name": "2300 SE Hawthorne Blvd", "detail": "Portland 97214", "lat": END[0], "lon": END[1]}]


def fix(lat, lon, kmh=40, course=0):
    return {"lat": lat, "lon": lon, "speed_kmh": kmh, "course_deg": course}


class NavTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        nav.RECENT = os.path.join(self.tmp.name, "dest.json")
        self.n = nav.Navigator(finder=FakePlaces())
        self.n.actor = FakeActor()

    def tearDown(self):
        self.tmp.cleanup()

    def go(self):
        self.n.go({"name": "2300 SE Hawthorne Blvd", "detail": "", "lat": END[0], "lon": END[1]}, fix(*START))
        for _ in range(100):
            if self.n.route:
                break
            threading.Event().wait(0.01)
        self.assertIsNotNone(self.n.route)

    def test_polyline_roundtrip(self):
        back = nav.decode_polyline(encode_polyline(SHAPE))
        self.assertEqual(len(back), len(SHAPE))
        self.assertAlmostEqual(back[-1][1], END[1], places=6)

    def test_route_and_guidance(self):
        self.go()
        r = self.n.route
        self.assertAlmostEqual(r["metres"], 1001 + 500, delta=20)
        self.assertEqual([s["turn"] for s in r["steps"]], [90, "arrive"])     # "Drive north" dropped
        req = self.n.actor.requests[0]
        self.assertEqual(req["locations"][0]["heading"], 0)                   # no U-turn routing
        self.n.update(fix(START[0] + 0.0045, START[1]))                       # half way up 39th
        p = self.n.progress
        self.assertEqual(p["step"]["street"], "SE Hawthorne Blvd")
        self.assertAlmostEqual(p["to_step"], 500, delta=60)
        self.assertAlmostEqual(p["left_m"], 1000, delta=60)
        self.n.update(fix(CORNER[0], CORNER[1] + 0.0032, course=90))          # on Hawthorne
        self.assertEqual(self.n.progress["step"]["turn"], "arrive")
        self.assertEqual(nav.recent()[0]["name"], "2300 SE Hawthorne Blvd")

    def test_reroute_when_off_route(self):
        self.go()
        self.n.last_route_at = 0
        off = fix(START[0] + 0.0045, START[1] + 0.003)                      # ~230 m east of 39th
        for _ in range(nav.OFF_ROUTE_FIXES):
            self.n.update(off)
        for _ in range(100):
            if len(self.n.actor.requests) > 1:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(self.n.actor.requests), 2)

    def test_reroute_near_the_end_doesnt_crash(self):
        """2026-10-17: rerouting near home kept the place on the old, longer route; the
        new short route had no such point and update() crashed the display."""
        self.go()
        self.n.progress = {"index": len(self.n.route["shape"]) + 400}      # from a much longer route
        self.n.update(fix(START[0] + 0.0045, START[1]))                       # must not raise
        self.assertIn("step", self.n.progress)
        self.n.progress = {"index": 999}
        self.n.last_route_at = 0
        self.n._route_async(fix(*START), "REROUTING...")
        for _ in range(100):
            if self.n.progress is None:
                break
            threading.Event().wait(0.01)
        self.assertIsNone(self.n.progress)                                   # a new route starts afresh

    def test_arrive_and_stop(self):
        self.go()
        self.n.update(fix(*END, kmh=0))
        self.assertTrue(self.n.progress["arrived"])
        self.n.stop()
        self.assertFalse(self.n.active)

    def test_nice_distance(self):
        self.assertEqual(nav.nice_distance(100), "350 ft")
        self.assertEqual(nav.nice_distance(1609.344 * 2.34), "2.3 mi")
        self.assertEqual(nav.nice_distance(1234, mph=False), "1.2 km")

    def test_picker_type_and_choose(self):
        class Feed:
            gps = fix(*START)
            track = []
        s = nav.DestinationScreen(self.n, Feed())
        kb = s.tap(*nav.DestinationScreen.TYPE[:2])
        self.assertEqual(type(kb).__name__, "Keyboard")
        s._search("2300 se hawthorne")                                        # what OK does (in a thread)
        self.assertEqual(s.state, "results")
        top = s.rows_top()
        self.assertEqual(s.tap(10, top + 3), "close")                          # first result
        self.assertTrue(self.n.active)

    def test_screens_render(self):
        from PIL import Image, ImageDraw
        from pixels import BG, LH, LW
        import mappage

        class Feed:
            gps = fix(*START)
            track = [START]
        s = nav.DestinationScreen(self.n, Feed())
        for state in ("pick", "listening", "thinking", "results"):
            s.state = state
            img = Image.new("RGB", (LW, LH), BG)
            s.render(img, ImageDraw.Draw(img))
        view = mappage.MapView(roads=type("R", (), {"available": False, "near": lambda *a: []})())
        view.nav = self.n
        for step in ("idle", "routing", "guiding", "arrived"):
            if step == "routing":
                self.n.go({"name": "x", "detail": "", "lat": END[0], "lon": END[1]}, {})   # no fix yet
            if step == "guiding":
                self.go()
                self.n.update(fix(START[0] + 0.0045, START[1]))
            if step == "arrived":
                self.n.update(fix(*END, kmh=0))
            img = Image.new("RGB", (LW, LH), BG)
            view.draw(img, ImageDraw.Draw(img), fix(*START), [START])
        self.assertEqual(view.tap(*mappage.END_BTN[:2]), "end")
        self.assertFalse(self.n.active)


class SavedPlacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, path in (("SAVED", "saved.json"), ("RECENT", "recent.json")):
            p = mock.patch.object(nav, name, os.path.join(self.tmp.name, path))
            p.start()
            self.addCleanup(p.stop)

        class Nav:
            available, voice = True, None
            places = type("P", (), {"available": False})()

            def __init__(self):
                self.went = []

            def go(self, dest, fix):
                self.went.append(dest)

        class Feed:
            gps = fix(*START)
            track = []
        self.nav, self.feed = Nav(), Feed()

    def test_add_home_here_then_go_there(self):
        go = nav.DestinationScreen(self.nav, self.feed)
        saved = go.tap(*nav.DestinationScreen.SAVED[:2])
        self.assertIsInstance(saved, nav.SavedScreen)
        kb = saved.tap(*nav.SavedScreen.ADD[:2])
        kb.value = "Home"
        r = kb.tap(*[rect for rect, k in kb.keys() if k == "OK"][0][:2])
        self.assertEqual(r[0], "replace")                       # naming hands over to "where is it?"
        where = r[1]
        self.assertEqual(where.title, "WHERE IS HOME?")
        self.assertEqual(where.tap(*nav.DestinationScreen.SAVED[:2]), "close")     # HERE
        self.assertEqual([p["name"] for p in nav.saved_places()], ["Home"])
        self.assertEqual(saved.rows[0]["lat"], START[0])
        self.assertEqual(saved.tap(10, nav.SavedScreen.ROWS_Y0 + 3), "done")
        self.assertEqual(self.nav.went[0]["name"], "Home")

    def test_same_name_moves_it_in_place(self):
        nav.save_place("Work", {"name": "A", "detail": "x", "lat": 1, "lon": 2})
        nav.save_place("Mom's House", {"name": "B", "detail": "y", "lat": 3, "lon": 4})
        nav.save_place("work", {"name": "C", "detail": "z", "lat": 5, "lon": 6})
        self.assertEqual([(p["name"], p["lat"]) for p in nav.saved_places()], [("work", 5), ("Mom's House", 3)])

    def test_edit_button_rename_move_delete(self):
        nav.save_place("Work", {"name": "", "detail": "x", "lat": 1, "lon": 2})
        nav.save_place("Home", {"name": "", "detail": "y", "lat": 3, "lon": 4})
        s = nav.SavedScreen(self.nav, self.feed)
        edit = s.tap(nav.LW - 10, nav.SavedScreen.ROWS_Y0 + 5)          # EDIT on Work's row
        self.assertIsInstance(edit, nav.PlaceScreen)
        self.assertEqual(self.nav.went, [])                              # EDIT doesn't navigate
        kb = edit.tap(*nav.PlaceScreen.RENAME[:2])
        self.assertEqual(kb.value, "Work")
        kb.value = "Office"
        kb.tap(*[rect for rect, k in kb.keys() if k == "OK"][0][:2])
        self.assertEqual([p["name"] for p in s.rows], ["Office", "Home"])
        where = edit.tap(*nav.PlaceScreen.MOVE[:2])
        where.tap(*nav.DestinationScreen.SAVED[:2])                      # HERE
        self.assertEqual(nav.saved_places()[0]["lat"], START[0])
        self.assertIsNone(edit.tap(*nav.PlaceScreen.DELETE[:2]))          # first tap asks
        self.assertEqual(len(nav.saved_places()), 2)
        self.assertEqual(edit.tap(*nav.PlaceScreen.DELETE[:2]), "close")
        self.assertEqual([p["name"] for p in s.rows], ["Home"])

    def test_picked_search_result_is_saved_with_its_address(self):
        rows = nav.save_place("Mom's House", {"name": "1234 SE Main St", "detail": "Portland 97214",
                                              "lat": 45.5, "lon": -122.6})
        self.assertEqual(rows[0]["detail"], "1234 SE Main St, Portland 97214")

    def test_renders(self):
        from PIL import Image, ImageDraw
        from pixels import BG, LH, LW
        for i in range(12):
            nav.save_place(f"Place {i}", {"name": "", "detail": "d", "lat": 45.5, "lon": -122.6})
        s = nav.SavedScreen(self.nav, self.feed)
        for screen in (s, nav.PlaceScreen(s, s.rows[0])):
            img = Image.new("RGB", (LW, LH), BG)
            screen.render(img, ImageDraw.Draw(img))
        s.tap(*nav.SavedScreen.MORE[:2])
        self.assertEqual(s.page, 1)


class PlacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "places.db")
        db = sqlite3.connect(path)
        db.executescript(places.SCHEMA)
        rows = [(0, "2300 Southeast Hawthorne Boulevard", "Portland 97214", 45.5122, -122.6421),
                (1, "Powell's City of Books", "books 1005 West Burnside Street Portland", 45.5231, -122.6814),
                (1, "Costco", "wholesale Portland", 45.4500, -122.5800),
                (1, "Costco", "wholesale Seattle", 47.5800, -122.3300),
                (3, "Southeast Hawthorne Boulevard", "street", 45.5120, -122.6300),
                (2, "Seattle", "city", 47.6062, -122.3321),
                (0, "4809 Northeast 107th Avenue", "Portland 97220", 45.5580, -122.5530),
                (0, "4901 Northeast 107th Avenue", "Portland 97220", 45.5600, -122.5530),
                (0, "4810 Northeast 107th Avenue", "Portland 97220", 45.5581, -122.5527),
                (0, "4830 Northeast 107th Avenue", "Portland 97220", 45.5585, -122.5527),
                (0, "4814 Northeast 108th Avenue", "Portland 97220", 45.5582, -122.5510),
                # a known house number, but OSM also has houses on the street with no town,
                # which used to give a guess from 2510 (at Division) ranked above it
                (0, "2300 Southeast 39th Avenue", "Portland 97214", 45.50700, -122.62240),
                (0, "2510 Southeast 39th Avenue", "", 45.50463, -122.62224),
                (0, "2520 Southeast 39th Avenue", "", 45.50440, -122.62224),
                (1, "Stumptown", "cafe 3356 Southeast Belmont Street Portland", 45.5163, -122.6302),
                (1, "Shell", "fuel", 45.5100, -122.6300),
                (1, "Costco Pharmacy", "pharmacy", 45.4501, -122.5801),
                (2, "Eugene", "city", 44.0505, -123.0951),
                (3, "Northeast Eugene Street", "street", 45.5400, -122.6300)]
        for i, (kind, name, detail, lat, lon) in enumerate(rows, 1):
            db.execute("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?)", (i, kind, name, detail, lat, lon))
            db.execute("INSERT INTO places_fts (rowid, words) VALUES (?, ?)", (i, " ".join(places.words(f"{name} {detail}"))))
        db.commit()
        db.close()
        self.p = places.Places(path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_search(self):
        here = (45.5120, -122.6350)
        self.assertEqual(self.p.search("2300 SE Hawthorne Blvd", here)[0]["name"], "2300 Southeast Hawthorne Boulevard")
        self.assertEqual(self.p.search("Navigate to 2300 southeast Hawthorne Boulevard.", here)[0]["name"],
                         "2300 Southeast Hawthorne Boulevard")
        self.assertEqual(self.p.search("powells", here)[0]["name"], "Powell's City of Books")
        costco = self.p.search("take me to the nearest Costco", here)
        self.assertEqual(costco[0]["detail"], "wholesale Portland")
        self.assertEqual(self.p.search("seattle", here)[0]["name"], "Seattle")
        self.assertEqual(self.p.search("zzzz nothing", here), [])

    def test_ranking(self):
        here = (45.5120, -122.6350)
        self.assertEqual(self.p.search("nearest gas station", here)[0]["name"], "Shell")      # gas -> fuel
        self.assertEqual(self.p.search("coffee", here)[0]["name"], "Stumptown")               # by category
        self.assertEqual(self.p.search("Eugene", here)[0]["detail"], "city")                  # the town, far away
        self.assertEqual(self.p.search("Seattle, Washington", here)[0]["name"], "Seattle")    # state dropped
        costco = self.p.search("costco", here)
        self.assertEqual((costco[0]["name"], costco[0]["detail"]), ("Costco", "wholesale Portland"))  # not the pharmacy

    def test_missing_house_number_is_estimated(self):
        here = (45.5120, -122.6350)
        r = self.p.search("4814 NE 107th", here)[0]
        self.assertEqual(r["name"], "4814 Northeast 107th Avenue")
        self.assertIn("approx", r["detail"])
        # even numbers are one side of the street: between 4810 and 4830, a fifth of the way
        self.assertAlmostEqual(r["lat"], 45.5581 + 0.2 * 0.0004, places=5)
        self.assertAlmostEqual(r["lon"], -122.5527, places=5)
        self.assertEqual(self.p.search("4814 NE 108th Ave", here)[0]["detail"], "Portland 97220")   # exact wins
        self.assertNotIn("approx", " ".join(x["detail"] for x in self.p.search("9999 NE 107th", here)))

    def test_known_address_beats_a_guess(self):
        found = self.p.search("2300 SE 39th", (45.5165, -122.6600))       # asked from across town
        self.assertEqual((found[0]["lat"], found[0]["detail"]), (45.50700, "Portland 97214"))
        self.assertFalse(any("approx" in r["detail"] and r["lat"] < 45.506 for r in found))    # no Division guess


def engine_noise(secs, level=4500, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(int(secs * RATE)) / RATE
    x = sum(np.sin(2 * np.pi * 73 * k * t + k) / k for k in range(1, 12))          # 2200 rpm firing
    x = x / np.std(x) + rng.normal(size=len(t)) * 0.3
    x += sum(0.25 / k * np.sin(2 * np.pi * 550 * k * t) for k in range(1, 6))      # alternator whine
    return x / np.sqrt(np.mean(x ** 2)) * level


def vowels(secs, level):
    """Speech-like: a voiced buzz (140 Hz harmonics) through changing formants, in syllables."""
    t = np.arange(int(secs * RATE)) / RATE
    x = np.zeros_like(t)
    for f1, f2, start in ((700, 1200, 0.0), (300, 2300, 0.35), (500, 1500, 0.7), (650, 1000, 1.05)):
        m = (t >= start) & (t < start + 0.28)
        for k in range(1, 30):
            f = 140 * k
            w = np.exp(-((f - f1) / 150) ** 2) + 0.6 * np.exp(-((f - f2) / 200) ** 2)
            x[m] += w * np.sin(2 * np.pi * f * t[m])
    return x / np.sqrt(np.mean(x[x != 0] ** 2)) * level


def detect(pcm):
    det, recent, started = voice.SpeechDetector(), [], False
    for i in range(0, len(pcm) - voice.CHUNK + 1, voice.CHUNK):
        a = pcm[i:i + voice.CHUNK]
        b = det.feed(a)
        if b is None:
            continue
        recent = (recent + [b >= voice.START_BANDS])[-voice.START_WINDOW:]
        started = started or sum(recent) >= voice.START_CHUNKS
        if b < voice.KEEP_BANDS:
            det.quiet(a)
    return started


class VoiceTest(unittest.TestCase):
    def scene(self, noise_level, speech_level):
        n = engine_noise(4.0, noise_level)
        s = vowels(1.4, speech_level)
        n[RATE:RATE + len(s)] += s
        return np.clip(n, -32767, 32767).astype(np.int16)

    def test_hears_speech_over_engine(self):
        self.assertTrue(detect(self.scene(4500, 4500)))           # as loud as the engine
        self.assertTrue(detect(self.scene(4500, 2500)))           # quieter than it (-5 dB)

    def test_ignores_engine_alone(self):
        self.assertFalse(detect(engine_noise(6.0, 4500).astype(np.int16)))
        self.assertFalse(detect(engine_noise(6.0, 170).astype(np.int16)))

    def test_hears_speech_parked(self):
        self.assertTrue(detect(self.scene(170, 3000)))

    def test_hallucinations(self):
        self.assertTrue(voice.hallucination("Thank you.", None))
        self.assertFalse(voice.hallucination("Take me to Costco", None))


class CleanQueryTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(places.clean_query("Hey Claude, navigate to the nearest Costco please."), ("Costco", True))
        self.assertEqual(places.clean_query("Directions to 2300 SE Hawthorne Blvd"), ("2300 SE Hawthorne Blvd", False))
        self.assertEqual(places.words("Southeast 12th Avenue"), places.words("SE 12th Ave"))


if __name__ == "__main__":
    unittest.main()
