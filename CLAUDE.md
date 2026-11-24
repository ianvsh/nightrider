# Nightrider (formerly Data Logger)

Raspberry Pi car computer for a 1980s Porsche with a Motronic DME (developed on a
924S). Logs live engine data, runs a 5" touchscreen dash (gauges, Spotify, maps,
Claude voice assistant, terminal) and serves a phone web app over Wi-Fi and
Bluetooth LE. The phone app is served by the Pi at `/` (`app/`); `PROTOCOL.md`
is the API spec, so keep it in sync with any API change. Install and setup:
`README.md`. Personal notes for one install can go in `CLAUDE.local.md` (not in git).

## Code

- `logger.py` - main loop: samples `dme.DME().snapshot()` at NIGHTRIDER_HZ (10) while the engine
  is on (plus `pi_temp_c`), one heartbeat a minute while off, stores to SQLite and
  hands each sample to listeners (BLE live notify). Settings are env vars in the
  systemd unit. `python3 logger.py --sim` runs against the DME simulator.
- `gps.py` - SIM7670G GNSS on /dev/serial0 (`NIGHTRIDER_GPS`, 0 = off): turns
  GNSS on, reads NMEA (RMC + GGA) at 1 Hz (the module's max), extrapolates speed
  between fixes (`speed_at`). The logger owns the port, so AT commands for the
  cellular side will need to share it later.
- `gear.py` - gear from RPM / speed (`GearFinder`): nearest gear within 12%,
  2 samples to change, 1 s hold through shifts, 0 = no gear (<5 km/h, <1000 rpm,
  no match). Learns ratios slowly while cruising, saves at most every 5 min.
- `store.py` - SQLite (`/var/lib/nightrider/log.db`, WAL, commits every 5 s).
  Sessions = drives, split after 30 s without engine data; each keeps `km` driven (odometer.py).
- `web.py` - HTTP API on port 80 + serves `app/` (stdlib only).
- `app/` - the phone app: plain HTML/CSS/JS, no build step and no CDN (the car
  hotspot has no internet). Live gauges over `/api/live`, drive list + canvas
  charts, status, °C/°F setting. Wi-Fi only: phone browsers block Web Bluetooth
  on plain-http pages. Reading names/labels/warning limits are in `READINGS` in
  `app/app.js`. Add to home screen for an app icon.
- `display.py` - 5" DSI touchscreen (800x480, capacitive ft5x06, fb0 vc4drmfb RGB565).
  Own service, reads the local HTTP API. Retro "Escape from New York" look: drawn
  at 240x160 with a built-in 5x7 pixel font (`GLYPHS`), doubled to 480x320 with
  scanlines; analog tach; wireframe city on the shutdown screen. Writes to the
  LCD framebuffer through mmap, changed rows only (fbtft then sends just those
  lines; a full screen is ~0.2 s at 16 MHz). Tabs always along the top (`TABS`):
  DRIVE (gauges; tapping the page or DRIVE again flips to the other readings),
  MUSIC (Spotify), AI (Claude), SET (the old Status page), TERM.
  Time notation is always lowercase (`duration`: 45s, 3m, 4h2m, 2d4h1m; units ms,
  g/s): pass upper=False when drawing it. Tabs are sized to their
  labels (`tab_rects`) across the full width; no clock up there (it's bottom right of
  the gauges page). Gauges: O2 volts is a tile, the trace below is airflow g/s over
  the last minute (auto-scaled in 20 g/s steps). Bottom row: DRIVE live timer while
  driving; tapping that box (`DRIVE_BOX`) steps LAST DRIVE / TODAY / THIS WEEK (from
  Monday) / THIS MONTH / THIS YEAR, totals from /api/sessions (Feed refetches every
  30 s). Taps count on finger-down;
  pages draw from `TOP` (y 19) down.
- `terminal.py` - TERM page: attaches to the `term` tmux session of
  `nightrider-term.service` (`tmux -L term attach -t term`, falls back to a
  plain `bash -l` if that's not running), so the shell (and Claude Code in it)
  survives display restarts. Underneath: `setsid -c` on a pty,
  TERM=linux, 66x26), emulated by pyte (in `.venv`), drawn in the pixel font
  (colours folded into green/amber/red). USB/Bluetooth keyboards found by evdev
  (hotplug, rescanned every 2 s) and grabbed so keys don't reach the console;
  keys only type while TERM is showing. US layout. Shift+PgUp/PgDn or a finger
  drag (down = back) scrolls: in tmux as mouse-wheel steps (`WHEEL_LINES`), so
  Claude Code scrolls itself and a shell uses tmux copy mode, which a key cancels. The shell lives on across tab switches; if it
  exits, any key starts a new one.
- `pixels.py` - shared retro drawing: colours, 5x7 font (upper + lower case),
  buttons, 240x160 -> 480x320 scale-up.
- `screens.py` - full-screen touch screens from the Status page: touch
  calibration (4 crosses, affine fit, saved to `~/.config/nightrider/touch.json`;
  runs the first time Status is tapped), Wi-Fi list, big-key keyboard (password
  shown as typed, on purpose). `wifi.py` - nmcli wrapper (`sudo -n nmcli`). Hold the SHUT DOWN box (SET page) 4 s = clean shutdown (`sudo -n systemctl poweroff`;
  only that box since 2026-10-15 - long presses elsewhere fired it by accident, `hold_progress`);
  Use it before switching the car off. Touch uses press/release only, no
  coordinates, so it needs no calibration. `python3 display.py --png out.png [page]` renders a frame to a file.
  Labels/limits mirror `READINGS` in `app/app.js`: keep them in step.
- Spotify page: `spotify.py` wraps the `spotify_player` CLI (`Player` polls
  playback every 2 s while the page shows, 15 s otherwise; buttons run in a
  thread). One button row at the bottom: prev / play-pause / next / SHUFFLE (filled
  = on) / SUGGEST (queues 5 songs like this one, picked ahead by suggest.py) / LIBRARY
  (USB music, liked songs, playlists). Heart (right of the artist):
  red = in Liked Songs, outline = not, dim = not known (offline); tap saves, never
  unsaves. Library calls use `me/library` (`is_liked`, `save_liked`): the older
  `me/tracks/contains` and `PUT me/tracks` answer 403 for this dev app (2026-10-16).
  Claude's `like` action uses the same and logs to taste. No volume: set it on the
  amp (Spotify volume fixed at 100). `spotify.run` won't call the CLI unless the
  player is listening on UDP 8080 (`player_up`): with no player running, the CLI
  starts a whole player of its own, which takes the port so spotify.service can't
  start (2026-10-15). `Watchdog` (in the display's Player poll thread, 2026-10-16):
  the player never recovers by itself if it starts offline (token refresh fails ->
  waits for a login on TCP 8989 forever), loses the network (one reconnect try, then
  dead: CLI answers with `[]` devices, no playback) or hits the rodio panic. Healthy =
  the configured device name in `get key devices`; when not (2 checks in a row; login wait at once) and
  accounts.spotify.com is reachable, it restarts spotify.service (`spotify.restart`,
  --no-block, backoff 45 s -> 10 min) and resumes if it was playing in the last
  10 min. Gives up after 3 login waits with internet ("NEEDS A NEW LOGIN": re-login
  below). Status shows in red on the MUSIC page (`Player.problem`). `Visualiser`: 16 spectrum bars from the real audio:
  Audio path (`system/asound.conf` -> `/etc/asound.conf`, NO dmix: spotify_player's
  cpal spins with silence on one): ALSA "default" (Spotify) plays into `snd-aloop`
  Loopback 0,1 and "voicebus" (speech) into 0,2 (loaded at boot,
  `/etc/modules-load.d/snd-aloop.conf`); `bt.Bridge` captures 1,1 + 1,2, sums them
  and plays to `tap_bt` (Bluetooth) or `tap` (AUX + a copy on Loopback 0,0);
  `AudioTap` runs arecord on `plughw:Loopback,1,0` only while the page shows and
  music plays. FFT, log bands, auto-gain. Screen runs at 15 fps while they move. The CLI exits 0 even when
  Spotify refuses, so trust the poll. Claude controls it with the `spotify` tool in
  `approve_mcp.py` (no approval: music only). Spotify Radio can't work with a dev
  key (bulk GET /tracks?ids is 403; station URIs unsupported), so a "radio" is
  Claude picking ~25 similar songs -> `play_songs` (Web API with spotify_player's
  token). The CLI's queue is stale; read it from the Web API. Pixel font shows accented letters as plain ones (`plain_char`).
- USB-stick music (`localmusic.py`, 2026-11-24): MUSIC > LIBRARY (was LISTS) > LOCALLY SAVED
  MUSIC. `system/90-nightrider-usb.rules` mounts any USB stick READ-ONLY at `/media/usb-<uuid>`
  (systemd-mount; unplug = unmounted). `Library` watches /proc/mounts every 5 s, indexes a new
  stick in a niced subprocess (`localmusic.py scan <dir>`, mutagen tags, folders for untagged
  files, only new/changed files after the first time) into `/var/lib/nightrider/music.db`, and
  browses from memory (artists A-Z with "The" ignored and accents folded, # last; albums oldest
  first). `LocalPlayer` = one mpg123 -R kept running, playing to ALSA "localbus" (Loopback 0,3);
  `bt.Bridge` mixes 1,3 in with Spotify and the voice (a broken channel drops out alone). Own
  queue + shuffle (saved in `~/.config/nightrider/local-music.json` with the last song). `Music`
  picks which player the MUSIC page / stereo buttons / voice hold control: starting the stick
  pauses Spotify (one CLI pause, `Player.pause`), any Spotify play (`Player.before_play`) or
  Spotify starting by itself pauses the stick; `Player.quiet` stops the Watchdog resuming Spotify
  over it. Screens: `LocalArtistsScreen` (A-Z -> `LetterScreen`, SHUFFLE = whole stick),
  `LocalArtistScreen` (songs under album headers; tap song = artist from there, tap album name =
  album). With USB music on, LIBRARY opens straight at the playing artist. SUGGEST is Spotify-only.
  Revert point: git tag `before-local-music`.
- Music taste (`taste.py`): Claude's `music_taste` tool (read / remember / forget,
  no approval). Read = saved likes/dislikes/notes (`~/.config/nightrider/music-taste.json`,
  Claude saves what the driver says), hearts (`music-likes.jsonl`, screen or Claude; the
  strongest signal), played-through songs (`music-plays.jsonl`: still going at 90% or
  in the last 30 s when the track changed), early skips (`music-skips.jsonl`: NEXT before 60% of a
  song, from the screen or Claude) and Spotify top artists/tracks + recent likes
  (`spotify.fetch_taste`, cached a day in `music-spotify.json` for no-internet drives).
  Saved likes/dislikes are also appended to the system prompt. The prompt tells
  Claude to read it before choosing music. `make_playlist` (spotify tool) saves a
  private playlist to the user's account and plays it (token has playlist-modify scopes).
- Claude page: `assistant.py` records from the USB mic (arecord,
  energy-based end of speech: stops after 1.8 s of quiet, 3 s while under 2.5 s of speech so a pause after "Hey Claude" doesn't end it), transcribes with faster-whisper `base.en` (car words hinted via `VOCAB`; in
  `.venv`, loaded at start-up), runs `claude -p` (stream-json, `--resume` to keep
  the chat) with cwd here. No prompts for reading/analysis: Bash runs in
  Claude Code's sandbox (`SETTINGS`; needs bubblewrap + socat) - reads anything, writes only
  `~/.cache/nightrider/work` (files older than a day are cleared), no network, never asks.
  The sandbox can't reach 127.0.0.1, so engine data comes from the `api` tool in
  `approve_mcp.py` (GET /api/..., saved to the work folder for Python). Leaving the sandbox
  (sudo, writes elsewhere), Edit/Write and anything not in `ALLOWED` goes to `approve_mcp.py` (`--permission-prompt-tool`), which
  asks the display over `~/.cache/nightrider/approve.sock`; the screen shows
  ALLOW / DENY on any page, no tap in 90 s = deny. Raw permission requests are
  logged to `~/.cache/nightrider/approve.log`. Design rule:
  no system change without a tap. Needs a Claude login (`claude auth login`) or an API key.
- `bt.py` - AUDIO setting (SET page, AUDIO button -> `screens.AudioScreen`): AUX
  (headphone jack) or BLUETOOTH (car stereo, A2DP via bluez-alsa). Settings in
  `~/.config/nightrider/audio.json`. Pairing over BlueZ D-Bus with our own agent
  (accepts, legacy PIN 0000; adapter pairable only while pairing). `Router` (thread in
  the display) reconnects the stereo every 20 s while it's away and routes to
  Bluetooth only while it is connected, by pointing its `Bridge` (copies the `mixbus`
  mix out, see 2026-11-15 ~18:00) at `tap_bt` or `tap` (AUX). Over Bluetooth the
  visualiser gets no audio: a multi of bluealsa + Loopback stalls (Loopback XRUN
  froze the stream, silence), and a plug in front of "bluealsa:" fails (it's already
  a plug), so tap_bt is a `type empty` pass-through. Spotify is never restarted for a
  switch (it plays into the mixer). Stereo gone = bridge stopped, music paused.
- `ble.py` - BLE GATT server on BlueZ D-Bus (python3-dbus + GLib). Messages over
  notifications are chunked (see PROTOCOL.md). No pairing.
- `dme.py`, `dme_sim.py`, `dme_protocol.md` - DME reader, simulator and protocol notes. `dme.py` sends read-only commands: never add 16, 42 or
  72 (they write to the DME), and never touch the FTDI CBUS pins (firmware-update
  mode). The real DME is installed (firmware v3.3.249): sessions 11-16 on 2026-10-15
  are real (13 and 16 are drives, the rest idling); earlier sessions are simulator runs.
  Airflow g/s reads ~1.9x the CFM reading's mass (and above what 2.5 L can breathe:
  179 g/s at 4925 rpm); CFM looks plausible. Not yet resolved.
