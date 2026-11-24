import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import localmusic  # noqa: E402

FRAME = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413)      # one silent 128 kbps MP3 frame


def mp3(path, secs=2, **tags):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(FRAME * int(secs * 38.28))
    if tags:
        from mutagen.easyid3 import EasyID3
        t = EasyID3()
        for k, v in tags.items():
            t[k] = v
        t.save(path)


def song(artist, album, track, title, year=0, path=None):
    return (path or f"/m/{artist}/{album}/{track}.mp3", "/m", artist, album, year, 0, track, title, 180.0)


def lib_of(rows):
    lib = localmusic.Library(db="/nonexistent", log=lambda *a: None, watch=False, mounts=lambda: ["/m"])
    lib.build(rows, ["/m"])
    return lib


class FakeSpotify:
    def __init__(self):
        self.playing, self.device, self.paused = False, "924S", 0
        self.before_play = self.quiet = None
        self._wanted = "x"

    def pause(self):
        self.paused += 1
        self.playing = False

    def toggle(self):
        if self.before_play:
            self.before_play()
        self.playing = not self.playing

    def refresh(self):
        pass


class NamesTest(unittest.TestCase):
    def test_sort_and_letters(self):
        self.assertEqual(localmusic.sort_key("The Beatles"), "beatles")
        self.assertEqual(localmusic.letter("The Beatles"), "B")
        self.assertEqual(localmusic.letter("2Pac"), "#")
        self.assertEqual(localmusic.letter("¡Forward, Russia!"), "F")
        self.assertEqual(localmusic.letter("Ángela Carrasco"), "A")
        self.assertEqual(localmusic.letter("Ñu"), "N")

    def test_from_folders(self):
        g = localmusic.from_path("/m/usb/Pink Floyd/Animals/02 - Dogs.mp3", "/m/usb")
        self.assertEqual((g["artist"], g["album"], g["title"], g["track"]), ("Pink Floyd", "Animals", "Dogs", 2))
        g = localmusic.from_path("/m/usb/Blondie - Atomic.mp3", "/m/usb")
        self.assertEqual((g["artist"], g["title"]), ("Blondie", "Atomic"))


class ScanTest(unittest.TestCase):
    def test_scan_reads_tags_folders_and_updates(self):
        with tempfile.TemporaryDirectory() as d:
            root, db = os.path.join(d, "usb-1"), os.path.join(d, "music.db")
            mp3(f"{root}/x/a.mp3", artist="Kraftwerk", album="Computer World", tracknumber="2/7",
                title="Pocket Calculator", date="1981")
            mp3(f"{root}/Devo/Freedom of Choice/03 Whip It.mp3")          # no tags: from the folders
            mp3(f"{root}/._junk.mp3")                                       # a Mac's sidecar file: skipped
            open(f"{root}/notes.txt", "w").close()
            self.assertEqual(localmusic.scan(root, db, log=lambda *a: None), (2, 2))
            lib = localmusic.Library(db=db, log=lambda *a: None, watch=False, mounts=lambda: [root])
            lib.load()
            self.assertEqual([a[0] for a in lib.artists], ["Devo", "Kraftwerk"])
            k = lib.songs["Kraftwerk"][0]
            self.assertEqual((k["title"], k["track"], k["year"], k["album"]), ("Pocket Calculator", 2, 1981, "Computer World"))
            self.assertGreater(k["secs"], 1)
            self.assertEqual(lib.songs["Devo"][0]["title"], "Whip It")
            # nothing changed: nothing read again; a file gone: dropped
            self.assertEqual(localmusic.scan(root, db, log=lambda *a: None), (2, 0))
            os.remove(f"{root}/x/a.mp3")
            localmusic.scan(root, db, log=lambda *a: None)
            lib.load()
            self.assertEqual([a[0] for a in lib.artists], ["Devo"])

    def test_unplugged_stick_hides_its_songs(self):
        with tempfile.TemporaryDirectory() as d:
            root, db = os.path.join(d, "usb-1"), os.path.join(d, "music.db")
            mp3(f"{root}/A/B/1 x.mp3")
            localmusic.scan(root, db, log=lambda *a: None)
            lib = localmusic.Library(db=db, log=lambda *a: None, watch=False, mounts=lambda: [])
            lib.load()
            self.assertEqual((lib.total, lib.artists, lib.loaded), (0, [], True))


