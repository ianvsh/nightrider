"""SUGGEST (suggest.py) with Claude and Spotify replaced, and its taste log."""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spotify  # noqa: E402
import suggest  # noqa: E402
import taste  # noqa: E402

SONG = {"name": "Harvest Moon", "id": "h" * 22, "type": "track", "artists": [{"name": "Neil Young"}],
        "album": {"name": "Harvest Moon"}, "duration_ms": 300000}


class SuggestTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.block, spotify.BLOCK_FILE = spotify.BLOCK_FILE, os.path.join(self.dir, "blocked")   # not the real one
        self.pend, spotify.PENDING_LIKES = spotify.PENDING_LIKES, os.path.join(self.dir, "pending.json")
        self.saved = {k: getattr(taste, k) for k in ("DIR", "NOTES", "SKIPS", "SPOTIFY_CACHE", "LIKES", "PLAYS", "SUGGESTED")}
        taste.DIR = self.dir
        for k in self.saved:
            if k != "DIR":
                setattr(taste, k, os.path.join(self.dir, k.lower()))
        self.asked, self.queued = [], []
        self.gate = threading.Event()

        def ask(prompt):
            self.asked.append(prompt)
            self.gate.wait(5)                               # Claude "thinking"
            return ('Here you go: ["Old Man - Neil Young", "Harvest Moon - Neil Young", "Pink Moon - Nick Drake", '
                    '"Nowhere To Be Found - Nobody", "Helpless - CSNY", "Wild Horses - The Rolling Stones", '
                    '"Into the Mystic - Van Morrison", "Fade Into You - Mazzy Star"]')

        def find(songs):
            ids = {"Harvest Moon - Neil Young": "h" * 22, "Nowhere To Be Found - Nobody": None}
            return [(ids.get(s, s[:22].ljust(22, "x")), s) for s in songs]
        self.s = suggest.Suggester(log=lambda m: None, ask=ask, find=find, queue=self.queued.append)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(taste, k, v)
        spotify.BLOCK_FILE, spotify.PENDING_LIKES = self.block, self.pend

    def wait(self, cond, secs=5):
        end = time.time() + secs
        while time.time() < end and not cond():
            time.sleep(0.02)
        self.assertTrue(cond())

    def test_tap_picks_and_queues(self):
        self.s.suggest(SONG)
        self.wait(lambda: self.s.busy)
        self.assertIn("PICKING", self.s.status)
        self.s.suggest(SONG)                                 # a second tap while working: ignored
        self.gate.set()
        self.wait(lambda: self.queued)
        self.assertEqual(len(self.asked), 1)
        self.assertIn('"Harvest Moon" by Neil Young', self.asked[0])
        ids = self.queued[0]
        self.assertEqual(len(ids), 5)
        self.assertNotIn("h" * 22, ids)                      # never the song that's playing
        self.wait(lambda: self.s.status.startswith("QUEUED 5"))
        self.assertIn("Old Man", self.s.status)
        self.assertFalse(self.s.busy)
        self.assertEqual(len(taste.suggested()), 5)          # logged for the taste file

    def test_nothing_without_a_tap(self):
        self.assertFalse(hasattr(self.s, "prefetch"))        # no picking ahead (got us blocked)
        time.sleep(0.1)
        self.assertEqual(self.asked, [])

    def test_reactions_to_picks_are_tracked(self):
        taste.record_suggested(SONG, [("o" * 22, "Old Man - Neil Young")])
        played = {"name": "Old Man", "id": "o" * 22, "artists": [{"name": "Neil Young"}], "duration_ms": 200000}
        self.assertTrue(taste.record_play(played, 195000))
        self.assertTrue(taste.plays()[-1]["suggested"])
        self.assertIn("o" * 22, taste.recent_ids())
        self.assertIn("played through: Old Man - Neil Young", taste.suggestion_brief())
        self.assertIn("SUGGEST picks: 1 played through", taste.profile())

    def test_spotify_limit_is_honoured(self):
        import io
        import urllib.error
        calls = []

        def refuse(req, timeout=15):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "600"}, io.BytesIO(b""))
        with mock.patch.object(spotify.urllib.request, "urlopen", refuse):
            with self.assertRaises(spotify.Blocked):
                spotify.web("GET", "search", "tok", q="x")
            with self.assertRaises(spotify.Blocked):          # stays away: no second call goes out
                spotify.web("GET", "search", "tok", q="y")
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(spotify.blocked_until(), time.time() + 600, delta=5)
        self.s.suggest(SONG)                                   # no picking while blocked
        self.assertIn("BLOCKED TILL", self.s.status)
        self.assertEqual(self.asked, [])

    def test_hearts_wait_out_a_block(self):
        spotify._save_pending([{"id": "a" * 22, "name": "Linger"}, {"id": "b" * 22, "name": "Chains"}])
        saved = []
        with open(spotify.BLOCK_FILE, "w") as f:
            f.write(str(time.time() + 600))
        with mock.patch.object(spotify, "save_liked", saved.append):
            self.assertEqual(spotify.flush_pending_likes(lambda m: None), 2)    # still blocked: kept
            os.remove(spotify.BLOCK_FILE)
            self.assertEqual(spotify.flush_pending_likes(lambda m: None), 0)    # block over: saved
        self.assertEqual(saved, ["a" * 22, "b" * 22])
        self.assertEqual(spotify.pending_likes(), [])

    def test_parse(self):
        self.assertEqual(suggest.parse_songs('["A - B", "C - D"]'), ["A - B", "C - D"])
        self.assertEqual(suggest.parse_songs("1. A - B\n2) C - D\nnoise"), ["A - B", "C - D"])


if __name__ == "__main__":
    unittest.main()