- Only one program can hold the DME's USB port.
- Tests: `.venv/bin/python -m unittest discover -s tests` (simulator, no hardware
  needed; display tests need the venv for pyte). Tabs are found by name (`tab_x`).
- Uses system Python packages only (python3-serial, python3-dbus, python3-gi,
  python3-pil, python3-evdev, python3-numpy, python3-pyosmium + osmium-tool for the
  map build only).

## System

- Installed by `install.sh` (templates `@USER@`/`@HOME@` in `system/*.service`); `setup.sh`
  = logins, `system/setup-maps.sh` = offline maps, `system/migrate.sh` = copy from an old Pi.
- `nightrider.service` - runs the logger as the install user. Logs: `journalctl -u nightrider`.
  Restart: `sudo systemctl restart nightrider`.
- `nightrider-display.service` - runs `display.py` with `.venv/bin/python`
  (system packages + faster-whisper); waits quietly if no screen.
  5" DSI touchscreen found by `display_auto_detect=1` (stock config.txt).
- `nightrider-term.service` (source `system/`, config `system/tmux-term.conf`) -
  tmux server `-L term`, session `term`, 66x26, no status bar; holds the TERM
  page's shell outside the display's cgroup. Own socket on purpose: the default
  tmux server belongs to spotify.service and dies with its watchdog restarts.
  `exit` in the last shell ends it; Restart=always brings a fresh one in 2 s.
  No prefix key (ctrl-b goes to Claude); scroll by finger drag. A reboot still ends it.
  Tests must not attach to it: `Terminal(tmux="nightrider-test-none")`.
