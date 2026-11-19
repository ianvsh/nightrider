import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import display  # noqa: E402
import gas  # noqa: E402
import mappage  # noqa: E402
import spotify  # noqa: E402


def tab_x(label):
    """x in the middle of a tab, so tests don't depend on how the tab bar is laid out."""
    i = [name for name, _ in display.TABS].index(label)
    r = display.tab_rects()[i]
    return (r[0] + r[2]) // 2


class FakeFeed:
    ok = True
    wifi = "HomeWifi"
    battery = {"soc_pct": 72.4, "estimated": True}
    info = {"ip": ["192.168.0.103"], "dme_connected": True, "samples": 10, "sessions": 1,
            "pi_temp_c": 45.0, "uptime": 3700}

    def __init__(self, engine_on):
        import collections
        self.engine_on = engine_on
        now = time.time()
        self.airflow = collections.deque(((now - i / 4, 4 + (i % 40) * 1.5) for i in range(240, 0, -1)), maxlen=240)
        self.drive_start = {1: now - 90}
        self.sessions = [{"id": 2, "start": now - 3000, "end": now - 1200, "samples": 1}]
        self.latest = {"id": 1, "ts": time.time(), "session": 1 if engine_on else None,
                       "data": {"rpm": 6400, "coolant_c": 110, "afr": 14.7, "mark_swapped": True,
                                "throttle": "idle", "weird_new_reading": 3,
                                "o2_volts": 0.62, "o2_state": "rich", "airflow_gs": 61.5, "idle_valve_pct": 31}}


class DisplayTest(unittest.TestCase):
    def test_every_page_renders(self):
        for on in (True, False):
            for page in range(len(display.PAGES)):
                self.assertEqual(display.render(FakeFeed(on), page).shape, (480, 800, 3))
        self.assertEqual(display.render_shutdown(0.5).shape, (480, 800, 3))

    def test_map_carries_car_on_between_fixes(self):
        f = display.Feed.__new__(display.Feed)          # no polling thread
        f.gps = {"lat": 45.0, "lon": -122.0, "speed_kmh": 36.0, "course_deg": 90}
        f.gps_at = time.time() - 1.0                    # 10 m/s for 1 s, heading east
        p = f.gps_now()
        self.assertAlmostEqual(p["lat"], 45.0, places=6)
        self.assertAlmostEqual(mappage.metres((45.0, -122.0), (p["lat"], p["lon"])), 10, delta=0.2)
        f.gps_at = time.time() - 60                     # stale: at most AHEAD_SECS
        p = f.gps_now()
        self.assertAlmostEqual(mappage.metres((45.0, -122.0), (p["lat"], p["lon"])), 15, delta=0.3)
        f.gps = {"lat": 45.0, "lon": -122.0, "speed_kmh": 0.0}
        self.assertEqual(f.gps_now(), f.gps)            # parked: unchanged

    def test_font_glyphs_are_5x7(self):
        self.assertEqual({k for k, v in display.GLYPHS.items() if len(v) != 35}, set())

    def test_warnings(self):
        display.blink = lambda: True
        self.assertEqual(display.colour("coolant_c", 110), display.RED)
        self.assertEqual(display.colour("mark_swapped", True), display.RED)
        self.assertEqual(display.fmt("mark_swapped", False), "NO")
        self.assertEqual(display.colour("coolant_c", 90), display.GREEN)
        self.assertEqual(display.colour("coolant_c", 40), display.AMBER)

    def test_framebuffer_writes_only_changed_rows(self):
        import mmap
        import numpy as np
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"\0" * 800 * 480 * 2)
            f.flush()
            fb = display.Framebuffer.__new__(display.Framebuffer)
            fb.bpp, fb.stride, fb.size, fb.prev = 16, 1600, (800, 480), None
            fb.f = open(f.name, "r+b", buffering=0)
            fb.mem = mmap.mmap(fb.f.fileno(), 1600 * 480)
            a = np.zeros((480, 800, 3), np.uint8)
            a[...] = (255, 0, 0)
            fb.show(a)
            self.assertEqual(fb.mem[:2], b"\x00\xf8")       # red = 0xF800, little-endian
            fb.mem[0:2] = b"\x12\x34"                       # a row we won't change again
            a[200] = (0, 0, 255)
            fb.show(a)
            self.assertEqual(fb.mem[:2], b"\x12\x34")        # untouched: not rewritten
            self.assertEqual(fb.mem[200 * 1600:200 * 1600 + 2], b"\x1f\x00")
            fb.mem.close()
            fb.f.close()


