"""MAP page: roads database queries and drawing (no GPS or OSM data needed)."""
import array
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mappage  # noqa: E402
import roadmap  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pixels import BG, LH, LW  # noqa: E402

HOME = (45.5189, -122.6793)


def make_db(path):
    """A '+' of two named streets crossing at HOME, plus a motorway to the east."""
    db = sqlite3.connect(path)
    db.executescript(roadmap.SCHEMA)
    lat, lon = HOME
    roads = [(1, 5, "SE Main St", [(lat, lon - 0.005), (lat, lon + 0.005)]),
             (2, 3, "SE 20th Ave", [(lat - 0.005, lon), (lat + 0.005, lon)]),
             (3, 0, "I 5", [(lat - 0.01, lon + 0.004), (lat + 0.01, lon + 0.004)])]
    for rid, cls, name, pts in roads:
        ints = [round(v * roadmap.SCALE) for p in pts for v in p]
        db.execute("INSERT INTO roads VALUES (?, ?, ?, ?)", (rid, cls, name, array.array("i", ints).tobytes()))
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        db.execute("INSERT INTO roads_idx VALUES (?, ?, ?, ?, ?)", (rid, min(lats), max(lats), min(lons), max(lons)))
    db.commit()
    db.close()


class MapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "roads.db")
        make_db(self.path)
        self.roads = roadmap.Roads(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_query_and_name(self):
        found = self.roads.near(*HOME, 100)
        self.assertEqual({r[1] for r in found}, {"SE Main St", "SE 20th Ave"})     # I 5 is ~310 m east
        self.assertEqual(len(self.roads.near(*HOME, 500)), 3)
        self.assertEqual(len(self.roads.near(*HOME, 500, max_cls=3)), 2)           # no residential
        # heading north: on 20th Ave; heading east: on Main St
        self.assertEqual(roadmap.nearest_name(found, *HOME, course=0), "SE 20th Ave")
        self.assertEqual(roadmap.nearest_name(found, *HOME, course=90), "SE Main St")
        self.assertIsNone(roadmap.nearest_name(found, HOME[0] + 0.003, HOME[1] + 0.003))

    def test_label(self):
        self.assertEqual(roadmap.label({"highway": "motorway", "ref": "I 5;US 30", "name": "Pacific Hwy"}), "I 5")
        self.assertEqual(roadmap.label({"highway": "primary", "ref": "OR 99E", "name": "SE McLoughlin Blvd"}), "SE McLoughlin Blvd")
        self.assertEqual(roadmap.label({"highway": "trunk", "ref": "US 26"}), "US 26")

    def draw(self, view, fix, track=()):
        img = Image.new("RGB", (LW, LH), BG)
        view.draw(img, ImageDraw.Draw(img), fix, list(track))
        return img

    def test_draws_roads_track_and_car(self):
        view = mappage.MapView(self.roads)
        fix = {"lat": HOME[0], "lon": HOME[1], "speed_kmh": 30, "course_deg": 0}
        self.draw(view, fix)
        for _ in range(100):                     # roads load in a thread
            if view.cache:
                break
            time.sleep(0.02)
        img = self.draw(view, fix, [(HOME[0] - 0.001, HOME[1]), HOME])
        px = img.load()
        car = px[LW // 2, mappage.CAR_Y]
        self.assertEqual(car, mappage.AMBER)
        lit = sum(1 for x in range(LW) for y in range(mappage.TOP + 12, LH - 12) if px[x, y] != BG)
        self.assertGreater(lit, 300)              # roads drawn
        self.assertEqual(view.road_name, "SE 20th Ave")

    def test_zoom_and_scale(self):
        view = mappage.MapView(self.roads)
        self.assertEqual(view.width_for(0), 600)
        self.assertEqual(view.width_for(120), 4800)
        view.width = 600
        view.tap(*mappage.ZOOM_IN[:2])
        self.assertEqual(view.width_for(120), 300)                 # by hand now, whatever the speed
        view.tap(*mappage.ZOOM_AUTO[:2])
        self.assertIsNone(view.manual_width)
        self.assertEqual(mappage.scale_bar(600, mph=True)[1], "500 ft")
        self.assertEqual(mappage.scale_bar(4800, mph=True)[1], "0.5 mi")
        self.assertEqual(mappage.scale_bar(4800, mph=False)[1], "1 km")

    def test_smooth_auto_zoom(self):
        w = mappage.auto_width
        self.assertAlmostEqual(w(50), 1200)
        self.assertLess(abs(w(64) / w(66) - 1), 0.1)                # no jump where 1200 -> 2400 used to be
        self.assertTrue(w(30) < w(60) < w(90) < w(110))
        view = mappage.MapView(self.roads)
        t = 100.0
        def run(kmh, secs):
            nonlocal t
            for _ in range(int(secs * 15)):                        # 15 fps
                t += 1 / 15
                view.width = view.eased_width(kmh, t)
            return view.width
        self.assertEqual(run(0, 1), 600)
        view.width = start = run(60, 1)                             # sped up: gliding out, not there yet
        self.assertTrue(600 < start < w(60))
        self.assertAlmostEqual(run(60, 10), w(60), delta=1)         # settles
        settled = view.width
        for kmh in (57, 63, 58, 62):                               # speed wobbling: map holds still
            self.assertEqual(run(kmh, 0.5), settled)
        self.assertGreater(run(110, 10), 4000)                      # a real change still zooms
        view.manual_width = 300
        self.assertEqual(view.eased_width(110, t + 0.1), 300)       # by hand: exact, no glide

    def test_streets_fade_rather_than_vanish(self):
        # zooming out one step at a time never drops residential and tertiary together
        widths = [300 * 2 ** i for i in range(8)]
        classes = [mappage.max_class(w) for w in widths]
        self.assertEqual(classes[:4], [5, 5, 5, 5])                 # to 2.4 km: every street
        self.assertTrue(all(a - b <= 1 for a, b in zip(classes, classes[1:])))
        self.assertEqual(mappage.style(5, 600)[0], mappage.DIM)
        self.assertEqual(mappage.style(5, 2400)[0], mappage.FAINT)

    def test_drag_and_pinch(self):
        view = mappage.MapView(self.roads)
        view.width, view.heading = 1330, 0                          # 5 m per drawing pixel, north up
        view.gesture([(100, 100)])
        view.gesture([(120, 90)])                                   # drag right 20 px, up 10 px
        self.assertAlmostEqual(view.pan[0], -100)                   # view moved west 100 m ...
        self.assertAlmostEqual(view.pan[1], -50)                    # ... and south 50 m
        self.assertTrue(view.panned)
        view.gesture([])
        view.tap(*mappage.ME_BTN[:2])
        self.assertFalse(view.panned)
        # pinch: fingers twice as far apart = half as wide, around their midpoint
        cx, cy = mappage.LW / 2, mappage.CAR_Y
        view.gesture([(cx - 10, cy), (cx + 10, cy)])
        view.gesture([(cx - 20, cy), (cx + 20, cy)])
        self.assertAlmostEqual(view.width, 665)
        self.assertEqual(view.pan, (0.0, 0.0))                      # midpoint on the car: stays put
        view.gesture([(cx - 20, cy), (cx + 20, cy)])
        view.gesture([(cx - 10, cy), (cx + 10, cy)])                # pinch back in
        self.assertAlmostEqual(view.width, 1330)
        for _ in range(30):
            view.gesture([(cx - 40, cy), (cx + 40, cy)])
            view.gesture([(cx - 1, cy), (cx + 1, cy)])              # way out: capped
        self.assertLessEqual(view.width, mappage.MAX_WIDTH)

    def test_drawn_when_dragged_and_zoomed_out(self):
        view = mappage.MapView(self.roads)
        fix = {"lat": HOME[0], "lon": HOME[1], "speed_kmh": 0, "course_deg": 0}
        view.width = 600
        view.zoom(2)                                                # 2.4 km: streets still there
        self.draw(view, fix)
        for _ in range(100):
            if view.cache and view.cache["max_cls"] == 5:
                break
            time.sleep(0.02)
        img = self.draw(view, fix)
        self.assertEqual(view.cache["max_cls"], 5)
        px = img.load()
        self.assertTrue(any(px[x, y] == mappage.FAINT for x in range(LW) for y in range(mappage.TOP + 12, LH - 12)))

    def test_no_gps_no_data(self):
        view = mappage.MapView(roadmap.Roads(os.path.join(self.tmp.name, "missing.db")))
        self.draw(view, {})                                          # waiting for GPS
        self.draw(view, {"lat": HOME[0], "lon": HOME[1], "speed_kmh": 0})   # no map data


if __name__ == "__main__":
    unittest.main()
