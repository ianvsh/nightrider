import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import odometer  # noqa: E402
import store  # noqa: E402

M_PER_DEG = 111195          # metres per degree of latitude (odometer's earth radius)


def north(km):
    """Latitude km north of 45.5 N (driving due north)."""
    return 45.5 + km * 1000 / M_PER_DEG


class OdometerTest(unittest.TestCase):
    def drive(self, odo, t0, secs, kmh, km0=0.0, hz=10, fix=lambda t: True):
        """Due north at kmh for secs. -> (km counted, end time, km actually driven)."""
        total, km = 0.0, km0
        for i in range(int(secs * hz)):
            t = t0 + i / hz
            km = km0 + kmh * (i / hz) / 3600
            data = {"rpm": 2000}
            if fix(t):
                data.update(lat=north(km), lon=-122.6, speed_kmh=kmh)
            total += odo.add(t, data)
        return total, t0 + secs, km

    def test_speed_times_time(self):
        counted, _, driven = self.drive(odometer.Odometer(), 1000, 600, 60)
        self.assertAlmostEqual(counted, driven, delta=driven * 0.005)

    def test_tunnel_bridged_with_a_straight_line(self):
        tunnel = lambda t: not (1100 <= t < 1160)                   # a minute with no fix
        counted, _, driven = self.drive(odometer.Odometer(), 1000, 600, 60, fix=tunnel)
        self.assertAlmostEqual(counted, driven, delta=driven * 0.005)   # not just the 9 km with a fix

    def test_pulling_away_before_the_first_fix(self):
        odo = odometer.Odometer()
        odo.parked(900, {"lat": north(0), "lon": -122.6})          # heartbeat while parked
        no_fix_first = lambda t: t >= 1120                          # cold start: 2 minutes to a fix
        counted, _, driven = self.drive(odo, 1000, 600, 40, fix=no_fix_first)
        self.assertAlmostEqual(counted, driven, delta=driven * 0.005)

    def test_wander_and_bad_fixes_dont_count(self):
        odo = odometer.Odometer()
        odo.parked(900, {"lat": north(0), "lon": -122.6})
        self.assertEqual(odo.add(1000, {"lat": north(0.01), "lon": -122.6, "speed_kmh": 0}), 0)   # 10 m drift
        odo.prev = None
        self.assertEqual(odo.add(1003, {"lat": north(5), "lon": -122.6, "speed_kmh": 0}), 0)      # 5 km in 3 s
        self.assertEqual(odo.add(1003.1, {"rpm": 800}), 0)          # no fix: nothing yet


class StoreDistanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def log_drive(self, s, t0, secs, kmh, km0=0.0):
        for i in range(secs * 10):
            km = km0 + kmh * (i / 10) / 3600
            s.add({"rpm": 2000, "lat": north(km), "lon": -122.6, "speed_kmh": kmh}, engine=True, ts=t0 + i / 10)
        return km0 + kmh * secs / 3600

    def test_sessions_keep_their_distance_and_survive_a_restart(self):
        s = store.Store(self.path)
        end = self.log_drive(s, 1000, 300, 60)                      # 5 km
        self.assertAlmostEqual(s.latest["km"], 60 * 0.1 / 3600)
        s.close()
        s = store.Store(self.path)                                  # logger restarted mid-drive...
        self.log_drive(s, 1320, 300, 60, km0=end + 60 * 20 / 3600)  # ...20 s later, 300 m further on
        sessions = s.sessions()
        self.assertEqual(len(sessions), 2)
        self.assertAlmostEqual(sessions[1]["km"], 5.0, delta=0.03)
        self.assertAlmostEqual(sessions[0]["km"], 5.0 + 0.333, delta=0.05)   # the gap is bridged
        s.close()

    def test_backfill_matches_live_counting(self):
        s = store.Store(self.path)
        self.log_drive(s, 1000, 300, 50)
        s.add({"lat": north(50 * 300 / 3600), "lon": -122.6}, engine=False, ts=1400)   # parked
        self.log_drive(s, 2000, 200, 70, km0=50 * 300 / 3600)
        live = [r["km"] for r in s.sessions()]
        s.db.execute("UPDATE sessions SET km = NULL")              # as logged before km existed
        s.db.commit()
        logs = []
        self.assertEqual(store.backfill_km(self.path, logs.append, s.set_km), 2)    # as the logger does
        self.assertEqual(store.backfill_km(self.path, logs.append), 0)       # once only
        for got, want in zip([r["km"] for r in s.sessions()], live):
            self.assertAlmostEqual(got, want, places=3)
        s.close()


if __name__ == "__main__":
    unittest.main()
