"""GPS NMEA parsing and gear detection (no hardware)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gear  # noqa: E402
import gps  # noqa: E402


def nmea(body):
    x = 0
    for c in body:
        x ^= ord(c)
    return f"${body}*{x:02X}"


class GPSTest(unittest.TestCase):
    def test_real_sentences(self):
        self.assertTrue(gps.checksum_ok("$GNRMC,051001.000,V,,,,,,,161026,,,N,V*2E"))
        self.assertFalse(gps.checksum_ok("$GNRMC,051001.000,V,,,,,,,161026,,,N,V*2F"))

    def test_fix_and_speed(self):
        g = gps.GPS(log=lambda m: None)
        self.assertFalse(g.handle("$GNRMC,051001.000,V,,,,,,,161026,,,N,V*2E", now=100))
        self.assertEqual(g.snapshot(100), {})
        g.handle(nmea("GNGGA,051247.000,4531.1340,N,12240.7560,W,1,08,1.45,55.6,M,,M,,"), now=100)
        self.assertTrue(g.handle(nmea("GNRMC,051247.000,A,4531.1340,N,12240.7560,W,27.0,208.0,161026,,,A,V"), now=100))
        s = g.snapshot(100.3)
        self.assertAlmostEqual(s["lat"], 45.518900, places=5)
        self.assertAlmostEqual(s["lon"], -122.679267, places=5)
        self.assertAlmostEqual(s["speed_kmh"], 50.0, places=0)     # 27 knots
        self.assertEqual((s["gps_sats"], s["alt_m"], s["course_deg"]), (8, 56, 208))
        # accelerating: 50 -> 60 km/h in a second, carried on between fixes
        g.handle(nmea(f"GNRMC,051248.000,A,4531.1340,N,12240.7560,W,{60 / 1.852:.3f},208.0,161026,,,A,V"), now=101)
        self.assertAlmostEqual(g.snapshot(101.5)["speed_kmh"], 68, delta=0.5)
        self.assertEqual(g.snapshot(110), {})                        # stale

    def test_parked_is_zero(self):
        g = gps.GPS(log=lambda m: None)
        g.handle(nmea("GNRMC,051247.000,A,4531.1340,N,12240.7560,W,0.07,184.6,161026,,,A,V"), now=5)
        self.assertEqual(g.snapshot(5)["speed_kmh"], 0)
        self.assertNotIn("course_deg", g.snapshot(5))


class GearTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.f = gear.GearFinder(os.path.join(self.tmp.name, "gears.json"), log=lambda m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, rpm, kmh, n=8, t0=0, accel=0.0):
        g = None
        for i in range(n):
            g = self.f.update(t0 + i * 0.25, rpm, kmh, accel)
        return g

    def test_each_gear(self):
        for g, r in gear.ESTIMATE.items():
            self.assertEqual(self.f.match(r * 40, 40), g)

    def test_not_in_gear(self):
        self.assertEqual(self.f.match(850, 0), 0)          # stopped
        self.assertEqual(self.f.match(800, 50), 0)         # clutch in, idling
        self.assertEqual(self.f.match(3000, 50), 0)        # 60 rpm per km/h: between 2nd and 3rd

    def test_confirm_and_hold(self):
        self.assertEqual(self.f.update(0, 2752, 80), 0)      # 4th? wait for a second sample
        self.assertEqual(self.f.update(0.25, 2752, 80), 4)
        self.assertEqual(self.f.update(0.5, 1500, 80), 4)     # clutch dip mid-shift: hold
        self.assertEqual(self.f.update(0.75, 1500, 80), 4)
        self.assertEqual(self.f.update(1.5, 1500, 80), 0)     # held too long: no gear
        self.assertEqual(self.drive(2208, 80, t0=2), 5)

    def test_learns_and_saves(self):
        true5 = 29.5                                          # real 5th is 7% taller than the estimate
        self.drive(true5 * 100, 100, n=4000)
        self.assertAlmostEqual(self.f.ratios[5], true5, delta=0.3)
        self.assertTrue(os.path.exists(self.f.path))
        again = gear.GearFinder(self.f.path, log=lambda m: None)
        self.assertAlmostEqual(again.ratios[5], self.f.ratios[5], delta=0.1)

    def test_no_learning_while_accelerating(self):
        self.drive(29.5 * 100, 100, n=400, accel=5.0)
        self.assertEqual(self.f.ratios[5], gear.ESTIMATE[5])


class LoggerGPSTest(unittest.TestCase):
    def test_samples_get_speed_and_gear(self):
        import logger as lg

        class FakeGPS:
            connected = True

            def snapshot(self, now=None):
                return {"lat": 45.5, "lon": -122.6, "speed_kmh": 80.0}

            def accel(self):
                return 0.0

        with tempfile.TemporaryDirectory() as d:
            lg_ = lg.Logger(None, None, FakeGPS())
            lg_.gears = gear.GearFinder(os.path.join(d, "g.json"), log=lambda m: None)
            out = [lg_._with_gps(i * 0.25, {"rpm": 2752}) for i in range(3)]
            self.assertEqual(out[-1]["speed_kmh"], 80.0)
            self.assertEqual(out[-1]["gear"], 4)
            self.assertNotIn("gear", lg_._with_gps(1, {}))     # heartbeat: no engine, no gear


if __name__ == "__main__":
    unittest.main()
