"""Fuel / MPG estimate (fuel.py) and the GAS screen (gas.py)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fuel  # noqa: E402
import gas  # noqa: E402

IDLE = {"rpm": 800, "airflow_cfm": 8.0, "baro_kpa": 102.3, "intake_c": 39.0, "injector_ms": 2.4,
        "speed_kmh": 0.0}


def drive(f, session, secs, data, t0=1000.0, hz=10):
    for i in range(int(secs * hz) + 1):
        f.add({"ts": t0 + i / hz, "session": session, "data": data})
    return t0 + secs


class FuelTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "fuel.json")
        self.f = fuel.Fuel(self.path, log=lambda m: None)

    def tearDown(self):
        self.dir.cleanup()

    def test_idle_burns_a_believable_amount(self):
        end = drive(self.f, 1, 60, IDLE)
        s = self.f.summary(end)
        self.assertIsNone(s["now_mpg"])
        self.assertGreater(s["now_gph"], 0.25)          # a 2.5 L four idles at ~0.3-0.5 gal/h
        self.assertLess(s["now_gph"], 0.5)

    def test_cfm_preferred_over_the_high_gs_reading(self):
        self.assertAlmostEqual(fuel.air_gs(dict(IDLE, airflow_gs=8.2)), 4.27, delta=0.05)
        self.assertEqual(fuel.air_gs({"airflow_gs": 5.0}), 5.0)
        self.assertIsNone(fuel.air_gs({}))

    def test_cruise_mpg_and_fuel_cut(self):
        cruise = dict(IDLE, rpm=2500, airflow_cfm=40.0, injector_ms=4.0, speed_kmh=96.0)
        end = drive(self.f, 1, 30, cruise)
        s = self.f.summary(end)
        self.assertTrue(15 < s["now_mpg"] < 60, s["now_mpg"])
        self.assertAlmostEqual(s["now_gph"], 60 / s["now_mpg"], delta=0.2)   # gal/h while moving too
        cut = dict(cruise, injector_ms=0.0)              # coasting in gear: no fuel
        self.assertEqual(fuel.fuel_grams(cut, 1.0), 0.0)

    def test_new_drive_resets_drive_not_tank(self):
        drive(self.f, 1, 10, dict(IDLE, speed_kmh=50))
        drive(self.f, 2, 10, dict(IDLE, speed_kmh=50), t0=5000)
        s = self.f.summary(5010)
        self.assertEqual(s["drive"]["session"], 2)
        self.assertAlmostEqual(s["tank"]["miles"], 2 * s["drive"]["miles"], delta=0.1)

    def test_fill_ups_calibrate(self):
        self.f.fill(10.0, now=1)                         # first fill: counting starts
        self.assertFalse(self.f.summary()["calibrated"])
        self.f.tank_km, self.f.tank_g = 300 * fuel.KM_PER_MILE, 10 * fuel.GRAMS_PER_GALLON   # est 10 gal
        s = self.f.fill(12.0, now=2)                     # really took 12
        self.assertTrue(s["calibrated"])
        self.assertAlmostEqual(s["factor"], 1.2)
        self.assertEqual(s["fills"][0]["mpg"], 25.0)
        self.assertEqual(s["tank"]["miles"], 0)
        again = fuel.Fuel(self.path, log=lambda m: None)   # saved
        self.assertAlmostEqual(again.factor, 1.2)
        with self.assertRaises(ValueError):
            self.f.fill(0)

    def test_range_left(self):
        self.assertIsNone(self.f.summary()["range"])          # no fill-up yet: nothing to count from
        self.f.fill(10.0, now=1)
        r = self.f.summary()["range"]
        self.assertEqual((r["gallons_left"], r["miles"]), (17.4, None))     # full, but no MPG known yet
        self.f.tank_km, self.f.tank_g = 5 * fuel.KM_PER_MILE, 0.25 * fuel.GRAMS_PER_GALLON
        r = self.f.summary()["range"]
        self.assertEqual((r["basis"], r["miles"]), ("this tank", round(17.15 * 20)))   # only 5 mi: still used
        self.f.tank_km, self.f.tank_g = 250 * fuel.KM_PER_MILE, 10 * fuel.GRAMS_PER_GALLON
        self.f.fill(12.5, now=2)                                # measured: 20 mpg
        self.f.tank_km, self.f.tank_g = 4 * fuel.KM_PER_MILE, 0.1 * self.f.factor ** -1 * fuel.GRAMS_PER_GALLON
        r = self.f.summary()["range"]
        self.assertEqual(r["basis"], "last fill-ups")         # 4 mi in: the measured fill-up MPG
        self.assertEqual(r["miles"], round(17.3 * 20))
        self.assertEqual(r["to_light"], round(15.3 * 20))
        self.f.tank_km, self.f.tank_g = 300 * fuel.KM_PER_MILE, 16.5 * self.f.factor ** -1 * fuel.GRAMS_PER_GALLON
        r = self.f.summary()["range"]
        self.assertEqual((r["basis"], r["to_light"]), ("this tank", 0))      # past the light

    def test_short_tank_doesnt_calibrate(self):
        self.f.fill(10.0, now=1)
        self.f.tank_km, self.f.tank_g = 5, 1000
        self.f.fill(1.0, now=2)
        self.assertFalse(self.f.summary()["calibrated"])


class GasScreenTest(unittest.TestCase):
    def test_numpad_entry(self):
        got = []
        p = gas.NumPad("GALLONS", got.append)
        keys = {k: r for r, k in p.keys()}
        tap = lambda k: p.tap((keys[k][0] + keys[k][2]) // 2, (keys[k][1] + keys[k][3]) // 2)  # noqa: E731
        for k in "11.4.":
            tap(k)
        self.assertEqual(p.value, "11.4")
        tap("DEL"); tap("6")
        self.assertEqual(tap("OK"), "close")
        self.assertEqual(got, [11.6])
        p2 = gas.NumPad("GALLONS", got.append)
        self.assertIsNone(p2.tap(*[(r[0] + 2, r[1] + 2) for r, k in p2.keys() if k == "OK"][0]))   # empty: refused
        self.assertTrue(p2.bad)

    def test_fill_tank_opens_numpad_and_posts(self):
        class Feed:
            fuel = {}
            def fill(self, g):
                self.got = g
                return {"fills": [{"gallons": g, "miles": 250.0, "mpg": 22.0}]}
        feed = Feed()
        s = gas.GasScreen(feed)
        pad = s.tap(*s.FILL[:2])
        self.assertIsInstance(pad, gas.NumPad)
        pad.on_done(11.4)
        import time
        for _ in range(50):
            if "SAVED" in s.note:
                break
            time.sleep(0.01)
        self.assertEqual(feed.got, 11.4)
        self.assertIn("22.0 MPG", s.note)
        self.assertEqual(s.tap(*s.BACK[:2]), "close")


if __name__ == "__main__":
    unittest.main()
