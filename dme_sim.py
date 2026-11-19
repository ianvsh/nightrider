"""Fake OBD+ Sport DME on a pseudo-terminal, for testing dme.py without the car.

Answers requests the way the real DME does (`61\r` -> `#61 850\r`) with values
for a warm 924S idling. Set `values` to change what it reports.
"""
import os
import random
import threading
import tty

IDLE = {
    "11": "3.3.249",
    "17": "2",
    "22": "4.31",     # ADC volts; x 3.26 = 14.1 V
    "27": "2.1",      # idle switch closed
    "2B": "3.7",      # ~100 kPa
    "2C": "0.45",
    "2D": "2.8",
    "32": "6.10",
    "33": "3.40",
    "34": "32",
    "35": "88",
    "53": "ON",
    "54": "0",
    "56": "3",
    "61": "850",
    "62": "3.1",
    "63": "2.4",
    "64": "10.5-0",     # NA car: no KLR value
    "66": "0",
    "67": "0",
    "68": "130",
    "69": "0000",
    "6E": "3.52",
}


class Simulator:
    def __init__(self, values=None, jitter=True):
        self.values = dict(IDLE if values is None else values)
        self.jitter = jitter
        self.requests = []
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.port = os.ttyname(slave)
        self._slave = slave
        self.running = True

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def close(self):
        self.running = False
        os.close(self._slave)

    def _reply(self, cmd):
        payload = self.values.get(cmd)
        if payload is None:
            return b""
        if self.jitter and cmd == "61":
            payload = str(int(payload) + random.randint(-15, 15))
        return f"#{cmd} {payload}\r".encode()

    def _run(self):
        buf = b""
        while self.running:
            try:
                buf += os.read(self.master, 256)
            except OSError:
                return
            *lines, buf = buf.split(b"\r")
            for line in lines:
                cmd = line.decode("ascii", "replace").split(" ")[0].upper()
                self.requests.append(cmd)
                out = self._reply(cmd)
                if out:
                    os.write(self.master, out)
