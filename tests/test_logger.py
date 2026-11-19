"""Run: python3 -m unittest discover -s tests   (from ~/nightrider)"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dme  # noqa: E402
import logger as lg  # noqa: E402
import web  # noqa: E402
from dme_sim import Simulator  # noqa: E402
from store import SESSION_GAP_SECS, Store  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sessions_split_on_gap(self):
        t = 1000.0
        for i in range(3):
            self.store.add({"rpm": 800}, engine=True, ts=t + i)
        self.store.add({}, engine=False, ts=t + 10)                    # heartbeat
        t2 = t + 3 + SESSION_GAP_SECS + 1
        self.store.add({"rpm": 900}, engine=True, ts=t2)
        sessions = self.store.sessions()
        self.assertEqual([s["samples"] for s in sessions], [1, 3])     # newest first
        self.assertEqual(self.store.samples(sessions[1]["id"])[0]["data"], {"rpm": 800})
        self.assertEqual(len(self.store.samples()), 5)
        self.assertEqual(self.store.counts(), {"samples": 5, "last_id": 5, "sessions": 2})

    def test_paging(self):
        for i in range(10):
            self.store.add({"n": i}, engine=True, ts=1000.0 + i)
        page1 = self.store.samples(limit=4)
        page2 = self.store.samples(since_id=page1[-1]["id"], limit=4)
        self.assertEqual([r["data"]["n"] for r in page1 + page2], list(range(8)))


class BleFramingTest(unittest.TestCase):
    """Pure-Python parts of ble.py (no Bluetooth needed)."""

    def setUp(self):
        try:
            import ble
        except ImportError as e:
            self.skipTest(f"dbus/gi not installed: {e}")
        self.ble = ble

    def test_chunks_roundtrip(self):
        msg = json.dumps({"x": "é" * 300, "n": list(range(100))})
        frames = self.ble.chunks(msg)
        self.assertTrue(all(len(f) <= self.ble.CHUNK for f in frames))
        self.assertTrue(frames[-1][0] & 0x80)
        self.assertFalse(any(f[0] & 0x80 for f in frames[:-1]))
        self.assertEqual([f[0] & 0x7F for f in frames], list(range(len(frames))))
        self.assertEqual(self.ble.unchunk(frames), msg)

    def test_short_message_is_one_final_chunk(self):
        self.assertEqual(self.ble.chunks("{}"), [b"\x80{}"])

    def test_queries(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "t.db"))
            store.add({"rpm": 850}, engine=True)
            fake = type("L", (), {"store": store, "info": lambda self: {"ok": 1}})()
            r = self.ble.handle_query(fake, '{"cmd":"samples","session":1}')
            self.assertTrue(r["ok"])
            self.assertEqual(r["result"][0]["data"]["rpm"], 850)
            self.assertFalse(self.ble.handle_query(fake, "not json")["ok"])
            self.assertFalse(self.ble.handle_query(fake, '{"cmd":"nope"}')["ok"])
            store.close()


class KeyOnOffTest(unittest.TestCase):
    """Replays what the real DME did on 2026-10-14: nonsense while booting,
    good data, one garbage reply at key-off, then silence."""

    def test_boot_and_keyoff_garbage_is_dropped(self):
        boot = {"rpm": 0, "flywheel_teeth": 0, "baro_kpa": 30.9, "dwell_ms": 24607.44, "intake_c": 141.0}
        good = [{"rpm": 850 + i, "flywheel_teeth": 130, "baro_kpa": 102.8, "coolant_c": 90.0} for i in range(5)]
        glitch = {"rpm": 1002, "flywheel_teeth": 130, "baro_kpa": 102.8, "coolant_c": 125.1}
        script = [boot] * 3 + good + [glitch] + [{}] * 3

        class Engine:
            connected = True

            def __init__(self, logger_ref):
                self.ref = logger_ref

            def snapshot(self):
                if not script:
                    self.ref[0].stop.set()
                    return {}
                return script.pop(0)

        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "t.db"))
            ref = []
            logger = lg.Logger(store, Engine(ref))
            ref.append(logger)
            old = lg.SAMPLE_HZ
            lg.SAMPLE_HZ = 500
            try:
                logger.run()
            finally:
                lg.SAMPLE_HZ = old
            engine_rows = store.samples(session=store.sessions()[0]["id"], limit=100)
            self.assertEqual([r["data"]["rpm"] for r in engine_rows], [850, 851, 852, 853, 854])
            self.assertTrue(all(r["data"]["coolant_c"] == 90.0 for r in engine_rows))
            store.close()


class EndToEndTest(unittest.TestCase):
    """Simulated DME -> logger -> HTTP API."""

    def test_http_api(self):
        with tempfile.TemporaryDirectory() as d:
            sim = Simulator().start()
            engine = dme.DME(sim.port, log=lambda m: None).start()
            store = Store(os.path.join(d, "t.db"))
            logger = lg.Logger(store, engine)
            server = web.start(logger, 0)
            port = server.server_address[1]
            t = threading.Thread(target=logger.run)
            t.start()
            try:
                get = lambda p: urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=5).read()
                deadline = time.time() + 10
                while time.time() < deadline and store.session is None:
                    time.sleep(0.2)
                time.sleep(1)
                info = json.loads(get("/api/info"))
                self.assertTrue(info["dme_connected"])
                self.assertTrue(info["engine_on"])
                latest = json.loads(get("/api/latest"))
                self.assertIn("rpm", latest["data"])
                sessions = json.loads(get("/api/sessions"))
                self.assertEqual(len(sessions), 1)
                rows = json.loads(get(f"/api/samples?session={sessions[0]['id']}&limit=3"))
                self.assertGreaterEqual(len(rows), 2)
                csv = get(f"/api/samples.csv?session={sessions[0]['id']}").decode()
                self.assertTrue(csv.startswith("id,time,session,"))
                self.assertIn("rpm", csv.splitlines()[0])
                self.assertIn(b"Data Logger", get("/"))
                self.assertIn(b"EventSource", get("/app/app.js"))
                self.assertIn(b"Logger", get("/app/manifest.webmanifest"))
                for bad in ("/app/../web.py", "/app/nope.js", "/app/%2e%2e%2fweb.py"):
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        get(bad)
                    self.assertEqual(e.exception.code, 404)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/samples.csv"
                                            f"?session={sessions[0]['id']}&download=1", timeout=5) as r:
                    self.assertIn("attachment", r.headers["Content-Disposition"])
                # live stream delivers at least one event
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/live", timeout=5) as r:
                    line = r.readline()
                self.assertTrue(line.startswith(b"data: "))
            finally:
                logger.stop.set()
                t.join(5)
                server.shutdown()
                engine.close()
                sim.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
