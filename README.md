# Nightrider

A retro touchscreen car computer for 1980s transaxle Porsches (924, 944, 928) with a
Motronic DME, on a Raspberry Pi (built on a 924S):

- logs live engine data from the DME (RPM, timing, airflow, O2, temps...) to SQLite
- 5" touchscreen dash: gauges, trip totals, gas and MPG, GPS map and navigation
- Spotify Connect player (via spotify_player), over AUX or Bluetooth to the stereo
- Claude voice assistant: ask about the car, the last drive, or for music
- phone web app over Wi-Fi or Bluetooth LE (`PROTOCOL.md`)
- built-in terminal for a USB/Bluetooth keyboard

## Hardware

- Raspberry Pi 4 or 5, Raspberry Pi OS Lite (64-bit, trixie)
- 5" DSI touchscreen (800x480)
- DME USB cable (FTDI)
- optional: SIM7670G 4G/GNSS HAT (internet + GPS), USB microphone (voice),
  Bluetooth stereo or AUX (on a Pi 5, AUX needs a USB DAC)

## Install

The install script is still to come. It will set up packages, the Python venv,
the services and the audio config.

## Setup: your own accounts

Nothing personal is stored in this repo. Each install needs:

1. **Spotify Premium** and a free Spotify developer app (developer.spotify.com).
   Add `http://127.0.0.1:8989/login` as its redirect URI, then put its client ID in
   `~/.config/spotify-player/app.toml` (`client_id = "..."`) and pick a device name
   (`[device] name = "..."`). Start the player and approve the login link it prints.
2. **Claude** (optional, for the AI page): `claude auth login` (Pro/Max plan), or an
   Anthropic API key.
3. **Personal settings** (optional): copy `config.example.toml` to
   `~/.config/datalogger/config.toml` and set your name, car and home location.

## Development

Tests run against the DME simulator, so no hardware is needed:

    .venv/bin/python -m unittest discover -s tests

`CLAUDE.md` describes how the code and system fit together.

## License

MIT (see `LICENSE`). Not affiliated with or endorsed by Porsche AG.