class LibraryTest(unittest.TestCase):
    def test_artists_a_to_z_then_numbers_and_albums_oldest_first(self):
        lib = lib_of([song("The Cure", "Disintegration", 1, "Plainsong", 1989),
                      song("The Cure", "Seventeen Seconds", 2, "Play for Today", 1980),
                      song("The Cure", "Seventeen Seconds", 1, "A Reflection", 1980),
                      song("ABBA", "Arrival", 1, "When I Kissed the Teacher", 1976),
                      song("10cc", "Sheet Music", 1, "Wall Street Shuffle", 1974)])
        self.assertEqual([a[0] for a in lib.artists], ["ABBA", "The Cure", "10cc"])
        self.assertEqual([a[1] for a in lib.artists], ["A", "C", "#"])
        albums = lib.albums("The Cure")
        self.assertEqual([a[0] for a in albums], ["Seventeen Seconds", "Disintegration"])
        self.assertEqual([s["title"] for s in albums[0][2]], ["A Reflection", "Play for Today"])
        self.assertEqual(lib.first_of("C"), 1)
        self.assertIsNone(lib.first_of("Z"))


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        rows = []
        for a in ("Aa", "Bb"):
            for t in range(1, 6):
                p = f"{d}/{a}/{t}.mp3"
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").close()
                rows.append(song(a, "Al", t, f"{a}{t}", path=p))
        self.lib = lib_of(rows)
        self.sent = []
        self.state = os.path.join(d, "state.json")
        self.p = localmusic.LocalPlayer(self.lib, log=lambda *a: None, state_path=self.state,
                                        cmd=["sh", "-c", "cat >/dev/null"])
        self.p._send = lambda line: self.sent.append(line) or True

    def tearDown(self):
        self.tmp.cleanup()

    def test_pick_plays_then_carries_on_in_order(self):
        p = self.p
        p.play_song(["artist", "Aa"], self.lib.songs["Aa"][2])
        self.assertEqual((p.title, p.playing), ("Aa3", True))
        self.assertTrue(self.sent[-1].startswith("L ") and self.sent[-1].endswith("/Aa/3.mp3"))
        p.next()
        self.assertEqual(p.title, "Aa4")
        p._ended(p._gen)                       # the song played to its end
        self.assertEqual(p.title, "Aa5")
        p._ended(p._gen)                       # last one: stops there
        self.assertFalse(p.playing)
        p.previous()
        self.assertEqual(p.title, "Aa4")

    def test_stale_end_is_ignored(self):
        p = self.p
        p.play(["artist", "Aa"])
        old = p._gen
        p.next()                               # the old song's "ended" arrives late
        p._ended(old)
        self.assertEqual(p.title, "Aa2")

    def test_shuffle_only_shuffles_the_stick_and_keeps_the_song(self):
        p = self.p
        p.play(["all"], 0)
        p.toggle_shuffle()
        self.assertTrue(p.shuffle)
        self.assertEqual(p.title, "Aa1")       # the song playing stays
        seen = {p.title}
        for _ in range(9):
            p.next()
            seen.add(p.title)
        self.assertEqual(len(seen), 10)        # every song once before any repeats
        p.toggle_shuffle()
        cur = p.title
        p.next()
        names = [s["title"] for s in self.lib.all_songs()]
        self.assertEqual(names.index(p.title), (names.index(cur) + 1) % 10)

    def test_pause_resume_and_progress(self):
        p = self.p
        p.play(["artist", "Bb"])
        p._start -= 10
        p.pause()
        self.assertFalse(p.playing)
        self.assertAlmostEqual(p.progress, 10000, delta=200)
        self.assertEqual(self.sent[-1], "P")
        p.toggle()
        self.assertTrue(p.playing)
        self.assertEqual(self.sent[-1], "P")

    def test_play_with_nothing_shuffles_everything(self):
        p = self.p
        p.toggle()
        self.assertTrue(p.playing and p.shuffle)
        self.assertEqual(p.context, ["all"])

    def test_back_after_a_few_seconds_restarts_the_song(self):
        p = self.p
        p.play(["artist", "Aa"], 2)
        p._start -= 10
        p.previous()
        self.assertEqual(p.title, "Aa3")
        self.assertLess(p.progress, 1000)

    def test_last_song_comes_back_after_a_restart(self):
        p = self.p
        p.play(["artist", "Bb"], 3)
        p._start -= 42
        p.pause()
        q = localmusic.LocalPlayer(self.lib, log=lambda *a: None, state_path=self.state)
        q._send = lambda line: self.sent.append(line) or True
        self.assertEqual(q.item["title"], "Bb4")
        self.assertFalse(q.playing)
        q.toggle()                             # plays on from where it was
        self.assertTrue(q.playing)
        self.assertEqual(self.sent[-1], "J 42s")
        q.next()
        self.assertEqual(q.title, "Bb5")

    def test_missing_file_says_so(self):
        p = self.p
        os.remove(self.lib.songs["Aa"][0]["path"])
        p.play(["artist", "Aa"], 0)
        self.assertEqual(p.problem, "USB STICK NOT FOUND")
        self.assertFalse(p.playing)

    def test_real_mpg123_takes_commands(self):
        """The real process: starts on first use, takes a load and a pause (dummy output)."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.mp3")
            mp3(path, secs=3)
            lib = lib_of([song("A", "B", 1, "T", path=path)])
            p = localmusic.LocalPlayer(lib, log=lambda *a: None, state_path=os.path.join(d, "s.json"),
                                       cmd=["mpg123", "-R", "-o", "dummy"])
            try:
                p.play(["artist", "A"])
                self.assertTrue(p.playing)
                self.assertIsNone(p._proc.poll())
            finally:
                p.stop()
                p._proc.wait(timeout=5)


class MusicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        p = f"{d}/x.mp3"
        open(p, "w").close()
        self.lib = lib_of([song("A", "B", 1, "T", path=p)])
        self.state = os.path.join(d, "state.json")
        self.local = localmusic.LocalPlayer(self.lib, log=lambda *a: None, state_path=self.state)
        self.local._send = lambda line: True
        self.sp = FakeSpotify()
        self.m = localmusic.Music(self.sp, self.local, log=lambda *a: None, state_path=self.state, device="924S")

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_pauses_spotify_and_spotify_pauses_local(self):
        m, sp = self.m, self.sp
        sp.playing = True
        m.tick()
        m.play_local(["all"])
        self.assertEqual((m.source, sp.paused, sp._wanted), ("local", 1, None))
        self.assertIs(m.active, self.local)
        self.assertTrue(sp.quiet())            # the watchdog won't bring Spotify back meanwhile
        sp.before_play()                       # a Spotify play (playlist, liked songs...)
        self.assertEqual(m.source, "spotify")
        self.assertFalse(self.local.playing)
        self.assertFalse(sp.quiet())

    def test_spotify_starting_by_itself_takes_over(self):
        m, sp = self.m, self.sp
        m.play_local(["all"])
        m._paused_sp_at = time.monotonic() - 30
        m.tick()
        sp.playing = True                      # started from the phone
        m.tick()
        self.assertEqual(m.source, "spotify")
        self.assertFalse(self.local.playing)

    def test_spotify_still_showing_playing_right_after_the_pause_doesnt_take_over(self):
        m, sp = self.m, self.sp
        sp.playing = True
        m.tick()
        m.play_local(["all"])
        sp.playing = False
        m.tick()
        sp.playing = True                      # a stale poll just after
        m.tick()
        self.assertEqual(m.source, "local")

    def test_buttons_go_to_the_active_player_and_source_is_remembered(self):
        m = self.m
        m.play_local(["all"])
        m.toggle()
        self.assertFalse(self.local.playing)
        m.toggle()
        self.assertTrue(self.local.playing)
        again = localmusic.Music(FakeSpotify(), self.local, log=lambda *a: None, state_path=self.state)
        self.assertEqual(again.source, "local")


class BridgeTest(unittest.TestCase):
    def test_three_channels_add_up_and_a_broken_one_drops_out(self):
        import io
        import numpy as np
        import bt

        class P:
            def __init__(self, data):
                self.stdout = io.BytesIO(data)
        n = bt.Bridge.CHUNK // 2
        music = np.full(n * 2, 100, np.int16).tobytes()
        voice = np.zeros(n * 2, np.int16).tobytes()
        local = np.full(n, 50, np.int16).tobytes()                  # only one chunk, then gone
        out = io.BytesIO()

        class Play:
            stdin = out
        b = bt.Bridge(lambda *a: None)
        import threading
        b._mix(P(music), P(voice), Play(), threading.Event(), P(local))
        got = np.frombuffer(out.getvalue(), np.int16)
        self.assertEqual(len(got), n * 2)
        self.assertTrue((got[:n] == 150).all())                      # music + USB music
        self.assertTrue((got[n:] == 100).all())                      # USB channel gone: music alone


if __name__ == "__main__":
    unittest.main()


class ScreensTest(unittest.TestCase):
    """MUSIC > LIBRARY > LOCALLY SAVED MUSIC on the real UI (taps, rendering)."""

    def setUp(self):
        import display
        import screens
        from test_display import FakeFeed, FakePlayer, tab_x
        self.display = display
        display._playlists, display._songs = (time.monotonic(), []), {}     # no Spotify calls
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        rows = []
        for i, a in enumerate(["ABBA", "Camarón de la Isla", "The Cure", "10cc"] + [f"Band {n:02d}" for n in range(20)]):
            for t in range(1, 4):
                p = f"{d}/{i}/{t}.mp3"
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").close()
                rows.append(song(a, "First" if t < 3 else "Second", t, f"{a} song {t}", 1980 + (t > 2), path=p))
        self.lib = lib_of(rows)
        self.local = localmusic.LocalPlayer(self.lib, log=lambda *a: None, state_path=f"{d}/s.json")
        self.sent = []
        self.local._send = lambda line: self.sent.append(line) or True
        self.sp = FakePlayer()
        self.sp.pause = lambda: None
        self.ui = display.UI(FakeFeed(False), None, self.sp)
        self.ui.music = localmusic.Music(self.sp, self.local, log=lambda *a: None, state_path=f"{d}/s.json")
        self.ui.cal = screens.Calibration([[1, 0], [0, 1], [0, 0]])
        self.ui.tap((tab_x("MUSIC"), 8))

    def tearDown(self):
        self.tmp.cleanup()

    def frame(self):
        self.assertEqual(self.ui.render().shape, (480, 800, 3))

    def test_browse_jump_pick_and_back(self):
        D, ui = self.display, self.ui
        ui.tap((240, 140))                                  # LIBRARY
        lib = ui.stack[-1]
        self.assertEqual(lib.rows[0][0], "local")           # first row: the USB stick
        self.frame()
        ui.tap((100, 22 + 5))
        artists = ui.stack[-1]
        self.assertIsInstance(artists, D.LocalArtistsScreen)
        self.frame()
        ui.tap((D.LW - 100, 8))                             # A-Z
        self.assertIsInstance(ui.stack[-1], D.LetterScreen)
        self.frame()
        letters = ui.stack[-1]
        x0, y0, x1, y1 = letters.cell(letters.LETTERS.index("C"))
        ui.tap(((x0 + x1) // 2, (y0 + y1) // 2))
        self.assertIs(ui.stack[-1], artists)
        self.assertEqual(self.lib.artists[artists.top][0], "Camarón de la Isla")
        x0, y0, x1, y1 = letters.cell(letters.LETTERS.index("Z"))   # no Z artists: nothing happens
        ui.tap((D.LW - 100, 8))
        ui.tap(((x0 + x1) // 2, (y0 + y1) // 2))
        self.assertIsInstance(ui.stack[-1], D.LetterScreen)
        ui.tap((10, 8))                                     # BACK
        ui.tap((100, artists.ROW_Y + artists.ROW_H + 4))    # 2nd row from C: The Cure
        songs = ui.stack[-1]
        self.assertIsInstance(songs, D.LocalArtistScreen)
        self.assertEqual([r[0] for r in songs.rows], ["album", "song", "song", "album", "song"])
        self.frame()
        ui.tap((100, songs.ROW_Y + 2 * songs.ROW_H + 4))    # song 2 of "First"
        self.assertEqual((self.local.title, self.local.playing, ui.music.source), ("The Cure song 2", True, "local"))
        time.sleep(songs.PICK_SECS + 0.05)
        self.frame()                                        # back to MUSIC
        self.assertEqual(ui.stack, [])
        ui.tap((150, 140))                                  # SHUFFLE: the stick's, not Spotify's
        self.assertTrue(self.local.shuffle)
        self.assertNotIn("toggle_shuffle", self.sp.calls)
        ui.tap((106, 140))                                  # NEXT
        self.assertNotIn("next", self.sp.calls)
        self.frame()
        ui.tap((240, 140))                                  # LIBRARY again: straight to the playing artist
        self.assertIsInstance(ui.stack[-1], D.LocalArtistScreen)
        self.assertEqual(ui.stack[-1].artist, self.local.artist)
        ui.tap((10, 8))
        self.assertIsInstance(ui.stack[-1], D.LocalArtistsScreen)

    def test_album_row_plays_the_album_and_shuffle_button_plays_artist_shuffled(self):
        D, ui = self.display, self.ui
        ui.stack.append(D.LocalArtistScreen(ui.music, "ABBA"))
        s = ui.stack[-1]
        ui.tap((100, s.ROW_Y + 3 * s.ROW_H + 4))            # the "Second" album's name
        self.assertEqual((self.local.context, self.local.title), (["album", "ABBA", "Second"], "ABBA song 3"))
        ui.stack[:] = [D.LocalArtistScreen(ui.music, "ABBA")]
        ui.tap((D.LW - 20, 8))
        self.assertEqual((self.local.context, self.local.shuffle, ui.stack), (["artist", "ABBA"], True, []))

    def test_music_page_and_drive_card_for_usb_music(self):
        D, ui = self.display, self.ui
        self.frame()                                        # nothing yet
        ui.music.play_local(["all"])
        self.frame()
        ui.page = D.PAGES.index("gauges")
        self.frame()
        self.local.next()                                   # song changes on DRIVE: the card
        self.frame()
        self.assertGreater(ui._card_until, time.monotonic())

    def test_empty_and_indexing_states_render(self):
        D, ui = self.display, self.ui
        empty = lib_of([])
        empty.roots = []
        self.local.library = empty
        s = D.LocalArtistsScreen(ui.music)
        ui.stack.append(s)
        self.frame()
        empty.scanning = "/media/usb-1"
        self.frame()
        ui.tap((D.LW - 20, 8))                              # SHUFFLE with nothing: nothing
        self.assertIs(ui.stack[-1], s)