class TouchTest(unittest.TestCase):
    def setUp(self):
        import threading
        self.clock = [100.0]
        self._real = time.monotonic
        display.time.monotonic = lambda: self.clock[0]
        self.t = display.Touch(threading.Event())

    def tearDown(self):
        display.time.monotonic = self._real

    def at(self, secs):
        self.clock[0] = 100.0 + secs

    def test_tap_fires_on_press_with_position(self):
        self.t.press(True, (100, 200))
        self.assertEqual(self.t.drain(), [("tap", (100, 200))])
        self.at(0.1); self.t.press(False)
        self.at(0.3); self.assertIsNone(self.t.poll())
        kind, pos = self.t.drain()[0]
        self.assertEqual(kind, "release")

    def test_release_position_is_averaged(self):
        self.t.press(True, (0, 0))
        for p in [(10, 10), (12, 14), (14, 12), (12, 12), (99, 99)]:
            self.t.move(p)
        self.at(0.2); self.t.press(False)
        self.at(0.5); self.t.poll()
        _, (x, y) = self.t.drain()[-1]
        self.assertAlmostEqual(x, 12, delta=0.5)       # edge readings dropped
        self.assertAlmostEqual(y, 12, delta=0.5)

    def test_hold_survives_flicker(self):
        self.t.press(True, (1, 1))
        self.at(2.0); self.t.press(False)          # brief lift from the resistive panel
        self.at(2.05); self.t.press(True, (1, 1))
        self.at(4.2); self.assertGreaterEqual(self.t.poll(), display.HOLD_FIRE)
        self.assertEqual([k for k, _ in self.t.drain()], ["tap"])

    def test_quick_taps_each_count(self):
        for n in range(3):
            self.at(n * 0.3); self.t.press(True, (n, n))
            self.at(n * 0.3 + 0.05); self.t.press(False)
        self.at(2); self.t.poll()
        self.assertEqual([k for k, _ in self.t.drain()].count("tap"), 3)

    def test_shutdown_screens_render(self):
        self.assertEqual(display.render_shutdown(0.5).shape, (480, 800, 3))
        self.assertEqual(display.render_shutdown(message=("a", "b\nc")).shape, (480, 800, 3))


