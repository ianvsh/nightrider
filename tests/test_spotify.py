"""spotify.Watchdog decisions, with the player, network and systemctl faked."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import spotify  # noqa: E402

HEALTHY = [{"name": spotify.DEVICE, "id": "x"}]


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.world = {"login": False, "up": True, "service": "active", "devices": HEALTHY, "online": True}
        self.restarts = 0
        w = self.world

        def get(key, timeout=15):
            if isinstance(w["devices"], Exception):
                raise w["devices"]
            return w["devices"]

        def restart(reason="", log=None):
            self.restarts += 1
            spotify.last_restart = self.now
            return True
        patches = [
            mock.patch.object(spotify.time, "monotonic", lambda: self.now),
            mock.patch.object(spotify, "waiting_for_login", lambda: w["login"]),
            mock.patch.object(spotify, "player_up", lambda: w["up"]),
            mock.patch.object(spotify, "service_state", lambda: w["service"]),
            mock.patch.object(spotify, "online", lambda: w["online"]),
            mock.patch.object(spotify, "get", get),
            mock.patch.object(spotify, "restart", restart),
            mock.patch.object(spotify, "last_restart", 0.0),
            mock.patch.object(spotify, "service_age", lambda: w.get("age", 1e6)),
            mock.patch.object(spotify, "network_id", lambda: w.get("net", "wlan0:0100A8C0")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.dog = spotify.Watchdog(log=lambda msg: None)

    def tick(self, secs=15, fine=False):
        self.now += secs
        return self.dog.check(fine)

    def test_healthy_never_restarts(self):
        for _ in range(20):
            self.assertFalse(self.tick(fine=True))
            self.assertFalse(self.tick(fine=False))
        self.assertEqual((self.restarts, self.dog.status), (0, ""))

    def test_lost_network_then_back(self):
        """The 2026-10-16 09:53 case: no devices; offline -> wait; online -> restart."""
        self.world.update(devices=[], online=False)
        for _ in range(10):
            self.tick()
        self.assertEqual((self.restarts, self.dog.status), (0, "NO INTERNET"))
        self.world["online"] = True
        self.tick()
        self.assertEqual(self.restarts, 0)        # one strike isn't enough
        self.assertTrue(self.tick())
        self.assertEqual(self.restarts, 1)
        self.world["devices"] = HEALTHY
        for _ in range(6):
            self.tick()
        self.assertEqual((self.restarts, self.dog.status), (1, ""))

    def test_login_wait_restarts_at_once_then_gives_up(self):
        self.world.update(login=True, up=False)
        self.assertTrue(self.tick())
        for _ in range(200):
            self.tick(30)
        self.assertEqual(self.restarts, spotify.Watchdog.LOGINS)
        self.assertIn("LOGIN", self.dog.status)

    def test_backoff_grows(self):
        self.world["devices"] = []
        times = []
        for _ in range(400):
            if self.tick(10):
                times.append(self.now)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(b >= a for a, b in zip(gaps, gaps[1:])))
        self.assertLessEqual(gaps[-1], spotify.Watchdog.MAX_BACKOFF + 30)

    def test_waiting_for_internet_at_start_is_left_alone(self):
        self.world.update(up=False, service="activating")
        for _ in range(20):
            self.tick()
        self.assertEqual(self.restarts, 0)
        self.assertIn("WAITING", self.dog.status)

    def test_new_network_restarts_a_dead_player_at_once(self):
        """Getting in the car: home Wi-Fi -> phone hotspot. The player that started at home
        is dead; it's restarted on the first check, not after a minute and two strikes."""
        self.tick(fine=True)
        self.world.update(devices=[], net="wlan0:010A14AC")
        self.assertTrue(self.tick(1))
        self.assertEqual(self.restarts, 1)

    def test_new_network_ignores_old_backoff(self):
        self.world["devices"] = []
        restarts = sum(self.tick(10) for _ in range(40))             # backoff has grown
        self.assertGreater(self.dog.backoff, spotify.Watchdog.GRACE)
        self.world["net"] = "wlan0:010A14AC"
        self.assertTrue(self.tick(1))
        self.assertEqual(self.restarts, restarts + 1)

    def test_player_just_started_on_new_network_is_left_to_warm_up(self):
        self.tick(fine=True)
        self.world.update(devices=[], net="wlan0:010A14AC", age=2)    # wait-net let it start 2 s ago
        for _ in range(3):
            self.assertFalse(self.tick(1))
        self.world.update(devices=HEALTHY)
        for _ in range(5):
            self.assertFalse(self.tick(5))
        self.assertEqual(self.restarts, 0)

    def test_music_playing_clears_reconnecting(self):
        self.world["devices"] = []
        self.tick()
        self.assertTrue(self.tick())
        self.assertEqual(self.dog.status, "RECONNECTING...")
        self.dog.playing_here()                      # a poll: playing on 924S
        self.assertEqual(self.dog.status, "")

    def test_cli_errors_count_as_sick(self):
        self.world["devices"] = RuntimeError("Spotify didn't answer")
        self.tick()
        self.assertTrue(self.tick())


