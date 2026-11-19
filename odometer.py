"""Distance driven, from the GPS. The car's own odometer doesn't work and its fuel
sender can't be trusted, so these miles are what MPG and "when to fill up" rest on.

Two ways, whichever fits each stretch:
- with a fix, speed x time between samples (the GPS speed is good to ~1%; it
  matched the summed track within 0.5% on 2026-10-16's drives);
- across a gap - no fix (cold start, tunnel, garage) or no samples (logger
  restarted) - the straight line from the last fix to the next one. That's a
  little short on a winding road, but far better than counting nothing. It also
  covers pulling away before the first fix: the last fix before the drive (a
  heartbeat while parked, or the end of the last drive) is the starting point.
"""
import math

MAX_GAP = 2.0          # s: samples further apart than this are a gap, not a stretch at speed
MIN_JUMP_M = 25        # a gap's straight line shorter than this is GPS wander, not driving
MAX_KMH = 200          # a gap that would need more than this is a bad fix: not counted


def metres(a, b):
    """Great-circle distance between (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def fix_of(data):
    lat, lon = (data or {}).get("lat"), (data or {}).get("lon")
    return (lat, lon) if lat is not None and lon is not None else None


class Odometer:
    def __init__(self, last_fix=None):
        self.last_fix = last_fix       # (ts, (lat, lon)) of the newest fix seen, driving or parked
        self.prev = None               # (ts, speed_kmh) of the previous sample with a fix, this drive

    def parked(self, ts, data):
        """A sample while the engine is off (heartbeat): only remembers where the car is."""
        pos = fix_of(data)
        if pos:
            self.last_fix = (ts, pos)
        self.prev = None

    def add(self, ts, data):
        """A sample while driving -> km covered since the one before."""
        pos = fix_of(data)
        if not pos:
            return 0.0
        km = 0.0
        if self.prev and 0 < ts - self.prev[0] <= MAX_GAP:
            km = (self.prev[1] or 0) * (ts - self.prev[0]) / 3600
        elif self.last_fix and ts > self.last_fix[0]:            # first fix after a gap
            m, hours = metres(self.last_fix[1], pos), (ts - self.last_fix[0]) / 3600
            if m >= MIN_JUMP_M and m / 1000 / hours <= MAX_KMH:
                km = m / 1000
        self.prev = (ts, data.get("speed_kmh") or 0)
        self.last_fix = (ts, pos)
        return km