- `spotify.service` - `~/.local/bin/spotify_player` 0.25.1 (prebuilt aarch64
  release; no daemon build, so it runs in a detached tmux session: `tmux attach
  -t spotify`, detach ctrl-b d). Spotify Connect device (name in app.toml), plays through the
  Pi's default ALSA output. Config `~/.config/spotify-player/app.toml`: the user's own
  Spotify developer app (`client_id`; the shared default key is badly
  rate-limited), tokens in `~/.cache/spotify-player`. Needs
  Spotify Premium. ExecStartPre `/usr/local/sbin/spotify-wait-net` (source `system/`)
  holds the start until accounts.spotify.com:443 answers (TimeoutStartSec=infinity),
  so "activating" = waiting for internet. Re-login (headless): stop the service, run it in tmux, open the printed
  URL on a phone, approve, then `curl` the `127.0.0.1:8989/login?code=...` address
  the phone fails to load. Needs internet (car: phone hotspot).
- `bluealsa.service` (bluez-alsa-utils): drop-in `system/bluealsa-nightrider.conf`
  limits it to A2DP source (the Pi sends audio, never receives) and asks for 44.1 kHz
  (`--a2dp-force-audio-cd`). Some stereos reconnect at
  16 kHz SBC (sounded low-res), so `bt.fix_rate` checks and corrects that live.