class PortTest(unittest.TestCase):
    def test_listening_parses_proc(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix="tcp", delete=False) as f:
            f.write("  sl  local_address rem_address   st\n"
                    "   0: 0100007F:231D 00000000:0000 0A 00000000\n")
        self.addCleanup(os.remove, f.name)
        self.assertTrue(spotify._listening(8989, (f.name,), state="0A"))
        self.assertFalse(spotify._listening(8989, (f.name,), state="01"))
        self.assertFalse(spotify._listening(8080, (f.name,)))


if __name__ == "__main__":
    unittest.main()


class PickedSongShowsAtOnceTest(unittest.TestCase):
    def test_pick(self):
        replies = iter([{"item": {"id": "old", "name": "Old Song"}, "is_playing": True},     # Spotify not caught up
                        {"item": {"id": "new1234567890123456789", "name": "Glycerine - Remastered",
                                  "duration_ms": 266000}, "is_playing": True}])
        with mock.patch.object(spotify.threading, "Thread"):
            p = spotify.Player()
        p.state = {"item": {"id": "old", "name": "Old Song"}, "is_playing": True}
        with mock.patch.object(spotify.threading, "Thread"):                # the play command itself not run
            p.play_song("liked", "spotify:track:new1234567890123456789", "Glycerine - Remastered", (), "Bush")
        self.assertEqual((p.title, p.artist), ("Glycerine - Remastered", "Bush"))     # at once
        with mock.patch.object(spotify, "get", lambda *a, **k: next(replies)):
            def one_poll():
                p._kick.set()
                with mock.patch.object(p._kick, "wait", side_effect=StopIteration):
                    try:
                        p._poll()
                    except StopIteration:
                        pass
            one_poll()
            self.assertEqual(p.title, "Glycerine - Remastered")             # stale reply ignored
            one_poll()
        self.assertEqual(p.duration, 266000)                                # the real state took over
        self.assertEqual(p._expect, (None, 0))


class BridgeTest(unittest.TestCase):
    """The Router keeps the mix flowing to the stereo by reopening the bridge -
    never by restarting Spotify (2026-11-15: restarts got the app blocked)."""

    def router(self, alive):
        import bt
        r = object.__new__(bt.Router)
        r.log, r.route, r._bridge_pcm, r._bridge_fails = (lambda m: None), "bluetooth", "/pcm1", 0
        started = []

        class B:
            started_at = 0.0

            def alive(self):
                return alive[0]

            def start(self, out):
                started.append(out)
                self.started_at = now[0]
        now = [100.0]
        r.bridge = B()
        return r, started, now

    def test_reopens_dead_bridge_with_backoff_and_no_spotify_restart(self):
        import bt
        alive = [True]
        r, started, now = self.router(alive)
        with mock.patch.object(bt.time, "monotonic", lambda: now[0]), \
                mock.patch.object(spotify, "restart", side_effect=AssertionError("restarted Spotify")):
            r._check_bridge("/pcm1")
            self.assertEqual(started, [])                       # flowing: nothing to do
            alive[0] = False
            r._check_bridge("/pcm1")
            self.assertEqual(started, ["tap_bt"])               # died: reopened at once
            now[0] += 2
            r._check_bridge("/pcm1")
            self.assertEqual(len(started), 1)                   # died again at once: backs off
            now[0] += 10
            r._check_bridge("/pcm1")
            self.assertEqual(len(started), 2)

    def test_new_stereo_stream_reopens(self):
        import bt
        r, started, now = self.router([True])
        with mock.patch.object(bt.time, "monotonic", lambda: now[0]):
            r._check_bridge("/pcm2")                            # stereo reconnected on a new stream
        self.assertEqual(started, ["tap_bt"])
        self.assertEqual(r._bridge_pcm, "/pcm2")


