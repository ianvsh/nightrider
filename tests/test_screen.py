"""Screen settings: backlight, auto-dim by the sun, colour schemes."""
import calendar
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pixels  # noqa: E402
import screen  # noqa: E402

PDX = (45.52, -122.68)


def utc(*args):
    return calendar.timegm(args + (0,) * (6 - len(args)))


class FakeBacklight:
    path = "/fake"

    def __init__(self):
        self.values = []

    def set(self, pct):
        self.values.append(pct)


class BacklightTest(unittest.TestCase):
    def test_never_darker_than_the_floor(self):
        bl = screen.Backlight.__new__(screen.Backlight)
        bl.max, bl.last = 255, None
        with tempfile.NamedTemporaryFile("w+") as f:
            bl.path = f.name
            # 1 % = Ian's dimmest readable (48), 100 % = full; 0 can't go below 1 %
            for pct, raw in ((0, 48), (1, 48), (100, 255), (50, 150), (1.5, 49)):
                bl.set(pct)
                f.seek(0)
                self.assertEqual(int(f.read()), raw)


class SunTest(unittest.TestCase):
    def test_elevation(self):
        # Portland solar noon ~20:15 UTC: ~68 deg at midsummer, ~21 deg at midwinter
        self.assertAlmostEqual(screen.sun_elevation(*PDX, utc(2026, 6, 21, 20, 15)), 68, delta=1.5)
        self.assertAlmostEqual(screen.sun_elevation(*PDX, utc(2026, 12, 21, 20, 10)), 21, delta=1.5)
        self.assertLess(screen.sun_elevation(*PDX, utc(2026, 10, 16, 8, 0)), -30)      # 1 am local

    def test_sunset_time(self):
        # 16 Oct 2026, Portland: sunset ~18:22 PDT = 01:22 UTC on the 17th
        what, when = screen.next_change(*PDX, utc(2026, 10, 16, 20, 0))
        self.assertEqual(what, "SUNSET")
        self.assertAlmostEqual(when, utc(2026, 10, 17, 1, 22), delta=15 * 60)

    def test_daylight_blend(self):
        self.assertEqual(screen.daylight(10), 1)
        self.assertEqual(screen.daylight(-10), 0)
        self.assertAlmostEqual(screen.daylight(-1.5), 0.5)
        self.assertLess(screen.daylight(2.5), 1)                       # eased: no corner at either end
        self.assertLess(screen.daylight(2.9) - screen.daylight(2.8), screen.daylight(-1.4) - screen.daylight(-1.5))

    def test_steps(self):
        down, v = [], 15
        while v > 1:
            v = screen.step(v, up=False)
            down.append(v)
        self.assertEqual(down, [10, 5, 4, 3, 2, 1])                    # 1 % at a time below 5 %
        self.assertEqual(screen.step(1, up=False), 1)
        self.assertEqual([screen.step(v, up=True) for v in (1, 4, 5, 95, 100)], [2, 5, 10, 100, 100])
        self.assertEqual(screen.step(10, up=False, lo=10), 10)          # day brightness keeps its 10 % floor


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bl = FakeBacklight()
        self.s = screen.Screen(os.path.join(self.tmp.name, "screen.json"), self.bl)

    def tearDown(self):
        pixels.set_scheme("green")
        self.tmp.cleanup()

    def test_auto_dim(self):
        self.s.change(brightness=80, night=20, auto=True)
        fix = {"lat": PDX[0], "lon": PDX[1]}
        self.s.tick(fix, now=utc(2026, 10, 16, 20, 0), force=True)        # midday
        self.assertEqual(self.bl.values[-1], 80)
        self.s.tick(fix, now=utc(2026, 10, 17, 6, 0), force=True)         # 11 pm
        self.assertEqual(self.bl.values[-1], 20)
        self.s.change(auto=False)
        self.assertEqual(self.bl.values[-1], 80)                          # manual: as set
        again = screen.Screen(self.s.path, FakeBacklight())               # settings kept
        self.assertEqual((again.settings["night"], again.settings["auto"]), (20, False))

    def test_sunset_fades_smoothly(self):
        self.s.change(brightness=100, night=1, auto=True)
        fix = {"lat": PDX[0], "lon": PDX[1]}
        levels = []
        for minute in range(-30, 75):                                    # 16 Oct sunset ~18:22 PDT
            self.s.tick(fix, now=utc(2026, 10, 17, 1, 22) + minute * 60, force=True)
            levels.append(self.s.level)
        self.assertEqual((levels[0], levels[-1]), (100, 1))
        self.assertTrue(all(b <= a for a, b in zip(levels, levels[1:])))   # only ever dims
        biggest = max(a / b for a, b in zip(levels, levels[1:]) if b)
        self.assertLess(biggest, 1.25)                                   # no step over ~20 % a minute, even near 1 %

    def test_settings_screen(self):
        from PIL import Image, ImageDraw
        ss = screen.ScreenSettings(self.s)
        start = self.s.settings["brightness"]
        x0, y0, _, _ = ss.at(ss.MINUS, ss.ROW_Y[0])
        ss.tap(x0 + 2, y0 + 2)
        self.assertEqual(self.s.settings["brightness"], start - screen.STEP)
        self.assertFalse(self.s.settings["auto"])                        # off until the panel's checked
        x0, y0, _, _ = ss.at(ss.AUTO, ss.ROW_Y[1])
        ss.tap(x0 + 2, y0 + 2)
        self.assertTrue(self.s.settings["auto"])
        x0, y0, _, _ = ss.at(ss.AMBER_BTN, ss.ROW_Y[3])
        ss.tap(x0 + 2, y0 + 2)
        self.assertEqual(self.s.settings["scheme"], "amber")
        img = Image.new("RGB", (pixels.LW, pixels.LH), pixels.BG)
        ss.render(img, ImageDraw.Draw(img))
        self.assertEqual(ss.tap(*ss.BACK[:2]), "close")

    def test_clean(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import display
        ui = display.UI(type("Feed", (), {"latest": None, "engine_on": False, "sessions": [], "drive_start": {},
                                          "battery": {}, "fuel": {}, "gps": None, "track": []})(), screen_settings=self.s)
        ui.stack.append(screen.ScreenSettings(self.s))
        clean = ui.stack[-1].tap(*screen.ScreenSettings.CLEAN[:2])
        self.assertIsInstance(clean, screen.CleanScreen)
        ui.stack.append(clean)
        self.assertTrue(ui.no_hold)                          # a finger held down wiping isn't a hold
        before = dict(self.s.settings)
        for xy in ((10, 30), (200, 140), (5, 5), (130, 80)):   # wiping: every touch ignored
            ui.tap(xy)
        self.assertIs(ui.stack[-1], clean)
        self.assertEqual(self.s.settings, before)
        frame = ui.render()
        self.assertLess(frame.mean(), 2)                     # black, but for the small countdown
        with mock.patch.object(screen.time, "monotonic", lambda: clean.started + clean.SECS + 0.1):
            ui.render()
        self.assertIsInstance(ui.stack[-1], screen.ScreenSettings)   # back where it was

    def test_amber_scheme(self):
        def px(c):
            return tuple(int(v) for v in pixels.recolour(np.array([[c]], np.uint8))[0, 0])
        self.assertEqual(px(pixels.GREEN), pixels.GREEN)                 # green: untouched
        pixels.set_scheme("amber")
        self.assertEqual(px(pixels.GREEN), (255, 168, 0))
        r, g, b = px(pixels.DIM)
        self.assertTrue(b == 0 and 1.4 < r / g < 1.6)                   # dim green -> dim amber, not olive
        self.assertEqual(px(pixels.AMBER), (255, 236, 186))              # highlights stay distinct
        self.assertEqual(px(pixels.RED)[0], 255)


if __name__ == "__main__":
    unittest.main()
