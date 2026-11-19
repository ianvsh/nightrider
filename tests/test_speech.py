"""speech.py text handling + the MUSIC heart rule (no voice model needed)."""
import os
import sys
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import speech  # noqa: E402
import spotify  # noqa: E402


class SpeechTextTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(speech.clean("**Coolant** is `92°C` - see [log](http://x)\n- fine"),
                         "Coolant is 92 degrees C - see log fine")
        self.assertEqual(speech.clean("You got 21 mpg at 2400 rpm."), "You got 21 M P G at 2400 R P M.")

    def test_sentences_split_as_text_streams(self):
        done, rest = speech.sentences("Coolant is fine. Oil pressure is go")
        self.assertEqual((done, rest), (["Coolant is fine."], "Oil pressure is go"))
        done, rest = speech.sentences(rest + "od! Anything else")
        self.assertEqual(done, ["Oil pressure is good!"])

    def test_colour_makes_stereo_44k(self):
        out = speech.colour((np.sin(np.arange(2205) / 5) * 8000).astype(np.int16), 22050)
        self.assertEqual(len(out), 2205 * 2 * 2)                     # x2 rate, x2 channels
        self.assertLessEqual(np.abs(out).max(), 32767)

    def test_voices_and_choice(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        with mock.patch.object(speech, "SETTINGS", os.path.join(tmp, "a.json")), \
                mock.patch.object(speech.threading, "Thread"):
            s = speech.Speaker()
            self.assertEqual(s.choice, "german")                   # Ian's pick is the default
            speech.save_settings(voice=True)
            s.set_voice("midatlantic")
            self.assertEqual(speech.load_settings(), {"voice": True, "voice_name": "midatlantic"})
            self.assertEqual(speech.Speaker().choice, "midatlantic")
            s.set_voice("nonsense")
            self.assertEqual(s.choice, "midatlantic")
        for name in speech.VOICES:
            if speech.engine(name) == "piper" or speech.shutil.which("flite") or speech.engine(name) != "flite":
                self.assertTrue(speech.available(name), name)

    def test_vintage_voices_are_instant(self):
        """2026-11-16: Piper took many seconds to start while driving; vintage voices must not."""
        import time
        with mock.patch.object(speech.threading, "Thread"):
            s = speech.Speaker(choice="klatt")
        for name in [n for n in speech.VOICES if speech.engine(n) != "piper" and speech.available(n)]:
            t0 = time.monotonic()
            pcm, rate = s.synth("Coolant is at eighty degrees. Shall I take you home?", name)
            self.assertLess(time.monotonic() - t0, 0.5, name)
            self.assertGreater(len(pcm) / rate, 1.5, name)
            out = speech.colour(pcm, rate, fx="raw")
            self.assertEqual(len(out) % 2, 0)                       # 44.1 kHz stereo, always
        self.assertLess(s.rt, speech.INSTANT)                       # plays the first piece at once


class HeartTest(unittest.TestCase):
    def player(self, context, liked):
        with mock.patch.object(spotify.threading, "Thread"):
            p = spotify.Player()
        p.state = {"item": {"id": "abc", "type": "track"}, "context": {"uri": context}}
        if liked is not None:
            p.liked["abc"] = liked
        return p

    def test_shown_only_for_songs_not_in_liked_songs(self):
        self.assertFalse(self.player("spotify:user:ian:collection", None).show_heart)    # playing Liked Songs
        self.assertFalse(self.player("spotify:playlist:x", True).show_heart)             # liked already
        self.assertTrue(self.player("spotify:playlist:x", False).show_heart)
        self.assertTrue(self.player("spotify:playlist:x", None).show_heart)              # not known yet

    def test_a_tapped_heart_stays_a_moment(self):
        p = self.player("spotify:playlist:x", False)
        p._hearted_at = time.monotonic()
        p.liked["abc"] = True
        self.assertTrue(p.show_heart)
        p._hearted_at -= 10
        self.assertFalse(p.show_heart)


if __name__ == "__main__":
    unittest.main()


class GapFreeTest(unittest.TestCase):
    def test_slow_making_waits_for_the_whole_answer(self):
        writes = []

        class FakeAplay:
            stdin = type("S", (), {"write": lambda self, b: writes.append((time.monotonic(), len(b))),
                                   "flush": lambda self: None, "close": lambda self: None})()

            def poll(self):
                return None

            def wait(self, timeout=None):
                pass

        class NoMusic:
            def hold(self):
                pass

            def release(self):
                pass
        s = speech.Speaker(NoMusic())
        s._open = lambda: FakeAplay()
        piece = np.zeros(int(1.0 * speech.OUT_RATE) * 2, np.int16)        # 1 s of made speech
        s.rt = 1.3                                                       # Pi busy: slower than real time
        t0 = time.monotonic()
        s._audio.put(piece)
        time.sleep(0.4)
        self.assertEqual(writes, [])                                     # 1 s isn't enough to start on
        s._audio.put(piece)
        s._audio.put(None)                                               # answer complete: play it all
        while not writes or len(writes) < 2:
            time.sleep(0.05)
            self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(len(writes), 2)


class TapToFinishTest(unittest.TestCase):
    """2026-11-16: tapping ASK to finish talking left the stop flag set, so every answer
    after it came as text only, never spoken."""

    def test_answer_is_spoken_after_tapping_to_finish(self):
        import assistant

        class Voice:
            ready = True

            def record(self, stop, changed, tag):
                stop.set()                     # Ian taps ASK: done talking
                return b"audio"

            def transcribe(self, rec, vocab):
                return "Tell me about Portland."
        with mock.patch.object(assistant.threading, "Thread"):
            a = assistant.Assistant(None, Voice())
        seen = []

        class Sp:
            available, busy = True, False

            def begin(self):
                pass

            def end(self):
                pass

            music = type("M", (), {"pause": lambda self: None})()
        a.speaker, a.spoken = Sp(), True
        a._ask = lambda text: seen.append(a._stop.is_set())
        a._run()
        self.assertEqual(seen, [False])        # not cancelled: the answer will be spoken
