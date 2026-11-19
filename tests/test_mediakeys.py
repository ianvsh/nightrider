"""The stereo's buttons (mediakeys.py) -> the player."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mediakeys  # noqa: E402


class Player:
    def __init__(self, playing=False):
        self.playing, self.calls = playing, []

    def toggle(self):
        self.calls.append("toggle")
        self.playing = not self.playing

    def next(self):
        self.calls.append("next")

    def previous(self):
        self.calls.append("previous")


class MediaKeysTest(unittest.TestCase):
    def test_buttons(self):
        p = Player()
        keys = mediakeys.MediaKeys(p, log=lambda m: None, scan=False)
        t = [100.0]
        with mock.patch.object(mediakeys.time, "monotonic", lambda: t[0]):
            for key in ("FASTFORWARD", "NEXTSONG", "PLAYPAUSE", "PLAY", "PAUSE", "PAUSE", "REWIND", "VOLUMEUP"):
                t[0] += 1
                keys.act(key, "TR7412UB-OR (AVRCP)")
            t[0] += 0.1
            keys.act("PAUSE")                          # sent twice in a row by the stereo: once
        # FF and NEXT = next; PLAYPAUSE toggles (now playing); PLAY while playing does nothing;
        # PAUSE pauses, PAUSE again does nothing; REWIND = back; volume isn't ours
        self.assertEqual(p.calls, ["next", "next", "toggle", "toggle", "previous"])
        self.assertFalse(p.playing)


if __name__ == "__main__":
    unittest.main()
