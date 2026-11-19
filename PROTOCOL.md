# Data Logger phone protocol

How a phone reads data from the Pi 4 data logger (hostname `datalogger`).
Both transports serve the same data. Use Wi-Fi for bulk downloads, Bluetooth
for live gauges and small queries when there's no Wi-Fi.

## Data model

- **Sample**: `{"id": 123, "ts": 1792015322.6, "session": 4, "data": {...}}`
  - `id` increases forever; use it to page and to sync incrementally.
  - `ts` is Unix time in seconds (the Pi 4 has no RTC: until it gets network
    time, timestamps after a cold boot without network can be wrong; `id` order is
    always right).
  - `session` is the drive the sample belongs to; `null` = heartbeat logged
    once a minute while the engine is off.
  - `data` holds readings by name, e.g. `rpm`, `coolant_c`, `intake_c`,
    `dme_volts`, `advance_deg`, `knock_retard_deg`, `injector_ms`, `throttle`,
    `o2_volts`, `o2_state`, `airflow_gs`, `idle_valve_pct`, `afr`, `lambda`,
    `fuel_pump`, plus `pi_temp_c`. Names come from `dme.py`'s decoders; a reading
    is missing when the DME didn't report it. Treat unknown keys as extra data.
  - With a GPS fix (`gps.py`, 1 Hz, carried between fixes): `speed_kmh` (km/h;
    apps convert to mph), `lat`, `lon` (decimal degrees), `course_deg` (only when
    moving), `alt_m`, `gps_sats`. With the engine on and a fix: `gear`, 0-5
    (`gear.py`; 0 = not in a gear: stopped, clutch in, neutral, mid-shift).
- **Session**: `{"id": 4, "start": ts, "end": ts, "samples": 5120}`. A session
  starts when engine data appears and ends after 30 s without it.
- **Info**: `{"name", "version", "time", "uptime", "ip": [..], "dme_connected",
  "gps_connected", "gps_fix", "engine_on", "session", "sample_hz", "pi_temp_c", "samples", "last_id", "sessions"}`

Samples are logged at `sample_hz` (10 Hz since 2026-10-15; 4 Hz before) while the engine is on.

## Wi-Fi (HTTP, port 80)

Address: `http://datalogger.local` (mDNS), or the IP from `info.ip`.
On the Pi's own hotspot (SSID `DataLogger`) it is always `http://10.42.0.1`.
All responses are JSON with `Access-Control-Allow-Origin: *`.

| Request | Returns |
| --- | --- |
| `GET /` | the phone app (web app in `app/`: live gauges, drives with charts, status) |
| `GET /api/info` | Info |
| `GET /api/latest` | newest Sample |
| `GET /api/gps` | the live GPS fix right now, engine on or off (sample GPS fields plus `age_s`, seconds since the fix; `{}` with no fix). Used by the screen's MAP page |
| `GET /api/sessions?limit=50&before=ID` | Sessions, newest first; `before` pages back. Each: `{id, start, end, samples, km}`; `km` = distance driven (GPS, gaps bridged, see odometer.py), `null` until an old drive's distance has been worked out |
| `GET /api/samples?session=ID&since_id=N&limit=500` | Samples with id > N, oldest first (limit max 5000). Omit `session` for everything incl. heartbeats |
| `GET /api/samples.csv?...` | same, as CSV with one column per reading (limit max 300000); add `download=1` to get it as a file |
| `GET /api/battery` | car battery charge estimate (no sensor: counted from drives and the Pi's draw, see battery.py): `{soc_pct, estimated: true, capacity_ah, pi_draw_a, hours_left_with_pi_on, parked_since}`; `{}` if unavailable |
| `GET /api/fuel` | fuel used + MPG, estimated from airflow (see fuel.py), US gallons/miles: `{now_mpg, now_gph, drive: {session, miles, gallons, mpg}, tank: {since, miles, gallons, mpg}, range: {gallons_left, tank_gallons, mpg, basis, miles, to_light} (null before the first fill-up; `miles`/`to_light` null with no MPG yet), factor, calibrated, fills: [{ts, gallons, miles?, estimated?, mpg?}]}` (newest fill first; `now_mpg` null under 3 mph, then `now_gph`); `{}` if unavailable |
| `POST /api/fuel/fill` | body `{"gallons": 11.4}`: filled to the click; ends the tank, updates the calibration; returns the new `/api/fuel` |
| `GET /api/live` | Server-sent events: `data: <Sample JSON>` per new sample |

Incremental sync: remember the last `id` you stored, then request
`/api/samples?since_id=<last>&limit=5000` until it returns `[]`.

## Bluetooth LE (GATT)

Advertises as `datalogger` (name may be shortened) with the service UUID below.
No pairing needed.

| Characteristic | UUID | Properties |
| --- | --- | --- |
| Service | `a5f00001-3c1e-4b8a-9d2f-6c1e0d4a7b10` | |
| info | `a5f00002-3c1e-4b8a-9d2f-6c1e0d4a7b10` | read: Info JSON (plain, not chunked) |
| live | `a5f00003-3c1e-4b8a-9d2f-6c1e0d4a7b10` | notify: Sample JSON, max 2 per second |
| query | `a5f00004-3c1e-4b8a-9d2f-6c1e0d4a7b10` | write: JSON command |
| result | `a5f00005-3c1e-4b8a-9d2f-6c1e0d4a7b10` | notify: JSON reply to the last command |

**Chunking.** Notifications (live and result) carry one message split into
chunks of at most 182 bytes. Byte 0 of each chunk is a header: bit 7 (`0x80`)
set = last chunk of the message; bits 0-6 = chunk number mod 128, starting at 0.
The rest is UTF-8. Append chunk bodies until the last one, then parse the JSON.
A chunk numbered 0 starts a new message (drop any partial one).
Android: request an MTU of at least 185 (`requestMtu(517)`); iOS does it itself.

**Commands** (write to query, subscribe to result first):

```
{"cmd":"info"}
{"cmd":"latest"}
{"cmd":"sessions","limit":20,"before":ID}         limit max 100
{"cmd":"samples","session":ID,"since_id":N,"limit":50}   limit max 200
```

Reply: `{"cmd":"samples","ok":true,"result":[...]}` or
`{"ok":false,"error":"..."}`. Send one command at a time and wait for its reply.
Bluetooth is slow (a few KB/s): for whole drives use Wi-Fi.