class StuckPlayerTest(unittest.TestCase):
    """2026-11-16: "playing" on 924S but the player dead and silent; the device list fooled
    the Watchdog. The Router restarts it from what the bridge hears."""

    def setUp(self):
        import bt
        self.bt = bt
        r = object.__new__(bt.Router)
        r.log, r.switching, r._stuck_since, r._playing_since = (lambda m: None), False, None, None
        self.now = [1000.0]

        class B:
            started_at, heard_at = 0.0, 0.0

            def alive(self):
                return True
        r.bridge = B()
        now = self.now

        class P:
            playing, device, error = True, spotify.DEVICE, ""
            state = {"item": {"duration_ms": 200000}, "progress_ms": 1000}

            @property
            def at(self):
                return now[0]
        r.player = P()
        self.r = r
        self.restarts = []
        for p in (mock.patch.object(bt.time, "monotonic", lambda: self.now[0]),
                  mock.patch.object(spotify, "online", lambda: True),
                  mock.patch.object(spotify, "restart", lambda why, log: self.restarts.append(why) or True),
                  mock.patch.object(spotify, "resume_here", lambda wait: None)):
            p.start()
            self.addCleanup(p.stop)

    def steps(self, n):
        for _ in range(n):
            self.r._check_stuck()
            self.now[0] += 5

    def test_sound_flowing_never_restarts(self):
        for _ in range(20):
            self.r.bridge.heard_at = self.now[0]
            self.steps(1)
        self.assertEqual(self.restarts, [])

    def test_silent_while_playing_restarts_once_it_has_been_long_enough(self):
        self.steps(5)                                   # 25 s: just started playing - wait
        self.assertEqual(self.restarts, [])
        self.steps(3)
        self.assertEqual(self.restarts, ["playing but silent"])

    def test_resumed_after_a_pause_is_not_stuck(self):
        self.r.player.playing = False
        self.steps(20)                                  # paused (e.g. the voice): silence is right
        self.r.player.playing = True
        self.steps(2)
        self.assertEqual(self.restarts, [])

    def test_position_past_the_end(self):
        self.r.player.state = {"item": {"duration_ms": 191106}, "progress_ms": 373045}
        self.r.bridge.heard_at = 1e12                   # (even with sound in the channel)
        self.steps(2)
        self.assertEqual(len(self.restarts), 1)


class RestartBudgetTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.ran = []
        for p in (mock.patch.object(spotify, "RESTART_FILE", os.path.join(d, "restarts")),
                  mock.patch.object(spotify, "BLOCK_FILE", os.path.join(d, "blocked")),
                  mock.patch.object(spotify.subprocess, "run", lambda *a, **k: self.ran.append(a))):
            p.start()
            self.addCleanup(p.stop)
        self.dir = d

    def test_at_most_a_few_an_hour(self):
        t = [1_800_000_000.0]
        with mock.patch.object(spotify.time, "time", lambda: t[0]):
            got = [spotify.restart("test", log=lambda m: None) for _ in range(6)]
            self.assertEqual(got, [True] * spotify.RESTARTS_PER_HOUR + [False] * (6 - spotify.RESTARTS_PER_HOUR))
            self.assertEqual(len(self.ran), spotify.RESTARTS_PER_HOUR)
            t[0] += 3601
            self.assertTrue(spotify.restart("test", log=lambda m: None))     # an hour on: allowed again

    def test_none_while_spotify_blocks_us(self):
        with open(spotify.BLOCK_FILE, "w") as f:
            f.write(f"{spotify.time.time() + 600:.0f}")
        said = []
        self.assertFalse(spotify.restart("test", log=said.append))
        self.assertEqual(self.ran, [])
        self.assertIn("wait until", said[0])


class WantedPlayTest(unittest.TestCase):
    def test_play_tapped_before_ready_starts_once_ready(self):
        with mock.patch.object(spotify.threading, "Thread"):
            p = spotify.Player()
        ran = []

        class T:                                        # threads run at once
            def __init__(self, target, daemon=None):
                self.target = target

            def start(self):
                self.target()
        devices = []

        def fn():
            ran.append(1)
            if not devices:
                raise RuntimeError("the car's Spotify player isn't running (spotify.service)")
        with mock.patch.object(spotify.threading, "Thread", T), \
                mock.patch.object(spotify.time, "sleep", lambda s: None), \
                mock.patch.object(spotify, "get", lambda key, timeout=15: devices):
            p._do("STARTING LIKED SONGS...", fn, play=True)
            self.assertIsNotNone(p._wanted)             # kept for later
            p._want_tried = -1e9
            p._play_wanted()                            # still not up: not tried again
            self.assertEqual(len(ran), 1)
            devices.append({"name": spotify.DEVICE})
            p._want_tried = -1e9
            p._play_wanted()
            self.assertEqual(len(ran), 2)
            self.assertIsNone(p._wanted)                # played: done

    def test_other_errors_arent_retried(self):
        with mock.patch.object(spotify.threading, "Thread"):
            p = spotify.Player()

        class T:
            def __init__(self, target, daemon=None):
                self.target = target

            def start(self):
                self.target()

        def fn():
            raise RuntimeError("Spotify said 404: no such playlist")
        with mock.patch.object(spotify.threading, "Thread", T), mock.patch.object(spotify.time, "sleep", lambda s: None):
            p._do("", fn, play=True)
        self.assertIsNone(p._wanted)