- `wifi-fallback.service` (`/usr/local/sbin/wifi-fallback`, source in `system/`):
  joins a saved network (NetworkManager), else starts the hotspot
  `nightrider-hotspot` (SSID `Nightrider`, Pi is 10.42.0.1). Retries saved
  networks every 30 min while no phone is on the hotspot. Add networks with
  `sudo nmcli dev wifi connect <SSID> password <pw>`.
- Car audio: the Pi 4 headphone jack is PWM and picks up ground-loop/alternator
  noise once wired into the car (heard 2026-10-15; power was fine, throttled=0x0).
  Not a software problem. Confirmed alternator whine (pitch follows RPM): fix
  with a ground loop isolator on the aux cable.
- Headless (no desktop). SSH is on. Claude Code is `~/.local/bin/claude`.
- Trimmed 2026-10-14: udisks2 masked; e2scrub_reap, e2scrub_all.timer,
  apt-daily(-upgrade).timer, man-db.timer, rpi-eeprom-update disabled (so `apt update`
  by hand before installing); mkvtoolnix + its libs removed. spotify_player's
  `app_refresh_duration_in_ms` = 1000 (hidden TUI; 32 cost ~8% of a core). cloud-init
  and rpi-connect were already off.
- In the car, power can be cut at any time; SQLite WAL handles that, but avoid
  adding frequent writes elsewhere on the SD card.
