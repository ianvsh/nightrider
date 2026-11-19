"""Wi-Fi access: a small JSON/CSV HTTP API plus the phone app (app/) at /.

Endpoints are documented in PROTOCOL.md.
"""
import csv
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_LIMIT = 5000

CSV_MAX_LIMIT = 300000     # ~8 hours at 10 Hz, for whole-drive downloads
APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".webmanifest": "application/manifest+json"}


def _int(q, key, default):
    try:
        return int(q[key][0])
    except (KeyError, ValueError, IndexError):
        return default


def make_handler(logger):
    store = logger.store

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):     # keep the journal quiet
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            if not isinstance(body, bytes):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, name):
            """A file from app/. Only plain names in that folder, nothing else."""
            if name not in os.listdir(APP_DIR) or os.path.splitext(name)[1] not in TYPES:
                return self._json({"error": "not found"}, 404)
            with open(os.path.join(APP_DIR, name), "rb") as f:
                body = f.read()
            # no-cache: phones revalidate, so app updates show up on the next load
            self._send(200, body, TYPES[os.path.splitext(name)[1]], {"Cache-Control": "no-cache"})

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, separators=(",", ":")))

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                self._static("index.html")
            elif path.startswith("/app/"):
                self._static(path[5:])
            elif path == "/api/info":
                self._json(logger.info())
            elif path == "/api/latest":
                self._json(store.latest)
            elif path == "/api/gps":
                self._json(logger.gps.snapshot() if logger.gps else {})
            elif path == "/api/sessions":
                self._json(store.sessions(min(_int(q, "limit", 50), MAX_LIMIT),
                                          _int(q, "before", None)))
            elif path in ("/api/samples", "/api/samples.csv"):
                session = _int(q, "session", None)
                csv_out = path.endswith(".csv")
                rows = store.samples(session, _int(q, "since_id", 0),
                                     min(_int(q, "limit", 500),
                                         CSV_MAX_LIMIT if csv_out else MAX_LIMIT))
                if csv_out:
                    name = f"drive-{session}.csv" if session else "nightrider.csv"
                    extra = {"Content-Disposition": f'attachment; filename="{name}"'} \
                        if "download" in q else {}
                    self._send(200, to_csv(rows), "text/csv", extra)
                else:
                    self._json(rows)
            elif path == "/api/fuel":
                self._json(logger.fuel.summary() if logger.fuel else {})
            elif path == "/api/battery":
                self._json(logger.battery.update() if logger.battery else {})
            elif path == "/api/live":
                self._live()
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            if path != "/api/fuel/fill" or not logger.fuel:
                return self._json({"error": "not found"}, 404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 1000)) or b"{}")
                self._json(logger.fuel.fill(body["gallons"]))
            except (ValueError, KeyError, TypeError) as e:
                self._json({"error": f"bad fill: {e}"}, 400)

        def _live(self):
            """Server-sent events: one `data:` line per new sample."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            last = None
            try:
                while not logger.stop.is_set():
                    s = store.latest
                    if s and s["id"] != last:
                        last = s["id"]
                        self.wfile.write(b"data: " + json.dumps(s).encode() + b"\n\n")
                        self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

    return Handler


def to_csv(rows):
    """Flatten samples to CSV, one column per reading."""
    keys = sorted({k for r in rows for k in r["data"]})
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "time", "session"] + keys)
    for r in rows:
        w.writerow([r["id"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                    r["session"] or ""] + [r["data"].get(k, "") for k in keys])
    return out.getvalue()


def start(logger, port, log=print):
    server = ThreadingHTTPServer(("", port), make_handler(logger))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