class ScreensTest(unittest.TestCase):
    def test_calibration_fit_handles_swapped_inverted_axes(self):
        import screens
        # raw touch axes swapped and inverted relative to the screen
        to_raw = lambda x, y: (3900 - y * 22, 200 + x * 15)      # noqa: E731
        targets = screens.CalibrateScreen.TARGETS
        cal, err = screens.Calibration.fit([to_raw(*t) for t in targets], targets)
        self.assertLess(err, 0.01)
        self.assertEqual(cal.map(to_raw(120, 80)), (120, 80))

    def test_calibrate_screen_saves(self):
        import screens
        with tempfile.TemporaryDirectory() as d:
            old = screens.CAL_FILE
            screens.CAL_FILE = os.path.join(d, "touch.json")
            try:
                got = []
                c = screens.CalibrateScreen(got.append)
                r = None
                for t in c.TARGETS:
                    r = c.release((t[0] * 10, t[1] * 10))
                self.assertEqual(r, "close")
                self.assertEqual(screens.Calibration.load().map((500, 500)), (50, 50))
            finally:
                screens.CAL_FILE = old

    def test_keyboard_types_shift_symbols_and_delete(self):
        import screens
        done = []
        k = screens.Keyboard("PW", done.append)
        centre = lambda key: next(((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)   # noqa: E731
                                  for r, c in k.keys() if c == key)
        for key in ["a", "SHIFT", "B", "MODE", "!", "DEL", "#", "SPACE"]:
            k.tap(*centre(key))
        self.assertEqual(k.value, "aB# ")
        self.assertEqual(k.tap(*centre("OK")), "close")
        self.assertEqual(done, ["aB# "])

    def test_nmcli_field_split(self):
        import wifi
        self.assertEqual(wifi.fields(r"*:My\:Net:70:WPA2"), ["*", "My:Net", "70", "WPA2"])
        self.assertEqual(wifi.friendly("Error: Connection activation failed: Secrets were required"),
                         "WRONG PASSWORD?")

    def test_mpg_row_opens_gas_screen(self):
        import gas
        import screens
        feed = FakeFeed(True)
        feed.fuel = {"now_mpg": 24.3, "drive": {"miles": 3.2, "gallons": 0.13, "mpg": 24.6},
                     "tank": {"miles": 3.2, "gallons": 0.13, "mpg": 24.6}, "factor": 1.0, "fills": []}
        ui = display.UI(feed)
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        x0, y0, x1, y1 = display.MPG_TAP
        ui.tap(((x0 + x1) // 2, (y0 + y1) // 2))
        self.assertIsInstance(ui.stack[-1], gas.GasScreen)
        self.assertEqual(ui.render().shape, (480, 800, 3))
        ui.tap(display.gas.GasScreen.FILL[:2])                   # FILL TANK -> number pad
        self.assertIsInstance(ui.stack[-1], gas.NumPad)
        self.assertEqual(ui.render().shape, (480, 800, 3))

    def test_status_page_taps(self):
        import screens
        ui = display.UI(FakeFeed(False))
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])     # raw = screen
        page = lambda: display.PAGES[ui.page]                      # noqa: E731
        ui.tap((60, 60))
        self.assertEqual(page(), "more")                           # gauges -> readings
        ui.tap((60, 60))
        self.assertEqual(page(), "gauges")
        ui.tap((5, 5))                                             # INFO tab flips too
        self.assertEqual(page(), "more")
        for x, name in ((tab_x("MAP"), "map"), (tab_x("MUSIC"), "spotify"), (tab_x("AI"), "claude"),
                        (tab_x("SET"), "status"), (tab_x("TERM"), "terminal"), (265, "terminal"),
                        (tab_x("DRIVE"), "gauges")):
            ui.tap((x, 8))                                         # the tab bar, from any page
            self.assertEqual(page(), name)
        ui.tap((tab_x("SET"), 8))
        ui.tap((10, 116))                                          # not a button: stay
        self.assertEqual(page(), "status")
        ui.screen = type("S", (), {"settings": {}})()
        ui.tap((60, 140))                                          # SCREEN button
        self.assertEqual(type(ui.stack[-1]).__name__, "ScreenSettings")
        ui.tap(ui.stack[-1].CAL[:2])                               # TOUCH CAL lives under SCREEN
        self.assertIsInstance(ui.stack[-1], screens.CalibrateScreen)
        self.assertTrue(ui.no_hold)
        ui.stack.clear()
        ui.tap((150, 140))                                         # SHUT DOWN box: stay put
        self.assertEqual(display.PAGES[ui.page], "status")
        self.assertTrue(ui.shutdown_armed)

    def test_map_drag_but_not_from_buttons(self):
        import mappage
        import screens
        ui = display.UI(FakeFeed(True))
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        ui.goto("map")
        ui.fingers([(100, 100)])
        ui.fingers([(130, 100)])                                   # drag right
        self.assertTrue(ui.map.panned)
        ui.fingers([])
        ui.map.recenter()
        x0, y0, _, _ = mappage.ZOOM_IN
        ui.fingers([(x0 + 2, y0 + 2)])                             # starts on "+": not a drag
        ui.fingers([(x0 - 40, y0 + 2)])
        self.assertFalse(ui.map.panned)
        ui.fingers([])
        ui.fingers([(100, 8)])                                     # starts on the tab bar
        ui.fingers([(150, 60)])
        self.assertFalse(ui.map.panned)
        ui.fingers([])
        ui.goto("gauges")
        ui.fingers([(100, 100)])
        ui.fingers([(130, 100)])                                   # other pages: nothing
        self.assertFalse(ui.map.panned)

    def test_only_shut_down_box_starts_countdown(self):
        import screens
        ui = display.UI(FakeFeed(True))
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        for page in display.PAGES:                                 # long press anywhere else: nothing
            ui.goto(page)
            ui.shutdown_armed = False
            ui.tap((120, 90))
            ui.stack.clear()                                       # (gauges: that opens GAS)
            self.assertIsNone(display.hold_progress(10.0, ui), page)
        ui.goto("status")
        ui.tap((150, 140))                                         # finger down on SHUT DOWN
        self.assertEqual(display.hold_progress(0.0, ui), 0.0)
        self.assertEqual(display.hold_progress(2.0, ui), 0.5)
        self.assertEqual(display.hold_progress(display.HOLD_FIRE, ui), 1.0)
        self.assertIsNone(display.hold_progress(None, ui))         # finger lifted


class ClaudePageTest(unittest.TestCase):
    class FakeAssistant:
        state, question, answer, status, level, logged_in, busy = "idle", "", "", "", 0.0, True, False
        approval = None
        speaker, spoken, talking = None, True, False

        def toggle_voice(self):
            self.calls.append("mode")

        def __init__(self):
            self.calls = []

        def ask_button(self):
            self.calls.append("ask")

        def new_chat(self):
            self.calls.append("new")

        def decide(self, allow):
            self.calls.append(("decide", allow))

        def check_login(self, force=False):
            return True

    def ui(self):
        import screens
        a = self.FakeAssistant()
        ui = display.UI(FakeFeed(True), a)
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        ui.page = display.PAGES.index("claude")
        return ui, a

    def test_buttons(self):
        ui, a = self.ui()
        ui.tap((150, 145))                       # ASK
        ui.tap((20, 145))                        # NEW
        ui.tap((100, 60))                        # conversation area: scroll, not page change
        self.assertEqual(a.calls, ["ask", "new"])
        self.assertEqual(display.PAGES[ui.page], "claude")
        ui.tap((215, 145))                       # right end of ASK
        self.assertEqual(a.calls, ["ask", "new", "ask"])
        ui.tap((20, 5))                          # INFO tab
        self.assertEqual(ui.page, 0)

    def test_approval_takes_over_every_page(self):
        import threading
        ui, a = self.ui()
        ui.page = 0
        a.approval = {"tool": "Bash", "detail": "sudo reboot", "deadline": time.monotonic() + 90,
                      "event": threading.Event(), "allow": False}
        self.assertEqual(ui.render().shape, (480, 800, 3))
        ui.tap((133, 140))                       # the gap between the buttons does nothing...
        ui.tap((120, 60))
        self.assertEqual(a.calls, [])
        ui.tap((180, 140))                       # ...ALLOW
        self.assertEqual(a.calls, [("decide", True)])
        self.assertEqual(ui.page, 0)

    def test_approval_cleared_mid_render(self):
        """Tapping ALLOW clears .approval from another thread while the screen draws."""
        import threading
        req = {"tool": "Bash", "detail": "curl -s http://127.0.0.1/api/sessions",
               "deadline": time.monotonic() + 90, "event": threading.Event(), "allow": True}

        class Clearing(self.FakeAssistant):
            reads = 0

            @property
            def approval(self):              # there on the first read, gone after
                self.reads += 1
                return req if self.reads == 1 else None

        ui, _ = self.ui()
        ui.assistant = Clearing()
        self.assertEqual(ui.render().shape, (480, 800, 3))

    def test_wrap_and_plain(self):
        lines = display.wrap(display.plain("**Hello** there, " + "x" * 50), 38)
        self.assertEqual(lines[0], "Hello there,")
        self.assertTrue(all(len(line) <= 38 for line in lines))

    def test_every_claude_state_renders(self):
        ui, a = self.ui()
        for state in ("idle", "listening", "transcribing", "thinking"):
            a.state, a.started = state, time.monotonic()
            self.assertEqual(ui.render().shape, (480, 800, 3))


class ApproveBridgeTest(unittest.TestCase):
    """approve_mcp.py <-> the screen's socket, the way Claude Code drives it."""

    def test_allow_and_deny(self):
        import json
        import socket
        import subprocess
        import threading
        with tempfile.TemporaryDirectory() as d:
            sock = os.path.join(d, "a.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock)
            srv.listen(2)
            answers = [True, False]

            def screen():
                for allow in answers:
                    c, _ = srv.accept()
                    req = json.loads(c.makefile().readline())
                    assert req["tool"] == "Bash"
                    c.sendall((json.dumps({"allow": allow, "reason": "" if allow else "no"}) + "\n").encode())
                    c.close()
            threading.Thread(target=screen, daemon=True).start()
            here = os.path.join(os.path.dirname(__file__), "..")
            env = dict(os.environ, DATALOGGER_APPROVE_SOCK=sock, HOME=d)
            p = subprocess.Popen([sys.executable, os.path.join(here, "approve_mcp.py")], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

            def call(i, msg):
                p.stdin.write(json.dumps(dict(jsonrpc="2.0", id=i, **msg)) + "\n")
                p.stdin.flush()
                return json.loads(p.stdout.readline())
            self.assertIn("tools", call(1, {"method": "initialize", "params": {}})["result"]["capabilities"])
            self.assertEqual(call(2, {"method": "tools/list"})["result"]["tools"][0]["name"], "approve")
            args = {"tool_name": "Bash", "input": {"command": "sudo reboot"}}
            for want in ("allow", "deny"):
                r = call(3, {"method": "tools/call", "params": {"name": "approve", "arguments": args}})
                d_ = json.loads(r["result"]["content"][0]["text"])
                self.assertEqual(d_["behavior"], want)
                if want == "allow":
                    self.assertEqual(d_["updatedInput"], args["input"])
            p.stdin.close()
            p.wait(5)
            srv.close()


if __name__ == "__main__":
    unittest.main()


class FakePlayer:
    item = {"name": "Todo es de color"}
    title, artist, album, device, volume = "Todo es de color", "Lole Y Manuel", "El origen", spotify.DEVICE, 40
    playing, shuffle, polled, doing, error, problem = True, False, True, "", "", ""
    duration, progress = 282000, 40000
    visible, heart, show_heart = False, False, True

    def __init__(self):
        self.calls = []
        for name in ("toggle", "next", "previous", "toggle_shuffle", "play_liked", "refresh", "like"):
            setattr(self, name, lambda name=name: self.calls.append(name))

    def play_playlist(self, pid, name="", shuffle=False):
        self.calls.append(("playlist", pid, shuffle))

    def play_song(self, pid, uri, title="", then=(), artists=""):
        self.calls.append(("song", pid, uri, list(then)))


class SpotifyPageTest(unittest.TestCase):
    def setUp(self):
        display._playlists, display._songs = None, {}        # nothing remembered from other tests

    def ui(self):
        import screens
        p = FakePlayer()
        ui = display.UI(FakeFeed(False), None, p)
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        ui.tap((tab_x("MUSIC"), 8))
        return ui, p

    def test_renders(self):
        ui, p = self.ui()
        self.assertTrue(p.visible)
        for state in ({}, {"item": None}, {"item": None, "polled": False}, {"error": "no internet", "problem": "no internet"},
                      {"title": "A very long song title that has to wrap onto two lines", "device": "Phone"},
                      {"heart": True}, {"heart": None}, {"item": {"name": "Pod", "type": "episode"}}):
            for k, v in state.items():
                setattr(p, k, v)
            self.assertEqual(ui.render().shape, (480, 800, 3))
        self.assertEqual(display.render(FakeFeed(True), display.PAGES.index("spotify")).shape, (480, 800, 3))

    def test_buttons(self):
        ui, p = self.ui()
        for xy in ((18, 140), (62, 140), (106, 140), (150, 140), (120, 80), (120, 40)):
            ui.tap(xy)                           # the bottom row; the rest of the page does nothing
        self.assertEqual(p.calls, ["refresh", "previous", "toggle", "next", "toggle_shuffle"])
        ui.tap((display.LW - 14, display.TOP + 28))   # the heart
        self.assertEqual(p.calls[-1], "like")
        del p.calls[-1]
        ui.tap((240, 140))                       # LISTS
        lists = ui.stack[-1]
        self.assertIsInstance(lists, display.PlaylistScreen)
        lists.status, lists.rows = "", [("liked", "LIKED SONGS"), ("abc", "Car Mix")]
        self.assertEqual(ui.render().shape, (480, 800, 3))
        import spotify
        pages = {0: (12, [(f"spotify:track:{n:022d}", f"Song {n}", "Band") for n in range(10)]),
                 10: (12, [(f"spotify:track:{n:022d}", f"Song {n}", "Band") for n in range(10, 12)])}
        asked = []

        def fake_songs(pid, offset=0):
            asked.append((pid, offset))
            return pages[offset]
        with mock.patch.object(spotify, "playlist_songs", fake_songs), \
                mock.patch.object(display.threading, "Thread", lambda target, daemon: mock.Mock(start=target)):
            ui.tap((100, 22 + 23 + 5))           # second row: opens the playlist, doesn't play it
            songs = ui.stack[-1]
            self.assertIsInstance(songs, display.SongsScreen)
            self.assertEqual(asked, [("abc", 0)])               # only the first page, only now
            self.assertEqual(ui.render().shape, (480, 800, 3))
            r = display.SongsScreen
            ui.tap((200, r.DOWN[1] + 5))                         # PAGE DOWN: songs 9-12, fetches the rest
            self.assertEqual(songs.top, 8)
            self.assertEqual(asked, [("abc", 0), ("abc", 10)])
            ui.tap((200, r.DOWN[1] + 5))                         # nothing past the end
            self.assertEqual(songs.top, 8)
            self.assertEqual(ui.render().shape, (480, 800, 3))
            ui.tap((10, r.UP[1] + 5))
            self.assertEqual(songs.top, 0)
            ui.tap((100, r.ROW_Y + 2 * r.ROW_H + 3))             # third song
        self.assertEqual(p.calls[-1][:3], ("song", "abc", "spotify:track:" + "2".zfill(22)))
        self.assertEqual(p.calls[-1][3][0], "spotify:track:" + "3".zfill(22))   # the rest follows it
        self.assertIs(ui.stack[-1], songs)                       # lit for a moment first...
        self.assertEqual(ui.render().shape, (480, 800, 3))
        with mock.patch.object(display.time, "monotonic", lambda: songs.picked[1] + r.PICK_SECS + 0.01):
            ui.render()
        self.assertEqual(ui.stack, [])                           # ...then back on the MUSIC page
        ui.tap((tab_x("AI"), 8))                 # leaving the page stops the fast poll
        self.assertFalse(p.visible)

    def test_playlist_opens_on_the_playing_song(self):
        import spotify
        pages = {o: (120, [(f"spotify:track:{n:022d}", f"Song {n}", "Band") for n in range(o, min(o + 50, 120))])
                 for o in (0, 50, 100)}
        asked = []

        def fake_songs(pid, offset=0):
            asked.append(offset)
            return pages[offset]
        p = FakePlayer()
        p.item = {"name": "Song 77", "id": "77".zfill(22)}       # the player's JSON: an id, no uri
        p.state = {"context": {"uri": "spotify:playlist:abc"}}
        with mock.patch.object(spotify, "playlist_songs", fake_songs), \
                mock.patch.object(display.threading, "Thread", lambda target, daemon: mock.Mock(start=target)):
            s = display.SongsScreen(p, "abc", "Car Mix")
            self.assertEqual(asked, [0, 50])                     # fetched on until song 77 turned up
            self.assertEqual(s.top, 72)                          # its page: songs 73-80
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (display.LW, display.LH), display.BG)
            s.render(img, ImageDraw.Draw(img))
            p.state = {"context": {"uri": "spotify:playlist:other"}}    # playing from elsewhere:
            asked.clear()
            display._songs.clear()
            s2 = display.SongsScreen(p, "abc", "Car Mix")
            self.assertEqual((asked, s2.top), ([0], 0))          # first page only, no hunting
            asked.clear()
            s3 = display.SongsScreen(p, "abc", "Car Mix")           # opened again: remembered
            self.assertEqual(asked, [])
            self.assertEqual(len(s3.songs), 50)
        display.SongsScreen.SEEK_PAGES, keep = 1, display.SongsScreen.SEEK_PAGES
        try:                                                     # and the hunt is capped
            p.state = {"context": {"uri": "spotify:playlist:abc"}}
            asked.clear()
            display._songs.clear()
            with mock.patch.object(spotify, "playlist_songs", fake_songs), \
                    mock.patch.object(display.threading, "Thread", lambda target, daemon: mock.Mock(start=target)):
                display.SongsScreen(p, "abc", "Car Mix")
            self.assertEqual(asked, [0])
        finally:
            display.SongsScreen.SEEK_PAGES = keep

    def test_suggest_button(self):
        ui, p = self.ui()
        p.item = {"name": "Todo es de color", "id": "a" * 22, "type": "track"}

        class Picker:
            busy, status, tapped = False, "", []

            def suggest(self, item):
                self.tapped.append(item["id"])
                self.busy, self.status = True, "PICKING 5 SONGS LIKE THIS ONE..."
        s = ui.suggester = Picker()
        self.assertEqual(ui.render().shape, (480, 800, 3))
        self.assertEqual(s.tapped, [])                  # showing a song does nothing
        ui.tap((190, 140))                              # SUGGEST
        self.assertEqual(s.tapped, ["a" * 22])
        self.assertEqual(ui.render().shape, (480, 800, 3))
        ui.suggester = None
        ui.tap((190, 140))                              # no suggester: nothing happens
        self.assertEqual(ui.render().shape, (480, 800, 3))

    def test_command_errors(self):
        import spotify
        self.assertTrue(spotify.command("dance").startswith("Error"))
        self.assertTrue(spotify.command("volume").startswith("Error"))
        self.assertTrue(spotify.command("play_songs").startswith("Error"))
        self.assertTrue(spotify.command("start", item_type="radio", query="x").startswith("Error"))
        self.assertTrue(spotify.command("start", item_type="song").startswith("Error"))
        self.assertEqual(spotify.summary(None), "Nothing is playing.")
        self.assertEqual(spotify.mmss(61000), "1:01")

    def test_visualiser(self):
        v = display.Visualiser()
        t = np.arange(display.AudioTap.N) / display.AudioTap.RATE
        bass = (0.5 * np.sin(2 * np.pi * 60 * t)).astype(np.float32)
        for n in range(10):
            v.step(bass, now=n / 15)
        self.assertGreater(v.h[1], 0.9)              # 60 Hz lands in the second bar
        self.assertEqual(max(v.h[6:]), 0)            # nothing up the top
        treble = (0.5 * np.sin(2 * np.pi * 8000 * t)).astype(np.float32)
        for n in range(10, 30):
            v.step(treble, now=n / 15)
        self.assertGreater(v.h[-2], 0.9)
        self.assertLess(v.h[1], 0.1)                 # bass bar has fallen back
        for n in range(30, 60):
            v.step(None, now=n / 15)
        self.assertEqual(max(v.h), 0)                # paused: bars empty

    def test_accents_drawn_as_plain_letters(self):
        import pixels
        self.assertEqual("".join(map(pixels.plain_char, "día Ñ’")), "dia N'")


class TerminalTest(unittest.TestCase):
    def test_keys(self):
        import terminal
        k = terminal.key_bytes
        self.assertEqual(k("A", set()), b"a")
        self.assertEqual(k("A", {"shift"}), b"A")
        self.assertEqual(k("A", set(), caps=True), b"A")
        self.assertEqual(k("1", {"shift"}, caps=True), b"!")
        self.assertEqual(k("C", {"ctrl"}), b"\x03")
        self.assertEqual(k("B", {"alt"}), b"\x1bb")
        self.assertEqual(k("ENTER", set()), b"\r")
        self.assertEqual(k("UP", set()), b"\x1b[A")
        self.assertEqual(k("UP", set(), app_cursor=True), b"\x1bOA")
        self.assertEqual(k("PAGEUP", {"shift"}), ("scroll", True))
        self.assertIsNone(k("VOLUMEUP", set()))

    def test_modifiers_shared_and_unstuck(self):
        import threading
        import terminal

        class ec:                                  # the few evdev codes the test needs
            KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_A = 29, 42, 30

        class Dev:
            def __init__(self, down):
                self.down = down

            def active_keys(self):
                return list(self.down)
        kb = object.__new__(terminal.Keyboards)    # no scan thread: it would grab the real keyboard
        logs = []
        kb.log, kb._lock, kb.caps = logs.append, threading.Lock(), False
        one, two = Dev([ec.KEY_LEFTSHIFT]), Dev([])
        kb.devs = {"/dev/input/event1": one, "/dev/input/event5": two}
        kb.mods = {"LEFTSHIFT": "shift"}           # went down on one interface...
        self.assertEqual(kb.held_mods(ec), {"shift"})   # ...and counts for keys from the other
        kb.mods["LEFTCTRL"] = "ctrl"               # its release got lost: the kernel says it's up
        self.assertEqual(kb.held_mods(ec), {"shift"})
        self.assertIn("LEFTCTRL was stuck", logs[0])
        self.assertEqual(kb.mods, {"LEFTSHIFT": "shift"})

    def test_finger_drag_scrolls(self):
        import screens

        class Term:
            def __init__(self):
                self.lines = []

            def scroll_lines(self, n):
                self.lines.append(n)
        t = Term()
        ui = display.UI(FakeFeed(False), term=t)
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        ui.page = display.PAGES.index("terminal")
        row = display.TERM_ROW
        for y in (40, 40 + row * 0.5, 40 + row * 3.2, 40 + row * 1.1):  # down 3.2 lines, back to 1.1
            ui.fingers([(100, y)])
        ui.fingers([])
        ui.fingers([(100, 10)])                  # on the tab bar: not a drag
        ui.fingers([(100, 90)])
        self.assertEqual(t.lines, [3, -1])                     # 1.9 lines back up = 1 whole line
        ui.tap((100, 90))                        # taps don't scroll any more

    def test_shell_on_the_page(self):
        import terminal
        t = terminal.Terminal(tmux="datalogger-test-none")     # no such tmux: the plain-shell fallback
        ui = display.UI(FakeFeed(False), term=t)
        ui.goto("terminal")
        try:
            for c in "echo hi-$((6*7))":
                name = {"-": "MINUS", "$": "4", "(": "9", ")": "0", "*": "8", " ": "SPACE"}.get(c, c.upper())
                ui.key(name, frozenset({"shift"}) if c in "$()*" else frozenset(), False)
            ui.key("ENTER", frozenset(), False)
            deadline = time.time() + 10
            while time.time() < deadline and not any("hi-42" in l for l in t.screen.display[1:]):
                time.sleep(0.1)
            self.assertTrue(any(l.startswith("hi-42") for l in t.screen.display), t.screen.display)
            display.render(ui.feed, ui.page, ui)
            ui.goto("spotify")
            ui.key("X", frozenset(), False)              # other pages: keys ignored
            time.sleep(0.3)
            self.assertFalse(any("x" in l for l in t.screen.display[-3:] if "hi" not in l and "$" not in l))
        finally:
            t.write(b"exit\r")
            deadline = time.time() + 5
            while t.alive and time.time() < deadline:
                time.sleep(0.1)
        self.assertTrue(t.ended)


class DriveTimeTest(unittest.TestCase):
    def test_totals(self):
        now = time.mktime((2026, 10, 14, 18, 0, 0, 0, 0, -1))       # a Wednesday
        day = 86400
        ss = [{"start": now - 3600, "end": now - 600, "km": 30},       # today, 50 min
              {"start": now - 2 * day, "end": now - 2 * day + 1200, "km": 10},   # Monday, 20 min
              {"start": now - 10 * day, "end": now - 10 * day + 600, "km": 5},   # 4 Oct, 10 min
              {"start": now - 40 * day, "end": now - 40 * day + 300, "km": None}]  # September, 5 min
        self.assertEqual(display.drive_seconds(ss, "last", now), 3000)
        self.assertEqual(display.drive_seconds(ss, "today", now), 3000)
        self.assertEqual(display.drive_seconds(ss, "week", now), 4200)
        self.assertEqual(display.drive_seconds(ss, "month", now), 4800)
        self.assertEqual(display.drive_seconds(ss, "year", now), 5100)
        self.assertIsNone(display.drive_seconds([], "last", now))
        self.assertEqual(display.drive_km(ss, "last", now), 30)
        self.assertEqual(display.drive_km(ss, "week", now), 40)
        self.assertEqual(display.drive_km(ss, "year", now), 45)                 # unknown km counts as nothing
        monday = display.period_start("week", now)
        across = [{"start": monday - 1800, "end": monday + 1800, "km": 20}]   # over midnight into Monday
        self.assertAlmostEqual(display.drive_km(across, "week", now), 10)      # only the Monday half
        self.assertIsNone(display.drive_km([], "last", now))
        self.assertEqual([display.distance(k) for k in (0.6, 20.1, 300)],
                         ["0.4mi" if display.MPH else "0.6km", "12.5mi" if display.MPH else "20.1km",
                          "186mi" if display.MPH else "300km"])
        self.assertEqual([display.duration(x) for x in (45, 180, 720, 3600, 14520, 187260)], ["45s", "3m", "12m", "1h", "4h2m", "2d4h1m"])

    def test_tap_cycles(self):
        import screens
        ui = display.UI(FakeFeed(False))
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        names = []
        for _ in range(len(display.DRIVE_MODES) + 1):
            names.append(display.DRIVE_MODES[ui.drive_mode][1])
            display.render(ui.feed, ui.page, ui)
            x0, y0, x1, y1 = display.DRIVE_TAP
            ui.tap(((x0 + x1) // 2, (y0 + y1) // 2))
        self.assertEqual(names, ["LAST DRIVE", "TODAY", "THIS WEEK", "THIS MONTH", "THIS YEAR", "LAST DRIVE"])
        self.assertEqual(display.PAGES[ui.page], "gauges")             # the box doesn't flip the page


class LayoutTest(unittest.TestCase):
    class Settings:
        def __init__(self):
            self.settings = {"layout": "classic"}

        def change(self, **kw):
            self.settings.update(kw)

    def test_every_layout_draws(self):
        for on in (True, False):
            feed = FakeFeed(on)
            ui = display.UI(feed, None, FakePlayer())
            ui.screen = self.Settings()
            for key, name, about in display.LAYOUTS:
                ui.screen.settings["layout"] = key
                self.assertEqual(display.render(feed, 0, ui).shape, (480, 800, 3), key)
        ui.screen.settings["layout"] = "nonsense"
        self.assertEqual(display.drive_layout(ui), "classic")          # unknown: the original

    def test_layout_taps_and_choosing(self):
        import screens
        feed = FakeFeed(True)
        ui = display.UI(feed, None, FakePlayer())
        ui.screen = self.Settings()
        ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        ui.page = display.PAGES.index("status")
        ui.tap(display.SCREEN_BTN[:2])                                  # layouts live under SCREEN now
        ui.tap(ui.stack[-1].LAYOUT[:2])
        chooser = ui.stack[-1]
        self.assertIsInstance(chooser, display.LayoutScreen)
        self.assertEqual(ui.render().shape, (480, 800, 3))
        ui.tap((100, chooser.ROW_Y + 4 * chooser.ROW_H + 5))          # MAP + MUSIC
        self.assertEqual(ui.screen.settings["layout"], "mapmusic")
        self.assertEqual(type(ui.stack[-1]).__name__, "ScreenSettings")   # back to SCREEN
        ui.stack.clear()
        ui.page = 0
        ui.tap((60, 140))                                               # the music strip
        self.assertEqual(display.PAGES[ui.page], "spotify")
        ui.page = 0
        ui.tap((60, 60))                                                # the map
        self.assertEqual(display.PAGES[ui.page], "map")
        ui.page = 0
        ui.screen.settings["layout"] = "bars"
        ui.tap((20, 125))                                               # MPG: the GAS screen
        self.assertIsInstance(ui.stack[-1], gas.GasScreen)
        ui.stack.clear()
        ui.tap((200, 80))                                               # elsewhere: flips DRIVE
        self.assertEqual(display.PAGES[ui.page], "more")


class TasteTest(unittest.TestCase):
    def setUp(self):
        import taste
        self.dir = tempfile.mkdtemp()
        self.saved = taste.DIR, taste.NOTES, taste.SKIPS, taste.SPOTIFY_CACHE, taste.LIKES, taste.PLAYS
        taste.DIR = self.dir
        taste.NOTES, taste.SKIPS, taste.SPOTIFY_CACHE, taste.LIKES, taste.PLAYS = (
            os.path.join(self.dir, f) for f in ("n.json", "s.jsonl", "sp.json", "l.jsonl", "p.jsonl"))

    def tearDown(self):
        import taste
        taste.DIR, taste.NOTES, taste.SKIPS, taste.SPOTIFY_CACHE, taste.LIKES, taste.PLAYS = self.saved

    def test_hearts_and_plays(self):
        import taste
        song = {"name": "Harvest Moon", "artists": [{"name": "Neil Young"}], "album": {"name": "Harvest Moon"},
                "duration_ms": 300000}
        self.assertTrue(taste.record_like(song, "screen"))
        self.assertFalse(taste.record_like({}, "screen"))
        self.assertTrue(taste.record_play(song, 280000))            # in its last 30 s: played through
        self.assertTrue(taste.record_play(song, 271000))            # past 90%
        self.assertFalse(taste.record_play(song, 120000))           # changed mid-song: not a play
        self.assertEqual(len(taste.likes()), 1)
        self.assertEqual(len(taste.plays()), 2)
        p = taste.profile()
        for want in ("Songs they hearted", "Harvest Moon - Neil Young", "play through most", "Neil Young (2)"):
            self.assertIn(want, p)
        self.assertIn("Songs they hearted lately: Harvest Moon - Neil Young.", taste.prompt_note())

    def test_player_heart_and_play_through(self):
        import spotify
        import taste
        saved, looked = [], []
        a = {"id": "a" * 22, "type": "track", "name": "A", "artists": [{"name": "X"}], "duration_ms": 200000}
        b = dict(a, id="b" * 22, name="B")
        states = iter([{"item": a, "progress_ms": 195000, "is_playing": False},
                       {"item": b, "progress_ms": 1000, "is_playing": True}])
        with mock.patch.object(spotify.threading, "Thread"), \
                mock.patch.object(spotify, "is_liked", lambda t: looked.append(t) or False), \
                mock.patch.object(spotify, "save_liked", saved.append), \
                mock.patch.object(spotify, "get", lambda *a, **k: next(states)):
            pl = spotify.Player()
            for _ in range(2):                                      # what one round of _poll does
                before = pl.state and dict(pl.state, progress_ms=pl.progress)
                pl.state, pl.at = spotify.get("playback"), spotify.time.monotonic()
                pl._song_changed(before)
            self.assertEqual(looked, [a["id"], b["id"]])
            self.assertIs(pl.heart, False)
            self.assertEqual([r["track"] for r in taste.plays()], ["A"])      # A ran to its end
            with mock.patch.object(spotify.threading, "Thread", lambda target, daemon: mock.Mock(start=target)):
                pl.like()
                pl.like()                                           # already liked: no second save
            self.assertEqual(saved, [b["id"]])
            self.assertIs(pl.heart, True)
            self.assertEqual([(r["track"], r["by"]) for r in taste.likes()], [("B", "screen")])

    def test_notes_skips_profile(self):
        import taste
        self.assertIn("Saved", taste.remember("like", "Elliott Smith"))
        taste.remember("dislike", "country")
        taste.remember("like", "country")                      # changed his mind: moves, not duplicated
        n = taste.notes()
        self.assertEqual([e["text"] for e in n["likes"]], ["Elliott Smith", "country"])
        self.assertEqual(n["dislikes"], [])
        self.assertIn("Forgot: country", taste.forget("COUNTRY"))
        song = {"item": {"name": "Song", "artists": [{"name": "Band"}], "duration_ms": 200000}}
        self.assertTrue(taste.record_skip(dict(song, progress_ms=20000), "screen"))
        self.assertTrue(taste.record_skip(dict(song, progress_ms=50000), "claude"))
        self.assertFalse(taste.record_skip(dict(song, progress_ms=180000), "screen"))   # near the end: not a skip
        fetched = {"top_artists_recent": ["Xavi"], "liked_recent": ["Dance II - Someone"]}
        p = taste.profile(fetch=lambda: fetched)
        for want in ("Elliott Smith", "Band (2)", "Song - Band at 20s", "Xavi", "Dance II"):
            self.assertIn(want, p)
        def offline():
            raise RuntimeError("no internet")
        self.assertIn("Xavi", taste.profile(fetch=offline))      # cached copy
        with mock.patch.object(taste.config, "DRIVER", "Sam"):
            self.assertEqual(taste.prompt_note(), "Sam likes: Elliott Smith.")

    def test_mcp_tool(self):
        import approve_mcp
        self.assertIn("Saved under dislikes", approve_mcp.taste_call(
            {"action": "remember", "kind": "dislike", "text": "Nickelback"}))
        self.assertIn("Nickelback", approve_mcp.taste_call({"action": "read"}).split("From Spotify")[0])
