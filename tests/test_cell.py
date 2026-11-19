"""cell.py parsing + the SET > 4G screen (no modem needed)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cell  # noqa: E402
import screens  # noqa: E402

REPLIES = {
    "AT+CPIN?": "\r\n+CPIN: READY\r\n\r\nOK\r\n",
    "AT+CEREG?": "\r\n+CEREG: 0,5\r\n\r\nOK\r\n",
    "AT+COPS?": '\r\n+COPS: 0,2,"310260",7\r\n\r\nOK\r\n',
    "AT+CSQ": "\r\n+CSQ: 16,0\r\n\r\nOK\r\n",
    "AT+CPSI?": "\r\n+CPSI: LTE,Online,310-260,0x35E9,21839618,12,EUTRAN-BAND66,66786,5,5,-7,-108,-101,16\r\n\r\nOK\r\n",
    "AT+CGPADDR": "\r\n+CGPADDR: 1,100.127.149.199\r\n\r\nOK\r\n",
}


class CellTest(unittest.TestCase):
    def test_parse_the_real_replies(self):
        i = cell.parse(REPLIES)
        self.assertEqual((i["sim"], i["reg"], i["operator"], i["tech"], i["band"]),
                         ("READY", "ROAMING", "T-MOBILE", "LTE", "BAND66"))
        self.assertEqual((i["rsrp"], i["snr"], i["bars"]), (-108, 16, 2))
        self.assertEqual(i["modem_ip"], "100.127.149.199")

    def test_no_sim_and_no_service(self):
        i = cell.parse({"AT+CPIN?": "\r\n+CME ERROR: SIM not inserted\r\n", "AT+CPSI?": "\r\n+CPSI: NO SERVICE,Online\r\n"})
        self.assertEqual(i["bars"], 0)
        self.assertEqual(i["tech"], "NO SERVICE")

    def test_bars(self):
        self.assertEqual([cell.bars(x) for x in (-85, -95, -105, -115, -125, None)], [4, 3, 2, 1, 0, 0])

    def test_screen_taps_save_and_apply(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        applied = []
        with mock.patch.object(cell, "FILE", os.path.join(tmp.name, "network.json")), \
                mock.patch.object(cell, "apply_prefer", lambda p: applied.append(p) or (True, "")), \
                mock.patch.object(cell, "apply_hotspot", lambda on: applied.append(on) or (True, "")), \
                mock.patch.object(cell, "hotspot_password", lambda: "pw"):
            m = cell.Modem(start=False)
            m.present, m.info = True, cell.parse(REPLIES)
            s = screens.CellScreen(m)
            self.assertEqual(s.settings["prefer"], "cell")              # Ian's default
            s.tap(*screens.CellScreen.WIFI_FIRST[:2])
            while s.busy:
                pass
            s.tap(*screens.CellScreen.HOT_ON[:2])
            while s.busy:
                pass
            self.assertEqual(applied, ["wifi", True])
            self.assertEqual(cell.load(), {"prefer": "wifi", "hotspot": True})
            from PIL import Image, ImageDraw
            from pixels import BG, LH, LW
            img = Image.new("RGB", (LW, LH), BG)
            s.render(img, ImageDraw.Draw(img))
            self.assertEqual(s.tap(*screens.CellScreen.BACK[:2]), "close")
            self.assertFalse(m.visible)


if __name__ == "__main__":
    unittest.main()


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "usage.json")
        self.c, self.b = [0], ["boot1"]

    def usage(self):
        return cell.Usage(self.path, counter=lambda: self.c[0], boot=lambda: self.b[0], now=lambda: 1000.0)

    def test_counts_across_reboots_and_replugs(self):
        u = self.usage()
        self.c[0] = 300_000_000
        u.update()
        self.c[0] = 500_000_000
        u.update()
        self.assertAlmostEqual(u.used_mb, 500)
        u.save()
        self.b[0], self.c[0] = "boot2", 50_000_000                   # rebooted: counter from 0 again
        u = self.usage()
        u.update()
        self.assertAlmostEqual(u.used_mb, 550)
        self.c[0] = 10_000_000                                       # modem replugged: counter reset
        u.update()
        self.assertAlmostEqual(u.used_mb, 560)
        self.assertAlmostEqual(u.fraction, 0.28)
        self.assertEqual((u.used_text(), u.plan_text()), ("560 MB", "2 GB"))

    def test_refill_and_plan(self):
        u = self.usage()
        self.c[0] = 1_900_000_000
        u.update()
        self.assertEqual(u.used_text(), "1.90 GB")
        u.refilled()
        self.assertEqual(u.used_mb, 0)
        self.c[0] = 1_950_000_000
        u.update()
        self.assertAlmostEqual(u.used_mb, 50)                        # only what came after
        u.next_plan()
        self.assertEqual(u.plan_text(), "3 GB")
        self.assertEqual(self.usage().plan_text(), "3 GB")           # kept


class StereoGoneTest(unittest.TestCase):
    """BLUETOOTH chosen, stereo goes: music paused (not played on through AUX), back on return."""

    def router(self):
        import bt
        r = object.__new__(bt.Router)
        r.log, r._silent, r._held = (lambda m: None), 0, False
        r._rate_pcm, r._rate_checks, r._rate_at = None, 0, 0.0
        r._via, r._via_at, r._quality_at = None, 0.0, -1e9
        r.settings = {"output": "bluetooth", "stereo": "AA", "name": "TR7412UB-OR", "quality_cell": 160,
                      "quality_wifi": 320}
        r.route, r._tried = "bluetooth", 1e18
        self.connected = [True]
        r.bt = type("B", (), {"connected": lambda _, mac: self.connected[0]})()
        self.calls = []
        r._switch = lambda route, resume=None, lost=False: (self.calls.append(("switch", route, resume)),
                                                             setattr(r, "route", route))
        r.bridge = type("Br", (), {"out": None, "stop": lambda _: None})()
        r._check_bridge = lambda pcm: None
        r._pause = lambda: self.calls.append(("pause",))

        class P:
            playing, device, error, at = True, spotify_device(), "", 0.0

            def refresh(self):
                pass
        r.player = P()
        return r

    def test_pause_then_resume(self):
        import bt
        r = self.router()
        with mock.patch.object(bt, "bt_pcm", lambda: None), \
                mock.patch.object(bt.time, "monotonic", lambda: 5.0), mock.patch.object(r, "_follow_quality", lambda: None), \
                mock.patch.object(bt.time, "sleep", lambda s: None):
            self.connected[0] = False
            r._step()
            self.assertEqual(self.calls, [("pause",), ("switch", "aux", False)])
            self.calls.clear()
            r._step()                                    # someone presses play: paused again
            self.assertEqual(self.calls, [("pause",)])
            self.calls.clear()
            r.player.playing = False
            self.connected[0] = True
            with mock.patch.object(bt, "bt_pcm", lambda: "/pcm"), mock.patch.object(bt, "fix_rate", lambda p: False):
                r._step()
            self.assertEqual(self.calls, [("switch", "bluetooth", True)])     # was playing: carries on

    def test_quality_follows_the_link(self):
        import bt
        r = self.router()
        conf = os.path.join(tempfile.mkdtemp(), "app.toml")
        with open(conf, "w") as f:
            f.write('[device]\nname = "924S"\nbitrate = 320\n')
        t = [0.0]
        restarts = []
        with mock.patch.object(bt.spotify, "CONFIG", conf), mock.patch.object(bt.cell, "internet_via", lambda: "4G"), \
                mock.patch.object(bt.time, "monotonic", lambda: t[0]), \
                mock.patch.object(bt.spotify, "restart", lambda reason, log: restarts.append(reason) or True), \
                mock.patch.object(bt.spotify, "resume_here", lambda wait: self.calls.append(("resume",))):
            r._follow_quality()                          # link just seen: wait for it to settle
            t[0] = 5.0
            r._follow_quality()
            self.assertEqual(bt.player_bitrate(conf), 320)
            t[0] = 20.0
            r._follow_quality()
            self.assertEqual(bt.player_bitrate(conf), 160)
            self.assertEqual(restarts, ["stream quality 160 kbps"])     # the bitrate is read at start-up
            self.assertEqual(self.calls, [("resume",)])                   # was playing: carries on
            t[0] = 200.0
            r._follow_quality()                          # already right: nothing
            self.assertEqual(len(restarts), 1)


def spotify_device():
    import spotify
    return spotify.DEVICE
