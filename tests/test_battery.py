"""battery.py: the charge estimate, replayed from a made-up log."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import battery  # noqa: E402
from store import Store  # noqa: E402

T0 = 1_800_000_000.0


class BatteryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "log.db")
        self.conf = os.path.join(self.dir.name, "battery.json")
        self.store = Store(self.db)

    def add(self, ts, data, engine):
        self.store.add(data, engine=engine, ts=ts)

    def park(self, t0, hours):
        for m in range(int(hours * 60)):
            self.add(t0 + m * 60, {}, False)
        return t0 + hours * 3600

    def drive(self, t0, minutes, volts):
        for s in range(int(minutes * 60)):
            self.add(t0 + s, {"rpm": 2000, "dme_volts": volts}, True)
        return t0 + minutes * 60

    def estimate(self, now):
        self.store.flush()
        return battery.Battery(self.db, self.conf, log=lambda m: None).update(now)

    def test_pi_drains_overnight(self):
        t = self.park(T0, 10)
        e = self.estimate(t)
        drain = (0.55 + 0.03) * 10 / 30 * 100                 # ~19%
        self.assertAlmostEqual(e["soc_pct"], 100 - drain, delta=0.5)

    def test_pi_off_only_parasitic(self):
        self.add(T0, {}, False)
        self.add(T0 + 10 * 3600, {}, False)                    # nothing in between: Pi was off
        self.assertAlmostEqual(self.estimate(T0 + 10 * 3600)["soc_pct"], 100 - 0.03 * 10 / 30 * 100, delta=0.2)

    def test_drive_charges_back_but_not_past_full(self):
        t = self.park(T0, 10)
        t = self.drive(t, 20, 13.9)
        self.assertGreater(self.estimate(t)["soc_pct"], 97)
        t = self.drive(t, 60, 13.9)
        self.assertLessEqual(self.estimate(t)["soc_pct"], 100)

    def test_idle_low_volts_drains(self):
        t = self.park(T0, 1)
        before = self.estimate(t)["soc_pct"]
        t = self.drive(t, 10, 13.1)
        self.assertLess(self.estimate(t)["soc_pct"], before - 2)

    def test_checkpoint_and_set(self):
        t = self.park(T0, 5)
        b = battery.Battery(self.db, self.conf, log=lambda m: None)
        b.update(t)
        b.set(60, now=t)
        with open(self.conf) as f:
            saved = json.load(f)
        self.assertEqual(saved["soc"], 60)
        self.assertEqual(saved["capacity_ah"], 30.0)            # settings written out to edit
        t2 = self.park(t + 60, 1)
        e = self.estimate(t2)
        self.assertLess(e["soc_pct"], 60)
        self.assertGreater(e["soc_pct"], 56)


if __name__ == "__main__":
    unittest.main()
